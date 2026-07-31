"""
C1/C10: nothing in the committed data should look like a real identifier or a
real institution.

The data is Faker-generated, so this is not defending against a leak -- it is
defending against the thing that actually goes wrong in projects like this,
which is a generator quietly emitting something that pattern-matches a real
national ID or account number and a reviewer noticing before you do.

Every committed CSV is scanned, including the scored artefact the dashboard
reads, because that one is assembled by a different code path from the
generator and is the one most likely to reintroduce something.
"""

from __future__ import annotations

import re

import pandas as pd
import pytest

import config as cfg

# A Canadian social insurance number is nine digits, usually written in three
# groups. Anything matching this shape has no business being in the data.
SIN_PATTERN = re.compile(r"\b\d{3}[\s-]?\d{3}[\s-]?\d{3}\b")

# Bank-account-shaped: a long unbroken digit run.
LONG_DIGIT_RUN = re.compile(r"\b\d{9,}\b")

# Payment-card-shaped, checked with Luhn below.
CARD_CANDIDATE = re.compile(r"\b\d{13,19}\b")

# Words that would imply a real financial institution or a real filing (C10).
FORBIDDEN_TERMS = [
    "fintrac", "cbsa", "cra ", "revenue agency", "rcmp", "osfi",
    "royal bank", "scotiabank", "td bank", "cibc", "bmo", "desjardins",
    "wells fargo", "hsbc", "citibank", "jpmorgan", "barclays",
    "suspicious transaction report", "str filing", "sar filing",
]

COMMITTED_FILES = [
    cfg.ENTITIES_PATH,
    cfg.COUNTERPARTIES_PATH,
    cfg.TRANSACTIONS_PATH,
    cfg.CASES_PATH,
    cfg.SCORED_PATH,
]


def _luhn_valid(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _text_columns(frame: pd.DataFrame) -> list[str]:
    return [c for c in frame.columns if frame[c].dtype == object]


@pytest.fixture(scope="module", params=[p.name for p in COMMITTED_FILES])
def committed_frame(request):
    path = next(p for p in COMMITTED_FILES if p.name == request.param)
    if not path.exists():
        pytest.skip(f"{path.name} missing -- run `python run_pipeline.py`")
    return path.name, pd.read_csv(path, keep_default_na=False)


def test_no_national_identifier_shapes(committed_frame):
    name, frame = committed_frame
    for column in _text_columns(frame):
        values = frame[column].astype(str)
        hits = values[values.str.contains(SIN_PATTERN, regex=True, na=False)]
        assert hits.empty, (
            f"{name}.{column} contains {len(hits)} value(s) shaped like a "
            f"national identifier, e.g. {hits.iloc[0]!r}"
        )


def test_no_account_number_shapes(committed_frame):
    """
    Identifiers in this project are prefixed (ENT-00001, CPT-00375), so a bare
    nine-plus digit run in a text column means something built one by accident.
    """
    name, frame = committed_frame
    for column in _text_columns(frame):
        values = frame[column].astype(str)
        hits = values[values.str.contains(LONG_DIGIT_RUN, regex=True, na=False)]
        assert hits.empty, (
            f"{name}.{column} contains {len(hits)} bare long digit run(s), "
            f"e.g. {hits.iloc[0]!r}"
        )


def test_no_luhn_valid_card_numbers(committed_frame):
    name, frame = committed_frame
    for column in _text_columns(frame):
        for value in frame[column].astype(str).unique():
            for candidate in CARD_CANDIDATE.findall(value):
                assert not _luhn_valid(candidate), (
                    f"{name}.{column} contains a Luhn-valid card-shaped number: "
                    f"{candidate!r}"
                )


def test_no_real_institutions_or_filings_named(committed_frame):
    """C10: genericised throughout, including in the generated display names."""
    name, frame = committed_frame
    for column in _text_columns(frame):
        joined = " ".join(frame[column].astype(str).unique()).lower()
        for term in FORBIDDEN_TERMS:
            assert term not in joined, (
                f"{name}.{column} mentions {term!r}, which C10 forbids"
            )


def test_geography_is_genericised(entities, counterparties):
    """No real place names anywhere -- regions are Region A..Region F."""
    for frame in (entities, counterparties):
        assert set(frame["home_region"].unique()) <= set(cfg.REGIONS)


def test_identifiers_follow_the_synthetic_prefix_convention(transactions):
    assert transactions["transaction_id"].str.fullmatch(r"TXN-\d{7}").all()
    assert transactions["entity_id"].str.fullmatch(r"ENT-\d{5}").all()
    with_counterparty = transactions.loc[
        transactions["counterparty_id"] != "", "counterparty_id"
    ]
    assert with_counterparty.str.fullmatch(r"CPT-\d{5}").all()


def test_disclaimer_text_exists_and_is_unambiguous():
    """The banner the README header and the app both render (C1)."""
    text = cfg.DISCLAIMER.lower()
    assert "synthetic" in text
    assert "not a compliance product" in text
    assert "no real institution" in text
