# Maintain a catalog

## Locate the editable catalog

Identify the intended catalog file or repository; ask if it is unclear. A project's
`.skill-manager.yaml` supplies its `catalog.path` or `catalog.git`, `ref`, and
optional `file` (default: `catalog.yaml`). Catalog maintenance can also start
directly from a catalog without an existing consuming project.

For a Git catalog, work in its editable repository checkout. Inspect its branch,
requested ref, and existing changes before editing. Do not edit the manager's
cached checkout or a catalog snapshot inside a project lock. Keep publishing and
project adoption within the user's requested scope.

## Edit sources, tags, and profiles

Preserve the catalog's naming and classification conventions. Merge changes into
its `sources` and `profiles`, following this structure:

```yaml
sources:
  engineering:
    git: https://github.com/your-team/agent-skills.git
    ref: <approved-tag-or-commit>
    discover:
      include: [skills/**/SKILL.md]
      exclude:
        skills: [experimental-helper]
    tags: [coding]
    skills:
      python-testing:
        tags:
          add: [python, testing]

profiles:
  python-development:
    include:
      - source: engineering
        tags: [python]
    exclude:
      - tags: [experimental]
```

- Declare exactly one of `git` or `path` per source. Git sources require `ref`;
  local sources must omit it. Use the approved tag, commit, or deliberately
  moving branch. Source paths and filesystem Git remotes are relative to the
  catalog file. Independent local directories remain live and unversioned.
- Scope discovery globs to the intended `SKILL.md` files. Discovery exclusions
  can specify `skills` by name or `paths` as file globs. Read skill names from
  frontmatter, falling back to directory names; do not guess them from paths.
- Source tags are inherited. Per-skill `tags.add` and `tags.remove` refine them;
  `inherit: false` starts without source tags. A tag cannot be both added and
  removed. Per-skill declarations must refer to discovered, non-excluded skills.
- Each profile needs at least one include selector. Within a selector, source,
  tags, and skills constraints all apply: all listed tags must be present, while
  any listed skill name may match. Include selectors combine their results;
  exclusions win. Profiles do not inherit from other profiles.
- Check affected profiles when renaming/removing sources, skills, or tags.
  Profile renames also require changes in consuming project declarations.
  An installed selection must be nonempty and have unique skill names.

## Preview the edited catalog

Do not use an already locked project's `skm list` to validate new catalog edits:
it reads the old catalog snapshot. Use a disposable project with no lock, pointing
directly at the edited local catalog. Replace the path and profile below with the
actual values, keeping the catalog in its original location so relative paths
retain their meaning:

```sh
preview_dir=$(mktemp -d)
mkdir "$preview_dir/project"
cat > "$preview_dir/project/.skill-manager.yaml" <<'YAML'
catalog:
  path: /absolute/path/to/edited/catalog.yaml
profile: python-development
YAML
skm list --project "$preview_dir/project" --state-dir "$preview_dir/state"
skm list --profiles --project "$preview_dir/project" --state-dir "$preview_dir/state"
```

Inspect selected names. Repeat `skm list` with each affected profile selected
in the temporary declaration. `list --profiles` alone is not a
validation gate: it can show zero for an empty profile without failing. Resolving
the selected profile detects empty selections and duplicate final skill names.

Listing may acquire the entire catalog's source inventory, requiring repository
access, but creates no project links or project lock. Keep the same temporary
state directory throughout and remove only the temporary project/store you
created when finished.

## Review and adoption

Review the catalog diff and report the sources, refs, and profile selections that
changed. Identify unavailable sources or profiles you could not validate.
Existing projects keep their locked catalog until they explicitly run `skm update`;
plain `install` reproduces the lock. A Git catalog must make the edit available at
the project's approved ref before that project can adopt it. Update can refresh
other source refs too. When project adoption is requested, follow the project
workflow linked from `SKILL.md` and verify the resulting declaration/lock diff.
