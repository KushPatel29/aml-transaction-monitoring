"""
Know your customer: customer risk rating, EDD triggers and beneficial ownership.

The ownership arithmetic is checked on structures small enough to work by hand;
the rating policy on single customers; the book-level results against the
committed outputs, the README and a rerun from the seed.
"""

from __future__ import annotations

import json
import re

import pandas as pd
import pytest

import config as cfg
from src import kyc


def edges(rows):
    frame = pd.DataFrame(rows, columns=["owner", "owned", "share"])
    frame["owner_type"] = (
        frame.owner.str[:3].map({"PER": "person", "HLD": "holding"}).fillna("business")
    )
    frame["owner_opaque_jurisdiction"] = frame.owner.eq("HLD-OPQ")
    return frame


def test_indirect_shares_multiply_along_the_chain():
    r = kyc.effective_ownership(
        "ENT-A",
        edges(
            [
                ("HLD-1", "ENT-A", 0.6),
                ("PER-1", "ENT-A", 0.4),
                ("PER-2", "HLD-1", 0.5),
                ("PER-3", "HLD-1", 0.5),
            ]
        ),
    )
    assert r.owners == pytest.approx({"PER-1": 0.4, "PER-2": 0.3, "PER-3": 0.3})
    assert r.max_depth == 2


def test_a_person_owning_through_two_routes_is_counted_once_with_both():
    r = kyc.effective_ownership(
        "ENT-A",
        edges(
            [
                ("PER-1", "ENT-A", 0.15),
                ("HLD-1", "ENT-A", 0.85),
                ("PER-1", "HLD-1", 0.2),
                ("PER-2", "HLD-1", 0.8),
            ]
        ),
    )
    assert r.owners["PER-1"] == pytest.approx(0.15 + 0.85 * 0.2)
    assert len(r.paths["PER-1"]) == 2


def test_the_25_percent_line_is_inclusive():
    owners, flags = kyc.beneficial_owners(
        pd.DataFrame({"entity_id": ["ENT-A"], "entity_type": ["business"]}),
        edges([("PER-1", "ENT-A", 0.25), ("PER-2", "ENT-A", 0.25), ("PER-3", "ENT-A", 0.5)]),
    )
    assert set(owners.loc[owners.is_beneficial_owner, "person_id"]) == {"PER-1", "PER-2", "PER-3"}
    assert not flags.no_25pct_owner.iloc[0]


def test_no_owner_at_25_percent_is_flagged():
    _, flags = kyc.beneficial_owners(
        pd.DataFrame({"entity_id": ["ENT-A"], "entity_type": ["business"]}),
        edges([(f"PER-{k}", "ENT-A", 0.2) for k in range(5)]),
    )
    assert flags.no_25pct_owner.iloc[0] and flags.beneficial_owners.iloc[0] == 0


def test_a_loop_ends_and_is_flagged():
    r = kyc.effective_ownership(
        "ENT-A",
        edges(
            [
                ("HLD-1", "ENT-A", 1.0),
                ("PER-1", "HLD-1", 0.9),
                ("ENT-A", "HLD-1", 0.1),
            ]
        ),
    )
    assert r.circular
    assert r.owners == pytest.approx({"PER-1": 0.9})


def test_an_opaque_holding_anywhere_in_the_chain_is_flagged():
    r = kyc.effective_ownership(
        "ENT-A", edges([("HLD-OPQ", "ENT-A", 1.0), ("PER-1", "HLD-OPQ", 1.0)])
    )
    assert r.opaque and r.owners == {"PER-1": 1.0}


@pytest.fixture(scope="module")
def risk():
    return pd.read_csv(kyc.RISK_PATH)


@pytest.fixture(scope="module")
def summary():
    return json.loads(kyc.SUMMARY_PATH.read_text(encoding="utf-8"))


def test_a_foreign_pep_is_high_risk_whatever_the_points(risk):
    peps = risk[risk.pep_status.eq("foreign_pep")]
    assert len(peps) > 0
    assert set(peps.onboarding_rating) == set(peps.refresh_rating) == {"High"}
    assert peps.edd_reasons.str.contains("foreign PEP").all()


def test_every_high_risk_customer_gets_enhanced_measures(risk):
    high = risk[risk.refresh_rating.eq("High")]
    assert high.edd_required.all()


def test_ratings_follow_the_points_and_the_bands(risk):
    for stage in ("onboarding", "refresh"):
        rated = risk[~risk.pep_status.eq("foreign_pep")]
        expect = rated[f"{stage}_points"].map(kyc._band)
        assert (expect == rated[f"{stage}_rating"]).all()


def test_the_refresh_only_adds_to_what_onboarding_knew(risk):
    assert (risk.refresh_points >= risk.onboarding_points).all()
    order = {"Low": 0, "Medium": 1, "High": 2}
    assert (risk.refresh_rating.map(order) >= risk.onboarding_rating.map(order)).all()


def test_every_rating_carries_its_reasons(risk):
    has_points = risk[risk.refresh_points > 0]
    for r in has_points.itertuples():
        stated = sum(int(p) for p in re.findall(r"\+(\d+)", r.factors))
        assert stated == r.refresh_points, r.entity_id


def test_review_cycle_follows_the_rating(risk):
    due = pd.to_datetime(risk.next_review)
    months = (due.dt.year - kyc.AS_OF.year) * 12 + (due.dt.month - kyc.AS_OF.month)
    assert (months == risk.refresh_rating.map(kyc.REVIEW_MONTHS)).all()


def test_the_kyc_files_name_no_one():
    profiles = pd.read_csv(kyc.PROFILES_PATH)
    owners = pd.read_csv(kyc.OWNERSHIP_PATH)
    assert "display_name" not in profiles.columns
    ids = set(owners.owner) | set(owners.owned)
    assert all(re.fullmatch(r"(PER-\d{5}|HLD-\d{4}|ENT-\d{5})", i) for i in ids)


def test_the_synthetic_files_regenerate_from_the_seed():
    entities = pd.read_csv(cfg.ENTITIES_PATH, keep_default_na=False)
    entities["payroll_base_cad"] = pd.to_numeric(entities.payroll_base_cad, errors="coerce")
    entities["typical_amount_cad"] = pd.to_numeric(entities.typical_amount_cad, errors="coerce")
    pd.testing.assert_frame_equal(
        kyc.make_profiles(entities).reset_index(drop=True),
        pd.read_csv(kyc.PROFILES_PATH),
        check_dtype=False,
    )
    pd.testing.assert_frame_equal(
        kyc.make_ownership(entities).reset_index(drop=True),
        pd.read_csv(kyc.OWNERSHIP_PATH),
        check_dtype=False,
    )


def test_the_summary_agrees_with_the_customer_file(risk, summary):
    assert summary["customers"] == len(risk) == 1500
    assert (
        summary["refresh"]
        == risk.refresh_rating.value_counts().reindex(["Low", "Medium", "High"]).to_dict()
    )
    assert summary["edd_required"] == int(risk.edd_required.sum())


def test_the_readme_states_the_kyc_numbers(summary):
    text = (cfg.ROOT / "README.md").read_text(encoding="utf-8")
    assert f"{summary['refresh']['High']} customers High" in text
    assert f"{summary['onboarding']['High']} at onboarding" in text
    assert (
        f"{summary['planted_high_at_onboarding']} of the {summary['planted_case_customers']} "
        f"planted-case customers"
    ) in text
    assert f"{summary['planted_high_at_refresh']} at the refresh" in text
    assert f"{summary['businesses_layered']} of {summary['businesses']} businesses" in text
