import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
import yaml
from conftest import Workspace, dump, git, write_skill

from skill_manager import filesystem
from skill_manager.catalog import CatalogError
from skill_manager.config import load_declaration
from skill_manager.filesystem import yaml_bytes
from skill_manager.project import (
    install_project,
    locate_project,
    project_status,
    uninstall_project,
)
from skill_manager.provision import ConsumerManifest, consumers, project_identity
from skill_manager.storage import LOCK_NAME, build_profile_view, resolve_installation


def installed(workspace: Workspace, directory: str = ".agents") -> Path:
    return workspace.root / directory / "skills"


def test_install_is_idempotent_and_preserves_support_files_and_unmanaged_skills(
    workspace: Workspace,
) -> None:
    support = workspace.source / "alpha" / "scripts" / "check.sh"
    support.parent.mkdir()
    support.write_text("#!/bin/sh\nexit 0\n")
    support.chmod(0o755)
    write_skill(installed(workspace) / "own", "own")
    install_project(workspace.root, workspace.state)
    entry = installed(workspace) / "alpha"
    before = entry.lstat()
    lock = (workspace.root / LOCK_NAME).read_bytes()
    install_project(workspace.root, workspace.state, locked=True)
    assert entry.is_symlink()
    assert entry.lstat().st_ino == before.st_ino
    assert entry.lstat().st_mtime_ns == before.st_mtime_ns
    assert (entry / "scripts/check.sh").read_text() == support.read_text()
    assert os.access(entry / "scripts/check.sh", os.X_OK)
    assert (installed(workspace) / "own").is_dir()
    assert (workspace.root / LOCK_NAME).read_bytes() == lock
    assert not project_status(workspace.root, workspace.state).issues
    status = git(workspace.root, "status", "--porcelain", "--untracked-files=all")
    assert ".agents/skills/alpha" not in status
    assert ".agents/skills/own/SKILL.md" in status


def test_update_adopts_catalog_changes_but_install_keeps_catalog_snapshot(
    workspace: Workspace,
) -> None:
    install_project(workspace.root, workspace.state)
    old_lock = (workspace.root / LOCK_NAME).read_bytes()
    catalog = yaml.safe_load(workspace.catalog.read_text())
    catalog["profiles"]["base"] = {"include": [{"source": "local", "skills": ["beta"]}]}
    dump(workspace.catalog, catalog)
    install_project(workspace.root, workspace.state)
    assert (installed(workspace) / "alpha").is_symlink()
    assert (workspace.root / LOCK_NAME).read_bytes() == old_lock
    assert not project_status(workspace.root, workspace.state).issues
    install_project(workspace.root, workspace.state, update=True)
    assert not os.path.lexists(installed(workspace) / "alpha")
    assert (installed(workspace) / "beta").is_symlink()
    assert (workspace.root / LOCK_NAME).read_bytes() != old_lock
    assert not project_status(workspace.root, workspace.state).issues


def test_project_refinements_are_shared_and_exclusions_win(
    workspace: Workspace,
) -> None:
    workspace.declare(include=[{"skills": ["beta"]}], exclude=[{"skills": ["alpha"]}])
    resolution = install_project(workspace.root, workspace.state)
    assert [skill.name for skill in resolution.selected] == ["beta"]
    assert "skill-manager.refinements" not in git(
        workspace.root, "config", "--local", "--list"
    )


def test_changed_declaration_requires_explicit_update(workspace: Workspace) -> None:
    install_project(workspace.root, workspace.state)
    workspace.declare(profile="all")
    assert "declaration changed" in str(
        project_status(workspace.root, workspace.state).issues
    )
    with pytest.raises(CatalogError, match="run skm update"):
        install_project(workspace.root, workspace.state)
    assert not (installed(workspace) / "beta").exists()
    install_project(workspace.root, workspace.state, update=True)
    assert (installed(workspace) / "beta").is_symlink()


def test_locked_install_requires_existing_lock(workspace: Workspace) -> None:
    with pytest.raises(CatalogError, match="cannot read project lock"):
        install_project(workspace.root, workspace.state, locked=True)
    assert not installed(workspace).exists()


def test_second_consumer_collision_writes_no_project_state(
    workspace: Workspace,
) -> None:
    collision = installed(workspace, ".claude") / "alpha"
    write_skill(collision, "alpha", "unmanaged")
    exclude = workspace.root / ".git/info/exclude"
    old_exclude = exclude.read_bytes()
    with pytest.raises(CatalogError, match="not managed"):
        install_project(workspace.root, workspace.state)
    assert not installed(workspace).exists()
    assert not (workspace.root / LOCK_NAME).exists()
    assert not (workspace.state / "manifests").exists()
    assert exclude.read_bytes() == old_exclude
    assert "unmanaged" in (collision / "SKILL.md").read_text()


@pytest.mark.parametrize("relative", [".agents", ".agents/skills", ".claude/skills"])
def test_rejects_symlink_consumers_before_resolving_paths(
    workspace: Workspace, relative: str
) -> None:
    outside = workspace.root.parent / "other-manager"
    outside.mkdir()
    alias = workspace.root / relative
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(CatalogError, match="symlink"):
        install_project(workspace.root, workspace.state)
    assert list(outside.iterdir()) == []
    assert not (workspace.root / LOCK_NAME).exists()


def test_update_rolls_back_links_manifests_lock_and_excludes_on_late_failure(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_project(workspace.root, workspace.state)
    before_lock = (workspace.root / LOCK_NAME).read_bytes()
    before_exclude = (workspace.root / ".git/info/exclude").read_bytes()
    before_manifests = {
        p: p.read_bytes() for p in (workspace.state / "manifests").glob("*.yaml")
    }
    catalog = yaml.safe_load(workspace.catalog.read_text())
    catalog["profiles"]["base"] = {"include": [{"source": "local", "skills": ["beta"]}]}
    dump(workspace.catalog, catalog)
    original = filesystem.atomic_write
    failed = False

    def fail_once(path: Path, content: bytes, mode: int = 0o644) -> None:
        nonlocal failed
        if path.name == LOCK_NAME and not failed:
            failed = True
            raise OSError("injected lock write failure")
        original(path, content, mode)

    monkeypatch.setattr(filesystem, "atomic_write", fail_once)
    with pytest.raises(OSError, match="injected"):
        install_project(workspace.root, workspace.state, update=True)
    for directory in (".agents", ".claude"):
        assert (installed(workspace, directory) / "alpha").is_symlink()
        assert not os.path.lexists(installed(workspace, directory) / "beta")
    assert (workspace.root / LOCK_NAME).read_bytes() == before_lock
    assert (workspace.root / ".git/info/exclude").read_bytes() == before_exclude
    assert {p: p.read_bytes() for p in before_manifests} == before_manifests
    assert not project_status(workspace.root, workspace.state).issues


def test_failed_project_addition_does_not_change_lock_or_installed_selection(
    workspace: Workspace,
) -> None:
    install_project(workspace.root, workspace.state)
    original = (workspace.root / LOCK_NAME).read_bytes()
    workspace.declare(include=[{"skills": ["beta"]}])
    write_skill(installed(workspace) / "beta", "beta", "unmanaged")
    with pytest.raises(CatalogError, match="not managed"):
        install_project(workspace.root, workspace.state, update=True)
    assert (workspace.root / LOCK_NAME).read_bytes() == original
    assert (installed(workspace) / "alpha").is_symlink()
    # The declaration is the user's deliberate edit; skm does not rewrite it.
    assert load_declaration(workspace.root / ".skill-manager.yaml").include


@pytest.mark.parametrize(
    "change, expected",
    [
        ("missing", "managed link is missing"),
        ("repointed", "changed externally"),
        ("target", "selects no skills"),
        ("exclude", "exclude block"),
    ],
)
def test_status_detects_local_drift(
    workspace: Workspace, change: str, expected: str
) -> None:
    install_project(workspace.root, workspace.state)
    link = installed(workspace) / "alpha"
    if change in {"missing", "repointed"}:
        link.unlink()
        if change == "repointed":
            link.symlink_to(workspace.source / "beta")
    elif change == "target":
        (workspace.source / "alpha/SKILL.md").unlink()
    else:
        (workspace.root / ".git/info/exclude").write_text("# user rules\n")
    assert expected in str(project_status(workspace.root, workspace.state).issues)


def test_status_does_not_write_or_network(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_project(workspace.root, workspace.state)
    before = {
        p: p.lstat().st_mtime_ns
        for base in (workspace.root, workspace.state)
        for p in base.rglob("*")
    }

    def reject_network(*args: object, **kwargs: object) -> None:
        pytest.fail("status must not acquire sources")

    monkeypatch.setattr("skill_manager.storage.acquire_source", reject_network)
    monkeypatch.setattr("skill_manager.storage.acquire_locked_source", reject_network)
    assert not project_status(workspace.root, workspace.state).issues
    after = {
        p: p.lstat().st_mtime_ns
        for base in (workspace.root, workspace.state)
        for p in base.rglob("*")
    }
    assert before == after


def test_uninstall_preserves_unmanaged_entries_lock_and_other_exclude_blocks(
    workspace: Workspace,
) -> None:
    exclude = workspace.root / ".git/info/exclude"
    unrelated = b"# custom rules\nkeep-me\n# BEGIN skill-manager:other\n/other\n# END skill-manager:other\n"
    exclude.write_bytes(unrelated)
    install_project(workspace.root, workspace.state)
    write_skill(installed(workspace) / "own", "own")
    before_lock = (workspace.root / LOCK_NAME).read_bytes()
    workspace.catalog.unlink()
    (workspace.root / ".skill-manager.yaml").unlink()
    assert uninstall_project(workspace.root, workspace.state) == 1
    assert not os.path.lexists(installed(workspace) / "alpha")
    assert (installed(workspace) / "own/SKILL.md").is_file()
    assert (workspace.root / LOCK_NAME).read_bytes() == before_lock
    assert exclude.read_bytes() == unrelated
    assert not list((workspace.state / "manifests").glob("*.yaml"))
    assert uninstall_project(workspace.root, workspace.state) == 0


def test_uninstall_refuses_externally_changed_owned_entry_without_partial_removal(
    workspace: Workspace,
) -> None:
    install_project(workspace.root, workspace.state)
    changed = installed(workspace, ".claude") / "alpha"
    changed.unlink()
    write_skill(changed, "alpha", "replacement")
    with pytest.raises(CatalogError, match="changed externally"):
        uninstall_project(workspace.root, workspace.state)
    assert (installed(workspace) / "alpha").is_symlink()
    assert "replacement" in (changed / "SKILL.md").read_text()


@pytest.mark.parametrize("parent_declared", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("from_subdirectory", [False, True])
def test_nested_uninstall_without_declaration_preserves_parent(
    workspace: Workspace,
    parent_declared: bool,
    legacy: bool,
    from_subdirectory: bool,
) -> None:
    install_project(workspace.root, workspace.state)
    if not parent_declared:
        (workspace.root / ".skill-manager.yaml").unlink()
    nested = workspace.root / "subproject"
    nested.mkdir()
    declaration = nested / ".skill-manager.yaml"
    dump(
        declaration,
        {"catalog": {"path": str(workspace.catalog)}, "profile": "all"},
    )
    install_project(nested, workspace.state)
    declaration.unlink()
    if legacy:
        (nested / LOCK_NAME).unlink()
        # Simulate manifests from the old installer, without newer writer locks.
        (
            workspace.state / "locks" / f"project-{project_identity(nested)}.lock"
        ).unlink()
        for receipt in (workspace.state / "installations").glob("*.yaml"):
            if yaml.safe_load(receipt.read_text())["project_root"] == str(nested):
                receipt.unlink()
    target = nested / "src" if from_subdirectory else nested
    target.mkdir(exist_ok=True)

    assert uninstall_project(target, workspace.state) == 2
    for directory in (".agents", ".claude"):
        assert (workspace.root / directory / "skills/alpha").is_symlink()
        assert not os.path.lexists(nested / directory / "skills/alpha")
        assert not os.path.lexists(nested / directory / "skills/beta")
    assert all(
        consumer.manifest_path.exists()
        for consumer in consumers(workspace.root, workspace.state)
    )
    assert all(
        not consumer.manifest_path.exists()
        for consumer in consumers(nested, workspace.state)
    )
    assert uninstall_project(target, workspace.state) == 0
    assert (workspace.root / ".agents/skills/alpha").is_symlink()


def test_legacy_manifests_are_reused_for_migration_and_uninstall(
    workspace: Workspace,
) -> None:
    legacy_view = workspace.state / "profiles/base/legacy"
    legacy_view.mkdir(parents=True)
    (legacy_view / "alpha").symlink_to(workspace.source / "alpha")
    for consumer in consumers(workspace.root, workspace.state):
        consumer.path.mkdir(parents=True)
        target = str(legacy_view / "alpha")
        (consumer.path / "alpha").symlink_to(target)
        consumer.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        consumer.manifest_path.write_bytes(
            yaml_bytes(
                ConsumerManifest(
                    consumer_path=consumer.path,
                    entries={"alpha": target},
                ).model_dump(mode="json")
            )
        )
    install_project(workspace.root, workspace.state)
    assert not project_status(workspace.root, workspace.state).issues
    assert uninstall_project(workspace.root, workspace.state) == 1


def test_concurrent_profile_creation_and_project_install_are_idempotent(
    workspace: Workspace,
) -> None:
    declaration = load_declaration(workspace.root / ".skill-manager.yaml")
    resolution = resolve_installation(workspace.root, declaration, workspace.state)
    with ThreadPoolExecutor(max_workers=4) as pool:
        views = list(
            pool.map(
                lambda _: build_profile_view(
                    workspace.state, "base", resolution.selected
                ),
                range(8),
            )
        )
    assert len(set(views)) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: install_project(workspace.root, workspace.state), range(8)
            )
        )
    assert len({result.lock.fingerprint() for result in results}) == 1
    assert not project_status(workspace.root, workspace.state).issues


def test_tracked_managed_links_are_rejected(workspace: Workspace) -> None:
    install_project(workspace.root, workspace.state)
    git(workspace.root, "add", "-f", ".agents/skills/alpha")
    with pytest.raises(CatalogError, match="Git-tracked"):
        install_project(workspace.root, workspace.state)
    with pytest.raises(CatalogError, match="Git-tracked"):
        uninstall_project(workspace.root, workspace.state)
    assert "Git-tracked" in str(project_status(workspace.root, workspace.state).issues)


def test_nested_discovery_stops_at_git_boundary(workspace: Workspace) -> None:
    nested = workspace.root / "src/package"
    nested.mkdir(parents=True)
    assert locate_project(nested) == workspace.root
    other = workspace.root / "nested-repo"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    with pytest.raises(CatalogError, match="no .skill-manager.yaml"):
        locate_project(other)


def test_locked_install_preserves_lock_bytes_including_comments(
    workspace: Workspace,
) -> None:
    install_project(workspace.root, workspace.state)
    path = workspace.root / LOCK_NAME
    content = b"# Deliberately reviewed lock\n" + path.read_bytes()
    path.write_bytes(content)
    install_project(workspace.root, workspace.state, locked=True)
    assert path.read_bytes() == content


def test_install_refuses_home_directory(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(workspace.root))
    with pytest.raises(CatalogError, match="home directory"):
        install_project(workspace.root, workspace.state)
    assert not installed(workspace).exists()


def test_shared_git_excludes_keep_each_projects_status_clean(
    workspace: Workspace,
) -> None:
    install_project(workspace.root, workspace.state)
    nested = workspace.root / "subproject"
    nested.mkdir()
    dump(
        nested / ".skill-manager.yaml",
        {
            "catalog": {"path": str(workspace.catalog)},
            "profile": "all",
        },
    )
    install_project(nested, workspace.state)
    exclude = workspace.root / ".git/info/exclude"
    with exclude.open("a") as handle:
        handle.write("# user rule after both managed blocks\n*.local\n")
    assert not project_status(workspace.root, workspace.state).issues
    assert not project_status(nested, workspace.state).issues
    uninstall_project(workspace.root, workspace.state)
    assert not project_status(nested, workspace.state).issues
    assert "*.local" in exclude.read_text()


def test_first_install_failure_restores_absent_links_directories_and_lock(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = filesystem.atomic_write
    failed = False

    def fail_once(path: Path, content: bytes, mode: int = 0o644) -> None:
        nonlocal failed
        if path.name == LOCK_NAME and not failed:
            failed = True
            raise OSError("injected failure")
        original(path, content, mode)

    before = (workspace.root / ".git/info/exclude").read_bytes()
    monkeypatch.setattr(filesystem, "atomic_write", fail_once)
    with pytest.raises(OSError, match="injected"):
        install_project(workspace.root, workspace.state)
    assert not (workspace.root / ".agents").exists()
    assert not (workspace.root / ".claude").exists()
    assert not (workspace.root / LOCK_NAME).exists()
    assert (workspace.root / ".git/info/exclude").read_bytes() == before


def test_uninstall_rolls_back_when_exclude_write_fails(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_project(workspace.root, workspace.state)
    original = filesystem.atomic_write
    failed = False

    def fail_once(path: Path, content: bytes, mode: int = 0o644) -> None:
        nonlocal failed
        if path.name == "exclude" and not failed:
            failed = True
            raise OSError("injected exclude failure")
        original(path, content, mode)

    monkeypatch.setattr(filesystem, "atomic_write", fail_once)
    with pytest.raises(OSError, match="injected"):
        uninstall_project(workspace.root, workspace.state)
    assert not project_status(workspace.root, workspace.state).issues


def test_non_git_project_can_install_check_and_uninstall(workspace: Workspace) -> None:
    # Non-Git projects have no local Git exclude file; they still use ownership manifests.
    import shutil

    shutil.rmtree(workspace.root / ".git")
    install_project(workspace.root, workspace.state)
    assert not project_status(workspace.root, workspace.state).issues
    assert uninstall_project(workspace.root, workspace.state) == 1


def test_two_projects_can_create_shared_manifests_directory_concurrently(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = workspace.root.parent / "other-project"
    other.mkdir()
    dump(
        other / ".skill-manager.yaml",
        {
            "catalog": {"path": str(workspace.catalog)},
            "profile": "base",
        },
    )
    original_mkdir = Path.mkdir
    barrier = Barrier(2)

    def racing_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path == workspace.state / "manifests":
            barrier.wait(timeout=10)
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(install_project, workspace.root, workspace.state)
        second = pool.submit(install_project, other, workspace.state)
        assert len(first.result().selected) == len(second.result().selected) == 1
    assert not project_status(workspace.root, workspace.state).issues
    assert not project_status(other, workspace.state).issues


def test_rollback_preserves_other_writers_new_files(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    with (
        pytest.raises(RuntimeError, match="abort"),
        filesystem.Transaction() as transaction,
    ):
        transaction.write(shared / "ours", b"new")
        (shared / "theirs").write_bytes(b"concurrent writer")
        raise RuntimeError("abort")
    assert not (shared / "ours").exists()
    assert (shared / "theirs").read_bytes() == b"concurrent writer"
