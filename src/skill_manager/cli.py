"""Command-line entry point for skill catalog and provisioning operations."""

import argparse
import os
from pathlib import Path

from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from skill_manager.catalog import (
    CatalogError,
    ResolvedSkill,
    ResolvedSource,
    resolve_profile,
)
from skill_manager.config import (
    LoadedConfiguration,
    initialize_configuration,
    load_configuration,
)
from skill_manager.project import (
    ProjectRefinements,
    apply_project,
    environment_export,
    generate_adapters,
    generate_global_adapters,
    installed_harnesses,
    load_project,
    refine_project_selection,
    run_harness,
    set_project_profile,
    set_project_refinements,
)
from skill_manager.provision import apply_profile, doctor
from skill_manager.storage import (
    build_profile_view,
    load_lock,
    resolve_locked_inventory,
    resolve_locked_profile,
    sync,
)


def main() -> None:
    parser = _build_parser()
    arguments = parser.parse_args()
    if (
        arguments.command == "inspect"
        and arguments.profiles
        and arguments.profile is not None
    ):
        parser.error("--profiles cannot be combined with --profile")
    try:
        if arguments.command == "inspect":
            configuration = load_configuration(arguments.config)
            if arguments.profiles:
                sources = resolve_locked_inventory(
                    configuration, load_lock(configuration)
                )
                _print_profile_breakdown(configuration, sources)
            else:
                sources, selected, profile = inspect_configuration(
                    configuration, arguments.profile
                )
                if arguments.tags:
                    _print_tag_breakdown(sources, selected, profile)
                else:
                    _print_inventory(sources, selected, profile)
        elif arguments.command == "project":
            configuration = load_configuration(arguments.config)
            _run_project_command(arguments, configuration)
        elif arguments.command in {"add", "remove"}:
            configuration = load_configuration(arguments.config)
            _run_refinement_command(arguments, configuration)
        elif arguments.command == "setup":
            console = Console()
            created = initialize_configuration(arguments.config)
            configuration = load_configuration(arguments.config)
            harnesses = () if arguments.no_shims else installed_harnesses(configuration)
            bin_dir = (
                None
                if arguments.no_shims
                else generate_global_adapters(configuration, harnesses)
            )
            if created:
                console.print(
                    "[green]✓[/] Created starter configuration at "
                    f"[bold]{escape(str(configuration.config_path))}[/]"
                )
                console.print(
                    "  Edit "
                    f"[bold]{escape(str(configuration.config_path.with_name('catalog.yaml')))}[/], "
                    "then run [bold]skm sync[/].\n"
                )
            if arguments.no_shims:
                console.print("[yellow]–[/] Skipped harness shim installation")
            elif harnesses:
                console.print(
                    f"[green]✓[/] Configured harness shims: [bold]{', '.join(harnesses)}[/]"
                )
                unavailable = sorted({"pi", "claude", "codex"} - set(harnesses))
                if unavailable:
                    console.print(
                        "[yellow]–[/] Skipped unavailable harnesses: "
                        f"{', '.join(unavailable)}"
                    )
                assert bin_dir is not None
                console.print("\n[bold]Add this to your shell startup file:[/]\n")
                print(environment_export(bin_dir))
            else:
                console.print(
                    "[yellow]–[/] No installed harnesses found; no shims were configured"
                )
        else:
            configuration = load_configuration(arguments.config)
            _run_lifecycle_command(
                arguments.command,
                configuration,
                update_lock=getattr(arguments, "update_lock", False),
            )
    except (CatalogError, ValidationError) as error:
        parser.error(str(error))


def _default_config_path() -> Path:
    configured = os.environ.get("SKILL_MANAGER_CONFIG")
    if configured:
        return Path(configured).expanduser()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "skill-manager" / "config.yaml"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skm")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser(
        "inspect", help="inspect a catalog without persisting it"
    )
    inspect.add_argument("--config", type=Path, default=_default_config_path())
    inspect.add_argument("--profile")
    inspect_views = inspect.add_mutually_exclusive_group()
    inspect_views.add_argument(
        "--tags", action="store_true", help="summarize skills by effective tag"
    )
    inspect_views.add_argument(
        "--profiles", action="store_true", help="summarize every configured profile"
    )

    for command in ("add", "remove"):
        refinement = commands.add_parser(
            command,
            help=f"{command} skills or tags in the current project's selection",
        )
        refinement.add_argument("skills", nargs="*")
        refinement.add_argument("--tag", action="append", default=[], dest="tags")
        refinement.add_argument("--project", type=Path, default=Path.cwd())
        refinement.add_argument("--config", type=Path, default=_default_config_path())

    setup = commands.add_parser(
        "setup", help="initialize configuration and harness shims"
    )
    setup.add_argument("--config", type=Path, default=_default_config_path())
    setup.add_argument(
        "--no-shims",
        action="store_true",
        help="initialize configuration without installing harness shims",
    )

    sync_command = commands.add_parser(
        "sync", help="converge local sources on locked revisions"
    )
    sync_command.add_argument(
        "config", type=Path, nargs="?", default=_default_config_path()
    )
    sync_command.add_argument(
        "--update-lock",
        action="store_true",
        help="resolve requested refs again and rewrite the lock",
    )

    for command, help_text in (
        ("apply", "apply an explicitly configured global profile"),
        ("doctor", "check global lock and consumer state without changing it"),
    ):
        lifecycle = commands.add_parser(command, help=help_text)
        lifecycle.add_argument(
            "config",
            type=Path,
            nargs="?",
            default=_default_config_path(),
        )

    project = commands.add_parser("project", help="project-scoped activation")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    for command, help_text in (
        ("apply", "materialize the project's locked profile"),
        ("env", "emit shell environment for isolated harness adapters"),
    ):
        project_command = project_commands.add_parser(command, help=help_text)
        project_command.add_argument("config", type=Path)
        project_command.add_argument(
            "project", type=Path, nargs="?", default=Path.cwd()
        )

    use = project_commands.add_parser("use", help="bind a profile in local Git config")
    use.add_argument("profile")
    use.add_argument("project", type=Path, nargs="?", default=Path.cwd())
    use.add_argument("--config", type=Path, default=_default_config_path())

    run = project_commands.add_parser("run", help="launch an isolated harness")
    run.add_argument("config", type=Path)
    run.add_argument("project", type=Path)
    run.add_argument("harness", choices=("pi", "claude", "codex"))
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser


def _run_refinement_command(
    arguments: argparse.Namespace,
    configuration: LoadedConfiguration,
) -> None:
    requested_skills = set(arguments.skills)
    requested_tags = set(arguments.tags)
    if not requested_skills and not requested_tags:
        raise CatalogError(f"skm {arguments.command} requires a skill or --tag")

    project = load_project(arguments.project, configuration.default_project_profile)
    lock = load_lock(configuration)
    sources = resolve_locked_inventory(configuration, lock)
    inventory = tuple(skill for source in sources for skill in source.skills)
    available_skills = {skill.name for skill in inventory}
    available_tags = {tag for skill in inventory for tag in skill.tags}
    missing_skills = requested_skills - available_skills
    if missing_skills:
        raise CatalogError(f"unknown skill '{min(missing_skills)}'")
    missing_tags = requested_tags - available_tags
    if missing_tags:
        raise CatalogError(f"unknown tag '{min(missing_tags)}'")

    current = project.refinements
    include_skills = set(current.include_skills)
    exclude_skills = set(current.exclude_skills)
    include_tags = set(current.include_tags)
    exclude_tags = set(current.exclude_tags)
    if arguments.command == "add":
        include_skills.update(requested_skills)
        exclude_skills.difference_update(requested_skills)
        include_tags.update(requested_tags)
        exclude_tags.difference_update(requested_tags)
    else:
        exclude_skills.update(requested_skills)
        include_skills.difference_update(requested_skills)
        exclude_tags.update(requested_tags)
        include_tags.difference_update(requested_tags)

    refinements = ProjectRefinements(
        include_skills=include_skills,
        exclude_skills=exclude_skills,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
    )
    profile_configuration = configuration.model_copy(
        update={"active_profile": project.declaration.profile}
    )
    _, base = resolve_locked_profile(profile_configuration, lock)
    refine_project_selection(sources, base, refinements)
    set_project_refinements(project.root, refinements)
    refined_project = project.model_copy(update={"refinements": refinements})
    _, _, count = apply_project(configuration, refined_project)
    action = "Added" if arguments.command == "add" else "Removed"
    Console().print(
        f"[green]✓[/] {action} project refinements; "
        f"[bold]{count}[/] skill(s) now active"
    )


def _run_project_command(
    arguments: argparse.Namespace,
    configuration: LoadedConfiguration,
) -> None:
    if arguments.project_command == "use":
        if arguments.profile not in configuration.catalog.profiles:
            raise CatalogError(f"unknown profile '{arguments.profile}'")
        root = set_project_profile(arguments.project, arguments.profile)
        Console().print(
            f"[green]✓[/] Bound [bold]{escape(str(root))}[/] to profile "
            f"[cyan]{escape(arguments.profile)}[/] in local Git config"
        )
        return

    project = load_project(arguments.project, configuration.default_project_profile)
    if arguments.project_command == "apply":
        project_configuration, _, count = apply_project(configuration, project)
        Console().print(
            "[green]✓[/] Applied project profile "
            f"[cyan]{escape(str(project_configuration.active_profile))}[/] "
            f"with [bold]{count}[/] skill(s)"
        )
    elif arguments.project_command == "env":
        print(environment_export(generate_adapters(configuration, project)))
    elif arguments.project_command == "run":
        harness_arguments = arguments.arguments
        if harness_arguments[:1] == ["--"]:
            harness_arguments = harness_arguments[1:]
        run_harness(
            configuration,
            project,
            arguments.harness,
            harness_arguments,
        )


def _run_lifecycle_command(
    command: str,
    configuration: LoadedConfiguration,
    *,
    update_lock: bool = False,
) -> None:
    if command == "sync":
        lock = sync(configuration, update_lock=update_lock)
        action = "Updated lock and synchronized" if update_lock else "Synchronized"
        Console().print(f"[green]✓[/] {action} [bold]{len(lock.sources)}[/] source(s)")
        return

    lock = load_lock(configuration)
    if not configuration.consumers:
        raise CatalogError("global apply requires explicitly configured consumers")

    _, selected = resolve_locked_profile(configuration, lock)
    if command == "doctor":
        issues = doctor(configuration, selected)
        if issues:
            for issue in issues:
                Console().print(f"[red]ERROR:[/] {escape(issue)}")
            raise SystemExit(1)
        Console().print("[green]✓[/] No problems found")
        return

    view = build_profile_view(configuration, selected)
    apply_profile(configuration, selected, view)
    Console().print(
        f"[green]✓[/] Applied profile [cyan]{escape(str(configuration.active_profile))}[/] "
        f"with [bold]{len(selected)}[/] skill(s) to "
        f"[bold]{len(configuration.consumers)}[/] consumer(s)"
    )


def inspect_configuration(
    configuration: LoadedConfiguration,
    profile_name: str | None,
) -> tuple[
    tuple[ResolvedSource, ...],
    tuple[ResolvedSkill, ...],
    str | None,
]:
    """Inspect synchronized machine inventory without accessing the network."""
    sources = resolve_locked_inventory(configuration, load_lock(configuration))
    selected_profile = (
        profile_name
        or configuration.default_project_profile
        or configuration.active_profile
    )
    if selected_profile is None:
        selected = tuple(skill for source in sources for skill in source.skills)
        return sources, selected, None
    try:
        profile = configuration.catalog.profiles[selected_profile]
    except KeyError as error:
        available = ", ".join(sorted(configuration.catalog.profiles)) or "none"
        raise CatalogError(
            f"unknown profile '{selected_profile}'; available profiles: {available}"
        ) from error
    selected = resolve_profile(selected_profile, profile, sources)
    return sources, selected, selected_profile


def _print_tag_breakdown(
    sources: tuple[ResolvedSource, ...],
    selected: tuple[ResolvedSkill, ...],
    profile_name: str | None,
) -> None:
    selected_keys = {(skill.source, skill.name) for skill in selected}
    skills = tuple(skill for source in sources for skill in source.skills)
    tags = sorted({tag for skill in skills for tag in skill.tags})
    console = Console()
    context = f" for profile {profile_name}" if profile_name else ""
    console.print(f"[bold cyan]Tag breakdown{escape(context)}[/]\n")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Tag", style="bold")
    table.add_column("Selected", justify="right")
    table.add_column("Available", justify="right")
    table.add_column("Sources")
    for tag in tags:
        tagged = tuple(skill for skill in skills if tag in skill.tags)
        selected_count = sum(
            (skill.source, skill.name) in selected_keys for skill in tagged
        )
        table.add_row(
            tag,
            str(selected_count),
            str(len(tagged)),
            ", ".join(sorted({skill.source for skill in tagged})),
        )
    console.print(table)


def _print_profile_breakdown(
    configuration: LoadedConfiguration,
    sources: tuple[ResolvedSource, ...],
) -> None:
    console = Console()
    console.print("[bold cyan]Profile breakdown[/]\n")
    table = Table(show_header=True, header_style="bold")
    table.add_column("", width=1)
    table.add_column("Profile", style="bold")
    table.add_column("Selection", justify="right")
    table.add_column("Sources")
    for name, profile in sorted(configuration.catalog.profiles.items()):
        try:
            selected = resolve_profile(name, profile, sources)
        except CatalogError:
            selected = ()
        markers = []
        if name == configuration.default_project_profile:
            markers.append("project default")
        if name == configuration.active_profile:
            markers.append("global")
        table.add_row(
            "[green]●[/]" if markers else "",
            name,
            f"{len(selected)} skill" + ("" if len(selected) == 1 else "s"),
            ", ".join(sorted({skill.source for skill in selected})) or "—",
            end_section=bool(markers),
        )
    console.print(table)
    console.print("[dim]● project default or globally active profile[/]")


def _print_inventory(
    sources: tuple[ResolvedSource, ...],
    selected: tuple[ResolvedSkill, ...],
    profile_name: str | None,
) -> None:
    selected_keys = {(skill.source, skill.name) for skill in selected}
    heading = f"Profile: {profile_name}" if profile_name else "Complete inventory"
    console = Console()
    console.print(f"[bold cyan]{escape(heading)}[/]")
    console.print(f"[dim]Selected: {len(selected)} skill(s)[/]\n")

    table = Table(show_header=True, header_style="bold")
    table.add_column("", width=1)
    table.add_column("Skill", style="bold")
    table.add_column("Source", style="cyan")
    table.add_column("Revision", style="dim", no_wrap=True)
    table.add_column("Tags")
    inventory = [(source, skill) for source in sources for skill in source.skills]
    inventory.sort(
        key=lambda item: (
            (item[1].source, item[1].name) not in selected_keys,
            item[1].source,
            item[1].name,
        )
    )
    for source, skill in inventory:
        revision = (
            source.resolved_revision[:10]
            if source.resolved_revision is not None
            else "local"
        )
        is_selected = (skill.source, skill.name) in selected_keys
        table.add_row(
            "[green]●[/]" if is_selected else "",
            skill.name,
            source.name,
            revision,
            ", ".join(sorted(skill.tags)) or "untagged",
            style=None if is_selected else "dim",
        )
    console.print(table)
