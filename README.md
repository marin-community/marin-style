# marin-style

Marin's coding standards, agent guidelines, and Claude Code skills packaged as an
installable kit. It exists so that coding agents and contributors working in
Marin-community forks (evalchemy, harbor, MarinSkyRL, vllm, tpu-inference, …)
behave the way they do in the main Marin monorepo: same lint and format checks,
same review catalog, same commit and PR conventions, same testing policy.

The kit has two halves:

- **Enforcement** — a pre-commit linter and an agentic lint-review catalog, run
  through a small `infra/pre-commit.py` shim in each consumer repo.
- **Guidance** — portable `AGENTS.md`/`TESTING.md` cores and a set of skills
  (`commit`, `write-tests`, `debug`, `file-issue`, `writing-style`) that
  `marin-style sync` vendors into the consumer repo's `.agents/` tree.

## Consumption model

A consumer repo adopts the kit with four pieces:

1. **A pinned git dev-dependency.** Add `marin-style` as a dev/tool dependency
   pinned to an exact revision, e.g. in `pyproject.toml`:

   ```toml
   [dependency-groups]
   dev = ["marin-style @ git+https://github.com/marin-community/marin-style@<REV>"]
   ```

   Pinning keeps every contributor and CI run on the same checks; bump `<REV>`
   to adopt a new version.

2. **An `infra/pre-commit.py` shim.** Copy `templates/pre-commit.py` to
   `infra/pre-commit.py`. It is a `uv run --script` file with PEP-723 metadata
   that pins the same revision and execs `marin_style.precommit.main()`. This is
   the single lint entry point the `commit` skill and CI both call — never
   `uv run pre-commit`.

   ```bash
   infra/pre-commit.py --all-files        # run the configured checks over all tracked files
   infra/pre-commit.py --changed-files    # diff-scoped, for fast local iteration
   infra/pre-commit.py --review           # advisory lint-review catalog via headless agents
   ```

3. **A `[tool.marin-style]` config block.** Declare which checks run and, for
   upstream-tracking forks, which paths they cover:

   ```toml
   [tool.marin-style]
   checks = ["ruff-check", "ruff-format", "typecheck"]
   # tier B only: scope every check to the Marin-authored delta
   include = ["marin_ext/**", "eval/**"]
   ```

4. **Vendored guidance via `marin-style sync`.** Run `marin-style sync` to copy
   the agent guidance and skills into the repo (see below), then reference the
   core from the repo's `AGENTS.md`.

### `[tool.marin-style]` keys

Per-repo behavior is read from a `[tool.marin-style]` table in the consumer
repo's root `pyproject.toml`:

| key | type | default | meaning |
| --- | --- | --- | --- |
| `checks` | list | `["ruff-check", "ruff-format"]` | which checks run; drawn from `ruff-check`, `ruff-format`, `black`, `typecheck`, `license-header` |
| `include` | list of globs | everything | limit which files are ever considered (scopes upstream-tracking forks to their own files) |
| `exclude` | list of globs | — | extra excludes appended to the built-in defaults |
| `license_header` | path | — | repo-relative path to a header text file (required when `license-header` is enabled) |
| `main_branch` | str | `"main"` | branch used for changed-file discovery via merge-base |
| `ruff_version` | str | `"0.14.3"` | ruff version to invoke — align with the repo's own ruff pin to avoid format skew |
| `black_version` | str | `"25.9.0"` | black version to invoke |

Tools are invoked as pinned `uvx` versions (ruff, black) and `pyrefly` (which
reads the consumer's own pyproject config).

## `marin-style sync`

```bash
marin-style sync [--repo-root PATH]   # default: git toplevel of the cwd
marin-style sync --check              # CI drift gate; nonzero if vendored files are stale
```

`sync` writes:

- `assets/agents/*.md` → `<root>/.agents/marin-style/` (`AGENTS-core.md`,
  `TESTING-core.md`)
- `assets/skills/<name>/*` → `<root>/.agents/skills/<name>/`

Every vendored file carries a generated-by header, so a re-run overwrites it in
place. Skills the repo authored itself are never touched. `sync` also creates a
`.claude/skills` → `../.agents/skills` symlink if one does not already exist, and
prints a reminder to reference `.agents/marin-style/AGENTS-core.md` from the
repo's `AGENTS.md` if it does not already. Run `marin-style sync --check` in CI to
fail when the vendored tree drifts from the pinned package.

## Tier A vs tier B

Consumer repos fall into two tiers, and the difference is entirely in the
`[tool.marin-style]` config:

- **Tier A — repos we own** (built by Marin, no upstream to track). Run the full
  checks over the whole tree. Formatting, imports, and the lint catalog all apply
  everywhere, exactly as in the monorepo.

- **Tier B — upstream-tracking forks** (e.g. a fork of an external `vllm` or
  `tpu-inference`). Scope every check to the Marin-authored delta with an
  `include` list. Never reformat or re-lint upstream code: doing so creates giant
  diffs against upstream and makes merges painful. The checks apply only to the
  files Marin added or substantially rewrote.

## Failure reporting (`actions/report-failure`)

Scheduled runs (nightlies, canaries) report failures through one composite
action, consumed cross-repo:

```yaml
  report-failure:
    needs: [nightly]   # every job whose failure should be reported
    # cancelled matters: a job that hits its timeout-minutes reports cancelled, not failure
    if: >-
      always() && github.event_name == 'schedule' &&
      (contains(needs.*.result, 'failure') || contains(needs.*.result, 'cancelled'))
    runs-on: ubuntu-latest
    permissions:
      issues: write    # file/update the tracking issue
      actions: read    # pull the failed jobs' logs for the excerpt
      contents: read
    steps:
      - uses: marin-community/marin-style/actions/report-failure@<REV>
        with:
          lane: <repo>-nightly
          trigger-token: ${{ secrets.LOOM_TRIGGER_GH_TOKEN }}
          slack-webhook-url: ${{ secrets.SLACK_WEBHOOK_URL }}
```

The action does three things, in order:

1. **Tracking issue.** Finds the newest open issue titled with the lane's prefix
   (default `[nightly-<lane>]`; override `issue-title-prefix` to attach to another
   convention, e.g. marin's `[canary-<lane>]` issues filed by its inline Claude
   triage) and creates it if absent. Every failure appends one comment with the
   run URL and a failed-job log excerpt, so a flaky week is one issue, not seven.
2. **Weaver auto-triage.** The failure comment is addressed *to* `@weaverbot`
   (the loom trigger phrase) and posted with `trigger-token`. The loom deployment
   launches a weaver session against the repo — or forwards the comment to the
   session already working the issue. The token's owner must be a loom-approved
   user: use a dedicated CI machine account, never weaverbot itself (loom's
   self-trigger guard ignores the bot's own comments), and note that
   `github-actions[bot]` can never trigger. When the token is empty the failure
   is still recorded, with a workflow notice and no mention.
3. **Slack.** Posts lane, run URL, and issue URL to `slack-webhook-url`; skips
   with a notice when empty.

Run it from a separate `needs:`-gated job (as above), not a step in the failing
job — a job-level timeout force-kills in-job cleanup steps, but a downstream job
still runs. Gate on `github.event_name == 'schedule'` so manual re-runs and PR
experiments don't page anyone. The `report-failure-smoke` workflow in this repo
(`workflow_dispatch`) exercises the whole path with a synthetic failure.

Pin `@<REV>` to a tag or commit SHA, like every other consumption of this kit.

## Agent prose cleanup (`actions/prose-cleanup`)

Automated cleanup of agent-generated issue and pull request descriptions is
temporarily disabled. Consumer workflows track `actions/prose-cleanup@main`, so
trusted changes in this repository apply without separate workflow updates. The
action exits successfully without posting comments or changing descriptions. It
needs no write permissions or Loom token while disabled.

## Shared workflow actions

Consumer workflows track these trusted actions on `main`:

- `actions/consult-agent` runs the pinned Claude Code action, converts quota
  exhaustion into a successful `rate_limited=true` result, preserves other
  failures, and forwards Claude's outputs.
- `actions/notify-slack` posts a fallback message or the contents of a workflow
  artifact named `slack_message.md` to an incoming webhook.

Use `marin-community/marin-style/actions/<name>@main` so workflow behavior can
be maintained in this repository.

## Adding a repo

1. Add the pinned `marin-style` git dev-dependency.
2. Copy `templates/pre-commit.py` to `infra/pre-commit.py` and set `<REV>` to the
   pinned revision.
3. Add a `[tool.marin-style]` block. Tier A: list `checks`. Tier B: also add an
   `include` list scoping checks to the Marin delta.
4. Run `marin-style sync`, then add `@.agents/marin-style/AGENTS-core.md` to the
   repo's `AGENTS.md` (the sync command prints this reminder).
5. Wire CI: run `infra/pre-commit.py --all-files` and `marin-style sync --check`
   on pull requests. Copy `src/marin_style/assets/templates/prose-cleanup.yaml`
   to install the disabled `agent-generated` description-cleanup placeholder.
   For end-to-end
   model/eval gating, adapt the other reference workflows under `templates/`
   (`e2e-ci.yaml`, `e2e-nightly.yaml`, `setup-github-wif.sh`) — they are copied
   verbatim from evalchemy and need per-repo edits before use.
6. Commit the vendored `.agents/` tree and the shim.
