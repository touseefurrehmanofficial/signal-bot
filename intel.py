"""Shadow link to the crypto-intel-bot (news calendar, funding, liquidation clusters).

SHADOW ONLY: the readings are recorded next to every signal and trade, and NOTHING here changes a trade decision.
After enough signals, research compares outcomes in "risky" vs "calm" conditions; the intel bot's help is switched on
only if that comparison shows it improves results.

Source: the intel bot's saved state (private repo touseefurrehmanofficial/crypto-intel-bot, branch bot-state,
state.json), read with a read-only token in the INTEL_TOKEN secret. Without the token every snapshot says so and the
bot works exactly as before.
"""
import os
import time

import requests

REPO = os.environ.get("INTEL_REPO", "touseefurrehmanofficial/crypto-intel-bot")
TOKEN = os.environ.get("INTEL_TOKEN", "")
BIG_CLUSTER = 300e6                                             # liquidation clusters counted as "big" (USD)
_cache = {"t": 0.0, "state": None}


def intel_state(max_age=300):
    """the intel bot's state.json (cached 5 minutes); None if unavailable."""
    if not TOKEN:
        return None
    if _cache["state"] is not None and time.time() - _cache["t"] < max_age:
        return _cache["state"]
    try:
        r = requests.get(f"https://api.github.com/repos/{REPO}/contents/state.json", params={"ref": "bot-state"},
                         headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github.raw"}, timeout=20)
        if r.status_code == 200:
            _cache.update(t=time.time(), state=r.json())
    except (requests.RequestException, ValueError):
        pass
    return _cache["state"]


def snapshot(state=None, now=None, btc_price=None):
    """compact readings for one moment. Every field is None when the intel state is unavailable."""
    now = now or time.time()
    s = state if state is not None else intel_state()
    out = {"at": int(now), "ok": bool(s)}
    if not s:
        return out
    misc = s.get("misc") or {}
    events = [e for e in ((misc.get("calendar") or {}).get("events") or []) if e.get("impact") == "High" and e.get("ts")]
    usd = [e for e in events if e.get("country") == "USD"]
    nxt = lambda ev: min((e["ts"] - now for e in ev if e["ts"] >= now), default=None)
    prev = lambda ev: min((now - e["ts"] for e in ev if e["ts"] < now), default=None)
    m = lambda x: None if x is None else round(x / 60)
    out.update(usd_news_in_min=m(nxt(usd)), usd_news_ago_min=m(prev(usd)), any_news_in_min=m(nxt(events)), any_news_ago_min=m(prev(events)))
    out["news_window"] = any(x is not None and x <= lim for x, lim in ((out["usd_news_in_min"], 120), (out["usd_news_ago_min"], 60)))
    for coin in ("BTC", "ETH", "SOL"):
        out[f"funding_{coin}"] = (misc.get(f"funding_{coin}") or {}).get("_market")
    if btc_price:
        cl = (s.get("clusters") or {}).get("BTC") or {}
        rows = [c for c in (cl.get("alerted") or []) + (cl.get("pending") or []) if isinstance(c, list) and len(c) >= 3 and c[2] >= BIG_CLUSTER]
        below = [(btc_price - c[0]) / btc_price * 100 for c in rows if c[1] == "long" and c[0] < btc_price]
        above = [(c[0] - btc_price) / btc_price * 100 for c in rows if c[1] == "short" and c[0] > btc_price]
        out["btc_long_cluster_below_pct"] = round(min(below), 2) if below else None
        out["btc_short_cluster_above_pct"] = round(min(above), 2) if above else None
    return out
