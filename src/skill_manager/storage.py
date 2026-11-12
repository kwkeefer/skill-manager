"""Project locks, reusable Git checkouts, and immutable profile link views."""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from skill_manager.acquire import acquire_locked_source, acquire_source
from skill_manager.catalog import (
    Catalog,
    CatalogError,
    ProfileDeclaration,
    ResolvedSkill,
    ResolvedSource,
    SourceDeclaration,
    discover_source,
    load_catalog,
    resolve_profile,
)
from skill_manager.config import ProjectDeclaration, resolve_git_location, resolve_path
from skill_manager.filesystem import digest, file_lock

Revision = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
LOCK_NAME = ".skill-manager.lock"


class LockedSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    git: str | None
    path: Path | None
    requested_ref: str | None
    resolved_revision: Revision | None

    @model_validator(mode="after")
    def validate_location(self) -> Self:
        if self.git is not None:
            if (
                self.path is not None
                or not self.requested_ref
                or self.resolved_revision is None
            ):
                raise ValueError(
                    "locked Git source requires a ref and revision, without a path"
                )
        elif (
            self.path is None
            or self.requested_ref is not None
            or self.resolved_revision is not None
        ):
            raise ValueError("locked local source requires only a path")
        return self


class LockFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    declaration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_revision: Revision | None
    catalog: Catalog
    sources: dict[str, LockedSource]

    @model_validator(mode="after")
    def validate_sources(self) -> Self:
        if self.sources.keys() != self.catalog.sources.keys():
            raise ValueError("lock sources do not match the catalog snapshot")
        for name, source in self.catalog.sources.items():
            locked = self.sources[name]
            if (locked.git, locked.path, locked.requested_ref) != (
                source.git,
                source.path,
                source.ref,
            ):
                raise ValueError(
                    f"locked source '{name}' identity differs from the catalog snapshot"
                )
        return self

    def fingerprint(self) -> str:
        return digest(self.model_dump())


@dataclass(frozen=True)
class Resolution:
    lock: LockFile
    sources: tuple[ResolvedSource, ...]
    selected: tuple[ResolvedSkill, ...]


def read_lock(root: Path, declaration: ProjectDeclaration) -> LockFile:
    """Reject changed project intent instead of silently following a new source."""
    path = root / LOCK_NAME
    try:
        lock = LockFile.model_validate(yaml.safe_load(path.read_text()))
    except (OSError, yaml.YAMLError) as error:
        raise CatalogError(
            f"cannot read project lock {path}; run skm install: {error}"
        ) from error
    if lock.declaration_digest != declaration.fingerprint():
        raise CatalogError("project declaration changed; run skm update to accept it")
    if (declaration.catalog.git is None) != (lock.catalog_revision is None):
        raise CatalogError(
            "catalog location does not match the project lock; run skm update"
        )
    return lock


def resolve_installation(
    root: Path,
    declaration: ProjectDeclaration,
    state_dir: Path,
    *,
    update: bool = False,
    locked: bool = False,
    network: bool = True,
) -> Resolution:
    """Prepare an installation, populating only shared caches before success.

    Existing locks include a catalog snapshot, so install never follows moving
    catalog or skill refs. Update refreshes both within the catalog's constraints.
    Local skill sources are deliberately live, not immutable content snapshots.
    """
    if update and locked:
        raise CatalogError("update and locked installation are mutually exclusive")
    old_lock = None
    if not update and (locked or (root / LOCK_NAME).exists() or not network):
        old_lock = read_lock(root, declaration)
    reference = declaration.catalog
    catalog_revision = None
    if reference.git is not None:
        origin = resolve_git_location(reference.git, root)
        assert reference.ref is not None
        catalog_root, catalog_revision = cache_git(
            state_dir,
            origin,
            reference.ref,
            old_lock.catalog_revision if old_lock else None,
            network=network,
        )
        catalog_path = catalog_root / reference.file
        if not catalog_path.resolve().is_relative_to(catalog_root.resolve()):
            raise CatalogError("catalog file escapes its repository")
    else:
        assert reference.path is not None
        catalog_path = resolve_path(reference.path, root)
    catalog = old_lock.catalog if old_lock else load_catalog(catalog_path)
    catalog_base = catalog_path.parent
    resolved: list[ResolvedSource] = []
    locks: dict[str, LockedSource] = {}
    for name, source in catalog.sources.items():
        revision = None
        if source.git is not None:
            origin = resolve_git_location(source.git, catalog_base)
            assert source.ref is not None
            source_root, revision = cache_git(
                state_dir,
                origin,
                source.ref,
                old_lock.sources[name].resolved_revision if old_lock else None,
                network=network,
            )
        else:
            assert source.path is not None
            source_root = resolve_path(source.path, catalog_base)
            if not source_root.is_dir():
                raise CatalogError(
                    f"source '{name}' path does not exist: {source_root}"
                )
        locks[name] = LockedSource(
            git=source.git,
            path=source.path,
            requested_ref=source.ref,
            resolved_revision=revision,
        )
        resolved.append(discover_source(name, source, source_root, revision))
    lock = old_lock or LockFile(
        declaration_digest=declaration.fingerprint(),
        catalog_revision=catalog_revision,
        catalog=catalog,
        sources=locks,
    )
    try:
        profile = catalog.profiles[declaration.profile]
    except KeyError as error:
        raise CatalogError(
            f"unknown catalog profile '{declaration.profile}'"
        ) from error
    combined = ProfileDeclaration(
        include=[*profile.include, *declaration.include],
        exclude=[*profile.exclude, *declaration.exclude],
    )
    selected = resolve_profile(declaration.profile, combined, resolved)
    seen: set[str] = set()
    for skill in selected:
        if skill.name in seen:
            raise CatalogError(
                f"project selection contains duplicate skill name '{skill.name}'"
            )
        seen.add(skill.name)
    return Resolution(lock, tuple(resolved), selected)


def cache_git(
    state_dir: Path,
    origin: str,
    ref: str,
    revision: str | None = None,
    *,
    network: bool = True,
) -> tuple[Path, str]:
    """Cache by repository identity and exact revision, never by a source alias."""
    identity = digest(origin)
    parent = state_dir / "sources" / identity
    if revision is not None:
        destination = parent / revision
        if destination.is_dir() and not destination.is_symlink():
            return destination, revision
    if not network:
        raise CatalogError(f"locked checkout is missing for {origin}; run skm install")
    declaration = SourceDeclaration(git=origin, ref=ref)
    with file_lock(state_dir / "locks" / f"source-{identity}.lock"):
        if revision is not None and (parent / revision).is_dir():
            if (parent / revision).is_symlink():
                raise CatalogError(f"source cache is a symlink: {parent / revision}")
            return parent / revision, revision
        acquisition = (
            acquire_locked_source(identity, declaration, revision)
            if revision
            else acquire_source(identity, declaration, Path.cwd())
        )
        with acquisition as checkout:
            assert checkout.resolved_revision is not None
            revision = checkout.resolved_revision
            destination = parent / revision
            if os.path.lexists(destination):
                if destination.is_symlink() or not destination.is_dir():
                    raise CatalogError(f"invalid source cache entry: {destination}")
                return destination, revision
            parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(mkdtemp(prefix=".acquiring-", dir=parent))
            try:
                shutil.copytree(
                    checkout.root, temporary, symlinks=True, dirs_exist_ok=True
                )
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
            return destination, revision


def profile_view_path(
    state_dir: Path, profile: str, selected: tuple[ResolvedSkill, ...]
) -> Path:
    identity = [
        {"name": skill.name, "source": skill.source, "path": str(skill.path)}
        for skill in selected
    ]
    return state_dir / "profiles" / profile / digest(identity)[:16]


def build_profile_view(
    state_dir: Path,
    profile: str,
    selected: tuple[ResolvedSkill, ...],
) -> Path:
    """Publish one immutable view; concurrent callers reuse the winning view."""
    destination = profile_view_path(state_dir, profile, selected)
    with file_lock(state_dir / "locks" / f"view-{digest(str(destination))}.lock"):
        if os.path.lexists(destination):
            if destination.is_symlink() or not destination.is_dir():
                raise CatalogError(f"invalid profile view: {destination}")
            expected = {skill.name: str(skill.path) for skill in selected}
            actual = {
                entry.name: os.readlink(entry) if entry.is_symlink() else None
                for entry in destination.iterdir()
            }
            if actual != expected:
                raise CatalogError(
                    f"profile view was modified externally: {destination}"
                )
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(mkdtemp(prefix=".building-", dir=destination.parent))
        try:
            for skill in selected:
                (temporary / skill.name).symlink_to(
                    skill.path, target_is_directory=True
                )
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return destination
