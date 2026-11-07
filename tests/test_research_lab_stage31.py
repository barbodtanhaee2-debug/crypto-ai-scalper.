"""Load Stage 3.1 laboratory tests into the project suite without nested discover."""

from __future__ import annotations

import sys
from pathlib import Path

LAB = Path(__file__).resolve().parents[1] / "research-lab"
for path in (LAB, LAB / "tests", LAB.parent):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from test_catalog_and_isolation import (  # noqa: E402
    DatasetSliceTests,
    HashHelperTests,
    IsolationTests,
)
from test_engine_and_clocks import EngineAndClockTests  # noqa: E402
from test_h1_confirmed_momentum import H1ConfirmedMomentumTests  # noqa: E402

__all__ = [
    "DatasetSliceTests",
    "HashHelperTests",
    "IsolationTests",
    "EngineAndClockTests",
    "H1ConfirmedMomentumTests",
]
