# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

import os

import pytest

import config
import main
import review as review_mod
from tests.test_review import PAYLOAD, make_config


@pytest.fixture
def env(monkeypatch):
    """A clean input environment with only the two required secrets set."""
    for key in [key for key in os.environ if key.startswith("INPUT_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("INPUT_OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("INPUT_GITHUB_TOKEN", "ghs-test")
    return monkeypatch


def test_defaults(env):
    cfg = config.load()
    assert cfg.model == "anthropic/claude-sonnet-4.5"
    assert cfg.post_comment is True
    assert cfg.post_inline_comments is False
    assert cfg.min_score is None
    assert cfg.fail_on_severity == "none"


def test_missing_api_key_is_fatal(env):
    env.delenv("INPUT_OPENROUTER_API_KEY")
    with pytest.raises(SystemExit):
        config.load()


def test_lists_accept_commas_and_newlines(env):
    env.setenv("INPUT_EXCLUDE", "**/*.lock, docs/**\n**/dist/**")
    env.setenv("INPUT_FALLBACK_MODELS", "openai/gpt-4.1, google/gemini-2.5-pro")
    cfg = config.load()
    assert cfg.exclude == ["**/*.lock", "docs/**", "**/dist/**"]
    assert cfg.fallback_models == ["openai/gpt-4.1", "google/gemini-2.5-pro"]


def test_models_puts_primary_first_and_dedupes(env):
    env.setenv("INPUT_MODEL", "a/one")
    env.setenv("INPUT_FALLBACK_MODELS", "a/one,b/two")
    assert config.load().models == ["a/one", "b/two"]


def test_labels_are_lowercased(env):
    env.setenv("INPUT_SKIP_LABELS", "No-AI-Review,WIP")
    assert config.load().skip_labels == ["no-ai-review", "wip"]


def test_bools_accept_common_spellings(env):
    env.setenv("INPUT_POST_INLINE_COMMENTS", "TRUE")
    env.setenv("INPUT_SKIP_DRAFT", "no")
    cfg = config.load()
    assert cfg.post_inline_comments is True
    assert cfg.skip_draft is False


def test_invalid_severity_and_score_are_rejected(env):
    env.setenv("INPUT_FAIL_ON_SEVERITY", "catastrophic")
    with pytest.raises(SystemExit):
        config.load()
    env.setenv("INPUT_FAIL_ON_SEVERITY", "high")
    env.setenv("INPUT_MIN_SCORE", "eighty")
    with pytest.raises(SystemExit):
        config.load()


# -- gating -----------------------------------------------------------------


def _review():
    return review_mod.Review(score=42, findings=review_mod._findings_from(PAYLOAD))


def test_enforce_passes_when_no_thresholds_are_set():
    assert main.enforce(make_config(), _review()) == 0


def test_enforce_fails_on_severity_threshold():
    assert main.enforce(make_config(fail_on_severity="critical"), _review()) == 1
    assert main.enforce(make_config(fail_on_severity="high"), _review()) == 1


def test_enforce_ignores_findings_below_the_threshold():
    clean = review_mod.Review(score=90, findings=[f for f in _review().findings if f.severity == "low"])
    assert main.enforce(make_config(fail_on_severity="high"), clean) == 0


def test_enforce_fails_below_min_score():
    assert main.enforce(make_config(min_score=70), _review()) == 1
    assert main.enforce(make_config(min_score=40), _review()) == 0
