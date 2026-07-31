"""
Identifier masking (C1).

The dashboard tests in test_dashboard_masks_identifiers.py assert that the app
routes everything through these helpers. This file asserts the helpers are
worth routing through.
"""

from __future__ import annotations

import pandas as pd
import pytest

import config as cfg
from src import masking


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ENT-01344", "ENT-...1344"),
        ("CPT-00375", "CPT-...0375"),
        ("", ""),
        (None, ""),
    ],
)
def test_identifier_masking(raw, expected):
    assert masking.mask_identifier(raw) == expected


def test_event_references_are_deliberately_not_masked():
    """
    Case and transaction numbers identify an event, not a party. They are what
    an analyst quotes when escalating, so masking them would cost usability and
    protect nothing.
    """
    assert "case_id" not in masking.IDENTIFIER_COLUMNS
    frame = pd.DataFrame({"case_id": ["CASE-0023"], "entity_id": ["ENT-01344"]})
    masked = masking.mask_frame(frame)
    assert masked["case_id"].iloc[0] == "CASE-0023"
    assert masked["entity_id"].iloc[0] == "ENT-...1344"


def test_short_identifiers_are_not_padded_into_something_misleading():
    """A tail shorter than the visible window must not gain invented digits."""
    assert masking.mask_identifier("ENT-12") == "ENT-12"
    assert "..." not in masking.mask_identifier("ENT-12")


def test_masking_preserves_distinguishability():
    """
    Masking that collapses two different accounts into the same string would
    make the queue unusable -- an analyst has to be able to see that two rows
    concern the same entity.
    """
    ids = [f"ENT-{i:05d}" for i in range(2_000)]
    masked = [masking.mask_identifier(i) for i in ids]
    assert len(set(masked)) == len(set(ids)), "masking collapsed distinct identifiers"


def test_name_masking_reduces_to_initials():
    assert masking.mask_name("Ada Lovelace") == "A. L."
    assert masking.mask_name("Grace Brewster Murray Hopper") == "G. B. M."
    assert masking.mask_name("") == ""
    assert masking.mask_name(None) == ""


def test_mask_frame_masks_every_identifier_and_name_column():
    frame = pd.DataFrame(
        {
            "entity_id": ["ENT-01344", "ENT-00001"],
            "counterparty_id": ["CPT-00375", ""],
            "case_id": ["CASE-0023", ""],
            "display_name": ["Ada Lovelace", "Northwind Supplies 0042"],
            "risk_score": [88.2, 12.0],
        }
    )
    masked = masking.mask_frame(frame)

    assert masking.contains_unmasked_identifier(masked) == []
    assert masked["entity_id"].tolist() == ["ENT-...1344", "ENT-...0001"]
    assert masked["counterparty_id"].tolist() == ["CPT-...0375", ""]
    assert masked["display_name"].iloc[0] == "A. L."
    # Non-identifier columns must pass through untouched.
    assert masked["risk_score"].tolist() == [88.2, 12.0]
    # And the original must not be mutated.
    assert frame["entity_id"].iloc[0] == "ENT-01344"


def test_detector_actually_detects_unmasked_identifiers():
    """
    A guard that never fires is worse than no guard. This proves
    contains_unmasked_identifier finds what it claims to.
    """
    leaky = pd.DataFrame({"entity_id": ["ENT-01344"], "note": ["fine"]})
    assert masking.contains_unmasked_identifier(leaky) == ["entity_id"]


def test_real_scored_data_is_clean_once_masked(scored):
    sample = scored.head(500)[
        ["entity_id", "counterparty_id", "case_id", "risk_score", "amount_cad"]
    ]
    assert masking.contains_unmasked_identifier(sample), (
        "the raw scored data should contain full identifiers -- if not, this "
        "test is not proving anything"
    )
    assert masking.contains_unmasked_identifier(masking.mask_frame(sample)) == []


def test_visible_character_count_comes_from_config():
    assert cfg.MASK_VISIBLE_CHARS >= 2
    masked = masking.mask_identifier("ENT-01344", visible=2)
    assert masked == "ENT-...44"
