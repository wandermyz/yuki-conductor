# Sparse checkout presets for task worktrees

Status: **proposal**. Open questions are listed at the end. Each one has a
recommended answer, so the plan can go ahead as written if the defaults are
fine.

## Problem

A worktree task gets a full checkout of the project:
`git worktree add <repo>/.claude/worktrees/task-<id> -b task/<id>-<slug> origin/<default>`
(`taskboard/worktree.py:_create`). In a large monorepo this has three costs:

- **Time and disk.** Every task writes the whole tree to disk, and ten tasks
  running at once write it ten times.
- **Agent focus.** `Grep`, `Glob` and `find` search everything, so results
  from unrelated parts of the repo crowd out the relevant ones.
- **Tooling.** IDE indexers, `pnpm install` in the root, and language servers
  started by the agent all scale with the size of the tree.

Most tasks in such a repo touch one or two areas, such as `services/payments`
and `libs/common`. Git's sparse checkout can limit a worktree to those
directories. The user should be able to save named sets of directories per
project ("presets") and pick one when creating a task.

## Git mechanics (verified on git 2.56)

```
git worktree add --no-checkout <path> -b <branch> origin/<base>
git -C <path> sparse-checkout set --cone <dir> <dir> …
git -C <path> checkout
```

A throwaway repo confirmed the following:

- **Sparseness is per worktree.** `sparse-checkout set` inside the worktree
  turns on `extensions.worktreeConfig` in the repo's shared config. It then
  writes `core.sparseCheckout` to that worktree's own `config.worktree` and
  the patterns to `$GIT_DIR/worktrees/<name>/info/sparse-checkout`. The main
  checkout still reports `this worktree is not sparse` and keeps every file.
- **Cone mode always includes files at the repo root,** such as `CLAUDE.md`,
  `AGENTS.md`, `package.json` and `.gitignore`. Directories are listed by
  name, so presets are short and fast to match.
- `git worktree remove` deletes the per-worktree config along with the
  worktree, so `cleanup()` needs no changes. `extensions.worktreeConfig=true`
  stays in the repo config. That setting is harmless.
- `--no-checkout` followed by `checkout` avoids writing the full tree and
  then deleting most of it.

This does **not** reduce what is fetched. The object store is shared with
the main checkout, so a partial clone (`--filter=blob:none`) would be a
separate, repo-level decision. It is out of scope here.

## Design

### Data

**Presets live on the project**, in a new `sparse_presets` table in the
workspace DB, next to `projects`:

```sql
CREATE TABLE sparse_presets (
  project    TEXT NOT NULL,     -- projects.name
  name       TEXT NOT NULL,     -- "payments", "web only"
  paths      TEXT NOT NULL,     -- JSON array of repo-relative dirs, cone mode
  is_default INTEGER NOT NULL DEFAULT 0,  -- preselected in the task form
  sort_order INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (project, name)
)
```

Deleting a project deletes its presets. `ProjectStore.list_all()` returns
each project's presets, so the task form needs no extra request.

**The task stores a snapshot, not a reference:**

| column | meaning |
| --- | --- |
| `tasks.sparse_preset` | preset name, for display only (`NULL` = full checkout) |
| `tasks.sparse_paths` | JSON array copied from the preset when the task is saved |

The worktree is built from `sparse_paths`. Editing or deleting a preset later
never changes a queued, running or reopened task. Showing "payments (edited)"
on the task would cost a comparison and gain little.

### Worktree creation (`worktree.py`)

`_create(task, repo)` gains one branch:

```python
if task.sparse_paths:
    _git(repo, "worktree", "add", "--no-checkout", str(path), *ref_args)
    _git(str(path), "sparse-checkout", "set", "--cone", *task.sparse_paths, *ALWAYS_SPARSE)
    _git(str(path), "checkout")
else:
    _git(repo, "worktree", "add", str(path), *ref_args)  # as today
```

The three existing cases in `_create` (local branch exists, remote branch
exists, new branch from `origin/<base>`) only differ in `ref_args`, so the
sparse step wraps all of them. A reopened task whose PR is still open
therefore gets the same sparse tree on its existing branch.

`ALWAYS_SPARSE = (".claude", ".agents", ".github")` keeps project skills,
Codex skills and CI config in every sparse worktree. Root files are already
included by cone mode. See Q3.

If a preset names a directory that doesn't exist on the base branch, git
accepts it silently and the directory is empty. That's acceptable at
creation. The preset editor warns about it instead (see UI).

When the worktree setup fails partway (for example, `sparse-checkout set`
fails), `_create` runs `worktree remove --force` before raising, so the next
attempt doesn't run into a half-built directory. `ensure_worktree` already
re-creates a worktree whose directory is missing.

### Telling the agent

`prompts.py` adds one line to the "where" section when `sparse_paths` is set:

> This worktree is a **sparse checkout** (cone mode) containing only:
> `services/payments`, `libs/common` (plus root files and `.claude/`).
> Other directories exist in the repo but aren't on disk. If you need one,
> run `git sparse-checkout add <dir>` and say so in your reply.

Without this line, an agent that can't find a file it expects would conclude
the file doesn't exist. The line also makes widening the checkout a normal,
visible step (Q4).

### API and CLI

- `GET /api/projects` → `[{name, path, presets: [{name, paths, is_default}]}]`
- `PUT /api/projects/{name}/presets`: replaces the project's whole preset
  list in one write, as reorder does. The editor only ever saves the full
  list, and this avoids per-preset ids and routes.
- `GET /api/projects/{name}/dirs?prefix=services/`: runs
  `git ls-tree -d --name-only origin/<default>:<prefix>`. It feeds
  autocomplete in the preset editor and is the source of truth for the
  "doesn't exist" warning. It reads the default branch, not the working
  tree, because the main checkout may itself be sparse or out of date.
- Task create and update (`routes.py`, `actions.create`) accept
  `sparse_preset: str | null`. The server resolves the name to `paths` and
  stores both. A name that doesn't exist is a 400.
- `yuki-conductor task create --sparse <preset>` (and `--no-sparse` to
  override a project default). `task show` prints `Sparse: payments
  (services/payments, libs/common)`.

### Rules

- A preset only applies with **Work in a git worktree** on. Turning the
  worktree off clears it.
- The preset can be changed in Edit until the task's worktree exists. After
  that, the field is read-only and shows "Set when the worktree was created".
  Changing a live worktree's sparseness under a running agent isn't worth it.
  The agent can widen the checkout itself, and a Reopen builds a new worktree
  from the stored snapshot.
- Follow-up tasks (`task create` from an agent with a parent) inherit the
  parent's preset unless `--sparse`/`--no-sparse` is given.

## UI mock

### Projects tab: presets on each project

Each project row gets a "Sparse presets" disclosure. It is collapsed by
default and shows the count when the project has presets.

```
┌ Projects ─────────────────────────────────────────────── [+ Add Project] ┐
│ ☰  yuki-conductor     C:\Codebase\yuki-conductor                 [Remove] │
│    ▸ Sparse presets (none)                                               │
├──────────────────────────────────────────────────────────────────────────┤
│ ☰  monorepo           C:\Git\monorepo                            [Remove] │
│    ▾ Sparse presets (2)                                                  │
│    ┌──────────────────────────────────────────────────────────────────┐  │
│    │ ★ payments              services/payments · libs/common          │  │
│    │                                                  [Edit] [Delete] │  │
│    │ ☆ web                   apps/web · libs/ui · libs/common         │  │
│    │                                                  [Edit] [Delete] │  │
│    │ [+ New preset]                                                   │  │
│    └──────────────────────────────────────────────────────────────────┘  │
│    ★ = default for new tasks in this project (click to toggle)          │
└──────────────────────────────────────────────────────────────────────────┘
```

### Preset editor (modal)

```
┌ Sparse preset · monorepo ───────────────────────────────────────────────┐
│ Name   [ payments                                   ]                   │
│                                                                         │
│ Directories (cone mode: each includes everything under it)              │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │ services/payments                                              ×  │  │
│  │ libs/common                                                    ×  │  │
│  │ libs/legacy-billing   ⚠ not on origin/main                     ×  │  │
│  │ [ services/led▌                                               ]   │  │
│  │   ├ services/ledger                                               │  │
│  │   └ services/ledger-migrations                                    │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│ Always included: files at the repo root, .claude/, .agents/, .github/   │
│                                                                         │
│ [✓] Default for new tasks in this project                               │
│                                                                         │
│                                            [Cancel]  [Save preset]      │
└─────────────────────────────────────────────────────────────────────────┘
```

- Typing completes against `/api/projects/{name}/dirs`, one level at a time,
  just like the folder picker. Typing `/` descends a level.
- A path missing from the default branch gets a ⚠ but can still be saved,
  since it may be about to land.
- A path inside another listed path is merged away on save, because cone
  mode already includes it.
- On a phone (~390px), the modal is full-screen and paths wrap under the
  name in the list.

### Task form: picking a preset

The preset selector sits under the worktree checkbox and is only shown when
the checkbox is on **and** the project has presets. Otherwise the form is
unchanged.

```
┌ New task   [◆ Claude Code · monorepo  Change] ──────────────────────────┐
│ Model: [ Opus ▾ ]                                                       │
│ [ Title (optional: defaults to the description)                     ]   │
│ ┌ Write | Preview ──────────────────────────────────────────────────┐  │
│ │ Fix the rounding bug in refund totals…                             │  │
│ └────────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│ [✓] Work in a git worktree                                              │
│     A fresh branch from the default branch. Leave off for read-only…    │
│     Checkout  [ payments ★                                     ▾ ]      │
│               services/payments · libs/common                           │
│                                                                         │
│ [ ] Wait for MCP                                                        │
│ [ ] Skip Permissions                                                    │
│                                     [Save as draft]  [Create task]      │
└─────────────────────────────────────────────────────────────────────────┘

  Checkout ▾ options:
    Full checkout
    payments ★      services/payments · libs/common
    web             apps/web · libs/ui · libs/common
    ─────────────
    Manage presets…   (opens Projects tab on this project)
```

- The default preset (★) is preselected. With no default, "Full checkout"
  is preselected.
- Switching project in the picker resets the selection to the new project's
  default.
- The draft (`draft.ts`) saves the preset name along with the other fields.

### Task detail

The meta line shows a chip next to the branch:

```
#191 · monorepo · task/191-refund-rounding · ◧ sparse: payments
```

Hovering the chip lists the paths. Clicking it does nothing. No chip is
shown for a full checkout.

## Implementation steps

1. `store.py`: create the `sparse_presets` table, add a presets field to
   `list_all()`, add `set_presets(project, presets)`, and make `remove()`
   cascade to presets. Tests: round trip, cascade, single default per project.
2. `task_store.py`: add `sparse_preset` and `sparse_paths` columns and fields.
   `actions.create`/`update` resolve the preset name and enforce the rules
   above.
3. `worktree.py`: add the sparse branch in `_create`, `ALWAYS_SPARSE`, and
   rollback on a failed setup. Tests use the existing temp repo with a bare
   origin. Check that only the listed dirs and root files are on disk, the
   main checkout is not sparse, and reopen with an open PR stays sparse.
4. `prompts.py`: add the sparse line. Test it in `test_taskboard_prompts`.
5. `web_server.py`: add `PUT /api/projects/{name}/presets` and
   `GET /api/projects/{name}/dirs`.
6. `cli_task.py`: add `--sparse`/`--no-sparse`, show the preset in
   `task show`, and update the `yuki-conductor-task` skill doc.
7. Frontend: the presets disclosure and editor in `ProjectsView.tsx`, the
   Checkout select in `TaskForm.tsx`, and the chip in `TaskDetail.tsx`.
   Screenshots on desktop and at 390px.
8. CLAUDE.md: add a "Sparse presets" bullet under Taskboard.

## Open questions

1. **Where should presets live: the DB (Projects tab) or a file in the repo
   (for example, `.yuki/sparse-presets.yaml` committed to the project)?**
   A committed file would be shared with teammates and versioned with the
   code it describes. Recommendation: **the DB.** Projects are already
   DB-only, this is single-user, and committing a file to every target repo
   is intrusive (and for work repos, not ours to commit). Reading a repo
   file could be added later as a read-only source.
2. **Cone mode only, or also raw gitignore-style patterns?**
   Recommendation: **cone only.** Raw patterns (`!*.png`, `/docs/*.md`) are
   much slower on large repos, which is where this feature matters. They are
   also easy to get wrong, and git itself discourages them. Revisit if a real
   use case shows up.
3. **Should `.claude/`, `.agents/` and `.github/` always be included?**
   Without them, a sparse task loses project skills, Codex skills and CI
   config. Recommendation: **yes, always include them, with no opt-out.**
   They are small, and the editor shows them as "Always included". Should
   any other directories be on that list?
4. **May the agent widen the checkout?** Options: (a) allowed, and the
   prompt says to mention it; (b) forbidden: the agent should ask with
   `task ask`; (c) silent. Recommendation: **(a).** A blocked agent costs a
   round trip, and the widening is visible both in its reply and in
   `git sparse-checkout list`.
5. **Should the preset be changeable after the worktree exists?**
   Recommendation: **no** (read-only after creation, as above). The other
   option is to run `sparse-checkout set` on the live worktree between turns.
   That works, but it would have to wait for a running turn to finish, and
   it removes directories the agent may have just added.
6. **Do we need a per-project default preset?** It saves a click on every
   task in a large repo, but a forgotten default quietly narrows tasks that
   needed the whole tree. Recommendation: **yes**, with the ★ shown in the
   form so it's never hidden.
7. **What happens when the main checkout itself is sparse?** Since git 2.36,
   `git worktree add` copies the source worktree's sparse settings into the
   new one. "Full checkout" would then quietly be sparse.
   Recommendation: **"Full checkout" means full.** Run
   `sparse-checkout disable` in the new worktree when the main checkout is
   sparse. Or should "Full checkout" be renamed "Same as main checkout" and
   follow it instead?
8. **Should chat conversations get presets too?** Chats don't create
   worktrees today, so this would come with chat worktrees, if those are
   ever built. Recommendation: **tasks only.**
9. **Partial clone (`--filter=blob:none`) for the fetch side:** in scope?
   Recommendation: **no.** It's a property of the clone the user set up, not
   of a task, and yuki-conductor never clones.
