"""
app/engines/__init__.py
----------------------
The seven-engine low-latency architecture:

  1. data_engine      - Real-time WebSocket market data per strategy/asset
  2. cost_engine      - Transaction-cost engine (charges + breakeven per asset)
  3. risk_engine      - Constraint health: margin, position, inventory
  4. signal_engine    - Signal metric generation from market data
  5. execution_engine - Real-time WebSocket order management
  6. strategy_engine  - Live strategy registry + decision metrics
  7. monitor_engine   - Pipeline visualization + control plane
"""

from app.engines.base import Engine

__all__ = ["Engine"]