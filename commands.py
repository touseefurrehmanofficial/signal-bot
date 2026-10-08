"""Answer commands sent to the Telegram bot. Reads the files the scanner last saved; it does not scan.

  /signal       signals from the last daily close (crypto, US stocks, dip rule), or "none"
  /demo_trade   the demo account: balance, success rate, open and finished trades
  /help         this list

Only messages from TELEGRAM_CHAT_ID are answered; anyone else who finds the bot gets no reply.
GitHub starts this every few minutes and it listens for about four minutes each time, so a reply
usually comes within seconds and occasionally takes several minutes when GitHub starts late.
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
TOKEN, CHAT = os.environ.get("TELEGRAM_TOKEN"), str(os.environ.get("TELEGRAM_CHAT_ID", ""))
STYLE = os.environ.get("TARGET_STYLE", "1")
ACCOUNT, RISK = float(os.environ.get("ACCOUNT_SIZE", "1000")), float(os.environ.get("RISK_PCT", "1"))
LISTEN_SECONDS = int(os.environ.get("LISTEN_SECONDS", "240"))
API = f"https://api.telegram.org/bot{TOKEN}"
DISCLAIMER = "Research output, not financial advice."


def fmt(x):
    return f"{x:,.1f}" if x >= 1000 else f"{x:.2f}" if x >= 100 else f"{x:.3f}" if x >= 1 else f"{x:.4f}" if x >= 0.01 else f"{x:.3g}"


def day(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%b %d")


def load(name):
    p = HERE / "docs" / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def signal_text():
    s = load("state.json")
    if not s:
        return "No scan has been saved yet."
    mexc, risk_money, lines = s.get("mexc") or {}, ACCOUNT * RISK / 100, []
    size = lambda rp: f"size for {RISK:g}% risk on ${ACCOUNT:,.0f}: ${risk_money / (rp / 100):,.0f}"
    brk = [(c, p["plans"][STYLE], p) for c, p in s["coins"].items() if (p["plans"].get(STYLE) or {}).get("status") == "open" and p["plans"][STYLE]["age_days"] <= 1]
    dips = [(c, p["last"], p) for c, p in (s.get("dips") or {}).items() if p["last"]["status"] == "open" and p["last"]["age_days"] <= 1]
    dips.sort(key=lambda x: (x[0] not in mexc, x[1]["risk_pct"]))
    if brk:
        lines += [f"BREAKOUT SIGNALS ({len(brk)})", ""]
        for c, t, p in brk:
            r = t["entry"] - t["stop"]
            tgt = "exit: daily close below the 20-day low" if STYLE == "trail" else f"target {fmt(t['entry'] + float(STYLE) * r)} (+{t['risk_pct'] * float(STYLE):.1f}%)"
            lines += [c + (" (US stock/ETF)" if p.get("market") == "stock" else ""), f"  entry {fmt(t['entry'])}, now {fmt(p['price'])}", f"  stop {fmt(t['stop'])} (-{t['risk_pct']:.1f}%)", f"  {tgt}", f"  {size(t['risk_pct'])}", ""]
    if dips:
        lines += [f"DIP SIGNALS ({len(dips)}), {sum(1 for c, _, _ in dips if c in mexc)} listed on MEXC", ""]
        for c, t, p in dips[:12]:
            kind = "" if t.get("mkt_z") is None else " - market-wide dip" if t["mkt_z"] < -1.5 else " - stock-only dip" if t["mkt_z"] > -0.5 else ""
            lines += [c + (f" (MEXC: {mexc[c]})" if c in mexc else " (not on MEXC)") + kind, f"  entry {fmt(t['entry'])}, now {fmt(p['price'])}", f"  stop {fmt(t['stop'])} (-{t['risk_pct']:.1f}%)",
                      f"  exit: first daily close above {fmt(p['ma20'])} (20-day average, moves daily) or after 20 trading days", f"  {size(t['risk_pct'])}", ""]
        if len(dips) > 12:
            lines += ["Also: " + ", ".join(c for c, _, _ in dips[12:72]), ""]
    m = s.get("market") or {}
    checked = datetime.fromtimestamp(s["updated"] / 1000, timezone.utc).strftime("%b %d %H:%M UTC")
    if not lines:
        lines = ["No new signals from the last daily close.", ""]
    n_open = sum(1 for p in s["coins"].values() if (p["plans"].get(STYLE) or {}).get("status") == "open")
    n_dip = sum(1 for p in (s.get("dips") or {}).values() if p["last"]["status"] == "open")
    lines += [f"Open plans from earlier signals: {n_open} breakout, {n_dip} dip.", f"Bitcoin filter: {'ON' if m.get('on') else 'OFF (no new crypto signals)'}.", f"Last scan: {checked}.",
              "Signals appear only after a daily close: about 00:00 UTC for crypto, about 21:00 UTC on weekdays for US stocks.", DISCLAIMER]
    return "\n".join(lines)


def demo_text():
    led, s = load("paper.json"), load("state.json")
    if not led:
        return "The demo account has not started yet."
    trades = sorted(led.get("trades", {}).values(), key=lambda t: -t["day"])
    price = lambda t: ((s.get("dips") or {}).get(t["coin"]) if t.get("rule") == "dip" else (s.get("coins") or {}).get(t["coin"]) or {}).get("price")
    closed, opened = [t for t in trades if t["status"] != "open"], [t for t in trades if t["status"] == "open"]
    wins, sum_r = [t for t in closed if t["R"] > 0], sum(t["R"] for t in closed)
    open_r = sum((price(t) - t["entry"]) / (t["entry"] - t["stop"]) for t in opened if price(t))
    c50 = [t for t in trades if t["x50"]["status"] != "open"]
    liq, sum50 = [t for t in c50 if t["x50"]["status"] == "liquidated"], sum(t["x50"]["pnl"] for t in c50)
    started = datetime.fromtimestamp(led["started"] / 1000, timezone.utc).strftime("%b %d %H:%M UTC")
    lines = ["DEMO ACCOUNT", f"Started {started} with $100. Only signals after that moment are counted.", "",
             "Risking $1 per trade (the tested way)", f"  balance ${100 + sum_r:.2f}", f"  finished {len(closed)}: {len(wins)} won, {len(closed) - len(wins)} lost"
             + (f" ({100 * len(wins) / len(closed):.0f}% success)" if closed else ""), f"  open {len(opened)}, unrealised {'+' if open_r >= 0 else '-'}${abs(open_r):.2f}", "",
             "$1 margin at 50x per trade", f"  balance ${100 + sum50:.2f}", f"  wiped out {len(liq)} of {len(c50)} finished" if c50 else "  nothing finished yet", ""]
    if not trades:
        lines += ["No trades yet. A trade is added when a new signal appears at a daily close.", ""]
    for title, group in (("Open trades", opened[:15]), ("Last finished trades", closed[:10])):
        if group:
            lines.append(title)
            for t in group:
                r = (price(t) - t["entry"]) / (t["entry"] - t["stop"]) if t["status"] == "open" and price(t) else t["R"]
                lines.append(f"  {t['coin']}{' (dip)' if t.get('rule') == 'dip' else ''} {day(t['day'])}: entry {fmt(t['entry'])}, stop {fmt(t['stop'])}, "
                             + (f"now {r:+.2f}R" if t["status"] == "open" else f"{t['status']} {t['R']:+.2f}R"))
            lines.append("")
    lines += ["No real money is involved. " + DISCLAIMER]
    return "\n".join(lines)


HELP = "Commands:\n/signal - signals from the last daily close\n/demo_trade - the demo account\n/help - this list\n\nNew signals are also sent automatically after each daily close."


def send(text):
    for chunk in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
        requests.post(f"{API}/sendMessage", json={"chat_id": CHAT, "text": chunk, "disable_web_page_preview": True}, timeout=20)


def main():
    if not (TOKEN and CHAT):
        print("no Telegram settings; nothing to do")
        return
    try:                                                       # makes the commands appear in Telegram's "/" menu
        requests.post(f"{API}/setMyCommands", json={"commands": [{"command": "signal", "description": "Signals from the last daily close"},
                      {"command": "demo_trade", "description": "Demo account: balance, success rate, trades"}, {"command": "help", "description": "List of commands"}]}, timeout=20)
    except Exception:
        pass
    offset, end, answered = None, time.time() + LISTEN_SECONDS, 0
    while time.time() < end:
        try:
            r = requests.get(f"{API}/getUpdates", params={"timeout": 25, "offset": offset, "allowed_updates": json.dumps(["message"])}, timeout=40).json()
        except Exception:
            time.sleep(3)
            continue
        if not r.get("ok"):
            print("Telegram said:", r.get("error_code"), r.get("description"))
            time.sleep(5)
            continue
        for u in r["result"]:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != CHAT:
                continue                                       # not the owner: ignore
            cmd = (msg.get("text") or "").strip().split()[0].split("@")[0].lower() if msg.get("text") else ""
            reply = signal_text() if cmd in ("/signal", "/signals") else demo_text() if cmd in ("/demo_trade", "/demo", "/demotrade") else HELP if cmd in ("/help", "/start") else None
            if reply:
                send(reply)
                answered += 1
    if offset is not None:                                     # tell Telegram these are handled
        try:
            requests.get(f"{API}/getUpdates", params={"timeout": 0, "offset": offset}, timeout=20)
        except Exception:
            pass
    print("commands answered:", answered)


if __name__ == "__main__":
    main()
