"""
Fixture-based tests for R1-R5.

Two obligations per rule: fire on a hand-built example of the pattern, and stay
silent on hand-built cases that miss it by exactly one condition. The silent
cases carry most of the weight -- a rule that quietly loses a clause still
passes its positive test.

The reason codes are tested too. A boolean an analyst cannot act on is not a
detection, and a reason string that says the wrong number is worse than none.
"""

from __future__ import annotations

import re

import pytest

import config as cfg
from src import features as feature_module, rules_engine
from tests.fixtures import hand_built_cases as fixtures


def _run(frame):
    features = feature_module.compute_features(frame)
    return rules_engine.apply_rules(frame, features)


@pytest.mark.parametrize("rule_code", sorted(fixtures.FIRES))
def test_rule_fires_on_its_own_pattern(rule_code):
    hits = _run(fixtures.FIRES[rule_code]())
    assert hits[rule_code].any(), f"{rule_code} did not fire on its positive fixture"


@pytest.mark.parametrize(
    "rule_code,description",
    [(code, desc) for code, cases in fixtures.STAYS_SILENT.items() for desc in cases],
)
def test_rule_stays_silent_on_near_misses(rule_code, description):
    frame = fixtures.STAYS_SILENT[rule_code][description]()
    hits = _run(frame)
    fired = hits[rule_code].sum()
    assert fired == 0, (
        f"{rule_code} fired on {fired} transaction(s) in the '{description}' case, "
        "which misses the pattern by exactly one condition"
    )


@pytest.mark.parametrize("rule_code", sorted(fixtures.FIRES))
def test_no_rule_fires_on_an_unremarkable_account(rule_code):
    hits = _run(fixtures.entirely_unremarkable())
    assert hits[rule_code].sum() == 0, f"{rule_code} fired on a quiet account"


@pytest.mark.parametrize("rule_code", sorted(fixtures.FIRES))
def test_rules_do_not_bleed_into_each_other(rule_code):
    """
    A structuring fixture should trip R1 and nothing else. Cross-firing means a
    rule is matching something more general than the typology it names, which
    would make the per-rule precision figures meaningless.
    """
    hits = _run(fixtures.FIRES[rule_code]())
    for other in cfg.RULE_CODES:
        if other == rule_code:
            continue
        assert hits[other].sum() == 0, (
            f"{other} also fired on the {rule_code} fixture"
        )


def test_reason_codes_are_populated_and_specific():
    hits = _run(fixtures.structuring_fires())
    fired = hits[hits["R1_STRUCTURING"]]
    assert len(fired) == 4, "R1 should flag the whole cluster, not just the last deposit"

    for reason in fired["reason_codes"]:
        assert reason.startswith("R1_STRUCTURING:")
        # The four fixture deposits total $35,600.00 across six days.
        assert "4 cash deposits" in reason
        assert "$35,600.00" in reason
        assert re.search(r"in 6\.\d days", reason), reason


def test_layering_reason_reports_the_real_passthrough():
    hits = _run(fixtures.layering_fires())
    reasons = [r for r in hits["reason_codes"] if r]
    assert reasons
    # $76,000 out of $80,000 in.
    assert all("95% passed through" in r for r in reasons)
    assert all("4 counterparties" in r for r in reasons)


def test_layering_flags_the_credit_and_every_leg():
    hits = _run(fixtures.layering_fires())
    assert hits["R2_LAYERING"].sum() == 5, (
        "R2 should flag the inbound credit and all four outbound legs, so an "
        "analyst opening the case sees the whole movement"
    )


def test_dormant_reason_reports_the_gap_and_multiple():
    hits = _run(fixtures.dormant_fires())
    reason = next(r for r in hits["reason_codes"] if r)
    assert "R5_DORMANT:" in reason
    assert "$4,000.00" in reason
    # History runs days 0-7 and the reactivation lands on day 128.
    assert "121 days" in reason
    assert "18.7x" in reason


def test_n_rules_fired_matches_the_boolean_columns():
    hits = _run(fixtures.structuring_fires())
    recomputed = hits[cfg.RULE_CODES].sum(axis=1)
    assert (hits["n_rules_fired"] == recomputed).all()
    assert ((hits["n_rules_fired"] > 0) == (hits["reason_codes"] != "")).all()


def test_rules_are_thresholds_not_generator_fingerprints():
    """
    Each rule must be strictly looser than the generator that produced the
    typology it detects. If a threshold is ever tightened to match the
    generator, precision stops being measurable and starts being assumed.
    """
    assert cfg.R1_AMOUNT_FLOOR < cfg.STRUCTURING_AMOUNT_MIN
    assert cfg.R2_MIN_INBOUND < cfg.LAYERING_LUMP_MIN
    assert cfg.R2_MIN_PASSTHROUGH < 1.0 - cfg.LAYERING_RETENTION_MAX
    assert cfg.R3_MIN_DISTINCT_SENDERS < cfg.SMURFING_MIN_SENDERS
    assert cfg.R3_MAX_AMOUNT_CV > cfg.SMURFING_AMOUNT_CV
    assert cfg.R3_MAX_MEAN_AMOUNT > cfg.SMURFING_MEAN_MAX
    assert cfg.R5_MIN_MULTIPLIER < cfg.DORMANT_MIN_MULTIPLIER
