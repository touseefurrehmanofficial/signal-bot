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
        - the leverage at which hitting the SL loses at most MAX_LOSS_PCT of equity (default 1%).
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
import time
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
BOOK_FILE = HERE / "trades.json"
BASE = "https://api.mexc.com"
KEY, SECRET = os.environ.get("MEXC_API_KEY", ""), os.environ.get("MEXC_API_SECRET", "")
STYLE = os.environ.get("TARGET_STYLE", "1")
MARGIN_PCT = float(os.environ.get("MARGIN_PCT", "1"))
MAX_LOSS_PCT = float(os.environ.get("MAX_LOSS_PCT", "1"))
MIN_BALANCE = float(os.environ.get("MIN_BALANCE", "1"))
LIQ_BUFFER = 0.9          # total open notional kept under 90% of equity: no liquidation price, with room for fees and funding
SL_LIMIT_OFFSET = 0.005   # SL limit order price 0.5% under the SL trigger
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
    done = [t for t in b["trades"].values() if t["status"] == "closed"]
    wins = [t for t in done if (t.get("pnl") or 0) > 0]
    tp_hits = sum(1 for t in b["trades"].values() for k in ("tp1_hit", "tp2_hit") if t.get(k))
    sl_hits = sum(1 for t in done if t.get("result") == "SL")
    n_open = sum(1 for t in b["trades"].values() if t["status"] == "open")
    head = (f"💼 Balance ${eq:,.2f} · " if eq is not None else "") + f"Open {n_open}/{b['max_trades']}"
    body = f"📊 Trades {len(done) + n_open} · TP hits {tp_hits} · SL hits {sl_hits}"
    if done:
        body += f" · Win rate {100 * len(wins) / len(done):.0f}% · P/L {sum(t.get('pnl') or 0 for t in done):+.2f}$"
    return head + "\n" + body


def sizing(eq, price, sl, cinfo, other_notional):
    """Leverage and volume for one trade under the owner's rules. Returns (leverage, vol, notional, reason_if_skipped)."""
    margin = eq * MARGIN_PCT / 100
    stop_frac = (price - sl) / price
    if margin <= 0 or stop_frac <= 0:
        return 0, 0, 0, "price is not above the SL"
    lev_liq = math.floor((LIQ_BUFFER * eq - other_notional) / margin)
    lev_risk = math.floor(MAX_LOSS_PCT / 100 * eq / (margin * stop_frac))
    lev = min(lev_liq, lev_risk, int(cinfo.get("maxLeverage") or 1))
    if lev < 1:
        return 0, 0, 0, (f"no leverage keeps liquidation at 0 (equity ${eq:,.2f}, bot positions ${other_notional:,.2f}, margin ${margin:,.2f})" if lev_liq < 1 else f"the SL is {stop_frac * 100:.0f}% away, too far for a {MAX_LOSS_PCT:g}% loss limit")
    size = float(cinfo["contractSize"])
    vol = to_unit(margin * lev / (size * price), cinfo.get("volUnit") or 1)
    if vol < float(cinfo.get("minVol") or 1):
        return lev, 0, 0, f"the order would be ${margin * lev:.2f}, below this contract's minimum of about ${float(cinfo.get('minVol') or 1) * size * price:.2f}"
    return lev, vol, vol * size * price, None


# --------------------------------------------------------------------------------------------- orders
def place_tpsl(pid, vol, tp, sl, cinfo):
    unit = cinfo["priceUnit"]
    body = {"positionId": pid, "vol": vol, "lossTrend": 1, "profitTrend": 1, "volType": 1, "profitLossVolType": "SAME", "priceProtect": "0",
            "takeProfitPrice": to_unit(tp, unit, ROUND_HALF_UP), "takeProfitType": 1, "takeProfitOrderPrice": to_unit(tp, unit, ROUND_HALF_UP),
            "stopLossPrice": to_unit(sl, unit, ROUND_HALF_UP), "stopLossType": 1, "stopLossOrderPrice": to_unit(sl * (1 - SL_LIMIT_OFFSET), unit)}
    return api_post("/api/v1/private/stoporder/place", body)


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


def open_trade(key, coin, t, eq, cinfo_all, prices, pos, send, persist):
    b, sym = load_book(), sym_of(coin)
    cinfo, price = cinfo_all[sym], prices[sym]
    r = t["entry"] - t["stop"]
    tp1, tp2, sl = t["entry"] + 0.5 * r, t["entry"] + r, t["stop"]
    # Owner's choice: only the bot's own open positions count toward the liquidation-0 room; manual positions are ignored.
    bot_pids = {t.get("position_id") for t in b["trades"].values() if t.get("status") == "open"}
    other = sum(notional(p, cinfo_all, prices) for pid, p in pos.items() if pid in bot_pids)
    lev, vol, value, why = sizing(eq, price, sl, cinfo, other)
    if why:                                                     # not permanent: tried again while the signal is fresh; message sent once per reason
        skips = b.setdefault("skips", {})
        kind = why.split(" (")[0].split(",")[0]                 # the reason without its live numbers
        if (skips.get(key) or "").split(" (")[0].split(",")[0] != kind:
            skips[key] = why
            persist()
            send(f"⏭ {coin} USDT signal not taken (yet)\nReason: {why}.\nThe bot keeps checking while the signal is fresh.")
        return
    b["trades"][key] = trade = {"key": key, "coin": coin, "symbol": sym, "status": "opening", "time": int(time.time() * 1000), "leverage": lev, "vol": vol,
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
            persist()
            send(f"⚠️ {coin} USDT: the entry order was not accepted ({err or 'not filled'}). Nothing is open.")
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
                send(f"⚠️ {coin} USDT: no stop loss could be placed ({e}), so the position was closed at once.")
                return
        if err:                                                 # the 20% leg was refused: the 80% leg stands with its own TP1 and SL
            send(f"ℹ️ {coin} USDT: the TP2 part was not opened ({err}); the trade runs with TP1 only.")
        margin = value / lev
        loss_at_sl = (avg - sl) / avg * value
        send(f"\U0001F7E2 TRADE OPENED — {coin} USDT\n\nDirection: LONG · Cross {lev}X\nEntry: {avg:g}\nSize: {filled:g} contracts ≈ ${value:,.2f} (margin ${margin:,.2f})\n"
             + (f"\U0001F3AF TP1: {to_unit(tp1, cinfo['priceUnit'], ROUND_HALF_UP):g} (80%)\n\U0001F3AF TP2: {to_unit(tp2, cinfo['priceUnit'], ROUND_HALF_UP):g} (20%)\n" if trade["split"]
                else f"\U0001F3AF TP1: {to_unit(tp1, cinfo['priceUnit'], ROUND_HALF_UP):g} (100%)\n")
             + f"\U0001F6E1 SL: {to_unit(sl, cinfo['priceUnit'], ROUND_HALF_UP):g} (on the exchange)\nLoss if SL is hit ≈ ${loss_at_sl:,.2f} · Liquidation: none\n\n" + stats_line(eq))
    except ApiError as e:
        trade.update(status="failed", reason=str(e))
        persist()
        send(f"⚠️ {coin} USDT: could not open the trade ({e}).")


def follow(eq, pos, prices, send, persist):
    """Detect TP / SL fills on the bot's open trades."""
    b = load_book()
    for t in [x for x in b["trades"].values() if x["status"] == "open"]:
        p = pos.get(t["position_id"])
        hold = float(p["holdVol"]) if p else 0.0
        if hold >= t["last_hold"] - 1e-12:
            continue
        price = prices.get(t["symbol"], 0)
        part = t["last_hold"] - hold
        if t.get("manual_close"):                               # closed from /positions: report it as that, at the current price
            t["pnl"] = round((t.get("pnl") or 0) + (price - t["entry"]) * part * float(contracts()[t["symbol"]]["contractSize"]), 4)
            t.update(last_hold=hold, status="closed" if hold <= 0 else "open", closed=int(time.time() * 1000), result="closed by you")
            if hold <= 0:
                send(f"✖ {t['coin']} USDT closed by you · result ≈ {t['pnl']:+.2f}$\n\n" + stats_line(eq))
            persist()
            continue
        hit_tp = price >= (t["tp1"] + t["sl"]) / 2                # which side of the range price is on when the size dropped
        gain = (t["tp2"] if t.get("tp1_hit") else t["tp1"]) if hit_tp else t["sl"]
        t["pnl"] = round((t.get("pnl") or 0) + (gain - t["entry"]) * part * float(contracts()[t["symbol"]]["contractSize"]), 4)
        t["last_hold"] = hold
        if hit_tp and not t.get("tp1_hit"):
            t["tp1_hit"] = True
            if hold > 0:
                send(f"\U0001F3AF {t['coin']} USDT TP 1 HIT SUCCESSFULLY ✅\U0001F525\n80% closed at about {t['tp1']:g} · profit ≈ {t['pnl']:+.2f}$\n"
                     f"SL still {t['sl']:g} for the rest. Stay focused for TP 2 \U0001F680\n\n" + stats_line(eq))
        elif hit_tp:
            t["tp2_hit"] = True
        if hold <= 0:
            t.update(status="closed", closed=int(time.time() * 1000), result=("TP2" if t.get("tp2_hit") else "TP1") if hit_tp else "SL")
            if hit_tp:
                send(f"\U0001F3AF {t['coin']} USDT {'TP 2' if t.get('tp2_hit') else 'TP 1'} HIT SUCCESSFULLY ✅\U0001F525\nTrade complete \U0001F3C6 · profit ≈ {t['pnl']:+.2f}$\n\n" + stats_line(eq))
            else:
                send(f"\U0001F6D1 {t['coin']} USDT SL HIT ❌\nLoss ≈ {t['pnl']:+.2f}$, as planned.\n\n" + stats_line(eq))
        persist()


def candidates(state, pos_symbols):
    b, out = load_book(), []
    for c, p in (state.get("coins") or {}).items():
        t = p.get("fresh")
        if p.get("market") == "stock" or not t or t["status"] != "open" or t["age_days"] > SIGNAL_DAYS or t.get("hits") or t.get("sl_t"):
            continue
        key = f"{c}:{t['day']}"
        if key in b["trades"] or sym_of(c) in pos_symbols:
            continue
        out.append((key, c, t))
    return sorted(out, key=lambda x: (x[2]["age_days"], x[2]["risk_pct"]))


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
                 f"Margin per trade: {MARGIN_PCT:g}% · Max loss per trade: {MAX_LOSS_PCT:g}%\nThe bot will open trades on new crypto signals from now on.")
        cinfo, prices, pos = contracts(), tickers(), positions()
        follow(eq, pos, prices, send, persist)
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
        for key, coin, t in candidates(state, open_syms):
            if len(pos) >= b["max_trades"]:
                break
            sym = sym_of(coin)
            if sym not in cinfo or not cinfo[sym].get("apiAllowed") or sym not in prices:
                continue
            price = prices[sym]
            if not (t["stop"] < price <= t["entry"] + 0.25 * (t["entry"] - t["stop"])):
                continue                                        # outside the entry zone right now; checked again next time
            open_trade(key, coin, t, eq, cinfo, prices, pos, send, persist)
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
    recent = sorted([t for t in b["trades"].values() if t["status"] in ("closed", "skipped", "failed")], key=lambda t: -t.get("closed", t.get("time", 0)))[:8]
    lines = ["\U0001F4BC FUTURES ACCOUNT", f"Equity ${eq:,.2f} · available ${avail:,.2f}", f"Trading: {'PAUSED' if b['paused'] else 'on'} · max open trades {b['max_trades']}",
             f"Margin per trade {MARGIN_PCT:g}% · max loss per trade {MAX_LOSS_PCT:g}%", "", stats_line(eq), ""]
    if recent:
        lines.append("Recent:")
        lines += [f"  {t['coin']}: {t.get('result') or t['status']}" + (f" {t['pnl']:+.2f}$" if t.get("pnl") is not None and t["status"] == "closed" else "")
                  + (f" ({t['reason']})" if t["status"] in ("skipped", "failed") else "") for t in recent]
    return "\n".join(lines)
