# Copyright (c) 2026 Anas Nashif
# SPDX-License-Identifier: Apache-2.0

import pytest

import profiles
import prompts
import review as review_mod


def test_builtin_profiles_are_discoverable():
    assert profiles.available() == ["default", "zephyr"]


def test_default_profile_loads():
    text = profiles.load("default")
    assert "meticulous senior software engineer" in text


def test_zephyr_profile_loads_with_its_domain_guidance():
    text = profiles.load("zephyr")
    assert text.startswith("You are an automated pull-request reviewer for the Zephyr Project.")
    for marker in ("Devicetree", "Kconfig", "`__syscall`", "SMP assumptions", "Review discipline"):
        assert marker in text


def test_empty_value_falls_back_to_default():
    assert profiles.load("") == profiles.load("default")


def test_unknown_name_fails_loudly_and_lists_the_alternatives():
    with pytest.raises(SystemExit, match="unknown review_profile 'zepyhr'.*default, zephyr"):
        profiles.load("zepyhr")


def test_a_path_that_does_not_exist_fails_loudly():
    with pytest.raises(SystemExit, match="not found"):
        profiles.load("ci/my-profile.md")


def test_custom_profile_loads_from_a_path(tmp_path):
    custom = tmp_path / "house-style.md"
    custom.write_text("You review only for thread safety.")
    assert profiles.load(str(custom)) == "You review only for thread safety."


def test_custom_profile_resolves_against_the_workspace(tmp_path, monkeypatch):
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci" / "profile.md").write_text("Workspace profile.")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    assert profiles.load("ci/profile.md") == "Workspace profile."


def test_empty_profile_file_is_rejected(tmp_path):
    empty = tmp_path / "empty.md"
    empty.write_text("   \n")
    with pytest.raises(SystemExit, match="is empty"):
        profiles.load(str(empty))


def test_oversized_profile_file_is_rejected(tmp_path):
    big = tmp_path / "big.md"
    big.write_text("x" * (profiles.MAX_PROFILE_CHARS + 1))
    with pytest.raises(SystemExit, match="exceeds"):
        profiles.load(str(big))


# -- the contract wins -------------------------------------------------------


def test_system_prompt_is_profile_then_contract():
    system = prompts.system_prompt("English", "PROFILE BODY")
    assert system.startswith("PROFILE BODY")
    assert "# Output contract" in system
    assert system.index("PROFILE BODY") < system.index("# Output contract")


def test_contract_overrides_a_profile_that_asks_for_markdown():
    """The Zephyr profile describes prose sections; the contract must still demand JSON."""
    system = prompts.system_prompt("English", profiles.load("zephyr"))
    assert "Return exactly one JSON object" in system
    assert "overrides any formatting, section layout, or heading structure requested above" in system
    assert "never a space" in system  # `request changes` -> request_changes
    assert "express it as severity `low`" in system  # the profile's `suggestion` severity


def test_contract_carries_the_language():
    assert "Write all prose in French." in prompts.system_prompt("French", "body")


# -- vocabulary normalization ------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("suggestion", "low"),  # zephyr's fifth severity
        ("SUGGESTION", "low"),
        ("nit", "low"),
        ("minor", "low"),
        ("major", "high"),
        ("blocker", "critical"),
        ("critical", "critical"),
        ("high", "high"),
        ("what", "medium"),  # unknown falls back to medium
        ("", "medium"),
    ],
)
def test_severity_aliases_map_onto_the_four_the_gate_understands(given, expected):
    assert review_mod._normalize_severity(given) == expected


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("request changes", "request_changes"),  # zephyr writes it with a space
        ("request-changes", "request_changes"),
        ("REQUEST_CHANGES", "request_changes"),
        ("  approve  ", "approve"),
        ("comment", "comment"),
    ],
)
def test_verdict_normalization(given, expected):
    assert review_mod._normalize_verdict(given) == expected


def test_a_suggestion_severity_finding_survives_parsing():
    payload = {
        "suggestions": [
            {"title": "Use k_work", "description": "Reinvents a work queue.", "severity": "suggestion", "line": 4}
        ]
    }
    (finding,) = review_mod._findings_from(payload)
    assert finding.severity == "low"
    assert finding.category == "suggestions"
    assert finding.rank == 1  # sorts below medium, above none
