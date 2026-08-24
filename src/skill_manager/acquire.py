"""Temporary source acquisition for the catalog prototype."""

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from skill_manager.catalog import CatalogError, SourceDeclaration


@dataclass(frozen=True)
class SourceCheckout:
    root: Path
    resolved_revision: str | None


@contextmanager
def acquire_source(
    source_name: str,
    source: SourceDeclaration,
    catalog_dir: Path,
) -> Iterator[SourceCheckout]:
    """Yield a local source tree, cloning Git sources into temporary storage."""
    if source.path is not None:
        root = source.path.expanduser()
        if not root.is_absolute():
            root = catalog_dir / root
        root = root.resolve()
        if not root.is_dir():
            raise CatalogError(f"source '{source_name}' path does not exist: {root}")
        yield SourceCheckout(root=root, resolved_revision=None)
        return

    assert source.git is not None
    assert source.ref is not None
    with TemporaryDirectory(prefix=f"skill-manager-{source_name}-") as temporary:
        root = Path(temporary) / "source"
        _run_git("init", "--quiet", str(root))
        _run_git("-C", str(root), "remote", "add", "origin", source.git)
        _run_git(
            "-C",
            str(root),
            "fetch",
            "--quiet",
            "--depth",
            "1",
            "origin",
            source.ref,
        )
        _run_git("-C", str(root), "checkout", "--quiet", "--detach", "FETCH_HEAD")
        revision = _run_git("-C", str(root), "rev-parse", "HEAD").strip()
        yield SourceCheckout(root=root, resolved_revision=revision)


@contextmanager
def acquire_locked_source(
    source_name: str,
    source: SourceDeclaration,
    resolved_revision: str,
) -> Iterator[SourceCheckout]:
    """Yield a Git source at an existing lock without advancing its revision."""
    if source.git is None or source.ref is None:
        raise CatalogError(f"source '{source_name}' is not a Git source")
    with TemporaryDirectory(prefix=f"skill-manager-{source_name}-") as temporary:
        root = Path(temporary) / "source"
        _run_git("init", "--quiet", str(root))
        _run_git("-C", str(root), "remote", "add", "origin", source.git)
        _run_git("-C", str(root), "fetch", "--quiet", "origin", source.ref)
        try:
            _run_git(
                "-C",
                str(root),
                "checkout",
                "--quiet",
                "--detach",
                resolved_revision,
            )
        except CatalogError as error:
            raise CatalogError(
                f"locked revision {resolved_revision} for source '{source_name}' "
                "is not available from its requested ref"
            ) from error
        actual_revision = _run_git("-C", str(root), "rev-parse", "HEAD").strip()
        if actual_revision != resolved_revision:
            raise CatalogError(
                f"source '{source_name}' resolved to {actual_revision}, "
                f"expected locked revision {resolved_revision}"
            )
        yield SourceCheckout(root=root, resolved_revision=actual_revision)


def _run_git(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise CatalogError("git is required to inspect Git sources") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or error.stdout.strip() or "unknown Git error"
        raise CatalogError(f"git {' '.join(arguments)} failed: {detail}") from error
    return result.stdout
