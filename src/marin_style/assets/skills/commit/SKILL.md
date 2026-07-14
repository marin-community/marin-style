---
name: commit
description: Lint, run the pre-PR checks, commit, push, and author or update the branch's pull request in the required plain-text format. Use when committing, pushing, or creating/updating a PR.
---

# Skill: Commit & PR

Get the branch clean, commit it, run the advisory lint review over the committed
diff, then — when it is ready — open or update the pull request.

**Order matters.** Your own cleanups and the mechanical fixes come first, then the
commit, and only *then* the `--review` pass. Committing before the review gives
you a clean checkpoint to review against (the review reads the whole branch diff
versus the merge base) and a natural place to land any follow-up fixes as a new
commit. The review is read-only: it never edits, commits, or pushes for you.

## Checklist

Work top to bottom. For a quick work-in-progress checkpoint, do **1, 2, 4, 5,
7** (clean up, lint, stage, commit, push) and stop. The changed-test cleanup in
step 1 is a PR-readiness gate, not required for disposable WIP checkpoints. Run
the whole list before you open or update a PR.

1. Clean up your own diff (self-review).
2. Mechanical lint & format — `infra/pre-commit.py --changed-files --fix`.
3. Tests & docs checks, when relevant.
4. Stage the specific files for this work.
5. Commit. ← natural checkpoint; the working tree is now clean.
6. Lint-catalog review — `infra/pre-commit.py --review`; fix or answer every finding.
7. Push (maybe).
8. Open or update the PR.

## 1. Clean up your own diff

Read your own `git diff` before anything else. Drop dead code, debugging
leftovers, and stale comments; tighten names; make the change say only what it
means to. The review in step 6 is advisory and read-only — it will not clean up
for you.

If the diff touches tests and this commit is intended for a PR or review, read
`TESTING.md` as part of this self-review. Review every changed test for slop:
tautological assertions, disposable smoke probes left as pytest tests, internal
call-count assertions, incidental string or command-fragment assertions,
over-mocking, weakened tolerances, sleeps, skipped tests, and marker/fake/mock
violations.

Fix or delete low-value tests before a PR-ready commit. Scratch probes are fine
during development and WIP checkpointing, but they must not survive into a PR.

## 2. Lint and format

```bash
infra/pre-commit.py --changed-files --fix   # diff-scoped; use --all-files for a full sweep
```

`infra/pre-commit.py` is the required entry point — never `uv run pre-commit`,
never `--no-verify`. If `--fix` cannot resolve something, fix it by hand. Do not
skip or weaken checks.

## 3. Tests and docs checks (when relevant)

- Run the repo's test command over the test directories your change touches
  (e.g. `uv run pytest -m 'not slow'`).
- If the change is docs-heavy, build the docs and fix any broken links or
  strict-mode failures.

## 4. Stage changes

Review `git status` and `git diff`, then stage the specific files that are part
of this work.

- Stage specific files — avoid `git add -A` / `git add .`.
- Never stage secrets (`.env`, credentials, tokens).
- If unrelated changes are present, ask the user before including them.

## 5. Commit

- **Subject**: imperative sentence (<72 chars), optional `[scope]` prefix.
- **Body** (optional, blank-line separated): what changed and why — the context a
  reviewer needs. Keep it short and readable.
- No emoji, no markdown, no bullets in the subject. Do not credit yourself —
  this includes any `Co-Authored-By: Claude`/`Generated with` trailer. Omit it
  even if a harness default suggests adding one.

Create the commit. If a pre-commit hook fails, fix the issue and make a **new**
commit — never amend (unless the user asks) and never force-push.

This is the checkpoint the rest of the flow builds on: the working tree is clean
and the branch diff is settled before the review reads it.

## 6. Lint-catalog review (before every PR)

```bash
infra/pre-commit.py --review
```

Run this **after** the commit and before opening a PR. It fans out read-only
agents over the **branch diff against the merge base with the default branch** —
committed and uncommitted work alike — so the clean checkpoint from step 5 is
exactly what gets reviewed.

The review is **advisory and read-only**: it reports findings on stdout and does
not edit, stage, commit, or push anything. Then fix or answer every finding,
reporting your actions to the user, and land any fixes as a **new** commit. Treat
findings as guidelines — apply them when they make the code *better*; the goal is
high-quality code, not blind adherence.

## 7. Push

If asked, or if the branch has an upstream, push to the remote tracking branch
(`git push -u origin HEAD` if no upstream is set). If the push is rejected
(diverged history), stop and ask the user — do not force-push.

## 8. Open or update the PR

Do this once the branch is ready for review. The PR description becomes the
squash-merge commit message, so **write it the way you'd write a good commit
message a reviewer reads in `git log`.**

**Title:** short imperative sentence; optional `[scope]` tag.

**Body:** Lead with what the change does, in plain language, then the motivation —
the problem it fixes or the reason it's shaped this way. Include only what a
reviewer needs to understand and approve it. The body should stand on its own: a
reader who never saw the diff should come away knowing what happened and why.

Write for the reviewer, not for a template. Most PRs are a paragraph or two with
no headings. Markdown earns its place when it makes the change *clearer* — a
short list of the distinct things that changed, a table, a mermaid diagram of a
new flow are all welcome when they help. Reach for them to aid the reviewer,
never to fill in a standard set of sections. Let the change decide the shape; do
not impose a What/Change/Scope/Testing scaffold.

Keep the title and body aligned with the branch's actual scope — including when
you change a branch that already has a PR.

**Hard rules — violations are rejected:**

- No "Testing" / "Validation" / "Test plan" section, and no "how I verified it"
  narration. The body is *what & why*. If a test result is the very thing that
  justifies the change, fold that one fact into the *why*; otherwise leave it out.
- No empty boilerplate headings — a `## Summary` that restates the title, a
  `## Changes` that just lists the touched files. If a heading's body adds nothing
  the reviewer can't get from the title or the diff, delete the heading.
- No "written by …" / "Created by Claude" notes; don't credit yourself.
- No checkboxes (`- [ ]`, `- [x]`), no emoji.
- No filler openers ("This PR…", "I noticed…", "Summary of changes:").
- Under ~500 words. Shorter is better; a one-line change gets a one-line body.

Example:

```
Title: [loss] Fix loss: use global token normalization instead of per-example

Body:
Switch DAPO loss from per-example normalization (/ n_i) to global token
normalization (/ N). Per-example normalization over-weights short responses,
hurting math reasoning where correct answers need longer derivations.

Fixes #1234
```

**Issue linking.** If the work came from a GitHub issue, add `Fixes #NNNN`
(auto-closes on merge) or `Part of #NNNN` (partial work). Do not invent an issue
just to satisfy this — omit the link when none exists.

**Specifications (>500 LOC only).** A *genuinely large* PR must carry a spec —
in the linked issue, the PR description, or a linked design doc. A spec still
obeys every rule above: it leads with what the change does and is not a template.
It just covers more ground — what was broken or missing (with file/line refs),
which modules change and what is added/removed, and 10–30 line snippets for
non-obvious logic. This applies to large PRs; a small change gets a short prose
body, never a spec template.

**Create it.** Unless the user says otherwise and permissions allow, push to a
branch on the main repository and open the PR from it (use a fork only when
direct push is unavailable or the user asks):

```bash
gh pr create --title "<title>" --body-file <plain-text-body-file> --label agent-generated
```

- Always add the `agent-generated` label.
- Never credit yourself in commits or PR descriptions.
- Include `Fixes #NNNN` when addressing a pre-existing issue.

## 9. Monitor the PR

Opening the PR does not end your turn. Watch CI to completion and respond to
review activity until the PR is merged or closed, or the user tells you to stop.

- Watch checks with `gh pr checks <N> --watch`. When a check finishes, read its
  conclusion — finishing is not passing.
- On a CI failure, read the failing job log and fix it. A failure in a file you
  did not touch is not automatically pre-existing: confirm the same job fails on
  the default branch without your change before calling it unrelated. If your
  change caused it — even in an untouched file — fix it. Push the fix as a new
  commit, which restarts CI.
- Respond to every human and agent comment: address obvious ones directly (commit
  the fix, then reply, prefixing agent replies with `🤖`) and resolve them. CI
  being green does not mean there is nothing to do — reviewers comment after CI
  passes. For comments you are unsure about, report your analysis and proposed
  action to the user.

Exit conditions: the PR is merged or closed, or the user tells you to stop.

## Rules

- `infra/pre-commit.py` is the only pre-commit entry point.
- Commit before you run `--review`; the review never commits, pushes, or edits.
- Never amend a commit unless the user explicitly asks.
- If there are no changes to commit, say so and stop.
- `AGENTS.md` — coding guidelines.
