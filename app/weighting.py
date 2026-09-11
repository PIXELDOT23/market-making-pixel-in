"""
app/weighting.py
----------------
RoM-weighted capital allocation support.

A per-symbol "cycle-profit-per-margin" (RoM) weight tells the scanner which
markets earn the most rupees per rupee of committed margin. The raw signal is
noisy (touch profit moves every refresh), so it is EWMA-smoothed here before
ranking / slot / sizing decisions ever see it.

The tracker itself stays pure and stateless-stateful: it only smooths whatever
value it is handed each scanner refresh.
"""

from __future__ import annotations

import math
from typing import Dict


class WeightTracker:
    """EWMA smoother for per-symbol RoM weights."""

    def __init__(self, alpha: float = 0.15):
        self.alpha = max(0.0, min(1.0, alpha))
        self._val: Dict[str, float] = {}

    def smooth(self, symbol: str, raw: float) -> float:
        """Fold a new raw RoM value into the running EWMA and return it.

        A non-finite raw (NaN/inf from poisoned margin math) is clamped to 0.0
        so one bad row can never contaminate the entire allocation state.
        """
        raw = max(raw, 0.0) if math.isfinite(raw) else 0.0
        prev = self._val.get(symbol)
        if prev is None:
            value = raw
        else:
            value = self.alpha * raw + (1.0 - self.alpha) * prev
        self._val[symbol] = value
        return value

    def value(self, symbol: str) -> float:
        return self._val.get(symbol, 0.0)