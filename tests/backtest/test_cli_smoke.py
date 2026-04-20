"""Smoke test: CLI runs end-to-end on live Binance data without exception."""
import subprocess
import sys
from pathlib import Path


def test_cli_runs_and_outputs_summary():
    script = Path(__file__).parent.parent.parent / "scripts" / "run_backtest.py"
    result = subprocess.run(
        [sys.executable, str(script),
         "--symbol", "BTC-PERP", "--exchange", "nado",
         "--days", "1", "--score-min", "2.5"],
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "NET PnL" in result.stdout
