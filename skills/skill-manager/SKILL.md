---
name: skill-manager
description: Manage project agent skills with the skm CLI. Use when selecting catalog profiles or skills, installing or updating managed skills, diagnosing missing skills or installation drift, or uninstalling manager-owned skills in projects using .skill-manager.yaml and .skill-manager.lock.
---

# Skill Manager

Use the existing `skm` CLI to manage the requested project's skills. This skill
supports the explicit project installer introduced in version 0.2.

## Inspect the project

1. Identify the intended project and read its `.skill-manager.yaml` and, if
   present, `.skill-manager.lock`. Use `--project /path/to/project` on commands
   when working elsewhere or when nested projects could be ambiguous.
2. Check `skm --help` and subcommand help for supported options. When developing
   Skill Manager from its own checkout, use `uv run skm` instead of `skm`.
3. Keep the same configured state directory throughout. Preserve an existing
   `SKILL_MANAGER_STATE_DIR` or pass the intended `--state-dir` to each command.
4. Run `skm status` to inspect local drift. It does not fetch or report upstream
   releases. Use `skm list --profiles` and `skm list` to inspect the catalog;
   listing may fetch missing content but does not install links or write a lock.

## Select skills

Edit the project's `profile`, `include`, or `exclude` to express its requested
selection. Preserve the catalog reference unless changing it is part of the task.
For a new project, establish the intended catalog and profile before installing;
do not guess a shared catalog or overwrite an existing declaration.

For example, add a skill from a source already declared in the catalog:

```yaml
include:
  - source: engineering
    skills: [python-testing]
```

Merge with existing selectors. Constraints within one selector must all match;
include selectors add skills, then profile and project exclusions win. A project
selector cannot introduce a new source: add that source to the shared catalog
only when the requested change includes catalog curation. Catalog edits can affect
other projects when they next update.

## Choose the operation

| Intent | Command |
| --- | --- |
| First installation, creating a lock | `skm install` |
| Reproduce an existing committed lock | `skm install --locked` |
| Repair missing managed links or cached content at locked revisions | `skm install` |
| Apply declaration/catalog changes or deliberately refresh approved refs | `skm update` |
| Inspect local installation consistency | `skm status` |
| Remove all manager-owned project skill links | `skm uninstall` |

Treat `update` as adoption of current catalog choices: it can advance multiple
sources within their approved refs. Do not use it merely to silence drift when
reproducing the existing lock is the user's intent. To remove one selected skill,
edit the selection and update; `uninstall` removes the whole managed installation.

## Preserve ownership and verify

- Edit declarations and catalogs; let the CLI write locks and managed links.
  Do not hand-edit ownership manifests, receipts, or shared-store internals.
- Inspect collisions and externally changed entries. Preserve user content;
  do not delete it or force ownership to make installation succeed.
- After install or update, run `skm status --check`. Review declaration and lock
  diffs together and report the changed selection and verification result.
- After uninstall, check that managed links were removed and unmanaged entries
  remain. Retained declarations and locks are expected; clean installed status
  is not an uninstall success criterion.
- Distinguish installed skills from skills loaded by the running agent. Clean
  status confirms local consistency, not agent reload or upstream freshness.
