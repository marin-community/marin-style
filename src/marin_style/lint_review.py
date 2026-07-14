# Copyright The Marin Authors
# SPDX-License-Identifier: Apache-2.0

"""Agentic lint-review runner (lanes + composer) invoked by `marin-precommit --review`.

Fans out one headless agent per "lane" (rule files in the packaged `lint/` catalog)
over the branch's changes and merges the per-lane findings with a composer agent (or a
deterministic dedupe-and-concat). Each lane is handed the changed-file inventory
(`git diff --stat`) and read-only git access, and probes each file itself rather than
reading a pasted diff — so it can follow the change into other files and skip
binary/oversized files on its own.
"""

import os
import pathlib
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from importlib import resources

import click

from marin_style import complexity as complexity_leads

LINT_REVIEW_AGENT_DEFAULT = "claude -p"

LINT_REVIEW_TIMEOUT = 600

# The lint review runs fully-headless agents over the working tree. They are REVIEWERS:
# the only job is to READ the change and emit advisory findings on stdout — never to modify
# a file or touch git/PR state. That contract is enforced two ways: the prompt mandate
# (READ_ONLY_MANDATE, stated to every agent) and, for `claude`, a hard tool lockdown
# (_readonly_agent_flags). Claude-specific flags are appended only when the binary is `claude`.

# Built-in tools the headless `claude` agent may have AT ALL. Edit/Write/NotebookEdit are
# absent, so the agent cannot modify a file regardless of what any inherited settings.json
# grants — a tool that does not exist cannot be permitted.
LINT_REVIEW_BUILTIN_TOOLS = "Bash,Read,Grep,Glob"

# Read-only git subcommands the lanes pre-approve to probe the change themselves (we hand
# them a `git diff --stat`, not a pasted diff). Pre-approved via `--allowedTools`.
LINT_REVIEW_READONLY_GIT = (
    "git status",
    "git diff",
    "git show",
    "git log",
    "git ls-files",
    "git rev-parse",
    "git merge-base",
    "git cat-file",
    "git blame",
    "git grep",
)

# State-changing git/gh commands explicitly DENIED via `--disallowedTools`. `deny` beats
# `allow` at every scope, so this holds even if the developer's own settings.json broadly
# allows `Bash(git:*)` or `Bash(gh:*)`.
LINT_REVIEW_DENIED_COMMANDS = (
    "git add",
    "git commit",
    "git push",
    "git pull",
    "git fetch",
    "git reset",
    "git rebase",
    "git checkout",
    "git switch",
    "git merge",
    "git restore",
    "git stash",
    "git tag",
    "git clean",
    "git apply",
    "git am",
    "git rm",
    "git mv",
    "git cherry-pick",
    "git revert",
    "git update-ref",
    "git config",
    "git remote",
    "git branch",
    "git worktree",
    "gh",
)

# Raw per-arm and combined output from each review run is written under here for debugging a
# slow or broken lint cycle; the run's directory is printed at the end of the review.
LINT_REVIEW_LOG_ROOT = pathlib.Path("/tmp/marin-style-lint")


# Prepended to every lane and composer prompt. The `claude` tool lockdown already makes
# mutation impossible; this states the same contract in plain language for every agent CLI
# (e.g. `codex exec`, which manages its own permissions) and orients the model on its job.
READ_ONLY_MANDATE = (
    "## Your role: READ-ONLY reviewer\n\n"
    "You are a code REVIEWER running non-interactively. Your ONLY output is advisory lint "
    "findings on stdout, in the Output format defined below. You are inspecting someone "
    "else's in-progress branch and have NO mandate to change it. You MUST NOT, under any "
    "circumstances:\n"
    "- edit, create, move, or delete any file;\n"
    "- run any state-changing git command (add, commit, push, pull, fetch, reset, rebase, "
    "checkout, switch, merge, restore, stash, tag, branch, worktree, config, …) — use git "
    "ONLY to READ the change (diff, show, log, status, ls-files, rev-parse, merge-base, "
    "cat-file, blame, grep);\n"
    "- run `gh`, open or comment on a pull request, or take any action beyond reading code;\n"
    "- try to 'fix' anything. A fix is a finding to report, never an edit to make.\n\n"
    "Committing, pushing, or opening a PR on the author's behalf is a serious error, not "
    "helpfulness. If you cannot complete the review read-only, emit nothing and stop."
)

LINT_LANE_INSTRUCTIONS = (
    "You are ONE lane of the review. Apply ONLY the rules in the lane catalog above "
    'to the change described below. Follow the shared "Detector usage" and "Output format" '
    "exactly: emit one finding per line in the format it specifies, and emit nothing at "
    "all when there are no findings. Resolve overlap precedence within your lane; leave "
    "cross-lane duplicates to the composer. You are handed the changed-file inventory, not a "
    "pasted diff: inspect each file yourself with read-only git and Read (see 'The change' below)."
)

# The meta lane is holistic: it reasons over the whole change, may read beyond the diff,
# and owns only its own (meta) codes. It replaces the per-hunk framing above.
META_LANE_INSTRUCTIONS = (
    "You are the META lane — the holistic reviewer. Unlike the other lanes, which scan "
    "added/modified hunks for a known local shape, you reason over the change as a UNIT: "
    "model what this PR is trying to do, then judge whether the means are the cleanest path "
    "to that end. Apply ONLY the meta rules in the catalog above.\n\n"
    "Diff scope is WIDE, rule scope is NARROW. You MAY read beyond the diff — open the whole "
    "file, follow the call graph, check a sibling module or an existing helper, grep the tree "
    "for a symbol — to confirm a finding (several rules require it). But you OWN only the meta "
    "ml- codes: if you notice a local smell (a bad name, one overloaded function, a swallowed "
    "exception), stay SILENT and let its lane catch it. Fire only where a hunk-scoped pass "
    "structurally CANNOT see the problem — it spans files, lives in an unchanged file, or is a "
    "property of the whole change. If a single hunk would let a local lane flag it, defer.\n\n"
    "Precision is the whole game. Honor each rule's confidence floor and its suppressors; where "
    "a rule says so, phrase the finding as a question to confirm rather than an assertion. "
    'Follow the shared "Output format" exactly: one finding per line, nothing at all when there '
    "are no findings. You are handed only the changed-file inventory, not a pasted diff — pulling "
    "each file's diff and reading into the surrounding code is exactly this lane's job."
)

# The holistic meta lane only runs on larger diffs — its rules need the whole change, and on a
# small PR there is no aggregate shape to see.
META_LANE_MIN_DIFF_LINES = 100


@dataclass(frozen=True)
class LintLane:
    """One fan-out lane of the lint review: a rule file in the packaged lint catalog."""

    name: str
    include_complexity_leads: bool
    # Instruction block appended after the lane catalog; the holistic meta lane overrides the
    # default per-hunk framing with its own wide-diff-scope framing.
    instructions: str = LINT_LANE_INSTRUCTIONS
    # Run this lane only when the diff has more than this many changed lines (0 = always run).
    min_diff_lines: int = 0


# Coarse lanes — one headless agent each. Keep this aligned with the packaged lint/*.md files.
LINT_LANES = (
    LintLane("complexity", True),
    LintLane("interfaces", False),
    LintLane("robustness", False),
    LintLane("cruft", False),
    LintLane("prose", False),
    LintLane("meta", False, META_LANE_INSTRUCTIONS, META_LANE_MIN_DIFF_LINES),
)

# The composer merges the lanes' outputs. Authored to never silently drop a real
# finding — it may only collapse true duplicates and drop overlap-precedence losers.
COMPOSER_INSTRUCTIONS = (
    "You are the COMPOSER. Several specialist lanes each scanned the SAME branch diff against "
    "their slice of the catalog and emitted findings in the Output format above — including one "
    "holistic 'meta' lane that reasons over the whole change rather than single hunks, so its "
    "findings anchor on different lines than a local finding for the same underlying issue. "
    "Their labelled raw outputs and the changed-file inventory follow below. "
    "You are an EDITOR, not a reviewer: merge them into the single final findings list, reasoned "
    "— not a blind concat. You do NOT re-scan for new issues or invent findings. Use read-only "
    "git and Read only to adjudicate a duplicate/precedence call or sanity-check that a cited "
    "line exists.\n\n"
    "PRIME DIRECTIVE — KEEP EVERY DISTINCT FINDING. Default to KEEP. Trust the lanes: a finding "
    "survives even if you would not have raised it, even if its confidence is low, even if its "
    'rule isn\'t "yours." The Self-evaluation / "when uncertain, suppress" guidance above governs '
    "the LANES, NOT you — never drop, soften, or re-judge a finding's substance, and never trim "
    "for brevity. Silently losing a real finding is your one unforgivable error.\n\n"
    "The ONLY findings you may remove:\n"
    "1. DUPLICATES — two findings sharing (path, line, underlying issue): same path:line describing "
    "the same concrete defect, even across lanes, different ml- codes, or different wording. "
    "Collapse to ONE; keep the higher-confidence finding's code and message (tie → crisper "
    "message), use the max confidence. You may lightly tighten the kept message but must never "
    "weaken or narrow its claim. Keep it ≤200 chars.\n"
    "2. PRECEDENCE LOSERS — two findings on the SAME line that conflict and the Overlap precedence "
    "section above names a winner (more-specific rule wins). Emit the winner alone; drop the loser "
    "(do not merge).\n\n"
    "NOT removable — keep BOTH: same line but two genuinely different defects; same code on "
    "different lines; compatible claims worded differently. When unsure whether two findings are "
    "the same issue, they are NOT — keep both. Different path:line is always distinct.\n\n"
    "Reason privately — cluster by path then ascending line, apply precedence, decide each "
    "near-collision — but this reasoning must NEVER appear in the output.\n\n"
    "Output ONLY the canonical lines from the Output format above — "
    "`<path>:<line>: <code> (<confidence>) <message>`, two-decimal confidence, message ≤200 chars "
    "— grouped by file, ascending line, no blank lines. No preamble, summary, counts, reasoning, "
    "markdown, or fences; a regex parser consumes this. Your first character is the first finding's, "
    "or the output is empty. If zero findings survive, emit nothing."
)

# Env vars that mark a Claude Code session and would re-bind the spawned headless
# agent to its parent's transcript / session. Stripped before exec so the
# sub-agent runs as a fresh, isolated session.
LINT_REVIEW_STRIPPED_ENV = (
    "ANTHROPIC_API_KEY",  # force subscription auth, not metered API billing
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SSE_PORT",
)


# Output format the agent emits, per the catalog's shared.md "Output format":
#   <path>:<line>: <code> (<confidence>) <message>
_FINDING_RE = re.compile(r"^(?P<path>[^:\s]+):(?P<line>\d+): (?P<code>ml-[\w-]+) \((?P<conf>[\d.]+)\) (?P<msg>.*)$")


def _catalog_text(name: str) -> str:
    """Read a catalog file (e.g. `shared.md`, `complexity.md`) from packaged lint data."""
    return resources.files("marin_style").joinpath("lint", name).read_text(encoding="utf-8")


def _parse_findings(stdout: str) -> list[list]:
    rows: list[list] = []
    for line in stdout.splitlines():
        m = _FINDING_RE.match(line.strip())
        if not m:
            continue
        try:
            line_no = int(m["line"])
            conf = float(m["conf"])
        except ValueError:
            continue
        rows.append([m["path"], line_no, m["code"], conf, m["msg"][:200]])
    return rows


def _base_ref(root: pathlib.Path, main_branch: str) -> str | None:
    """Resolve the branch base ref: prefer `origin/<main_branch>`, fall back to the local branch."""
    for ref in (f"origin/{main_branch}", main_branch):
        r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=root, capture_output=True, text=True)
        if r.returncode == 0:
            return ref
    return None


def _diff_numstat(root: pathlib.Path, merge_base: str) -> tuple[int, int, int]:
    """`(files, added, removed)` for the branch vs `merge_base`, from `git diff --numstat`.

    Binary files (numstat renders their counts as `-`) count as one changed file with
    zero line deltas. Used for the meta lane's diff-size gate and the run summary.
    """
    out = subprocess.run(
        ["git", "diff", "--numstat", merge_base],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    files = added = removed = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        files += 1
        if parts[0].isdigit():
            added += int(parts[0])
        if parts[1].isdigit():
            removed += int(parts[1])
    return files, added, removed


def _git(root: pathlib.Path, args: list[str]) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=2)
        return r.stdout.strip() or None
    except Exception:
        return None


@dataclass(frozen=True)
class LaneResult:
    name: str
    stdout: str
    stderr: str
    returncode: int | None  # None means the lane agent timed out
    elapsed: float  # wall-clock seconds the lane's agent ran
    prompt: str  # the exact prompt fed to the lane (logged for debugging)


@dataclass(frozen=True)
class ComposerRun:
    """The composer agent's raw run, captured for logging."""

    prompt: str
    stdout: str
    stderr: str
    returncode: int | None  # None means the composer timed out
    elapsed: float


@dataclass(frozen=True)
class MergeOutcome:
    """Result of merging the lanes: the final findings plus how they were produced."""

    findings_text: str
    mode: str
    composer_rc: int
    timed_out: bool
    composer: ComposerRun | None  # None when lanes were concatenated (no composer ran)


@dataclass(frozen=True)
class ReviewRun:
    """One review run's facts, gathered for logging (the summary and per-arm dumps)."""

    lane_results: list[LaneResult]
    outcome: MergeOutcome
    merge_base: str
    diff_stats: tuple[int, int, int]
    elapsed_total: float
    branch: str | None


def _lint_review_env() -> dict[str, str]:
    # Strip session/auth markers so the headless agent runs as a fresh session
    # rather than nesting under the calling agent's transcript or being billed
    # via metered API auth.
    return {k: v for k, v in os.environ.items() if k not in LINT_REVIEW_STRIPPED_ENV}


def _readonly_agent_flags() -> list[str]:
    """Flags that lock a headless `claude` agent to read-only review.

    Three layers: `--tools` removes the edit tools entirely (no Edit/Write/NotebookEdit),
    `--allowedTools` pre-approves only read-only git plus Read/Grep/Glob, and
    `--disallowedTools` hard-denies every mutating git/gh command so an over-permissive
    inherited settings.json (`Bash(git:*)`, `Bash(gh:*)`, …) cannot re-open the hole —
    `deny` wins over `allow` at every scope.
    """
    allow = ["Read", "Grep", "Glob", *(f"Bash({c}:*)" for c in LINT_REVIEW_READONLY_GIT)]
    deny = [f"Bash({c}:*)" for c in LINT_REVIEW_DENIED_COMMANDS]
    return [
        "--tools",
        LINT_REVIEW_BUILTIN_TOOLS,
        "--allowedTools",
        ",".join(allow),
        "--disallowedTools",
        ",".join(deny),
    ]


def _with_readonly_access(agent_cmd: list[str]) -> list[str]:
    """Lock a headless `claude` agent to read-only review (see `_readonly_agent_flags`): it
    can probe the changed files (read-only git + Read/Grep/Glob) but cannot edit a file or
    run any state-changing git/gh command.

    No-op for a non-`claude` agent or when `--allowedTools` is already set: the flags are
    Claude Code's, and other agents (e.g. `codex exec`) manage their own permissions.
    """
    if os.path.basename(agent_cmd[0]) != "claude" or "--allowedTools" in agent_cmd:
        return agent_cmd
    return [*agent_cmd, *_readonly_agent_flags()]


def _run_agent(
    agent_cmd: list[str], root: pathlib.Path, env: dict[str, str], prompt: str
) -> subprocess.CompletedProcess | None:
    """Run one headless agent over `prompt`; None if it times out."""
    try:
        return subprocess.run(
            agent_cmd,
            input=prompt,
            cwd=root,
            capture_output=True,
            text=True,
            env=env,
            timeout=LINT_REVIEW_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None


def _timed_agent(
    agent_cmd: list[str], root: pathlib.Path, env: dict[str, str], prompt: str
) -> tuple[subprocess.CompletedProcess | None, float]:
    """`_run_agent` plus the wall-clock seconds it took (measured inside the worker thread)."""
    start = time.time()
    cp = _run_agent(agent_cmd, root, env, prompt)
    return cp, time.time() - start


def _changed_py_files(root: pathlib.Path, merge_base: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", merge_base, "--name-only", "--", "*.py"],
        cwd=root,
        capture_output=True,
        text=True,
    ).stdout
    return [p for p in out.splitlines() if p.strip()]


def _worktree_reader(root: pathlib.Path) -> Callable[[str], str | None]:
    def read(rel_path: str) -> str | None:
        try:
            return (root / rel_path).read_text(encoding="utf-8")
        except OSError:
            return None

    return read


def _change_context(merge_base: str, stat: str) -> str:
    """The concrete change handed to each lane/composer: the merge-base SHA and the
    `git diff --stat` inventory. How to inspect it — probe per file, what to skip — lives
    in the catalog's "Inputs" section, which is in the same prompt, so it is not repeated here.
    """
    return (
        "## The change\n\n"
        f"Merge base: `{merge_base}`. Inspect each changed file below per the catalog's "
        '"Inputs" section (`git diff` or `Read` it; skip what it says to skip).\n\n'
        f"```\n{stat}\n```"
    )


def _lane_prompt(shared_text: str, lane: LintLane, merge_base: str, stat: str, leads: str) -> str:
    parts = [READ_ONLY_MANDATE, shared_text, _catalog_text(f"{lane.name}.md")]
    if lane.include_complexity_leads and leads:
        parts.append(leads)
    parts.append(lane.instructions)
    parts.append(_change_context(merge_base, stat))
    return "\n\n".join(parts) + "\n"


def _run_lanes(
    lanes: list[LintLane],
    shared_text: str,
    merge_base: str,
    stat: str,
    leads: str,
    agent_cmd: list[str],
    root: pathlib.Path,
    env: dict[str, str],
) -> list[LaneResult]:
    prompts = {lane.name: _lane_prompt(shared_text, lane, merge_base, stat, leads) for lane in lanes}
    results: dict[str, LaneResult] = {}
    with ThreadPoolExecutor(max_workers=len(lanes)) as pool:
        futures = {pool.submit(_timed_agent, agent_cmd, root, env, prompts[lane.name]): lane for lane in lanes}
        for future in as_completed(futures):
            lane = futures[future]
            cp, elapsed = future.result()
            prompt = prompts[lane.name]
            if cp is None:
                results[lane.name] = LaneResult(lane.name, "", "", None, elapsed, prompt)
            else:
                results[lane.name] = LaneResult(
                    lane.name, cp.stdout.strip(), cp.stderr.strip(), cp.returncode, elapsed, prompt
                )
    return [results[lane.name] for lane in lanes]


def _lane_body(result: LaneResult) -> str:
    if result.returncode is None:
        return "(lane timed out — no findings)"
    if result.returncode != 0:
        return "(lane errored — no findings)"
    return result.stdout or "(no findings)"


def _composer_prompt(lane_results: list[LaneResult], shared_text: str, merge_base: str, stat: str) -> str:
    labelled = "\n\n".join(f"=== Lane: {r.name} ===\n{_lane_body(r)}" for r in lane_results)
    parts = [READ_ONLY_MANDATE, shared_text, COMPOSER_INSTRUCTIONS, labelled, _change_context(merge_base, stat)]
    return "\n\n".join(parts) + "\n"


def _compose(
    lane_results: list[LaneResult],
    shared_text: str,
    merge_base: str,
    stat: str,
    agent_cmd: list[str],
    root: pathlib.Path,
    env: dict[str, str],
) -> ComposerRun:
    prompt = _composer_prompt(lane_results, shared_text, merge_base, stat)
    cp, elapsed = _timed_agent(agent_cmd, root, env, prompt)
    if cp is None:
        return ComposerRun(prompt, "", "", None, elapsed)
    return ComposerRun(prompt, cp.stdout.strip(), cp.stderr.strip(), cp.returncode, elapsed)


def _concat_findings(lane_results: list[LaneResult]) -> str:
    """Deterministic merge: dedupe by (path, line, code), keep max confidence, sort."""
    best: dict[tuple, list] = {}
    for r in lane_results:
        for row in _parse_findings(r.stdout):
            key = (row[0], row[1], row[2])
            if key not in best or row[3] > best[key][3]:
                best[key] = row
    ordered = sorted(best.values(), key=lambda x: (x[0], x[1], x[2]))
    return "\n".join(f"{p}:{ln}: {code} ({conf:.2f}) {msg}" for p, ln, code, conf, msg in ordered)


def _resolve_review_stat(root: pathlib.Path, main_branch: str) -> tuple[str, str] | None:
    """Resolve the merge-base with the main branch and a `git diff --stat` of the branch.

    Returns `(merge_base, stat)`, or None (after echoing the reason) when the
    merge-base can't be resolved or the branch has no changes — both advisory
    no-ops for the caller. Lanes get this changed-file inventory (every file, any
    language), not a pasted diff, and probe each file themselves.
    """
    base_ref = _base_ref(root, main_branch)
    if base_ref is None:
        click.echo(f"  ⚠ Lint review skipped: could not resolve '{main_branch}' or 'origin/{main_branch}'")
        click.echo(f"    (run `git fetch origin {main_branch}` first)")
        return None
    base = subprocess.run(["git", "merge-base", base_ref, "HEAD"], cwd=root, capture_output=True, text=True)
    if base.returncode != 0:
        click.echo(f"  ⚠ Lint review skipped: could not resolve merge-base with {base_ref}")
        click.echo(f"    (git said: {base.stderr.strip()})")
        return None
    merge_base = base.stdout.strip()
    # Stat the working tree against the merge-base: covers all branch work, committed and
    # uncommitted, so the review runs whether or not the author has committed.
    stat = subprocess.run(
        ["git", "diff", "--stat", merge_base],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if not stat.strip():
        click.echo("Lint review: no changes on this branch.")
        return None
    return merge_base, stat


def _merge_lane_results(
    lane_results: list[LaneResult],
    lanes: list[LintLane],
    shared_text: str,
    merge_base: str,
    stat: str,
    agent_cmd: list[str],
    root: pathlib.Path,
    env: dict[str, str],
    compose: bool,
) -> MergeOutcome:
    """Merge lane outputs via the composer (default) or a deterministic concat."""
    timed_out = any(r.returncode is None for r in lane_results)
    if compose and len(lanes) > 1:
        run = _compose(lane_results, shared_text, merge_base, stat, agent_cmd, root, env)
        if run.returncode is None:
            click.echo("  ⚠ Lint composer timed out; falling back to concat")
            return MergeOutcome(_concat_findings(lane_results), "compose", -1, True, run)
        if run.returncode != 0:
            click.echo(f"  ⚠ Lint composer exited {run.returncode}; falling back to concat")
            return MergeOutcome(_concat_findings(lane_results), "compose", run.returncode, timed_out, run)
        return MergeOutcome(run.stdout, "compose", 0, timed_out, run)
    mode = "concat" if len(lanes) > 1 else f"lane:{lanes[0].name}"
    return MergeOutcome(_concat_findings(lane_results), mode, 0, timed_out, None)


def _write_arm_log(
    arm_dir: pathlib.Path, prompt: str, stdout: str, stderr: str, returncode: int | None, elapsed: float
) -> None:
    """Write one arm's exact prompt and raw output (stdout/stderr/status) under `arm_dir`."""
    arm_dir.mkdir(parents=True, exist_ok=True)
    (arm_dir / "prompt.md").write_text(prompt)
    status = "timed out" if returncode is None else f"exit {returncode}"
    body = (
        f"# {arm_dir.name} — lint review arm\n\n"
        f"- status: {status}\n"
        f"- elapsed: {elapsed:.2f}s\n\n"
        f"## stdout\n\n{stdout}\n\n"
        f"## stderr\n\n{stderr}\n"
    )
    (arm_dir / "output.md").write_text(body)


def _summary_md(log_dir: pathlib.Path, run: ReviewRun) -> str:
    files, added, removed = run.diff_stats
    outcome = run.outcome
    n_findings = len(_parse_findings(outcome.findings_text)) if outcome.findings_text else 0
    lines = [
        f"# marin-style lint review — {log_dir.name}",
        "",
        f"- log dir: `{log_dir}`",
        f"- branch: `{run.branch or '?'}`",
        f"- merge base: `{run.merge_base}`",
        f"- diff: {files} files, +{added} -{removed}",
        f"- merge mode: {outcome.mode}",
        f"- composer exit: {outcome.composer_rc}",
        f"- timed out: {str(outcome.timed_out).lower()}",
        f"- total elapsed: {run.elapsed_total:.2f}s",
        f"- findings: {n_findings} (see `combined.md`)",
        "",
        "## Arms",
        "",
        "| arm | status | elapsed | stdout lines |",
        "| --- | --- | --- | --- |",
    ]
    arms: list[tuple[str, int | None, float, str]] = [
        (r.name, r.returncode, r.elapsed, r.stdout) for r in run.lane_results
    ]
    if outcome.composer is not None:
        c = outcome.composer
        arms.append(("composer", c.returncode, c.elapsed, c.stdout))
    for name, rc, elapsed, stdout in arms:
        status = "timed out" if rc is None else f"exit {rc}"
        lines.append(f"| {name} | {status} | {elapsed:.2f}s | {len(stdout.splitlines())} |")
    return "\n".join(lines) + "\n"


def _review_log_dir(branch: str | None, started: float) -> pathlib.Path:
    """Create and return this run's log directory.

    Runs are namespaced by branch so concurrent worktrees on one host don't interleave,
    and the leaf is `mkdtemp`-unique so two runs starting in the same second don't
    clobber each other: `/tmp/marin-style-lint/<sanitized-branch>/<timestamp>-<uniq>/`.
    """
    branch_root = LINT_REVIEW_LOG_ROOT / re.sub(r"[^\w.-]+", "-", branch or "detached")
    branch_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(started))
    return pathlib.Path(tempfile.mkdtemp(prefix=f"{stamp}-", dir=branch_root))


def _write_review_log(log_dir: pathlib.Path, run: ReviewRun) -> None:
    """Persist the raw per-arm prompts/outputs, the combined findings, and a run summary.

    Layout under `log_dir` (one directory per review run):
        <arm>/prompt.md, <arm>/output.md  — one per lane plus `composer/`
        combined.md                       — the final merged findings (what is printed)
        summary.md                        — run metadata and a per-arm status/timing table
    """
    for r in run.lane_results:
        _write_arm_log(log_dir / r.name, r.prompt, r.stdout, r.stderr, r.returncode, r.elapsed)
    if run.outcome.composer is not None:
        c = run.outcome.composer
        _write_arm_log(log_dir / "composer", c.prompt, c.stdout, c.stderr, c.returncode, c.elapsed)
    (log_dir / "combined.md").write_text((run.outcome.findings_text or "") + "\n")
    (log_dir / "summary.md").write_text(_summary_md(log_dir, run))


def run_lint_review(
    agent_command: str,
    root: pathlib.Path,
    main_branch: str = "main",
    lane_names: list[str] | None = None,
    compose: bool = True,
) -> int:
    """Run the advisory lint catalog over the branch's changes via headless agents.

    Fans out one agent per lane (see `LINT_LANES`); each lane is handed the changed-file
    inventory (`git diff --stat`) plus read-only git access and probes the files itself.
    The complexity lane is also fed static-complexity leads. Their outputs are merged by a
    composer agent (`compose=True`) or a deterministic dedupe-and-concat (`compose=False`).
    `lane_names` restricts the run to a subset of lanes for debugging.

    `agent_command` is the headless CLI invocation each lane/composer agent reads its
    prompt from on stdin (e.g. `claude -p`, `codex exec`). `root` is the repository root
    and `main_branch` names the branch the diff is scoped against.

    Findings are advisory and never block. Returns 0 for every outcome that fits that
    contract (no findings, findings emitted, agent unavailable, merge-base unresolved,
    lane/composer timeout). Returns 1 only on a usage error (unknown lane) or when every
    lane's agent failed to run, which indicates a broken agent CLI worth surfacing.
    """
    lanes = list(LINT_LANES)
    if lane_names:
        known = {lane.name for lane in LINT_LANES}
        unknown = [n for n in lane_names if n not in known]
        if unknown:
            click.echo(f"Error: unknown lint lane(s): {', '.join(unknown)}. Valid: {', '.join(sorted(known))}", err=True)
            return 1
        lanes = [lane for lane in LINT_LANES if lane.name in lane_names]

    agent_cmd = shlex.split(agent_command)
    if not agent_cmd or shutil.which(agent_cmd[0]) is None:
        agent_name = agent_cmd[0] if agent_cmd else "(empty)"
        click.echo(f"  ⚠ Lint review skipped: agent '{agent_name}' not found on PATH")
        return 0
    agent_cmd = _with_readonly_access(agent_cmd)

    resolved = _resolve_review_stat(root, main_branch)
    if resolved is None:
        return 0
    merge_base, stat = resolved

    # Drop lanes whose diff-size floor the change doesn't clear (only the holistic meta lane
    # sets one). Do this before computing leads / running agents so neither pays for a lane
    # that will not run.
    diff_stats = _diff_numstat(root, merge_base)
    changed_lines = diff_stats[1] + diff_stats[2]
    runnable = []
    for lane in lanes:
        if changed_lines > lane.min_diff_lines:
            runnable.append(lane)
        else:
            click.echo(
                f"  Lint review: '{lane.name}' lane skipped (diff {changed_lines} ≤ {lane.min_diff_lines}-line floor)"
            )
    lanes = runnable
    if not lanes:
        return 0

    shared_text = _catalog_text("shared.md")
    leads = ""
    if any(lane.include_complexity_leads for lane in lanes):
        leads = complexity_leads.compute_leads(_worktree_reader(root), _changed_py_files(root, merge_base))
    env = _lint_review_env()
    started = time.time()

    lane_results = _run_lanes(lanes, shared_text, merge_base, stat, leads, agent_cmd, root, env)
    for r in lane_results:
        if r.returncode is None:
            click.echo(f"  ⚠ Lint lane '{r.name}' timed out after {LINT_REVIEW_TIMEOUT}s")
        elif r.returncode != 0:
            detail = r.stderr.splitlines()[0] if r.stderr else ""
            click.echo(f"  ⚠ Lint lane '{r.name}' exited {r.returncode}: {detail}")

    outcome = _merge_lane_results(lane_results, lanes, shared_text, merge_base, stat, agent_cmd, root, env, compose)

    # Persist raw per-arm + combined output for debugging a slow/broken cycle. Log I/O is a
    # side channel: a write failure (e.g. /tmp not writable) must not fail the advisory review.
    branch = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    run = ReviewRun(lane_results, outcome, merge_base, diff_stats, time.time() - started, branch)
    try:
        log_dir = _review_log_dir(branch, started)
        _write_review_log(log_dir, run)
        click.echo(f"  Lint review logs: {log_dir}")
    except OSError as e:
        click.echo(f"  ⚠ Lint review: could not write logs under {LINT_REVIEW_LOG_ROOT}: {e}")

    if all(r.returncode != 0 for r in lane_results):
        click.echo("  ⚠ Lint review: every lane failed to run (is the agent CLI working?)")
        return 1

    if not outcome.findings_text:
        click.echo("Lint review: no findings.")
        return 0

    click.echo("Lint review findings (advisory — search the marin-style lint catalog for each ml-... code):\n")
    click.echo(outcome.findings_text)
    return 0
