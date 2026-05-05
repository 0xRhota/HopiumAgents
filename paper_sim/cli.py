"""Paper sim CLI.

Usage:
    python -m paper_sim.cli run --account A
    python -m paper_sim.cli run --account B
    python -m paper_sim.cli run --account C
    python -m paper_sim.cli run --account D
    python -m paper_sim.cli report
    python -m paper_sim.cli report --account A
    python -m paper_sim.cli calibrate --duration 24h
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Dict

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from paper_sim.runner import PaperRunner, RunnerConfig
from paper_sim.strategies.account_a_tight_slow import AccountATightSlow, AccountAConfig
from paper_sim.strategies.account_b_funding_arb import AccountBFundingArb, AccountBConfig
from paper_sim.strategies.account_c_llm_scout import AccountCLLMScout, AccountCConfig
from paper_sim.strategies.account_d_hl_maker import AccountDHLMaker, AccountDConfig
from paper_sim.strategies.account_e_eth_btc_pair import (
    AccountEEthBtcPair, AccountEConfig,
)
from paper_sim.venues.paradex import ParadexVenue
from paper_sim.venues.hyperliquid import HyperliquidVenue


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("paper_sim.cli")


def _build_strategy(account: str):
    """Build strategy for one of: A, B, C1, C2, C3, D.

    C1 = Opus + DeepSeek    (consensus)
    C2 = Opus + Qwen         (consensus)
    C3 = Opus + Grok         (consensus)

    For C1/C2/C3, both LLM clients log every call (briefing + raw response +
    parsed ideas) to logs/paper/{account}_decisions.jsonl. The strategy also
    appends one consensus record per cycle.
    """
    if account == "A":
        return AccountATightSlow(AccountAConfig())
    if account == "B":
        return AccountBFundingArb(AccountBConfig())
    if account in ("C1", "C2", "C3"):
        from paper_sim.llm_clients import (
            MODEL_OPUS, MODEL_DEEPSEEK, MODEL_QWEN, MODEL_GROK,
            make_logged_client,
        )
        from paper_sim.core.decision_log import DecisionLog

        decision_log = DecisionLog(
            f"logs/paper/{account}_decisions.jsonl", account=account)
        partner_model = {"C1": MODEL_DEEPSEEK, "C2": MODEL_QWEN,
                         "C3": MODEL_GROK}[account]

        # cycle_id provider lets logged clients tag their call with the
        # strategy's current cycle id (set inside evaluate() before LLM calls)
        strategy_holder = {}
        cycle_id_provider = lambda: strategy_holder["s"].cycle_id()

        opus_logged = make_logged_client(MODEL_OPUS, decision_log, cycle_id_provider)
        partner_logged = make_logged_client(partner_model, decision_log,
                                            cycle_id_provider)

        s = AccountCLLMScout(
            llm_clients=[opus_logged, partner_logged],
            config=AccountCConfig(),
            decision_log=decision_log,
        )
        strategy_holder["s"] = s
        return s
    if account == "D":
        return AccountDHLMaker(AccountDConfig())
    if account == "E":
        return AccountEEthBtcPair(AccountEConfig())
    raise ValueError(f"unknown account: {account}")


def _build_venues(strategy) -> Dict:
    venues: Dict = {}
    for v in strategy.venues():
        if v == "paradex":
            venues[v] = ParadexVenue()
        elif v == "hyperliquid":
            venues[v] = HyperliquidVenue()
        else:
            raise ValueError(f"no client for venue {v}")
    return venues


async def _run(account: str, starting_equity: float = 5000.0):
    strategy = _build_strategy(account)
    venues = _build_venues(strategy)
    config = RunnerConfig(
        account=account,
        starting_equity=starting_equity,
        decision_interval_seconds=60.0,
        ledger_dir="logs/paper",
        record_market_data=True,
    )
    runner = PaperRunner(strategy, venues, config)
    logger.info(f"Starting paper account {account} with ${starting_equity}")
    try:
        await runner.run()
    except KeyboardInterrupt:
        logger.info("Stopping...")
        runner.stop()


def _cmd_report(account: str | None):
    from paper_sim.reports import report_all, report_for_account
    if account:
        rep = report_for_account(f"logs/paper/{account}_ledger.jsonl")
        print(rep.render())
    else:
        for rep in report_all():
            print(rep.render())
            print()


async def _cmd_calibrate(duration_seconds: float):
    """Shadow-mode: record live data + replay through sim and compare to live ledger.

    For now this just records L2 + trades for `duration_seconds` to
    logs/paper/{venue}_l2_recording.jsonl. Calibration analysis is run via
    paper_sim.tests.calibration.test_shadow_divergence after recording.
    """
    from paper_sim.venues.replay import RecorderVenue
    venues = {
        "paradex": ParadexVenue(),
        "hyperliquid": HyperliquidVenue(),
    }
    recorders = {
        v: RecorderVenue(f"logs/paper/{v}_l2_recording.jsonl") for v in venues
    }
    symbols_per_venue = {
        "paradex": ["BTC-USD-PERP", "ETH-USD-PERP", "SOL-USD-PERP"],
        "hyperliquid": ["BTC", "ETH", "SOL"],
    }

    async def consume(name, client):
        async for event in client.stream(symbols_per_venue[name]):
            recorders[name].record(event)

    for c in venues.values():
        await c.connect()

    tasks = [asyncio.create_task(consume(n, c)) for n, c in venues.items()]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=duration_seconds)
    except asyncio.TimeoutError:
        pass
    finally:
        for c in venues.values():
            await c.close()
        for r in recorders.values():
            r.close()
        logger.info(f"Calibration recording complete: {list(recorders.keys())}")


def _parse_duration(s: str) -> float:
    s = s.strip().lower()
    if s.endswith("h"):
        return float(s[:-1]) * 3600.0
    if s.endswith("m"):
        return float(s[:-1]) * 60.0
    if s.endswith("s"):
        return float(s[:-1])
    return float(s)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="paper_sim")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="run a paper account")
    p_run.add_argument("--account", required=True,
                       choices=["A", "B", "C1", "C2", "C3", "D", "E"])
    p_run.add_argument("--starting-equity", type=float, default=5000.0)

    p_report = sub.add_parser("report", help="show paper-account PnL")
    p_report.add_argument("--account", default=None,
                          help="account letter (omit for all)")

    p_cal = sub.add_parser("calibrate", help="record live L2 for shadow-mode calibration")
    p_cal.add_argument("--duration", default="24h")

    p_pf = sub.add_parser(
        "preflight",
        help="validate a venue connection delivers all event types")
    p_pf.add_argument("--venue", required=True,
                      choices=["paradex", "hyperliquid"])
    p_pf.add_argument("--duration", default="90s")
    p_pf.add_argument("--symbols", nargs="+",
                      default=None,
                      help="defaults to BTC/ETH/SOL of the venue")

    args = parser.parse_args(argv)

    if args.cmd == "run":
        asyncio.run(_run(args.account, args.starting_equity))
    elif args.cmd == "report":
        _cmd_report(args.account)
    elif args.cmd == "calibrate":
        asyncio.run(_cmd_calibrate(_parse_duration(args.duration)))
    elif args.cmd == "preflight":
        from paper_sim.preflight import run_preflight
        if args.venue == "paradex":
            client = ParadexVenue()
            symbols = args.symbols or ["BTC-USD-PERP", "ETH-USD-PERP", "SOL-USD-PERP"]
        else:
            client = HyperliquidVenue()
            symbols = args.symbols or ["BTC", "ETH", "SOL"]
        result = asyncio.run(run_preflight(
            client, symbols, _parse_duration(args.duration)))
        print(result.render())
        sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
