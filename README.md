# AI PR Review

A GitHub Action that reviews pull requests with any language model available through
[OpenRouter](https://openrouter.ai). It reads the PR diff, asks the model for a structured
review, and posts one comment covering bugs, vulnerabilities, performance, suggestions,
best practices, an overall score, and closing guidance.

Pure Python on the standard library — no `pip install` step, no Docker image, no `node_modules`.

## What it produces

The action posts (and updates in place, one comment per PR) something like:

> ## 🤖 AI Pull Request Review
>
> 🛑 **Request changes** — at least one issue should be fixed before merge.
>
> **Score:** `████░░░░░░` **42/100** · **Findings:** 3 · **Confidence:** high
>
> <details open><summary><b>🔐 Security & vulnerabilities</b> — 1 finding(s), highest 🔴 critical</summary>
>
> #### 1. SQL injection via string-concatenated token
> 🔴 critical · `app/auth.py:13` · [CWE-89](https://cwe.mitre.org/data/definitions/89.html)
> …with a `suggested_code` block you can commit in one click.
> </details>

Sections are ordered by the severity of their worst finding, so a critical vulnerability
always sits above a medium-severity bug.

## Usage

Store your OpenRouter key as the `OPENROUTER_API_KEY` repository secret, then:

```yaml
name: AI PR Review

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review, labeled, unlabeled]

permissions:
  contents: read
  pull-requests: write

concurrency:
  group: ai-pr-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: your-org/action-prrev@v1
        with:
          openrouter_api_key: ${{ secrets.OPENROUTER_API_KEY }}
          model: anthropic/claude-sonnet-4.5
```

`synchronize` fires on every push to the branch, and `labeled`/`unlabeled` let a reviewer
re-run the analysis by toggling a label. The `concurrency` block cancels a stale review when
a new commit lands, so you never pay for a review of a diff nobody will read.

### As a merge gate

By default the step always succeeds — the review is advice, not a blocker. To make it fail:

```yaml
with:
  openrouter_api_key: ${{ secrets.OPENROUTER_API_KEY }}
  fail_on_severity: high   # fail when a high or critical finding stands
  min_score: "70"          # ...or when the overall score drops below 70
```

The comment is still posted when the gate fails. Be deliberate here: models produce false
positives, and a failing required check that nobody can override becomes a merge blocker
that engineers learn to route around.

### Inline comments

`post_inline_comments: "true"` additionally attaches each finding to the line it concerns as
a review comment, using GitHub's ```` ```suggestion ```` blocks so the author can commit the
fix from the web UI. Findings whose line does not appear on the new side of the diff are
silently dropped rather than rejected wholesale by the API.

### Steering the review

`project_context` is prepended to the prompt. Use it to tell the model what you actually
care about — it changes the output more than the model choice does:

```yaml
with:
  project_context: |
    Python 3.11 service behind an authenticated gateway. We care most about input
    validation at the HTTP boundary and anything that widens the blast radius of a
    compromised worker. Do not flag missing type hints.
```

## Inputs

| Input | Default | Description |
| --- | --- | --- |
| `openrouter_api_key` | — | **Required.** OpenRouter API key. |
| `github_token` | `${{ github.token }}` | Token used to read the diff and post comments. |
| `model` | `anthropic/claude-sonnet-4.5` | Any OpenRouter model slug. |
| `fallback_models` | — | Comma-separated slugs tried in order if the primary fails. |
| `base_url` | `https://openrouter.ai/api/v1` | API base URL. |
| `temperature` | `0.1` | Sampling temperature. |
| `max_tokens` | `8000` | Max tokens per response. |
| `exclude` | lockfiles, images, `node_modules`, `dist`, … | Glob patterns to omit from the diff. |
| `max_diff_chars` | `180000` | Diff size above which chunking begins. |
| `chunk_chars` | `60000` | Approximate size of each slice of a large diff. |
| `max_chunks` | `8` | Cap on slices reviewed; excess is disclosed in the comment. |
| `skip_labels` | `no-ai-review,skip-review` | Labels that suppress the review. |
| `required_labels` | — | If set, review only runs when one of these is present. |
| `skip_draft` | `true` | Skip while the PR is a draft. |
| `post_comment` | `true` | Post/update the summary comment. |
| `post_inline_comments` | `false` | Also attach findings to the changed lines. |
| `comment_tag` | `ai-pr-review` | Marker used to find and update the previous comment. |
| `fail_on_severity` | `none` | `none`, `low`, `medium`, `high`, or `critical`. |
| `min_score` | — | Fail when the score falls below this. |
| `project_context` | — | Repository context handed to the model. |
| `language` | `English` | Language for the review prose. |

## Outputs

| Output | Description |
| --- | --- |
| `score` | Overall score, 0–100. |
| `verdict` | `approve`, `comment`, or `request_changes`. |
| `findings_count` | Total findings across all categories. |
| `highest_severity` | Highest severity observed, or `none`. |
| `comment_url` | URL of the posted comment. |
| `skipped` | `true` when gating suppressed the review. |

## Behavior worth knowing

**Large pull requests.** Files are filtered, then packed into slices of `chunk_chars`. A file
bigger than one slice is split at hunk boundaries, with its `diff --git` header repeated so the
model always knows which file it is reading. Each slice is reviewed independently; a final
synthesis pass produces the summary, score, and verdict from the merged findings. If
`max_chunks` forces part of the diff to be dropped, the comment says so rather than implying
full coverage.

**Binary and generated files** never reach the model. The comment footer reports how many were
excluded.

**Structured output.** The action requests a strict JSON schema. Models that reject
`response_format` fall back to an unconstrained request, and the response is then recovered from
raw JSON, a fenced block, or prose surrounding an object.

**Failure modes.** Transient HTTP failures retry with exponential backoff and honor `Retry-After`.
If the primary model fails, `fallback_models` are tried in order. If every model fails the step
errors with a GitHub annotation rather than a traceback.

## Forked pull requests

The `pull_request` event gives forked PRs a read-only token and **no access to secrets**, so this
action cannot run on them as configured above. That restriction is deliberate on GitHub's part.
`pull_request_target` runs with a privileged token against the base repo, which lets the review
work — but it will happily execute anything a `checkout` of the fork's head brings in. If you go
that route, do not check out the fork's code; this action only needs the API:

```yaml
on:
  pull_request_target:
    types: [opened, synchronize, reopened, labeled, unlabeled]

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: your-org/action-prrev@v1   # note: no actions/checkout of the head ref
        with:
          openrouter_api_key: ${{ secrets.OPENROUTER_API_KEY }}
```

Understand [the risks of `pull_request_target`](https://securitylab.github.com/resources/github-actions-preventing-pwn-requests/)
before enabling it.

## Privacy and cost

The diff of every reviewed PR is sent to OpenRouter and on to the model provider you selected.
Check your provider's retention policy before pointing this at a private repository, and use
`exclude` to keep sensitive paths out of the prompt. Token usage and OpenRouter's reported cost
are printed in the comment footer.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install pytest ruff
.venv/bin/pytest -q          # unit + end-to-end tests against stub API servers
.venv/bin/ruff check src tests
```

The end-to-end tests in [tests/test_e2e.py](tests/test_e2e.py) run `src/main.py` as a subprocess
against local stub GitHub and OpenRouter servers, so the real urllib paths, event parsing, output
files, and gating logic are all exercised without network access or an API key.

## Layout

| File | Purpose |
| --- | --- |
| [action.yml](action.yml) | Action metadata, inputs, outputs. |
| [src/main.py](src/main.py) | Entry point: gating, orchestration, outputs. |
| [src/config.py](src/config.py) | Input parsing and validation. |
| [src/diffparse.py](src/diffparse.py) | Unified-diff parsing, filtering, chunking. |
| [src/github_api.py](src/github_api.py) | GitHub REST client. |
| [src/openrouter.py](src/openrouter.py) | OpenRouter client and JSON recovery. |
| [src/prompts.py](src/prompts.py) | System prompt and response schema. |
| [src/review.py](src/review.py) | Model orchestration, normalization, merging. |
| [src/render.py](src/render.py) | Markdown comment and inline comment rendering. |
| [src/httpclient.py](src/httpclient.py) | urllib wrapper with retries and backoff. |

## License

MIT
