# Skill Manager

An explicit project installer for filesystem-based AI agent skills. A shared
catalog is curated centrally; projects choose a profile and update independently.

**Catalog:** A YAML inventory of sources, classifications, and reusable profiles.
Referenced explicitly by a project, from a Git repository/ref or local file.
Catalog composition is one level deep; there are no conditional fragments.

**Source:** A Git repository or local directory containing skills. A source is
the unit of acquisition and revision locking. Avoid: package.

**Skill:** A discovered directory containing `SKILL.md` and optional supporting
resources. Its name comes from frontmatter, falling back to the directory name.

**Requested ref:** The branch, tag, or commit approved in a catalog source or
project catalog reference.

**Resolved revision:** The exact immutable Git object ID recorded in the lock.
Independent local path sources have no Git revision and remain live.

**Tag:** A classification label inherited from a source and refined per skill.

**Profile:** A named group of include/exclude selectors in the shared catalog.

**Project declaration:** The committed `.skill-manager.yaml` naming the catalog,
profile, and optional shared include/exclude refinements. It is authoritative;
machine configuration and clone-local Git profile bindings are not consulted.

**Project lock:** `.skill-manager.lock`, recording project intent, a catalog
snapshot, its Git revision when applicable, and exact skill-source identities
and revisions. Normally committed beside the declaration with matching visibility.

**Available skill:** A skill discovered in the project's catalog inventory.

**Installed skill:** A selected available skill exposed through manager-owned
project symlinks. Installation does not mean the agent has loaded the skill.

**Consumer directory:** A directory an agent scans for skills. The installer
currently targets the fixed `.agents/skills` and `.claude/skills` pair.

**Source store:** Machine-local acquired checkouts, profile views, ownership
manifests, installation receipts and writer locks. Shared by projects. Git source
identity includes the repository location, not just a human-readable alias.

**Profile view:** An immutable directory of symlinks for one resolved selection,
under `profiles/<profile>/<digest>` in the source store. Project links target it.

**Ownership manifest:** A record of the exact link targets the manager owns in
one consumer. An unrelated same-name entry is never adopted automatically.

**Installation receipt:** The project root and fingerprint of the installed lock.
It detects cases where a project lock changed without installation.

**Drift:** A mismatch among project intent, lock, receipt, expected links, and Git
exclude bookkeeping. Local `status` does not detect new upstream releases or
content edits inside live local skills.

**Install:** Reproduce a compatible existing lock, or resolve and create the
first lock. Missing Git content may be hydrated without moving locked revisions.

**Update:** Deliberately adopt the catalog's current choices within its approved
refs, update the project lock, and install the resulting selection.

**Uninstall:** Remove verified owned project links and their install bookkeeping.
Retain the declaration, project lock, unmanaged skills and shared source storage.

**Retention:** State that outlives an installation or declaration. Source
revisions and generated views currently have no garbage collection.

The former concepts of activation scope, isolated session, harness pass-through,
and implicit clone-local profile binding are retired. Skill Manager neither
intercepts agent launches nor suppresses home-directory skills.
