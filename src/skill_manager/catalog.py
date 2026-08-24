"""Catalog validation, skill discovery, tag inheritance, and profile resolution."""

from collections.abc import Iterable
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class CatalogError(RuntimeError):
    """Raised when a valid catalog cannot be resolved against its sources."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TagRefinement(StrictModel):
    inherit: bool = True
    add: set[str] = Field(default_factory=set)
    remove: set[str] = Field(default_factory=set)

    @model_validator(mode="after")
    def additions_and_removals_must_not_overlap(self) -> Self:
        overlap = self.add & self.remove
        if overlap:
            names = ", ".join(sorted(overlap))
            raise ValueError(f"tags cannot be both added and removed: {names}")
        return self


class SkillDeclaration(StrictModel):
    tags: TagRefinement = Field(default_factory=TagRefinement)


class DiscoveryExclusions(StrictModel):
    paths: list[str] = Field(default_factory=list)
    skills: set[str] = Field(default_factory=set)


class DiscoveryRules(StrictModel):
    include: list[str] = Field(default_factory=lambda: ["**/SKILL.md"], min_length=1)
    exclude: DiscoveryExclusions = Field(default_factory=DiscoveryExclusions)


class SourceDeclaration(StrictModel):
    git: str | None = None
    path: Path | None = None
    ref: str | None = None
    discover: DiscoveryRules = Field(default_factory=DiscoveryRules)
    tags: set[str] = Field(default_factory=set)
    skills: dict[str, SkillDeclaration] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_location(self) -> Self:
        if (self.git is None) == (self.path is None):
            raise ValueError("declare exactly one of 'git' or 'path'")
        if self.git is not None and self.ref is None:
            raise ValueError("Git sources require a 'ref'")
        if self.path is not None and self.ref is not None:
            raise ValueError("local path sources cannot declare a 'ref'")
        return self


class ProfileSelector(StrictModel):
    source: str | None = None
    tags: set[str] = Field(default_factory=set)
    skills: set[str] = Field(default_factory=set)

    @model_validator(mode="after")
    def require_constraint(self) -> Self:
        if self.source is None and not self.tags and not self.skills:
            raise ValueError(
                "a profile selector must constrain source, tags, or skills"
            )
        return self


class ProfileDeclaration(StrictModel):
    include: list[ProfileSelector] = Field(min_length=1)
    exclude: list[ProfileSelector] = Field(default_factory=list)


class Catalog(StrictModel):
    sources: dict[str, SourceDeclaration] = Field(min_length=1)
    profiles: dict[str, ProfileDeclaration] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_identifiers(self) -> Self:
        for kind, names in (("source", self.sources), ("profile", self.profiles)):
            for name in names:
                if not _is_identifier(name):
                    raise ValueError(f"invalid {kind} identifier: {name!r}")
        return self


class ResolvedSkill(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    source: str
    path: Path
    tags: frozenset[str]


class ResolvedSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    requested_ref: str | None
    resolved_revision: str | None
    skills: tuple[ResolvedSkill, ...]


def load_catalog(path: Path) -> Catalog:
    """Load and validate a YAML catalog."""
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise CatalogError(f"cannot read catalog {path}: {error}") from error
    return Catalog.model_validate(raw)


def discover_source(
    source_name: str,
    declaration: SourceDeclaration,
    root: Path,
    resolved_revision: str | None = None,
) -> ResolvedSource:
    """Discover and classify every skill in one checked-out source."""
    skill_files = _matching_skill_files(
        root,
        declaration.discover.include,
        declaration.discover.exclude.paths,
    )
    discovered: list[ResolvedSkill] = []
    seen_names: set[str] = set()

    for skill_file in skill_files:
        name = _read_skill_name(skill_file)
        if not _is_identifier(name):
            raise CatalogError(f"invalid skill name {name!r} in {skill_file}")
        if name in declaration.discover.exclude.skills:
            continue
        if name in seen_names:
            raise CatalogError(
                f"source '{source_name}' contains more than one skill named '{name}'"
            )
        seen_names.add(name)
        refinement = declaration.skills.get(name, SkillDeclaration()).tags
        inherited = declaration.tags if refinement.inherit else set()
        effective_tags = (inherited | refinement.add) - refinement.remove
        discovered.append(
            ResolvedSkill(
                name=name,
                source=source_name,
                path=skill_file.parent,
                tags=frozenset(effective_tags),
            )
        )

    missing_overrides = declaration.skills.keys() - seen_names
    if missing_overrides:
        names = ", ".join(sorted(missing_overrides))
        raise CatalogError(
            f"source '{source_name}' has declarations for undiscovered skills: {names}"
        )

    return ResolvedSource(
        name=source_name,
        requested_ref=declaration.ref,
        resolved_revision=resolved_revision,
        skills=tuple(sorted(discovered, key=lambda skill: skill.name)),
    )


def resolve_profile(
    profile_name: str,
    profile: ProfileDeclaration,
    sources: Iterable[ResolvedSource],
) -> tuple[ResolvedSkill, ...]:
    """Resolve a profile's selectors against discovered source inventory."""
    skills = [skill for source in sources for skill in source.skills]
    included = {
        (skill.source, skill.name): skill
        for skill in skills
        if any(_matches(skill, selector) for selector in profile.include)
    }
    for key, skill in tuple(included.items()):
        if any(_matches(skill, selector) for selector in profile.exclude):
            del included[key]

    if not included:
        raise CatalogError(f"profile '{profile_name}' selects no skills")
    return tuple(
        sorted(included.values(), key=lambda skill: (skill.source, skill.name))
    )


def _matches(skill: ResolvedSkill, selector: ProfileSelector) -> bool:
    return (
        (selector.source is None or skill.source == selector.source)
        and (not selector.tags or selector.tags <= skill.tags)
        and (not selector.skills or skill.name in selector.skills)
    )


def _matching_skill_files(
    root: Path,
    include_patterns: list[str],
    exclude_patterns: list[str],
) -> list[Path]:
    included = {
        match.resolve()
        for pattern in include_patterns
        for match in root.glob(pattern)
        if match.is_file() and match.name == "SKILL.md"
    }
    excluded = {
        match.resolve()
        for pattern in exclude_patterns
        for match in root.glob(pattern)
        if match.is_file()
    }
    source_root = root.resolve()
    matches = included - excluded
    escaped = [match for match in matches if not match.is_relative_to(source_root)]
    if escaped:
        raise CatalogError(f"discovered skill escapes source root: {escaped[0]}")
    return sorted(matches)


def _is_identifier(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
    )


def _read_skill_name(skill_file: Path) -> str:
    try:
        text = skill_file.read_text()
    except OSError as error:
        raise CatalogError(f"cannot read {skill_file}: {error}") from error

    if not text.startswith("---\n"):
        return skill_file.parent.name

    marker = text.find("\n---", 4)
    if marker == -1:
        raise CatalogError(f"unclosed YAML frontmatter in {skill_file}")
    try:
        metadata = yaml.safe_load(text[4:marker]) or {}
    except yaml.YAMLError as error:
        raise CatalogError(
            f"invalid YAML frontmatter in {skill_file}: {error}"
        ) from error
    if not isinstance(metadata, dict):
        raise CatalogError(f"frontmatter must be a mapping in {skill_file}")
    name = metadata.get("name", skill_file.parent.name)
    if not isinstance(name, str) or not name.strip():
        raise CatalogError(f"skill name must be a non-empty string in {skill_file}")
    return name.strip()
