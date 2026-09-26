"""Machine configuration and multi-catalog loading."""

import os
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from skill_manager.catalog import Catalog, CatalogError, SourceDeclaration, load_catalog
from skill_manager.harness import SUPPORTED_HARNESSES, Harness


class ConsumerOwnership(StrEnum):
    MANAGED = "managed"
    SHARED = "shared"


class ConsumerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    ownership: ConsumerOwnership = ConsumerOwnership.SHARED
    reserved: set[str] = Field(default_factory=set)


class MachineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalogs: list[Path] = Field(min_length=1)
    active_profile: str | None = None
    default_project_profile: str | None = None
    state_dir: Path | None = None
    consumers: dict[str, ConsumerConfig] = Field(default_factory=dict)
    harness_passthrough: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_consumer_names(self) -> MachineConfig:
        supported = {harness.value for harness in SUPPORTED_HARNESSES}
        for name, commands in self.harness_passthrough.items():
            if name not in supported:
                raise ValueError(
                    f"harness_passthrough key '{name}' is not a supported harness: "
                    f"{', '.join(sorted(supported))}"
                )
            for command in commands:
                if not command or command.startswith("-"):
                    raise ValueError(
                        f"invalid harness passthrough command: {command!r};"
                        " only subcommand names are accepted"
                    )
        for name in self.consumers:
            if not _is_identifier(name):
                raise ValueError(f"invalid consumer identifier: {name!r}")
        for consumer in self.consumers.values():
            invalid = sorted(
                name for name in consumer.reserved if not _is_identifier(name)
            )
            if invalid:
                raise ValueError(f"invalid reserved skill name: {invalid[0]!r}")
        return self


class LoadedConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True)

    config_path: Path
    state_dir: Path
    lock_path: Path
    catalog: Catalog
    active_profile: str | None
    default_project_profile: str | None
    consumers: dict[str, ConsumerConfig]
    harness_passthrough: dict[Harness, tuple[str, ...]]


def initialize_configuration(path: Path) -> bool:
    """Create a minimal local catalog when setup has no machine configuration."""
    config_path = path.expanduser().resolve()
    if config_path.exists():
        return False

    config_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path = config_path.with_name("catalog.yaml")
    if catalog_path.exists():
        raise CatalogError(
            f"cannot create starter configuration because {catalog_path} exists"
        )

    local_skills = config_path.parent / "local-skills"
    local_skills.mkdir(exist_ok=True)
    catalog = {
        "sources": {"local": {"path": "local-skills", "tags": ["local"]}},
        "profiles": {"default": {"include": [{"source": "local"}]}},
    }
    created_catalog = False
    created_config = False
    try:
        _write_yaml_exclusive(catalog_path, catalog)
        created_catalog = True
        _write_yaml_exclusive(config_path, {"catalogs": ["catalog.yaml"]})
        created_config = True
    except BaseException:
        if created_catalog:
            catalog_path.unlink(missing_ok=True)
        if created_config:
            config_path.unlink(missing_ok=True)
        raise
    return True


def load_configuration(path: Path) -> LoadedConfiguration:
    """Load machine configuration and merge its catalogs without overrides."""
    config_path = path.expanduser().resolve()
    try:
        raw = yaml.safe_load(config_path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise CatalogError(
            f"cannot read configuration {config_path}: {error}"
        ) from error
    config = MachineConfig.model_validate(raw)
    base = config_path.parent

    merged_sources: dict[str, SourceDeclaration] = {}
    merged_profiles = {}
    for declared_path in config.catalogs:
        catalog_path = _resolve_path(declared_path, base)
        catalog = load_catalog(catalog_path)
        for name, source in catalog.sources.items():
            if name in merged_sources:
                raise CatalogError(f"duplicate source '{name}' across loaded catalogs")
            if source.path is not None:
                source = source.model_copy(
                    update={"path": _resolve_path(source.path, catalog_path.parent)}
                )
            merged_sources[name] = source
        for name, profile in catalog.profiles.items():
            if name in merged_profiles:
                raise CatalogError(f"duplicate profile '{name}' across loaded catalogs")
            merged_profiles[name] = profile

    catalog = Catalog(sources=merged_sources, profiles=merged_profiles)
    for configured_profile in (
        config.active_profile,
        config.default_project_profile,
    ):
        if (
            configured_profile is not None
            and configured_profile not in catalog.profiles
        ):
            raise CatalogError(f"unknown configured profile '{configured_profile}'")

    default_state = (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        / "skill-manager"
    )
    state_dir = _resolve_path(config.state_dir or default_state, base)
    if state_dir in {Path(state_dir.anchor), Path.home()}:
        raise CatalogError(f"unsafe state directory: {state_dir}")
    lock_path = config_path.with_name(f"{config_path.stem}.lock.yaml")
    consumers = {
        name: consumer.model_copy(update={"path": _resolve_path(consumer.path, base)})
        for name, consumer in config.consumers.items()
    }
    harness_passthrough = {
        Harness(name): tuple(commands)
        for name, commands in config.harness_passthrough.items()
    }
    return LoadedConfiguration(
        config_path=config_path,
        state_dir=state_dir,
        lock_path=lock_path,
        catalog=catalog,
        active_profile=config.active_profile,
        default_project_profile=config.default_project_profile,
        consumers=consumers,
        harness_passthrough=harness_passthrough,
    )


def _write_yaml_exclusive(path: Path, value: object) -> None:
    try:
        with path.open("x") as handle:
            yaml.safe_dump(value, handle, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        raise CatalogError(f"cannot create starter file {path}: {error}") from error


def _resolve_path(path: Path, base: Path) -> Path:
    expanded = path.expanduser()
    return (expanded if expanded.is_absolute() else base / expanded).resolve()


def _is_identifier(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
    )
