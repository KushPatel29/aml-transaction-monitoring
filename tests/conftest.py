"""
Shared fixtures.

The pipeline artefacts are committed, so tests read them directly rather than
regenerating. `python run_pipeline.py` rebuilds them and CI runs it before
pytest, which means a test failing here is a real regression in the committed
state and not a stale-artefact accident.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as cfg  # noqa: E402


def _read(path: Path) -> pd.DataFrame:
    if not path.exists():
        pytest.skip(f"{path.name} missing -- run `python run_pipeline.py` first")
    # keep_default_na=False keeps the nullable ground-truth columns as empty
    # strings instead of NaN, so equality checks against "" behave.
    return pd.read_csv(path, keep_default_na=False)


@pytest.fixture(scope="session")
def transactions() -> pd.DataFrame:
    return _read(cfg.TRANSACTIONS_PATH)


@pytest.fixture(scope="session")
def entities() -> pd.DataFrame:
    return _read(cfg.ENTITIES_PATH)


@pytest.fixture(scope="session")
def counterparties() -> pd.DataFrame:
    return _read(cfg.COUNTERPARTIES_PATH)


@pytest.fixture(scope="session")
def cases() -> pd.DataFrame:
    return _read(cfg.CASES_PATH)
