"""
The privacy impact assessment's inventory against the files it describes.

docs/PRIVACY_IMPACT_ASSESSMENT.md says every data element has a class, a purpose
and a retention rule. That is only true while the inventory
(governance/privacy_inventory.json) keeps up with the data, so a new file or a
new column fails here until someone classifies it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import pytest

from governance.investigator_workflow import GROUND_TRUTH_COLUMNS
from src.masking import IDENTIFIER_COLUMNS, NAME_COLUMNS

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = json.loads((ROOT / "governance" / "privacy_inventory.json").read_text(encoding="utf-8"))
PIA = (ROOT / "docs" / "PRIVACY_IMPACT_ASSESSMENT.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _data_files() -> list[str]:
    paths = (
        sorted(ROOT.glob("data/generated/*.csv.gz"))
        + sorted(ROOT.glob("results/*.csv"))
        + sorted(ROOT.glob("results/*.csv.gz"))
    )
    return [p.relative_to(ROOT).as_posix() for p in paths]


def _columns(path: str) -> list[str]:
    return list(pd.read_csv(ROOT / path, nrows=1).columns)


def test_every_data_file_is_in_the_inventory_and_nothing_else():
    assert set(_data_files()) == set(INVENTORY["files"])


@pytest.mark.parametrize("path", sorted(INVENTORY["files"]))
def test_every_column_has_a_class(path):
    meta = INVENTORY["files"][path]
    assert meta["retention"] and meta["audience"]
    if meta.get("aggregate"):
        assert not set(_columns(path)) & {"entity_id", "counterparty_id", "display_name"}, (
            f"{path} is listed as aggregate but has a row per party"
        )
        return
    unclassified = [c for c in _columns(path) if c not in INVENTORY["columns"]]
    assert not unclassified, f"{path}: classify {unclassified} in governance/privacy_inventory.json"
    assert all(INVENTORY["columns"][c] in INVENTORY["classes"] for c in _columns(path))


def test_the_inventory_lists_no_column_that_no_longer_exists():
    seen = {
        c
        for path, meta in INVENTORY["files"].items()
        if not meta.get("aggregate")
        for c in _columns(path)
    }
    assert set(INVENTORY["columns"]) == seen


def test_names_and_party_keys_are_masked_on_screen():
    for column, cls in INVENTORY["columns"].items():
        if cls == "direct_identifier":
            assert column in NAME_COLUMNS, column
    displayed_keys = {"entity_id", "counterparty_id"}
    assert displayed_keys <= set(IDENTIFIER_COLUMNS)


def test_the_published_register_carries_initials_not_names():
    register = pd.read_csv(ROOT / "results" / "investigation_case_register.csv")
    names = register["display_name"].dropna().astype(str)
    assert len(names) > 0
    assert names.str.fullmatch(r"([A-Z0-9]\. ?){1,3}").all(), names[
        ~names.str.fullmatch(r"([A-Z0-9]\. ?){1,3}")
    ].head()


@pytest.mark.parametrize(
    "path",
    [
        "results/investigation_case_register.csv",
        "results/investigation_entity_graph.csv",
        "results/kyc_customer_risk.csv",
        "results/kyc_beneficial_owners.csv",
    ],
)
def test_no_answer_key_reaches_an_investigator_output(path):
    truth = {
        c for c, cls in INVENTORY["columns"].items() if cls == "synthetic_ground_truth"
    } | GROUND_TRUTH_COLUMNS
    assert not set(_columns(path)) & truth


def test_generator_internals_are_never_displayed_or_rated():
    internal = {c for c, cls in INVENTORY["columns"].items() if cls == "generator_internal"}
    for path in INVENTORY["files"]:
        if path.startswith("results/"):
            assert not set(_columns(path)) & internal, path


def test_the_assessment_states_the_inventory_and_every_risk():
    columns, files = len(INVENTORY["columns"]), len(INVENTORY["files"])
    classes = len(INVENTORY["classes"])
    assert f"{columns} columns across {files} files, each in one of {classes} classes" in PIA
    ids = re.findall(r"^\| (P\d+) \|", PIA, re.M)
    assert ids == [f"P{i}" for i in range(1, len(ids) + 1)]
    in_place = len(re.findall(r"^\| P\d+ \|.*\| In place", PIA, re.M))
    assert f"{in_place} of {len(ids)} risks are handled in code and tested" in PIA
    assert f"{columns} columns across {files} files in one of {classes} classes" in README
    assert f"{in_place} of {len(ids)} risks are handled in code and tested" in README


def test_every_code_reference_in_the_assessment_exists():
    for target in re.findall(r"\]\(\.\./([^)#]+)\)", PIA):
        assert (ROOT / target).exists(), target


def test_the_streaming_retention_finding_is_still_true():
    """P7 says the demo's transaction topic is kept forever. If that changes,
    the assessment has to change with it."""
    kafka = (ROOT / "streaming" / "kafka_io.py").read_text(encoding="utf-8")
    assert ('"retention.ms": "-1"' in kafka) == ("retention.ms = -1" in PIA)
