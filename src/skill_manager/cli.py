"""Commands for explicit installation from centrally curated skill catalogs."""

import argparse
import os
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from skill_manager.catalog import CatalogError, resolve_profile
from skill_manager.project import (
    install_project,
    list_project,
    project_status,
    uninstall_project,
)


class Command(StrEnum):
    INSTALL = "install"
    UPDATE = "update"
    STATUS = "status"
    UNINSTALL = "uninstall"
    LIST = "list"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skm", description="Install project skills from a curated catalog."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    state = Path(
        os.environ.get(
            "SKILL_MANAGER_STATE_DIR",
            str(
                Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
                / "skill-manager"
            ),
        )
    )
    helps = {
        Command.INSTALL: "reproduce the project lock, creating it on first install",
        Command.UPDATE: "adopt the catalog's current selection and approved versions",
        Command.STATUS: "check local installation drift without networking",
        Command.UNINSTALL: "remove only verified manager-owned project links",
        Command.LIST: "show skills or profiles in the project's catalog",
    }
    for command in Command:
        subparser = commands.add_parser(command.value, help=helps[command])
        subparser.add_argument("--project", type=Path, default=Path.cwd())
        subparser.add_argument(
            "--state-dir",
            type=Path,
            default=state,
            help="shared source store and ownership state",
        )
        if command is Command.INSTALL:
            subparser.add_argument(
                "--locked",
                action="store_true",
                help="require a compatible existing lock",
            )
        elif command is Command.STATUS:
            subparser.add_argument(
                "--check", action="store_true", help="exit 1 when local state differs"
            )
        elif command is Command.LIST:
            subparser.add_argument(
                "--profiles",
                action="store_true",
                help="list profiles instead of skills",
            )
    return parser


def main() -> None:
    parser = _build_parser()
    arguments = parser.parse_args()
    command = Command(arguments.command)
    state_dir = arguments.state_dir.expanduser().resolve()
    console = Console()
    try:
        if state_dir in {Path(state_dir.anchor), Path.home()}:
            raise CatalogError(f"unsafe state directory: {state_dir}")
        if command in {Command.INSTALL, Command.UPDATE}:
            resolution = install_project(
                arguments.project,
                state_dir,
                update=command is Command.UPDATE,
                locked=getattr(arguments, "locked", False),
            )
            action = "Updated" if command is Command.UPDATE else "Installed"
            console.print(
                f"[green]✓[/] {action} {len(resolution.selected)} skill(s) from the project's catalog"
            )
        elif command is Command.UNINSTALL:
            count = uninstall_project(arguments.project, state_dir)
            console.print(
                f"[green]✓[/] Uninstalled {count} managed skill(s); retained the declaration, lock and source store"
            )
        elif command is Command.STATUS:
            status = project_status(arguments.project, state_dir)
            for issue in status.issues:
                console.print(f"[yellow]DRIFT:[/] {escape(issue)}")
            if not status.issues:
                console.print(
                    f"[green]✓[/] {len(status.selected_names)} skill(s) match the project lock"
                )
            if arguments.check and status.issues:
                raise SystemExit(1)
        else:
            resolution = list_project(arguments.project, state_dir)
            table = Table(
                "Profile" if arguments.profiles else "Skill",
                "Sources" if arguments.profiles else "Source",
                "Selection",
            )
            if arguments.profiles:
                for name, profile in sorted(resolution.lock.catalog.profiles.items()):
                    try:
                        skills = resolve_profile(name, profile, resolution.sources)
                    except CatalogError:
                        skills = ()
                    table.add_row(
                        escape(name),
                        escape(", ".join(sorted({skill.source for skill in skills}))),
                        str(len(skills)),
                    )
            else:
                selected = {(skill.source, skill.name) for skill in resolution.selected}
                for source in resolution.sources:
                    for skill in source.skills:
                        table.add_row(
                            escape(skill.name),
                            escape(skill.source),
                            "selected"
                            if (skill.source, skill.name) in selected
                            else "",
                        )
            console.print(table)
    except (CatalogError, ValidationError, OSError) as error:
        parser.error(str(error))
