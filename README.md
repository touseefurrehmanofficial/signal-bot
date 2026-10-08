# Signal bot — phone alerts and a hosted dashboard, with your laptop off

This folder is a complete, self-contained project. Put it in a GitHub repository and GitHub will run the scan for you several times a day, send alerts to your phone through Telegram, and host the dashboard as a web page you can open anywhere.

It uses public market data only. It holds no exchange keys and places no trades. It is a research display, not financial advice.

## What you get

- **Telegram message** when a new signal appears (coin, entry, stop, target, position size), and when a plan reaches its target or stop.
- **Dashboard** at `https://<your-username>.github.io/<repo-name>/` with live prices, trade plans, a sound alert, and the demo account.
- **Demo account** that records every new signal as a paper trade from the moment you set it up.

New signals can only appear when the daily candle closes, at 00:00 UTC (05:00 in Pakistan). The scan runs at 00:06 UTC for signals and three more times a day for targets and stops. Expect about two signals a week on the 40-coin list.

## Files

| File | Purpose |
|---|---|
| `core.py` | The rule: turns daily candles into entry, stop, target and status for each coin |
| `scan.py` | One scan: updates `docs/state.json` and `docs/paper.json`, sends Telegram alerts |
| `coins.txt` | The 40 coins followed (largest by trading volume; the rule failed on small coins) |
| `stocks.txt` | 12 stock-index funds and 40 large US stocks followed with the same rule |
| `dip_tickers.txt` | 583 US stocks and funds followed with the dip rule |
| `evidence.json` | Test results shown on the dashboard |
| `docs/index.html` | The dashboard page |
| `.github/workflows/scan.yml` | The schedule GitHub runs |

## Setup (about 15 minutes, all done by you)

These steps involve creating accounts and handling a secret token, so they are yours to do. Nothing here needs your exchange account.

### 1. Make a Telegram bot

1. In Telegram, open a chat with **@BotFather** and send `/newbot`. Choose any name and username.
2. BotFather replies with a **token** that looks like `1234567890:AA…`. Keep it private; anyone with it can send messages as your bot.
3. Open a chat with your new bot and send it any message, for example `hi`.
4. Get your **chat id**: open a chat with **@userinfobot** and send `/start`. It replies with your numeric id.

### 2. Put this folder on GitHub

1. Create a GitHub account if you do not have one, then create a new repository. It can be **public** (GitHub Pages is free for public repositories; the repository contains only public market levels, never your token).
2. Upload the contents of this `signal-bot` folder so that `scan.py` is at the top level of the repository. From this folder:

```bash
git init -b main
```
```bash
git add .
```
```bash
git commit -m "signal bot"
```
```bash
git remote add origin https://github.com/<your-username>/<repo-name>.git
```
```bash
git push -u origin main
```

### 3. Give GitHub the Telegram details

In the repository: **Settings → Secrets and variables → Actions → New repository secret**. Add two secrets:

| Name | Value |
|---|---|
| `TELEGRAM_TOKEN` | the token from BotFather |
| `TELEGRAM_CHAT_ID` | your numeric chat id |

Optional, under the **Variables** tab on the same page:

| Name | Default | Meaning |
|---|---|---|
| `TARGET_STYLE` | `1` | `0.5`, `1`, `2`, `3` or `trail` — which target the alerts and demo account use |
| `ACCOUNT_SIZE` | `1000` | used only to print a position size in the alert |
| `RISK_PCT` | `1` | percent of the account risked per trade |

### 4. Turn on the schedule and the web page

1. **Actions** tab → enable workflows if asked → open **scan** → **Run workflow**. Within a minute your bot should message you "Scanner is set up…".
2. **Settings → Pages → Build and deployment → Source: Deploy from a branch**, branch `main`, folder `/docs`. After a minute the dashboard is live at the address GitHub shows.

That is all. The schedule now runs by itself.

## Asking the bot

Send these to your bot in Telegram at any time:

| Command | Reply |
|---|---|
| `/signal` | Signals from the last daily close with entry, stop and target, or "no new signals" |
| `/open` | Every open plan and where price is now, in R |
| `/mexc` | Open plans you can trade on MEXC (crypto, and US stocks it lists as futures) |
| `/near` | Coins and stocks within 5% of a breakout signal |
| `/coin BTC` | One coin or stock: open plan, last result, next signal level |
| `/results` | Everything that finished in the last 14 days, wins and losses |
| `/demo_trade` | The demo account: balance, success rate, open and finished trades |
| `/size 150.9 140.1` | Position size from entry and stop (optionally account and risk %), with where each leverage level would be liquidated |
| `/odds` | What the tests showed for each rule |
| `/status` | Last scan time, Bitcoin filter, next closes |
| `/dashboard` | Link to the dashboard |
| `/help` | The list of commands |

The bot answers only the chat id you set. The listener (`commands.py`) runs for 50 minutes at a time and starts its own successor, so a reply normally comes within a second or two; during the hand-over, about once an hour, it can take a minute. This keeps one GitHub runner busy all day, which is free on a public repository; if GitHub ever limits it, replies fall back to the half-hourly safety schedule. It reads what the last scan saved; it does not run a new scan.

## Good to know

- GitHub's scheduler can start a run several minutes late. Signals are based on the daily close, so a short delay changes nothing.
- GitHub pauses scheduled workflows in repositories with no activity for 60 days. The scan commits a small update each run, which keeps it active.
- To stop alerts, disable the workflow in the **Actions** tab or delete the two secrets.
- To test on your own computer first: `python scan.py` prints the alert it would send if the two Telegram values are not set.
- WhatsApp is not used because sending automated WhatsApp messages needs a paid business account; Telegram bots are free and built for this.

## Running it on your own computer instead

```bash
python ../pipeline/17_live_scanner.py
```

Then open http://localhost:8765. This refreshes levels every two minutes and keeps its own demo account in `docs/paper_local.json`, but it only works while the computer is on.

## What the test showed (so you know what the alerts are worth)

For the 40-coin list, over up to nine years:

| Target | Won | Average per trade | Since 2022 | Signals a week |
|---|---|---|---|---|
| Half the stop distance | 77% | +0.15R | 77%, +0.15R | about 3.6 |
| Same as the stop distance | 65% | +0.30R | 65%, +0.28R | about 2.4 |
| Twice the stop distance | 53% | +0.57R | 50%, +0.49R | about 1.7 |
| No target, trail the exit | 43% | +1.02R | 41%, +0.59R | about 1.9 |

These figures include the Bitcoin filter: a signal only counts on a day Bitcoin closes above its 200-day average. "Signals a week" is the rate while the filter is on. When Bitcoin is below the average (about half of all days since 2018, including all of 2022) there are no signals at all, and the bot sends one message when the filter switches.

R is the amount risked. The stop is usually 20–30% from entry, so positions must be small: at 1% risk a typical position is 3–5% of the account, with no leverage.

### US stocks and index funds (`stocks.txt`)

The same rule, unchanged, on 40 large US stocks and 12 stock-index funds, daily data from 2005 (`pipeline/26_short_and_markets.py`). No Bitcoin filter applies to these.

| Target | Won | Average per trade | Since 2022 | Signals a week |
|---|---|---|---|---|
| Half the stop distance | 74% | +0.10R | 73%, +0.09R | about 4.6 |
| Same as the stop distance | 62% | +0.23R | 61%, +0.21R | about 2.7 |
| Twice the stop distance | 49% | +0.47R | 48%, +0.44R | about 1.4 |
| No target, trail the exit | 47% | +0.22R | 46%, +0.18R | about 2.6 |

Read these with three cautions:

- About half of the stock result is the stock market's own long rise. Buying on random days with the same stop and target made +0.12R; breakout days made +0.23R (`pipeline/27_random_entry_control.py`). The signal adds roughly +0.12R, not the whole figure.
- The 40 stocks are today's largest companies, which is hindsight. The 12 index funds are the cleaner evidence (since 2022: 61% won, +0.21R at the 1R target), but since 2022 their advantage over random days is not statistically clear.
- Stock signals appear after the US close (about 21:00 UTC on weekdays). Data comes from Yahoo Finance, so stock prices on the dashboard update at each scan, not every second. Stops are about 7–11% from entry.

- A later, harder check (`pipeline/29_wide_sweep_reality_check.py`, 583 instruments) compared breakout days with random days in the same calendar month. For stocks the difference was zero (+0.008R, t 0.55; slightly negative since 2022). The stock breakout signals made money because stocks rose, not because the breakout day was a better day to buy. Treat the stock list as "buy stocks in a rising market with a fixed stop", nothing more.
- The same check on the crypto rule gave +0.12R over random days, but with a t-value of 1.6, which is weaker than the pooled figures above suggest.

Delete `stocks.txt` to keep the bot crypto-only.

### Dip rule on 583 US stocks and funds (`dip_tickers.txt`)

A second rule with its own tab on the dashboard and its own Telegram messages (`pipeline/28`, `29`, `30`).

- **Signal:** the stock is above its 200-day average and closes below its lower band (20-day average minus 2 standard deviations).
- **Entry:** that close. **Stop:** 3 average daily ranges below, about 7%.
- **Exit:** the first later daily close above the 20-day average, or after 20 trading days.

On a market-wide dip day (the S&P 500 more than 1.5 standard deviations below its own 20-day average) a smaller dip of 1 standard deviation also counts (`pipeline/33`, `34`). Market-wide dips won 72% in the test; stock-only dips 64%. Each card and alert says which kind it is.

| Measure | Result |
|---|---|
| Trades won | 70% (67% since 2022) |
| Average per trade | +0.12R (+0.07R since 2022) |
| Average winner / loser | +0.53R / −0.85R; 23% of trades hit the full stop |
| Signals | about 34 a week, in bursts: 32% of days have none, the busiest day had 203 |
| Account capped at 10 open, 0.5% risk each | about 4 trades a week, 66% won, +9% a year, worst year −18% (2008), deepest fall −24% |

The live logic was checked against the test: all 506 finished trades of the last 60 days match. The single-company list is today's S&P 500, which is hindsight; on the 86 funds alone the rule made +0.135R but gives only about 4 signals a week.

**MEXC.** Each scan reads MEXC's public contract list and marks the stocks MEXC offers as perpetual futures (174 of the 583 at the time of writing). The dashboard has an "Only what MEXC lists" switch, and alerts list those first. The test used real stock closing prices with no leverage. A MEXC stock future has its own price, funding payments and, with leverage, liquidation; none of that was tested.

Delete `dip_tickers.txt` to switch the dip rule off.

### What failed in the same tests

- Shorting crypto with the mirror-image rule lost money in every market condition, and lost most when Bitcoin was below its 200-day average. There is no tested signal for the months when the Bitcoin filter is off.
- Forex failed in both directions. Commodities were weak and did not beat random entries since 2022.

Limits you should keep in mind:

- The 40 coins were chosen by today's trading volume, which is hindsight. Coins that died are not in the test. Both make the numbers look better than they were.
- With the filter, 2019 and 2025 were about flat to slightly negative; without it 2022 was a clear loss. The filter helped only from 2022 onward; before that the signals it rejects were good ones.
- Signals cluster. Many open plans can be stopped together in a market drop.
- The same signals taken as $1 of margin at 50× were wiped out on about 9 trades in 10, with losing runs of 75 or more in a row, and an average result that could not be told apart from zero (`pipeline/23_leverage_variant.py`). The demo account shows that column next to the tested sizing so you can watch the difference.
- Past results do not guarantee future results.
