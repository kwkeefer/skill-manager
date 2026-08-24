"""Project-scoped profile activation and harness launch adapters."""

import json
import os
import shlex
import shutil
import subprocess
from collections.abc import Iterable
from enum import StrEnum
from hashlib import sha256
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from skill_manager.catalog import CatalogError, ResolvedSkill, ResolvedSource
from skill_manager.config import (
    ConsumerConfig,
    ConsumerOwnership,
    LoadedConfiguration,
)
from skill_manager.provision import apply_profile
from skill_manager.storage import build_profile_view, load_lock, resolve_locked_profile


class GlobalSkillsPolicy(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"


class ProjectDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: str
    global_skills: GlobalSkillsPolicy = GlobalSkillsPolicy.DISABLED


class ProjectRefinements(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    include_skills: frozenset[str] = Field(default_factory=frozenset)
    exclude_skills: frozenset[str] = Field(default_factory=frozenset)
    include_tags: frozenset[str] = Field(default_factory=frozenset)
    exclude_tags: frozenset[str] = Field(default_factory=frozenset)


class LoadedProject(BaseModel):
    model_config = ConfigDict(frozen=True)

    root: Path
    declaration_path: Path | None
    declaration: ProjectDeclaration
    refinements: ProjectRefinements = Field(default_factory=ProjectRefinements)


def load_project(
    path: Path,
    default_profile: str | None = None,
) -> LoadedProject:
    """Resolve a project profile from a declaration, local Git config, or default."""
    candidate = path.expanduser().resolve()
    explicit_file = candidate if candidate.is_file() else None
    local_declaration = (
        candidate / ".skill-manager.yaml" if candidate.is_dir() else None
    )
    declaration_path = explicit_file or (
        local_declaration if local_declaration and local_declaration.is_file() else None
    )
    search_from = candidate.parent if explicit_file else candidate
    git_root = _git_root(search_from)
    if declaration_path is None and git_root is not None:
        root_declaration = git_root / ".skill-manager.yaml"
        if root_declaration.is_file():
            declaration_path = root_declaration
    root = declaration_path.parent if declaration_path else (git_root or search_from)

    if declaration_path is not None:
        try:
            raw = yaml.safe_load(declaration_path.read_text())
        except (OSError, yaml.YAMLError) as error:
            raise CatalogError(
                f"cannot read project declaration {declaration_path}: {error}"
            ) from error
        declaration = ProjectDeclaration.model_validate(raw)
        return LoadedProject(
            root=declaration_path.parent,
            declaration_path=declaration_path,
            declaration=declaration,
            refinements=_git_refinements(root),
        )

    profile = _git_profile(root) or default_profile
    if profile is None:
        raise CatalogError("project has no profile; run 'skm project use <profile>'")
    return LoadedProject(
        root=root,
        declaration_path=None,
        declaration=ProjectDeclaration(profile=profile),
        refinements=_git_refinements(root),
    )


def set_project_refinements(
    path: Path,
    refinements: ProjectRefinements,
) -> Path:
    """Persist clone-local skill and tag refinements in Git configuration."""
    root = _git_root(path.expanduser().resolve())
    if root is None:
        raise CatalogError("project refinements currently require a Git repository")
    value = json.dumps(
        {
            "include_skills": sorted(refinements.include_skills),
            "exclude_skills": sorted(refinements.exclude_skills),
            "include_tags": sorted(refinements.include_tags),
            "exclude_tags": sorted(refinements.exclude_tags),
        },
        separators=(",", ":"),
    )
    _run_git(root, "config", "--local", "skill-manager.refinements", value)
    return root


def set_project_profile(path: Path, profile: str) -> Path:
    """Store a clone-local profile binding in .git/config."""
    candidate = path.expanduser().resolve()
    root = _git_root(candidate)
    if root is None:
        raise CatalogError("project use currently requires a Git repository")
    _run_git(root, "config", "--local", "skill-manager.profile", profile)
    return root


def apply_project(
    configuration: LoadedConfiguration,
    project: LoadedProject,
) -> tuple[LoadedConfiguration, Path, int]:
    """Apply a locked profile into shared project-local skill directories."""
    if project.declaration.profile not in configuration.catalog.profiles:
        raise CatalogError(f"unknown project profile '{project.declaration.profile}'")
    project_configuration = _project_configuration(configuration, project)
    lock = load_lock(project_configuration)
    sources, selected = resolve_locked_profile(project_configuration, lock)
    selected = refine_project_selection(sources, selected, project.refinements)
    view = build_profile_view(project_configuration, selected)
    apply_profile(project_configuration, selected, view)
    _update_git_excludes(project.root, {skill.name for skill in selected})
    return project_configuration, view, len(selected)


def refine_project_selection(
    sources: tuple[ResolvedSource, ...],
    base: tuple[ResolvedSkill, ...],
    refinements: ProjectRefinements,
) -> tuple[ResolvedSkill, ...]:
    """Apply project-local tag and skill refinements to a base profile."""
    inventory = tuple(skill for source in sources for skill in source.skills)
    selected = {(skill.source, skill.name): skill for skill in base}

    for skill in inventory:
        key = (skill.source, skill.name)
        if skill.tags & refinements.include_tags:
            selected[key] = skill
    for key, skill in tuple(selected.items()):
        if skill.tags & refinements.exclude_tags:
            del selected[key]
    for skill in inventory:
        if skill.name in refinements.include_skills:
            selected[(skill.source, skill.name)] = skill
    for key, skill in tuple(selected.items()):
        if skill.name in refinements.exclude_skills:
            del selected[key]

    duplicate_names = _duplicate_names(selected.values())
    if duplicate_names:
        raise CatalogError(
            "project selection contains duplicate skill names: "
            + ", ".join(sorted(duplicate_names))
        )
    if not selected:
        raise CatalogError("project refinements select no skills")
    return tuple(
        sorted(selected.values(), key=lambda skill: (skill.source, skill.name))
    )


def generate_adapters(
    configuration: LoadedConfiguration,
    project: LoadedProject,
) -> Path:
    """Generate PATH shims that launch each harness with project isolation."""
    adapter_dir = _adapter_dir(configuration, project)
    bin_dir = adapter_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    manager = shutil.which("skm")
    if manager is None:
        raise CatalogError("cannot locate the skm executable")
    for harness in ("pi", "claude", "codex"):
        script = (
            "#!/bin/sh\n"
            f"exec {shlex.quote(manager)} project run "
            f"{shlex.quote(str(configuration.config_path))} "
            f"{shlex.quote(str(project.root))} "
            f'{harness} -- "$@"\n'
        )
        path = bin_dir / harness
        temporary = bin_dir / f".{harness}.{os.getpid()}.tmp"
        temporary.write_text(script)
        temporary.chmod(0o755)
        os.replace(temporary, path)
    return bin_dir


def installed_harnesses(configuration: LoadedConfiguration) -> tuple[str, ...]:
    """Return harnesses with real executables outside manager-owned state."""
    search = _external_search_path(configuration)
    return tuple(
        harness
        for harness in ("pi", "claude", "codex")
        if shutil.which(harness, path=search) is not None
    )


def generate_global_adapters(
    configuration: LoadedConfiguration,
    harnesses: Iterable[str],
) -> Path:
    """Generate stable shims for selected installed harnesses."""
    selected = tuple(harnesses)
    invalid = set(selected) - {"pi", "claude", "codex"}
    if invalid:
        raise CatalogError(f"unsupported harness: {min(invalid)}")
    bin_dir = configuration.state_dir / "shims" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for harness in ("pi", "claude", "codex"):
        if harness not in selected:
            (bin_dir / harness).unlink(missing_ok=True)
    if not selected:
        return bin_dir

    manager = shutil.which("skm", path=_external_search_path(configuration))
    if manager is None:
        raise CatalogError("cannot locate the skm executable outside managed shims")
    for harness in selected:
        script = (
            "#!/bin/sh\n"
            f"exec {shlex.quote(manager)} project run "
            f"{shlex.quote(str(configuration.config_path))} "
            f'"$PWD" {harness} -- "$@"\n'
        )
        path = bin_dir / harness
        temporary = bin_dir / f".{harness}.{os.getpid()}.tmp"
        temporary.write_text(script)
        temporary.chmod(0o755)
        os.replace(temporary, path)
    return bin_dir


def environment_export(bin_dir: Path) -> str:
    """Return POSIX shell code suitable for eval from direnv or shell startup."""
    return f'export PATH={shlex.quote(str(bin_dir))}:"$PATH"'


def run_harness(
    configuration: LoadedConfiguration,
    project: LoadedProject,
    harness: str,
    arguments: list[str],
) -> None:
    """Apply project state and replace this process with an isolated harness."""
    if harness not in {"pi", "claude", "codex"}:
        raise CatalogError(f"unsupported harness: {harness}")
    apply_project(configuration, project)
    executable = _find_real_executable(configuration, project, harness)
    command, environment = harness_command(
        executable,
        harness,
        arguments,
        project,
    )
    os.execvpe(executable, command, environment)


def harness_command(
    executable: str,
    harness: str,
    arguments: list[str],
    project: LoadedProject,
) -> tuple[list[str], dict[str, str]]:
    """Construct one harness launch without executing it."""
    command = [executable]
    environment = dict(os.environ)
    if project.declaration.global_skills is GlobalSkillsPolicy.ENABLED:
        return [*command, *arguments], environment

    if harness == "pi":
        command.append("--no-skills")
        for skill_path in _pi_project_skill_paths(project.root, Path.cwd()):
            command.extend(("--skill", str(skill_path)))
    elif harness == "claude":
        command.extend(("--setting-sources", "project,local"))
    elif harness == "codex":
        disabled = _codex_home_skill_overrides()
        if disabled:
            command.extend(("-c", f"skills.config={disabled}"))
    else:
        raise CatalogError(f"unsupported harness: {harness}")
    command.extend(arguments)
    return command, environment


def _project_configuration(
    configuration: LoadedConfiguration,
    project: LoadedProject,
) -> LoadedConfiguration:
    identity = sha256(str(project.root).encode()).hexdigest()[:16]
    consumers = {
        f"project-{identity}-agents": ConsumerConfig(
            path=project.root / ".agents" / "skills",
            ownership=ConsumerOwnership.SHARED,
        ),
        f"project-{identity}-claude": ConsumerConfig(
            path=project.root / ".claude" / "skills",
            ownership=ConsumerOwnership.SHARED,
        ),
    }
    return configuration.model_copy(
        update={
            "active_profile": project.declaration.profile,
            "consumers": consumers,
        }
    )


def _adapter_dir(
    configuration: LoadedConfiguration,
    project: LoadedProject,
) -> Path:
    identity = sha256(str(project.root).encode()).hexdigest()[:16]
    return configuration.state_dir / "project-adapters" / identity


def _find_real_executable(
    configuration: LoadedConfiguration,
    project: LoadedProject,
    harness: str,
) -> str:
    search = _external_search_path(configuration)
    executable = shutil.which(harness, path=search)
    if executable is None:
        raise CatalogError(
            f"cannot find real '{harness}' executable outside adapter PATH"
        )
    return executable


def _external_search_path(configuration: LoadedConfiguration) -> str:
    state_dir = configuration.state_dir.resolve()
    return os.pathsep.join(
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if not Path(entry).resolve().is_relative_to(state_dir)
    )


def _pi_project_skill_paths(project_root: Path, cwd: Path) -> list[Path]:
    current = cwd.resolve()
    root = project_root.resolve()
    if not current.is_relative_to(root):
        current = root
    directories: list[Path] = []
    while True:
        for relative in (Path(".agents/skills"), Path(".pi/skills")):
            candidate = current / relative
            if candidate.is_dir():
                directories.append(candidate)
        if current == root:
            break
        current = current.parent
    return directories


def _git_root(path: Path) -> Path | None:
    try:
        output = _run_git(path, "rev-parse", "--show-toplevel")
    except CatalogError:
        return None
    return Path(output).resolve()


def _git_refinements(path: Path) -> ProjectRefinements:
    root = _git_root(path)
    if root is None:
        return ProjectRefinements()
    try:
        raw = _run_git(
            root,
            "config",
            "--local",
            "--get",
            "skill-manager.refinements",
        )
    except CatalogError:
        return ProjectRefinements()
    try:
        return ProjectRefinements.model_validate_json(raw)
    except ValueError as error:
        raise CatalogError(
            "invalid skill-manager.refinements in local Git config"
        ) from error


def _git_profile(root: Path) -> str | None:
    try:
        return _run_git(
            root,
            "config",
            "--local",
            "--get",
            "skill-manager.profile",
        )
    except CatalogError:
        return None


def _run_git(cwd: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise CatalogError("git is required for project profile bindings") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or error.stdout.strip() or "Git command failed"
        raise CatalogError(detail) from error
    return result.stdout.strip()


def _update_git_excludes(project_root: Path, skill_names: set[str]) -> None:
    git_root = _git_root(project_root)
    if git_root is None:
        return
    git_path = Path(_run_git(git_root, "rev-parse", "--git-path", "info/exclude"))
    if not git_path.is_absolute():
        git_path = git_root / git_path
    identity = sha256(str(project_root.resolve()).encode()).hexdigest()[:16]
    begin = f"# BEGIN skill-manager:{identity}"
    end = f"# END skill-manager:{identity}"
    try:
        lines = git_path.read_text().splitlines()
    except FileNotFoundError:
        lines = []
    except OSError as error:
        raise CatalogError(
            f"cannot read Git exclude file {git_path}: {error}"
        ) from error

    if begin in lines:
        start = lines.index(begin)
        try:
            stop = lines.index(end, start + 1)
        except ValueError as error:
            raise CatalogError(f"unclosed skill-manager block in {git_path}") from error
        del lines[start : stop + 1]

    project_relative = project_root.resolve().relative_to(git_root)
    patterns = []
    for directory in (Path(".agents/skills"), Path(".claude/skills")):
        for name in sorted(skill_names):
            relative = project_relative / directory / name
            patterns.append("/" + _escape_gitignore(relative.as_posix()))
    if patterns:
        if lines and lines[-1]:
            lines.append("")
        lines.extend((begin, *patterns, end))

    git_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = git_path.with_name(f".{git_path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text("\n".join(lines) + ("\n" if lines else ""))
        os.replace(temporary, git_path)
    finally:
        temporary.unlink(missing_ok=True)


def _escape_gitignore(value: str) -> str:
    special = set("\\!#*?[] ")
    return "".join(
        f"\\{character}" if character in special else character for character in value
    )


def _duplicate_names(skills: Iterable[ResolvedSkill]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for skill in skills:
        if skill.name in seen:
            duplicates.add(skill.name)
        seen.add(skill.name)
    return duplicates


def _codex_home_skill_overrides() -> str:
    root = Path.home() / ".agents" / "skills"
    if not root.is_dir():
        return ""
    paths = [
        str(skill_file)
        for entry in sorted(root.iterdir())
        if (skill_file := entry / "SKILL.md").is_file()
    ]
    entries = ",".join(f"{{path={json.dumps(path)},enabled=false}}" for path in paths)
    return f"[{entries}]"
