"""Answer commands sent to the Telegram bot. Reads the files the scanner last saved; it does not scan.

  /signal       signals from the last daily close (crypto, US stocks, dip rule), or "none"
  /demo_trade   the demo account: balance, success rate, open and finished trades
  /help         this list

Only messages from TELEGRAM_CHAT_ID are answered; anyone else who finds the bot gets no reply.
A run listens for 50 minutes and then starts its successor (see .github/workflows/commands.yml), so a
reply normally comes within a second or two; during the hand-over, about once an hour, it can take a minute.
"""
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from core import signal_card

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


_pulled = [0.0]


def refresh():
    """A listener runs for most of an hour; fetch the scanner's newest files before answering (at most once a minute)."""
    if time.time() - _pulled[0] > 60:
        _pulled[0] = time.time()
        try:
            subprocess.run(["git", "pull", "--quiet", "--ff-only"], cwd=HERE, timeout=30, check=False, capture_output=True)
        except Exception:
            pass


def load(name):
    p = HERE / "docs" / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def signal_text():
    s = load("state.json")
    if not s:
        return "No scan has been saved yet."
    mexc, risk_money, lines = s.get("mexc") or {}, ACCOUNT * RISK / 100, []
    size = lambda rp: f"size for {RISK:g}% risk on ${ACCOUNT:,.0f}: ${risk_money / (rp / 100):,.0f}"
    cards = []
    brk = [(c, p["plans"][STYLE], p) for c, p in s["coins"].items() if (p["plans"].get(STYLE) or {}).get("status") == "open" and p["plans"][STYLE]["age_days"] <= 1]
    dips = [(c, p["last"], p) for c, p in (s.get("dips") or {}).items() if p["last"]["status"] == "open" and p["last"]["age_days"] <= 1]
    dips.sort(key=lambda x: (x[0] not in mexc, x[1]["risk_pct"]))
    now = lambda p: f"\nNow: {fmt(p['price'])}"
    for c, t, p in brk:
        stock = p.get("market") == "stock"
        cards.append(signal_card(c, "stock" if stock else "crypto", t, mexc.get(c) if stock else None, None, ACCOUNT, RISK) + now(p))
    for c, t, p in dips[:10]:
        cards.append(signal_card(c, "dip", t, mexc.get(c), p["ma20"], ACCOUNT, RISK) + now(p))
    if len(dips) > 10:
        lines += [f"\u2795 {len(dips) - 10} more dip signals: " + ", ".join(c for c, _, _ in dips[10:80]), "Send /coin NAME for any of them.", ""]
    m = s.get("market") or {}
    checked = datetime.fromtimestamp(s["updated"] / 1000, timezone.utc).strftime("%b %d %H:%M UTC")
    if not cards:
        lines = ["No new signals from the last daily close.", ""]
    n_open = sum(1 for p in s["coins"].values() if (p["plans"].get(STYLE) or {}).get("status") == "open")
    n_dip = sum(1 for p in (s.get("dips") or {}).values() if p["last"]["status"] == "open")
    lines += [f"Open plans from earlier signals: {n_open} breakout, {n_dip} dip.", f"Bitcoin filter: {'ON' if m.get('on') else 'OFF (no new crypto signals)'}.", f"Last scan: {checked}.",
              DISCLAIMER]
    return cards + ["\n".join(lines)]


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


DASHBOARD = os.environ.get("DASHBOARD_URL", "")


def _open_rows(s, only_mexc=False):
    """(name, kind, trade, price, exit text) for every open plan, newest first."""
    mexc, rows = s.get("mexc") or {}, []
    for c, p in s["coins"].items():
        t = p["plans"].get(STYLE) or {}
        if t.get("status") == "open" and not (only_mexc and p.get("market") == "stock" and c not in mexc):
            tgt = "trailing exit" if STYLE == "trail" else f"target {fmt(t['entry'] + float(STYLE) * (t['entry'] - t['stop']))}"
            rows.append((c, "stock breakout" if p.get("market") == "stock" else "crypto breakout", t, p["price"], tgt))
    for c, p in (s.get("dips") or {}).items():
        t = p["last"]
        if t["status"] == "open" and not (only_mexc and c not in mexc):
            rows.append((c, "dip", t, p["price"], f"exit above {fmt(p['ma20'])}, {max(0, 20 - t['age_days'])} days left"))
    return sorted(rows, key=lambda r: r[2]["age_days"])


def open_text(arg="", only_mexc=False):
    s = load("state.json")
    if not s:
        return "No scan has been saved yet."
    rows, mexc = _open_rows(s, only_mexc), s.get("mexc") or {}
    head = f"OPEN PLANS{' YOU CAN TRADE ON MEXC' if only_mexc else ''} ({len(rows)})"
    if only_mexc:
        head += "\nCrypto coins, and US stocks MEXC lists as futures. The test used no leverage."
    lines = [head, ""]
    for c, kind, t, price, ex in rows[:40]:
        r = (price - t["entry"]) / (t["entry"] - t["stop"])
        lines.append(f"{c}{' [' + mexc[c] + ']' if c in mexc else ''} ({kind}, day {t['age_days']}): entry {fmt(t['entry'])}, stop {fmt(t['stop'])}, {ex}, now {fmt(price)} = {r:+.2f}R")
    if len(rows) > 40:
        lines.append(f"...and {len(rows) - 40} older ones. Use /coin NAME for any single one.")
    avg = sum((price - t["entry"]) / (t["entry"] - t["stop"]) for _, _, t, price, _ in rows) / len(rows) if rows else 0
    lines += ["", f"Average of all open plans right now: {avg:+.2f}R.", DISCLAIMER]
    return "\n".join(lines)


def near_text(arg=""):
    s = load("state.json")
    if not s:
        return "No scan has been saved yet."
    brk = sorted(((p["next_entry"] / p["price"] - 1) * 100, c, p) for c, p in s["coins"].items() if (p["plans"].get(STYLE) or {}).get("status") != "open" and p["next_entry"] / p["price"] - 1 < 0.05)
    lines = [f"CLOSE TO A BREAKOUT SIGNAL ({len(brk)})", "No open plan, and within 5% of the level a daily close must beat.", ""]
    for gap, c, p in brk[:20]:
        lines.append(f"{c}: now {fmt(p['price'])}, needs a close above {fmt(p['next_entry'])} ({'above it now' if gap <= 0 else f'{gap:.1f}% away'}); stop would be {fmt(p['next_stop'])}")
    if not brk:
        lines.append("Nothing is close right now.")
    lines += ["", "It only becomes a signal if the day closes above the level.", DISCLAIMER]
    return "\n".join(lines)


def coin_text(arg=""):
    s = load("state.json")
    name = arg.strip().upper().replace("USDT", "").replace("/", "")
    if not name:
        return "Send a name with it, for example:  /coin BTC   or   /coin AAPL"
    p, d, mexc = (s.get("coins") or {}).get(name), (s.get("dips") or {}).get(name), s.get("mexc") or {}
    if not p and not d:
        in_dip = name in (HERE / "dip_tickers.txt").read_text().split() if (HERE / "dip_tickers.txt").exists() else False
        return f"{name}: watched by the dip rule, no dip trade in the last 60 days." if in_dip else f"{name} is not on the scanner's lists."
    lines, cards = [name + (f"  (MEXC futures: {mexc[name]})" if name in mexc else ""), ""], []
    if p and (p["plans"].get(STYLE) or {}).get("status") == "open":
        cards.append(signal_card(name, "stock" if p.get("market") == "stock" else "crypto", p["plans"][STYLE], mexc.get(name) if p.get("market") == "stock" else None, None, ACCOUNT, RISK, "Open plan"))
    if d and d["last"]["status"] == "open":
        cards.append(signal_card(name, "dip", d["last"], mexc.get(name), d["ma20"], ACCOUNT, RISK, "Open plan"))
    if p:
        t = p["plans"].get(STYLE)
        lines.append(f"Breakout rule. Price {fmt(p['price'])}.")
        if t and t["status"] == "open":
            r = (p["price"] - t["entry"]) / (t["entry"] - t["stop"])
            lines += [f"  OPEN plan, day {t['age_days']}: entry {fmt(t['entry'])}, stop {fmt(t['stop'])} (-{t['risk_pct']:.1f}%)"
                      + ("" if STYLE == "trail" else f", target {fmt(t['entry'] + float(STYLE) * (t['entry'] - t['stop']))}"), f"  now {r:+.2f}R"]
        else:
            if t:
                lines.append(f"  last plan: {t['status']} on {day(t['when'])} ({t['R']:+.2f}R)")
            gap = (p["next_entry"] / p["price"] - 1) * 100
            lines.append(f"  next signal: a daily close above {fmt(p['next_entry'])} ({'above it now' if gap <= 0 else f'{gap:.1f}% away'}); stop would be {fmt(p['next_stop'])}")
        done = [x for x in p["recent"].get(STYLE, []) if x["status"] != "open"]
        if done:
            lines.append(f"  last 120 days: {len(done)} finished, {sum(1 for x in done if x['R'] > 0)} won, total {sum(x['R'] for x in done):+.2f}R")
        lines.append("")
    if d:
        t = d["last"]
        lines.append(f"Dip rule. Price {fmt(d['price'])}, 20-day average {fmt(d['ma20'])}.")
        if t["status"] == "open":
            r = (d["price"] - t["entry"]) / (t["entry"] - t["stop"])
            lines += [f"  OPEN plan, day {t['age_days']} of 20: entry {fmt(t['entry'])}, stop {fmt(t['stop'])} (-{t['risk_pct']:.1f}%), exit on a close above {fmt(d['ma20'])}", f"  now {r:+.2f}R"]
        else:
            lines.append(f"  last plan: {t['status']} on {day(t['when'])} ({t['R']:+.2f}R)")
        done = [x for x in d["recent"] if x["status"] != "open"]
        if done:
            lines.append(f"  last 60 days: {len(done)} finished, {sum(1 for x in done if x['R'] > 0)} won, total {sum(x['R'] for x in done):+.2f}R")
        lines.append("")
    return cards + ["\n".join(lines + [DISCLAIMER])]


def results_text(arg=""):
    s = load("state.json")
    if not s:
        return "No scan has been saved yet."
    cut, rows = s["updated"] - 14 * 86_400_000, []
    for c, p in s["coins"].items():
        rows += [(t["when"], c, "breakout", t) for t in p["recent"].get(STYLE, []) if t["status"] != "open" and t["when"] >= cut]
    for c, p in (s.get("dips") or {}).items():
        rows += [(t["when"], c, "dip", t) for t in p["recent"] if t["status"] != "open" and t["when"] >= cut]
    rows.sort(key=lambda r: -r[0])
    lines = ["FINISHED IN THE LAST 14 DAYS", "Every plan the rules produced, wins and losses together.", ""]
    for kind in ("breakout", "dip"):
        g = [t for _, _, k, t in rows if k == kind]
        if g:
            lines.append(f"{kind}: {len(g)} finished, {sum(1 for t in g if t['R'] > 0)} won ({100 * sum(1 for t in g if t['R'] > 0) / len(g):.0f}%), total {sum(t['R'] for t in g):+.1f}R, average {sum(t['R'] for t in g) / len(g):+.2f}R")
    lines.append("")
    lines += [f"{day(w)} {c} ({k}): {t['status']} {t['R']:+.2f}R" for w, c, k, t in rows[:30]]
    if len(rows) > 30:
        lines.append(f"...and {len(rows) - 30} more.")
    if not rows:
        lines.append("Nothing finished in the last 14 days.")
    return "\n".join(lines + ["", "Two weeks is too short to judge a rule either way.", DISCLAIMER])


def status_text(arg=""):
    s = load("state.json")
    if not s:
        return "No scan has been saved yet."
    m, now = s.get("market") or {}, datetime.now(timezone.utc)
    age = (now.timestamp() * 1000 - s["updated"]) / 3_600_000
    to_crypto = 24 - now.hour - now.minute / 60
    n_open = sum(1 for p in s["coins"].values() if (p["plans"].get(STYLE) or {}).get("status") == "open")
    n_dip = sum(1 for p in (s.get("dips") or {}).values() if p["last"]["status"] == "open")
    lines = ["STATUS", f"Last scan: {datetime.fromtimestamp(s['updated'] / 1000, timezone.utc).strftime('%b %d %H:%M UTC')} ({age:.1f} hours ago)" + ("  <- LATE, scans should come at least every 9 hours" if age > 9 else ""),
             f"Watching: {len(s['coins'])} coins and stocks (breakout rule), {len((HERE / 'dip_tickers.txt').read_text().split()) if (HERE / 'dip_tickers.txt').exists() else 0} stocks and funds (dip rule)",
             f"Open plans: {n_open} breakout, {n_dip} dip", f"Bitcoin filter: {'ON' if m.get('on') else 'OFF'} for {m.get('days', '?')} days (BTC {fmt(m['btc_close'])} vs 200-day average {fmt(m['btc_avg200'])})" if m else "",
             f"MEXC lists {len(s.get('mexc') or {})} US stocks as futures", "", f"Next crypto daily close: in {to_crypto:.1f} hours (00:00 UTC = 05:00 Pakistan)",
             "Next US stock close: about 21:00 UTC on weekdays (02:00 Pakistan)", f"Alerts use: target style {STYLE}, account ${ACCOUNT:,.0f}, risk {RISK:g}%"]
    if DASHBOARD:
        lines += ["", "Dashboard: " + DASHBOARD]
    return "\n".join(x for x in lines if x is not None)


def odds_text(arg=""):
    ev = (load("state.json").get("evidence")) or {}
    key = {"0.5": "TP_0.5R", "1": "TP_1R", "2": "TP_2R", "3": "TP_3R", "trail": "TRAIL"}[STYLE]
    lines = ["WHAT THE TESTS SHOWED", "R = the amount risked on a trade.", ""]
    for title, k in (("Crypto breakout, 40 coins, Bitcoin filter on", "top40"), ("US stock breakout, 52 names", "stocks")):
        a, b = ((ev.get(k) or {}).get(key) or {}).get("ALL"), ((ev.get(k) or {}).get(key) or {}).get("SINCE_2022")
        if a:
            lines += [title, f"  won {a['win_pct']}%, average {a['avg_R']:+.2f}R, typical length {a['median_days']} days", f"  since 2022: won {b['win_pct']}%, average {b['avg_R']:+.2f}R, about {b['trades_per_week']} signals a week", ""]
    d = ev.get("dip")
    if d:
        a, b, c = d["ALL"], d["SINCE_2022"], d["capped_account_10_open_half_pct_risk"]
        lines += [f"Dip rule, {d['instruments']} US stocks and funds", f"  won {a['win_pct']}%, average {a['avg_R']:+.2f}R (winners {a['avg_winner_R']:+.2f}R, losers {a['avg_loser_R']:+.2f}R), typical length {a['median_days']} days",
                  f"  since 2022: won {b['win_pct']}%, average {b['avg_R']:+.2f}R, about {b['trades_per_week']} signals a week, in bursts",
                  f"  account limited to 10 positions at 0.5% risk: about {c['taken_per_week']} trades a week, {c['avg_return_per_year_pct']:+.1f}% a year, worst year {c['worst_year_pct']}%, deepest fall {c['deepest_fall_pct']}%", ""]
    lines += ["The stock breakout result is mostly the stock market's own rise; breakout days did no better than random days in the same month.",
              "At 50x leverage the same signals were wiped out about 9 times in 10.", "Past results do not guarantee future results. " + DISCLAIMER]
    return "\n".join(lines)


def size_text(arg=""):
    try:
        nums = [float(x.replace(",", "")) for x in arg.split()]
        entry, stop = nums[0], nums[1]
        account, risk = (nums[2] if len(nums) > 2 else ACCOUNT), (nums[3] if len(nums) > 3 else RISK)
        assert entry > 0 and stop > 0 and entry != stop and account > 0 and 0 < risk <= 100
    except Exception:
        return ("Position size from entry and stop.\nSend:  /size ENTRY STOP\nor:    /size ENTRY STOP ACCOUNT RISK%\n\nExample:  /size 150.92 140.11\nExample:  /size 150.92 140.11 500 1")
    dist = abs(entry - stop) / entry * 100
    money = account * risk / 100
    pos = money / (dist / 100)
    lines = [f"Entry {fmt(entry)}, stop {fmt(stop)} ({'long' if stop < entry else 'short'}), stop distance {dist:.2f}%", f"Account ${account:,.2f}, risk {risk:g}% = ${money:,.2f} lost if the stop is hit", "",
             f"Position size: ${pos:,.2f}  ({pos / entry:,.4f} units)", f"That is {pos / account * 100:.0f}% of the account" + (" - more than the account, so it would need leverage" if pos > account else ", no leverage needed"), ""]
    lines += [f"At {lev}x leverage: margin ${pos / lev:,.2f}; liquidation about {100 / lev:.1f}% from entry" + ("  <- BEFORE your stop: the stop would never be reached" if 100 / lev <= dist else "") for lev in (5, 10, 20, 50)]
    return "\n".join(lines + ["", "Arithmetic only. " + DISCLAIMER])


def dashboard_text(arg=""):
    return ("Dashboard: " + DASHBOARD) if DASHBOARD else "No dashboard address is set."


# command -> (function, description for Telegram's "/" menu)
COMMANDS = {
    "signal": (lambda a="": signal_text(), "New signals from the last daily close, one card each"),
    "open": (open_text, "All open plans and where price is now"),
    "mexc": (lambda a="": open_text(a, only_mexc=True), "Open plans you can trade on MEXC"),
    "near": (near_text, "Coins and stocks close to a breakout signal"),
    "coin": (coin_text, "One coin or stock, e.g. /coin BTC"),
    "results": (results_text, "What finished in the last 14 days"),
    "demo_trade": (lambda a="": demo_text(), "Demo account: balance, success rate, trades"),
    "size": (size_text, "Position size, e.g. /size 150.9 140.1"),
    "odds": (odds_text, "What the tests showed for each rule"),
    "status": (status_text, "Last scan, Bitcoin filter, next close"),
    "dashboard": (dashboard_text, "Link to the dashboard"),
}
ALIASES = {"signals": "signal", "demo": "demo_trade", "demotrade": "demo_trade", "plan": "coin", "stock": "coin", "result": "results", "risk": "size", "start": "help"}
HELP = "Commands:\n" + "\n".join(f"/{k} - {d}" for k, (_, d) in COMMANDS.items()) + "\n/help - this list\n\nNew signals are also sent automatically after each daily close."


def send(text):
    """text may be one message or a list of messages (one card each)."""
    for part in ([text] if isinstance(text, str) else text):
        for chunk in [part[i:i + 3800] for i in range(0, len(part), 3800)]:
            requests.post(f"{API}/sendMessage", json={"chat_id": CHAT, "text": chunk, "disable_web_page_preview": True}, timeout=20)
            time.sleep(0.4)


# GitHub's own schedule starts scans late or not at all, so the listener (which is always running) starts them.
SCAN_TIMES = [(0, 6, False), (6, 20, False), (12, 20, False), (18, 20, False), (21, 25, True)]   # UTC hour, minute, weekdays only
_scan_check = [0.0]


def maybe_start_scan():
    if not os.environ.get("GH_TOKEN") or time.time() - _scan_check[0] < 180:
        return
    _scan_check[0] = time.time()
    now = datetime.now(timezone.utc)
    due = max(d for back in (0, 1, 2) for h, m, wk in SCAN_TIMES
              if (d := (now - timedelta(days=back)).replace(hour=h, minute=m, second=0, microsecond=0)) <= now and not (wk and d.weekday() >= 5))
    try:
        refresh()
        if load("state.json").get("updated", 0) / 1000 >= due.timestamp():
            return
        repo = os.environ["GITHUB_REPOSITORY"]
        last = subprocess.run(["gh", "run", "list", "--repo", repo, "--workflow", "scan.yml", "--limit", "1", "--json", "createdAt", "--jq", ".[0].createdAt"],
                              capture_output=True, text=True, timeout=30).stdout.strip()
        if last and datetime.fromisoformat(last.replace("Z", "+00:00")) >= due:
            return                                             # a scan for this slot is already running or done
        subprocess.run(["gh", "workflow", "run", "scan.yml", "--repo", repo, "--ref", "main"], capture_output=True, timeout=30)
        print("started the scan due at", due.strftime("%H:%M UTC"))
    except Exception as e:
        print("scan check failed:", type(e).__name__)


def main():
    if not (TOKEN and CHAT):
        print("no Telegram settings; nothing to do")
        return
    try:                                                       # makes the commands appear in Telegram's "/" menu
        requests.post(f"{API}/setMyCommands", json={"commands": [{"command": k, "description": d} for k, (_, d) in COMMANDS.items()] + [{"command": "help", "description": "List of commands"}]}, timeout=20)
    except Exception:
        pass
    offset, end, answered = None, time.time() + LISTEN_SECONDS, 0
    while time.time() < end:
        maybe_start_scan()
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
            text = (msg.get("text") or "").strip()
            slash = text.startswith("/")
            cmd, _, arg = text.lstrip("/").partition(" ")
            cmd = cmd.split("@")[0].lower()
            cmd = ALIASES.get(cmd, cmd)
            if not slash and cmd not in COMMANDS and cmd != "help":
                continue                                       # ordinary text that is not a command word: stay quiet
            refresh()
            try:
                reply = HELP if cmd == "help" else COMMANDS[cmd][0](arg) if cmd in COMMANDS else "I do not know that command.\n\n" + HELP
            except Exception as e:                              # one bad reply must not stop the listener
                reply = f"Something went wrong answering /{cmd} ({type(e).__name__}). The scan data may be mid-update; try again in a minute."
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
