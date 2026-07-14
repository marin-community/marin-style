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

## Adding a repo

1. Add the pinned `marin-style` git dev-dependency.
2. Copy `templates/pre-commit.py` to `infra/pre-commit.py` and set `<REV>` to the
   pinned revision.
3. Add a `[tool.marin-style]` block. Tier A: list `checks`. Tier B: also add an
   `include` list scoping checks to the Marin delta.
4. Run `marin-style sync`, then add `@.agents/marin-style/AGENTS-core.md` to the
   repo's `AGENTS.md` (the sync command prints this reminder).
5. Wire CI: run `infra/pre-commit.py --all-files` and `marin-style sync --check`
   on pull requests. For end-to-end model/eval gating, adapt the reference
   workflows under `templates/` (`e2e-ci.yaml`, `e2e-nightly.yaml`,
   `setup-github-wif.sh`) — they are copied verbatim from evalchemy and need
   per-repo edits before use.
6. Commit the vendored `.agents/` tree and the shim.
