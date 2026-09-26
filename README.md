# Skill Manager

A prototype for declaring, classifying, versioning, and provisioning
filesystem-based AI agent skills.

Sources are versioned as a unit, skills inherit and refine source tags, and
profiles select discovered skills using source, tag, and name constraints.
Project-scoped activation is the default; persistent storage is never directly
scanned by harnesses.

## Why this exists

I kept running into the same problem across different projects: they needed many
of the same skills, but not quite the same set. Copying skill files into every
repository made updates tedious and let the copies drift apart. Installing all
of them globally avoided duplication, but created a different problem: every
agent saw an ever-growing collection of skills, including many that had nothing
to do with the project in front of it.

That kind of skill sprawl is more than untidy. A large global catalog gives the
agent more descriptions to consider, makes accidental or ambiguous skill
activation more likely, and spends context on capabilities that are irrelevant
to the current task. The useful set of skills for a Rust service is not
necessarily the useful set for a Python library or a writing project.

Skill Manager exists to separate **where skills come from** from **where they
are enabled**. Multiple projects can reuse skills from the same maintained,
versioned sources, while custom profiles select only the skills appropriate for
a particular project. This keeps shared skills consistent without forcing every
skill into every agent session.

## Installation

Skill Manager currently supports macOS and Linux with Python 3.14 or newer, Git,
a POSIX shell, and filesystem symlinks. Install [`uv`](https://docs.astral.sh/uv/),
then install `skm` from a repository checkout:

```bash
uv tool install .
skm --help
```

For development, use `uv run skm` from the checkout instead.

## Preferred workflow

Keep one machine configuration at `~/.config/skill-manager/config.yaml`. Override
that path with `SKILL_MANAGER_CONFIG` when needed.

```bash
# One-time machine setup
skm setup
# Edit the generated catalog, then acquire its locked sources
skm sync
```

When no machine configuration exists, `setup` creates a starter configuration,
catalog, and local skills directory under `~/.config/skill-manager/`. Existing
configuration is left untouched. By default it detects installed harnesses,
creates shims only for those found, and prints a `PATH` export for shell startup.
Use `skm setup --no-shims` to initialize configuration without intercepting any
harness commands. Installed shims resolve a profile whenever a harness starts.

Bind a Git clone to a profile once:

```bash
cd ~/code/some-project
skm project use rust-development
```

The binding is written by `git config --local` to `.git/config`. It does not
modify the working tree and cannot be committed or pushed. Normal use is then:

```bash
pi
claude
codex
```

Each shim reapplies locked state without networking, suppresses ordinary
home-directory skills where supported, and launches with the project's profile
plus unmanaged project-local skills.

### Shims and harness management

Shims isolate agent sessions only. A harness recognizes its own management
subcommands in the first argument, before it reads any agent options, so
`pi update`, `pi install <source>`, `pi list`, `pi config`, `pi auth`,
`claude doctor`, `claude mcp`, `codex exec`, `codex login`, and `--help` or
`--version` are handed to the real executable with their arguments unchanged and
without applying project state. Injecting isolation flags ahead of those words
would make the harness read the subcommand as a prompt, which is why
`pi update` once started a session prompted with the word `update`.

Arguments after the harness name reach the harness exactly as typed, including
option-looking values and `--`. Two overrides cover policy drift:

```bash
# Isolate a launch the shim classified as management, such as a prompt that
# begins with a subcommand word:
SKM_LAUNCH_MODE=isolate pi update the flaky tests

# Never intercept a launch, without editing shell startup:
SKM_LAUNCH_MODE=passthrough pi
```

Machine configuration can add subcommands a harness introduced after this
release, so the built-in allowlist never becomes a blocker:

```yaml
harness_passthrough:
  pi:
    - sessions
```

Inspect the current inventory and classifications:

```bash
skm inspect                    # skills in the default profile
skm inspect --tags             # tag counts and source coverage
skm inspect --profile rust     # one profile's selected skills
skm inspect --profile rust --tags
skm inspect --profiles         # compare all configured profiles
```

Refine the current project's base profile without creating a project file:

```bash
skm add tdd
skm add --tag rust
skm remove rust-perf
skm remove --tag productivity
```

Use `--project /path/to/project` when operating outside the target directory.
Refinements are stored in clone-local Git configuration and applied immediately.
Explicit skill choices override tag choices; exclusions override inclusions at
the same specificity.

## Optional committed declaration

A team may deliberately share a profile by committing `.skill-manager.yaml`:

```yaml
profile: rust-development
global_skills: disabled
```

This takes precedence over local Git configuration, but it is not required for
the normal workflow. Machine configuration may also define
`default_project_profile` as a fallback.

## Prototype example

The example keeps all generated state beneath `examples/`:

```bash
uv run skm sync examples/config.yaml
uv run skm project apply examples/config.yaml examples/project

eval "$(uv run skm project env examples/config.yaml examples/project)"
pi
```

## Safety and trust model

Skill Manager is an alpha workstation tool, not a sandbox. It assumes that your
machine configuration, catalogs, declared Git repositories, local source trees,
and project declarations are trusted. Acquired skills can contain executable
scripts and instructions that agent harnesses may use. Do not add an untrusted
source or launch a managed harness in an untrusted project without reviewing it.
Manager state is protected against accidental collisions, not malicious changes
by another process running as the same user.

Existing project-local skills are preserved. The manager records only links it
created and refuses to overwrite a same-named unmanaged skill. Generated links
are hidden from `git status` through a marked block in `.git/info/exclude`; the
tracked `.gitignore` is not changed. Harness launch never accesses the network.
Run `skm sync` to hydrate locked revisions on a workstation, or
`skm sync --update-lock` when you deliberately want to resolve newer
source revisions.

Global activation remains available only when machine configuration explicitly
defines both `active_profile` and `consumers`.

## Disabling or uninstalling

For a single launch, or a temporary pause without editing shell startup, set
`SKM_LAUNCH_MODE=passthrough`. To stop intercepting harness commands permanently,
remove the `PATH` export printed by `skm setup` from your shell startup file and
remove the generated shims:

```bash
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/skill-manager/shims"
```

If installed with uv, uninstall the CLI with:

```bash
uv tool uninstall skill-manager
```

Configuration and acquired state are retained intentionally. Delete
`~/.config/skill-manager` and `${XDG_DATA_HOME:-$HOME/.local/share}/skill-manager`
only when you no longer need them. Previously generated project skill symlinks
are not removed automatically; remove only symlinks owned by Skill Manager and
preserve unmanaged project skills.

See [`examples/catalog.yaml`](examples/catalog.yaml) and
[`examples/config.yaml`](examples/config.yaml) for complete configuration examples.

## Prototype limitations

- The management subcommand allowlist is version-pinned; harnesses that add new
  subcommands need a `harness_passthrough` entry until this tool is revalidated.
- Non-Git project bindings do not yet have a central path registry.
- Claude Code setting-source isolation needs integration testing against the installed release.
- Codex isolation enumerates home skills at process launch and disables them by exact path.
- Built-in and administrator-managed harness skills are not uniformly suppressible.
- No migration/adoption command exists for links created by the current chezmoi scripts.
- Old source revisions and profile generations are not garbage-collected.

## License

Licensed under the [Apache License 2.0](LICENSE).
