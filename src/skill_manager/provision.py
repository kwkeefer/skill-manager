"""Guarded profile materialization into shared consumer directories."""

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from skill_manager.catalog import CatalogError, ResolvedSkill
from skill_manager.config import ConsumerConfig, ConsumerOwnership, LoadedConfiguration
from skill_manager.storage import _write_yaml_atomically


class ConsumerManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    consumer_path: Path
    entries: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_entry_names(self) -> ConsumerManifest:
        invalid = sorted(name for name in self.entries if not _is_identifier(name))
        if invalid:
            raise ValueError(f"invalid manifest skill name: {invalid[0]!r}")
        return self


def apply_profile(
    configuration: LoadedConfiguration,
    selected: tuple[ResolvedSkill, ...],
    view: Path,
) -> None:
    """Apply selected profile entries to every configured consumer."""
    for consumer_name, consumer in configuration.consumers.items():
        _apply_consumer(configuration, consumer_name, consumer, selected, view)


def doctor(
    configuration: LoadedConfiguration,
    selected: tuple[ResolvedSkill, ...],
) -> list[str]:
    """Return ownership and materialization problems without changing state."""
    issues: list[str] = []
    selected_by_name = {skill.name: skill for skill in selected}
    selected_names = set(selected_by_name)
    for consumer_name, consumer in configuration.consumers.items():
        conflicts = selected_names & consumer.reserved
        for name in sorted(conflicts):
            issues.append(
                f"consumer '{consumer_name}': active skill '{name}' is reserved"
            )

        manifest = _load_manifest(configuration, consumer_name, required=False)
        if manifest is None:
            issues.append(f"consumer '{consumer_name}': no apply manifest")
            continue
        if manifest.consumer_path != consumer.path:
            issues.append(f"consumer '{consumer_name}': configured path changed")
            continue
        manifest_names = set(manifest.entries)
        for name in sorted(manifest_names - selected_names):
            issues.append(
                f"consumer '{consumer_name}': apply manifest has stale skill '{name}'"
            )
        for name in sorted(selected_names - manifest_names):
            issues.append(
                f"consumer '{consumer_name}': active skill '{name}' is not applied"
            )
        for name, expected in manifest.entries.items():
            entry = consumer.path / name
            if not os.path.lexists(entry):
                issues.append(
                    f"consumer '{consumer_name}': managed skill '{name}' is missing"
                )
            elif not entry.is_symlink() or os.readlink(entry) != expected:
                issues.append(
                    f"consumer '{consumer_name}': managed skill '{name}' changed externally"
                )
            elif (
                name in selected_by_name
                and entry.resolve() != selected_by_name[name].path.resolve()
            ):
                issues.append(
                    f"consumer '{consumer_name}': managed skill '{name}' points to stale source"
                )

        if consumer.path.is_dir() and consumer.ownership is ConsumerOwnership.MANAGED:
            known = set(manifest.entries) | consumer.reserved
            for entry in consumer.path.iterdir():
                if entry.name not in known:
                    issues.append(
                        f"consumer '{consumer_name}': unmanaged entry '{entry.name}'"
                    )
    return issues


def _apply_consumer(
    configuration: LoadedConfiguration,
    consumer_name: str,
    consumer: ConsumerConfig,
    selected: tuple[ResolvedSkill, ...],
    view: Path,
) -> None:
    selected_names = {skill.name for skill in selected}
    conflicts = selected_names & consumer.reserved
    if conflicts:
        raise CatalogError(
            f"consumer '{consumer_name}' reserves active skills: "
            + ", ".join(sorted(conflicts))
        )
    if consumer.path.is_symlink():
        raise CatalogError(
            f"consumer '{consumer_name}' directory is itself a symlink; refusing to modify it"
        )
    filesystem_root = Path(consumer.path.anchor)
    if consumer.path in {filesystem_root, Path.home()}:
        raise CatalogError(f"unsafe consumer path: {consumer.path}")
    consumer.path.mkdir(parents=True, exist_ok=True)

    manifest = _load_manifest(configuration, consumer_name, required=False)
    if manifest is None:
        manifest = ConsumerManifest(consumer_path=consumer.path)
    elif manifest.consumer_path != consumer.path:
        raise CatalogError(
            f"consumer '{consumer_name}' path changed since last apply; refusing stale cleanup"
        )

    desired = {name: str(view / name) for name in selected_names}
    retained_manifest = {
        name: target
        for name, target in manifest.entries.items()
        if name not in consumer.reserved
    }
    _validate_existing_entries(
        consumer_name,
        consumer,
        retained_manifest,
        desired,
    )

    previous = {
        name: target
        for name, target in retained_manifest.items()
        if os.path.lexists(consumer.path / name)
    }
    stale = set(retained_manifest) - set(desired)
    try:
        for name in stale:
            (consumer.path / name).unlink(missing_ok=True)
        for name, target in desired.items():
            _replace_symlink(consumer.path / name, target)
        new_manifest = ConsumerManifest(
            consumer_path=consumer.path,
            entries=desired,
        )
        _write_yaml_atomically(
            _manifest_path(configuration, consumer_name),
            new_manifest.model_dump(mode="json"),
        )
    except BaseException:
        _rollback(consumer.path, desired, previous)
        raise


def _validate_existing_entries(
    consumer_name: str,
    consumer: ConsumerConfig,
    previous: dict[str, str],
    desired: dict[str, str],
) -> None:
    for name, expected in previous.items():
        entry = consumer.path / name
        if os.path.lexists(entry) and (
            not entry.is_symlink() or os.readlink(entry) != expected
        ):
            raise CatalogError(
                f"consumer '{consumer_name}' managed entry '{name}' changed externally"
            )

    for name in set(desired) - set(previous):
        entry = consumer.path / name
        if os.path.lexists(entry):
            raise CatalogError(
                f"consumer '{consumer_name}' entry '{name}' already exists and is not managed"
            )

    if consumer.ownership is ConsumerOwnership.MANAGED:
        known = set(previous) | set(consumer.reserved)
        unknown = sorted(
            entry.name for entry in consumer.path.iterdir() if entry.name not in known
        )
        if unknown:
            raise CatalogError(
                f"consumer '{consumer_name}' contains unmanaged entries: "
                + ", ".join(unknown)
            )


def _rollback(
    consumer_path: Path,
    desired: dict[str, str],
    previous: dict[str, str],
) -> None:
    for name, target in desired.items():
        entry = consumer_path / name
        if entry.is_symlink() and os.readlink(entry) == target:
            entry.unlink()
    for name, target in previous.items():
        _replace_symlink(consumer_path / name, target)


def _replace_symlink(path: Path, target: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_manifest(
    configuration: LoadedConfiguration,
    consumer_name: str,
    *,
    required: bool,
) -> ConsumerManifest | None:
    path = _manifest_path(configuration, consumer_name)
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        if required:
            raise CatalogError(f"missing consumer manifest: {path}") from None
        return None
    except (OSError, yaml.YAMLError) as error:
        raise CatalogError(f"cannot read consumer manifest {path}: {error}") from error
    return ConsumerManifest.model_validate(raw)


def _manifest_path(
    configuration: LoadedConfiguration,
    consumer_name: str,
) -> Path:
    return configuration.state_dir / "manifests" / f"{consumer_name}.yaml"


def _is_identifier(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
    )
