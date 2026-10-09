"""Run one scan: update docs/state.json and send phone alerts for anything new.

Runs anywhere Python runs: on a schedule in GitHub Actions (laptop off), or by hand.

Alerts go to Telegram if two environment variables are set:
  TELEGRAM_TOKEN    the token BotFather gave you for your bot
  TELEGRAM_CHAT_ID  your chat id with that bot
Without them the script still updates docs/state.json and prints what it would have sent.

Optional:
  TARGET_STYLE  0.5 | 1 | 2 | 3 | trail   (default 1)   which target the alerts use
  ACCOUNT_SIZE  default 1000   and   RISK_PCT  default 1   (only used to print a position size)
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from core import btc_regime, dip_plan, market_z, mexc_stocks, plan, signal_card, stock_frames, update_ledger

HERE = Path(__file__).resolve().parent
STATE = HERE / "docs" / "state.json"
PAPER = HERE / "docs" / "paper.json"
STYLE = os.environ.get("TARGET_STYLE", "1")
ACCOUNT = float(os.environ.get("ACCOUNT_SIZE", "1000"))
RISK = float(os.environ.get("RISK_PCT", "1"))
MAX_CARDS = 10           # at most this many full cards per rule per scan; the rest are listed by name
NAMES = {"0.5": "target at half the stop distance", "1": "target equal to the stop distance", "2": "target at twice the stop distance",
         "3": "target at three times the stop distance", "trail": "no target, trailing exit"}


def fmt(x):
    return f"{x:,.1f}" if x >= 1000 else f"{x:.2f}" if x >= 100 else f"{x:.3f}" if x >= 1 else f"{x:.4f}" if x >= 0.01 else f"{x:.3g}"


def send(text):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("[no Telegram settings, not sent]\n" + text + "\n")
        return False
    ok = True
    for chunk in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat, "text": chunk, "disable_web_page_preview": True}, timeout=20)
        if r.status_code != 200:
            print("Telegram error", r.status_code, r.text[:200])
            ok = False
    return ok


def main():
    coins = (HERE / "coins.txt").read_text().split()
    stocks = (HERE / "stocks.txt").read_text().split() if (HERE / "stocks.txt").exists() else []
    evidence = json.loads((HERE / "evidence.json").read_text(encoding="utf-8"))
    old = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"coins": {}}
    first_run = not old.get("coins")
    new_state, failed = {}, []
    regime, market = btc_regime()
    for coin in coins:
        try:
            p = plan(coin, regime)
            if p:
                new_state[coin] = p
        except Exception:
            failed.append(coin)
        time.sleep(0.05)
    for ticker in stocks:
        try:
            p = plan(ticker, None, market="stock")
            if p:
                new_state[ticker] = p
        except Exception:
            failed.append(ticker)

    # ---- the dip rule on the wide stock list
    dip_file, dips, old_dips = HERE / "dip_tickers.txt", {}, old.get("dips")
    if dip_file.exists():
        frames = stock_frames(dip_file.read_text().split())
        mkt = market_z(frames["SPY"]) if "SPY" in frames else {}
        for ticker, df in frames.items():
            try:
                p = dip_plan(ticker, df, mkt)
                if p and p["recent"]:
                    dips[ticker] = p
            except Exception:
                pass
        if len(frames) < 100:                                 # Yahoo did not answer: keep what we had, send nothing
            dips, old_dips = old.get("dips") or {}, None
    try:
        mexc = mexc_stocks()
    except Exception:
        mexc = old.get("mexc") or {}
    if old_dips is not None:
        prev_last = lambda t: (old_dips.get(t) or {}).get("last") or {}
        fresh = [(t, p["last"]) for t, p in dips.items() if p["last"]["status"] == "open" and p["last"]["age_days"] <= 1 and prev_last(t).get("day") != p["last"]["day"]
                 and t in (old.get("dip_checked") or [])]      # a stock the previous scan could not load is not a new signal
        closed = [(t, p["last"]) for t, p in dips.items() if p["last"]["status"] != "open" and prev_last(t).get("day") == p["last"]["day"] and prev_last(t).get("status") == "open"]
        risk_money = ACCOUNT * RISK / 100
        if fresh:
            fresh.sort(key=lambda x: (x[0] not in mexc, x[1]["risk_pct"]))
            for t, s in fresh[:MAX_CARDS]:                      # one clean card per signal, MEXC-listed first
                send(signal_card(t, "dip", s, mexc.get(t), dips[t]["ma20"], ACCOUNT, RISK))
                time.sleep(1.1)
            if len(fresh) > MAX_CARDS:
                rest = fresh[MAX_CARDS:]
                send(f"\u2795 {len(rest)} more dip signals today ({sum(1 for t, _ in rest if t in mexc)} on MEXC):\n" + ", ".join(t for t, _ in rest[:80])
                     + (f" and {len(rest) - 80} more" if len(rest) > 80 else "") + "\n\nSend /coin NAME for the card of any of them.")
        if closed:
            send("\U0001F4CB Dip plans closed\n\n" + "\n".join(
                (f"\U0001F6D1 SL hit \u2014 {t}  (-1R)" if s["status"] == "stopped" else f"{'\u2705' if s['R'] > 0 else '\u26AA'} Exit \u2014 {t}  ({s['R']:+.2f}R, "
                 + ("closed above 20-day average" if s["status"] == "exited" else "20-day time limit") + ")") for t, s in closed[:40])
                 + (f"\n...and {len(closed) - 40} more" if len(closed) > 40 else ""))

    signals, targets, stops = [], [], []
    for coin, p in new_state.items():
        cur = p["plans"].get(STYLE)
        prev = (old["coins"].get(coin) or {}).get("plans", {}).get(STYLE)
        if not cur:
            continue
        is_new_plan = prev is None or prev.get("day") != cur["day"]
        if cur["status"] == "open" and is_new_plan and cur["age_days"] <= 1 and not first_run:
            signals.append((coin, cur))
        elif prev and prev.get("day") == cur["day"] and prev.get("status") == "open" and cur["status"] != "open":
            (targets if cur["status"] == "target" else stops).append((coin, cur))

    risk_money = ACCOUNT * RISK / 100
    for coin, s in sorted(signals, key=lambda x: x[1]["risk_pct"]):
        stock = new_state[coin].get("market") == "stock"
        send(signal_card(coin, "stock" if stock else "crypto", s, mexc.get(coin) if stock else None, None, ACCOUNT, RISK))
        time.sleep(1.1)
    if targets or stops:
        tp = {"0.5": "TP1", "1": "TP2", "2": "TP3"}.get(STYLE, "Target")
        send("\U0001F4CB Plans closed\n\n" + "\n".join(
            [f"\u2705 {tp} hit \u2014 {c}  ({s['R']:+g}R, entry {fmt(s['entry'])})" for c, s in targets]
            + [f"\U0001F6D1 SL hit \u2014 {c}  (-1R, entry {fmt(s['entry'])}, SL {fmt(s['stop'])})" if s["status"] == "stopped" else f"\u26AA Trailed out \u2014 {c}  ({s['R']:+.2f}R)" for c, s in stops]))
    was_on = (old.get("market") or {}).get("on")
    if was_on is not None and was_on != market["on"]:
        send(("BITCOIN FILTER: ON\nBitcoin closed above its 200-day average. New signals count again." if market["on"] else
              "BITCOIN FILTER: OFF\nBitcoin closed below its 200-day average. No new signals will be sent until it closes back above. "
              "Open plans keep their stops and targets.") + f"\nBTC {fmt(market['btc_close'])} vs average {fmt(market['btc_avg200'])}")
    announced = bool(old.get("announced"))
    if not announced:                                         # sent once, the first time Telegram actually works
        n_open = sum(1 for p in new_state.values() if (p["plans"].get(STYLE) or {}).get("status") == "open")
        announced = send(f"Scanner is set up. Watching {len(new_state)} coins with: {NAMES[STYLE]}.\n{n_open} plans are already open from earlier signals; "
             f"you will only be alerted about NEW ones from now on.\nThe dip rule is watching {len(dip_file.read_text().split()) if dip_file.exists() else 0} US stocks and funds; "
             f"{len(mexc)} US stocks are listed on MEXC as futures.")

    STATE.parent.mkdir(exist_ok=True)
    ledger = json.loads(PAPER.read_text(encoding="utf-8")) if PAPER.exists() else None
    PAPER.write_text(json.dumps(update_ledger(ledger, new_state, STYLE, dips), separators=(",", ":")), encoding="utf-8")
    STATE.write_text(json.dumps({"updated": int(time.time() * 1000), "error": None, "order": list(new_state), "coins": new_state, "evidence": evidence, "market": market,
                                 "announced": announced, "dip_checked": sorted(frames) if dip_file.exists() and len(frames) >= 100 else (old.get("dip_checked") or []), "dips": dips, "dips_updated": int(time.time() * 1000), "mexc": mexc},
                                separators=(",", ":")), encoding="utf-8")
    print(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "| coins", len(new_state), "| failed", len(failed),
          "| dip plans", len(dips), "| new signals", len(signals), "| targets", len(targets), "| stops", len(stops))


if __name__ == "__main__":
    main()
