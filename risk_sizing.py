"""Risk-based position sizing for MEXC linear USDT perpetuals (long entries).

NOT USED BY THE LIVE BOT YET. futures_trader.sizing() still decides live orders; this module is for dry runs, paper
trading and comparison until the owner approves activation.

Principle: the money lost if the stop is hit (price move + fees in and out + expected funding) must not exceed
    budget = min(risk_frac, max_loss_frac) x equity,   further limited by the open-risk room
             max_open_risk_frac x equity - risk already open.
Position size = floor(budget / loss_per_contract) contracts (rounded DOWN to the contract's volume step).
If even MEXC's minimum order would lose more than the budget, the trade is REJECTED (never silently enlarged).
Leverage only sets the margin; it is the lowest that keeps this trade's margin <= margin_frac x equity, and the trade
is rejected if the cross-margin liquidation estimate would come before the stop.
"""
import math
from dataclasses import dataclass, field, asdict


@dataclass(frozen=True)
class Contract:
    symbol: str
    contract_size: float          # coins per contract
    min_vol: float                # minimum contracts per order
    vol_unit: float = 1.0         # contract step
    max_leverage: int = 20
    taker_fee: float = 0.0002     # fraction of notional per side
    maker_fee: float = 0.0


@dataclass(frozen=True)
class RiskConfig:
    risk_frac: float = 0.02           # intended loss at the stop, fraction of equity
    max_loss_frac: float = 0.10       # existing hard cap per trade (/maxloss); never exceeded
    max_open_risk_frac: float = 0.06  # sum of all open trades' loss at their stops
    slippage: float = 0.0005          # extra fraction of notional per side on market orders / stops
    funding_per_day: float = 0.0003   # expected funding paid by a long, fraction of notional per day
    hold_days: float = 22.0           # expected holding time (backtest average)
    margin_frac: float = 0.20         # at most this share of equity posted as margin for one trade
    maint_margin: float = 0.005       # rough maintenance margin for the liquidation estimate


@dataclass
class SizingResult:
    ok: bool
    reason: str
    vol: float = 0.0
    notional: float = 0.0
    leverage: int = 0
    intended_risk_usd: float = 0.0    # the budget
    risk_usd: float = 0.0             # what this order actually risks (incl. fees + funding)
    risk_frac: float = 0.0
    min_order_risk_usd: float = 0.0   # what the exchange minimum would risk
    liquidation_est: float = 0.0
    audit: dict = field(default_factory=dict)


def _finite_pos(*xs):
    return all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0 for x in xs)


def loss_per_contract(entry, stop, c: Contract, cfg: RiskConfig):
    """money lost per contract if the stop is hit: price move + taker fee and slippage in and out + expected funding."""
    move = (entry - stop) * c.contract_size
    costs = c.contract_size * (entry * (c.taker_fee + cfg.slippage) + stop * (c.taker_fee + cfg.slippage))
    funding = c.contract_size * entry * cfg.funding_per_day * cfg.hold_days
    return move + costs + funding


def est_liquidation(equity, total_notional, entry, maint=0.005):
    """cross margin: price at which equity is gone if every open position fell by the same %. 0.0 = none above zero."""
    if total_notional <= 0 or equity >= total_notional * (1 + maint):
        return 0.0
    return entry * (1 - max(equity / total_notional - maint, 0.0))


def size_trade(equity, entry, stop, c: Contract, cfg: RiskConfig = RiskConfig(), open_risk_usd=0.0, other_notional=0.0):
    audit = dict(equity=equity, entry=entry, stop=stop, contract=asdict(c), config=asdict(cfg),
                 open_risk_usd=open_risk_usd, other_notional=other_notional)
    if not _finite_pos(equity, entry) or stop is None or not isinstance(stop, (int, float)) or not math.isfinite(stop) or stop <= 0:
        return SizingResult(False, "invalid equity, price or stop", audit=audit)
    if stop >= entry:
        return SizingResult(False, "stop is not below the entry (long)", audit=audit)
    if not _finite_pos(c.contract_size, c.min_vol, c.vol_unit) or c.max_leverage < 1:
        return SizingResult(False, "invalid contract specification", audit=audit)
    cap = min(cfg.risk_frac, cfg.max_loss_frac) * equity
    room = cfg.max_open_risk_frac * equity - max(open_risk_usd, 0.0)
    budget = min(cap, room)
    per = loss_per_contract(entry, stop, c, cfg)
    min_risk = per * c.min_vol
    base = dict(intended_risk_usd=round(budget, 6), min_order_risk_usd=round(min_risk, 6), audit=audit)
    if budget <= 0:
        return SizingResult(False, f"open-risk limit reached ({open_risk_usd:.2f}$ of {cfg.max_open_risk_frac * equity:.2f}$)", **base)
    steps = math.floor(budget / per / c.vol_unit + 1e-9)
    vol = round(steps * c.vol_unit, 10)
    if vol < c.min_vol:
        return SizingResult(False, f"minimum order would risk {min_risk:.2f}$, more than the budget {budget:.2f}$", **base)
    notional = vol * c.contract_size * entry
    lev = max(1, math.ceil(notional / (cfg.margin_frac * equity)))
    if lev > c.max_leverage:
        return SizingResult(False, f"needs {lev}x leverage, contract allows {c.max_leverage}x", **base)
    liq = est_liquidation(equity, other_notional + notional, entry, cfg.maint_margin)
    if liq >= stop:
        return SizingResult(False, f"estimated liquidation {liq:.6g} is at or above the stop {stop:.6g}", liquidation_est=liq, **base)
    risk = per * vol
    assert risk <= budget + 1e-9                                  # by construction
    return SizingResult(True, "ok", vol=vol, notional=notional, leverage=lev, risk_usd=risk, risk_frac=risk / equity,
                        liquidation_est=liq, **base)


def order_key(signal_key, leg):
    """deterministic client order id: the same signal can never produce two different entry orders."""
    return ("sb" + signal_key.replace(":", "") + leg)[:32]


def is_duplicate(trades, signal_key):
    """a signal already recorded (opening, open, closed, manual, failed) is never ordered again."""
    return signal_key in trades


class PaperLedger:
    """paper trading: records intended vs filled risk; fills at a given price plus slippage, never touches an exchange."""

    def __init__(self, equity):
        self.equity, self.open, self.closed, self.log = equity, {}, [], []

    def open_risk(self):
        return sum(p["risk_usd"] for p in self.open.values())

    def enter(self, key, res: SizingResult, fill_price, stop, c: Contract):
        if is_duplicate(self.open, key) or any(t["key"] == key for t in self.closed):
            self.log.append(dict(key=key, event="duplicate refused"))
            return False
        if not res.ok:
            self.log.append(dict(key=key, event="rejected", reason=res.reason))
            return False
        actual = loss_per_contract(fill_price, stop, c, RiskConfig(**res.audit["config"])) * res.vol
        self.open[key] = dict(key=key, vol=res.vol, entry=fill_price, stop=stop, cs=c.contract_size, risk_usd=actual,
                              intended=res.intended_risk_usd, fee=c.taker_fee)
        self.log.append(dict(key=key, event="entry", intended_risk=res.intended_risk_usd, actual_risk=actual, vol=res.vol))
        return True

    def exit(self, key, price, slippage=0.0005, funding_paid=0.0):
        p = self.open.pop(key)
        gross = (price - p["entry"]) * p["vol"] * p["cs"]
        fees = (p["entry"] + price) * p["vol"] * p["cs"] * (p["fee"] + slippage)
        pnl = gross - fees - funding_paid
        self.equity += pnl
        self.closed.append(dict(p, exit=price, pnl=pnl))
        self.log.append(dict(key=key, event="exit", pnl=pnl, equity=self.equity))
        return pnl
