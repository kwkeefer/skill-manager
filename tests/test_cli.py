import os
import subprocess
import sys

import pytest
import yaml
from conftest import Workspace, dump

from skill_manager.cli import main
from skill_manager.project import install_project


def invoke(workspace: Workspace, monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "skm",
            *args,
            "--project",
            str(workspace.root),
            "--state-dir",
            str(workspace.state),
        ],
    )
    main()


def test_cli_install_status_list_and_uninstall(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    invoke(workspace, monkeypatch, "install")
    assert "Installed 1 skill" in capsys.readouterr().out
    invoke(workspace, monkeypatch, "status", "--check")
    assert "match the project lock" in capsys.readouterr().out
    invoke(workspace, monkeypatch, "list")
    assert "alpha" in capsys.readouterr().out
    invoke(workspace, monkeypatch, "list", "--profiles")
    assert "all" in capsys.readouterr().out
    invoke(workspace, monkeypatch, "uninstall")
    assert "Uninstalled 1 managed skill" in capsys.readouterr().out


def test_cli_status_check_fails_on_drift_but_plain_status_reports_it(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_project(workspace.root, workspace.state)
    (workspace.root / ".agents/skills/alpha").unlink()
    with pytest.raises(SystemExit) as error:
        invoke(workspace, monkeypatch, "status", "--check")
    assert error.value.code == 1
    assert "missing" in capsys.readouterr().out
    invoke(workspace, monkeypatch, "status")
    assert "DRIFT" in capsys.readouterr().out


def test_cli_missing_lock_is_drift_and_locked_install_does_not_create_it(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SystemExit) as status:
        invoke(workspace, monkeypatch, "status", "--check")
    assert status.value.code == 1
    with pytest.raises(SystemExit) as install:
        invoke(workspace, monkeypatch, "install", "--locked")
    assert install.value.code == 2
    assert not (workspace.root / ".skill-manager.lock").exists()


def test_removed_interception_commands_are_not_available(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["skm", "--help"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert "{install,update,status,uninstall,list}" in help_text
    for command in ("setup", "sync", "apply", "doctor", "project", "add", "remove"):
        monkeypatch.setattr(sys, "argv", ["skm", command])
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 2


def test_legacy_declaration_gets_migration_message(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dump(
        workspace.root / ".skill-manager.yaml",
        {"profile": "base", "global_skills": "disabled"},
    )
    with pytest.raises(SystemExit):
        invoke(workspace, monkeypatch, "install")
    assert "legacy project declaration" in capsys.readouterr().err


def test_environment_only_controls_store_not_catalog_selection(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SKILL_MANAGER_STATE_DIR", str(workspace.state))
    monkeypatch.setenv(
        "SKILL_MANAGER_CONFIG", str(workspace.root / "missing-machine-config.yaml")
    )
    monkeypatch.setattr(
        sys, "argv", ["skm", "install", "--project", str(workspace.root)]
    )
    main()
    assert (workspace.root / ".agents/skills/alpha").is_symlink()


def test_list_before_install_creates_no_project_files(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoke(workspace, monkeypatch, "list", "--profiles")
    assert not (workspace.root / ".agents").exists()
    assert not (workspace.root / ".skill-manager.lock").exists()


@pytest.mark.parametrize("project_selectors", [False, True])
def test_fingerprints_are_stable_between_cli_processes(
    workspace: Workspace, project_selectors: bool
) -> None:
    catalog = yaml.safe_load(workspace.catalog.read_text())
    catalog["sources"]["local"]["tags"] = ["coding", "python", "testing"]
    catalog["profiles"]["base"]["include"] = [
        {"source": "local", "skills": ["alpha", "beta", "gamma"]}
    ]
    dump(workspace.catalog, catalog)
    if project_selectors:
        workspace.declare(
            include=[{"skills": ["alpha", "beta", "gamma"]}],
            exclude=[{"tags": ["experimental", "private", "disabled"]}],
        )

    def run(seed: int, *arguments: str) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from skill_manager.cli import main; main()",
                *arguments,
                "--project",
                str(workspace.root),
                "--state-dir",
                str(workspace.state),
            ],
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    run(1, "install")
    lock = workspace.root / ".skill-manager.lock"
    before = lock.read_bytes()
    receipt = next((workspace.state / "installations").glob("*.yaml"))
    before_receipt = receipt.read_bytes()
    for seed in (2, 3):
        run(seed, "status", "--check")
        run(seed, "install", "--locked")
        assert lock.read_bytes() == before
        assert receipt.read_bytes() == before_receipt
