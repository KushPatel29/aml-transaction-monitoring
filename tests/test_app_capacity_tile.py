"""The capacity tile's colour has to follow the sign of the headroom it shows.

The delta is capacity minus alerts per week. It used to switch to Streamlit's
"inverse" colouring whenever the queue was over capacity, which paints a
negative delta green: the one state the tile exists to warn about read as good
news. Run through Streamlit's own script runner, with the analyst slider moved
to both ends and, for the shortfall, the alert threshold at its lowest.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app" / "streamlit_app.py"


def _capacity_tile(analysts: int, lowest_threshold: bool = False):
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(APP), default_timeout=180)
    app.run()
    if app.error and not app.metric:
        pytest.skip("scored data is not built: run python run_pipeline.py")
    if lowest_threshold:
        threshold = app.slider[0]
        threshold.set_value(threshold.min).run()
    slider = next(s for s in app.slider if s.label == "Analysts")
    slider.set_value(analysts).run()
    assert not app.exception, [e.message for e in app.exception]
    return next(m for m in app.metric if m.label == "Alerts / week")


def test_headroom_reads_green_and_a_shortfall_reads_red():
    roomy = _capacity_tile(20)
    short = _capacity_tile(1, lowest_threshold=True)
    assert roomy.proto.delta.startswith("+") and roomy.proto.color == roomy.proto.GREEN
    assert short.proto.delta.startswith("-"), "one analyst should not cover the queue"
    assert short.proto.color == short.proto.RED
