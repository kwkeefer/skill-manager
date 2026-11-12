from pathlib import Path

import pytest
from conftest import Workspace, dump, write_skill
from pydantic import ValidationError

from skill_manager.catalog import CatalogError, SourceDeclaration, discover_source
from skill_manager.config import CatalogReference, ProjectDeclaration
from skill_manager.project import install_project


def test_tag_inheritance_and_exclusions(workspace: Workspace) -> None:
    dump(
        workspace.catalog,
        {
            "sources": {
                "local": {
                    "path": "source",
                    "tags": ["coding"],
                    "skills": {
                        "beta": {"tags": {"inherit": False, "add": ["writing"]}}
                    },
                }
            },
            "profiles": {"base": {"include": [{"tags": ["coding"]}]}},
        },
    )
    result = install_project(workspace.root, workspace.state)
    assert [skill.name for skill in result.selected] == ["alpha"]
    workspace.declare(include=[{"tags": ["writing"]}], exclude=[{"skills": ["alpha"]}])
    result = install_project(workspace.root, workspace.state, update=True)
    assert [skill.name for skill in result.selected] == ["beta"]


def test_duplicate_final_names_fail_before_installation(workspace: Workspace) -> None:
    duplicate = workspace.root.parent / "duplicate"
    write_skill(duplicate / "alpha", "alpha")
    dump(
        workspace.catalog,
        {
            "sources": {"one": {"path": "source"}, "two": {"path": str(duplicate)}},
            "profiles": {"base": {"include": [{"skills": ["alpha"]}]}},
        },
    )
    with pytest.raises(CatalogError, match="duplicate skill name"):
        install_project(workspace.root, workspace.state)
    assert not (workspace.root / ".agents").exists()
    workspace.declare(exclude=[{"source": "two"}])
    assert len(install_project(workspace.root, workspace.state).selected) == 1


def test_discovery_rejects_skill_file_symlink_outside_source(tmp_path: Path) -> None:
    write_skill(tmp_path / "outside/alpha", "alpha")
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").symlink_to(tmp_path / "outside/alpha/SKILL.md")
    with pytest.raises(CatalogError, match="escapes source root"):
        discover_source("local", SourceDeclaration(path=source), source)


@pytest.mark.parametrize("name", ["../escape", ".", "..", "", "x\ny", "x\0y"])
def test_project_profile_rejects_unsafe_identifiers(name: str) -> None:
    with pytest.raises(ValidationError):
        ProjectDeclaration(
            catalog=CatalogReference(path=Path("catalog.yaml")), profile=name
        )


@pytest.mark.parametrize(
    "value",
    [
        {"git": "repo"},
        {"git": "repo", "ref": "main", "file": "../catalog.yaml"},
        {"git": "repo", "ref": "main", "file": "/catalog.yaml"},
        {"path": "catalog.yaml", "git": "repo", "ref": "main"},
        {"path": "catalog.yaml", "ref": "main"},
    ],
)
def test_catalog_reference_rejects_ambiguous_or_escaping_locations(
    value: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        CatalogReference.model_validate(value)
