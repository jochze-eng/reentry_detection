# CLAUDE.md — Development Workflow

This file defines **how work gets done** in this repository: how bugs and features
are tracked, branched, committed, and merged. Claude Code and human contributors
both follow it.

> For **what the code is and how it's built**, see [`agents.md`](./agents.md) — the
> architecture and codebase guide. This file is process; that file is structure.

---

## 1. Every change starts as a GitHub Issue

No work begins without an issue. Issues are the unit of tracking for both bug fixes
and new features.

- **Bugs** → open a [🐛 Bug Report](.github/ISSUE_TEMPLATE/bug_report.yml)
- **Features** → open an [✨ Feature Request](.github/ISSUE_TEMPLATE/feature_request.yml)

Each new issue lands with `status: triage`. During triage, add:

| Label group  | Pick exactly | Examples |
|--------------|--------------|----------|
| `type:`      | one          | `type: bug`, `type: feature`, `type: chore`, `type: docs` |
| `priority:`  | one          | `priority: critical` → `priority: low` |
| `area:`      | one or more  | `area: LPR`, `area: FR`, `area: auth`, `area: infra`, `area: ui` |
| `status:`    | one (moves)  | `triage` → `in-progress` → `in-review` → *(closed)* |

The full label set lives in [`.github/labels.yml`](.github/labels.yml). Create/update
the labels on GitHub with [`.github/setup-github.sh`](.github/setup-github.sh).

## 2. Branch per issue

Never commit feature/fix work directly to `main`. Cut a branch named for the issue:

```
<type>/<issue-number>-<short-slug>
```

| Work type   | Prefix   | Example                          |
|-------------|----------|----------------------------------|
| Bug fix     | `fix/`   | `fix/42-duplicate-fr-events`     |
| Feature     | `feat/`  | `feat/57-csv-export`             |
| Chore       | `chore/` | `chore/61-bump-fastapi`          |
| Docs        | `docs/`  | `docs/63-installer-readme`       |

```bash
git switch -c fix/42-duplicate-fr-events
```

Move the issue to `status: in-progress`.

## 3. Commit convention (Conventional Commits)

Commit messages match the repo's existing history:

```
<type>: <imperative summary>

<optional body — the "why", wrapped ~72 cols>
```

Types: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`. Keep commits focused and
scoped to the issue. Do **not** commit `config.json`, credentials, certs, or `dist/`.

## 4. Open a Pull Request

Push the branch and open a PR. The [PR template](.github/pull_request_template.md)
is applied automatically — fill in every section and **link the issue** so it closes
on merge:

```
Closes #42
```

Move the issue to `status: in-review`. Before requesting review, verify the change by
exercising the affected flow end-to-end (not just tests) and run whatever is in `tests/`.

## 5. Merge & close

Merge to `main` once reviewed and green. `Closes #<n>` closes the issue automatically.
Delete the branch. If the change altered architecture or behavior, confirm `agents.md`
was updated in the same PR.

---

## Instructions for Claude Code

When working in this repo, follow the process above **proactively**:

1. **Bug fix or feature request from the user** → first restate it as an issue
   (title + template fields + proposed labels). Offer to create it with `gh issue create`
   if `gh` is available; otherwise give the user the ready-to-paste issue body.
2. **Before editing code** → create the correctly-named branch (`fix/…` or `feat/…`).
   If no issue number exists yet, ask for or create the issue first.
3. **Commits** → Conventional Commits format, focused, with the trailing
   `Co-Authored-By` line. Never commit secrets or `config.json`.
4. **When done** → open a PR (or draft the PR body) that links the issue with
   `Closes #<n>`, and fill the PR template. Verify the change end-to-end first.
5. **Architecture/behavior changed** → update `agents.md` in the same change.
6. **Direct commits to `main`** are only for trivial, unbranched fixes the user
   explicitly asks to commit in place — default to the branch + PR flow otherwise.

**One-time setup** (not yet run): install the `gh` CLI, `gh auth login`, then run
`bash .github/setup-github.sh` to create the labels and optional Projects board.
