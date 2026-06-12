import time
import requests
import os
import csv
import pytz
try:
    import feedparser
    FEEDPARSER_AVAILABLE = True
except ImportError:
    FEEDPARSER_AVAILABLE = False
    print("feedparser not installed — news filter disabled")
from datetime import datetime, timedelta

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (StockBarsRequest, StockSnapshotRequest,
                                   StockLatestQuoteRequest)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# =========================
# CONFIG
# =========================

ALPACA_API_KEY     = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY  = os.environ.get("ALPACA_SECRET_KEY")
PUSHOVER_USER_KEY  = os.environ.get("PUSHOVER_USER_KEY")
PUSHOVER_API_TOKEN = os.environ.get("PUSHOVER_API_TOKEN")

TAKE_PROFIT      = 1.50
STOP_LOSS        = 0.75
TRAIL_ACTIVATION = 0.25  # was $0.50 — activates sooner to catch reversals
TRAIL_DISTANCE   = 0.20  # was $0.30 — tighter trail
MAX_WEEKLY_LOSS  = 4.50
MAX_DAY_TRADES   = 3
MAX_POSITIONS    = 1
MIN_PRICE        = 10
MAX_PRICE        = 25
MAX_QTY          = 4      # hard cap — $25×4=$100 = 40% of $250
BASE_QTY         = 2
QTY_PER_50       = 2
POSITION_PCT     = 0.35
MIN_TP_PCT       = 0.004
MIN_SL_PCT       = 0.005

# Entry filters
REGIME_THRESHOLD     = 0.001
VOLUME_MULTIPLIER    = 1.2
ORB_BUFFER           = 0.001
MIN_ORB_RANGE_PCT    = 0.005
MIN_SPY_RANGE_PCT    = 0.001
SCANNER_RETRY_MINUTE = 50

# Time filters
ORB_VALID_UNTIL_HOUR   = 11
ORB_VALID_UNTIL_MINUTE = 45
NO_ENTRY_AFTER_HOUR    = 15
NO_ENTRY_AFTER_MINUTE  = 45  # was 30 — avoid last 15min chop

# Milestone levels for notifications
MILESTONES = [260, 275, 300, 350, 500, 750, 1000]

PAPER_START_DATE  = os.environ.get("PAPER_START_DATE", "2026-05-13")
DATA_DIR          = os.environ.get("DATA_DIR", ".")
LOG_FILE          = os.path.join(DATA_DIR, "trade_log.csv")
SKIP_LOG_FILE     = os.path.join(DATA_DIR, "skip_log.csv")
MILESTONE_FILE    = os.path.join(DATA_DIR, "milestones.txt")
os.makedirs(DATA_DIR, exist_ok=True)

ET = pytz.timezone("America/New_York")

# =========================
# SECTOR ETFS + STOCKS
# =========================

SECTOR_ETFS = {
    "Technology":   "XLK",
    "Energy":       "XLE",
    "Financials":   "XLF",
    "Healthcare":   "XLV",
    "ConsumerDisc": "XLY",
    "Industrials":  "XLI",
    "Materials":    "XLB",
    "Utilities":    "XLU",
    "RealEstate":   "XLRE",
    "ConsumerStap": "XLP",
}

# Verified $10-$25 range June 2026
SECTOR_STOCKS = {
    "Technology":   ["SOFI", "SNAP", "INTC", "BB", "ERIC", "NOK"],
    "Energy":       ["OXY", "SLB", "DVN", "HAL", "MRO", "CVX"],
    "Financials":   ["SOFI", "NU", "HOOD", "BAC", "C", "WFC"],
    "Healthcare":   ["PFE", "NVAX", "MRNA", "CVS", "WBA"],
    "ConsumerDisc": ["F", "GM", "RIVN", "NIO", "PARA", "WBD"],
    "Industrials":  ["AAL", "UAL", "DAL", "GE"],
    "Materials":    ["CLF", "AA", "FCX", "MT", "X"],
    "Utilities":    ["PCG", "NEE", "SO", "EXC"],
    "RealEstate":   ["OPEN", "RKT", "RDFN"],
    "ConsumerStap": ["GO", "KR", "SFM"],
}

# Symbol → sector ETF map for sector strength filter
SYMBOL_SECTOR = {
    "SOFI": "XLF", "NU": "XLF", "HOOD": "XLF",
    "SNAP": "XLK", "INTC": "XLK",
    "OXY": "XLE", "SLB": "XLE", "DVN": "XLE",
    "PFE": "XLV", "NVAX": "XLV",
    "F": "XLY", "GM": "XLY", "RIVN": "XLY",
    "AAL": "XLI", "UAL": "XLI",
    "CLF": "XLB", "AA": "XLB",
    "PCG": "XLU", "OPEN": "XLRE", "GO": "XLP",
}

FALLBACK_SYMBOLS = ["SOFI", "F", "SNAP", "CLF", "AAL", "INTC", "NU"]

# News keywords that signal unpredictable moves — skip these stocks
NEWS_SKIP_KEYWORDS = [
    "earnings", "fda", "merger", "acquisition", "lawsuit",
    "investigation", "bankruptcy", "recall", "sec", "fraud",
    "guidance", "downgrade", "upgrade", "dividend"
]

# =========================
# INIT CLIENTS
# =========================

trading_client = TradingClient(
    ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True
)
data_client = StockHistoricalDataClient(
    ALPACA_API_KEY, ALPACA_SECRET_KEY
)

# =========================
# NOTIFICATIONS
# =========================

def notify(message):
    print(message)
    if not PUSHOVER_USER_KEY or not PUSHOVER_API_TOKEN:
        return
    try:
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={"token": PUSHOVER_API_TOKEN,
                  "user": PUSHOVER_USER_KEY,
                  "message": message},
            timeout=10
        )
        if resp.status_code != 200:
            print(f"Pushover error {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"Notification failed: {e}")

# =========================
# MILESTONE TRACKER
# =========================

def check_milestones(equity):
    """Sends a Pushover notification when account hits a new milestone."""
    try:
        reached = set()
        if os.path.exists(MILESTONE_FILE):
            with open(MILESTONE_FILE) as f:
                reached = set(int(x.strip()) for x in f.readlines() if x.strip())
        for m in MILESTONES:
            if equity >= m and m not in reached:
                reached.add(m)
                emojis = {260:"✅",275:"🎯",300:"🚀",
                          350:"💪",500:"💰",750:"🔥",1000:"🏆"}
                emoji = emojis.get(m, "🎉")
                notify(
                    f"{emoji} MILESTONE HIT: ${m}\n"
                    f"Account: ${equity:.2f}\n"
                    f"Return: +{((equity-250)/250*100):.1f}% from $250 start"
                )
                with open(MILESTONE_FILE, "w") as f:
                    f.write("\n".join(str(x) for x in sorted(reached)))
    except Exception as e:
        print(f"Milestone check failed: {e}")

# =========================
# TRADE LOGGER
# =========================

def init_log():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            csv.writer(f).writerow([
                "date","symbol","side","entry_price","exit_price",
                "qty","pnl","result","regime","exit_reason"
            ])
    if not os.path.exists(SKIP_LOG_FILE):
        with open(SKIP_LOG_FILE, "w", newline="") as f:
            csv.writer(f).writerow(["date","symbol","reason"])

def log_trade(symbol, side, entry_price, exit_price,
              qty, regime, exit_reason=""):
    pnl    = ((exit_price - entry_price) * qty if side == "LONG"
              else (entry_price - exit_price) * qty)
    result = "WIN" if pnl > 0 else "LOSS"
    with open(LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(ET).strftime("%Y-%m-%d %H:%M"),
            symbol, side,
            round(entry_price,2), round(exit_price,2),
            qty, round(pnl,2), result, regime, exit_reason
        ])
    return pnl, result

def log_skip(symbol, reason):
    with open(SKIP_LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(ET).strftime("%Y-%m-%d %H:%M"),
            symbol, reason
        ])

def read_all_trades():
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE) as f:
        return list(csv.DictReader(f))

def get_week_trades(trades):
    now  = datetime.now(ET)
    week = now.isocalendar()[1]
    return [t for t in trades if _trade_week(t) == (now.year, week)]

def _trade_week(trade):
    try:
        dt = datetime.strptime(trade["date"], "%Y-%m-%d %H:%M")
        return (dt.year, dt.isocalendar()[1])
    except Exception:
        return (0,0)

def get_recent_results(n=4):
    """Returns last N trade results for streak detection."""
    trades = read_all_trades()
    return [t["result"] for t in trades[-n:]]

# =========================
# STATS
# =========================

def calc_stats(trades):
    if not trades:
        return {"total":0,"wins":0,"losses":0,"win_rate":0,
                "total_pnl":0,"avg_win":0,"avg_loss":0,"profit_factor":0}
    wins      = [t for t in trades if t["result"]=="WIN"]
    losses    = [t for t in trades if t["result"]=="LOSS"]
    total_pnl = sum(float(t["pnl"]) for t in trades)
    avg_win   = sum(float(t["pnl"]) for t in wins)/len(wins) if wins else 0
    avg_loss  = sum(float(t["pnl"]) for t in losses)/len(losses) if losses else 0
    pf        = abs(avg_win/avg_loss) if avg_loss else 0
    # Exit reason breakdown
    exit_reasons = {}
    for t in trades:
        r = t.get("exit_reason","unknown")
        exit_reasons[r] = exit_reasons.get(r,0)+1
    return {
        "total":         len(trades),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(len(wins)/len(trades)*100,1),
        "total_pnl":     round(total_pnl,2),
        "avg_win":       round(avg_win,2),
        "avg_loss":      round(avg_loss,2),
        "profit_factor": round(pf,2),
        "exit_reasons":  exit_reasons,
    }

# =========================
# WIN/LOSS STREAK DETECTION
# Adapts SL and position size based on recent performance
# =========================

def get_streak_adjustments():
    """
    Returns (sl_multiplier, position_multiplier) based on recent results.
    Losing streak → tighter stops, smaller position
    Winning streak → slightly larger position
    """
    recent = get_recent_results(4)
    if len(recent) < 2:
        return 1.0, 1.0

    consecutive_losses = 0
    for r in reversed(recent):
        if r == "LOSS":
            consecutive_losses += 1
        else:
            break

    consecutive_wins = 0
    for r in reversed(recent):
        if r == "WIN":
            consecutive_wins += 1
        else:
            break

    if consecutive_losses >= 2:
        print(f"Losing streak ({consecutive_losses}) — tightening stops")
        return 0.75, 0.85   # tighter SL, smaller position
    elif consecutive_wins >= 2:
        print(f"Winning streak ({consecutive_wins}) — slight size boost")
        return 1.0, 1.15    # normal SL, slightly larger position
    return 1.0, 1.0

# =========================
# GO-LIVE RECOMMENDATION
# =========================

def check_go_live_recommendation():
    try:
        start = datetime.strptime(
            PAPER_START_DATE,"%Y-%m-%d"
        ).replace(tzinfo=ET)
        if (datetime.now(ET)-start).days/7 < 4:
            return
        trades = read_all_trades()
        if len(trades) < 8:
            return
        stats = calc_stats(trades)
        if stats["win_rate"] >= 55 and stats["profit_factor"] >= 1.3:
            rec = "✅ RECOMMEND GOING LIVE"
        elif stats["win_rate"] >= 50 and stats["profit_factor"] >= 1.0:
            rec = "⚠️ BORDERLINE — 2 more weeks"
        else:
            rec = "❌ NOT READY — keep paper trading"
        notify(
            f"4-WEEK PAPER REVIEW\n"
            f"Trades: {stats['total']} | Win rate: {stats['win_rate']}%\n"
            f"P&L: ${stats['total_pnl']} | "
            f"Profit factor: {stats['profit_factor']}\n{rec}"
        )
    except Exception as e:
        print(f"Go-live check failed: {e}")

# =========================
# STATE RECOVERY
# =========================

def recover_state():
    try:
        trades    = read_all_trades()
        now       = datetime.now(ET)
        cur_week  = now.isocalendar()[1]
        today_str = now.strftime("%Y-%m-%d")
        wt  = [t for t in trades if _trade_week(t)==(now.year,cur_week)]
        tt  = [t for t in wt if t["date"].startswith(today_str)]
        wtc = len(wt)
        wpnl= sum(float(t["pnl"]) for t in wt)
        override = os.environ.get("WEEK_TRADE_OVERRIDE")
        if override:
            wtc = max(wtc, int(override))
            print(f"WEEK_TRADE_OVERRIDE active — using {wtc}/3")
        elif wtc > 0:
            print(f"Recovered — week: {wtc}/3 | P&L: ${wpnl:.2f}")
        return wtc, wpnl, len(tt)
    except Exception as e:
        print(f"State recovery failed: {e}")
        return 0, 0.0, 0

# =========================
# DYNAMIC QTY
# =========================

def get_dynamic_qty():
    try:
        account    = trading_client.get_account()
        equity     = float(account.equity)
        sim_equity = min(equity, 250.0+max(0, equity-100000))
        growth     = max(0, sim_equity-250)
        qty        = BASE_QTY + int(growth/50)*QTY_PER_50
        max_safe   = int((sim_equity*0.40)/MAX_PRICE)
        qty        = max(1, min(qty, max_safe, MAX_QTY))
        print(f"Equity: ${equity:.2f} | QTY: {qty}")
        check_milestones(equity)
        return qty, equity
    except Exception as e:
        print(f"Could not get equity: {e}")
        return BASE_QTY, 250.0

# =========================
# WEEKLY REPORT
# =========================

def send_weekly_report(all_trades, equity, qty):
    wt         = get_week_trades(all_trades)
    all_stats  = calc_stats(all_trades)
    week_stats = calc_stats(wt)
    try:
        start = datetime.strptime(
            PAPER_START_DATE,"%Y-%m-%d"
        ).replace(tzinfo=ET)
        week_num = max(1, int((datetime.now(ET)-start).days/7))
    except Exception:
        week_num = 1

    projected = (f"24mo: ~${250*(1.05**24):,.0f}"
                 if all_stats["win_rate"] >= 50
                 else "Win rate below 50%")
    trend = ("🔥 Strong" if all_stats["win_rate"] >= 60
             else "✅ On track" if all_stats["win_rate"] >= 50
             else "⚠️ Below target")

    # Best exit reason
    er = all_stats.get("exit_reasons", {})
    best_exit = max(er, key=er.get) if er else "none"

    notify(
        f"📊 WEEKLY REPORT — Week {week_num}\n"
        f"Account: ${equity:.2f} | QTY: {qty}\n"
        f"─────────────────\n"
        f"THIS WEEK\n"
        f"Trades: {week_stats['total']}/3 | "
        f"W:{week_stats['wins']} L:{week_stats['losses']}\n"
        f"P&L: ${week_stats['total_pnl']}\n"
        f"─────────────────\n"
        f"ALL TIME\n"
        f"Trades: {all_stats['total']} | "
        f"Win: {all_stats['win_rate']}%\n"
        f"P&L: ${all_stats['total_pnl']} | "
        f"PF: {all_stats['profit_factor']}\n"
        f"Best exit: {best_exit}\n"
        f"Status: {trend} | {projected}"
    )

# =========================
# EOD SUMMARY
# =========================

def send_eod_summary(regime, watchlist, trades_today,
                     weekly_pnl, week_trade_count, no_trade_reason):
    reason_str = (f"No trade: {no_trade_reason}"
                  if trades_today == 0 and no_trade_reason else "")
    notify(
        f"📋 EOD — {datetime.now(ET).strftime('%b %d')}\n"
        f"Regime: {regime.upper()} | Trades: {trades_today}\n"
        f"Week: {week_trade_count}/3 | P&L: ${weekly_pnl:.2f}\n"
        f"Watchlist: {', '.join(watchlist[:5]) if watchlist else 'None'}\n"
        f"{reason_str}"
    )

# =========================
# MARKET HOURS
# =========================

def is_market_open():
    now = datetime.now(ET)
    if now.weekday() > 4:
        return False
    return (now.replace(hour=9,minute=30,second=0,microsecond=0)
            <= now <
            now.replace(hour=16,minute=0,second=0,microsecond=0))

def is_after_no_entry_time():
    now = datetime.now(ET)
    return (now.hour > NO_ENTRY_AFTER_HOUR or
           (now.hour == NO_ENTRY_AFTER_HOUR and
            now.minute >= NO_ENTRY_AFTER_MINUTE))

def is_orb_window_complete():
    now = datetime.now(ET)
    return now >= now.replace(hour=9,minute=45,second=0,microsecond=0)

def is_orb_still_valid():
    now = datetime.now(ET)
    return now < now.replace(
        hour=ORB_VALID_UNTIL_HOUR,
        minute=ORB_VALID_UNTIL_MINUTE,
        second=0,microsecond=0
    )

def is_near_market_close():
    now  = datetime.now(ET)
    diff = (now.replace(hour=16,minute=0,second=0,microsecond=0)-now).total_seconds()
    return 0 < diff <= 300

def is_early_session():
    now = datetime.now(ET)
    return now.hour < 10 or (now.hour == 10 and now.minute < 30)

def is_monday_morning():
    now = datetime.now(ET)
    return now.weekday() == 0 and now.hour == 9 and now.minute < 31

def is_market_close_time():
    now = datetime.now(ET)
    return now.hour == 16 and now.minute == 0

def is_scanner_retry_time():
    now = datetime.now(ET)
    return now.hour == 9 and now.minute == SCANNER_RETRY_MINUTE

def is_scanner_second_retry_time():
    now = datetime.now(ET)
    return now.hour == 10 and now.minute == 0

def is_premarket_scan_time():
    """9:00 AM — run pre-market gap scan."""
    now = datetime.now(ET)
    return now.hour == 9 and now.minute == 0

def get_time_quality_score():
    """
    ORB breakouts are strongest in first 30 min.
    Returns a quality multiplier for entry scoring.
    """
    now = datetime.now(ET)
    mins_since_open = (now.hour - 9) * 60 + now.minute - 30
    if mins_since_open < 30:  return 1.0
    if mins_since_open < 60:  return 0.8
    if mins_since_open < 90:  return 0.6
    return 0.4

# =========================
# MARKET REGIME
# =========================

def get_market_regime():
    try:
        req = StockBarsRequest(
            symbol_or_symbols=["SPY"],
            timeframe=TimeFrame.Minute, limit=100
        )
        df           = data_client.get_stock_bars(req).df.reset_index()
        open_price   = df["open"].iloc[0]
        cur          = df["close"].iloc[-1]
        daily_change = (cur - open_price) / open_price
        spy_range    = (df["high"].max() - df["low"].min()) / open_price
        if spy_range < MIN_SPY_RANGE_PCT:
            return "choppy"
        mid         = len(df)//2
        hh = df["high"].iloc[mid:].max() > df["high"].iloc[:mid].max()
        hl = df["low"].iloc[mid:].min()  > df["low"].iloc[:mid].min()
        lh = df["high"].iloc[mid:].max() < df["high"].iloc[:mid].max()
        ll = df["low"].iloc[mid:].min()  < df["low"].iloc[:mid].min()
        if daily_change > REGIME_THRESHOLD and (hh or hl):
            return "bullish"
        elif daily_change < -REGIME_THRESHOLD and (lh or ll):
            return "bearish"
        return "choppy"
    except Exception as e:
        print(f"Regime check failed: {e}")
        return "choppy"

def get_spy_daily_change():
    try:
        req = StockBarsRequest(
            symbol_or_symbols=["SPY"],
            timeframe=TimeFrame.Minute, limit=100
        )
        df = data_client.get_stock_bars(req).df.reset_index()
        return (df["close"].iloc[-1]-df["open"].iloc[0])/df["open"].iloc[0]
    except Exception:
        return 0.0

# =========================
# PRE-MARKET GAP SCANNER
# Runs at 9:00 AM — finds stocks with strong pre-market momentum
# =========================

def scan_premarket_gaps():
    """
    Finds stocks that gapped 2-5% pre-market vs yesterday's close.
    These become priority candidates — institutional interest is high.
    Gaps > 8% skipped — likely earnings, too unpredictable.
    """
    try:
        all_candidates = list(set(
            s for stocks in SECTOR_STOCKS.values() for s in stocks
        ))
        req   = StockSnapshotRequest(symbol_or_symbols=all_candidates)
        snaps = data_client.get_stock_snapshot(req)

        gap_stocks = []
        for sym, snap in snaps.items():
            try:
                if not snap or not snap.daily_bar or not getattr(snap, 'prev_daily_bar', None):
                    continue
                cur_open   = snap.daily_bar.open
                prev_close = getattr(snap, 'prev_daily_bar', None).close
                gap_pct    = (cur_open - prev_close) / prev_close

                # Sweet spot: 2-5% gap up, not earnings spike
                if 0.02 <= gap_pct <= 0.05:
                    if MIN_PRICE <= cur_open <= MAX_PRICE:
                        gap_stocks.append((sym, gap_pct))
            except Exception:
                continue

        gap_stocks.sort(key=lambda x: x[1], reverse=True)
        if gap_stocks:
            symbols = [s[0] for s in gap_stocks]
            gaps    = [f"{s[0]}+{s[1]:.1%}" for s in gap_stocks]
            print(f"Pre-market gaps: {gaps}")
            notify(f"🌅 Pre-market movers: {', '.join(gaps)}")
            return symbols
        return []
    except Exception as e:
        print(f"Pre-market scan failed: {e}")
        return []

# =========================
# NEWS FILTER
# Skips stocks with major news that causes unpredictable moves
# =========================

def has_major_news(symbol):
    """
    Checks Yahoo Finance RSS for breaking news.
    Skips if earnings, FDA, merger, lawsuit etc. found.
    Returns True if stock should be skipped.
    """
    if not FEEDPARSER_AVAILABLE:
        return False  # skip news check if feedparser not installed
    try:
        url  = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
        feed = feedparser.parse(url)
        today_str = datetime.now(ET).strftime("%Y-%m-%d")
        for entry in feed.entries[:5]:
            title    = entry.get("title", "").lower()
            pub_date = entry.get("published", "")
            # Only check today's news
            if today_str not in pub_date and datetime.now(ET).strftime("%a,") not in pub_date:
                continue
            for keyword in NEWS_SKIP_KEYWORDS:
                if keyword in title:
                    print(f"Skipping {symbol} — news: {entry['title'][:50]}")
                    return True
        return False
    except Exception:
        return False  # if RSS fails, don't block the trade

# =========================
# SECTOR STRENGTH FILTER
# Only enter if the stock's sector ETF is also green
# =========================

def is_sector_bullish(symbol):
    """
    Checks if the stock's sector ETF is up on the day.
    Prevents buying a stock swimming against its sector tide.
    """
    try:
        etf = SYMBOL_SECTOR.get(symbol)
        if not etf:
            return True  # unknown sector — don't block
        req = StockBarsRequest(
            symbol_or_symbols=[etf],
            timeframe=TimeFrame.Minute, limit=20
        )
        df = data_client.get_stock_bars(req).df.reset_index()
        return df["close"].iloc[-1] >= df["open"].iloc[0]
    except Exception:
        return True  # if check fails, don't block

# =========================
# SECTOR + STOCK SCANNER
# =========================

def get_top_sectors():
    try:
        etfs   = list(SECTOR_ETFS.values())
        req    = StockSnapshotRequest(symbol_or_symbols=etfs)
        snaps  = data_client.get_stock_snapshot(req)
        scored = []
        for sector, etf in SECTOR_ETFS.items():
            snap = snaps.get(etf)
            if not snap or not snap.daily_bar:
                continue
            cur  = snap.daily_bar.close
            prev = (getattr(snap, 'prev_daily_bar', None).close if getattr(snap, 'prev_daily_bar', None) else cur)
            pct  = (cur-prev)/prev if prev else 0
            scored.append((sector, pct))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [s[0] for s in scored[:3]]
    except Exception:
        return ["Technology","ConsumerDisc","Financials"]

def scan_symbols(regime, priority_symbols=None):
    """
    Builds watchlist from top sectors + pre-market gap stocks.
    priority_symbols: list from pre-market scan, added to front.
    """
    try:
        spy_change  = get_spy_daily_change()
        top_sectors = get_top_sectors()
        candidates  = []
        for sector in top_sectors:
            candidates.extend(SECTOR_STOCKS.get(sector,[]))
        candidates = list(set(candidates))
        if not candidates:
            return None

        req   = StockSnapshotRequest(symbol_or_symbols=candidates)
        snaps = data_client.get_stock_snapshot(req)

        prev_count = sum(1 for s in snaps.values() if s and getattr(s, 'prev_daily_bar', None))
        print(f"Prev bar: {prev_count}/{len(snaps)} stocks ready")

        scored = []
        for sym, snap in snaps.items():
            try:
                if not snap or not snap.daily_bar:
                    continue
                cur  = snap.daily_bar.close
                op   = snap.daily_bar.open
                vol  = snap.daily_bar.volume
                prev = (getattr(snap, 'prev_daily_bar', None).close if getattr(snap, 'prev_daily_bar', None) else cur)
                if cur < 5 or cur > 60:
                    continue
                if prev and abs((op-prev)/prev) > 0.05:
                    continue
                stock_change = (cur-prev)/prev if prev else 0
                rel_strength = stock_change - spy_change
                score        = vol * (abs(rel_strength)+0.001)
                if regime in ["bullish","choppy"] and cur > prev:
                    score *= 1.5
                if rel_strength > 0:
                    score *= 1.2
                # Boost pre-market gap stocks
                if priority_symbols and sym in priority_symbols:
                    score *= 2.0
                scored.append((sym, score))
            except Exception:
                continue

        print(f"Scanner found {len(scored)} stocks")
        scored.sort(key=lambda x: x[1], reverse=True)
        top = [s[0] for s in scored[:10]]

        if not top:
            scored_r = []
            for sym, snap in snaps.items():
                try:
                    if not snap or not snap.daily_bar:
                        continue
                    cur  = snap.daily_bar.close
                    prev = (getattr(snap, 'prev_daily_bar', None).close if getattr(snap, 'prev_daily_bar', None) else cur)
                    if 5 <= cur <= 60:
                        score = snap.daily_bar.volume * abs((cur-prev)/prev) if prev else 0
                        scored_r.append((sym,score))
                except Exception:
                    continue
            scored_r.sort(key=lambda x: x[1], reverse=True)
            top = [s[0] for s in scored_r[:10]]

        if not top:
            return None

        print(f"Watchlist ({regime}): {top}")
        notify(f"Market: {regime.upper()} | Scanning: {', '.join(top)}")
        return top
    except Exception as e:
        print(f"Scanner exception: {e}")
        return None

# =========================
# GET BARS (1-min and 5-min)
# =========================

def get_data(symbol):
    req  = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame.Minute, limit=100
    )
    return data_client.get_stock_bars(req).df.reset_index()

def get_data_5min(symbol):
    req = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame(5, TimeFrameUnit.Minute), limit=30
    )
    return data_client.get_stock_bars(req).df.reset_index()

# =========================
# ORB LEVELS
# =========================

def get_orb_levels(df):
    try:
        df["timestamp_et"] = df["timestamp"].dt.tz_convert(ET)
        now_et     = datetime.now(ET)
        open_time  = now_et.replace(hour=9,minute=30,second=0,microsecond=0)
        close_time = now_et.replace(hour=9,minute=45,second=0,microsecond=0)
        orb = df[(df["timestamp_et"]>=open_time)&(df["timestamp_et"]<close_time)]
        if len(orb) < 5:
            orb = df.iloc[:15]
    except Exception:
        orb = df.iloc[:15]
    return orb["high"].max(), orb["low"].min()

def is_orb_range_valid(high, low, price):
    orb_range = (high-low)/price
    if orb_range < MIN_ORB_RANGE_PCT:
        return False, f"ORB too tight ({orb_range:.3%})"
    return True, ""

# =========================
# MULTI-TIMEFRAME CONFIRMATION
# Both 1-min and 5-min must agree before entry
# =========================

def is_confirmed_on_5min(symbol, orb_high_1min):
    """
    Checks 5-minute chart also shows bullish structure.
    Dramatically reduces false breakouts.
    """
    try:
        df_5 = get_data_5min(symbol)
        if df_5.empty or len(df_5) < 5:
            return True  # not enough data — don't block

        df_5["timestamp_et"] = df_5["timestamp"].dt.tz_convert(ET)
        now_et    = datetime.now(ET)
        open_time = now_et.replace(hour=9,minute=30,second=0,microsecond=0)
        orb_5min  = df_5[df_5["timestamp_et"]>=open_time].iloc[:3]

        if len(orb_5min) < 1:
            return True

        high_5min  = orb_5min["high"].max()
        cur_5min   = df_5["close"].iloc[-1]

        # 5-min price must also be above 5-min ORB high
        return cur_5min > high_5min * (1 + ORB_BUFFER)
    except Exception:
        return True  # if check fails, don't block

# =========================
# DYNAMIC TAKE PROFIT (ATR-based)
# Wider TP on volatile days, tighter on flat days
# =========================

def get_atr_adjusted_tp(df, base_tp_per_share):
    """
    Uses Average True Range to scale TP with market volatility.
    Volatile day (ATR high) → TP stays at base or higher
    Flat day (ATR low) → TP scaled down for easier hit
    """
    try:
        atr        = (df["high"]-df["low"]).rolling(14).mean().iloc[-1]
        multiplier = max(0.6, min(1.5, atr/0.15))
        return base_tp_per_share * multiplier
    except Exception:
        return base_tp_per_share

# =========================
# VOLUME + MOMENTUM
# =========================

def has_volume_confirmation(df):
    if is_early_session():
        return True
    return df["volume"].iloc[-1] > df["volume"].mean() * VOLUME_MULTIPLIER

def has_momentum(df):
    if len(df) < 6:
        return True
    recent = df["close"].iloc[-6:]
    return recent.iloc[3:].mean() > recent.iloc[:3].mean()

# =========================
# SMART EXIT SYSTEM
# =========================

def get_vwap(df):
    typical = (df["high"]+df["low"]+df["close"])/3
    return (typical*df["volume"]).sum()/df["volume"].sum()

def is_momentum_dying(df):
    if len(df) < 4:
        return False
    closes = df["close"].iloc[-4:].values
    gains  = [closes[i+1]-closes[i] for i in range(3)]
    return gains[2] < gains[1] and gains[2] < 0

def is_volume_drying_up(df):
    if len(df) < 10:
        return False
    return df["volume"].iloc[-3:].mean() < df["volume"].mean()*0.5

def is_uptrend_broken(df):
    if len(df) < 4:
        return False
    highs = df["high"].iloc[-4:].values
    return highs[-1] < highs[-2] and highs[-1] < highs[-3]

def is_volume_spike(df):
    if len(df) < 10:
        return False
    return df["volume"].iloc[-1] > df["volume"].mean()*3.0

def should_smart_exit(df, pos, current_price, entry_time):
    qty  = pos["qty"]
    gain = ((current_price-pos["entry"])*qty if pos["side"]=="LONG"
            else (pos["entry"]-current_price)*qty)
    # Always check momentum/uptrend signals regardless of gain
    # Only skip profit-protection exits when losing
    vwap = get_vwap(df)
    # --- PROFIT PROTECTION exits (only when winning) ---
    if gain > 0:
        if is_volume_spike(df):
            return True, f"Volume spike — blow-off top +${gain:.2f}"
        if current_price < vwap and gain > 0.10*qty:
            return True, f"Below VWAP +${gain:.2f}"
        if is_momentum_dying(df) and gain > pos["tp"]*0.20:
            return True, f"Momentum dying +${gain:.2f}"
        if is_volume_drying_up(df) and gain > 0.10*qty:
            return True, f"Volume drying up +${gain:.2f}"
        if is_uptrend_broken(df) and gain > pos["tp"]*0.15:
            return True, f"Uptrend broken +${gain:.2f}"

    # --- LOSS LIMITING exits (fire even when losing) ---
    # If stock was up and is now coming back down fast — exit early
    peak_price = pos.get("peak_price", pos["entry"])
    peak_gain  = (peak_price - pos["entry"]) * qty
    drawdown   = peak_gain - gain  # how much we've given back from peak

    # If we gave back more than 50% of the peak gain — exit to protect capital
    if peak_gain > 0.20*qty and drawdown > peak_gain * 0.50:
        return True, f"Peak giveback {drawdown:.2f} from +{peak_gain:.2f}"

    # If momentum is dying AND we are losing — don't wait for SL
    if is_momentum_dying(df) and gain < 0 and abs(gain) > 0.10*qty:
        return True, f"Momentum dying in loss ${gain:.2f}"

    # Stale trade check — works regardless of gain/loss
    minutes_held = (datetime.now(ET)-entry_time).seconds/60
    if minutes_held > 45 and abs(gain) < pos["tp"]*0.20 and gain < 0:
        return True, f"Stale losing trade {minutes_held:.0f}min ${gain:.2f}"

    return False, ""

def update_trailing_stop(pos, current_price):
    """Moves trailing stop up as price rises. Locks in profit."""
    qty  = pos["qty"]
    gain = ((current_price - pos["entry"]) * qty
            if pos["side"] == "LONG"
            else (pos["entry"] - current_price) * qty)
    if gain < TRAIL_ACTIVATION:
        return pos
    if pos["side"] == "LONG":
        peak = max(pos.get("peak_price", pos["entry"]), current_price)
        trail_dist = pos.get("trail_distance", TRAIL_DISTANCE)
        pos["peak_price"]  = peak
        pos["trail_price"] = max(
            pos.get("trail_price", 0), peak - trail_dist
        )
    return pos

def is_trailing_stop_hit(pos, current_price):
    if "trail_price" not in pos:
        return False
    return pos["side"]=="LONG" and current_price <= pos["trail_price"]

# =========================
# PARTIAL PROFIT TAKING
# Sells half at TP, lets other half run with tight trailing stop
# =========================

def take_partial_profit(symbol, pos, current_price, regime):
    """
    Sells half the position at TP.
    Remaining half gets a tighter trailing stop to capture more upside.
    Returns updated position or None if fully closed.
    """
    half_qty = max(1, pos["qty"]//2)
    exit_side = OrderSide.SELL if pos["side"]=="LONG" else OrderSide.BUY

    place_order(symbol, exit_side, half_qty)
    partial_pnl = ((current_price-pos["entry"])*half_qty
                   if pos["side"]=="LONG"
                   else (pos["entry"]-current_price)*half_qty)

    notify(
        f"💰 PARTIAL EXIT {symbol} x{half_qty} @ ${current_price:.2f}\n"
        f"+${partial_pnl:.2f} | Letting {pos['qty']-half_qty} shares run\n"
        f"Tight trail now active"
    )

    remaining = pos["qty"] - half_qty
    if remaining <= 0:
        return None, partial_pnl

    # Update position — tighter trail on remaining shares
    pos["qty"]            = remaining
    pos["trail_distance"] = TRAIL_DISTANCE * 0.5  # tighter on remainder
    pos["partial_taken"]  = True
    return pos, partial_pnl

# =========================
# POSITION SIZING
# =========================

def get_position_qty(price, equity, pos_multiplier=1.0):
    risk      = equity * POSITION_PCT * pos_multiplier
    qty       = max(1, int(risk/price))
    return min(qty, MAX_QTY)

def get_tp_sl(entry_price, qty, df=None, sl_multiplier=1.0):
    base_per_share_tp = min(TAKE_PROFIT, entry_price*0.06)
    base_per_share_sl = min(STOP_LOSS,   entry_price*0.03)
    base_per_share_tp = max(base_per_share_tp, entry_price*MIN_TP_PCT)
    base_per_share_sl = max(base_per_share_sl, entry_price*MIN_SL_PCT)

    # ATR adjustment for TP
    if df is not None:
        base_per_share_tp = get_atr_adjusted_tp(df, base_per_share_tp)

    # Streak adjustment for SL
    base_per_share_sl *= sl_multiplier

    return base_per_share_tp*qty, base_per_share_sl*qty

# =========================
# PDT CHECK
# =========================

def get_day_trade_count():
    for attempt in range(3):
        try:
            return int(trading_client.get_account().daytrade_count)
        except Exception as e:
            print(f"PDT attempt {attempt+1} failed: {e}")
            time.sleep(5)
    return 0

# =========================
# ORDER EXECUTION
# =========================

def place_order(symbol, side, qty):
    trading_client.submit_order(MarketOrderRequest(
        symbol=symbol, qty=qty, side=side,
        time_in_force=TimeInForce.DAY
    ))

# =========================
# TRADE TARGETING
# =========================

def get_trades_allowed_today(week_trade_count, trades_today):
    now         = datetime.now(ET)
    weekday     = now.weekday()
    trades_left = MAX_DAY_TRADES - week_trade_count
    if trades_left <= 0:
        return 0
    if weekday >= 3:
        return trades_left
    return min(trades_left, max(1, trades_left-(4-weekday)))

# =========================
# STATE
# =========================

init_log()
_wtc, _wpnl, _tt = recover_state()

positions        = {}
pending_orders   = set()
watchlist        = []
premarket_gaps   = []
scanner_failed   = False
regime           = "choppy"
last_date        = None
last_week        = None
weekly_pnl       = _wpnl
week_trade_count = _wtc
trades_today     = _tt
report_sent      = False
eod_sent         = False
premarket_done   = False
no_trade_reason  = ""
qty              = BASE_QTY
equity           = 250.0
entry_times      = {}
partial_pnl_total = 0.0

notify(
    f"Bot started — PDT margin account\n"
    f"TP: ${TAKE_PROFIT} | SL: ${STOP_LOSS} | "
    f"Trail: +${TRAIL_ACTIVATION}\n"
    f"Position: {int(POSITION_PCT*100)}% | "
    f"Multi-TF + Smart exits active\n"
    f"Week: {week_trade_count}/3 | P&L: ${weekly_pnl:.2f}"
)

# =========================
# MAIN LOOP
# =========================

while True:
    try:
        now_et   = datetime.now(ET)
        today    = now_et.date()
        cur_week = now_et.isocalendar()[1]

        # ── NEW WEEK RESET ──
        if last_week is not None and last_week != cur_week:
            weekly_pnl        = 0.0
            week_trade_count  = 0
            report_sent       = False
            partial_pnl_total = 0.0
            print("New week — reset")
        last_week = cur_week

        # ── NEW DAY RESET ──
        if last_date != today:
            positions         = {}
            pending_orders    = set()
            entry_times       = {}
            watchlist         = []
            premarket_gaps    = []
            premarket_done    = False
            scanner_failed    = False
            regime            = "choppy"
            trades_today      = 0
            eod_sent          = False
            no_trade_reason   = ""
            partial_pnl_total = 0.0
            last_date         = today
            qty, equity       = get_dynamic_qty()
            allowed           = get_trades_allowed_today(week_trade_count, 0)
            notify(
                f"New day: {today}\n"
                f"QTY: {qty} | Equity: ${equity:.2f}\n"
                f"Target: {allowed} trade(s) | Week: {week_trade_count}/3"
            )
            check_go_live_recommendation()

        # ── WEEKLY REPORT — Monday 9 AM ──
        if is_monday_morning() and not report_sent:
            send_weekly_report(read_all_trades(), equity, qty)
            report_sent = True

        if not is_market_open():
            print(f"Market closed — "
                  f"{now_et.strftime('%Y-%m-%d %H:%M ET')} — sleeping 60s")
            time.sleep(60)
            continue

        # ── EOD SUMMARY ──
        if is_market_close_time() and not eod_sent:
            send_eod_summary(regime, watchlist, trades_today,
                             weekly_pnl, week_trade_count, no_trade_reason)
            eod_sent = True

        # ── PRE-MARKET GAP SCAN — 9:00 AM ──
        if is_premarket_scan_time() and not premarket_done:
            premarket_gaps = scan_premarket_gaps()
            premarket_done = True

        # ── BUILD WATCHLIST at 9:45 AM ──
        if not watchlist and is_orb_window_complete() and not scanner_failed:
            regime    = get_market_regime()
            result    = scan_symbols(regime, premarket_gaps)
            if result is None:
                scanner_failed = True
            else:
                watchlist      = result
                scanner_failed = False

        # -- SCANNER RETRY at 9:50 AM --
        if scanner_failed and not watchlist and is_scanner_retry_time():
            print("Retrying scanner at 9:50 AM...")
            regime = get_market_regime()
            result = scan_symbols(regime, premarket_gaps)
            if result is not None:
                watchlist      = result
                scanner_failed = False
                print(f"9:50 retry succeeded: {watchlist}")
            # If still None, wait for 10:00 AM retry below

        # -- SCANNER SECOND RETRY at 10:00 AM --
        if scanner_failed and not watchlist and is_scanner_second_retry_time():
            print("Final scanner retry at 10:00 AM...")
            regime = get_market_regime()
            result = scan_symbols(regime, premarket_gaps)
            if result is not None:
                watchlist      = result
                scanner_failed = False
                print(f"10 AM retry succeeded: {watchlist}")
            else:
                # All retries exhausted -- use fallback
                watchlist      = FALLBACK_SYMBOLS
                scanner_failed = False
                notify(f"Scanner failed 3x -- using fallback: "
                       f"{', '.join(watchlist)}")

        if not watchlist:
            print(f"Waiting for scanner — {now_et.strftime('%H:%M ET')}")
            time.sleep(60)
            continue

        # ── REFRESH REGIME every 30 min ──
        if now_et.minute % 30 == 0:
            new_regime = get_market_regime()
            if new_regime != regime:
                notify(f"Regime: {regime.upper()} → {new_regime.upper()}")
                regime = new_regime

        # ── CIRCUIT BREAKER ──
        if weekly_pnl <= -MAX_WEEKLY_LOSS:
            no_trade_reason = f"Weekly loss limit (${weekly_pnl:.2f})"
            print(no_trade_reason)
            time.sleep(60)
            continue

        # ── THURSDAY REMINDER ──
        if (now_et.weekday()==3 and now_et.hour==9
                and now_et.minute==45 and week_trade_count==0):
            notify("⚠️ Thursday — 0 trades this week")

        # ── GET STREAK ADJUSTMENTS ──
        sl_mult, pos_mult = get_streak_adjustments()

        pdt_used      = get_day_trade_count()
        allowed_today = get_trades_allowed_today(week_trade_count, trades_today)

        # ── FORCE EXIT NEAR CLOSE ──
        if is_near_market_close():
            for sym, pos in list(positions.items()):
                try:
                    exit_side  = (OrderSide.SELL if pos["side"]=="LONG"
                                  else OrderSide.BUY)
                    exit_price = get_data(sym)["close"].iloc[-1]
                    place_order(sym, exit_side, pos["qty"])
                    pnl, _ = log_trade(
                        sym, pos["side"], pos["entry"],
                        exit_price, pos["qty"], regime, "EOD forced close"
                    )
                    weekly_pnl       += pnl
                    week_trade_count += 1
                    trades_today     += 1
                    notify(
                        f"EOD CLOSE {sym} | ${pnl:+.2f}\n"
                        f"Week P&L: ${weekly_pnl:.2f} | "
                        f"Trades: {week_trade_count}/3"
                    )
                    del positions[sym]
                    entry_times.pop(sym, None)
                except Exception as e:
                    print(f"EOD close failed {sym}: {e}")
            time.sleep(60)
            continue

        # ── PROCESS EACH SYMBOL ──
        for symbol in watchlist:
            try:
                df = get_data(symbol)
                if df.empty or len(df) < 20:
                    continue

                current_price = df["close"].iloc[-1]
                high, low     = get_orb_levels(df)

                # ── MANAGE OPEN POSITION ──
                if symbol in positions:
                    pos        = positions[symbol]
                    entry_time = entry_times.get(symbol, now_et)
                    trail_dist = pos.get("trail_distance", TRAIL_DISTANCE)
                    pos        = update_trailing_stop(pos, current_price)
                    positions[symbol] = pos

                    gain = ((current_price-pos["entry"])*pos["qty"]
                            if pos["side"]=="LONG"
                            else (pos["entry"]-current_price)*pos["qty"])
                    exit_side = (OrderSide.SELL if pos["side"]=="LONG"
                                 else OrderSide.BUY)

                    # 1. Take profit — partial exit first
                    if gain >= pos["tp"] and not pos.get("partial_taken"):
                        new_pos, ppnl = take_partial_profit(
                            symbol, pos, current_price, regime
                        )
                        partial_pnl_total += ppnl
                        weekly_pnl        += ppnl
                        if new_pos is None:
                            log_trade(symbol, pos["side"], pos["entry"],
                                      current_price, pos["qty"],
                                      regime, "TP full exit")
                            week_trade_count += 1
                            trades_today     += 1
                            del positions[symbol]
                            entry_times.pop(symbol, None)
                        else:
                            positions[symbol] = new_pos

                    # 2. Full TP hit (after partial already taken)
                    elif gain >= pos["tp"] and pos.get("partial_taken"):
                        place_order(symbol, exit_side, pos["qty"])
                        pnl, _ = log_trade(
                            symbol, pos["side"], pos["entry"],
                            current_price, pos["qty"], regime, "TP hit"
                        )
                        weekly_pnl       += pnl
                        week_trade_count += 1
                        trades_today     += 1
                        notify(
                            f"✅ TP HIT {symbol} @ ${current_price:.2f}\n"
                            f"+${pnl:.2f} | Week: {week_trade_count}/3"
                        )
                        del positions[symbol]
                        entry_times.pop(symbol, None)

                    # 3. Trailing stop
                    elif is_trailing_stop_hit(pos, current_price):
                        place_order(symbol, exit_side, pos["qty"])
                        pnl, _ = log_trade(
                            symbol, pos["side"], pos["entry"],
                            current_price, pos["qty"], regime,
                            f"Trail stop ${pos['trail_price']:.2f}"
                        )
                        weekly_pnl       += pnl
                        week_trade_count += 1
                        trades_today     += 1
                        notify(
                            f"🔒 TRAIL STOP {symbol} @ ${current_price:.2f}\n"
                            f"${pnl:+.2f} | Week: {week_trade_count}/3"
                        )
                        del positions[symbol]
                        entry_times.pop(symbol, None)

                    else:
                        # 4. Smart exit signals
                        should_exit, exit_reason = should_smart_exit(
                            df, pos, current_price, entry_time
                        )
                        if should_exit:
                            place_order(symbol, exit_side, pos["qty"])
                            pnl, _ = log_trade(
                                symbol, pos["side"], pos["entry"],
                                current_price, pos["qty"],
                                regime, exit_reason
                            )
                            weekly_pnl       += pnl
                            week_trade_count += 1
                            trades_today     += 1
                            notify(
                                f"🧠 SMART EXIT {symbol} "
                                f"@ ${current_price:.2f}\n"
                                f"${pnl:+.2f} | {exit_reason}\n"
                                f"Week: {week_trade_count}/3"
                            )
                            del positions[symbol]
                            entry_times.pop(symbol, None)

                        # 5. Hard stop loss
                        elif gain <= -pos["sl"]:
                            place_order(symbol, exit_side, pos["qty"])
                            pnl, _ = log_trade(
                                symbol, pos["side"], pos["entry"],
                                current_price, pos["qty"], regime, "SL hit"
                            )
                            weekly_pnl       += pnl
                            week_trade_count += 1
                            trades_today     += 1
                            notify(
                                f"🛑 SL HIT {symbol} @ ${current_price:.2f}\n"
                                f"${pnl:.2f} | Week: {week_trade_count}/3"
                            )
                            del positions[symbol]
                            entry_times.pop(symbol, None)

                # ── LOOK FOR NEW ENTRY ──
                elif (not is_after_no_entry_time()
                      and is_orb_window_complete()
                      and is_orb_still_valid()
                      and pdt_used < MAX_DAY_TRADES
                      and week_trade_count < MAX_DAY_TRADES
                      and allowed_today > 0
                      and len(positions) < MAX_POSITIONS
                      and symbol not in pending_orders
                      and has_volume_confirmation(df)
                      and has_momentum(df)):

                    # Strict price check
                    if current_price < MIN_PRICE or current_price > MAX_PRICE:
                        log_skip(symbol, f"Price ${current_price:.2f} out of range")
                        continue

                    # ORB range check
                    valid_orb, skip_reason = is_orb_range_valid(
                        high, low, current_price
                    )
                    if not valid_orb:
                        log_skip(symbol, skip_reason)
                        no_trade_reason = skip_reason
                        continue

                    # News filter
                    if has_major_news(symbol):
                        log_skip(symbol, "Major news today — skipping")
                        continue

                    # Sector strength filter
                    if not is_sector_bullish(symbol):
                        log_skip(symbol, "Sector ETF is red — skipping")
                        continue

                    # Multi-timeframe confirmation
                    if not is_confirmed_on_5min(symbol, high):
                        log_skip(symbol, "Not confirmed on 5min chart")
                        no_trade_reason = "5min not confirmed"
                        continue

                    # Time quality — skip weak late entries unless behind
                    time_quality = get_time_quality_score()
                    if time_quality < 0.5 and week_trade_count >= 1:
                        log_skip(symbol, f"Low time quality ({time_quality})")
                        continue

                    trade_qty      = get_position_qty(
                        current_price, equity, pos_mult
                    )
                    tp_amt, sl_amt = get_tp_sl(
                        current_price, trade_qty, df, sl_mult
                    )

                    if (regime in ["bullish","choppy"]
                            and current_price > high*(1+ORB_BUFFER)):
                        pending_orders.add(symbol)
                        place_order(symbol, OrderSide.BUY, trade_qty)
                        positions[symbol] = {
                            "side":        "LONG",
                            "entry":       current_price,
                            "tp":          tp_amt,
                            "sl":          sl_amt,
                            "qty":         trade_qty,
                            "peak_price":  current_price,
                            "partial_taken": False,
                        }
                        entry_times[symbol] = now_et
                        pdt_used       += 1
                        allowed_today  -= 1
                        no_trade_reason = ""

                        gap_tag = " 🌅GAP" if symbol in premarket_gaps else ""
                        notify(
                            f"📈 BUY {symbol} x{trade_qty} "
                            f"@ ${current_price:.2f}{gap_tag}\n"
                            f"TP: +${tp_amt:.2f} | SL: -${sl_amt:.2f}\n"
                            f"Trail: +${TRAIL_ACTIVATION} | "
                            f"Partial exit at TP\n"
                            f"Week: {week_trade_count+1}/3 | "
                            f"Time quality: {time_quality:.0%}"
                        )
                    else:
                        reason = ("Bearish" if regime=="bearish"
                                  else f"Price ${current_price:.2f} "
                                       f"below ORB ${high:.2f}")
                        log_skip(symbol, reason)
                        if not no_trade_reason:
                            no_trade_reason = reason

            except Exception as e:
                if "not allowed to short" not in str(e):
                    print(f"Error processing {symbol}: {e}")
                continue

        time.sleep(60)

    except Exception as e:
        notify(f"ERROR: {e}")
        print(f"Error: {e}")
        time.sleep(60)
