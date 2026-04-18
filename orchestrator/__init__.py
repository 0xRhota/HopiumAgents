"""
Swing Trading Orchestrator

Coordinates 4 DEX agents for swing trading with:
- Funding rate exploitation
- Technical analysis scoring
- Cross-exchange intelligence
- 15-minute decision cycles
"""

from .config import *
from .swing_orchestrator import SwingOrchestrator

__version__ = "1.0.0"
