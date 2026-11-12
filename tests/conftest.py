"""Shared local-only installer fixtures."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml


def dump(path: Path, value: object) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False))


def write_skill(path: Path, name: str, content: str = "") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\n---\n{content}\n")


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(root: Path, message: str) -> str:
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        message,
    )
    return git(root, "rev-parse", "HEAD")


@dataclass(frozen=True)
class Workspace:
    root: Path
    state: Path
    source: Path
    catalog: Path

    def declare(self, *, profile: str = "base", **extra: object) -> None:
        dump(
            self.root / ".skill-manager.yaml",
            {
                "catalog": {"path": str(self.catalog)},
                "profile": profile,
                **extra,
            },
        )


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    source = tmp_path / "source"
    write_skill(source / "alpha", "alpha", "alpha contents")
    write_skill(source / "beta", "beta", "beta contents")
    catalog = tmp_path / "catalog.yaml"
    dump(
        catalog,
        {
            "sources": {"local": {"path": "source", "tags": ["coding"]}},
            "profiles": {
                "base": {"include": [{"source": "local", "skills": ["alpha"]}]},
                "all": {"include": [{"source": "local"}]},
            },
        },
    )
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-q", "-b", "main")
    result = Workspace(project, tmp_path / "state", source, catalog)
    result.declare()
    return result
