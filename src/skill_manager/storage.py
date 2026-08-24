"""Persistent source revisions, lock state, and profile views."""

import hashlib
import json
import os
import shutil
from collections.abc import Iterable
from contextlib import ExitStack
from pathlib import Path
from tempfile import mkdtemp

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from skill_manager.acquire import acquire_locked_source, acquire_source
from skill_manager.catalog import (
    CatalogError,
    ResolvedSkill,
    ResolvedSource,
    SourceDeclaration,
    discover_source,
    resolve_profile,
)
from skill_manager.config import LoadedConfiguration


class LockedSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requested_ref: str | None
    resolved_revision: str | None

    @field_validator("resolved_revision")
    @classmethod
    def validate_revision(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) not in {40, 64}
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("resolved revision must be a hexadecimal Git object ID")
        return value


class LockFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    sources: dict[str, LockedSource]


def sync(
    configuration: LoadedConfiguration,
    *,
    update_lock: bool = False,
) -> LockFile:
    """Converge local source storage on the lock, optionally advancing it."""
    if update_lock or not configuration.lock_path.is_file():
        return _update_lock(configuration)

    lock = load_lock(configuration)
    source_store = configuration.state_dir / "sources"
    source_store.mkdir(parents=True, exist_ok=True)
    for name, declaration in configuration.catalog.sources.items():
        if declaration.path is not None:
            if not declaration.path.is_dir():
                raise CatalogError(
                    f"source '{name}' path does not exist: {declaration.path}"
                )
            continue

        locked = lock.sources[name]
        assert locked.resolved_revision is not None
        destination = source_store / name / locked.resolved_revision
        if destination.is_dir():
            continue
        with acquire_locked_source(
            name,
            declaration,
            locked.resolved_revision,
        ) as checkout:
            _copy_source_atomically(checkout.root, destination)
    return lock


def _update_lock(configuration: LoadedConfiguration) -> LockFile:
    """Resolve and persist every configured source, then atomically write the lock."""
    locked: dict[str, LockedSource] = {}
    source_store = configuration.state_dir / "sources"
    source_store.mkdir(parents=True, exist_ok=True)

    with ExitStack() as stack:
        for name, declaration in configuration.catalog.sources.items():
            if declaration.path is not None:
                if not declaration.path.is_dir():
                    raise CatalogError(
                        f"source '{name}' path does not exist: {declaration.path}"
                    )
                locked[name] = LockedSource(
                    requested_ref=None,
                    resolved_revision=None,
                )
                continue

            checkout = stack.enter_context(
                acquire_source(name, declaration, configuration.config_path.parent)
            )
            assert checkout.resolved_revision is not None
            destination = source_store / name / checkout.resolved_revision
            if not destination.exists():
                _copy_source_atomically(checkout.root, destination)
            locked[name] = LockedSource(
                requested_ref=declaration.ref,
                resolved_revision=checkout.resolved_revision,
            )

    lock = LockFile(sources=locked)
    _write_yaml_atomically(configuration.lock_path, lock.model_dump(mode="json"))
    return lock


def load_lock(configuration: LoadedConfiguration) -> LockFile:
    """Load lock state and ensure it still describes the configured sources."""
    try:
        raw = yaml.safe_load(configuration.lock_path.read_text())
    except OSError as error:
        raise CatalogError(
            f"cannot read lock file {configuration.lock_path}; run sync first"
        ) from error
    except yaml.YAMLError as error:
        raise CatalogError(
            f"invalid lock file {configuration.lock_path}: {error}"
        ) from error
    lock = LockFile.model_validate(raw)

    declared_names = set(configuration.catalog.sources)
    if set(lock.sources) != declared_names:
        raise CatalogError(
            "lock sources differ from configured sources; run sync --update-lock"
        )
    for name, declaration in configuration.catalog.sources.items():
        locked = lock.sources[name]
        if locked.requested_ref != declaration.ref:
            raise CatalogError(f"source '{name}' ref changed; run sync --update-lock")
    return lock


def resolve_locked_inventory(
    configuration: LoadedConfiguration,
    lock: LockFile,
) -> tuple[ResolvedSource, ...]:
    """Discover every skill available from locally synchronized sources."""
    resolved_sources: list[ResolvedSource] = []
    for name, declaration in configuration.catalog.sources.items():
        locked = lock.sources[name]
        root = _locked_source_root(configuration, name, declaration, locked)
        resolved_sources.append(
            discover_source(name, declaration, root, locked.resolved_revision)
        )

    return tuple(resolved_sources)


def resolve_locked_profile(
    configuration: LoadedConfiguration,
    lock: LockFile,
) -> tuple[tuple[ResolvedSource, ...], tuple[ResolvedSkill, ...]]:
    """Resolve the configured active profile against locked local inventory."""
    sources = resolve_locked_inventory(configuration, lock)
    profile_name = configuration.active_profile
    if profile_name is None:
        raise CatalogError("no active profile configured")
    profile = configuration.catalog.profiles[profile_name]
    selected = resolve_profile(profile_name, profile, sources)
    duplicate_names = _duplicates(skill.name for skill in selected)
    if duplicate_names:
        raise CatalogError(
            "active profile contains duplicate skill names: "
            + ", ".join(sorted(duplicate_names))
        )
    return sources, selected


def build_profile_view(
    configuration: LoadedConfiguration,
    selected: tuple[ResolvedSkill, ...],
) -> Path:
    """Build an immutable symlink view for the selected profile."""
    identity = [
        {
            "name": skill.name,
            "source": skill.source,
            "path": str(skill.path),
        }
        for skill in selected
    ]
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[
        :16
    ]
    profile_name = configuration.active_profile
    if profile_name is None:
        raise CatalogError("no active profile configured")
    parent = configuration.state_dir / "profiles" / profile_name
    destination = parent / digest
    if destination.is_dir():
        return destination

    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(mkdtemp(prefix=".building-", dir=parent))
    try:
        for skill in selected:
            (temporary / skill.name).symlink_to(skill.path, target_is_directory=True)
        os.replace(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def _locked_source_root(
    configuration: LoadedConfiguration,
    name: str,
    declaration: SourceDeclaration,
    locked: LockedSource,
) -> Path:
    if declaration.path is not None:
        if not declaration.path.is_dir():
            raise CatalogError(
                f"source '{name}' path does not exist: {declaration.path}"
            )
        return declaration.path
    if locked.resolved_revision is None:
        raise CatalogError(f"Git source '{name}' has no locked revision")
    root = configuration.state_dir / "sources" / name / locked.resolved_revision
    if not root.is_dir():
        raise CatalogError(f"locked source '{name}' is missing; run sync")
    return root


def _copy_source_atomically(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(mkdtemp(prefix=".acquiring-", dir=destination.parent))
    shutil.rmtree(temporary)
    try:
        shutil.copytree(source, temporary, symlinks=True)
        os.replace(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _write_yaml_atomically(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x") as handle:
            yaml.safe_dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _duplicates(names: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for name in names:
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    return duplicates
