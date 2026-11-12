"""Explicit project declarations and references to reusable curated catalogs."""

from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from skill_manager.catalog import CatalogError, ProfileSelector
from skill_manager.filesystem import digest, is_identifier


class CatalogReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path | None = Field(default=None, description="Local catalog YAML file.")
    git: str | None = Field(default=None, description="Catalog repository URL or path.")
    ref: str | None = Field(
        default=None, description="Approved catalog branch, tag or commit."
    )
    file: Path = Field(
        default=Path("catalog.yaml"),
        description="Catalog file within a Git repository.",
    )

    @model_validator(mode="after")
    def validate_location(self) -> Self:
        if (self.path is None) == (self.git is None):
            raise ValueError("catalog requires exactly one of 'path' or 'git'")
        if self.git is not None and (not self.git.strip() or not self.ref):
            raise ValueError("Git catalogs require non-empty 'git' and 'ref'")
        if self.path is not None and (
            self.ref is not None or self.file != Path("catalog.yaml")
        ):
            raise ValueError("local catalogs use 'path' only, without 'ref' or 'file'")
        if self.file.is_absolute() or ".." in self.file.parts or self.file == Path("."):
            raise ValueError(
                "catalog file must be a relative path inside its repository"
            )
        return self


class ProjectDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    catalog: CatalogReference = Field(
        description="The shared catalog this project uses."
    )
    profile: str = Field(description="Profile selected from the catalog.")
    include: list[ProfileSelector] = Field(
        default_factory=list, description="Additional project selectors."
    )
    exclude: list[ProfileSelector] = Field(
        default_factory=list, description="Selectors removed from the final selection."
    )

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        if not is_identifier(self.profile):
            raise ValueError(f"invalid profile identifier: {self.profile!r}")
        return self

    def fingerprint(self) -> str:
        return digest(self.model_dump())


def load_declaration(path: Path) -> ProjectDeclaration:
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise CatalogError(
            f"cannot read project declaration {path}: {error}"
        ) from error
    if isinstance(raw, dict) and "catalog" not in raw and "profile" in raw:
        raise CatalogError(
            "legacy project declaration: add an explicit 'catalog' reference and "
            "remove 'global_skills'; machine profile bindings are no longer used"
        )
    return ProjectDeclaration.model_validate(raw)


def resolve_path(path: Path, base: Path) -> Path:
    expanded = path.expanduser()
    return (expanded if expanded.is_absolute() else base / expanded).resolve()


def resolve_git_location(location: str, base: Path) -> str:
    """Anchor filesystem remotes; preserve URL and SCP-style Git addresses."""
    if ":" in location:
        return location
    return str(resolve_path(Path(location), base))
