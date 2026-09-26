import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from skill_manager.catalog import CatalogError
from skill_manager.config import LoadedConfiguration, load_configuration
from skill_manager.harness import (
    LAUNCH_MODE_ENV,
    Harness,
    LaunchKind,
    LaunchMode,
    classify_launch,
    harness_command,
    resolve_launch_mode,
)
from skill_manager.project import generate_global_adapters, run_harness
from skill_manager.storage import sync


class Executed(RuntimeError):
    """Raised in place of os.execvpe to capture a harness launch."""

    def __init__(self, command: list[str], environ: dict[str, str]) -> None:
        super().__init__("executed")
        self.command = command
        self.environ = environ


@pytest.fixture
def capture_exec(monkeypatch: pytest.MonkeyPatch):
    def fake_execvpe(file: str, argv: list[str], environ: dict[str, str]) -> None:
        raise Executed(list(argv), dict(environ))

    monkeypatch.setattr(os, "execvpe", fake_execvpe)


def test_management_subcommands_never_receive_isolation_flags() -> None:
    for harness, subcommand in (
        ("pi", "update"),
        ("pi", "install"),
        ("pi", "remove"),
        ("pi", "uninstall"),
        ("pi", "list"),
        ("pi", "config"),
        ("pi", "auth"),
        ("claude", "doctor"),
        ("claude", "mcp"),
        ("claude", "update"),
        ("codex", "exec"),
        ("codex", "resume"),
        ("codex", "login"),
    ):
        kind = classify_launch(Harness(harness), [subcommand, "--json"])
        assert kind is LaunchKind.PASSTHROUGH, f"{harness} {subcommand}"


def test_session_launches_stay_isolated() -> None:
    for harness, arguments in (
        ("pi", []),
        ("pi", ["-p", "add a test"]),
        ("pi", ["--no-skills", "explain this"]),
        ("pi", ["refactor update_handler.py"]),
        ("pi", ["--mode", "rpc"]),
        ("claude", ["fix the login bug"]),
        ("codex", ["-c", "model=o3", "summarize the diff"]),
    ):
        kind = classify_launch(Harness(harness), arguments)
        assert kind is LaunchKind.ISOLATED_SESSION, f"{harness} {arguments}"


def test_metadata_flags_pass_through_without_project_state() -> None:
    assert classify_launch(Harness.PI, ["--version"]) is LaunchKind.PASSTHROUGH
    assert classify_launch(Harness.CODEX, ["-h"]) is LaunchKind.PASSTHROUGH


def test_launch_mode_override_is_validated_and_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(LAUNCH_MODE_ENV, raising=False)
    assert resolve_launch_mode(os.environ) is LaunchMode.AUTO
    assert classify_launch(Harness.PI, ["update"], mode=LaunchMode.ISOLATE) is (
        LaunchKind.ISOLATED_SESSION
    )
    assert classify_launch(Harness.PI, [], mode=LaunchMode.PASSTHROUGH) is (
        LaunchKind.PASSTHROUGH
    )

    monkeypatch.setenv(LAUNCH_MODE_ENV, "  Isolate  ")
    assert resolve_launch_mode(os.environ) is LaunchMode.ISOLATE
    monkeypatch.setenv(LAUNCH_MODE_ENV, "sometimes")
    with pytest.raises(CatalogError, match="supported modes"):
        resolve_launch_mode(os.environ)


def test_configured_extra_subcommands_extend_passthrough_policy() -> None:
    assert classify_launch(Harness.PI, ["sessions"]) is LaunchKind.ISOLATED_SESSION
    assert (
        classify_launch(Harness.PI, ["sessions"], extra_subcommands=("sessions",))
        is LaunchKind.PASSTHROUGH
    )


def test_isolated_command_keeps_isolation_before_verbatim_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    _write_skill(project_root / ".agents" / "skills" / "local", "local")
    monkeypatch.chdir(project_root)

    command, _ = harness_command(
        "/real/pi",
        "pi",
        ["-p", "--keep  this", "@notes.md"],
        project_root=project_root,
    )

    assert command == [
        "/real/pi",
        "--no-skills",
        "--skill",
        str(project_root / ".agents" / "skills"),
        "-p",
        "--keep  this",
        "@notes.md",
    ]


def test_isolation_flags_per_harness_and_home_only_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    home = tmp_path / "home"
    _write_skill(home / ".agents" / "skills" / "global", "global")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project_root)

    claude, _ = harness_command(
        "/real/claude", Harness.CLAUDE, ["hi"], project_root=project_root
    )
    assert claude == ["/real/claude", "--setting-sources", "project,local", "hi"]

    codex, _ = harness_command(
        "/real/codex", Harness.CODEX, [], project_root=project_root
    )
    assert codex[1] == "-c"
    assert str(home / ".agents" / "skills" / "global" / "SKILL.md") in codex[2]
    assert "enabled=false" in codex[2]

    unmanaged, _ = harness_command(
        "/real/pi",
        "pi",
        ["hello"],
        project_root=project_root,
        isolate_skills=False,
    )
    assert unmanaged == ["/real/pi", "hello"]


def test_passthrough_launch_forwards_arguments_without_project_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_exec: None,
) -> None:
    configuration, project_root = _setup(tmp_path)
    executables = _fake_executables(tmp_path, "pi")
    monkeypatch.setenv("PATH", str(executables))

    with pytest.raises(Executed) as captured:
        run_harness(configuration, project_root, "pi", ["update", "--self"])

    assert captured.value.command == [str(executables / "pi"), "update", "--self"]
    assert not (project_root / ".agents").exists()
    assert not (project_root / ".claude").exists()


def test_management_subcommand_survives_an_unreadable_declaration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_exec: None,
) -> None:
    configuration, project_root = _setup(tmp_path)
    (project_root / ".skill-manager.yaml").write_text("unexpected: yes\n")
    executables = _fake_executables(tmp_path, "pi")
    monkeypatch.setenv("PATH", str(executables))

    with pytest.raises(Executed) as captured:
        run_harness(configuration, project_root, "pi", ["list"])

    assert captured.value.command == [str(executables / "pi"), "list"]


def test_project_run_forwards_arguments_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration, project_root = _setup(tmp_path)
    executables = _fake_executables(tmp_path, "pi")
    monkeypatch.setenv("PATH", str(executables))
    monkeypatch.chdir(project_root)
    driver = tmp_path / "skm-driver.py"
    driver.write_text("import sys\nfrom skill_manager.cli import main\n\nmain()\n")

    def run(*arguments: str) -> list[str]:
        result = subprocess.run(
            [sys.executable, str(driver), *arguments],
            check=True,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            },
        )
        return result.stdout.splitlines()

    assert run(
        "project",
        "run",
        str(configuration.config_path),
        str(project_root),
        "pi",
        "update",
        "--dry run",
    ) == [str(executables / "pi"), "update", "--dry run"]

    launched = run(
        "project",
        "run",
        str(configuration.config_path),
        str(project_root),
        "pi",
        "--",
        "-p",
        "hi there",
    )
    assert launched[0] == str(executables / "pi")
    assert "--no-skills" in launched
    assert launched[-2:] == ["-p", "hi there"]


def test_session_launch_applies_project_then_forwards_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_exec: None,
) -> None:
    configuration, project_root = _setup(tmp_path)
    executables = _fake_executables(tmp_path, "pi")
    monkeypatch.setenv("PATH", str(executables))
    monkeypatch.chdir(project_root)

    with pytest.raises(Executed) as captured:
        run_harness(configuration, project_root, "pi", ["-p", "run the tests"])

    assert captured.value.command[0] == str(executables / "pi")
    assert captured.value.command[1] == "--no-skills"
    assert captured.value.command[-2:] == ["-p", "run the tests"]
    assert (project_root / ".agents" / "skills" / "alpha").is_symlink()


def test_unsupported_harness_and_bad_declaration_fail_loudly(
    tmp_path: Path,
    capture_exec: None,
) -> None:
    configuration, project_root = _setup(tmp_path)
    (project_root / ".skill-manager.yaml").write_text("profile: nope\n")

    with pytest.raises(CatalogError, match="unknown project profile"):
        run_harness(configuration, project_root, "pi", ["-p", "hi"])


def test_generated_shims_forward_every_argument_verbatim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration, project_root = _setup(tmp_path)
    executables = _fake_executables(tmp_path, "pi", "skm")
    monkeypatch.setenv("PATH", str(executables))

    bin_dir = generate_global_adapters(configuration, ("pi",))
    forwarded = subprocess.run(
        [str(bin_dir / "pi"), "update", "--dry run", "-p", "-", "--", "@file"],
        check=True,
        capture_output=True,
        text=True,
        cwd=project_root,
    )

    assert forwarded.stdout.splitlines() == [
        "pi",
        "update",
        "--dry run",
        "-p",
        "-",
        "--",
        "@file",
    ]
    assert "skm project run" in (bin_dir / "pi").read_text()


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
        {"catalogs": ["catalog.yaml"], "state_dir": "state"},
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


def _fake_executables(tmp_path: Path, *names: str) -> Path:
    directory = tmp_path / "executables"
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        script = directory / name
        if name == "skm":
            script.write_text(
                "#!/bin/sh\n"
                'echo "$5"\n'
                "shift 6\n"
                'for argument in "$@"; do echo "$argument"; done\n'
            )
        else:
            script.write_text(
                '#!/bin/sh\necho "$0"\nfor argument in "$@"; do echo "$argument"; done\n'
            )
        script.chmod(0o755)
    return directory


def _write_skill(path: Path, name: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\n---\n# {name}\n")


def _dump(path: Path, value: object) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False))


def test_harness_passthrough_configuration_is_validated(tmp_path: Path) -> None:
    _dump(
        tmp_path / "catalog.yaml",
        {"sources": {}, "profiles": {"default": {"include": []}}},
    )
    _dump(
        tmp_path / "config.yaml",
        {"catalogs": ["catalog.yaml"], "harness_passthrough": {"p1": ["sessions"]}},
    )

    with pytest.raises(ValueError, match="not a supported harness"):
        load_configuration(tmp_path / "config.yaml")


def test_harness_passthrough_configuration_reaches_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_exec: None,
) -> None:
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
            "harness_passthrough": {"pi": ["sessions"]},
        },
    )
    project_root = tmp_path / "project"
    project_root.mkdir()
    _dump(project_root / ".skill-manager.yaml", {"profile": "default"})
    configuration = load_configuration(tmp_path / "config.yaml")
    sync(configuration)
    executables = _fake_executables(tmp_path, "pi")
    monkeypatch.setenv("PATH", str(executables))

    with pytest.raises(Executed) as captured:
        run_harness(configuration, project_root, "pi", ["sessions"])

    assert captured.value.command == [str(executables / "pi"), "sessions"]
    assert not (project_root / ".agents").exists()
