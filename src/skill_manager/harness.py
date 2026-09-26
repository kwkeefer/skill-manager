"""Harness launch classification, isolation flags, and process hand-off.

Shims intercept every launch of a supported harness. Only agent sessions may be
given isolation flags: harnesses dispatch their own management subcommands from
the first argument, so injecting flags there turns `pi update` into a session
prompted with the word `update`. This module decides whether one launch is an
isolated session or an untouched pass-through, and builds the resulting command.
"""

import json
import os
import shlex
import shutil
import sys
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Final, NoReturn

from skill_manager.catalog import CatalogError


class Harness(StrEnum):
    """Agent CLI whose launch Skill Manager can intercept."""

    PI = "pi"
    CLAUDE = "claude"
    CODEX = "codex"


SUPPORTED_HARNESSES: Final[tuple[Harness, ...]] = tuple(Harness)

LAUNCH_MODE_ENV: Final[str] = "SKM_LAUNCH_MODE"


class LaunchMode(StrEnum):
    """Operator override for interception, read from :data:`LAUNCH_MODE_ENV`."""

    AUTO = "auto"
    ISOLATE = "isolate"
    PASSTHROUGH = "passthrough"


class LaunchKind(StrEnum):
    """Disposition of one intercepted harness launch."""

    ISOLATED_SESSION = "isolated-session"
    PASSTHROUGH = "passthrough"


METADATA_FLAGS: Final[frozenset[str]] = frozenset(
    {"-h", "--help", "-v", "--version", "-V"}
)

# Top-level subcommands a harness dispatches before agent-session parsing. These
# are pinned to the harness releases this tool was validated against; machine
# configuration `harness_passthrough` adds subcommands introduced later.
MANAGEMENT_SUBCOMMANDS: Final[Mapping[Harness, frozenset[str]]] = {
    Harness.PI: frozenset(
        {
            "auth",
            "config",
            "install",
            "list",
            "remove",
            "uninstall",
            "update",
        }
    ),
    Harness.CLAUDE: frozenset(
        {
            "agents",
            "completion",
            "doctor",
            "install",
            "mcp",
            "migrate-installer",
            "plugin",
            "plugins",
            "setup-token",
            "uninstall",
            "update",
            "upgrade",
        }
    ),
    Harness.CODEX: frozenset(
        {
            "apply",
            "cloud",
            "completion",
            "config",
            "debug",
            "exec",
            "execpolicy",
            "features",
            "feedback",
            "fork",
            "login",
            "logout",
            "mcp",
            "mcp-server",
            "resume",
            "sandbox",
            "update",
            "upgrade",
        }
    ),
}


def parse_harness(value: str) -> Harness:
    """Convert a harness name from configuration, PATH, or argv into its enum."""
    try:
        return Harness(value)
    except ValueError as error:
        supported = ", ".join(harness.value for harness in SUPPORTED_HARNESSES)
        raise CatalogError(
            f"unsupported harness '{value}'; supported harnesses: {supported}"
        ) from error


def resolve_launch_mode(environ: Mapping[str, str] = os.environ) -> LaunchMode:
    """Read the interception override, failing on unrecognized values."""
    raw = environ.get(LAUNCH_MODE_ENV)
    if raw is None or not raw.strip():
        return LaunchMode.AUTO
    try:
        return LaunchMode(raw.strip().lower())
    except ValueError as error:
        supported = ", ".join(mode.value for mode in LaunchMode)
        raise CatalogError(
            f"invalid {LAUNCH_MODE_ENV}='{raw}'; supported modes: {supported}"
        ) from error


def classify_launch(
    harness: Harness,
    arguments: Sequence[str],
    *,
    mode: LaunchMode = LaunchMode.AUTO,
    extra_subcommands: Iterable[str] = (),
) -> LaunchKind:
    """Decide whether one harness invocation is an isolable agent session."""
    if mode is LaunchMode.PASSTHROUGH:
        return LaunchKind.PASSTHROUGH
    if mode is LaunchMode.ISOLATE:
        return LaunchKind.ISOLATED_SESSION
    if not arguments:
        return LaunchKind.ISOLATED_SESSION
    head = arguments[0]
    subcommands = MANAGEMENT_SUBCOMMANDS[harness] | frozenset(extra_subcommands)
    if head in METADATA_FLAGS or head in subcommands:
        return LaunchKind.PASSTHROUGH
    return LaunchKind.ISOLATED_SESSION


def announce_passthrough(harness: Harness, arguments: Sequence[str]) -> None:
    """Tell an interactive operator why a launch kept the harness unmanaged.

    Scripted launches stay silent so harness output and captured streams are
    unchanged; a terminal gets the explanation because a prompt that merely
    begins with a management word, such as `pi update the tests`, also runs
    without isolation.
    """
    if not sys.stderr.isatty():
        return
    invocation = " ".join((harness.value, *arguments))
    print(
        f"skm: passing '{invocation}' through without project skill isolation;"
        f" set {LAUNCH_MODE_ENV}=isolate to isolate it anyway",
        file=sys.stderr,
    )


def harness_command(
    executable: str,
    harness: str | Harness,
    arguments: Sequence[str],
    *,
    project_root: Path,
    isolate_skills: bool = True,
    cwd: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Build one isolated agent-session launch without executing it."""
    command = [executable]
    environment = dict(os.environ)
    selected = harness if isinstance(harness, Harness) else parse_harness(harness)
    if isolate_skills:
        command.extend(isolation_arguments(selected, project_root, cwd or Path.cwd()))
    command.extend(arguments)
    return command, environment


def isolation_arguments(
    harness: Harness,
    project_root: Path,
    cwd: Path | None = None,
) -> list[str]:
    """Return flags that hide home-directory skills while keeping project skills."""
    root = project_root.expanduser().resolve()
    if harness is Harness.PI:
        flags = ["--no-skills"]
        for skill_path in _pi_project_skill_paths(root, cwd or Path.cwd()):
            flags.extend(("--skill", str(skill_path)))
        return flags
    if harness is Harness.CLAUDE:
        return ["--setting-sources", "project,local"]
    if harness is Harness.CODEX:
        disabled = _codex_home_skill_overrides()
        return ["-c", f"skills.config={disabled}"] if disabled else []
    raise CatalogError(f"unsupported harness: {harness.value}")


def find_executable(
    harness: Harness,
    *,
    search_path: str | None = None,
) -> str:
    """Locate the real harness binary outside Skill Manager-owned PATH entries."""
    executable = shutil.which(harness.value, path=search_path)
    if executable is None:
        raise CatalogError(
            f"cannot find real '{harness.value}' executable outside managed shims"
        )
    return executable


def external_search_path(managed_dirs: Iterable[Path]) -> str:
    """Return PATH without entries owned by Skill Manager, preserving order."""
    managed = [path.expanduser().resolve() for path in managed_dirs]
    return os.pathsep.join(
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry
        and not any(
            Path(entry).expanduser().resolve().is_relative_to(owner)
            for owner in managed
        )
    )


def exec_harness(command: Sequence[str], environment: Mapping[str, str]) -> NoReturn:
    """Replace this process with the harness, preserving argv exactly."""
    try:
        os.execvpe(command[0], list(command), dict(environment))
    except OSError as error:
        raise CatalogError(f"cannot launch '{command[0]}': {error}") from error
    raise CatalogError(f"exec of '{command[0]}' returned unexpectedly")


def environment_export(bin_dir: Path) -> str:
    """Return POSIX shell code suitable for eval from direnv or shell startup."""
    return f'export PATH={shlex.quote(str(bin_dir))}:"$PATH"'


def _pi_project_skill_paths(project_root: Path, cwd: Path) -> list[Path]:
    current = cwd.expanduser().resolve()
    if not current.is_relative_to(project_root):
        current = project_root
    directories: list[Path] = []
    while True:
        for relative in (Path(".agents/skills"), Path(".pi/skills")):
            candidate = current / relative
            if candidate.is_dir():
                directories.append(candidate)
        if current == project_root:
            break
        current = current.parent
    return directories


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
