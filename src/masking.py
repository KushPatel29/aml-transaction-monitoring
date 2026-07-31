"""
Identifier masking for anything that renders (C1).

The data is synthetic, so nothing here is protecting a real person. It is here
because a monitoring dashboard is exactly the kind of surface where showing
full identifiers becomes a habit, and because the discipline should be visible
in the code rather than asserted in a README.

Everything the app displays goes through `mask_frame`. The dashboard has no
other way to put a table on screen -- app/streamlit_app.py routes every table
through a single helper, and tests/test_dashboard_masks_identifiers.py asserts
both that the masking works and that the helper is the only route.
"""

from __future__ import annotations

import pandas as pd

from . import _ROOT  # noqa: F401  (side effect: repo root on sys.path)

import config as cfg  # noqa: E402

# Mask who, not what.
#
# entity_id, counterparty_id and display names identify a PARTY, and those are
# what a triage screen should not be putting up in full next to a risk score.
# case_id and transaction_id identify an EVENT: they are the references an
# analyst quotes when escalating, and masking them would make the queue
# unusable while protecting nothing.
IDENTIFIER_COLUMNS = ["entity_id", "counterparty_id"]
NAME_COLUMNS = ["display_name", "entity_name", "counterparty_name"]


def mask_identifier(value: object, visible: int = cfg.MASK_VISIBLE_CHARS) -> str:
    """
    ENT-01344 -> ENT-...1344

    Keeps the type prefix, so an analyst can still tell an entity from a
    counterparty, and the last few characters, so two rows referring to the
    same account are still visibly the same account.
    """
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""

    prefix, separator, tail = text.partition("-")
    if not separator:
        prefix, tail = "", text
    if len(tail) <= visible:
        return f"{prefix}-{tail}" if prefix else tail
    masked = f"...{tail[-visible:]}"
    return f"{prefix}-{masked}" if prefix else masked


def mask_name(value: object) -> str:
    """
    Ada Lovelace -> A. L.

    Initials keep a drill-down readable without putting a full name on screen
    next to a risk score.
    """
    if value is None:
        return ""
    parts = [p for p in str(value).split() if p]
    if not parts:
        return ""
    return " ".join(f"{p[0].upper()}." for p in parts[:3])


def mask_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with every identifier and name column masked."""
    masked = frame.copy()
    for column in IDENTIFIER_COLUMNS:
        if column in masked.columns:
            masked[column] = masked[column].map(mask_identifier)
    for column in NAME_COLUMNS:
        if column in masked.columns:
            masked[column] = masked[column].map(mask_name)
    return masked


def contains_unmasked_identifier(frame: pd.DataFrame) -> list[str]:
    """
    Report any cell still holding a full identifier.

    Used by the tests rather than by the app: it is the check that the masking
    actually happened, applied to whatever the dashboard is about to render.
    """
    offenders = []
    for column in frame.columns:
        if frame[column].dtype != object:
            continue
        values = frame[column].dropna().astype(str)
        # A full party identifier looks like ENT-01344 or CPT-00375 -- prefix,
        # dash, then digits with no ellipsis. Event references (CASE-, TXN-)
        # are intentionally not covered; see IDENTIFIER_COLUMNS above.
        if values.str.fullmatch(r"(ENT|CPT)-\d+").any():
            offenders.append(column)
    return offenders
