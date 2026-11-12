"""Preflighted ownership plans for project skill links and legacy manifests."""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from skill_manager.catalog import CatalogError, ResolvedSkill
from skill_manager.filesystem import Transaction, is_identifier, yaml_bytes


class ConsumerManifest(BaseModel):
    """Version one stays compatible with manifests written before the pivot."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    consumer_path: Path
    entries: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_entries(self) -> Self:
        if any(not is_identifier(name) for name in self.entries):
            raise ValueError("invalid skill name in consumer manifest")
        return self


@dataclass(frozen=True)
class Consumer:
    path: Path
    manifest_path: Path


@dataclass(frozen=True)
class ConsumerPlan:
    consumer: Consumer
    previous: ConsumerManifest
    desired: dict[str, str]


def project_identity(root: Path) -> str:
    # Preserve the original manifest IDs so migration retains ownership evidence.
    return hashlib.sha256(str(root).encode()).hexdigest()[:16]


def consumers(root: Path, state_dir: Path) -> tuple[Consumer, ...]:
    identity = project_identity(root)
    return tuple(
        Consumer(
            root / directory / "skills",
            state_dir / "manifests" / f"project-{identity}-{name}.yaml",
        )
        for directory, name in ((".agents", "agents"), (".claude", "claude"))
    )


def check_consumer_path(root: Path, path: Path) -> None:
    """Check lexical paths before resolving them, including parent aliases."""
    for current in (path.parent, path):
        if current.is_symlink():
            raise CatalogError(
                f"consumer directory is a symlink; refusing to modify it: {current}"
            )
        if os.path.lexists(current) and not current.is_dir():
            raise CatalogError(f"consumer directory is not a directory: {current}")
    if not path.is_relative_to(root) or path == root:
        raise CatalogError(f"unsafe consumer directory: {path}")


def load_manifest(consumer: Consumer) -> ConsumerManifest | None:
    path = consumer.manifest_path
    if path.is_symlink():
        raise CatalogError(f"consumer manifest is a symlink: {path}")
    try:
        manifest = ConsumerManifest.model_validate(yaml.safe_load(path.read_text()))
    except FileNotFoundError:
        return None
    except (OSError, yaml.YAMLError) as error:
        raise CatalogError(f"cannot read consumer manifest {path}: {error}") from error
    if manifest.consumer_path != consumer.path:
        raise CatalogError(
            f"consumer path changed since the last install: {consumer.path}"
        )
    return manifest


def plan_consumers(
    root: Path,
    state_dir: Path,
    selected: tuple[ResolvedSkill, ...],
    view: Path | None,
) -> tuple[ConsumerPlan, ...]:
    """Validate every destination before any consumer or manifest is modified."""
    plans: list[ConsumerPlan] = []
    for consumer in consumers(root, state_dir):
        check_consumer_path(root, consumer.path)
        previous = load_manifest(consumer) or ConsumerManifest(
            consumer_path=consumer.path
        )
        desired = (
            {skill.name: str(view / skill.name) for skill in selected} if view else {}
        )
        for name, target in previous.entries.items():
            path = consumer.path / name
            if os.path.lexists(path) and (
                not path.is_symlink() or os.readlink(path) != target
            ):
                raise CatalogError(
                    f"managed entry changed externally; refusing to replace it: {path}"
                )
        for name in desired.keys() - previous.entries.keys():
            if os.path.lexists(consumer.path / name):
                raise CatalogError(
                    f"entry already exists and is not managed: {consumer.path / name}"
                )
        plans.append(ConsumerPlan(consumer, previous, desired))
    return tuple(plans)


def apply_plans(
    plans: tuple[ConsumerPlan, ...],
    transaction: Transaction,
    *,
    uninstall: bool = False,
) -> None:
    for plan in plans:
        for name in plan.previous.entries.keys() - plan.desired.keys():
            transaction.remove(plan.consumer.path / name)
        for name, target in plan.desired.items():
            transaction.symlink(plan.consumer.path / name, target)
        if uninstall:
            transaction.remove(plan.consumer.manifest_path)
        else:
            manifest = ConsumerManifest(
                consumer_path=plan.consumer.path, entries=plan.desired
            )
            transaction.write(
                plan.consumer.manifest_path,
                yaml_bytes(manifest.model_dump(mode="json")),
            )


def consumer_issues(
    root: Path,
    state_dir: Path,
    selected: tuple[ResolvedSkill, ...],
    view: Path,
) -> list[str]:
    """Report per-skill drift without repairing it or acquiring source content."""
    issues: list[str] = []
    by_name = {skill.name: skill for skill in selected}
    for consumer in consumers(root, state_dir):
        check_consumer_path(root, consumer.path)
        manifest = load_manifest(consumer)
        if manifest is None:
            issues.append(f"{consumer.path}: not installed (no ownership manifest)")
            continue
        for name in sorted(manifest.entries.keys() - by_name.keys()):
            issues.append(f"{consumer.path / name}: stale installed skill")
        for name in sorted(by_name.keys() - manifest.entries.keys()):
            issues.append(f"{consumer.path / name}: skill is not installed")
        for name, target in manifest.entries.items():
            path = consumer.path / name
            if not os.path.lexists(path):
                issues.append(f"{path}: managed link is missing")
            elif not path.is_symlink() or os.readlink(path) != target:
                issues.append(f"{path}: managed link changed externally")
            elif not (path / "SKILL.md").is_file():
                issues.append(f"{path}: skill target is missing")
            elif name in by_name and (
                target != str(view / name)
                or path.resolve() != by_name[name].path.resolve()
            ):
                issues.append(
                    f"{path}: installed target differs from the locked selection"
                )
    return issues
