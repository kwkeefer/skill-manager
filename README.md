# Skill Manager

Curate a catalog once. Install selected skills into each project explicitly.

Skill Manager keeps a shared store of acquired skills, while each project names
the catalog and profile it uses. A project lock records the exact catalog and
skill revisions. Projects can share cached content and still update independently.

Starting an agent does not run Skill Manager or change project state. There are
no PATH shims, launch interception, machine-selected profiles, or global installs.

## Installation

Requires macOS or Linux, Python 3.14+, Git, and filesystem symlinks. From a checkout:

```sh
uv tool install .
skm --help
```

For development, use `uv run skm`. Version 0.2 replaces the previous shim-based
CLI; existing users should follow [the migration guide](docs/migration.md).

## Curate the shared catalog

Keep `catalog.yaml` in a repository you maintain:

```yaml
sources:
  engineering:
    git: git@github.com:your-team/agent-skills.git
    ref: v1.2.0
    tags: [coding]
    skills:
      python-testing:
        tags:
          add: [python]

profiles:
  python-development:
    include:
      - source: engineering
        tags: [python]
```

A source is a repository or local directory containing `SKILL.md` files. Skills
inherit source tags; per-skill declarations can add/remove tags or disable
inheritance. Selectors constrain source, tags, and/or skill names. All constraints
within a selector must match; include selectors are combined, then exclusions win.

Use a tag or commit for a deliberately approved skill version. A branch such as
`main` explicitly allows `update` to resolve that branch's current revision.
Skill Manager never chooses a version beyond the ref declared by the catalog.

## Select skills for a project

Commit `.skill-manager.yaml` in the project:

```yaml
catalog:
  git: git@github.com:your-team/skill-catalog.git
  ref: main
  # file: catalogs/engineering.yaml  # defaults to catalog.yaml
profile: python-development
```

Or reference a local catalog file:

```yaml
catalog:
  path: ../skill-catalog/catalog.yaml
profile: python-development
```

Local catalog paths are relative to the project declaration. Local source paths
and filesystem Git remotes are relative to the catalog file. A Git catalog may
bundle skills using `path: skills`; those skills come from the locked catalog
checkout. Independent local source directories remain live and unversioned.

Project-specific refinements are optional, shared, and explicit:

```yaml
catalog:
  path: ../skill-catalog/catalog.yaml
profile: python-development
include:
  - skills: [review-checklist]
exclude:
  - tags: [experimental]
```

Project includes add to the profile's includes. Profile and project exclusions
both apply to the final selection. Duplicate final skill names are an error.
Refinements are no longer read from clone-local Git configuration.

## Install and update

```sh
skm install             # first run resolves the catalog and creates the lock
skm install --locked    # reproduce an existing compatible project lock
skm status              # report local drift, without networking or repairs
skm status --check      # exit 1 on drift; 0 when clean
skm update              # adopt current catalog choices and update this project
skm list                # available skills, marking the selected ones
skm list --profiles     # catalog profiles and their selection counts
skm uninstall           # remove verified manager-owned links and install records
```

Every command accepts `--project /path/to/project`. Running inside a subdirectory
finds the nearest `.skill-manager.yaml`, stopping at the Git repository root.
No declaration means no implicit machine profile or installation.

`install` uses `.skill-manager.lock` when it exists and hydrates missing cached
content at the locked revisions. It does not advance catalog or source refs or
rewrite an existing compatible lock. `--locked` additionally requires a lock.
Cached installs work offline; fetching missing Git checkouts requires access to
their repositories and exact revisions.

`update` rereads the local catalog or fetches its requested Git ref, resolves the
skill refs it approves, then installs and writes the new lock. Editing the project
declaration requires an explicit `update` if a lock already exists. Other projects
keep their own locks and installed selections.

Commit the declaration and lock together. The lock contains a validated catalog
snapshot, catalog revision, source locations, requested refs, and resolved
revisions. Give it the same visibility as the catalog: private URLs, source names,
or local paths must not be published in a public project. Git refs alone do not
replace a lock; branch and tag names can move.

`status` checks the declaration against the lock, the installation receipt, each
owned link/target, and the Git exclude block. It does not check whether an upstream
branch has advanced. Independent local skill content is live through symlinks;
status does not hash that content or claim it is immutable. `list` uses the locked
catalog when present; without a lock it may fetch content into the shared store
but does not create project links or a lock. Discovery currently acquires the
catalog's entire source inventory, not just selected skills.

For CI, install first, then check:

```sh
skm install --locked
skm status --check
```

Drift exits 1 with `status --check`; invalid usage/configuration and command errors
exit 2. A fresh clone has no installation receipt, so checking before installation
reports drift. Private catalogs need the usual Git credentials available in CI.

## Storage and ownership

Skills are symlinked into both `.agents/skills` and `.claude/skills`. The target is
a generated profile view in the shared store; supporting scripts and resources
remain beside each `SKILL.md`. Skill content is never copied into the project.

The default store is `${XDG_DATA_HOME:-$HOME/.local/share}/skill-manager`.
Override it with `SKILL_MANAGER_STATE_DIR` or `--state-dir`. Use the same store
for install, status, and uninstall: it contains ownership manifests as well as
cached sources. Source checkouts are keyed by repository identity and revision.

Unmanaged project-local skills are preserved. Same-name collisions, externally
changed owned entries, Git-tracked managed entries, and symlinked consumer
directories are refused. All consumers are preflighted before mutation. Ordinary
failures roll back links, manifests, the project lock, receipt, and Git excludes.
Cooperating commands sharing a store serialize project changes and view creation.
Rollback is not a durable crash-recovery journal; sudden process termination or
disk failure may require inspection with `status`.

Generated links are hidden with a project-specific block in `.git/info/exclude`.
Other rules and other projects' blocks are preserved. Non-Git projects have no
Git ignore bookkeeping. Ignore rules are convenience, not a confidentiality
boundary: Git can record a symlink's name and target, and forced adds remain possible.

`uninstall` uses ownership manifests and works without the catalog, project
declaration, or source checkouts. It retains the project lock/declaration,
unmanaged entries, and shared source/profile storage. It refuses to delete owned
entries that have changed externally. Old source revisions are not garbage-collected.
When a declaration is missing, retained project locks and installation records
keep uninstall scoped to the nearest known project, including nested projects.

## Agent behavior and trust

Installation adds project skills. Skill Manager does not suppress skills loaded
from home directories, ancestor directories, plugins, or agent built-ins. Keeping
user-managed home skills empty is an operational policy, not an enforced sandbox.
Each agent retains its own skill-discovery behavior.

Catalogs, project declarations, source trees, and manager state are trusted
workstation inputs. Skills may include executable scripts and instructions.
Review a catalog before installing it. Separate private catalogs explicitly and
keep private source/state locations outside personal dotfile publishing paths.

## Optional agent skill

This repository includes [a `skill-manager` skill](skills/skill-manager/SKILL.md)
that teaches agents to select, install, update, and troubleshoot project skills,
and to maintain shared catalogs and profiles. Its short entry point links to
separate project and catalog workflows that agents can read as needed.
It is opt-in through the existing catalog and profile/project selectors. Installing
the Python CLI alone does not install this skill into any agent directory.

Add this source to your shared catalog, choosing a tag or commit that contains
the skill and matches the explicit installer CLI (0.2 or later):

```yaml
sources:
  skill-manager:
    git: https://github.com/kwkeefer/skill-manager.git
    ref: <approved-tag-or-commit>
    discover:
      include: [skills/skill-manager/SKILL.md]
```

Then add this selector to a catalog profile or to your project's declaration:

```yaml
include:
  - source: skill-manager
    skills: [skill-manager]
```

Merge these entries with your existing configuration. Project selectors can only
select sources already declared by the catalog; profile and project exclusions
still apply. Run `skm install` for a first installation, or `skm update` to adopt
the change in an already locked project, then `skm status --check`. An update
also refreshes other catalog sources within their approved refs. To opt out,
remove the selector (or exclude this skill if a shared profile includes it) and
run `skm update` again.

The skill uses normal revision locking, ownership, and uninstall behavior. Its
explicit discovery path keeps development skills elsewhere in this repository
out of the source inventory.

## Example and development

The checked-in example fetches its declared Git source:

```sh
uv run skm install --project examples/project --state-dir examples/.prototype-state
uv run skm status --check --project examples/project --state-dir examples/.prototype-state
uv run skm uninstall --project examples/project --state-dir examples/.prototype-state
```

```sh
uv run pytest
ruff check src tests
ruff format --check src tests
```

See [ADR 0001](docs/adr/0001-explicit-project-installer.md) for the architectural
decision and [CONTEXT.md](CONTEXT.md) for the glossary.

## License

Licensed under the [Apache License 2.0](LICENSE).
