import subprocess
import sys
from pathlib import Path

import yaml

from skill_manager.cli import main


def test_setup_bootstraps_missing_default_configuration(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setattr(sys, "argv", ["skm", "setup", "--no-shims"])

    main()

    manager_config = config_home / "skill-manager" / "config.yaml"
    catalog_path = manager_config.with_name("catalog.yaml")
    assert yaml.safe_load(manager_config.read_text()) == {"catalogs": ["catalog.yaml"]}
    catalog = yaml.safe_load(catalog_path.read_text())
    assert catalog["sources"]["local"]["path"] == "local-skills"
    assert (manager_config.parent / "local-skills").is_dir()
    assert not (data_home / "skill-manager" / "shims").exists()
    output = capsys.readouterr().out
    assert "Created starter configuration" in output
    assert "Skipped harness shim installation" in output


def test_inspect_uses_default_machine_configuration(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(sys, "argv", ["skm", "setup", "--no-shims"])
    main()
    monkeypatch.setattr(sys, "argv", ["skm", "sync"])
    main()
    capsys.readouterr()

    monkeypatch.setattr(sys, "argv", ["skm", "inspect"])
    main()

    output = capsys.readouterr().out
    assert "Complete inventory" in output
    assert "Selected: 0 skill(s)" in output


def test_inspect_can_summarize_tags_and_profiles(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(sys, "argv", ["skm", "setup", "--no-shims"])
    main()
    skill = tmp_path / "config" / "skill-manager" / "local-skills" / "alpha"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: alpha\n---\n# Alpha\n")
    monkeypatch.setattr(sys, "argv", ["skm", "sync"])
    main()
    capsys.readouterr()

    monkeypatch.setattr(sys, "argv", ["skm", "inspect", "--tags"])
    main()
    tags_output = capsys.readouterr().out
    assert "Tag breakdown" in tags_output
    assert "local" in tags_output

    monkeypatch.setattr(sys, "argv", ["skm", "inspect", "--profiles"])
    main()
    profiles_output = capsys.readouterr().out
    assert "Profile breakdown" in profiles_output
    assert "default" in profiles_output
    assert "1 skill" in profiles_output


def test_add_and_remove_refine_current_project_by_skill_and_tag(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    source = tmp_path / "source"
    for name in ("alpha", "beta", "gamma"):
        skill = source / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n# {name}\n")
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(
        yaml.safe_dump(
            {
                "sources": {
                    "local": {
                        "path": str(source),
                        "skills": {
                            "beta": {"tags": {"add": ["rust"]}},
                            "gamma": {"tags": {"add": ["rust"]}},
                        },
                    }
                },
                "profiles": {
                    "base": {"include": [{"source": "local", "skills": ["alpha"]}]}
                },
            },
            sort_keys=False,
        )
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "catalogs": ["catalog.yaml"],
                "default_project_profile": "base",
                "state_dir": "state",
            },
            sort_keys=False,
        )
    )
    project = tmp_path / "project"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", project],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(sys, "argv", ["skm", "sync", str(config_path)])
    main()
    capsys.readouterr()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "skm",
            "add",
            "--tag",
            "rust",
            "--project",
            str(project),
            "--config",
            str(config_path),
        ],
    )
    main()
    skills = project / ".agents" / "skills"
    assert {entry.name for entry in skills.iterdir()} == {"alpha", "beta", "gamma"}

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "skm",
            "remove",
            "beta",
            "--project",
            str(project),
            "--config",
            str(config_path),
        ],
    )
    main()
    assert {entry.name for entry in skills.iterdir()} == {"alpha", "gamma"}
    refinements = subprocess.run(
        [
            "git",
            "-C",
            project,
            "config",
            "--local",
            "--get",
            "skill-manager.refinements",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert '"include_tags":["rust"]' in refinements
    assert '"exclude_skills":["beta"]' in refinements


def test_setup_installs_shims_only_for_detected_harnesses(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    executable_dir = tmp_path / "executables"
    executable_dir.mkdir()
    for name in ("skm", "pi"):
        executable = executable_dir / name
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(executable_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(sys, "argv", ["skm", "setup"])

    main()

    shim_dir = tmp_path / "data" / "skill-manager" / "shims" / "bin"
    assert (shim_dir / "pi").is_file()
    assert not (shim_dir / "claude").exists()
    assert not (shim_dir / "codex").exists()
    output = capsys.readouterr().out
    assert "Configured harness shims: pi" in output
    assert "Skipped unavailable harnesses: claude, codex" in output
