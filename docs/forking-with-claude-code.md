# Forking ROMulus & Continuing the Build with Claude Code

ROMulus was built almost entirely by a single human maintainer driving
[Claude Code](https://docs.claude.com/en/docs/claude-code) — the agentic
CLI from Anthropic — through a structured workflow of project rules,
session checklists, and a layered design doc. This guide is for the next
person who wants to fork ROMulus and keep the same workflow working.

You don't need the same plugins, the same agents, or even Claude Code
specifically (Codex, Cursor, Aider, and others can all read the same
docs). But the workflow assumes its tooling, and most of the value
comes from the docs + tooling working together. So:

1. **You can use ROMulus as-is** with vanilla Claude Code (no plugins,
   no extra agents). Everything still works — the workflow just leans
   on the general-purpose agent to do everything.
2. **You'll get more leverage with the subagents** referenced in
   `CLAUDE.md` (e.g. `python-pro`, `code-reviewer`, `security-auditor`,
   `test-automator`, `docs-writer`). Skip to the [Plugins
   section](#plugins-and-subagents) below for install pointers.

This document covers both paths.

---

## 1. What you're forking

ROMulus is shipped as four interlocking layers of documentation. When
you fork, you are forking all four; understanding what each one is for
will save you time.

| Layer | File | What it owns |
|---|---|---|
| **Project rules** | [`CLAUDE.md`](../CLAUDE.md) | The non-negotiable rules, design constraints, agent routing table, git policy, and "what is the current state of the project" snapshot. Every Claude Code session starts by reading this. Keep it under ~500 lines. |
| **High-level architecture** | [`docs/architecture.md`](architecture.md) | The system diagram, design rules with rationale, threading model, sync modes, schema overview, configuration reference, known limitations. The "how is this built and why" doc. |
| **Implementation spec** | [`docs/TECHNICAL_PLAN.md`](TECHNICAL_PLAN.md) | The deep reference — schema column-by-column, identifier pipeline pseudocode, every subsystem in depth, post-implementation fix records. Read on-demand for edge cases. |
| **Per-feature sessions** | [`docs/sessions/NN-slug.md`](sessions/) | Self-contained task lists for a single piece of work — context, scope, acceptance criteria, completion summary. Sessions 00–11 are the bootstrap (done); newer work is committed directly via Conventional Commits without a session file. |

Plus the supporting docs:

- [`docs/sync-design.md`](sync-design.md), [`docs/import-design.md`](import-design.md) — feature-specific references for the shipped Sync and Import workflows.
- [`docs/strict-1to1-design.md`](strict-1to1-design.md) — the v0.4.0 strict 1:1 rom↔game data-model design doc (background, model, trade-offs, future work).
- [`docs/ROM-FORMATS-REFERENCE.md`](ROM-FORMATS-REFERENCE.md), [`docs/ROM-DEDUP-METHODOLOGY.md`](ROM-DEDUP-METHODOLOGY.md), [`docs/ROM-LIBRARY-ANALYSIS-REPORT.md`](ROM-LIBRARY-ANALYSIS-REPORT.md) — domain knowledge.
- [`docs/CREDITS.md`](CREDITS.md) — upstream attribution.
- [`docs/KNOWN-ISSUES.md`](KNOWN-ISSUES.md) — open bugs triaged for later. Check before proposing new work.
- [`CHANGELOG.md`](../CHANGELOG.md) — per-release log.

The `Co-Authored-By: Claude Opus ...` trailers on commits are the
audit trail for which work was LLM-assisted (most of it).

---

## 2. Setting up your fork

### Clone + branch

```bash
git clone https://github.com/Sphexi/ROMulous.git my-romulus-fork
cd my-romulus-fork
git remote rename origin upstream
git remote add origin git@github.com:<you>/<your-fork>.git
git push -u origin main
```

If you want to track upstream changes, leave the `upstream` remote in
place. Otherwise drop it.

### Python environment

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1   # Windows
# source .venv/bin/activate   # macOS / Linux
pip install -e ".[dev]"
```

Sanity-check the test suite before you touch anything:

```bash
.venv/Scripts/python.exe -m pytest
.venv/Scripts/python.exe -m ruff check src/ tests/
```

Current expected state: **1,015 tests passing, 8 skipped** (7
platform-specific cover-UI skips + 1 POSIX chmod skip on Windows).
If anything else fails on a clean checkout, file an issue against
upstream before assuming the fork is the problem.

### What to change in `CLAUDE.md`

When you fork, edit `CLAUDE.md` first. The sections that almost always
need updating:

- **"Project Tier"** — leave `Standard` unless you're scaling down (no
  tests, hobby-only) or up (security review every PR, multiple
  reviewers).
- **"Current State"** — this is a moving snapshot. Update it when test
  counts change or major features land.
- **"License"** — Apache 2.0 unless you're explicitly relicensing.
- **"Git Policy"** — change if you want Claude Code to push, or if
  you're not using Conventional Commits.
- **"Agent & Plugin Routing"** — adapt to whichever subagents you
  actually have installed (see [§3](#plugins-and-subagents)).

What **not** to weaken without thinking it through: the
[Key Design Rules](../CLAUDE.md#key-design-rules-non-negotiable). They
encode hard-won architectural decisions (single-library design,
tombstone-don't-delete, atomic writes only, hacks-are-first-class,
local-first, strict 1:1 rom identity, sibling-copy API gate).
Re-read [`docs/architecture.md`](architecture.md) and
[`docs/strict-1to1-design.md`](strict-1to1-design.md) before deleting
any of them.

---

## 3. Plugins and subagents

ROMulus's `CLAUDE.md` routes most tasks to specialized subagents (e.g.
`python-pro` for Python, `code-reviewer` for code review,
`security-auditor` for security audits, `test-automator` for test
suites). These come from the **claude-code-workflows** plugin
marketplace built on **[wshobson/agents](https://github.com/wshobson/agents)** —
a community curated collection of specialist agents and skills.

### Path A: Install the same plugins (recommended)

The fastest path to "the workflow works the way the docs describe":

```text
/plugin marketplace add wshobson/agents
/plugin install python-development@claude-code-workflows
/plugin install backend-development@claude-code-workflows
/plugin install code-documentation@claude-code-workflows
/plugin install debugging-toolkit@claude-code-workflows
/plugin install git-pr-workflows@claude-code-workflows
/plugin install comprehensive-review@claude-code-workflows
/plugin install unit-testing@claude-code-workflows
/plugin install codebase-cleanup@claude-code-workflows
/plugin install error-debugging@claude-code-workflows
/plugin install application-performance@claude-code-workflows
/plugin install security-scanning@claude-code-workflows
/plugin install database-design@claude-code-workflows
```

Plus, if you want the optional bits:

```text
/plugin install frontend-mobile-development@claude-code-workflows   # only if you add a web UI
/plugin install cloud-infrastructure@claude-code-workflows          # only if you deploy
/plugin install context-management@claude-code-workflows
/plugin install llm-application-dev@claude-code-workflows           # only if you embed an LLM
```

The full list is in the [agent routing table in
`CLAUDE.md`](../CLAUDE.md#agent--plugin-routing). Each row points at a
specific `<plugin>:<agent>` you may want.

Marketplace and per-agent source: <https://github.com/wshobson/agents>.

### Path B: Use Claude Code as-is, no plugins

Vanilla Claude Code ships with one general-purpose agent (the default
when you don't specify one), plus the `Explore` and `Plan` agents.
That's enough to get real work done on ROMulus, but you'll lose the
domain-specialist quality bump.

Practical translations:

- Anywhere `CLAUDE.md` routes to `python-pro`, `frontend-developer`,
  `code-reviewer`, etc. — just let Claude Code do it directly. The
  routing table becomes "skip this, use defaults."
- For multi-step research ("where is feature X implemented?"), use the
  `Explore` agent.
- For implementation planning ("how should I add Y?"), use the
  `Plan` agent.
- For session-scale work breakdown, the project's own custom
  `task-orchestrator` agent in [`.claude/agents/`](../.claude/agents/)
  is local to this repo and doesn't need a plugin. Same for
  `project-architect`, `docs-writer`, `bash-powershell-engineer`,
  `docker-engineer`, `frontend-engineer`, `integration-test-runner`,
  `networking-engineer`, `python-engineer`, `rest-api-engineer`, and
  `test-engineer`. They check into the repo as Markdown files and load
  automatically.

You can also delete or rewrite any of those custom agents — they're
just files. If they don't match your style, replace them.

### Path C: A different marketplace, or your own agents

If you have your own agents (or use a different marketplace —
[here's a curated list](https://github.com/anthropics/claude-code/blob/main/PLUGINS.md)),
edit `CLAUDE.md`'s **Agent & Plugin Routing** table to point at the
ones you actually have. Claude Code only references whatever names you
write down in `CLAUDE.md`.

---

## 4. The session workflow

For non-trivial features, the original maintainer used a
session-per-feature pattern:

1. **Capture intent in a session file.** `docs/sessions/NN-slug.md`
   (e.g. `12-import-roms.md` for the next big feature). The session
   file holds: context, goal, scope, out-of-scope, acceptance criteria,
   and any open questions.
2. **At session start**, point Claude Code at it: *"Read `CLAUDE.md`
   then `docs/sessions/12-import-roms.md`. Produce an execution plan
   before writing code."*
3. **Claude Code reads, plans, asks clarifying questions if needed,
   then implements.** The `task-orchestrator` agent (if installed) is
   the natural orchestrator; otherwise the general-purpose agent
   handles it.
4. **At session end**, append the
   [Completion Summary block](../CLAUDE.md#completion-summary-template)
   to the bottom of the session file. Commit the work. Move on.

For smaller features (a bug fix, a tweak, a one-file refactor), you
can skip the session file and just commit directly with a Conventional
Commits message. That's what 90% of recent work uses.

Look at [`docs/sessions/00-bootstrap.md`](sessions/00-bootstrap.md) for
the canonical example of a complete session file. After session 11
the project moved off numbered sessions entirely — newer work
(Import ROMs, Verify Library, the per-system summary dialog, the
sync diff perf rewrite) was committed directly via Conventional
Commits. See [`CHANGELOG.md`](../CHANGELOG.md) for the per-feature
history of post-bootstrap work.

### Conventions to respect

These came up enough during the original build that they got encoded
into the project. If you want the workflow to keep humming:

- **Conventional Commits.** `feat(scope):`, `fix(scope):`,
  `refactor(scope):`, `docs(scope):`. The historic `Session N:`
  pattern is retired.
- **Co-author trailers.** `Co-Authored-By: Claude Opus 4.7 (1M context)
  <noreply@anthropic.com>` (or whichever model you're using). This is
  the audit trail.
- **Test discipline.** New features add tests. The CI runs `ruff check`
  and `pytest`; both must be clean before merging.
- **No `git push` from Claude Code.** The original workflow denies
  pushes. Either keep that policy, or relax it in `CLAUDE.md` once
  you're comfortable.
- **No `--no-verify` skipping hooks.** Investigate hook failures, don't
  bypass them.

---

## 5. Forking patterns that work well

A few patterns the original maintainer found useful when running
Claude Code over long stretches:

- **Trust but verify.** Always look at the diff before committing.
  Claude Code is good at the bulk typing but will occasionally invent
  function signatures or skip tests. The session checklist's
  "Completion Summary" forces a sanity pass.
- **Keep `CLAUDE.md` short.** The whole thing loads into every
  session's context. Anything that doesn't need to be in working
  memory belongs in `docs/architecture.md` or `docs/TECHNICAL_PLAN.md`.
- **Capture future work as design docs, not TODOs.** When something
  comes up but isn't this session's job, write it as a self-contained
  design note in `docs/`. The `import-design.md` doc is the canonical
  example — when the time comes, the next session has the requirements
  + an obvious implementation path in front of it.
- **Don't let the docs drift.** When code changes invalidate the
  architecture doc or the plan, update them in the same commit. The
  `sync-design.md` doc has a `## 12. Post-implementation notes`
  section specifically because the implementation discovered things
  the spec missed.
- **Use `/loop` for long-running iteration**. Useful for "keep running
  this until X is true" type work (e.g. fixing test failures one at a
  time, watching CI runs).
- **Test on Windows even if you develop on Linux.** ROMulus is
  Windows-first; the CI runs on `windows-latest` to match the
  shipping target. Linux + PySide6 + sqlite3 has produced more than
  one C-level segfault that didn't reproduce on Windows.

---

## 6. When the workflow breaks down

A few failure modes the original maintainer hit, with fixes:

- **"Claude Code keeps re-suggesting the same wrong approach."** It
  doesn't remember. Add the constraint to `CLAUDE.md` under
  [Key Design Rules](../CLAUDE.md#key-design-rules-non-negotiable) or
  the per-feature session file.
- **"Tests pass locally but CI fails."** Local Windows is the
  reference platform; CI is `windows-latest`. If you're developing on
  macOS or Linux and CI fails, the disagreement is almost always
  Qt-related — check that you have `QT_QPA_PLATFORM=offscreen` set in
  the test env locally.
- **"Long context => Claude Code starts forgetting earlier
  decisions."** Compress, or split the session. Each session should
  fit comfortably in one or two context windows.
- **"Agent says it did X but X isn't in the diff."** Always read the
  diff. Agents return *intended* summaries, not necessarily what
  actually happened.

---

## 7. Getting help

- **Claude Code docs:** <https://docs.claude.com/en/docs/claude-code>
- **Claude Agent SDK docs:** <https://docs.claude.com/en/api/agent-sdk/overview>
- **Plugin marketplace (wshobson/agents):**
  <https://github.com/wshobson/agents>
- **Issues / discussions on ROMulus:**
  <https://github.com/Sphexi/ROMulous/issues>

If you build something interesting with your fork, open a discussion
upstream — particularly if you've added new destination profiles,
metadata sources, or platform support. The interesting work is
usually in the extension surface.
