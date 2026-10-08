"""Shared logic for the scanner: one coin's trade plans from daily candles, plus the demo-account ledger.

The rule (tested in pipeline/21_exit_variants.py and 22_universe_test.py):
  SIGNAL  a day closes above the highest close of the previous 55 days
          (and the coin traded at least $1M a day on average over the previous 30 days)
  ENTRY   that day's close
  STOP    the lowest close of the 20 days before the signal, as a resting order
  TARGET  entry + k x (entry - stop) for k in 0.5, 1, 2, 3; or no target and leave on a
          daily close below the trailing 20-day low
  A coin gets a new plan only on a signal day that comes after its previous plan finished.
  FILTER  a signal counts only if Bitcoin closed above its own 200-day average that day
          (pipeline/25_btc_filter.py: since 2022 the signals this rejects lost money)

Uses only public market data. No keys, no orders.
"""
import time

import numpy as np
import pandas as pd
import requests

# data-api.binance.vision is Binance's public market-data mirror; unlike api.binance.com it also answers from US-hosted servers.
APIS = ["https://data-api.binance.vision/api/v3", "https://api.binance.com/api/v3"]
STYLES = ("0.5", "1", "2", "3", "trail")
MIN_VALUE = 1_000_000
LEVERAGE = 50            # for the "what if I used 50x" column of the demo account
DAY = 86_400_000
DIP_LIMIT = 20           # the dip rule leaves after 20 trading days if nothing else has happened
DIP_RECENT = 60          # days of finished dip trades kept for the dashboard


def klines(coin, limit=500):
    last = None
    for base in APIS:
        try:
            r = requests.get(base + "/klines", params={"symbol": coin + "USDT", "interval": "1d", "limit": limit}, timeout=20)
            if r.status_code == 200:
                df = pd.DataFrame(r.json()).iloc[:, :6].astype(float)
                df.columns = ["t", "open", "high", "low", "close", "volume"]
                return df
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = str(e)
    raise RuntimeError(f"{coin}: {last}")


def klines_stock(ticker):
    """Daily candles for a US stock or ETF from Yahoo Finance (adjusted for splits and dividends)."""
    import yfinance as yf
    d = yf.download(ticker, period="3y", interval="1d", progress=False, auto_adjust=True)
    if d is None or d.empty:
        raise RuntimeError(f"{ticker}: no data")
    d.columns = [c[0].lower() if isinstance(c, tuple) else str(c).lower() for c in d.columns]
    d = d.dropna(subset=["close"])
    df = pd.DataFrame({"t": (d.index.astype("int64") // 1_000_000).astype(float), "open": d.open.values, "high": d.high.values,
                       "low": d.low.values, "close": d.close.values, "volume": d.volume.values})
    return _with_placeholder(df)


def _with_placeholder(df):
    """plan() and dip_plan() drop the last row as "still forming". After the US close (21:15 UTC) and on
    weekends the last row is final, so a copy is added for them to drop instead."""
    now = pd.Timestamp.now(tz="UTC")
    last_day = pd.Timestamp(df.t.iloc[-1], unit="ms", tz="UTC").date()
    if not (last_day == now.date() and (now.hour, now.minute) < (21, 15)):
        df = pd.concat([df, df.iloc[[-1]].assign(t=df.t.iloc[-1] + DAY)], ignore_index=True)
    return df


def stock_frames(tickers, period="2y"):
    """Daily candles for many US stocks and funds in a few requests (Yahoo Finance, adjusted)."""
    import yfinance as yf
    out = {}
    for k in range(0, len(tickers), 100):
        chunk = tickers[k:k + 100]
        try:
            d = yf.download(chunk, period=period, interval="1d", progress=False, auto_adjust=True, group_by="ticker", threads=True)
        except Exception:
            continue
        for t in chunk:
            try:
                x = d[t].dropna(subset=["Close"])
                if len(x) < 260:
                    continue
                ms = ((x.index - pd.Timestamp(0)) // pd.Timedelta(milliseconds=1)).astype(float)
                out[t] = _with_placeholder(pd.DataFrame({"t": ms, "open": x["Open"].values, "high": x["High"].values, "low": x["Low"].values,
                                                         "close": x["Close"].values, "volume": x["Volume"].values}))
            except Exception:
                pass
    return out


def mexc_stocks():
    """US-stock tickers that MEXC lists as perpetual futures: ticker -> MEXC contract symbol. Public endpoint, no key."""
    data = requests.get("https://contract.mexc.com/api/v1/contract/detail", timeout=30).json()["data"]
    fix = {"BRKB": "BRK-B", "BFB": "BF-B"}
    return {fix.get(x["baseCoinName"], x["baseCoinName"]): x["symbol"] for x in data
            if "mc-trade-zone-Stock" in (x.get("conceptPlate") or []) and x.get("state") == 0}


def market_z(spy_df):
    """day -> how far the S&P 500 fund (SPY) closed from its 20-day average, in standard deviations.
    Dip signals on days when the whole market is down too (below -1.5) tested better than stock-only dips
    (pipeline/31: 71% won against 64%)."""
    d = spy_df.iloc[:-1]
    z = (d.close - d.close.rolling(20).mean()) / d.close.rolling(20).std()
    return {int(t): round(float(v), 2) for t, v in zip(d.t, z) if not np.isnan(v)}


def dip_plan(ticker, df, mkt=None):
    """The dip rule (tested in pipeline/28, 29 and 30), replayed over the candles exactly as in the test:
      SIGNAL  a day closes above its 200-day average AND below its lower band (20-day average minus 2 standard deviations);
              on a market-wide dip day (S&P 500 more than 1.5 sd below its own 20-day average) 1 standard deviation is enough
              (pipeline/33 and 34: more signals, and they won more often than stock-only dips)
      ENTRY   that day's close            STOP  entry minus 3 x the average daily range of the last 20 days
      EXIT    the first later day that closes above its 20-day average, or after 20 trading days
      One trade at a time per instrument."""
    live_price = float(df.close.iloc[-1])
    df = df.iloc[:-1].reset_index(drop=True)
    n = len(df)
    if n < 230:
        return None
    c, h, l = df.close.values, df.high.values, df.low.values
    pc = df.close.shift()
    atr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1).rolling(20).mean().values
    ma20, ma200 = df.close.rolling(20).mean().values, df.close.rolling(200).mean().values
    sd = df.close.rolling(20).std().values
    band = ma20 - 2 * sd
    wide = np.array([(mkt or {}).get(int(t), 0.0) < -1.5 for t in df.t])
    signal = (c < band) | (wide & (c < ma20 - sd))
    r6 = lambda x: float(f"{x:.6g}")
    last, recent, i = None, [], 200
    while i < n:
        if np.isnan(ma200[i]) or np.isnan(atr[i]) or not (c[i] > ma200[i] and signal[i]) or 3 * atr[i] / c[i] < 0.005:
            i += 1
            continue
        entry, stop = float(c[i]), float(c[i] - 3 * atr[i])
        risk = entry - stop
        status, when, r_mult, j = "open", None, None, i
        for j in range(i + 1, min(n, i + DIP_LIMIT + 1)):
            if l[j] <= stop:
                status, when, r_mult = "stopped", int(df.t[j]), -1.0
                break
            if c[j] > ma20[j]:
                status, when, r_mult = "exited", int(df.t[j]), round(float((c[j] - entry) / risk), 3)
                break
            if j - i >= DIP_LIMIT:
                status, when, r_mult = "time limit", int(df.t[j]), round(float((c[j] - entry) / risk), 3)
        liq = entry * (1 - 1 / LEVERAGE)
        hit = [k for k in range(i + 1, (j if status != "open" else n - 1) + 1) if l[k] <= liq]
        if hit:
            x50 = {"status": "liquidated", "when": int(df.t[hit[0]]), "pnl": -1.0}
        elif status == "open":
            x50 = {"status": "open", "when": None, "pnl": None}
        else:
            x50 = {"status": status, "when": when, "pnl": round(LEVERAGE * r_mult * risk / entry, 2)}
        last = {"day": int(df.t[i]), "age_days": int(n - 1 - i), "entry": r6(entry), "stop": r6(stop), "risk_pct": round(risk / entry * 100, 2),
                "status": status, "when": when, "R": r_mult, "x50": x50, "mkt_z": (mkt or {}).get(int(df.t[i]))}
        if n - 1 - i <= DIP_RECENT:
            recent.append(last)
        if status == "open":
            break
        i = j + 1
    return {"coin": ticker, "price": r6(live_price), "last_close_day": int(df.t.iloc[-1]), "ma20": r6(ma20[-1]), "band": r6(band[-1]),
            "up": bool(c[-1] > ma200[-1]), "last": last, "recent": recent}


def btc_regime():
    """day timestamp -> True when Bitcoin closed above its 200-day average; plus a summary for the dashboard."""
    df = klines("BTC", 1000).iloc[:-1].reset_index(drop=True)
    ma = df.close.rolling(200).mean()
    ok = {int(t): bool(c > m) for t, c, m in zip(df.t, df.close, ma) if not np.isnan(m)}
    days_in_state = 1
    vals = list(ok.values())
    while days_in_state < len(vals) and vals[-1 - days_in_state] == vals[-1]:
        days_in_state += 1
    return ok, {"on": bool(vals[-1]), "btc_close": float(df.close.iloc[-1]), "btc_avg200": float(ma.iloc[-1]), "days": days_in_state}


def plan(coin, regime, market="crypto"):
    """market="stock": same rule on a US stock or ETF. No Bitcoin filter and no minimum-volume rule (these are large, liquid names)."""
    stock = market == "stock"
    df = klines_stock(coin) if stock else klines(coin)
    live_price = float(df.close.iloc[-1])
    df = df.iloc[:-1].reset_index(drop=True)                 # completed days only
    if len(df) < 80:
        return None
    c, h, l, n = df.close.values, df.high.values, df.low.values, len(df)
    hi55 = df.close.rolling(55).max().shift(1).values
    lo20 = df.close.rolling(20).min().shift(1).values
    value = (df.close * df.volume).rolling(30).mean().shift(1).values
    if stock:
        value = np.full(n, MIN_VALUE, dtype=float)
        regime = {int(t): True for t in df.t}
    out = {"coin": coin, "market": market, "price": live_price, "next_entry": float(np.max(c[-55:])), "next_stop": float(np.min(c[-20:])),
           "last_close_day": int(df.t.iloc[-1]), "plans": {}, "recent": {}}
    for key in STYLES:
        tp = None if key == "trail" else float(key)
        last, recent, i = None, [], 56
        while i < n:
            if (np.isnan(hi55[i]) or not (c[i] > hi55[i]) or np.isnan(lo20[i]) or lo20[i] >= c[i] or not (value[i] >= MIN_VALUE)
                    or not regime.get(int(df.t[i]), False)):
                i += 1
                continue
            entry, stop = float(c[i]), float(lo20[i])
            risk = entry - stop
            status, when, r_mult, j = "open", None, None, i
            for j in range(i + 1, n):
                if l[j] <= stop:                              # the stop is checked before the target, as in the test
                    status, when, r_mult = "stopped", int(df.t[j]), -1.0
                    break
                if tp is not None and h[j] >= entry + tp * risk:
                    status, when, r_mult = "target", int(df.t[j]), tp
                    break
                if tp is None and c[j] < lo20[j]:
                    status, when, r_mult = "trailed out", int(df.t[j]), float((c[j] - entry) / risk)
                    break
            # The same trade taken as $1 of margin at 50x: wiped out the first day price trades 1/50 = 2% below entry.
            liq = entry * (1 - 1 / LEVERAGE)
            end = j if status != "open" else n - 1
            hit = [k for k in range(i + 1, end + 1) if l[k] <= liq]
            if hit:
                x50 = {"status": "liquidated", "when": int(df.t[hit[0]]), "pnl": -1.0}
            elif status == "open":
                x50 = {"status": "open", "when": None, "pnl": None}
            else:
                x50 = {"status": status, "when": when, "pnl": round(LEVERAGE * r_mult * risk / entry, 2)}
            last = {"day": int(df.t[i]), "age_days": int(n - 1 - i), "entry": entry, "stop": stop, "risk_pct": risk / entry * 100,
                    "status": status, "when": when, "R": r_mult, "x50": x50, "trail_level": float(np.min(c[-20:]))}
            if n - 1 - i <= 120:
                recent.append({k: last[k] for k in ("day", "entry", "stop", "risk_pct", "status", "when", "R", "x50")})
            if status == "open":
                break
            i = j + 1
        out["plans"][key] = last
        out["recent"][key] = recent
    return out


def update_ledger(ledger, coins_state, style, dips=None):
    """Demo account: every signal whose day closes AFTER the ledger was started becomes a paper trade.
    Nothing from before the start date is added, so the record is a genuine forward test."""
    if not ledger:
        ledger = {"started": int(time.time() * 1000), "style": style, "start_balance": 100.0, "risk_per_trade": 1.0,
                  "leverage": LEVERAGE, "trades": {}}
    if ledger.get("style") != style:                          # a different target style is a different experiment
        ledger.update(style=style, started=int(time.time() * 1000), trades={})
    for coin, p in coins_state.items():
        for t in (p.get("recent") or {}).get(style, []):
            if t["day"] + DAY >= ledger["started"]:           # the signal day closed after the ledger began
                ledger["trades"][f"{coin}:{t['day']}"] = {"coin": coin, **t}
    for ticker, p in (dips or {}).items():                   # dip-rule trades, the same forward-only way
        for t in p.get("recent") or []:
            if t["day"] + DAY >= ledger["started"]:
                ledger["trades"][f"dip:{ticker}:{t['day']}"] = {"coin": ticker, "rule": "dip", **t}
    return ledger
