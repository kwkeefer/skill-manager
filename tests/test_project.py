import os
import subprocess
from pathlib import Path

import pytest
import yaml

from skill_manager.catalog import CatalogError
from skill_manager.config import LoadedConfiguration, load_configuration
from skill_manager.harness import Harness
from skill_manager.project import (
    apply_project,
    environment_export,
    generate_adapters,
    generate_global_adapters,
    load_project,
    set_project_profile,
)
from skill_manager.storage import sync


def test_project_apply_materializes_profile_and_preserves_local_skills(
    tmp_path: Path,
) -> None:
    configuration, project_root = _setup(tmp_path)
    _write_skill(project_root / ".agents" / "skills" / "local", "local")
    _write_skill(project_root / ".claude" / "skills" / "local", "local")

    project = load_project(project_root)
    applied, _, count = apply_project(configuration, project)

    assert count == 1
    assert applied.active_profile == "default"
    for directory in (".agents", ".claude"):
        skills = project_root / directory / "skills"
        assert (skills / "alpha").is_symlink()
        assert (skills / "local").is_dir()


def test_project_declaration_is_found_from_nested_directory(tmp_path: Path) -> None:
    _, project_root = _setup(tmp_path)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", project_root],
        check=True,
        capture_output=True,
    )
    nested = project_root / "src" / "package"
    nested.mkdir(parents=True)

    project = load_project(nested)

    assert project.root == project_root.resolve()
    assert project.declaration_path == project_root / ".skill-manager.yaml"
    assert project.declaration.profile == "default"


def test_project_apply_refuses_to_replace_same_named_local_skill(
    tmp_path: Path,
) -> None:
    configuration, project_root = _setup(tmp_path)
    _write_skill(project_root / ".agents" / "skills" / "alpha", "alpha")

    with pytest.raises(CatalogError, match="already exists and is not managed"):
        apply_project(configuration, load_project(project_root))

    assert (project_root / ".agents" / "skills" / "alpha").is_dir()


def test_git_local_profile_requires_no_project_file(tmp_path: Path) -> None:
    configuration, project_root = _setup(tmp_path)
    (project_root / ".skill-manager.yaml").unlink()
    subprocess.run(
        ["git", "init", "-b", "main", project_root],
        check=True,
        capture_output=True,
    )

    root = set_project_profile(project_root, "default")
    project = load_project(project_root)

    assert root == project_root.resolve()
    assert project.declaration_path is None
    assert project.declaration.profile == "default"
    apply_project(configuration, project)
    assert (
        subprocess.run(
            [
                "git",
                "-C",
                project_root,
                "config",
                "--local",
                "--get",
                "skill-manager.profile",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "default"
    )
    assert (
        subprocess.run(
            ["git", "-C", project_root, "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == ""
    )
    exclude = Path(
        subprocess.run(
            ["git", "-C", project_root, "rev-parse", "--git-path", "info/exclude"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    if not exclude.is_absolute():
        exclude = project_root / exclude
    assert "/.agents/skills/alpha" in exclude.read_text()
    assert configuration.default_project_profile is None


def test_generated_adapters_forward_to_project_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration, project_root = _setup(tmp_path)
    executables = tmp_path / "executables"
    executables.mkdir()
    manager = executables / "skm"
    manager.write_text("#!/bin/sh\nexit 0\n")
    manager.chmod(0o755)
    monkeypatch.setenv("PATH", str(executables))
    project = load_project(project_root)

    bin_dir = generate_adapters(configuration, project)
    global_bin_dir = generate_global_adapters(
        configuration, (harness.value for harness in Harness)
    )

    assert environment_export(bin_dir).startswith("export PATH=")
    assert (global_bin_dir / "pi").is_file()
    assert '"$PWD" pi -- "$@"' in (global_bin_dir / "pi").read_text()
    for harness in ("pi", "claude", "codex"):
        wrapper = bin_dir / harness
        assert os.access(wrapper, os.X_OK)
        assert "skm project run" in wrapper.read_text()
        assert str(project_root) in wrapper.read_text()


def test_generate_global_adapters_rejects_unknown_harness(
    tmp_path: Path,
) -> None:
    configuration, _ = _setup(tmp_path)

    with pytest.raises(CatalogError, match="unsupported harness"):
        generate_global_adapters(configuration, ("gemini",))


def _setup(tmp_path: Path) -> tuple[LoadedConfiguration, Path]:
    source = tmp_path / "source"
    _write_skill(source / "alpha", "alpha")
    _dump(
        tmp_path / "catalog.yaml",
        {
            "sources": {"demo": {"path": str(source)}},
            "profiles": {"default": {"include": [{"source": "demo"}]}},
        },
    )
    _dump(
        tmp_path / "config.yaml",
        {
            "catalogs": ["catalog.yaml"],
            "state_dir": "state",
        },
    )
    project_root = tmp_path / "project"
    project_root.mkdir()
    _dump(
        project_root / ".skill-manager.yaml",
        {"profile": "default", "global_skills": "disabled"},
    )
    configuration = load_configuration(tmp_path / "config.yaml")
    sync(configuration)
    return configuration, project_root


def _write_skill(path: Path, name: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\n---\n# {name}\n")


def _dump(path: Path, value: object) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False))
