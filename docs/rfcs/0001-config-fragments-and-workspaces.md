# RFC 0001 — Persona-scoped configs and private inventories

Status: **Superseded by [ADR 0001](../adr/0001-explicit-project-installer.md).**
The launch-resolution, fragment, and workspace phases below are historical; do not implement them.

Original status: **v2, planning only.** v1 recommended a merge engine; two adversarial
reviews against the code and this machine's real state reversed that. v1's
recommendation was wrong in three specific ways, recorded in §3 so the reasoning
is not lost.

The literal ask: *resolve different local skill files depending on the workspace I
launched from, while work-only skill content never enters personal dotfiles.*

## 1. The problem, separated

| # | Problem | Question | Nature |
|---|---------|----------|--------|
| A | **Resolution** | which config does this launch read, and who decides? | one env var + one shim fix |
| B | **Placement** | where does private material physically live? | one filesystem decision |
| C | **Naming** | what stops a private *name*, *SHA*, or *path* from being committed by personal tooling? | commit-time check, outside skm's process |
| D | **Composition** | how do two inventories share source declarations without duplication? | already works: `catalogs:` composes, and relative paths resolve against the config file |
| E | **Activation logic** | conditional fragments (`when: env/hostname/paths`)? | **cut** (§4.3) |

v1 treated E as the design and A as a detail. It is the other way round: A + B + C
satisfy the ask; E is the only item that adds a subsystem with no additional
containment.

## 2. Verified current state

Everything below was confirmed on this machine (`~/.config/skill-manager`,
`~/.local/share/skill-manager`, `~/.local/share/chezmoi`) or in
`src/skill_manager/`.

1. **Catalogs compose one level deep, unconditionally.** `load_configuration()`
   validates one `MachineConfig`, then concatenates `catalogs:` into one `Catalog`;
   duplicate source/profile names are a hard error
   (`config.py:107-141`, dup raise `:129-134`). No conditionals exist anywhere.
2. **One lock per config file, validated all-or-nothing.**
   `lock_path = config_path.with_name(f"{stem}.lock.yaml")` (`config.py:160`);
   `load_lock` requires `set(lock.sources) == set(configured sources)` and equal
   `requested_ref` per source (`storage.py:135-143`). **Correction to v1:** this is
   not "breaks sync everywhere". Three distinct outcomes:
   - machine *has* a lock → loud mismatch until `--update-lock` (`sync` bypasses
     `load_lock` when the lock is absent or `--update-lock`, `storage.py:57`);
   - machine has **no** lock → **silently writes a merged lock naming work sources
     into a chezmoi-managed directory**. Worse than breakage; this is the leak.
   - work catalog file absent on a personal machine → `load_catalog` raises for
     *every* subcommand (`config.py:113`, `catalog.py:118-125`), so `inspect`,
     `project …`, and every shim-launched harness die.
   Because a second config file gets its own lock for free (`config.py:160`),
   per-fragment locking is an argument for *fragments*, not against persona-per-config.
3. **The lock is published today.** `git ls-files` shows
   `private_dot_config/skill-manager/config.lock.yaml` tracked in
   `github.com/kwkeefer/dotfiles`, and both `README.md:95` and
   `dot_ai/skills/README.md:16` instruct `chezmoi add ~/.config/skill-manager/config.lock.yaml`.
   The validator accepts 40 **or** 64 hex (`storage.py:33-39`), and `path:` sources
   are recorded as a bare name with two nulls (`storage.py:96-100`) — the name
   `dotfiles-local` is already published without a SHA.
4. **`path:` sources are referenced in place, never copied** (`storage.py:64-69`,
   `:93-101`, `:224-231`; `acquire.py:22-33`), so private *content* stays in the
   private repo. Two caveats: `_copy_source_atomically` uses
   `copytree(..., symlinks=True)` (`storage.py:243`), and today's only `path:`
   source points **into the dotfiles tree** — `catalog.yaml:94`
   `path: ~/.local/share/chezmoi/dot_ai/skills` — so a copy-pasted work catalog
   inherits that path. Composition itself can be the leak.
5. **`~/.config/skill-manager` is itself a chezmoi-managed directory**
   (`chezmoi managed` lists it plus all three files). Any `conf.d/` placed inside it
   sits in managed territory: one `chezmoi add -r` publishes it. **This invalidates
   v1's proposed fragment location.**
6. **Skill Manager has already run a project apply inside the dotfiles repo.**
   `manifests/project-bcedd2f07caa7c3d-*.yaml` record
   `consumer_path: /Users/keefer/.local/share/chezmoi/.agents/skills`, and
   `chezmoi/.git/info/exclude` holds a `BEGIN skill-manager:bcedd2f07caa7c3d` block
   listing 16 skill names. Excludes are uncommitted, but the symlinks are in the
   working tree, and `_update_git_excludes` returns early for non-git roots
   (`project.py:431-434`), so outside a repo there is **no ignore net at all**.
   Blocks are keyed `sha256(project_root)` with no GC (`README.md:221`).
7. **Shims pin a literal config path and share one directory.** Live
   `shims/bin/pi`: `exec ~/.local/bin/skm project run
   ~/.config/skill-manager/config.yaml "$PWD" pi -- "$@"`. `SKILL_MANAGER_CONFIG`
   only feeds argparse defaults (`cli.py:130-137`); `project run` takes config
   positionally (`cli.py:219`). Worse, `generate_global_adapters` writes one shared
   `state_dir/shims/bin` and **unlinks non-selected shims** (`project.py:249-252`).
8. **The personal ansible role destroys the other persona's shims.**
   `ansible/roles/skill_manager/tasks/main.yml:34-62` runs bare `skm sync` and bare
   `skm setup` (env sets only `PATH`), gated only on the *personal* config existing —
   and chezmoi deploys that config on every machine. Fish prepends the shared shim
   dir unconditionally (`config.fish:17`). Net effect on a work machine: the next
   `chezmoi apply` + role run rewrites shims to pin the personal config, so work
   sessions lose work skills **and work projects receive personal `apply_project`
   runs**, deleting private symlinks and rewriting the exclude block mid-tree.
9. **`default_project_profile` is a global fallback** (`project.py:291`), unbound by
   any root restriction.
10. **Profile names may not contain `/`** — `_is_identifier` rejects it in three
    copies (`catalog.py:234-240`, `config.py:196-202`, `provision.py:248-254`) and
    `build_profile_view` uses the profile name as a path segment
    (`storage.py:199-207`). **v1's `work/engineering` example cannot load.**
11. **`extra="forbid"` fails closed against a pinned install.** `MachineConfig`,
    `LockFile`, and `LockedSource` all forbid extras (`config.py:28`,
    `storage.py:25,42`), and the role pins `skill_manager_revision: 54e3703…` and
    runs *after* chezmoi deploys config. New schema fields hard-fail every machine
    until the pin is bumped. Per-fragment lock fields are an on-disk format break,
    not an additive change.
12. **Environment bug, unrelated to this RFC but worth fixing now:** the live uv
    receipt is `requirements = [{ name = "skill-manager", directory =
    "/Users/keefer/code/skill-manager" }]`. The role's guard (`tasks/main.yml:28-32`)
    tests whether `skill_manager_repository` appears in the receipt, which a
    directory install never satisfies, so **every ansible run force-reinstalls skm
    from `git+…@54e3703`**, clobbering the dev checkout install.
13. **No hooks in the dotfiles repo** — `.git/hooks` contains only `*.sample` and
    `core.hooksPath` is unset. Any commit-time guard in §6 is unbuilt.
14. Tests: 33 collected (`pytest --collect-only -q`).

## 3. What v1 got wrong

- **v1 §3 placed private fragments at `~/.config/skill-manager/conf.d/`** — inside
  the directory the dotfiles README tells you to `chezmoi add` (§2.3, §2.5). It cited
  that instruction as a threat and then parked the private file under it.
- **v1 §7.1 claimed load-time leak-root checks make accidental commit "a hard error
  rather than a push."** They cannot: the base config's git toplevel resolves to
  nothing, the fragment path is outside `~/.local/share/chezmoi`, and the leak
  happens at `chezmoi add`/`git commit` time when skm is not in the process. The
  load-time rule is near-inert; the only effective rule is commit-time.
- **v1 §7.2 called "no cross-boundary selection" the load-bearing rule.** It is
  load-bearing only *inside the design v1 chose*. Under persona-per-config it is a
  theorem: a private source is not in the loaded catalog, so no selector can match
  it. It costs code only after you merge — and more than v1 estimated (§5.4).
- **v1 recommended Option 1 and then asked, in §11 Q1, whether the premise holds.**
  That is a decision deferred, not made.

## 4. Options

### 4.1 Recommended: persona-resolved config ("Option 0+")

Each persona has its own config file; the **launch environment** decides which one
is read.

```
~/work/agent-skills/            # private repo; NOT under ~/.config, NOT under chezmoi
  config.yaml                   # catalogs: [<personal catalog>, catalog.yaml]
  catalog.yaml                  # private inventory + profiles
  config.lock.yaml              # written by skm next to config.yaml, out of reach
```

```yaml
# ~/work/agent-skills/config.yaml
catalogs:
  - ~/.config/skill-manager/catalog.yaml   # personal sources/profiles, reused in place
  - catalog.yaml                           # private additions
state_dir: ~/.local/share/skill-manager    # shared: one copy of each Git revision
default_project_profile: work-default      # but see LV-2: prefer omitting it
```

`~/work/.envrc` (in a work-only repo, or unmanaged) carries
`export SKILL_MANAGER_CONFIG=~/work/agent-skills/config.yaml`; the shim change in
§5.1 makes that reach harness launches, which is the part that does not work today.

| | |
|---|---|
| **Cost** | One code change (§5.1), one repo, one `.envrc`, one commit hook. |
| **Containment** | Content, lock, profile names, and views all live outside personal-managed paths. Cross-boundary selection is impossible because only one inventory is loaded per persona. |
| **Honest limits** | Shared `state_dir` still holds private revisions and view entry names (LV-4/LV-6). Home-directory harness transcripts from work sessions are still personal-tooling territory (LV-3/LV-10). Neither is solved by fragments either. |

Relative catalog paths resolve against the config file's parent (`_resolve_path`,
`config.py:188-191`), which is why the personal catalog is reused without
duplication, and why `state_dir` sharing does not clone Git sources twice.

### 4.2 Deferred: additive overlay fragments

`conf.d/` auto-discovery with `MachineConfig`-shaped additive merge, provenance, and
per-fragment locks. **Justified by exactly one thing**: one `skm setup` serving a
machine that hosts both personas, because shims bake the config path and share one
shim dir (§2.7). §5.1 removes that justification for the cost of ~5 lines. Revisit
fragments only if (a) a single machine must expose *both* inventories to *different*
projects simultaneously with a single setup, or (b) teammates consume one shared
catalog that composes with personal ones — where (b) is the stronger argument.

Place `conf.d` **outside** any chezmoi-managed directory if it ever lands, and carry
the cost notes in §5.

### 4.3 Cut

`when:` activation predicates + `skm fragment why`; profile `namespace:` with `/`;
`private_project_roots` and root-bound refusal; private state subtrees; `$HOME`
transcript scanning; `SKM_WORKSPACE` (a second env var meaning what
`SKILL_MANAGER_CONFIG` already means); Options "workspace dimension in schema" and
"per-persona dotfiles repos" (§7).

`when:` is the biggest saving. File existence is a complete, `ls`-inspectable
predicate that fails safe. A predicate DSL adds: load-time side effects plus a
trace subsystem to explain them; a partial boolean algebra that grows
(`any:` without `all:`/`not:`); a *new* failure mode (a stale `CORP_*` export
activates private skills on a personal machine); and a *silent-deactivation* mode
existence cannot have — a harness launched from an IDE launcher that never saw the
env var loses work skills with no error. The `dot_gitconfig.tmpl:48` hostname
precedent v1 cited is chezmoi *templating*, which is exactly the mechanism that
puts work text into the personal repo.

`private_project_roots` also earns little: bindings already live in `.git/config`,
which cannot be committed or pushed, so a private profile is already inert outside
the clones that explicitly bound it.

## 5. Changes to Skill Manager (ordered by value)

### 5.1 Shim resolves persona at launch — the one real change

`_write_adapter_bin` (`project.py:300-320`) emits the baked path as a *default*
rather than an unconditional literal:

```sh
exec skm project run "${SKILL_MANAGER_CONFIG:-/home/keefer/.config/skill-manager/config.yaml}" "$PWD" pi -- "$@"
```

Harness children inherit the environment unchanged (`harness.py:186`), so direnv,
per-shell exports, and CI all reach it. Baked value remains the fallback: nothing
regresses when unset. Test seams: `tests/test_harness.py:278`,
`tests/test_project.py:125`.

**Also required** (`project.py:243-252`, §2.7-2.8): make the shim directory
persona-scoped, or refuse to overwrite an existing shim whose embedded config path
differs without `--force`. Today the second `skm setup` silently rewrites the first
persona's shims — and the personal ansible role runs `setup` on every host (LV-1).

### 5.2 Source-identity guard in shared state

Two configs sharing `state_dir` that both declare a source named `skills` for
different repos silently resolve to whichever synced first (`storage.py:61-72` skips
existing dirs; `_locked_source_root` reads the same path, `:224-236`). Write a
one-line provenance marker next to `sources/<name>/<rev>`, or namespace acquired
sources by config identity. Small, prevents a wrong-content-by-silence failure.

### 5.3 Refuse to manage a leak root

`apply_project` currently treats `~/.local/share/chezmoi` as an ordinary project
(§2.6). Add: refuse apply/launch when the project root is inside a configured
`protected_roots` (default: the chezmoi source dir, `dirname(config_path)`). Cheap,
and it is the only skm-side action that prevents private material being *created*
inside personal trees.

### 5.4 If and when fragments land, these are the real costs (from review)

- **Per-fragment locks are a seam refactor.** `LoadedConfiguration` is a frozen
  single-person object with one `lock_path`/`state_dir`/merged catalog
  (`config.py:60-73`) consumed wholesale by `sync`/`_update_lock`
  (`storage.py:53-120`, one file, no partial merge), `load_lock` (`:135-143`),
  `resolve_locked_inventory` (`:152-160`), `apply_project` (`project.py:173-174`).
  Needs a source→fragment index threaded through storage, project, and cli, plus
  `LockFile.version` handling that exists but is unused (`storage.py:41-45`).
- **Order of operations changes.** `load_configuration` validates configured
  profiles eagerly against the merged catalog (`config.py:143-149`) and computes
  `state_dir`/`lock_path`/`consumers` once (`:153-166`). Fragments require:
  discover → merge catalogs → merge scalars+provenance → validate. Merge rules are
  also still undefined for two non-scalar fields: `consumers` and
  `harness_passthrough`.
- **"No cross-boundary selection" needs provenance that does not exist.**
  `resolve_profile`/`_matches` see a flat `ResolvedSkill` tuple with no declaring
  file (`catalog.py:178-207`), and `refine_project_selection` re-selects from the
  *entire* inventory using clone-local git config (`project.py:183-205`) with
  availability checks in `cli.py:233-260`. A personal clone carrying
  `include_tags: [engineering]` selects private `engineering` skills on a work
  machine. Enforcing the rule means provenance in three places, not one.
- **`skm audit` is a subsystem, not a flag**: no provenance, no fragment registry,
  no record of which persona produced a state (`ConsumerManifest` stores only
  `consumer_path` + entries, `provision.py:14-19`).
- **Fragment-local `state_dir` breaks PATH hygiene**: `_managed_dirs` is a
  one-tuple (`project.py:336-337`) feeding `external_search_path`/`find_executable`
  (`harness.py:209-235`). A second state dir is treated as unmanaged, so a shim
  inside it can be chosen as the "real" harness → self-exec loop.

## 6. Where the boundary is actually enforced

**Load-bearing invariant:** *private material lives where no personal-repo tooling
walks, and nothing tracked by a personal repo may name a private artifact.*
Placement + a commit-time check. §7.2-style selector isolation is a consequence of
placement under this design, not a mechanism.

Hard (blocking) vs soft (reporting):

| Control | Where | Hard? |
|---|---|---|
| Dotfiles pre-commit hook: reject staged paths matching `skill-manager/conf.d/*`, `*work*catalog*`, `*work.lock*`, and staged content matching a private-name list in a gitignored local file | git hook, `core.hooksPath` (unset today) | **yes — the only thing that can be**, since the leak happens outside skm |
| Private repo outside `~/.config` and outside the chezmoi tree | filesystem placement | yes, by construction |
| `.chezmoiignore` + `.gitignore` entries for `.pi-subagents/`, `.agents/skills/`, `.claude/skills/`, `*.lock.yaml` | chezmoi repo (already uses this mechanism, `.chezmoiignore:24-25`) | yes for chezmoi-managed paths |
| Un-track `config.lock.yaml`, delete both `chezmoi add` instructions | chezmoi repo | yes |
| Shim refuses to clobber a foreign persona's shim without `--force` | skm (§5.1) | yes |
| Refuse apply/launch inside `protected_roots` | skm (§5.3) | yes |
| `skm audit [--staged]` reporting private names under protected roots | skm | soft; backs the hook later |

**Lock policy (recommendation: stop publishing locks).** Intent-pinning already
lives in the catalog (`ref: v1.2.3`). The lock is derived machine state whose
payload is names + full commit SHAs — precisely the metadata at issue. Un-track it,
drop the README/`chezmoi add` step, and let `skm sync` rebuild per machine.

## 7. Leak register (review findings, ranked)

| ID | Sev | Vector | Owner |
|----|-----|--------|-------|
| LV-1 | Critical | Personal ansible role runs bare `skm sync`/`setup`; `generate_global_adapters` shares one shim dir and unlinks others → work machine silently repinned to personal config; personal `apply_project` then mutates work projects | dotfiles + skm §5.1 |
| LV-2 | High | `default_project_profile` materializes private symlinks into *any* unbound dir; `_update_git_excludes` early-returns outside git (`project.py:431-434`) → no ignore net | skm §5.3 + omit default |
| LV-3 | High | Harness artifacts already inside the dotfiles tree (`chezmoi/.pi-subagents/{artifacts,missions}`); a work session run there writes private content into the personal repo. No hooks installed | skm §5.3 + ignore files + hook |
| LV-4 | Med-high | No GC: deleting a fragment/persona leaves `sources/<name>/<sha>`, `profiles/<profile>/…`, and manifests that **name private project roots**, captured by later backups (`README.md:221`) | skm (needs `forget`) |
| LV-5 | Med-high | `conf.d` inside a chezmoi-managed dir; realpath-only checks pass a symlink whose *name and target string* sit in managed territory | placement (§6) |
| LV-6 | Med | Nothing validates contributed paths (catalogs, `path:` roots, `state_dir`, consumers) against protected roots; the personal catalog already carries an absolute `path:` into the dotfiles tree (`catalog.yaml:94`) | skm + review discipline |
| LV-7 | Med | `skm project env` prints state-dir paths embedding `sha256(project root)` (`project.py:225-230`); habitually pasted into a committed `.envrc` → home path + persona per repo | docs + skm wording |
| LV-8 | Med | Env-injected config/fragments are unauthenticated: any command a session runs (repo hook, justfile) can point `SKILL_MANAGER_CONFIG` at the private config and have LV-2 write into its chosen cwd | skm: cwd-bound or `shared`-only env contributions |
| LV-9 | Low-med | Install facts leak via receipts: uv receipt holds the full requirement and is `slurp`ed by the role; expressing "work fork" by overriding `skill_manager_repository` puts a private URL in personal dotfiles | dotfiles inventory |
| LV-10 | Low-med | Containment is one-directional. `_codex_home_skill_overrides()` embeds every `~/.agents/skills/*/SKILL.md` absolute path in argv (`harness.py:272-283`) and pi gets `--skill` project dirs (`:194-200`) → **personal** names land in **work** transcripts (`~/.codex/history.jsonl`, `~/.pi/agent/*`). Clone-local refinements (`include_tags: [engineering]`) also carry across personas | skm (transparency note) + §5.4 provenance |
| LV-11 | Low-med | No implementation behind permission/backup claims: default umask dirs, `_write_yaml_atomically` 0644 (`storage.py:251-260`), `~/.local/share` in Time Machine scope, chezmoi has no encryption configured | skm (`0700` + test) + `tmutil addexclusion` doc |
| LV-12 | Low | Ignore-layer assumptions: `.chezmoiignore` covers neither `config.lock.yaml` nor `skill-manager/`; dotfiles `.gitignore` is a Python template with bare `dist/` (`:26`), `lib/` (`:30`), `var/` (`:37`), `*.log` (`:75`) — with one surgical rescue (`!dot_ai/skills/caido-mode/lib/`) — so skill support files under those names can silently fail to ship | dotfiles hygiene |

## 8. Phases

**Phase 0 — boundary now, no skm code (hours).**
Private repo at `~/work/agent-skills/{config.yaml,catalog.yaml}` outside
`~/.config` and outside chezmoi. `.envrc` in work roots exporting
`SKILL_MANAGER_CONFIG`. Un-track `config.lock.yaml` + delete both `chezmoi add`
instructions (`dotfiles README:95`, `dot_ai/skills/README.md:16`). Install a real
dotfiles pre-commit hook via `core.hooksPath` (none today) with the private-name
list. Extend `.chezmoiignore`/`.gitignore` for `.pi-subagents/`,
`.agents/skills/`, `.claude/skills/`. Stop the role from running bare `setup` on
work hosts (persona-qualified var from external inventory, not from the personal
repo). **Do not set `default_project_profile` in the work config** until §5.3 lands.
*Exit:* a work-session harness exposes work skills and no personal ones; nothing
work-named appears in `git -C ~/.local/share/chezmoi status` or its tracked files.

**Phase 1 — the persona fix (~1 day).**
§5.1 shim resolves `${SKILL_MANAGER_CONFIG:-baked}`, plus non-clobbering/persona-scoped
shim dir; §5.2 source-identity guard; §5.3 `protected_roots` refusal.
*Exit:* two personas coexist on one machine without `skm setup` fighting;
`tests/test_harness.py:278` + `tests/test_project.py:125` extended and green.

**Phase 2 — only if Q1 = hybrid *and* Phase 1 proves insufficient.**
`conf.d` discovery outside protected roots + additive `MachineConfig` merge +
provenance + per-fragment locks, budgeted per §5.4. Ship with **no** `when`,
`visibility`, `namespace`, or `private_project_roots`: fragments are active iff
present, which is Option 0 with a glob and is where the composability value lives.

**Phase 3 — containment, minimal, if Q3 demands more than placement.**
One `private: true` boolean: (a) refuse a fragment/state path under a protected
root, (b) make its sources unselectable by selectors not contributed by it — which
first requires selection provenance in `refine_project_selection` and the
`skm add/remove` path (§5.4). `skm audit` reduces to: private names in git-tracked
or staged files under a protected root, callable from the hook.

**Cut outright** (revisit with evidence): `when:` DSL + `fragment why`; profile
namespaces; `private_project_roots`; private state subtrees; `$HOME` transcript
scanning (README paragraph instead); `SKM_WORKSPACE`; Options 2 and 3 (one line
each in a decision log).

**Also fix now, unrelated to this RFC:** the role's receipt guard never matches a
`directory` install, so every ansible run force-reinstalls skm from
`git+…@54e3703` (§2.12) — which also means the pinned revision, not your dev
checkout, is what actually runs.

## 9. Testing

- **Shim:** emitted shim resolves `SKILL_MANAGER_CONFIG` when set and the baked
  path when unset; a second `skm setup` for a different config does not silently
  rewrite or delete a foreign persona's shim; harness children inherit the var.
- **Collision:** two configs sharing `state_dir`, same source name, different repos
  → error naming both origins, not first-sync-wins.
- **Protected roots:** apply/launch inside a protected root refuses non-zero and
  creates no symlinks, no manifest, no exclude block.
- **Lock scoping:** each persona config writes only its own `*.lock.yaml`; a persona
  sync succeeds with the other persona's repo unreachable.
- **Deferred (fragments):** absent `conf.d` is a no-op; lexicographic order;
  duplicate names error naming both files; malformed fragment aborts rather than
  skipping; scalar last-wins with provenance; `consumers`/`harness_passthrough`
  merge rules defined and tested.
- **Deferred (containment):** `private` under a protected root → load error;
  selector isolation enforced in `resolve_profile`, `refine_project_selection`, and
  CLI availability checks; `audit --staged` flags a planted name.

## 10. Vocabulary for CONTEXT.md

**Persona**: an independently-owned configuration and inventory set (personal,
work) with its own config file and lock. Personas do not share declarations by
merging; one config may *reuse* another's catalog by path. _Avoid_: profile, env

**Persona resolution**: choosing which config file a launch reads, decided by the
launch environment (`SKILL_MANAGER_CONFIG`, shell startup, direnv). Skill Manager
never guesses from the current directory. _Avoid_: auto-detect, workspace sniffing

**Protected root**: a tree whose contents leave the private boundary — a personal
dotfiles repo, the chezmoi source dir, the base config's directory — where Skill
Manager will not create managed entries and whose tracked files may not name private
artifacts. _Avoid_: leak root (imprecise: names leak, trees do not)

**Materialization**: writing managed skill links into a project's consumer
directories. Distinct from activation: a profile can be active in config yet
materialize nothing if the root is protected. _Avoid_: install

**Retention**: state that outlives the declaration that produced it — acquired
revisions, profile views, manifests, exclude blocks. Currently unbounded (§2.6,
LV-4). _Avoid_: cache, garbage

Deferred vocabulary (do not add until fragments exist): Fragment/Overlay,
Provenance, Visibility, Namespace.

## 11. Decisions needed before any schema work

1. **Hybrid?** Does any single machine need *both* personas live at once, for
   different projects, under a single `skm setup`? Phase 0+1 covers hybrid *if*
   per-launch selection via env is acceptable; Phase 2 (fragments) is only for
   "one setup, both inventories, no env juggling".
2. **Private skill home**: one private git repo you control (`git:` + refs,
   lockable) or a local tree you edit in place (`path:`, live, unversioned), or
   both? Shared with coworkers? If shared, the *work catalog* becomes the shared
   artifact and composition (Phase 2) jumps in priority — this is the strongest
   argument for fragments, stronger than the hybrid case.
3. **Required strength.** "Names would be embarrassing on GitHub" → placement + hook
   is enough. "Administratively separated" → separate unix user or machine, private
   state on encrypted storage excluded from backups, and no shared home-dir
   transcripts; skm's claim is then *inventory separation*, not confidentiality.
   Record whichever we accept.
4. **Locks out of dotfiles?** (recommendation §6: yes). Touches two READMEs and the
   role.
5. **Where does persona truth live for provisioning?** The role currently derives
   everything from the personal repo and cannot express "this host is work" without
   putting work text in personal dotfiles (LV-1, LV-9). Options: ansible inventory
   group with a vault var, a machine-local unmanaged marker file, or per-persona
   dotfiles repos.
6. **Transcript hygiene** (LV-3, LV-10): is redirecting harness session/artifact
   dirs per persona in scope (env like `PI_SESSION_DIR`), or explicitly out of scope
   and documented as such?
7. **Retention/GC** (LV-4): does skm need `skm forget`/`skm gc` before a private
   persona is trustworthy, given that private names and private *project paths*
   persist in state and backups after the declaration is gone?
