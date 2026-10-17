"""Automatic MEXC futures trading for the crypto breakout signals (long only).

Runs inside the always-on listener (commands.py), which calls tick() every few seconds.
Uses MEXC's official Futures API (https://www.mexc.com/api-docs/futures), which reopened on 2026-03-31.

RULES (set by the owner)
  * A trade is opened when a crypto signal can still be entered as tested: from the last 3 daily closes,
    price inside the entry zone (or below entry, above SL), no TP reached yet, SL not touched.
  * Margin per trade: MARGIN_PCT of futures equity (default 1%). Cross margin.
  * Leverage: the LOWER of
        - the highest leverage at which the position cannot be liquidated even if price went to zero
          (total open notional stays below LIQ_BUFFER x equity), and
        - the leverage at which hitting the SL loses at most the max-loss % of equity (/maxloss, else MAX_LOSS_PCT).
    If that is below 1x, or the order would be below the contract's minimum size, the trade is skipped.
  * At most `max_trades` open positions on the account (default 10; /maxtrades changes it).
  * TP1 (+0.5R) closes 80%, TP2 (+1R) closes the rest. If the position is too small to split, all of it closes at TP1.
  * TP and SL are exchange-side TP/SL orders of the LIMIT type, so they work even if this bot is offline.
    The SL limit price sits SL_LIMIT_OFFSET below the trigger so it still fills in a fast fall.
  * No new trades while futures equity is below MIN_BALANCE (default $1), or while paused (/pause).

The key needs only Futures "View Account Details", "View Order Details" and "Order Placing". Never withdrawal.
Everything the bot does is recorded in trades.json and reported on Telegram.
"""
import hashlib
import hmac
import json
import math
import os
import re
import time
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path

import requests

from core import signal_card

HERE = Path(__file__).resolve().parent
BOOK_FILE = HERE / "trades.json"
BASE = "https://api.mexc.com"
KEY, SECRET = os.environ.get("MEXC_API_KEY", ""), os.environ.get("MEXC_API_SECRET", "")
STYLE = os.environ.get("TARGET_STYLE", "1")
MARGIN_PCT = float(os.environ.get("MARGIN_PCT", "1"))
MAX_LOSS_PCT = float(os.environ.get("MAX_LOSS_PCT", "1"))      # default; /maxloss in Telegram overrides it (stored in trades.json)
MIN_BALANCE = float(os.environ.get("MIN_BALANCE", "1"))
LIQ_BUFFER = 0.9          # total open notional kept under 90% of equity: no liquidation price, with room for fees and funding
SL_LIMIT_OFFSET = 0.005   # SL limit order price 0.5% under the SL trigger
BE_FEES = 0.0016          # after TP1 the stop moves to entry + 0.16% (taker fee in and out), so the rest closes at about $0
TP1_SHARE = 0.8
SIGNAL_DAYS = 3
ALIAS = {"FIL": "FILECOIN"}

_mem = {"t": 0.0, "contracts": None, "contracts_t": 0.0, "err_t": {}, "pending": {}}
BOOK = None


class ApiError(Exception):
    pass


# --------------------------------------------------------------------------------------------- API
def _sign(ts, param):
    return hmac.new(SECRET.encode(), (KEY + ts + param).encode(), hashlib.sha256).hexdigest()


def _headers(param):
    ts = str(int(time.time() * 1000))
    return {"ApiKey": KEY, "Request-Time": ts, "Signature": _sign(ts, param), "Content-Type": "application/json", "Recv-Window": "20", "Language": "en"}


def _check(r):
    try:
        d = r.json()
    except Exception:
        raise ApiError(f"HTTP {r.status_code}")
    if not d.get("success"):
        raise ApiError(f"code {d.get('code')}: {d.get('message') or d.get('msg') or 'failed'}")
    return d.get("data")


def api_get(path, params=None, private=True):
    params = {k: v for k, v in (params or {}).items() if v is not None}
    q = "&".join(f"{k}={params[k]}" for k in sorted(params))
    r = requests.get(BASE + path + ("?" + q if q else ""), headers=_headers(q) if private else {}, timeout=15)
    return _check(r)


def api_post(path, body):
    if isinstance(body, dict):
        body = {k: v for k, v in body.items() if v is not None}
    raw = json.dumps(body, separators=(",", ":"))
    return _check(requests.post(BASE + path, data=raw, headers=_headers(raw), timeout=15))


def contracts():
    if not _mem["contracts"] or time.time() - _mem["contracts_t"] > 3600:
        _mem["contracts"] = {x["symbol"]: x for x in api_get("/api/v1/contract/detail", private=False)}
        _mem["contracts_t"] = time.time()
    return _mem["contracts"]


def tickers():
    return {x["symbol"]: float(x["lastPrice"]) for x in api_get("/api/v1/contract/ticker", private=False)}


def equity():
    a = api_get("/api/v1/private/account/asset/USDT")
    return float(a.get("equity") or 0), float(a.get("availableBalance") or 0)


def positions():
    # state 1 = holding, 2 = system-held, 3 = closed: closed positions are not counted even if MEXC still returns them
    return {p["positionId"]: p for p in (api_get("/api/v1/private/position/open_positions") or [])
            if float(p.get("holdVol") or 0) > 0 and int(p.get("state") or 1) in (1, 2)}


def tpsl_orders(symbol=None):
    return api_get("/api/v1/private/stoporder/open_orders", {"symbol": symbol}) or []


# --------------------------------------------------------------------------------------------- helpers
def sym_of(coin):
    return f"{ALIAS.get(coin, coin)}_USDT"


def to_unit(x, unit, mode=ROUND_DOWN):
    u = Decimal(str(unit))
    return float((Decimal(str(x)) / u).quantize(Decimal("1"), rounding=mode) * u)


def load_book():
    global BOOK
    if BOOK is None:
        try:
            BOOK = json.loads(BOOK_FILE.read_text(encoding="utf-8"))
        except Exception:
            BOOK = {}
        BOOK.setdefault("max_trades", 10)
        BOOK.setdefault("paused", False)
        BOOK.setdefault("trades", {})
        for k in [k for k, t in BOOK["trades"].items() if t.get("status") == "skipped"]:   # old permanent skips: retry them
            BOOK.setdefault("skips", {})[k] = BOOK["trades"].pop(k).get("reason")
        BOOK.setdefault("flags", {})
    return BOOK


def notional(p, cinfo, prices):
    c = cinfo.get(p["symbol"], {})
    return float(p["holdVol"]) * float(c.get("contractSize", 0)) * prices.get(p["symbol"], float(p.get("openAvgPrice") or 0))


def stats_line(eq=None):
    b = load_book()
    n_open = sum(1 for t in b["trades"].values() if t["status"] == "open")
    head = (f"💼 Balance ${eq:,.2f} · " if eq is not None else "") + f"Bot trades open {n_open}" + (f"/{b['max_trades']}" if b["max_trades"] else "")
    try:
        L = ledger()
    except Exception:
        L = None
    if not L:
        return head
    line = lambda g: (f"{g['closed']} closed, {g['wins']} won / {g['losses']} lost ({g['win_rate']}), P/L {g['net']:+.2f}$" if g["closed"] else "none closed yet")
    return (head + f"\n\U0001F916 Bot: {line(L['bot'])}\n\U0001F464 You, from bot signals: {line(L['follow'])}"
            + f"\n\U0001F4C8 Since tracking began: {L['total_net']:+.2f}$ ({L['total_pct']:+.1f}% of ${L['start_equity']:,.2f})")


# ------------------------------------------------------------------------- results ledger (MEXC's own closed-position records)
SIGNAL_WINDOW = 6 * 86_400_000                                  # a manual trade counts as "from a bot signal" if opened within 6 days of it


def note_signal(sym, day, key, kind):
    """Remember every signal the bot announced on a contract, to recognise manual trades that follow it."""
    b = load_book()
    lst = b.setdefault("signals", {}).setdefault(sym, [])
    if not any(x[1] == key for x in lst):
        lst.append([int(day), key, kind])
        del lst[:-20]
        return True
    return False


def history_positions(since):
    out, page = [], 1
    while page <= 20:
        d = api_get("/api/v1/private/position/list/history_positions", {"page_num": page, "page_size": 100, "start_time": since})
        rows = d.get("resultList") if isinstance(d, dict) else d
        rows = rows or []
        out += rows
        if len(rows) < 100:
            break
        page += 1
    return out


def ledger(force=False):
    """Every position since tracking began, classified as bot / manual from a bot signal / manual own, with P/L from MEXC.
    P/L = close P/L - fees + funding (MEXC's numbers). % = P/L / initial margin, as MEXC shows it."""
    if not force and _mem.get("ledger") and time.time() - _mem["ledger_t"] < 60:
        return _mem["ledger"]
    b = load_book()
    flags = b.setdefault("flags", {})
    if "ledger_start" not in flags:
        flags["ledger_start"], flags["start_equity"] = int(time.time() * 1000) - 7 * 86_400_000, round(equity()[0], 4)
    since = max(flags["ledger_start"], 1791450280118)
    bot_pids = {str(t.get("position_id")) for t in b["trades"].values() if t.get("position_id")}
    sig = b.get("signals") or {}
    for t in b["trades"].values():                              # hand-overs and refused orders are bot signals too
        if t.get("status") in ("manual", "failed") and t.get("symbol"):
            lst = sig.setdefault(t["symbol"], [])
            if not any(x[1] == t["key"] for x in lst):
                lst.append([int(t.get("day") or t.get("time", 0)), t["key"], t.get("kind", "crypto")])
    groups = {g: {"closed": 0, "wins": 0, "losses": 0, "net": 0.0, "fees": 0.0, "funding": 0.0, "pct": [], "best": None, "worst": None, "list": []}
              for g in ("bot", "follow", "own")}

    def group_of(p):
        if str(p.get("positionId")) in bot_pids:
            return "bot"
        made = int(p.get("createTime") or 0)
        if int(p.get("positionType") or 1) == 1 and any(0 <= made - s[0] <= SIGNAL_WINDOW for s in sig.get(p["symbol"], [])):
            return "follow"
        return "own"

    for p in history_positions(since):
        if int(p.get("state") or 3) != 3 or int(p.get("createTime") or 0) < since:
            continue
        g = groups[group_of(p)]
        net = float(p.get("closeProfitLoss") or 0) - abs(float(p.get("totalFee") or p.get("fee") or 0)) + float(p.get("holdFee") or 0)
        g["closed"] += 1
        g["wins" if net > 0 else "losses"] += 1
        g["net"] += net
        g["fees"] += abs(float(p.get("totalFee") or p.get("fee") or 0))
        g["funding"] += float(p.get("holdFee") or 0)
        g["best"], g["worst"] = net if g["best"] is None else max(g["best"], net), net if g["worst"] is None else min(g["worst"], net)
        if p.get("profitRatio") is not None:
            g["pct"].append(float(p["profitRatio"]) * 100)
        g["list"].append((int(p.get("updateTime") or 0), p["symbol"], net, float(p.get("profitRatio") or 0) * 100))
    open_pos = positions()
    unreal = {g: 0.0 for g in groups}
    n_open = {g: 0 for g in groups}
    for p in open_pos.values():
        if int(p.get("createTime") or 0) < since and str(p.get("positionId")) not in bot_pids:
            continue
        g = group_of(p)
        unreal[g] += float(p.get("unRealizedPnl") or 0)
        n_open[g] += 1
    for k, g in groups.items():
        g["win_rate"] = f"{100 * g['wins'] / g['closed']:.0f}%" if g["closed"] else "-"
        g["avg_pct"] = sum(g["pct"]) / len(g["pct"]) if g["pct"] else 0.0
        g["open"], g["unrealised"] = n_open[k], unreal[k]
        g["list"].sort(reverse=True)
    total = sum(g["net"] for g in groups.values())
    out = {**groups, "total_net": total, "start_equity": flags["start_equity"],
           "total_pct": 100 * total / flags["start_equity"] if flags["start_equity"] else 0.0, "since": since}
    _mem["ledger"], _mem["ledger_t"] = out, time.time()
    return out


def pnl_text():
    if not (KEY and SECRET):
        return "No MEXC API key is set."
    L, eq = ledger(force=True), equity()[0]
    names = {"bot": "\U0001F916 BOT TRADES", "follow": "\U0001F464 YOUR TRADES FROM BOT SIGNALS", "own": "\U0001F9D1 YOUR OTHER TRADES"}
    lines = ["\U0001F4CA PROFIT & LOSS (from MEXC's closed-position records, after fees and funding)",
             f"Since {time.strftime('%b %d', time.gmtime(L['since'] / 1000))} · balance now ${eq:,.2f}", ""]
    for k in ("bot", "follow", "own"):
        g = L[k]
        lines.append(names[k])
        if g["closed"]:
            lines += [f"  Closed {g['closed']}: {g['wins']} won, {g['losses']} lost ({g['win_rate']})",
                      f"  P/L {g['net']:+.2f}$ · average {g['avg_pct']:+.1f}% on margin",
                      f"  Best {g['best']:+.2f}$ · worst {g['worst']:+.2f}$ · fees {g['fees']:.2f}$ · funding {g['funding']:+.2f}$"]
            lines += [f"    {time.strftime('%b %d', time.gmtime(t / 1000))} {s.replace('_USDT', '')}: {n:+.2f}$ ({pc:+.1f}%)" for t, s, n, pc in g["list"][:5]]
        else:
            lines.append("  None closed yet")
        if g["open"]:
            lines.append(f"  Open now {g['open']}, unrealised {g['unrealised']:+.2f}$")
        lines.append("")
    lines.append(f"TOTAL closed P/L {L['total_net']:+.2f}$ = {L['total_pct']:+.1f}% of ${L['start_equity']:,.2f} (balance when tracking began)")
    lines.append("A trade of yours counts as 'from bot signals' if it is a long on the same contract, opened within 6 days after the bot's signal.")
    return "\n".join(lines)


def max_loss_pct():
    """the owner's max loss per trade, % of equity: the /maxloss setting if made, else MAX_LOSS_PCT."""
    v = load_book().get("max_loss_pct")
    return float(v) if v else MAX_LOSS_PCT


def sizing(eq, price, sl, cinfo, other_notional):
    """Leverage and volume for one trade under the owner's rules. Returns (leverage, vol, notional, reason_if_skipped)."""
    margin = eq * MARGIN_PCT / 100
    stop_frac = (price - sl) / price
    if margin <= 0 or stop_frac <= 0:
        return 0, 0, 0, "price is not above the SL"
    lev_liq = math.floor((LIQ_BUFFER * eq - other_notional) / margin)
    loss_pct = max_loss_pct()
    lev_risk = math.floor(loss_pct / 100 * eq / (margin * stop_frac))
    max_lev = int(cinfo.get("maxLeverage") or 1)
    size, unit, min_vol = float(cinfo["contractSize"]), cinfo.get("volUnit") or 1, float(cinfo.get("minVol") or 1)
    lev = min(lev_liq, lev_risk, max_lev)
    if lev >= 1:
        vol = to_unit(margin * lev / (size * price), unit)
        if vol >= min_vol:
            return lev, vol, vol * size * price, None           # first choice: liquidation stays at 0
    # Owner's rule: when no leverage keeps liquidation at 0, still take the trade, with the LOWEST leverage that
    # reaches the contract's minimum order: that is the farthest liquidation price available.
    lev = math.ceil(min_vol * size * price / margin)
    if lev > lev_risk:
        loss = min_vol * size * price * stop_frac
        return 0, 0, 0, (f"MEXC's smallest order would lose about ${loss:.2f} at the SL ({stop_frac * 100:.0f}% away), "
                         f"more than your {loss_pct:g}% limit of ${loss_pct / 100 * eq:.2f}")
    if lev > max_lev:
        return 0, 0, 0, f"even at this contract's maximum {max_lev}X the order would be below its minimum of about ${min_vol * size * price:.2f}"
    return lev, min_vol, min_vol * size * price, None


def est_liquidation(eq, total_notional, entry):
    """Rough cross-margin liquidation price for a long, if every open bot position fell by the same % as this one.
    None = no liquidation even at a price of zero."""
    if total_notional <= 0 or eq >= total_notional * 1.005:
        return None
    drop = eq / total_notional - 0.005                         # about 0.5% maintenance margin
    return entry * (1 - max(drop, 0.0))


# --------------------------------------------------------------------------------------------- orders
def place_tpsl(pid, vol, tp, sl, cinfo):
    unit = cinfo["priceUnit"]
    body = {"positionId": pid, "vol": vol, "lossTrend": 1, "profitTrend": 1, "volType": 1, "profitLossVolType": "SAME", "priceProtect": "0",
            "takeProfitPrice": to_unit(tp, unit, ROUND_HALF_UP), "takeProfitType": 1, "takeProfitOrderPrice": to_unit(tp, unit, ROUND_HALF_UP),
            "stopLossPrice": to_unit(sl, unit, ROUND_HALF_UP), "stopLossType": 1, "stopLossOrderPrice": to_unit(sl * (1 - SL_LIMIT_OFFSET), unit)}
    return api_post("/api/v1/private/stoporder/place", body)


def move_sl_to_breakeven(t):
    """After TP1: stop loss of what is left goes to entry + fees. Returns the new stop price."""
    cinfo = contracts()[t["symbol"]]
    be = to_unit(t["entry"] * (1 + BE_FEES), cinfo["priceUnit"], ROUND_HALF_UP)
    found = 0
    for o in tpsl_orders(t["symbol"]):
        if str(o.get("positionId")) != str(t["position_id"]) or o.get("isFinished") or int(o.get("state") or 1) != 1:
            continue
        found += 1
        if float(o.get("stopLossPrice") or 0) >= be:
            continue
        tp = float(o.get("takeProfitPrice") or 0)
        body = {"stopPlanOrderId": o["id"], "stopLossPrice": be, "lossTrend": 1}
        if tp > 0:
            body.update(takeProfitPrice=tp, profitTrend=1)
        try:
            api_post("/api/v1/private/stoporder/change_plan_price", body)
        except ApiError:
            if str(o.get("orderId") or "0") == "0":
                raise
            body.pop("stopPlanOrderId")                         # a TP/SL attached to an order is changed through that order
            api_post("/api/v1/private/stoporder/change_price", dict(body, orderId=o["orderId"]))
    if not found:
        raise ApiError("no live TP/SL order found for the position")
    return be


def market_close(pid, symbol, vol, price):
    for o in tpsl_orders(symbol):
        if str(o.get("positionId")) == str(pid) and not o.get("isFinished"):
            try:
                api_post("/api/v1/private/stoporder/cancel", [{"stopPlanOrderId": o["id"]}])
            except ApiError:
                pass
    return api_post("/api/v1/private/order/create", {"symbol": symbol, "price": price, "vol": vol, "side": 4, "type": 5, "openType": 2, "positionId": pid})


def wait_fill(order_id):
    for _ in range(8):
        o = api_get(f"/api/v1/private/order/get/{order_id}")
        if o and int(o.get("state", 0)) in (3, 4, 5):
            return o
        time.sleep(1)
    return api_get(f"/api/v1/private/order/get/{order_id}")


def label(coin, sym, kind):
    return f"{coin} USDT" if kind == "crypto" else f"{sym.replace('_', ' ')} ({coin}, {'stock dip' if kind == 'dip' else 'stock breakout'})"


def open_trade(key, coin, t, eq, cinfo_all, prices, pos, send, persist, sym=None, kind="crypto"):
    b, sym = load_book(), sym or sym_of(coin)
    name = label(coin, sym, kind)
    cinfo, price = cinfo_all[sym], prices[sym]
    r = t["entry"] - t["stop"]
    k1, k2 = (0.3, 0.5) if kind == "dip" else (0.5, 1.0)
    tp1, tp2, sl = t["entry"] + k1 * r, t["entry"] + k2 * r, t["stop"]
    # Owner's choice: only the bot's own open positions count toward the liquidation-0 room; manual positions are ignored.
    bot_pids = {t.get("position_id") for t in b["trades"].values() if t.get("status") == "open"}
    other = sum(notional(p, cinfo_all, prices) for pid, p in pos.items() if pid in bot_pids)
    lev, vol, value, why = sizing(eq, price, sl, cinfo, other)
    if why:                                                     # the bot cannot take it: hand it to the owner as a manual trade
        manual(key, coin, t, kind, sym, why, eq, send, persist)
        return
    b["trades"][key] = trade = {"key": key, "coin": coin, "symbol": sym, "kind": kind, "name": name, "day": t["day"], "status": "opening", "time": int(time.time() * 1000), "leverage": lev, "vol": vol,
                                "sl": sl, "tp1": tp1, "tp2": tp2, "entry_signal": t["entry"]}
    persist()                                                   # recorded before ordering, so a restart can never order twice
    try:
        try:
            api_post("/api/v1/private/position/change_leverage", {"openType": 2, "symbol": sym, "positionType": 1, "leverage": lev})
        except ApiError:
            pass                                                # the order below carries the leverage too
        # SL and TP are attached to the entry order itself (the order call MEXC accepts); 80% carries TP1, 20% carries TP2.
        unit, min_vol = cinfo["priceUnit"], float(cinfo.get("minVol") or 1)
        vol_a = to_unit(vol * TP1_SHARE, cinfo.get("volUnit") or 1)
        split = vol_a >= min_vol and vol - vol_a >= min_vol
        legs = [(vol_a, tp1, "a"), (vol - vol_a, tp2, "b")] if split else [(vol, tp1, "a")]
        filled, cost, pid, err = 0.0, 0.0, None, None
        for v, tp, tag in legs:
            try:
                oid = api_post("/api/v1/private/order/create", {
                    "symbol": sym, "price": price, "vol": v, "leverage": lev, "side": 1, "type": 5, "openType": 2,
                    "stopLossPrice": to_unit(sl, unit, ROUND_HALF_UP), "takeProfitPrice": to_unit(tp, unit, ROUND_HALF_UP),
                    "lossTrend": 1, "profitTrend": 1, "externalOid": ("sb" + key.replace(":", "") + tag)[:32]})["orderId"]
            except ApiError as e:
                err = e
                break
            trade.setdefault("order_ids", []).append(oid)
            o = wait_fill(oid)
            f = float(o.get("dealVol") or 0)
            if f > 0:
                filled, cost, pid = filled + f, cost + f * float(o.get("dealAvgPrice") or price), o["positionId"]
        if filled <= 0:
            trade.update(status="failed", reason=str(err) if err else "entry order not filled")
            if err and "8950" in str(err):                      # "unavailable in your country or region": never try this contract again
                b.setdefault("blocked", [])
                if sym not in b["blocked"]:
                    b["blocked"].append(sym)
            persist()
            reason = ("MEXC does not let the bot open this contract from its server's region (you can, from Pakistan)"
                      if err and "8950" in str(err) else f"the entry order was not accepted ({err or 'not filled'})")
            manual(key, coin, t, kind, sym, reason, eq, send, persist, record=False)
            return
        avg = cost / filled
        trade.update(status="open", position_id=pid, entry=avg, vol=filled, last_hold=filled, split=split and filled > vol_a)
        persist()
        # make sure the exchange really holds an SL for this position; if not, try once to add one, else close
        has_sl = False
        for _ in range(4):
            time.sleep(1)
            try:
                has_sl = any(str(x.get("positionId")) == str(pid) and float(x.get("stopLossPrice") or 0) > 0 for x in tpsl_orders(sym))
            except ApiError:
                pass
            if has_sl:
                break
        if not has_sl:
            try:
                place_tpsl(pid, filled, tp1, sl, cinfo)
                has_sl = True
            except ApiError as e:
                try:
                    market_close(pid, sym, filled, price)
                except ApiError:
                    pass
                trade.update(status="closed", result="no SL possible, closed", pnl=0.0, closed=int(time.time() * 1000))
                persist()
                send(f"⚠️ {name}: no stop loss could be placed ({e}), so the position was closed at once.")
                return
        if err:                                                 # the 20% leg was refused: the 80% leg stands with its own TP1 and SL
            send(f"ℹ️ {name}: the TP2 part was not opened ({err}); the trade runs with TP1 only.")
        margin = value / lev
        loss_at_sl = (avg - sl) / avg * value
        liq = est_liquidation(eq, other + filled * float(cinfo["contractSize"]) * avg, avg)
        liq_text = ("Liquidation: none" if liq is None else
                    f"Liquidation ≈ {liq:g} (cross-margin estimate)" + (" ⚠️ BEFORE the SL" if liq >= sl else ""))
        send(f"\U0001F7E2 TRADE OPENED — {name}\n\nDirection: LONG · Cross {lev}X\nEntry: {avg:g}\nSize: {filled:g} contracts ≈ ${value:,.2f} (margin ${margin:,.2f})\n"
             + (f"\U0001F3AF TP1: {to_unit(tp1, cinfo['priceUnit'], ROUND_HALF_UP):g} (80%)\n\U0001F3AF TP2: {to_unit(tp2, cinfo['priceUnit'], ROUND_HALF_UP):g} (20%)\n" if trade["split"]
                else f"\U0001F3AF TP1: {to_unit(tp1, cinfo['priceUnit'], ROUND_HALF_UP):g} (100%)\n")
             + f"\U0001F6E1 SL: {to_unit(sl, cinfo['priceUnit'], ROUND_HALF_UP):g} (on the exchange)\nLoss if SL is hit ≈ ${loss_at_sl:,.2f} · {liq_text}\n\n" + stats_line(eq))
    except ApiError as e:
        trade.update(status="failed", reason=str(e))
        persist()
        send(f"⚠️ {name}: could not open the trade ({e}).")


def follow(eq, pos, prices, send, persist, state=None):
    """Detect TP / SL fills on the bot's open trades, and close dip trades when their daily exit rule fires."""
    b = load_book()
    for t in [x for x in b["trades"].values() if x["status"] == "open"]:
        p = pos.get(t["position_id"])
        hold = float(p["holdVol"]) if p else 0.0
        t.setdefault("name", f"{t['coin']} USDT")
        d = ((state or {}).get("dips") or {}).get(t["coin"], {}).get("last") if t.get("kind") == "dip" else None
        if d and d.get("day") == t.get("day") and d["status"] in ("exited", "time limit") and hold > 0 and not t.get("manual_close"):
            try:
                market_close(t["position_id"], t["symbol"], hold, prices.get(t["symbol"], t["entry"]))
                t["dip_exit"] = d["status"]
            except ApiError as e:
                send(f"⚠️ {t['name']}: the daily exit could not be sent ({e}). It will be tried again.")
            continue
        if t.get("dip_exit") and hold < t["last_hold"]:          # the bot's own daily exit went through
            price = prices.get(t["symbol"], 0)
            t["pnl"] = round((t.get("pnl") or 0) + (price - t["entry"]) * (t["last_hold"] - hold) * float(contracts()[t["symbol"]]["contractSize"]), 4)
            t["last_hold"] = hold
            if hold <= 0:
                t.update(status="closed", closed=int(time.time() * 1000), result="exit")
                why = "closed above the 20-day average" if t["dip_exit"] == "exited" else "20-trading-day limit reached"
                send(f"\U0001F3C1 {t['name']} EXIT {'✅' if t['pnl'] > 0 else ''}\nPlan finished: {why}. Result ≈ {t['pnl']:+.2f}$\n\n" + stats_line(eq))
            persist()
            continue
        if hold >= t["last_hold"] - 1e-12:
            continue
        price = prices.get(t["symbol"], 0)
        part = t["last_hold"] - hold
        if t.get("manual_close"):                               # closed from /positions: report it as that, at the current price
            t["pnl"] = round((t.get("pnl") or 0) + (price - t["entry"]) * part * float(contracts()[t["symbol"]]["contractSize"]), 4)
            t.update(last_hold=hold, status="closed" if hold <= 0 else "open", closed=int(time.time() * 1000), result="closed by you")
            if hold <= 0:
                send(f"✖ {t['name']} closed by you · result ≈ {t['pnl']:+.2f}$\n\n" + stats_line(eq))
            persist()
            continue
        hit_tp = price >= ((t["tp2"] if t.get("be_moved") else t["tp1"]) + t["sl"]) / 2   # which side of the range price is on when the size dropped
        gain = (t["tp2"] if t.get("tp1_hit") else t["tp1"]) if hit_tp else t["sl"]
        t["pnl"] = round((t.get("pnl") or 0) + (gain - t["entry"]) * part * float(contracts()[t["symbol"]]["contractSize"]), 4)
        t["last_hold"] = hold
        if hit_tp and not t.get("tp1_hit"):
            t["tp1_hit"] = True
            if hold > 0:
                try:
                    t["sl"], t["be_moved"] = move_sl_to_breakeven(t), True
                    sl_text = f"\U0001F512 SL moved to breakeven {t['sl']:g} for the rest (a fast gap through it can still cost a little)."
                except ApiError as e:
                    sl_text = f"\u26A0\uFE0F Could not move the SL to breakeven yet ({e}); retrying. SL is still {t['sl']:g}."
                send(f"\U0001F3AF {t['name']} TP 1 HIT SUCCESSFULLY ✅\U0001F525\n80% closed at about {t['tp1']:g} · profit ≈ {t['pnl']:+.2f}$\n"
                     f"{sl_text}\nStay focused for TP 2 \U0001F680\n\n" + stats_line(eq))
        elif hit_tp:
            t["tp2_hit"] = True
        if hold <= 0:
            t.update(status="closed", closed=int(time.time() * 1000),
                     result=("TP2" if t.get("tp2_hit") else "TP1") if hit_tp else ("TP1 + breakeven" if t.get("be_moved") else "SL"))
            if not hit_tp and t.get("be_moved"):
                send(f"\U0001F512 {t['name']} closed at breakeven after TP 1 \u2705\nTrade complete \u00B7 total profit \u2248 {t['pnl']:+.2f}$\n\n" + stats_line(eq))
            elif hit_tp:
                send(f"\U0001F3AF {t['name']} {'TP 2' if t.get('tp2_hit') else 'TP 1'} HIT SUCCESSFULLY ✅\U0001F525\nTrade complete \U0001F3C6 · profit ≈ {t['pnl']:+.2f}$\n\n" + stats_line(eq))
            else:
                send(f"\U0001F6D1 {t['name']} SL HIT ❌\nLoss ≈ {t['pnl']:+.2f}$, as planned.\n\n" + stats_line(eq))
        persist()


def manual(key, coin, t, kind, sym, reason, eq, send, persist, record=True):
    """The bot cannot open this signal: send the owner the full card to take it by hand, once per signal.
    The signal is then marked as handed over, so the bot never opens it later on top of a manual trade."""
    b = load_book()
    if record:
        if key in b["trades"]:
            return
        b["trades"][key] = {"key": key, "coin": coin, "symbol": sym, "kind": kind, "name": label(coin, sym, kind), "day": t["day"],
                            "status": "manual", "reason": reason, "time": int(time.time() * 1000)}
    persist()
    card = signal_card(coin, kind, t, sym if kind != "crypto" else None, t.get("ma20"), eq, 1.0)
    extra = ""
    try:                                                        # what MEXC's minimum order means at the bot's own margin
        c = contracts()[sym]
        min_order = float(c.get("minVol") or 1) * float(c["contractSize"]) * t["entry"]
        margin = eq * MARGIN_PCT / 100
        extra = (f"\nMEXC minimum order: about ${min_order:,.2f}. With the bot's {MARGIN_PCT:g}% margin (${margin:,.2f}) "
                 f"that needs {math.ceil(min_order / margin)}X leverage" + (f"; this contract allows at most {int(c.get('maxLeverage') or 1)}X." if math.ceil(min_order / margin) > int(c.get("maxLeverage") or 1) else "."))
    except Exception:
        pass
    send(f"✋ TAKE THIS TRADE MANUALLY\nThe bot could not open it: {reason}.\nThe bot will not open it later, so there is no risk of a double trade.\n\n" + card + extra)


def candidates(state, pos_symbols):
    """(key, name, plan, kind, contract symbol) for every signal that can still be entered.
    kind: "crypto" (breakout), "stock" (breakout on a US stock MEXC lists), "dip" (dip rule on a US stock MEXC lists)."""
    b, out, mexc = load_book(), [], state.get("mexc") or {}
    usable = lambda t: t and t["status"] == "open" and t["age_days"] <= SIGNAL_DAYS and not t.get("hits") and not t.get("sl_t")
    for c, p in (state.get("coins") or {}).items():
        t, stock = p.get("fresh"), p.get("market") == "stock"
        if usable(t) and (not stock or c in mexc):
            out.append((f"{c}:{t['day']}", c, t, "stock" if stock else "crypto", mexc[c] if stock else sym_of(c)))
    for c, p in (state.get("dips") or {}).items():
        t = p.get("last")
        if usable(t) and c in mexc:
            out.append((f"dip:{c}:{t['day']}", c, dict(t, ma20=p.get("ma20")), "dip", mexc[c]))
    out = [x for x in out if x[0] not in b["trades"] and x[4] not in pos_symbols]   # blocked contracts are handed over in tick()
    return sorted(out, key=lambda x: (x[3] != "crypto", x[2]["age_days"], x[2]["risk_pct"]))


def tick(state, send, persist):
    if not (KEY and SECRET) or time.time() - _mem["t"] < 15:
        return
    _mem["t"] = time.time()
    b = load_book()
    try:
        eq, _ = equity()
        if not b["flags"].get("connected"):
            mode = api_get("/api/v1/private/position/position_mode")
            mode = mode.get("positionMode") if isinstance(mode, dict) else mode if isinstance(mode, int) else 1
            b["flags"]["connected"] = True
            b["flags"]["key_seen"] = int(time.time() * 1000)
            persist()
            send(f"✅ MEXC futures connected\nFutures balance: ${eq:,.2f}\nPosition mode: {'Hedge' if mode == 1 else 'One-way'}\nMax open trades: {b['max_trades']}\n"
                 f"Margin per trade: {MARGIN_PCT:g}% · Max loss per trade: {max_loss_pct():g}%\nThe bot will open trades on new crypto signals, and on stock signals for stocks MEXC lists, from now on.")
        cinfo, prices, pos = contracts(), tickers(), positions()
        for t in [x for x in b["trades"].values() if x["status"] == "open" and x.get("tp1_hit") and not x.get("be_moved")]:
            if pos.get(t["position_id"]):
                try:
                    t["sl"], t["be_moved"] = move_sl_to_breakeven(t), True
                    persist()
                    send(f"\U0001F512 {t.get('name', t['coin'])}: SL moved to breakeven {t['sl']:g} (TP 1 was already hit).")
                except ApiError:
                    pass
        follow(eq, pos, prices, send, persist, state)
        if b["paused"]:
            return
        if eq < MIN_BALANCE:
            if not b["flags"].get("low"):
                b["flags"]["low"] = True
                persist()
                send(f"⛔ Futures balance ${eq:,.2f} is under ${MIN_BALANCE:g}. No new trades until it is back above.")
            return
        if b["flags"].get("low"):
            b["flags"]["low"] = False
            persist()
        seen_day = b["flags"].get("key_seen", 0)
        if seen_day and time.time() * 1000 - seen_day > 80 * 86_400_000 and not b["flags"].get("renew_warned"):
            b["flags"]["renew_warned"] = True
            persist()
            send("\U0001F511 Your MEXC API key is about 80 days old. Keys without a linked IP expire after 90 days: renew it under API Management → My API Key → Renew.")
        open_syms = {p["symbol"] for p in pos.values()}
        noted = False
        for key, coin, t, kind, sym, *_ in [c for c in candidates(state, set())]:   # every signal, so manual trades can be matched
            noted = note_signal(sym, t["day"], key, kind) or noted
        if noted:
            persist()
        for key, coin, t, kind, sym in candidates(state, open_syms):
            if b["max_trades"] and len(pos) >= b["max_trades"]:
                break
            if sym in {p["symbol"] for p in pos.values()}:
                continue
            if sym not in cinfo or not cinfo[sym].get("apiAllowed") or sym not in prices:
                continue
            price = prices[sym]
            if not (t["stop"] < price <= t["entry"] + 0.25 * (t["entry"] - t["stop"])):
                continue                                        # outside the entry zone right now; checked again next time
            if sym in set(b.get("blocked") or []):              # MEXC refuses this contract for the bot: hand it over
                manual(key, coin, t, kind, sym, "MEXC does not let the bot open this contract from its server's region (you can, from Pakistan)", eq, send, persist)
                continue
            open_trade(key, coin, t, eq, cinfo, prices, pos, send, persist, sym, kind)
            pos = positions()
            eq, _ = equity()
    except ApiError as e:
        last = _mem["err_t"].get(str(e), 0)
        if time.time() - last > 3600:
            _mem["err_t"][str(e)] = time.time()
            send(f"⚠️ MEXC API problem: {e}\nThe bot keeps retrying; open positions keep their exchange-side TP/SL.")
    except requests.RequestException:
        pass


# --------------------------------------------------------------------------------------------- Telegram commands
def positions_view():
    """(text, keyboard rows) for /positions."""
    if not (KEY and SECRET):
        return "No MEXC API key is set.", []
    pos, prices = positions(), tickers()
    if not pos:
        return "No open futures positions.", []
    b = load_book()
    by_pid = {t.get("position_id"): t for t in b["trades"].values() if t["status"] == "open"}
    lines, rows = ["\U0001F4C8 OPEN FUTURES POSITIONS", "Tick positions, then choose an action.", ""], []
    for pid, p in pos.items():
        sym, avg = p["symbol"], float(p.get("openAvgPrice") or p.get("holdAvgPrice") or 0)
        now = prices.get(sym, avg)
        side = 1 if int(p.get("positionType", 1)) == 1 else -1
        move = side * (now / avg - 1) * 100 if avg else 0
        im = float(p.get("im") or p.get("oim") or 0)
        pnl = float(p.get("unRealizedPnl") or side * (now - avg) * float(p["holdVol"]) * float(contracts().get(sym, {}).get("contractSize", 0)))
        on_margin = pnl / im * 100 if im else 0
        tag = " (bot)" if pid in by_pid else ""
        lines.append(f"{sym.replace('_', ' ')}{tag} {'LONG' if side == 1 else 'SHORT'} {p.get('leverage')}X: price {move:+.2f}% · P/L {pnl:+.2f}$ ({on_margin:+.1f}% of margin)")
        rows.append([{"text": f"☐ {sym.split('_')[0]} {on_margin:+.1f}%", "callback_data": f"t:{pid}"}])
    rows.append([{"text": "\U0001F6D1 Close selected", "callback_data": "close"}, {"text": f"➕ Add {MARGIN_PCT:g}% margin", "callback_data": "add"}])
    rows.append([{"text": "\U0001F504 Refresh", "callback_data": "ref"}])
    return "\n".join(lines), rows


def ask_confirm(action, pids):
    token = f"{int(time.time())}{len(_mem['pending'])}"
    _mem["pending"][token] = (action, pids, time.time())
    return token


def run_confirmed(token):
    item = _mem["pending"].pop(token, None)
    if not item or time.time() - item[2] > 600:
        return "This confirmation expired. Send /positions again."
    action, pids, _ = item
    pos, prices, cinfo, out = positions(), tickers(), contracts(), []
    eq, _ = equity()
    b = load_book()
    for pid in pids:
        p = pos.get(pid)
        if not p:
            out.append(f"{pid}: already closed")
            continue
        sym = p["symbol"]
        try:
            if action == "close":
                market_close(pid, sym, float(p["holdVol"]), prices[sym])
                for t in b["trades"].values():
                    if t.get("position_id") == pid and t["status"] == "open":
                        t["manual_close"] = True
                out.append(f"\U0001F6D1 {sym.replace('_', ' ')}: closed at market")
            else:
                lev, c, price = int(p["leverage"]), cinfo[sym], prices[sym]
                margin = eq * MARGIN_PCT / 100
                bot_pids = {t.get("position_id") for t in b["trades"].values() if t.get("status") == "open"}
                other = sum(notional(q, cinfo, prices) for qid, q in pos.items() if qid in bot_pids)   # manual positions ignored
                if other + margin * lev > LIQ_BUFFER * eq:
                    out.append(f"⛔ {sym.replace('_', ' ')}: not added, liquidation would no longer be 0")
                    continue
                vol = to_unit(margin * lev / (float(c["contractSize"]) * price), c.get("volUnit") or 1)
                if vol < float(c.get("minVol") or 1):
                    out.append(f"⛔ {sym.replace('_', ' ')}: {MARGIN_PCT:g}% margin is below the contract minimum")
                    continue
                t = next((t for t in b["trades"].values() if t.get("position_id") == pid and t["status"] == "open"), None)
                body = {"symbol": sym, "price": price, "vol": vol, "leverage": lev, "side": 1, "type": 5, "openType": 2}
                if t:                                           # the added part gets the trade's own SL and TP1
                    body.update(stopLossPrice=to_unit(t["sl"], c["priceUnit"], ROUND_HALF_UP), takeProfitPrice=to_unit(t["tp1"], c["priceUnit"], ROUND_HALF_UP), lossTrend=1, profitTrend=1)
                oid = api_post("/api/v1/private/order/create", body)["orderId"]
                o = wait_fill(oid)
                filled = float(o.get("dealVol") or 0)
                if filled and t:
                    t["last_hold"] = t.get("last_hold", 0) + filled
                out.append(f"➕ {sym.replace('_', ' ')}: added {filled:g} contracts at {lev}X" + ("" if t else " (not a bot trade: no TP/SL was added)"))
        except ApiError as e:
            out.append(f"⚠️ {sym.replace('_', ' ')}: {e}")
    BOOK_DIRTY[0] = True
    return "\n".join(out) or "Nothing to do."


BOOK_DIRTY = [False]


def account_text():
    if not (KEY and SECRET):
        return "No MEXC API key is set."
    eq, avail = equity()
    b = load_book()
    recent = sorted([t for t in b["trades"].values() if t["status"] in ("closed", "skipped", "failed", "manual")], key=lambda t: -t.get("closed", t.get("time", 0)))[:8]
    lines = ["\U0001F4BC FUTURES ACCOUNT", f"Equity ${eq:,.2f} · available ${avail:,.2f}", f"Trading: {'PAUSED' if b['paused'] else 'on'} · max open trades {b['max_trades']}",
             f"Margin per trade {MARGIN_PCT:g}% · max loss per trade {max_loss_pct():g}%", "", stats_line(eq), ""]
    if recent:
        lines.append("Recent:")
        lines += [f"  {t['coin']}: {t.get('result') or t['status']}" + (f" {t['pnl']:+.2f}$" if t.get("pnl") is not None and t["status"] == "closed" else "")
                  + (f" ({t['reason'][:80]})" if t["status"] in ("skipped", "failed", "manual") else "") for t in recent]
    return "\n".join(lines)
