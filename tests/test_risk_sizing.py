"""Unit tests for risk_sizing.py. Run: python -m unittest discover -s tests   (from signal-bot/)"""
import math
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from risk_sizing import Contract, RiskConfig, PaperLedger, size_trade, loss_per_contract, est_liquidation, order_key, is_duplicate  # noqa: E402

BTC = Contract("BTC_USDT", contract_size=0.0001, min_vol=1, vol_unit=1, max_leverage=500, taker_fee=0.0002)
INJ = Contract("INJ_USDT", contract_size=1, min_vol=1, vol_unit=1, max_leverage=300, taker_fee=0.0002)
ADA = Contract("ADA_USDT", contract_size=1, min_vol=1, vol_unit=1, max_leverage=200, taker_fee=0.0002)
NOCOST = RiskConfig(slippage=0.0, funding_per_day=0.0)


class Sizing(unittest.TestCase):
    def test_basic_math_no_costs(self):
        c = Contract("X", contract_size=1, min_vol=1, vol_unit=1, max_leverage=100, taker_fee=0.0)
        r = size_trade(1000, 100, 90, c, NOCOST)                   # budget 2% = $20, $10 per contract -> 2 contracts
        self.assertTrue(r.ok)
        self.assertEqual(r.vol, 2)
        self.assertAlmostEqual(r.risk_usd, 20.0)
        self.assertAlmostEqual(r.notional, 200.0)

    def test_rounds_down_to_volume_step(self):
        c = Contract("X", contract_size=1, min_vol=0.1, vol_unit=0.1, max_leverage=100, taker_fee=0.0)
        r = size_trade(1000, 100, 70, c, NOCOST)                   # $20 / $30 = 0.666 -> 0.6
        self.assertTrue(r.ok)
        self.assertAlmostEqual(r.vol, 0.6)
        self.assertLessEqual(r.risk_usd, r.intended_risk_usd + 1e-9)

    def test_minimum_order_too_risky_is_rejected_not_enlarged(self):
        r = size_trade(17.40, 8.70, 6.76, INJ, RiskConfig(risk_frac=0.02))   # min order risks ~$1.95 > $0.35
        self.assertFalse(r.ok)
        self.assertIn("minimum order", r.reason)
        self.assertGreater(r.min_order_risk_usd, r.intended_risk_usd)
        self.assertEqual(r.vol, 0)

    def test_cap_is_never_exceeded_even_if_risk_frac_is_larger(self):
        c = Contract("X", contract_size=1, min_vol=1, vol_unit=1, max_leverage=100, taker_fee=0.0)
        r = size_trade(100, 10, 9, c, RiskConfig(risk_frac=0.50, max_loss_frac=0.10, max_open_risk_frac=1.0, slippage=0, funding_per_day=0))
        self.assertTrue(r.ok)
        self.assertLessEqual(r.risk_usd, 10.0 + 1e-9)

    def test_open_risk_limit(self):
        r = size_trade(100, 10, 9, ADA, RiskConfig(max_open_risk_frac=0.06), open_risk_usd=6.0)
        self.assertFalse(r.ok)
        self.assertIn("open-risk", r.reason)
        r2 = size_trade(100, 1.0, 0.9, ADA, RiskConfig(risk_frac=0.02, max_open_risk_frac=0.06, slippage=0, funding_per_day=0), open_risk_usd=5.0)
        self.assertTrue(r2.ok)                                     # only $1 of room left, ~$0.10 per contract
        self.assertLessEqual(r2.risk_usd, 1.0 + 1e-9)

    def test_costs_are_part_of_the_risk(self):
        c = Contract("X", contract_size=1, min_vol=1, vol_unit=1, max_leverage=100, taker_fee=0.001)
        per = loss_per_contract(100, 90, c, RiskConfig(slippage=0.0005, funding_per_day=0.0003, hold_days=20))
        self.assertAlmostEqual(per, 10 + (100 + 90) * 0.0015 + 100 * 0.006)

    def test_invalid_inputs(self):
        for eq, e, s in ((0, 10, 9), (100, 0, 9), (100, 10, None), (100, 10, float("nan")), (100, 10, 10), (100, 10, 11), (-5, 10, 9), (100, 10, -1)):
            r = size_trade(eq, e, s, ADA)
            self.assertFalse(r.ok, (eq, e, s))
            self.assertEqual(r.vol, 0)

    def test_invalid_contract(self):
        bad = Contract("X", contract_size=0, min_vol=1)
        self.assertFalse(size_trade(100, 10, 9, bad).ok)

    def test_leverage_limit(self):
        c = Contract("X", contract_size=1, min_vol=1, vol_unit=1, max_leverage=2, taker_fee=0.0)
        r = size_trade(100, 100, 99.9, c, RiskConfig(risk_frac=0.02, slippage=0, funding_per_day=0))   # tight stop -> big notional
        self.assertFalse(r.ok)
        self.assertIn("leverage", r.reason)

    def test_liquidation_before_stop_rejected(self):
        c = Contract("X", contract_size=1, min_vol=1, vol_unit=1, max_leverage=100, taker_fee=0.0)
        r = size_trade(100, 100, 95, c, RiskConfig(risk_frac=0.10, max_open_risk_frac=1, slippage=0, funding_per_day=0), other_notional=5000)
        self.assertFalse(r.ok)
        self.assertIn("liquidation", r.reason)

    def test_liquidation_estimate(self):
        self.assertEqual(est_liquidation(100, 50, 10), 0.0)        # notional below equity: no liquidation
        self.assertAlmostEqual(est_liquidation(100, 200, 10), 10 * (1 - (0.5 - 0.005)))

    def test_property_actual_risk_never_above_budget(self):
        rnd = random.Random(7)
        for _ in range(3000):
            c = Contract("X", contract_size=10 ** rnd.uniform(-4, 1), min_vol=rnd.choice((1, 0.1, 10)), vol_unit=rnd.choice((1, 0.1)),
                         max_leverage=rnd.choice((5, 20, 100, 500)), taker_fee=rnd.choice((0, 0.0002, 0.0004)))
            eq, entry = rnd.uniform(5, 5000), 10 ** rnd.uniform(-3, 5)
            stop = entry * (1 - rnd.uniform(0.003, 0.5))
            cfg = RiskConfig(risk_frac=rnd.uniform(0.005, 0.2))
            r = size_trade(eq, entry, stop, c, cfg, open_risk_usd=rnd.uniform(0, 0.05) * eq, other_notional=rnd.uniform(0, 2) * eq)
            if r.ok:
                self.assertLessEqual(r.risk_usd, min(cfg.risk_frac, cfg.max_loss_frac) * eq + 1e-9)
                self.assertLessEqual(r.risk_usd, cfg.max_open_risk_frac * eq + 1e-9)
                self.assertGreaterEqual(r.vol, c.min_vol)
                self.assertTrue(math.isclose(round(r.vol / c.vol_unit), r.vol / c.vol_unit, abs_tol=1e-6))
                self.assertLessEqual(r.leverage, c.max_leverage)

    def test_audit_record(self):
        r = size_trade(17.4, 0.5, 0.45, ADA)
        for k in ("equity", "entry", "stop", "contract", "config", "open_risk_usd"):
            self.assertIn(k, r.audit)


class Duplicates(unittest.TestCase):
    def test_order_key_is_deterministic(self):
        self.assertEqual(order_key("BTC:1792108800000", "a"), order_key("BTC:1792108800000", "a"))
        self.assertLessEqual(len(order_key("VERYLONGCOINNAME:1792108800000", "a")), 32)

    def test_is_duplicate(self):
        self.assertTrue(is_duplicate({"BTC:1": {}}, "BTC:1"))
        self.assertFalse(is_duplicate({"BTC:1": {}}, "BTC:2"))


class Paper(unittest.TestCase):
    def test_paper_round_trip_and_duplicate(self):
        led = PaperLedger(17.4)
        r = size_trade(17.4, 0.50, 0.45, ADA, RiskConfig(risk_frac=0.05))
        self.assertTrue(r.ok)
        self.assertTrue(led.enter("ADA:1", r, 0.50, 0.45, ADA))
        self.assertFalse(led.enter("ADA:1", r, 0.50, 0.45, ADA))  # duplicate refused
        pnl = led.exit("ADA:1", 0.45)
        self.assertLess(pnl, 0)
        self.assertGreaterEqual(pnl, -r.intended_risk_usd - 1e-6)  # the stop loss stays inside the budget
        self.assertAlmostEqual(led.equity, 17.4 + pnl)

    def test_rejected_trade_is_logged(self):
        led = PaperLedger(17.4)
        r = size_trade(17.4, 8.7, 6.76, INJ, RiskConfig(risk_frac=0.02))
        self.assertFalse(led.enter("INJ:1", r, 8.7, 6.76, INJ))
        self.assertEqual(led.log[-1]["event"], "rejected")


if __name__ == "__main__":
    unittest.main()
