"""
Detection pipeline.

Importing anything from this package puts the repository root on sys.path so
`import config` resolves the same way whether the caller is run_pipeline.py,
pytest, or the Streamlit app.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
