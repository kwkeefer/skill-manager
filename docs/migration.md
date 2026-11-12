# Migrating from harness shims to the explicit installer

Version 0.2 intentionally removes `setup`, `sync`, global `apply`/`doctor`,
`project use`/`apply`/`env`/`run`, and clone-local `add`/`remove`. It does not delete
shell configuration, old shims, home skills, catalogs, or machine locks for you.

## Prepare the projects

1. Preserve your existing shared catalog. Its sources, tags, skill refinements,
   and profiles keep their schema. It can remain a local file or become a Git
   catalog reused by projects.
2. Add an explicit `catalog` reference and `profile` to each project's
   `.skill-manager.yaml`. Remove `global_skills`. If the old binding was only in
   `.git/config`, inspect it with `git config --local --get skill-manager.profile`.
3. Inspect any `skill-manager.refinements` Git value and deliberately translate
   it into shared declaration selectors. The former rule allowing explicit skill
   inclusions to override tag exclusions is gone: all new exclusions win. Review
   the resulting selection before adopting it.
4. Use the **same state directory** as the previous installer. Version-one project
   manifests retain their IDs, letting new installs replace already-owned links
   safely. Do not delete the store or manifests first. Links without ownership
   evidence are treated as unmanaged collisions.
5. Run `skm install`, then `skm status --check` in each project. Commit the new
   project declaration and `.skill-manager.lock` where its metadata is appropriate.
   Existing machine locks are not imported: the first install resolves the declared
   catalog. To preserve an exact old revision, pin it in the catalog before this step.

Old Git source caches use different keys and are retained. New Git acquisitions
are keyed by repository location and revision; the first migration may fetch again.
Old profile views and ownership records are not silently deleted.

## Switch the machine deliberately

Old shims invoke the removed `project run` command. Coordinate removal of their
PATH entries with upgrading the installed CLI, and invoke real agent executables
directly during the transition. New commands never launch an agent.

- Remove the Skill Manager shim PATH entry from shell startup and any project
  environment files. Then remove only the obsolete manager-generated shim files.
- Move wanted home-directory skills into catalog sources and install them into
  the relevant projects before removing their old home entries. Preserve the
  source content and account for any ownership by chezmoi or another tool.
- Keep private catalog/source material outside personal dotfile publishing paths.
- Stop automatic bare `skm setup` and `skm sync` provisioning. Update the pinned
  package revision before distributing new-schema project declarations.
- If provisioning inspects a uv receipt, handle directory installs explicitly.
  A test for a repository URL cannot recognize a directory-based installation.
- Remove old machine lock tracking and instructions to publish those locks.
  Project locks follow the visibility of their own project/catalog instead.

The prior handoff identified fish PATH setup, an Ansible role pin and receipt
guard, home skill links, a tracked personal machine lock, and an old installation
inside the dotfiles repository. Recheck those machine-specific facts before
changing them; this code migration does not assume their paths or current state.

For an old managed project you no longer want populated, run:

```sh
skm uninstall --project /path/to/project --state-dir /path/to/existing/store
```

This works from legacy manifests even without a valid declaration or available
catalog. It removes only verified owned links and that project's Git exclude
block. It leaves unrelated skills, other exclude blocks, and shared content intact.

## Limits

There is no global skill suppression. Agent plugins, built-ins, inherited
directories, and home skills follow each agent's own rules. A project install
does not establish a confidentiality boundary. Status checks local consistency;
upstream catalog changes are adopted only by `update`.
