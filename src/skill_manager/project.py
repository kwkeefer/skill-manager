"""Explicit project installation, offline status, and owned-link removal."""

import subprocess
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from skill_manager.catalog import CatalogError
from skill_manager.config import load_declaration
from skill_manager.filesystem import Transaction, digest, file_lock, yaml_bytes
from skill_manager.provision import (
    ConsumerPlan,
    apply_plans,
    consumer_issues,
    consumers,
    plan_consumers,
    project_identity,
)
from skill_manager.storage import (
    LOCK_NAME,
    Resolution,
    build_profile_view,
    profile_view_path,
    resolve_installation,
)

DECLARATION_NAME = ".skill-manager.yaml"


class InstallationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    project_root: Path
    lock_digest: str


@dataclass(frozen=True)
class ProjectStatus:
    issues: tuple[str, ...]
    selected_names: tuple[str, ...] = ()


def locate_project(
    path: Path,
    *,
    require_declaration: bool = True,
    state_dir: Path | None = None,
) -> Path:
    """Find a declaration, or an uninstall boundary, within the Git root.

    Uninstall also recognizes persisted project state when the declaration has
    been removed. Retained writer locks keep repeated legacy uninstalls scoped
    to the same project after its manifests and receipt have been removed.
    """
    candidate = path.expanduser().resolve()
    if candidate.is_file():
        if candidate.name != DECLARATION_NAME:
            raise CatalogError(f"expected {DECLARATION_NAME}, got {candidate}")
        candidate = candidate.parent
    if not candidate.is_dir():
        raise CatalogError(f"project directory does not exist: {candidate}")
    git_root = _git_root(candidate)
    current = candidate
    while True:
        if not require_declaration and state_dir is not None:
            markers = (
                current / LOCK_NAME,
                _receipt_path(current, state_dir),
                _project_lock(current, state_dir),
                *(consumer.manifest_path for consumer in consumers(current, state_dir)),
            )
            if any(marker.exists(follow_symlinks=False) for marker in markers):
                return current
        if (current / DECLARATION_NAME).is_file():
            return current
        if current == git_root or current.parent == current:
            break
        current = current.parent
    if not require_declaration:
        return git_root or candidate
    raise CatalogError(
        f"no {DECLARATION_NAME} found; declare a catalog and profile explicitly"
    )


def install_project(
    path: Path,
    state_dir: Path,
    *,
    update: bool = False,
    locked: bool = False,
) -> Resolution:
    """Install/update as one rollback-capable operation after all preflights."""
    root = locate_project(path)
    if root in {Path(root.anchor), Path.home().resolve()}:
        raise CatalogError(
            "installation requires a project directory, not the filesystem root or home directory"
        )
    with file_lock(_project_lock(root, state_dir)):
        declaration = load_declaration(root / DECLARATION_NAME)
        resolution = resolve_installation(
            root, declaration, state_dir, update=update, locked=locked
        )
        view = build_profile_view(state_dir, declaration.profile, resolution.selected)
        plans = plan_consumers(root, state_dir, resolution.selected, view)
        _reject_tracked_entries(root, plans)
        exclude_path = _exclude_path(root)
        guard = (
            file_lock(state_dir / "locks" / f"exclude-{digest(str(exclude_path))}.lock")
            if exclude_path
            else nullcontext()
        )
        with guard:
            exclude_content = (
                _exclude_content(
                    root, exclude_path, {skill.name for skill in resolution.selected}
                )
                if exclude_path
                else None
            )
            receipt = InstallationReceipt(
                project_root=root, lock_digest=resolution.lock.fingerprint()
            )
            with Transaction() as transaction:
                apply_plans(plans, transaction)
                if exclude_path is not None and exclude_content is not None:
                    transaction.write(exclude_path, exclude_content)
                if update or not (root / LOCK_NAME).exists():
                    transaction.write(
                        root / LOCK_NAME,
                        yaml_bytes(resolution.lock.model_dump(mode="json")),
                    )
                transaction.write(
                    _receipt_path(root, state_dir),
                    yaml_bytes(receipt.model_dump(mode="json")),
                )
        return resolution


def uninstall_project(path: Path, state_dir: Path) -> int:
    """Remove verified owned links, including legacy installs, without a catalog."""
    root = locate_project(path, require_declaration=False, state_dir=state_dir)
    with file_lock(_project_lock(root, state_dir)):
        plans = plan_consumers(root, state_dir, (), None)
        _reject_tracked_entries(root, plans)
        count = len({name for plan in plans for name in plan.previous.entries})
        exclude_path = _exclude_path(root)
        guard = (
            file_lock(state_dir / "locks" / f"exclude-{digest(str(exclude_path))}.lock")
            if exclude_path
            else nullcontext()
        )
        with guard:
            content = (
                _exclude_content(root, exclude_path, set()) if exclude_path else None
            )
            with Transaction() as transaction:
                apply_plans(plans, transaction, uninstall=True)
                if (
                    exclude_path is not None
                    and content is not None
                    and exclude_path.exists()
                ):
                    transaction.write(exclude_path, content)
                transaction.remove(_receipt_path(root, state_dir))
        return count


def project_status(path: Path, state_dir: Path) -> ProjectStatus:
    """Compare declaration, lock, receipt, links and Git excludes without writes."""
    root = locate_project(path)
    try:
        declaration = load_declaration(root / DECLARATION_NAME)
        resolution = resolve_installation(
            root, declaration, state_dir, locked=True, network=False
        )
        view = profile_view_path(state_dir, declaration.profile, resolution.selected)
        issues = consumer_issues(root, state_dir, resolution.selected, view)
        receipt_path = _receipt_path(root, state_dir)
        if not receipt_path.is_file():
            issues.append("no installation receipt; run skm install")
        else:
            receipt = InstallationReceipt.model_validate(
                yaml.safe_load(receipt_path.read_text())
            )
            if (
                receipt.project_root != root
                or receipt.lock_digest != resolution.lock.fingerprint()
            ):
                issues.append(
                    "installed lock differs from the project lock; run skm install"
                )
        exclude_path = _exclude_path(root)
        if exclude_path:
            expected = _exclude_content(
                root, exclude_path, {skill.name for skill in resolution.selected}
            )
            actual = exclude_path.read_bytes() if exclude_path.exists() else b""
            if expected != actual:
                issues.append(
                    "generated skill links are not covered by the expected Git exclude block"
                )
        # Even an unchanged ignore block does not hide previously tracked links.
        if not issues:
            _reject_tracked_entries(
                root, plan_consumers(root, state_dir, resolution.selected, view)
            )
        return ProjectStatus(
            tuple(issues), tuple(skill.name for skill in resolution.selected)
        )
    except (CatalogError, ValidationError, OSError, yaml.YAMLError) as error:
        return ProjectStatus((str(error),))


def list_project(path: Path, state_dir: Path) -> Resolution:
    """Inspect the locked catalog, or resolve a first catalog into shared caches."""
    root = locate_project(path)
    declaration = load_declaration(root / DECLARATION_NAME)
    return resolve_installation(root, declaration, state_dir)


def _project_lock(root: Path, state_dir: Path) -> Path:
    return state_dir / "locks" / f"project-{project_identity(root)}.lock"


def _receipt_path(root: Path, state_dir: Path) -> Path:
    return state_dir / "installations" / f"{project_identity(root)}.yaml"


def _run_git(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "--literal-pathspecs", "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise CatalogError("git is required for Git project bookkeeping") from error
    except subprocess.CalledProcessError as error:
        raise CatalogError(error.stderr.strip() or "Git command failed") from error
    return result.stdout


def _git_root(path: Path) -> Path | None:
    try:
        return Path(_run_git(path, "rev-parse", "--show-toplevel").strip()).resolve()
    except CatalogError as error:
        if "not a git repository" in str(error):
            return None
        raise


def _exclude_path(root: Path) -> Path | None:
    git_root = _git_root(root)
    if git_root is None:
        return None
    path = Path(_run_git(git_root, "rev-parse", "--git-path", "info/exclude").strip())
    return path if path.is_absolute() else git_root / path


def _reject_tracked_entries(root: Path, plans: tuple[ConsumerPlan, ...]) -> None:
    if _git_root(root) is None:
        return
    paths = [
        str((plan.consumer.path / name).relative_to(root))
        for plan in plans
        for name in plan.previous.entries.keys() | plan.desired.keys()
    ]
    if paths:
        tracked = _run_git(root, "ls-files", "-z", "--", *paths).split("\0")
        if tracked[0]:
            raise CatalogError(
                f"refusing to manage Git-tracked skill entry: {tracked[0]}"
            )


def _exclude_content(root: Path, path: Path, names: set[str]) -> bytes:
    if path.is_symlink():
        raise CatalogError(f"Git exclude file is a symlink: {path}")
    git_root = _git_root(root)
    assert git_root is not None
    identity = project_identity(root)
    begin, end = f"# BEGIN skill-manager:{identity}", f"# END skill-manager:{identity}"
    lines = path.read_text().splitlines() if path.exists() else []
    original = list(lines)
    insertion = len(lines)
    if begin in lines:
        start = lines.index(begin)
        if lines.count(begin) != 1 or lines.count(end) != 1 or lines.index(end) < start:
            raise CatalogError(f"malformed skill-manager exclude block in {path}")
        del lines[start : lines.index(end) + 1]
        insertion = start
    elif end in lines:
        raise CatalogError(f"malformed skill-manager exclude block in {path}")
    if names:
        relative = root.relative_to(git_root)
        patterns = [
            "/" + _escape_gitignore((relative / directory / "skills" / name).as_posix())
            for directory in (".agents", ".claude")
            for name in sorted(names)
        ]
        lines[insertion:insertion] = [begin, *patterns, end]
    if lines == original and path.exists():
        return path.read_bytes()
    return ("\n".join(lines) + ("\n" if lines else "")).encode()


def _escape_gitignore(value: str) -> str:
    special = set("\\!#*?[] ")
    return "".join(
        f"\\{character}" if character in special else character for character in value
    )
