import time
import requests
import os
import csv
import pytz
from datetime import datetime, timedelta

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# =========================
# CONFIG
# =========================

ALPACA_API_KEY     = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY  = os.environ.get("ALPACA_SECRET_KEY")
PUSHOVER_USER_KEY  = os.environ.get("PUSHOVER_USER_KEY")
PUSHOVER_API_TOKEN = os.environ.get("PUSHOVER_API_TOKEN")

TAKE_PROFIT     = 2.00   # $2.00 target — 2:1 ratio
STOP_LOSS       = 1.00   # $1.00 stop
MAX_WEEKLY_LOSS = 6.00   # weekly circuit breaker
MAX_DAY_TRADES  = 3      # PDT limit
MAX_POSITIONS   = 1      # one position at a time
MIN_PRICE       = 5      # skip stocks below this
MAX_PRICE       = 49     # skip stocks above this (strict — keeps BAC out)
MAX_QTY         = 10     # hard cap regardless of account size
BASE_QTY        = 2      # starting shares
QTY_PER_50      = 1      # +1 share per $50 account growth above $250
MIN_TP_PCT      = 0.004  # minimum 0.4% for TP floor
MIN_SL_PCT      = 0.002  # minimum 0.2% for SL floor

# Entry filters
REGIME_THRESHOLD      = 0.001  # SPY needs 0.1% move to call bullish
VOLUME_MULTIPLIER     = 1.2    # volume check multiplier (after 10:30 AM only)
ORB_BUFFER            = 0.001  # price needs 0.1% above ORB high
MIN_ORB_RANGE_PCT     = 0.005  # ORB range must be at least 0.5% of price
MIN_SPY_RANGE_PCT     = 0.003  # SPY must move at least 0.3% total to trade
RELATIVE_STRENGTH_MIN = 0.001  # stock must outperform SPY by 0.1% minimum

# ORB validity window — no new entries after 11:45 AM
ORB_VALID_UNTIL_HOUR   = 11
ORB_VALID_UNTIL_MINUTE = 45

# Scanner retry — if scanner fails at 9:45, retry at this minute
SCANNER_RETRY_MINUTE = 50  # retry at 9:50 AM

NO_ENTRY_AFTER_HOUR   = 15
NO_ENTRY_AFTER_MINUTE = 30

PAPER_START_DATE = os.environ.get("PAPER_START_DATE", "2026-05-13")

# DATA_DIR uses Railway Volume if set, otherwise current folder
# Add Volume in Railway dashboard, mount at /data
# Then add Railway Variable: DATA_DIR = /data
DATA_DIR      = os.environ.get("DATA_DIR", ".")
LOG_FILE      = os.path.join(DATA_DIR, "trade_log.csv")
SKIP_LOG_FILE = os.path.join(DATA_DIR, "skip_log.csv")

# Create data directory if it doesn't exist
os.makedirs(DATA_DIR, exist_ok=True)

ET = pytz.timezone("America/New_York")

# =========================
# SECTOR ETFS + STOCKS
# BAC removed from fallback — above MAX_PRICE
# All stocks verified under $49
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

SECTOR_STOCKS = {
    "Technology":   ["AMD", "INTC", "MU", "PLTR", "SOFI", "SNAP"],
    "Energy":       ["OXY", "SLB", "HAL", "DVN"],
    "Financials":   ["SOFI", "COIN", "NU", "HOOD"],
    "Healthcare":   ["PFE", "MRNA", "NVAX", "SAVA"],
    "ConsumerDisc": ["F", "GM", "RIVN", "NIO", "LCID"],
    "Industrials":  ["GE", "AAL", "UAL", "DAL"],
    "Materials":    ["FCX", "AA", "CLF", "MT"],
    "Utilities":    ["NEE", "SO", "PCG"],
    "RealEstate":   ["SPG", "OPEN"],
    "ConsumerStap": ["WMT", "KO"],
}

# Fallback watchlist — all verified under $49, no BAC
FALLBACK_SYMBOLS = ["AMD", "SOFI", "F", "PLTR", "NIO", "SNAP", "RIVN"]

# =========================
# INIT CLIENTS
# =========================

trading_client = TradingClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    paper=True  # <-- Change to False when going live
)

data_client = StockHistoricalDataClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY
)

# =========================
# NOTIFICATIONS
# =========================

def notify(message):
    print(message)
    if not PUSHOVER_USER_KEY or not PUSHOVER_API_TOKEN:
        print("WARNING: Pushover env vars missing")
        return
    try:
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": PUSHOVER_API_TOKEN,
                "user":  PUSHOVER_USER_KEY,
                "message": message
            },
            timeout=10
        )
        if resp.status_code != 200:
            print(f"Pushover error {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"Notification failed: {e}")

# =========================
# TRADE LOGGER
# =========================

def init_log():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "date", "symbol", "side", "entry_price",
                "exit_price", "qty", "pnl", "result", "regime"
            ])
    if not os.path.exists(SKIP_LOG_FILE):
        with open(SKIP_LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["date", "symbol", "reason"])

def log_trade(symbol, side, entry_price, exit_price, qty, regime):
    pnl    = ((exit_price - entry_price) * qty if side == "LONG"
              else (entry_price - exit_price) * qty)
    result = "WIN" if pnl > 0 else "LOSS"
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now(ET).strftime("%Y-%m-%d %H:%M"),
            symbol, side,
            round(entry_price, 2), round(exit_price, 2),
            qty, round(pnl, 2), result, regime
        ])
    return pnl, result

def log_skip(symbol, reason):
    with open(SKIP_LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now(ET).strftime("%Y-%m-%d %H:%M"),
            symbol, reason
        ])

def read_all_trades():
    trades = []
    if not os.path.exists(LOG_FILE):
        return trades
    with open(LOG_FILE, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append(row)
    return trades

def get_week_trades(trades):
    now  = datetime.now(ET)
    week = now.isocalendar()[1]
    year = now.year
    return [t for t in trades if _trade_week(t) == (year, week)]

def _trade_week(trade):
    try:
        dt = datetime.strptime(trade["date"], "%Y-%m-%d %H:%M")
        return (dt.year, dt.isocalendar()[1])
    except Exception:
        return (0, 0)

# =========================
# STATS
# =========================

def calc_stats(trades):
    if not trades:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "total_pnl": 0, "avg_win": 0, "avg_loss": 0, "profit_factor": 0}
    wins      = [t for t in trades if t["result"] == "WIN"]
    losses    = [t for t in trades if t["result"] == "LOSS"]
    total_pnl = sum(float(t["pnl"]) for t in trades)
    avg_win   = sum(float(t["pnl"]) for t in wins)   / len(wins)   if wins   else 0
    avg_loss  = sum(float(t["pnl"]) for t in losses) / len(losses) if losses else 0
    pf        = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    return {
        "total":         len(trades),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(len(wins) / len(trades) * 100, 1),
        "total_pnl":     round(total_pnl, 2),
        "avg_win":       round(avg_win, 2),
        "avg_loss":      round(avg_loss, 2),
        "profit_factor": round(pf, 2)
    }

# =========================
# GO-LIVE RECOMMENDATION
# =========================

def check_go_live_recommendation():
    try:
        start = datetime.strptime(PAPER_START_DATE, "%Y-%m-%d").replace(tzinfo=ET)
        weeks = (datetime.now(ET) - start).days / 7
        if weeks < 4:
            return
        trades = read_all_trades()
        if len(trades) < 8:
            return
        stats = calc_stats(trades)
        if stats["win_rate"] >= 55 and stats["profit_factor"] >= 1.3:
            rec = "✅ RECOMMEND GOING LIVE"
        elif stats["win_rate"] >= 50 and stats["profit_factor"] >= 1.0:
            rec = "⚠️ BORDERLINE — 2 more weeks recommended"
        else:
            rec = "❌ NOT READY — keep paper trading"
        notify(
            f"4-WEEK PAPER REVIEW\n"
            f"Trades: {stats['total']} | Win rate: {stats['win_rate']}%\n"
            f"P&L: ${stats['total_pnl']} | Profit factor: {stats['profit_factor']}\n"
            f"{rec}"
        )
    except Exception as e:
        print(f"Go-live check failed: {e}")

# =========================
# DYNAMIC QTY
# =========================

def get_dynamic_qty():
    try:
        account    = trading_client.get_account()
        equity     = float(account.equity)
        sim_equity = min(equity, 250.0 + max(0, equity - 100000))
        growth     = max(0, sim_equity - 250)
        qty        = BASE_QTY + int(growth / 50) * QTY_PER_50
        max_safe   = int((sim_equity * 0.40) / MAX_PRICE)
        qty        = max(1, min(qty, max_safe, MAX_QTY))
        print(f"Equity: ${equity:.2f} | Simulated: ${sim_equity:.2f} | QTY: {qty}")
        return qty, equity
    except Exception as e:
        print(f"Could not get account equity: {e}")
        return BASE_QTY, 250.0

# =========================
# WEEKLY REPORT
# =========================

def send_weekly_report(all_trades, equity, qty):
    week_trades = get_week_trades(all_trades)
    all_stats   = calc_stats(all_trades)
    week_stats  = calc_stats(week_trades)
    try:
        start         = datetime.strptime(PAPER_START_DATE, "%Y-%m-%d").replace(tzinfo=ET)
        weeks_running = max(1, int((datetime.now(ET) - start).days / 7))
    except Exception:
        weeks_running = 1
    if all_stats["win_rate"] >= 50:
        projected      = 250 * (1.05 ** 24)
        projection_str = f"24mo projection: ~${projected:,.0f}"
    else:
        projection_str = "Win rate below 50% — review strategy"
    if all_stats["win_rate"] >= 60:
        trend = "🔥 Strong"
    elif all_stats["win_rate"] >= 50:
        trend = "✅ On track"
    else:
        trend = "⚠️ Below target"
    notify(
        f"📊 WEEKLY REPORT — Week {weeks_running}\n"
        f"QTY: {qty} | PDT: 3 trades/week\n"
        f"─────────────────\n"
        f"THIS WEEK\n"
        f"Trades: {week_stats['total']}/3 | "
        f"W: {week_stats['wins']} L: {week_stats['losses']}\n"
        f"P&L: ${week_stats['total_pnl']}\n"
        f"─────────────────\n"
        f"ALL TIME\n"
        f"Trades: {all_stats['total']} | "
        f"Win rate: {all_stats['win_rate']}%\n"
        f"Total P&L: ${all_stats['total_pnl']}\n"
        f"Avg win: ${all_stats['avg_win']} | "
        f"Avg loss: ${all_stats['avg_loss']}\n"
        f"Profit factor: {all_stats['profit_factor']}\n"
        f"Status: {trend} | {projection_str}"
    )

# =========================
# EOD SUMMARY
# =========================

def send_eod_summary(regime, watchlist, trades_today,
                     weekly_pnl, week_trade_count, no_trade_reason):
    reason_str = f"No trade: {no_trade_reason}" if (trades_today == 0
                                                     and no_trade_reason) else ""
    notify(
        f"📋 END OF DAY — "
        f"{datetime.now(ET).strftime('%b %d')}\n"
        f"Regime: {regime.upper()} | "
        f"Trades today: {trades_today}\n"
        f"Week: {week_trade_count}/3 | "
        f"Week P&L: ${weekly_pnl:.2f}\n"
        f"Watchlist: "
        f"{', '.join(watchlist[:5]) if watchlist else 'None'}\n"
        f"{reason_str}"
    )

# =========================
# MARKET HOURS
# =========================

def is_market_open():
    now = datetime.now(ET)
    if now.weekday() > 4:
        return False
    market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now < market_close

def is_after_no_entry_time():
    now = datetime.now(ET)
    return (now.hour > NO_ENTRY_AFTER_HOUR or
           (now.hour == NO_ENTRY_AFTER_HOUR and
            now.minute >= NO_ENTRY_AFTER_MINUTE))

def is_orb_window_complete():
    now = datetime.now(ET)
    return now >= now.replace(hour=9, minute=45, second=0, microsecond=0)

def is_orb_still_valid():
    now    = datetime.now(ET)
    cutoff = now.replace(
        hour=ORB_VALID_UNTIL_HOUR,
        minute=ORB_VALID_UNTIL_MINUTE,
        second=0, microsecond=0
    )
    return now < cutoff

def is_near_market_close():
    now          = datetime.now(ET)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    diff         = (market_close - now).total_seconds()
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
    """9:50 AM — retry scanner if it failed at 9:45."""
    now = datetime.now(ET)
    return (now.hour == 9 and now.minute == SCANNER_RETRY_MINUTE)

# =========================
# MARKET REGIME
# Includes SPY range check — won't trade flat days
# =========================

def get_market_regime():
    try:
        req = StockBarsRequest(
            symbol_or_symbols=["SPY"],
            timeframe=TimeFrame.Minute,
            limit=100
        )
        df            = data_client.get_stock_bars(req).df.reset_index()
        open_price    = df["open"].iloc[0]
        current_price = df["close"].iloc[-1]
        daily_change  = (current_price - open_price) / open_price

        # SPY range check — sit out if market is too flat
        spy_range = (df["high"].max() - df["low"].min()) / open_price
        if spy_range < MIN_SPY_RANGE_PCT:
            print(f"SPY range too tight ({spy_range:.3%}) — choppy")
            return "choppy"

        mid          = len(df) // 2
        higher_highs = df["high"].iloc[mid:].max() > df["high"].iloc[:mid].max()
        higher_lows  = df["low"].iloc[mid:].min()  > df["low"].iloc[:mid].min()
        lower_highs  = df["high"].iloc[mid:].max() < df["high"].iloc[:mid].max()
        lower_lows   = df["low"].iloc[mid:].min()  < df["low"].iloc[:mid].min()

        if daily_change > REGIME_THRESHOLD and (higher_highs or higher_lows):
            return "bullish"
        elif daily_change < -REGIME_THRESHOLD and (lower_highs or lower_lows):
            return "bearish"
        else:
            return "choppy"
    except Exception as e:
        print(f"Regime check failed: {e}")
        return "choppy"

def get_spy_daily_change():
    """Returns SPY % change today — used for relative strength filter."""
    try:
        req = StockBarsRequest(
            symbol_or_symbols=["SPY"],
            timeframe=TimeFrame.Minute,
            limit=100
        )
        df  = data_client.get_stock_bars(req).df.reset_index()
        return (df["close"].iloc[-1] - df["open"].iloc[0]) / df["open"].iloc[0]
    except Exception:
        return 0.0

# =========================
# SECTOR SCANNER — top 3 sectors
# =========================

def get_top_sectors():
    try:
        etf_list = list(SECTOR_ETFS.values())
        req      = StockSnapshotRequest(symbol_or_symbols=etf_list)
        snaps    = data_client.get_stock_snapshot(req)
        scored   = []
        for sector, etf in SECTOR_ETFS.items():
            snap = snaps.get(etf)
            if not snap or not snap.daily_bar:
                continue
            try:
                cur  = snap.daily_bar.close
                prev = snap.prev_daily_bar.close if snap.prev_daily_bar else cur
                pct  = (cur - prev) / prev if prev else 0
                scored.append((sector, pct))
            except Exception:
                continue
        scored.sort(key=lambda x: x[1], reverse=True)
        return [s[0] for s in scored[:3]]
    except Exception as e:
        print(f"Sector scan failed: {e}")
        return ["Technology", "ConsumerDisc", "Financials"]

# =========================
# STOCK SCANNER
# Added: relative strength filter vs SPY
# Improved: safer prev_daily_bar handling
# =========================

def scan_symbols(regime):
    """
    Returns scored watchlist or None if data isn't ready yet.
    Returning None triggers a retry at 9:50 AM.
    """
    try:
        # Get SPY change for relative strength
        spy_change = get_spy_daily_change()

        top_sectors = get_top_sectors()
        candidates  = []
        for sector in top_sectors:
            candidates.extend(SECTOR_STOCKS.get(sector, []))
        candidates = list(set(candidates))

        req   = StockSnapshotRequest(symbol_or_symbols=candidates)
        snaps = data_client.get_stock_snapshot(req)

        # Count how many have prev_daily_bar — if too few, data isn't ready
        prev_bar_count = sum(
            1 for snap in snaps.values()
            if snap and snap.prev_daily_bar
        )
        total = len(snaps)
        if total > 0 and prev_bar_count / total < 0.5:
            print(f"Only {prev_bar_count}/{total} stocks have prev bar — retrying later")
            return None  # signal to retry

        scored = []
        for sym, snap in snaps.items():
            try:
                if not snap or not snap.daily_bar:
                    continue
                cur  = snap.daily_bar.close
                op   = snap.daily_bar.open
                vol  = snap.daily_bar.volume
                prev = snap.prev_daily_bar.close if snap.prev_daily_bar else cur

                # Price filter
                if cur < MIN_PRICE or cur > MAX_PRICE:
                    continue

                # Earnings gap filter
                if prev and abs((op - prev) / prev) > 0.05:
                    print(f"Skipping {sym} — earnings gap")
                    continue

                # Relative strength filter — must outperform SPY
                stock_change = (cur - prev) / prev if prev else 0
                rel_strength = stock_change - spy_change
                if regime in ["bullish", "choppy"] and rel_strength < RELATIVE_STRENGTH_MIN:
                    continue  # skip stocks lagging behind SPY

                score = vol * abs(rel_strength)
                if regime in ["bullish", "choppy"] and cur > prev:
                    score *= 1.5

                scored.append((sym, score))
            except Exception as e:
                print(f"Error scoring {sym}: {e}")
                continue

        scored.sort(key=lambda x: x[1], reverse=True)
        top = [s[0] for s in scored[:10]]

        if not top:
            print("Scanner returned empty after filters")
            return None  # retry rather than use fallback immediately

        print(f"Watchlist ({regime}): {top}")
        notify(f"Market: {regime.upper()} | Scanning: {', '.join(top)}")
        return top

    except Exception as e:
        print(f"Scanner exception: {e}")
        return None  # retry on any exception

# =========================
# GET BARS
# =========================

def get_data(symbol):
    req  = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame.Minute,
        limit=100
    )
    bars = data_client.get_stock_bars(req)
    return bars.df.reset_index()

# =========================
# ORB LEVELS
# =========================

def get_orb_levels(df):
    try:
        df["timestamp_et"] = df["timestamp"].dt.tz_convert(ET)
        now_et     = datetime.now(ET)
        open_time  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
        close_time = now_et.replace(hour=9,  minute=45, second=0, microsecond=0)
        orb_bars   = df[(df["timestamp_et"] >= open_time) &
                        (df["timestamp_et"] < close_time)]
        if len(orb_bars) < 5:
            orb_bars = df.iloc[:15]
    except Exception:
        orb_bars = df.iloc[:15]
    return orb_bars["high"].max(), orb_bars["low"].min()

def is_orb_range_valid(high, low, current_price):
    orb_range = (high - low) / current_price
    if orb_range < MIN_ORB_RANGE_PCT:
        return False, f"ORB range too tight ({orb_range:.3%})"
    return True, ""

# =========================
# VOLUME CONFIRMATION
# Skipped before 10:30 AM
# =========================

def has_volume_confirmation(df):
    if is_early_session():
        return True
    return df["volume"].iloc[-1] > df["volume"].mean() * VOLUME_MULTIPLIER

# =========================
# DYNAMIC QTY per stock price
# =========================

def get_position_qty(price, equity):
    risk_per_trade = equity * 0.20
    qty            = max(1, int(risk_per_trade / price))
    return min(qty, MAX_QTY)

# =========================
# DYNAMIC TP/SL
# =========================

def get_tp_sl(entry_price, qty):
    tp = max(TAKE_PROFIT, entry_price * MIN_TP_PCT) * qty
    sl = max(STOP_LOSS,   entry_price * MIN_SL_PCT) * qty
    return tp, sl

# =========================
# PDT CHECK — retries on timeout
# =========================

def get_day_trade_count():
    for attempt in range(3):
        try:
            account = trading_client.get_account()
            return int(account.daytrade_count)
        except Exception as e:
            print(f"PDT check attempt {attempt + 1} failed: {e}")
            time.sleep(5)
    print("PDT check failed after 3 attempts — defaulting to 0")
    return 0

# =========================
# ORDER EXECUTION
# =========================

def place_order(symbol, side, qty):
    order = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY
    )
    trading_client.submit_order(order)

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
    return min(trades_left, max(1, trades_left - (4 - weekday)))

# =========================
# STATE RECOVERY
# Reads trade log on startup so restarts don't lose weekly counts
# =========================

def recover_state():
    """
    Reads trade_log.csv to recover week_trade_count and weekly_pnl
    so a mid-week restart doesn't think it has 3 fresh trades.
    Also recovers trades_today for the current day.
    """
    try:
        all_trades  = read_all_trades()
        now         = datetime.now(ET)
        cur_week    = now.isocalendar()[1]
        cur_year    = now.year
        today_str   = now.strftime("%Y-%m-%d")

        week_trades = [t for t in all_trades
                       if _trade_week(t) == (cur_year, cur_week)]
        today_trades = [t for t in week_trades
                        if t["date"].startswith(today_str)]

        week_trade_count = len(week_trades)
        weekly_pnl       = sum(float(t["pnl"]) for t in week_trades)
        trades_today     = len(today_trades)

        if week_trade_count > 0:
            print(
                f"State recovered from log — "
                f"week trades: {week_trade_count}/3 | "
                f"week P&L: ${weekly_pnl:.2f} | "
                f"trades today: {trades_today}"
            )
        return week_trade_count, weekly_pnl, trades_today

    except Exception as e:
        print(f"State recovery failed: {e} — starting fresh")
        return 0, 0.0, 0

# =========================
# STATE
# =========================

init_log()

# Recover persistent state from trade log
_wtc, _wpnl, _tt  = recover_state()

positions        = {}
watchlist        = []
scanner_failed   = False
regime           = "choppy"
last_date        = None
last_week        = None
weekly_pnl       = _wpnl
week_trade_count = _wtc
trades_today     = _tt
report_sent      = False
eod_sent         = False
no_trade_reason  = ""
qty              = BASE_QTY
equity           = 250.0

notify(
    f"Bot started — PDT margin account\n"
    f"TP: ${TAKE_PROFIT} | SL: ${STOP_LOSS} | Ratio: 2:1\n"
    f"Week trades so far: {week_trade_count}/3 | "
    f"Week P&L: ${weekly_pnl:.2f}\n"
    f"ORB valid until "
    f"{ORB_VALID_UNTIL_HOUR}:{ORB_VALID_UNTIL_MINUTE:02d} ET"
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
        if last_week != cur_week:
            weekly_pnl       = 0.0
            week_trade_count = 0
            report_sent      = False
            last_week        = cur_week
            print("New week — reset")

        # ── NEW DAY RESET ──
        if last_date != today:
            positions       = {}
            watchlist       = []
            scanner_failed  = False
            regime          = "choppy"
            trades_today    = 0
            eod_sent        = False
            no_trade_reason = ""
            last_date       = today
            qty, equity     = get_dynamic_qty()
            allowed         = get_trades_allowed_today(week_trade_count, 0)
            print(f"New day: {today} | QTY: {qty} | Equity: ${equity:.2f}")
            notify(
                f"New day: {today}\n"
                f"QTY: {qty} | Equity: ${equity:.2f}\n"
                f"Target: {allowed} trade(s) | "
                f"Week: {week_trade_count}/3"
            )
            check_go_live_recommendation()

        # ── WEEKLY REPORT — Monday 9 AM ET ──
        if is_monday_morning() and not report_sent:
            all_trades = read_all_trades()
            send_weekly_report(all_trades, equity, qty)
            report_sent = True

        if not is_market_open():
            print(f"Market closed — "
                  f"{now_et.strftime('%Y-%m-%d %H:%M ET')} — sleeping 60s")
            time.sleep(60)
            continue

        # ── EOD SUMMARY — 4 PM ET ──
        if is_market_close_time() and not eod_sent:
            send_eod_summary(
                regime, watchlist, trades_today,
                weekly_pnl, week_trade_count, no_trade_reason
            )
            eod_sent = True

        # ── BUILD WATCHLIST at 9:45 AM ──
        if not watchlist and is_orb_window_complete():
            regime = get_market_regime()
            result = scan_symbols(regime)
            if result is None:
                # Data not ready yet — will retry at 9:50 AM
                scanner_failed = True
                print("Scanner returned None — will retry at 9:50 AM")
            else:
                watchlist      = result
                scanner_failed = False

        # ── SCANNER RETRY at 9:50 AM ──
        if (scanner_failed and not watchlist
                and is_scanner_retry_time()):
            print("Retrying scanner at 9:50 AM...")
            regime = get_market_regime()
            result = scan_symbols(regime)
            if result is not None:
                watchlist      = result
                scanner_failed = False
                print(f"Scanner retry succeeded: {watchlist}")
            else:
                # Final fallback — use hardcoded list
                watchlist = FALLBACK_SYMBOLS
                notify(
                    f"Scanner failed twice — using fallback: "
                    f"{', '.join(watchlist)}"
                )
                scanner_failed = False

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

        # ── WEEKLY CIRCUIT BREAKER ──
        if weekly_pnl <= -MAX_WEEKLY_LOSS:
            no_trade_reason = f"Weekly loss limit (${weekly_pnl:.2f})"
            print(f"Weekly loss limit hit — sitting out")
            time.sleep(60)
            continue

        # ── THURSDAY REMINDER ──
        if (now_et.weekday() == 3 and now_et.hour == 9
                and now_et.minute == 45 and week_trade_count == 0):
            notify(
                f"⚠️ Thursday — 0 trades this week\n"
                f"Attempting all 3 trades today/tomorrow"
            )

        pdt_used      = get_day_trade_count()
        allowed_today = get_trades_allowed_today(week_trade_count, trades_today)

        # ── FORCE EXIT NEAR CLOSE ──
        if is_near_market_close():
            for sym, pos in list(positions.items()):
                try:
                    exit_side  = OrderSide.SELL if pos["side"] == "LONG" else OrderSide.BUY
                    exit_price = get_data(sym)["close"].iloc[-1]
                    place_order(sym, exit_side, pos["qty"])
                    pnl, result = log_trade(
                        sym, pos["side"], pos["entry"],
                        exit_price, pos["qty"], regime
                    )
                    weekly_pnl       += pnl
                    week_trade_count += 1
                    trades_today     += 1
                    notify(
                        f"EOD CLOSE {pos['side']} {sym} | ${pnl:+.2f}\n"
                        f"Week P&L: ${weekly_pnl:.2f} | "
                        f"Trades: {week_trade_count}/3"
                    )
                    del positions[sym]
                except Exception as e:
                    print(f"EOD close failed for {sym}: {e}")
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
                    pos  = positions[symbol]
                    gain = ((current_price - pos["entry"]) * pos["qty"]
                            if pos["side"] == "LONG"
                            else (pos["entry"] - current_price) * pos["qty"])

                    if gain >= pos["tp"]:
                        exit_side = OrderSide.SELL if pos["side"] == "LONG" else OrderSide.BUY
                        place_order(symbol, exit_side, pos["qty"])
                        pnl, result = log_trade(
                            symbol, pos["side"], pos["entry"],
                            current_price, pos["qty"], regime
                        )
                        weekly_pnl       += pnl
                        week_trade_count += 1
                        trades_today     += 1
                        notify(
                            f"✅ WIN {pos['side']} {symbol} "
                            f"@ ${current_price:.2f}\n"
                            f"+${pnl:.2f} | Week P&L: ${weekly_pnl:.2f}\n"
                            f"Trades: {week_trade_count}/3"
                        )
                        del positions[symbol]

                    elif gain <= -pos["sl"]:
                        exit_side = OrderSide.SELL if pos["side"] == "LONG" else OrderSide.BUY
                        place_order(symbol, exit_side, pos["qty"])
                        pnl, result = log_trade(
                            symbol, pos["side"], pos["entry"],
                            current_price, pos["qty"], regime
                        )
                        weekly_pnl       += pnl
                        week_trade_count += 1
                        trades_today     += 1
                        notify(
                            f"🛑 LOSS {pos['side']} {symbol} "
                            f"@ ${current_price:.2f}\n"
                            f"${pnl:.2f} | Week P&L: ${weekly_pnl:.2f}\n"
                            f"Trades: {week_trade_count}/3"
                        )
                        del positions[symbol]

                # ── LOOK FOR NEW ENTRY ──
                elif (not is_after_no_entry_time()
                      and is_orb_window_complete()
                      and is_orb_still_valid()
                      and pdt_used < MAX_DAY_TRADES
                      and week_trade_count < MAX_DAY_TRADES
                      and allowed_today > 0
                      and len(positions) < MAX_POSITIONS
                      and has_volume_confirmation(df)):

                    # ORB range check
                    valid_orb, skip_reason = is_orb_range_valid(
                        high, low, current_price
                    )
                    if not valid_orb:
                        log_skip(symbol, skip_reason)
                        no_trade_reason = skip_reason
                        continue

                    trade_qty      = get_position_qty(current_price, equity)
                    tp_amt, sl_amt = get_tp_sl(current_price, trade_qty)

                    # Longs only (bullish or choppy)
                    if (regime in ["bullish", "choppy"]
                            and current_price > high * (1 + ORB_BUFFER)):
                        place_order(symbol, OrderSide.BUY, trade_qty)
                        positions[symbol] = {
                            "side":  "LONG",
                            "entry": current_price,
                            "tp":    tp_amt,
                            "sl":    sl_amt,
                            "qty":   trade_qty
                        }
                        pdt_used        += 1
                        allowed_today   -= 1
                        no_trade_reason  = ""
                        notify(
                            f"📈 BUY {symbol} x{trade_qty} "
                            f"@ ${current_price:.2f}\n"
                            f"TP: +${tp_amt:.2f} | SL: -${sl_amt:.2f}\n"
                            f"Week: {week_trade_count+1}/3 | "
                            f"PDT: {pdt_used}/3"
                        )
                    else:
                        if regime == "bearish":
                            reason = "Bearish — longs disabled"
                        elif current_price <= high * (1 + ORB_BUFFER):
                            reason = (f"Price ${current_price:.2f} "
                                      f"below ORB high ${high:.2f}")
                        else:
                            reason = "No setup"
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
