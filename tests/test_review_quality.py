from copy import deepcopy

import pytest

from governance import review_quality as quality


@pytest.fixture
def prepared():
    cases, graph = quality.evidence()
    return quality.training_note(cases, graph), cases, graph


def test_complete_draft_remains_nondecisional_and_unreviewed(prepared):
    note, cases, graph = prepared
    result = quality.assess(note, cases, graph)
    assert result["structural_quality"] == "READY FOR HUMAN REVIEW"
    assert result["decision_authorized"] is False
    assert result["independent_review"] == "PENDING"


@pytest.mark.parametrize(
    "change",
    [
        "unknown_case",
        "cross_case_reference",
        "labels",
        "no_observations",
        "unresolved_closure",
        "filing",
        "approval",
        "missing_alternative",
    ],
)
def test_unsafe_or_incomplete_note_is_blocked(prepared, change):
    original, cases, graph = prepared
    note = deepcopy(original)
    if change == "unknown_case":
        note["investigation_id"] = "unknown"
    elif change == "cross_case_reference":
        note["observed_facts"][0]["source_ref"] = "register:INV-OTHER"
    elif change == "labels":
        note["nested"] = [{"ground_truth": "known suspicious"}]
    elif change == "no_observations":
        note["observed_facts"] = []
    elif change == "unresolved_closure":
        note["recommendation"] = "propose_closure"
    elif change == "filing":
        note["filing_status"] = "FILED"
    elif change == "approval":
        note["human_approved"] = True
    else:
        note["alternative_explanation"] = ""
    result = quality.assess(note, cases, graph)
    assert result["structural_quality"] == "BLOCKED"
    assert result["problems"]
    assert result["decision_authorized"] is False
