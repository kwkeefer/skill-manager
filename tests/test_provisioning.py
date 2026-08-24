import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from skill_manager.catalog import CatalogError
from skill_manager.config import LoadedConfiguration, load_configuration
from skill_manager.provision import apply_profile, doctor
from skill_manager.storage import (
    build_profile_view,
    load_lock,
    resolve_locked_profile,
    sync,
)


def test_locked_git_skill_preserves_supporting_files_after_source_removed(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    skill = repository / "skills" / "alpha"
    _write_skill(skill, "alpha")
    (skill / "references").mkdir()
    (skill / "references" / "guide.md").write_text("# Supporting guide\n")
    (skill / "scripts").mkdir()
    script = skill / "scripts" / "validate.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    subprocess.run(
        ["git", "init", "-b", "main", repository], check=True, capture_output=True
    )
    subprocess.run(["git", "-C", repository, "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            repository,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    catalog = {
        "sources": {
            "demo": {
                "git": str(repository),
                "ref": "main",
                "tags": ["coding"],
            }
        },
        "profiles": {"default": {"include": [{"source": "demo"}]}},
    }
    configuration = _write_configuration(tmp_path, catalog)

    lock = sync(configuration)
    stored = (
        configuration.state_dir
        / "sources"
        / "demo"
        / lock.sources["demo"].resolved_revision
    )
    assert stored.is_dir()

    # Apply must use only the lock and persistent store, not the original source.
    repository.rename(tmp_path / "repository-removed")
    selected = resolve_locked_profile(configuration, load_lock(configuration))[1]
    view = build_profile_view(configuration, selected)
    apply_profile(configuration, selected, view)

    installed = configuration.consumers["agents"].path / "alpha"
    assert installed.is_symlink()
    assert (installed / "SKILL.md").is_file()
    assert (installed / "references" / "guide.md").read_text() == "# Supporting guide\n"
    installed_script = installed / "scripts" / "validate.sh"
    assert installed_script.read_text() == "#!/bin/sh\nexit 0\n"
    assert os.access(installed_script, os.X_OK)
    assert doctor(configuration, selected) == []


def test_sync_hydrates_locked_revision_without_advancing_branch(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    skill = repository / "skills" / "alpha"
    _write_skill(skill, "alpha")
    (skill / "version.txt").write_text("one\n")
    subprocess.run(
        ["git", "init", "-b", "main", repository], check=True, capture_output=True
    )
    _commit(repository, "one")
    catalog = {
        "sources": {"demo": {"git": str(repository), "ref": "main"}},
        "profiles": {"default": {"include": [{"source": "demo"}]}},
    }
    configuration = _write_configuration(tmp_path, catalog)
    original_lock = sync(configuration)

    (skill / "version.txt").write_text("two\n")
    _commit(repository, "two")
    shutil.rmtree(configuration.state_dir / "sources")

    hydrated_lock = sync(configuration)
    selected = resolve_locked_profile(configuration, hydrated_lock)[1]

    assert hydrated_lock == original_lock
    assert (selected[0].path / "version.txt").read_text() == "one\n"

    updated_lock = sync(configuration, update_lock=True)
    assert updated_lock.sources["demo"] != original_lock.sources["demo"]


def test_shared_consumer_preserves_reserved_and_removes_only_stale_managed_links(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write_skill(source / "alpha", "alpha")
    _write_skill(source / "beta", "beta")
    catalog = {
        "sources": {"demo": {"path": str(source)}},
        "profiles": {"default": {"include": [{"source": "demo"}]}},
    }
    configuration = _write_configuration(
        tmp_path,
        catalog,
        reserved=["external"],
    )
    consumer = configuration.consumers["agents"].path
    consumer.mkdir(parents=True)
    (consumer / "external").mkdir()

    lock = sync(configuration)
    selected = resolve_locked_profile(configuration, lock)[1]
    apply_profile(configuration, selected, build_profile_view(configuration, selected))
    assert (consumer / "alpha").is_symlink()
    assert (consumer / "beta").is_symlink()

    catalog["profiles"]["default"] = {
        "include": [{"source": "demo", "skills": ["alpha"]}]
    }
    _dump(tmp_path / "catalog.yaml", catalog)
    configuration = load_configuration(tmp_path / "config.yaml")
    lock = sync(configuration)
    selected = resolve_locked_profile(configuration, lock)[1]
    apply_profile(configuration, selected, build_profile_view(configuration, selected))

    assert (consumer / "alpha").is_symlink()
    assert not os.path.lexists(consumer / "beta")
    assert (consumer / "external").is_dir()


def test_doctor_reports_manifest_that_does_not_match_current_selection(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write_skill(source / "alpha", "alpha")
    _write_skill(source / "beta", "beta")
    catalog = {
        "sources": {"demo": {"path": str(source)}},
        "profiles": {"default": {"include": [{"source": "demo"}]}},
    }
    configuration = _write_configuration(tmp_path, catalog)
    lock = sync(configuration)
    selected = resolve_locked_profile(configuration, lock)[1]
    apply_profile(configuration, selected, build_profile_view(configuration, selected))

    issues = doctor(
        configuration, tuple(skill for skill in selected if skill.name == "alpha")
    )

    assert issues == ["consumer 'agents': apply manifest has stale skill 'beta'"]


def test_doctor_reports_skill_linked_to_old_source_revision(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_skill(source / "alpha", "alpha")
    catalog = {
        "sources": {"demo": {"path": str(source)}},
        "profiles": {"default": {"include": [{"source": "demo"}]}},
    }
    configuration = _write_configuration(tmp_path, catalog)
    lock = sync(configuration)
    selected = resolve_locked_profile(configuration, lock)[1]
    apply_profile(configuration, selected, build_profile_view(configuration, selected))
    replacement = tmp_path / "replacement" / "alpha"
    _write_skill(replacement, "alpha")
    current_selection = (selected[0].model_copy(update={"path": replacement}),)

    issues = doctor(configuration, current_selection)

    assert issues == ["consumer 'agents': managed skill 'alpha' points to stale source"]


def test_apply_refuses_active_reserved_skill(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_skill(source / "alpha", "alpha")
    catalog = {
        "sources": {"demo": {"path": str(source)}},
        "profiles": {"default": {"include": [{"source": "demo"}]}},
    }
    configuration = _write_configuration(tmp_path, catalog, reserved=["alpha"])
    lock = sync(configuration)
    selected = resolve_locked_profile(configuration, lock)[1]

    with pytest.raises(CatalogError, match="reserves active skills: alpha"):
        apply_profile(
            configuration, selected, build_profile_view(configuration, selected)
        )


def test_apply_refuses_to_overwrite_unmanaged_collision(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_skill(source / "alpha", "alpha")
    catalog = {
        "sources": {"demo": {"path": str(source)}},
        "profiles": {"default": {"include": [{"source": "demo"}]}},
    }
    configuration = _write_configuration(tmp_path, catalog)
    lock = sync(configuration)
    selected = resolve_locked_profile(configuration, lock)[1]
    consumer = configuration.consumers["agents"].path
    (consumer / "alpha").mkdir(parents=True)

    with pytest.raises(CatalogError, match="already exists and is not managed"):
        apply_profile(
            configuration, selected, build_profile_view(configuration, selected)
        )

    assert (consumer / "alpha").is_dir()


def _write_configuration(
    root: Path,
    catalog: dict,
    *,
    reserved: list[str] | None = None,
) -> LoadedConfiguration:
    _dump(root / "catalog.yaml", catalog)
    _dump(
        root / "config.yaml",
        {
            "catalogs": ["catalog.yaml"],
            "active_profile": "default",
            "state_dir": "state",
            "consumers": {
                "agents": {
                    "path": "consumer",
                    "ownership": "shared",
                    "reserved": reserved or [],
                }
            },
        },
    )
    return load_configuration(root / "config.yaml")


def _commit(repository: Path, message: str) -> None:
    subprocess.run(["git", "-C", repository, "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            repository,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            message,
        ],
        check=True,
        capture_output=True,
    )


def _write_skill(path: Path, name: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\n---\n# {name}\n")


def _dump(path: Path, value: object) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False))
