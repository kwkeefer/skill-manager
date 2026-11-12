"""Exercise the shipped usage skill through ordinary catalog selection."""

import os
from pathlib import Path

import yaml
from conftest import Workspace, dump

from skill_manager.project import install_project, project_status, uninstall_project
from skill_manager.storage import LOCK_NAME


def test_shipped_skill_is_discovered_and_installed_only_when_selected(
    workspace: Workspace,
) -> None:
    repository = Path(__file__).resolve().parents[1]
    shipped = repository / "skills/skill-manager/SKILL.md"
    catalog = yaml.safe_load(workspace.catalog.read_text())
    catalog["sources"]["skill-manager"] = {
        "path": str(repository),
        "discover": {"include": ["skills/skill-manager/SKILL.md"]},
    }
    dump(workspace.catalog, catalog)

    initial = install_project(workspace.root, workspace.state)
    inventory = next(
        source for source in initial.sources if source.name == "skill-manager"
    )
    assert [skill.name for skill in inventory.skills] == ["skill-manager"]
    for directory in (".agents", ".claude"):
        assert not os.path.lexists(workspace.root / directory / "skills/skill-manager")

    workspace.declare(
        include=[{"source": "skill-manager", "skills": ["skill-manager"]}]
    )
    updated = install_project(workspace.root, workspace.state, update=True)
    assert {skill.name for skill in updated.selected} == {"alpha", "skill-manager"}
    for directory in (".agents", ".claude"):
        installed = workspace.root / directory / "skills/skill-manager"
        assert installed.is_symlink()
        assert (installed / "SKILL.md").read_bytes() == shipped.read_bytes()
    assert not project_status(workspace.root, workspace.state).issues

    lock = (workspace.root / LOCK_NAME).read_bytes()
    assert uninstall_project(workspace.root, workspace.state) == 2
    for directory in (".agents", ".claude"):
        assert not os.path.lexists(workspace.root / directory / "skills/skill-manager")
    assert (workspace.root / LOCK_NAME).read_bytes() == lock
    assert shipped.is_file()
