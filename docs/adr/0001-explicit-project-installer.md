# ADR 0001 — Explicit project installation from curated catalogs

Status: accepted and implemented for 0.2.

## Decision

Replace harness interception with explicit `install`, `update`, `status`, `list`,
and `uninstall` commands. Remove launch classification, shims, isolation flags,
global provisioning commands, machine-selected defaults, and clone-local profile
refinements. The project declaration is authoritative.

Keep a centrally curated catalog reusable across projects. A project explicitly
names one local catalog file or one Git catalog repository/ref/file and selects
a profile. Optional project include/exclude selectors are committed in the same
declaration. Catalogs are not recursively composed and no workspace detection or
conditional fragments are introduced.

`install` reproduces an existing compatible lock; without one, it resolves and
creates the first lock. `install --locked` requires a lock and never rewrites it.
`update` refreshes the catalog and resolves only the skill refs that catalog
approves. One project's update never advances another project's lock.

The lock stores a validated catalog snapshot and catalog revision, plus each
skill source's full location/ref/revision identity. This preserves profile
selection when a catalog branch moves. Local catalogs are snapshotted; independent
local skill directories stay live. Skills bundled inside a Git catalog come from
that catalog's locked checkout.

`status --check` compares local intent, lock, installed receipt, links, and Git
excludes, returning nonzero on drift. It never fetches upstream revisions or
repairs state. A fresh clone must run `install --locked` before status can be clean.

## Ownership and failure handling

Keep the fixed `.agents/skills` and `.claude/skills` targets. Keep version-one
ownership manifests and their project-path IDs so earlier managed project links
can be upgraded or uninstalled. Never infer ownership from a symlink alone.

Preflight every consumer and tracked path before changing links. Coordinate
links, manifests, project lock, installed receipt and Git exclude writes in one
rollback-capable operation. Use unique temporary files and cooperative process
locks. Refuse symlinked consumer directories before resolving their paths.
Cache acquisition may persist after a failed install. The transaction covers
ordinary exceptions and interrupts, not sudden process death or disk failure.

Source caches use repository identity plus exact revision. Profile publication
is serialized per view to handle simultaneous first installs safely. Uninstall
requires only the ownership state; it does not require a reachable catalog.

## Consequences

- Starting an agent causes no Skill Manager writes or network activity.
- Catalog curation remains centralized; project adoption is explicit and reviewable.
- There is no automatic freshness enforcement at agent launch.
- Installation adds skills and cannot suppress global, inherited, or built-in skills.
- Keeping user-managed home skills empty is an operational policy.
- Declarations and locks may reveal source names, URLs, revisions, or local paths;
  their repository visibility must match the material they reference.
- Symlinks and ignore rules reduce accidental copying but do not enforce confidentiality.
- Shared store retention remains unbounded; garbage collection is deferred.
- Machine setup, private-source placement and dotfile migration are operational
  changes, not hidden side effects of `install`.

## Superseded work

RFC 0001 remains a historical current-state and risk record. Its shim fixes,
persona environment resolution, fragments, and workspace phases are superseded.
Do not implement `conf.d`, activation predicates, profile namespaces, workspace
environment variables, or optional interception as part of this installer.

See [migration.md](../migration.md) for transition steps and remaining machine work.
