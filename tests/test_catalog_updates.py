import os
import shutil
from pathlib import Path

import pytest
import yaml
from conftest import Workspace, commit, dump, git, write_skill
from pydantic import ValidationError

from skill_manager.catalog import CatalogError
from skill_manager.project import install_project, project_status
from skill_manager.storage import LOCK_NAME, LockFile


def repository(path: Path, content: str) -> str:
    write_skill(path / "alpha", "alpha", content)
    git(path, "init", "-q", "-b", "main")
    return commit(path, content)


def remote_catalog(workspace: Workspace, skill_repo: Path) -> Path:
    repo = workspace.root.parent / "catalog-repository"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    dump(
        repo / "catalog.yaml",
        {
            "sources": {"approved": {"git": str(skill_repo), "ref": "v1"}},
            "profiles": {"base": {"include": [{"source": "approved"}]}},
        },
    )
    commit(repo, "approve v1")
    dump(
        workspace.root / ".skill-manager.yaml",
        {
            "catalog": {"git": str(repo), "ref": "main"},
            "profile": "base",
        },
    )
    return repo


def test_project_update_follows_catalog_approvals_not_latest_skill_branch(
    workspace: Workspace,
) -> None:
    skill_repo = workspace.root.parent / "skills-repository"
    first = repository(skill_repo, "version one")
    git(skill_repo, "tag", "v1")
    catalog_repo = remote_catalog(workspace, skill_repo)
    initial = install_project(workspace.root, workspace.state)
    first_catalog = initial.lock.catalog_revision
    write_skill(skill_repo / "alpha", "alpha", "version two")
    second = commit(skill_repo, "two")
    git(skill_repo, "tag", "v2")
    # A project update cannot override the catalog curator's pinned v1 choice.
    unchanged = install_project(workspace.root, workspace.state, update=True)
    assert unchanged.lock.sources["approved"].resolved_revision == first
    catalog = yaml.safe_load((catalog_repo / "catalog.yaml").read_text())
    catalog["sources"]["approved"]["ref"] = "v2"
    dump(catalog_repo / "catalog.yaml", catalog)
    new_catalog = commit(catalog_repo, "approve v2")
    # Install and offline status keep the old catalog/skill snapshot.
    unchanged = install_project(workspace.root, workspace.state, locked=True)
    assert unchanged.lock.catalog_revision == first_catalog
    assert unchanged.lock.sources["approved"].resolved_revision == first
    assert not project_status(workspace.root, workspace.state).issues
    updated = install_project(workspace.root, workspace.state, update=True)
    assert updated.lock.catalog_revision == new_catalog
    assert updated.lock.sources["approved"].resolved_revision == second
    assert (
        "version two" in (workspace.root / ".agents/skills/alpha/SKILL.md").read_text()
    )


def test_locked_install_hydrates_old_revisions_on_fresh_machine_without_lock_change(
    workspace: Workspace,
) -> None:
    skill_repo = workspace.root.parent / "skills-repository"
    first = repository(skill_repo, "version one")
    git(skill_repo, "tag", "v1")
    catalog_repo = remote_catalog(workspace, skill_repo)
    install_project(workspace.root, workspace.state)
    lock_bytes = (workspace.root / LOCK_NAME).read_bytes()
    write_skill(skill_repo / "alpha", "alpha", "version two")
    commit(skill_repo, "two")
    (catalog_repo / "note.txt").write_text("catalog advanced")
    commit(catalog_repo, "catalog advanced")
    fresh_project = workspace.root.parent / "fresh-project"
    fresh_project.mkdir()
    shutil.copy2(workspace.root / ".skill-manager.yaml", fresh_project)
    shutil.copy2(workspace.root / LOCK_NAME, fresh_project)
    fresh_state = workspace.root.parent / "fresh-store"
    result = install_project(fresh_project, fresh_state, locked=True)
    assert result.lock.sources["approved"].resolved_revision == first
    assert (fresh_project / LOCK_NAME).read_bytes() == lock_bytes
    assert (
        "version one" in (fresh_project / ".agents/skills/alpha/SKILL.md").read_text()
    )


def test_cached_install_and_status_work_when_catalog_and_source_are_unavailable(
    workspace: Workspace,
) -> None:
    skill_repo = workspace.root.parent / "skills-repository"
    repository(skill_repo, "cached content")
    git(skill_repo, "tag", "v1")
    catalog_repo = remote_catalog(workspace, skill_repo)
    install_project(workspace.root, workspace.state)
    skill_repo.rename(skill_repo.with_name("skills-unavailable"))
    catalog_repo.rename(catalog_repo.with_name("catalog-unavailable"))
    install_project(workspace.root, workspace.state, locked=True)
    assert not project_status(workspace.root, workspace.state).issues
    assert (
        "cached content"
        in (workspace.root / ".agents/skills/alpha/SKILL.md").read_text()
    )


def test_failed_update_with_unreachable_catalog_leaves_installation_unchanged(
    workspace: Workspace,
) -> None:
    skill_repo = workspace.root.parent / "skills-repository"
    repository(skill_repo, "cached content")
    git(skill_repo, "tag", "v1")
    catalog_repo = remote_catalog(workspace, skill_repo)
    install_project(workspace.root, workspace.state)
    before = (workspace.root / LOCK_NAME).read_bytes()
    target = os.readlink(workspace.root / ".agents/skills/alpha")
    catalog_repo.rename(catalog_repo.with_name("catalog-unavailable"))
    with pytest.raises(CatalogError, match="git"):
        install_project(workspace.root, workspace.state, update=True)
    assert (workspace.root / LOCK_NAME).read_bytes() == before
    assert os.readlink(workspace.root / ".agents/skills/alpha") == target


def test_changed_source_url_is_adopted_only_by_update_and_caches_are_separate(
    workspace: Workspace,
) -> None:
    first_repo = workspace.root.parent / "first"
    second_repo = workspace.root.parent / "second"
    first = repository(first_repo, "FIRST")
    second = repository(second_repo, "SECOND")
    catalog = {
        "sources": {"same-name": {"git": str(first_repo), "ref": "main"}},
        "profiles": {"base": {"include": [{"source": "same-name"}]}},
    }
    dump(workspace.catalog, catalog)
    install_project(workspace.root, workspace.state)
    old_target = (workspace.root / ".agents/skills/alpha").resolve()
    catalog["sources"]["same-name"]["git"] = str(second_repo)
    dump(workspace.catalog, catalog)
    assert (
        install_project(workspace.root, workspace.state)
        .lock.sources["same-name"]
        .resolved_revision
        == first
    )
    updated = install_project(workspace.root, workspace.state, update=True)
    assert updated.lock.sources["same-name"].git == str(second_repo)
    assert updated.lock.sources["same-name"].resolved_revision == second
    assert "SECOND" in (workspace.root / ".agents/skills/alpha/SKILL.md").read_text()
    assert old_target.is_dir()


def test_changed_catalog_url_requires_update_even_when_ref_is_unchanged(
    workspace: Workspace,
) -> None:
    skill_repo = workspace.root.parent / "skills-repository"
    repository(skill_repo, "content")
    git(skill_repo, "tag", "v1")
    remote_catalog(workspace, skill_repo)
    install_project(workspace.root, workspace.state)
    declaration = yaml.safe_load((workspace.root / ".skill-manager.yaml").read_text())
    declaration["catalog"]["git"] = str(workspace.root.parent / "another-repository")
    dump(workspace.root / ".skill-manager.yaml", declaration)
    with pytest.raises(CatalogError, match="declaration changed"):
        install_project(workspace.root, workspace.state)


def test_two_projects_share_checkout_but_keep_independent_locks(
    workspace: Workspace,
) -> None:
    skill_repo = workspace.root.parent / "skills-repository"
    first = repository(skill_repo, "one")
    catalog = {
        "sources": {"shared": {"git": str(skill_repo), "ref": "main"}},
        "profiles": {"base": {"include": [{"source": "shared"}]}},
    }
    dump(workspace.catalog, catalog)
    other = workspace.root.parent / "other"
    other.mkdir()
    shutil.copy2(workspace.root / ".skill-manager.yaml", other)
    install_project(workspace.root, workspace.state)
    install_project(other, workspace.state)
    assert (workspace.root / ".agents/skills/alpha").resolve() == (
        other / ".agents/skills/alpha"
    ).resolve()
    write_skill(skill_repo / "alpha", "alpha", "two")
    second = commit(skill_repo, "two")
    result = install_project(workspace.root, workspace.state, update=True)
    other_lock = LockFile.model_validate(
        yaml.safe_load((other / LOCK_NAME).read_text())
    )
    assert result.lock.sources["shared"].resolved_revision == second
    assert other_lock.sources["shared"].resolved_revision == first
    assert "one" in (other / ".agents/skills/alpha/SKILL.md").read_text()


def test_git_catalog_can_bundle_relative_local_skills(workspace: Workspace) -> None:
    repo = workspace.root.parent / "bundled-catalog"
    write_skill(repo / "skills/alpha", "alpha", "bundled")
    git(repo, "init", "-q", "-b", "main")
    dump(
        repo / "catalog.yaml",
        {
            "sources": {"bundled": {"path": "skills"}},
            "profiles": {"base": {"include": [{"source": "bundled"}]}},
        },
    )
    revision = commit(repo, "catalog and skills")
    dump(
        workspace.root / ".skill-manager.yaml",
        {
            "catalog": {"git": str(repo), "ref": "main"},
            "profile": "base",
        },
    )
    result = install_project(workspace.root, workspace.state)
    assert result.lock.catalog_revision == revision
    assert (
        (workspace.root / ".agents/skills/alpha")
        .resolve()
        .is_relative_to(workspace.state)
    )
    assert "bundled" in (workspace.root / ".agents/skills/alpha/SKILL.md").read_text()


def test_lock_rejects_unknown_version_and_source_identity_tampering(
    workspace: Workspace,
) -> None:
    result = install_project(workspace.root, workspace.state)
    raw = result.lock.model_dump(mode="json")
    raw["version"] = 2
    with pytest.raises(ValidationError):
        LockFile.model_validate(raw)
    raw["version"] = 1
    raw["sources"]["local"]["path"] = "wrong-location"
    with pytest.raises(ValidationError, match="identity differs"):
        LockFile.model_validate(raw)
