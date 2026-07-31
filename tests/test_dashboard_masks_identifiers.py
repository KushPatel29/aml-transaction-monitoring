"""
C1: the dashboard must not put party identifiers on screen in full.

Two kinds of check here, and both are needed.

The behavioural checks call the app's data-shaping functions directly and
assert that what would have been rendered is clean. Those catch a panel that
builds the wrong frame.

The source check asserts `show_table` is the only caller of `st.dataframe` in
the app. That catches the failure the behavioural tests never can: somebody
adding a new panel next year that renders a frame nobody wrote a test for.
"""

from __future__ import annotations

import ast
import re

import pandas as pd
import pytest

import config as cfg
from src import masking

streamlit_app = pytest.importorskip(
    "app.streamlit_app", reason="streamlit not installed"
)

APP_SOURCE_PATH = cfg.ROOT / "app" / "streamlit_app.py"


# ---------------------------------------------------------------------------
# Structural: is there any other way onto the screen?
# ---------------------------------------------------------------------------
def _streamlit_calls(source: str) -> list[tuple[str, str]]:
    """Every st.<something>(...) call, with the enclosing function name."""
    tree = ast.parse(source)
    calls = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and isinstance(inner.func.value, ast.Name)
                and inner.func.value.id == "st"
            ):
                calls.append((node.name, inner.func.attr))
    return calls


def test_show_table_is_the_only_route_to_a_dataframe():
    source = APP_SOURCE_PATH.read_text(encoding="utf-8")
    renderers = {
        function for function, attribute in _streamlit_calls(source)
        if attribute in ("dataframe", "table", "data_editor")
    }
    assert renderers == {"show_table"}, (
        f"tables are rendered from {sorted(renderers)}; every table must go "
        "through show_table so masking cannot be forgotten"
    )


def test_show_table_actually_masks():
    source = APP_SOURCE_PATH.read_text(encoding="utf-8")
    body = source[source.index("def show_table"):]
    body = body[: body.index("\ndef ")]
    assert "mask_frame" in body, "show_table renders without masking"


def test_the_disclaimer_is_rendered_unconditionally():
    """
    C1 requires the banner in the app itself, not only in the README. It must
    also run before the early return when data is missing.
    """
    source = APP_SOURCE_PATH.read_text(encoding="utf-8")
    assert "cfg.DISCLAIMER" in source
    main_body = source[source.index("def main("):]
    disclaimer_position = main_body.index("render_disclaimer()")
    error_position = main_body.index("st.error(")
    assert disclaimer_position < error_position, (
        "the missing-data error path returns before rendering the disclaimer"
    )


def test_no_raw_identifier_interpolated_into_display_strings():
    """
    Masking the tables is not enough if a caption or a title formats a raw
    entity_id into a string. Every display-string use of an identifier must go
    through mask_identifier.
    """
    source = APP_SOURCE_PATH.read_text(encoding="utf-8")
    offenders = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#") or "mask_identifier" in line:
            continue
        # f-string interpolation of a bare identifier variable
        if re.search(r"\{\s*(entity_id|selected|counterparty_id)\s*[}:!]", line):
            offenders.append(f"{line_number}: {stripped}")
    assert not offenders, "raw identifiers interpolated into display text:\n" + "\n".join(
        offenders
    )


# ---------------------------------------------------------------------------
# Behavioural: is what would be rendered actually clean?
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def queue(scored, entities):
    return streamlit_app.build_case_queue(scored, entities, threshold=39.6)


def test_case_queue_masks_cleanly(queue):
    assert not queue.empty
    assert masking.contains_unmasked_identifier(queue), (
        "the raw queue should hold full identifiers, otherwise this test proves nothing"
    )
    assert masking.contains_unmasked_identifier(masking.mask_frame(queue)) == []


def test_case_queue_is_one_row_per_entity(queue, scored):
    assert queue["entity_id"].is_unique
    flagged_entities = scored.loc[scored["risk_score"] >= 39.6, "entity_id"].nunique()
    assert len(queue) == flagged_entities


def test_case_queue_is_ordered_by_risk(queue):
    assert queue["risk_score"].is_monotonic_decreasing


def test_entity_timeline_masks_cleanly(scored, queue):
    entity_id = queue["entity_id"].iloc[0]
    timeline = streamlit_app.build_entity_timeline(scored, entity_id)
    assert not timeline.empty
    assert masking.contains_unmasked_identifier(masking.mask_frame(timeline)) == []


def test_feature_profile_reports_the_largest_deviations_first(scored, queue):
    profile = streamlit_app.build_feature_profile(scored, queue["entity_id"].iloc[0])
    deviations = profile["deviation_sigma"].abs()
    assert deviations.is_monotonic_decreasing
    assert set(profile["feature"]) == set(cfg.FEATURE_COLUMNS)


def test_network_graph_labels_are_masked(scored, queue):
    graph = streamlit_app.build_counterparty_network(scored, queue["entity_id"].iloc[0])
    if graph.number_of_nodes() == 0:
        pytest.skip("selected entity has no counterparty activity")
    for node in graph.nodes():
        assert masking.mask_identifier(node) != node or "-" not in str(node)


def test_unclassified_alerts_are_labelled_honestly():
    """
    An alert with no rule behind it must not be assigned the nearest typology.
    The model can say a transaction is unusual; it cannot say which pattern it
    resembles, and the queue should not pretend otherwise.
    """
    none_fired = dict.fromkeys(cfg.RULE_CODES, False)
    assert streamlit_app.infer_typology(none_fired) == "Unclassified (model only)"

    one_fired = {**none_fired, "R1_STRUCTURING": True}
    assert streamlit_app.infer_typology(one_fired) == "Structuring"

    two_fired = {**one_fired, "R3_SMURFING": True}
    assert " + " in streamlit_app.infer_typology(two_fired)


def test_baseline_comparison_shows_both_systems(scored):
    frame = streamlit_app.baseline_comparison_frame(scored, threshold=39.6)
    assert len(frame.columns) == 3
    assert any("Naive" in c for c in frame.columns)
    assert "Layered system" in frame.columns
    assert set(frame["measure"]) >= {"Precision", "Recall", "Expected cost (CAD)"}


def test_queue_filters_narrow_without_reordering(queue):
    filtered = streamlit_app.filter_queue(queue, ["business"], [], 0.0)
    assert len(filtered) <= len(queue)
    assert set(filtered["entity_type"]) <= {"business"}
    assert filtered["risk_score"].is_monotonic_decreasing

    by_amount = streamlit_app.filter_queue(queue, [], [], 1_000_000.0)
    assert (by_amount["amount_involved_cad"] >= 1_000_000.0).all()


def test_empty_queue_still_has_the_expected_shape(scored, entities):
    """A threshold above every score must return an empty frame, not explode."""
    empty = streamlit_app.build_case_queue(scored, entities, threshold=101.0)
    assert empty.empty
    assert list(empty.columns) == streamlit_app.QUEUE_COLUMNS


def test_currency_is_escaped_before_reaching_markdown():
    """
    Streamlit parses `$...$` as inline LaTeX. A reason code with two currency
    amounts renders as "paid in 10,995.55 ... (avg 999.60)" -- both dollar
    signs eaten and the text between them set as maths. Caught on the live app,
    so it is pinned here.
    """
    reason = (
        "R3_SMURFING: 11 distinct senders paid in $10,995.55 over 66h, "
        "amounts within 29% of each other (avg $999.60)"
    )
    escaped = streamlit_app.escape_markdown(reason)
    assert escaped.count(r"\$") == 2
    assert "$1" not in escaped.replace(r"\$", "")

    source = APP_SOURCE_PATH.read_text(encoding="utf-8")
    for line_number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith(("st.markdown(", "st.caption(")):
            continue
        if "$" in line and "escape_markdown" not in line:
            pytest.fail(
                f"line {line_number} passes a dollar sign to Streamlit markdown "
                f"without escaping: {stripped}"
            )


def test_app_requires_no_secrets_or_network(scored):
    """
    C5: deployable on the free tier with no API keys. Nothing in the app may
    read a secret or open a connection.
    """
    source = APP_SOURCE_PATH.read_text(encoding="utf-8")
    for forbidden in ("st.secrets", "requests.", "urllib", "os.environ", "boto3", "http"):
        assert forbidden not in source, f"the app references {forbidden!r}"
