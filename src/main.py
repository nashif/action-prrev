# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

"""Entry point for the AI PR review action."""

from __future__ import annotations

import logging
import os
import sys

import config
import context
import diffparse
import github_api
import render
import review as review_mod
from config import SEVERITY_ORDER
from openrouter import OpenRouterClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stdout)
log = logging.getLogger("ai-pr-review")


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    delimiter = f"__EOF_{name.upper()}__"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


def write_step_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def skip(reason: str) -> None:
    log.info("Skipping review: %s", reason)
    set_output("skipped", "true")
    set_output("score", "")
    set_output("verdict", "skipped")
    set_output("findings_count", "0")
    set_output("highest_severity", "none")
    set_output("comment_url", "")
    write_step_summary(f"## AI PR Review\n\nSkipped: {reason}")
    sys.exit(0)


def gate(cfg: config.Config, pr: github_api.PullRequest) -> None:
    labels = {label.lower() for label in pr.labels}

    blocked = labels & set(cfg.skip_labels)
    if blocked:
        skip(f"pull request carries the label {sorted(blocked)[0]!r}")

    if cfg.required_labels and not labels & set(cfg.required_labels):
        skip(f"none of the required labels {cfg.required_labels} are present")

    if cfg.skip_draft and pr.draft:
        skip("pull request is a draft")


def files_summary(files: list[diffparse.FileDiff]) -> str:
    return "\n".join(f"- `{f.path}` ({f.status}, +{f.additions}/-{f.deletions})" for f in files[:200])


def enforce(cfg: config.Config, result: review_mod.Review) -> int:
    """Return the process exit code implied by the failure thresholds."""
    failures: list[str] = []

    if cfg.fail_on_severity != "none":
        threshold = SEVERITY_ORDER.index(cfg.fail_on_severity)
        offending = [f for f in result.findings if f.rank >= threshold]
        if offending:
            failures.append(
                f"{len(offending)} finding(s) at or above severity '{cfg.fail_on_severity}' "
                f"(highest: {result.highest_severity})"
            )

    if cfg.min_score is not None and result.score < cfg.min_score:
        failures.append(f"score {result.score} is below the required minimum of {cfg.min_score}")

    for failure in failures:
        log.error("Review gate failed: %s", failure)
        print(f"::error title=AI PR Review::{failure}")
    return 1 if failures else 0


def main() -> int:
    cfg = config.load()

    number = github_api.pull_request_number()
    if number is None:
        skip("no pull request found in the event payload")

    repo = github_api.repo_slug()
    gh = github_api.GitHubClient(cfg.github_token, repo)

    pr = gh.get_pull_request(number)
    log.info("Reviewing %s#%d (%s)", repo, pr.number, pr.head_sha[:7])
    gate(cfg, pr)

    raw_diff = gh.get_diff(number)
    if not raw_diff.strip():
        skip("the pull request has an empty diff")

    all_files = diffparse.parse(raw_diff)
    files, excluded = diffparse.filter_files(all_files, cfg.exclude)
    if excluded:
        log.info("Excluded %d file(s) from the review: %s", len(excluded), ", ".join(excluded[:10]))
    if not files:
        skip("every changed file is binary or matched an exclude pattern")

    total = sum(f.size for f in files)
    if total <= cfg.max_diff_chars and total <= cfg.chunk_chars:
        one = diffparse.Chunk(diff="".join(f.patch for f in files), paths=[f.path for f in files])
        chunks, dropped = [one], False
    else:
        log.info("Diff is %d chars; splitting into slices of ~%d", total, cfg.chunk_chars)
        chunks, dropped = diffparse.chunk(files, cfg.chunk_chars, cfg.max_chunks)

    client = OpenRouterClient(
        cfg.api_key,
        cfg.base_url,
        referer=f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}",
        title=f"AI PR Review ({repo})",
    )

    builder = context.ContextBuilder(gh, repo, pr.head_sha, source=cfg.context_source)
    overview = builder.repo_overview() if cfg.include_repo_overview else ""

    by_path = {f.path: f for f in files}

    def context_for(paths: list[str]) -> str:
        if not cfg.include_file_context:
            return ""
        wanted = [by_path[path] for path in paths if path in by_path]
        return builder.file_context(wanted, cfg.context_lines, cfg.max_context_chars)

    result = review_mod.run(
        client,
        cfg,
        pr,
        chunks,
        files_summary(files),
        repo=repo,
        repo_overview=overview,
        context_for=context_for,
    )
    result.truncated = dropped
    result.excluded_files = excluded

    log.info(
        "Review complete: score=%d verdict=%s findings=%d tokens=%d context=%d chars",
        result.score,
        result.verdict,
        len(result.findings),
        result.prompt_tokens + result.completion_tokens,
        result.context_chars,
    )

    comment_url = ""
    if cfg.post_comment:
        body = render.comment_body(result, cfg, pr, repo)
        comment_url = gh.upsert_comment(number, body, render.marker(cfg.comment_tag))
        log.info("Posted summary comment: %s", comment_url)

    if cfg.post_inline_comments:
        comments = render.inline_comments(result, diffparse.commentable_map(files))
        if comments and gh.create_review(number, pr.head_sha, comments):
            log.info("Posted %d inline comment(s)", len(comments))

    write_step_summary(render.step_summary(result))
    set_output("skipped", "false")
    set_output("score", str(result.score))
    set_output("verdict", result.verdict)
    set_output("findings_count", str(len(result.findings)))
    set_output("highest_severity", result.highest_severity)
    set_output("comment_url", comment_url)

    return enforce(cfg, result)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - surface a clean annotation, not a traceback wall
        log.exception("AI PR review failed")
        print(f"::error title=AI PR Review::{exc}")
        sys.exit(1)
