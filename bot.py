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

TAKE_PROFIT      = 1.50   # base dollar TP per share
STOP_LOSS        = 0.75   # base dollar SL per share — 2:1 ratio
TRAIL_ACTIVATION = 0.50   # trailing stop activates once up $0.50 total
TRAIL_DISTANCE   = 0.30   # trailing stop trails by $0.30 from peak
MAX_WEEKLY_LOSS  = 4.50   # circuit breaker
MAX_DAY_TRADES   = 3      # PDT limit
MAX_POSITIONS    = 1      # one position at a time
MIN_PRICE        = 10     # strict entry price floor
MAX_PRICE        = 25     # strict entry price ceiling
MAX_QTY          = 10     # hard cap on shares
BASE_QTY         = 2      # starting qty
QTY_PER_50       = 2      # +2 shares per $50 growth
POSITION_PCT     = 0.35   # use 35% of account per trade (was 20%)
MIN_TP_PCT       = 0.004
MIN_SL_PCT       = 0.005

# Entry filters
REGIME_THRESHOLD      = 0.001
VOLUME_MULTIPLIER     = 1.2
ORB_BUFFER            = 0.001
MIN_ORB_RANGE_PCT     = 0.005
MIN_SPY_RANGE_PCT     = 0.001
RELATIVE_STRENGTH_MIN = 0.001
SCANNER_RETRY_MINUTE  = 50

# ORB validity — no new entries after 11:45 AM
ORB_VALID_UNTIL_HOUR   = 11
ORB_VALID_UNTIL_MINUTE = 45

NO_ENTRY_AFTER_HOUR   = 15
NO_ENTRY_AFTER_MINUTE = 30

PAPER_START_DATE = os.environ.get("PAPER_START_DATE", "2026-05-13")
DATA_DIR         = os.environ.get("DATA_DIR", ".")
LOG_FILE         = os.path.join(DATA_DIR, "trade_log.csv")
SKIP_LOG_FILE    = os.path.join(DATA_DIR, "skip_log.csv")
os.makedirs(DATA_DIR, exist_ok=True)

ET = pytz.timezone("America/New_York")

# =========================
# SECTOR ETFS + STOCKS
# All verified $10-$25 as of June 2026
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
    "Technology":   ["SOFI", "SNAP", "INTC"],
    "Energy":       ["OXY", "SLB", "DVN"],
    "Financials":   ["SOFI", "NU", "HOOD"],
    "Healthcare":   ["PFE", "NVAX"],
    "ConsumerDisc": ["F", "GM", "RIVN"],
    "Industrials":  ["AAL", "UAL"],
    "Materials":    ["CLF", "AA"],
    "Utilities":    ["PCG"],
    "RealEstate":   ["OPEN"],
    "ConsumerStap": ["GO"],
}

FALLBACK_SYMBOLS = ["SOFI", "F", "SNAP", "CLF", "AAL", "INTC", "NU"]

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
                "exit_price", "qty", "pnl", "result",
                "regime", "exit_reason"
            ])
    if not os.path.exists(SKIP_LOG_FILE):
        with open(SKIP_LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["date", "symbol", "reason"])

def log_trade(symbol, side, entry_price, exit_price,
              qty, regime, exit_reason=""):
    pnl    = ((exit_price - entry_price) * qty if side == "LONG"
              else (entry_price - exit_price) * qty)
    result = "WIN" if pnl > 0 else "LOSS"
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now(ET).strftime("%Y-%m-%d %H:%M"),
            symbol, side,
            round(entry_price, 2), round(exit_price, 2),
            qty, round(pnl, 2), result, regime, exit_reason
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
                "total_pnl": 0, "avg_win": 0, "avg_loss": 0,
                "profit_factor": 0}
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
        start = datetime.strptime(
            PAPER_START_DATE, "%Y-%m-%d"
        ).replace(tzinfo=ET)
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
            f"Trades: {stats['total']} | "
            f"Win rate: {stats['win_rate']}%\n"
            f"P&L: ${stats['total_pnl']} | "
            f"Profit factor: {stats['profit_factor']}\n"
            f"{rec}"
        )
    except Exception as e:
        print(f"Go-live check failed: {e}")

# =========================
# STATE RECOVERY
# =========================

def recover_state():
    try:
        all_trades   = read_all_trades()
        now          = datetime.now(ET)
        cur_week     = now.isocalendar()[1]
        cur_year     = now.year
        today_str    = now.strftime("%Y-%m-%d")
        week_trades  = [t for t in all_trades
                        if _trade_week(t) == (cur_year, cur_week)]
        today_trades = [t for t in week_trades
                        if t["date"].startswith(today_str)]
        wtc  = len(week_trades)
        wpnl = sum(float(t["pnl"]) for t in week_trades)
        tt   = len(today_trades)
        override = os.environ.get("WEEK_TRADE_OVERRIDE")
        if override is not None:
            wtc = max(wtc, int(override))
            print(f"WEEK_TRADE_OVERRIDE active — using {wtc}/3")
        elif wtc > 0:
            print(f"Recovered — week trades: {wtc}/3 | "
                  f"P&L: ${wpnl:.2f} | today: {tt}")
        return wtc, wpnl, tt
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
        sim_equity = min(equity, 250.0 + max(0, equity - 100000))
        growth     = max(0, sim_equity - 250)
        qty        = BASE_QTY + int(growth / 50) * QTY_PER_50
        max_safe   = int((sim_equity * 0.40) / MAX_PRICE)
        qty        = max(1, min(qty, max_safe, MAX_QTY))
        print(f"Equity: ${equity:.2f} | QTY: {qty}")
        return qty, equity
    except Exception as e:
        print(f"Could not get equity: {e}")
        return BASE_QTY, 250.0

# =========================
# WEEKLY REPORT
# =========================

def send_weekly_report(all_trades, equity, qty):
    week_trades = get_week_trades(all_trades)
    all_stats   = calc_stats(all_trades)
    week_stats  = calc_stats(week_trades)
    try:
        start         = datetime.strptime(
            PAPER_START_DATE, "%Y-%m-%d"
        ).replace(tzinfo=ET)
        weeks_running = max(1, int((datetime.now(ET) - start).days / 7))
    except Exception:
        weeks_running = 1
    projected      = 250 * (1.05 ** 24) if all_stats["win_rate"] >= 50 else 0
    projection_str = (f"24mo projection: ~${projected:,.0f}"
                      if projected else "Win rate below 50% — review strategy")
    trend = ("🔥 Strong" if all_stats["win_rate"] >= 60
             else "✅ On track" if all_stats["win_rate"] >= 50
             else "⚠️ Below target")

    # Exit reason breakdown
    exit_reasons = {}
    for t in all_trades:
        reason = t.get("exit_reason", "unknown")
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
    reason_str = " | ".join([f"{k}:{v}" for k, v in exit_reasons.items()])

    notify(
        f"📊 WEEKLY REPORT — Week {weeks_running}\n"
        f"QTY: {qty} | Account: ${equity:.2f}\n"
        f"─────────────────\n"
        f"THIS WEEK\n"
        f"Trades: {week_stats['total']}/3 | "
        f"W:{week_stats['wins']} L:{week_stats['losses']}\n"
        f"P&L: ${week_stats['total_pnl']}\n"
        f"─────────────────\n"
        f"ALL TIME\n"
        f"Trades: {all_stats['total']} | "
        f"Win rate: {all_stats['win_rate']}%\n"
        f"Total P&L: ${all_stats['total_pnl']}\n"
        f"Avg win: ${all_stats['avg_win']} | "
        f"Avg loss: ${all_stats['avg_loss']}\n"
        f"Profit factor: {all_stats['profit_factor']}\n"
        f"Exits: {reason_str}\n"
        f"Status: {trend} | {projection_str}"
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
    now = datetime.now(ET)
    return now.hour == 9 and now.minute == SCANNER_RETRY_MINUTE

# =========================
# MARKET REGIME
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
        spy_range     = (df["high"].max() - df["low"].min()) / open_price
        if spy_range < MIN_SPY_RANGE_PCT:
            print(f"SPY range tight ({spy_range:.3%}) — cautious mode")
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
    try:
        req = StockBarsRequest(
            symbol_or_symbols=["SPY"],
            timeframe=TimeFrame.Minute,
            limit=100
        )
        df = data_client.get_stock_bars(req).df.reset_index()
        return (df["close"].iloc[-1] - df["open"].iloc[0]) / df["open"].iloc[0]
    except Exception:
        return 0.0

# =========================
# SECTOR SCANNER
# =========================

def get_top_sectors():
    try:
        etf_list = list(SECTOR_ETFS.values())
        if not etf_list:
            return ["Technology", "ConsumerDisc", "Financials"]
        req    = StockSnapshotRequest(symbol_or_symbols=etf_list)
        snaps  = data_client.get_stock_snapshot(req)
        scored = []
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

def scan_symbols(regime):
    try:
        spy_change  = get_spy_daily_change()
        top_sectors = get_top_sectors()
        candidates  = []
        for sector in top_sectors:
            candidates.extend(SECTOR_STOCKS.get(sector, []))
        candidates = list(set(candidates))
        if not candidates:
            return None

        req   = StockSnapshotRequest(symbol_or_symbols=candidates)
        snaps = data_client.get_stock_snapshot(req)

        prev_bar_count = sum(
            1 for snap in snaps.values()
            if snap and snap.prev_daily_bar
        )
        if len(snaps) > 0 and prev_bar_count / len(snaps) < 0.5:
            print(f"Only {prev_bar_count}/{len(snaps)} have prev bar — retry")
            return None

        scored = []
        for sym, snap in snaps.items():
            try:
                if not snap or not snap.daily_bar:
                    continue
                cur  = snap.daily_bar.close
                op   = snap.daily_bar.open
                vol  = snap.daily_bar.volume
                prev = snap.prev_daily_bar.close if snap.prev_daily_bar else cur

                # Wider filter in scanner — entry enforces strict $10-$25
                if cur < 5 or cur > 60:
                    continue
                if prev and abs((op - prev) / prev) > 0.05:
                    print(f"Skipping {sym} — earnings gap")
                    continue

                stock_change = (cur - prev) / prev if prev else 0
                rel_strength = stock_change - spy_change
                score        = vol * (abs(rel_strength) + 0.001)
                if regime in ["bullish", "choppy"] and cur > prev:
                    score *= 1.5
                if rel_strength > 0:
                    score *= 1.2
                scored.append((sym, score))
            except Exception as e:
                print(f"Error scoring {sym}: {e}")
                continue

        print(f"Scanner found {len(scored)} stocks")
        scored.sort(key=lambda x: x[1], reverse=True)
        top = [s[0] for s in scored[:10]]

        if not top:
            # Relaxed fallback within scanner
            scored_relaxed = []
            for sym, snap in snaps.items():
                try:
                    if not snap or not snap.daily_bar:
                        continue
                    cur = snap.daily_bar.close
                    vol = snap.daily_bar.volume
                    prev = snap.prev_daily_bar.close if snap.prev_daily_bar else cur
                    if cur < 5 or cur > 60:
                        continue
                    score = vol * abs((cur - prev) / prev) if prev else vol
                    scored_relaxed.append((sym, score))
                except Exception:
                    continue
            scored_relaxed.sort(key=lambda x: x[1], reverse=True)
            top = [s[0] for s in scored_relaxed[:10]]

        if not top:
            return None

        print(f"Watchlist ({regime}): {top}")
        notify(f"Market: {regime.upper()} | Scanning: {', '.join(top)}")
        return top

    except Exception as e:
        print(f"Scanner exception: {e}")
        return None

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
        return False, f"ORB too tight ({orb_range:.3%})"
    return True, ""

# =========================
# VOLUME + MOMENTUM CHECKS
# =========================

def has_volume_confirmation(df):
    if is_early_session():
        return True
    return df["volume"].iloc[-1] > df["volume"].mean() * VOLUME_MULTIPLIER

def has_momentum(df):
    """Last 3 closes must average higher than the 3 before."""
    if len(df) < 6:
        return True
    recent      = df["close"].iloc[-6:]
    first_half  = recent.iloc[:3].mean()
    second_half = recent.iloc[3:].mean()
    return second_half > first_half

# =========================
# SMART EXIT SYSTEM
# Detects the best time to sell rather than waiting for EOD
# =========================

def get_vwap(df):
    """Volume Weighted Average Price — institutional benchmark."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    return (typical * df["volume"]).sum() / df["volume"].sum()

def is_momentum_dying(df):
    """
    Returns True if upward momentum is weakening.
    Checks if consecutive candle gains are shrinking or reversing.
    """
    if len(df) < 4:
        return False
    closes = df["close"].iloc[-4:].values
    gains  = [closes[i+1] - closes[i] for i in range(3)]
    # Momentum dying: gains shrinking AND latest candle is down
    return gains[2] < gains[1] and gains[2] < 0

def is_volume_drying_up(df):
    """
    Returns True if recent volume is less than half the average.
    Smart money leaving = move about to reverse.
    """
    if len(df) < 10:
        return False
    avg_vol    = df["volume"].mean()
    recent_vol = df["volume"].iloc[-3:].mean()
    return recent_vol < avg_vol * 0.5

def is_uptrend_broken(df):
    """
    Returns True if stock stopped making higher highs.
    First sign of trend exhaustion.
    """
    if len(df) < 4:
        return False
    highs = df["high"].iloc[-4:].values
    # Uptrend broken if latest high is lower than two previous highs
    return highs[-1] < highs[-2] and highs[-1] < highs[-3]

def is_volume_spike(df):
    """
    Returns True if latest candle has 3× normal volume.
    Often signals a news-driven blow-off top — take profit immediately.
    """
    if len(df) < 10:
        return False
    avg_vol    = df["volume"].mean()
    latest_vol = df["volume"].iloc[-1]
    return latest_vol > avg_vol * 3.0

def should_smart_exit(df, pos, current_price, entry_time):
    """
    Master exit function — checks all signals to find best sell time.
    Only triggers smart exits when position is profitable.
    Returns (should_exit, reason)
    """
    qty  = pos["qty"]
    gain = ((current_price - pos["entry"]) * qty
            if pos["side"] == "LONG"
            else (pos["entry"] - current_price) * qty)

    # Never smart-exit a losing trade — let SL handle it
    if gain <= 0:
        return False, ""

    vwap = get_vwap(df)

    # 1. Volume spike — blow-off top, take profit NOW
    if is_volume_spike(df) and gain > 0:
        return True, f"Volume spike — blow-off top +${gain:.2f}"

    # 2. Price crossed below VWAP while profitable
    if current_price < vwap and gain > 0.30 * qty:
        return True, f"Below VWAP — exiting +${gain:.2f}"

    # 3. Momentum dying AND reasonable profit locked in
    if is_momentum_dying(df) and gain > pos["tp"] * 0.40:
        return True, f"Momentum dying — locking +${gain:.2f}"

    # 4. Volume drying up AND in profit
    if is_volume_drying_up(df) and gain > 0.20 * qty:
        return True, f"Volume drying up — exiting +${gain:.2f}"

    # 5. Uptrend broken AND meaningful profit
    if is_uptrend_broken(df) and gain > pos["tp"] * 0.30:
        return True, f"Uptrend broken — locking +${gain:.2f}"

    # 6. Held 60+ minutes with minimal gain — stale trade
    minutes_held = (datetime.now(ET) - entry_time).seconds / 60
    if minutes_held > 60 and gain < pos["tp"] * 0.25:
        return True, f"Stale trade ({minutes_held:.0f}min) — exiting +${gain:.2f}"

    return False, ""

def update_trailing_stop(pos, current_price):
    """
    Updates trailing stop level as price moves in our favor.
    Activates once position is up TRAIL_ACTIVATION dollars total.
    """
    qty  = pos["qty"]
    gain = ((current_price - pos["entry"]) * qty
            if pos["side"] == "LONG"
            else (pos["entry"] - current_price) * qty)

    # Only activate trailing stop after minimum gain
    if gain < TRAIL_ACTIVATION:
        return pos

    # Track peak price
    if pos["side"] == "LONG":
        peak = max(pos.get("peak_price", pos["entry"]), current_price)
        trail_price = peak - TRAIL_DISTANCE
        pos["peak_price"]  = peak
        pos["trail_price"] = max(
            pos.get("trail_price", 0), trail_price
        )
    return pos

def is_trailing_stop_hit(pos, current_price):
    """Returns True if price has dropped below the trailing stop."""
    if "trail_price" not in pos:
        return False
    if pos["side"] == "LONG":
        return current_price <= pos["trail_price"]
    return False

# =========================
# POSITION SIZING
# =========================

def get_position_qty(price, equity):
    """Use 35% of account per trade for more meaningful gains."""
    risk_per_trade = equity * POSITION_PCT
    qty            = max(1, int(risk_per_trade / price))
    return min(qty, MAX_QTY)

def get_tp_sl(entry_price, qty):
    per_share_tp = min(TAKE_PROFIT, entry_price * 0.06)
    per_share_sl = min(STOP_LOSS,   entry_price * 0.03)
    per_share_tp = max(per_share_tp, entry_price * MIN_TP_PCT)
    per_share_sl = max(per_share_sl, entry_price * MIN_SL_PCT)
    return per_share_tp * qty, per_share_sl * qty

# =========================
# PDT CHECK
# =========================

def get_day_trade_count():
    for attempt in range(3):
        try:
            account = trading_client.get_account()
            return int(account.daytrade_count)
        except Exception as e:
            print(f"PDT attempt {attempt+1} failed: {e}")
            time.sleep(5)
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
# STATE
# =========================

init_log()
_wtc, _wpnl, _tt = recover_state()

positions        = {}
pending_orders   = set()
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
entry_times      = {}   # tracks when each position was opened

notify(
    f"Bot started — PDT margin account\n"
    f"TP: ${TAKE_PROFIT} | SL: ${STOP_LOSS} | "
    f"Trail: activates at +${TRAIL_ACTIVATION}\n"
    f"Position size: {int(POSITION_PCT*100)}% of account\n"
    f"Week trades: {week_trade_count}/3 | P&L: ${weekly_pnl:.2f}"
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
            weekly_pnl       = 0.0
            week_trade_count = 0
            report_sent      = False
            print("New week — reset")
        last_week = cur_week

        # ── NEW DAY RESET ──
        if last_date != today:
            positions       = {}
            pending_orders  = set()
            entry_times     = {}
            watchlist       = []
            scanner_failed  = False
            regime          = "choppy"
            trades_today    = 0
            eod_sent        = False
            no_trade_reason = ""
            last_date       = today
            qty, equity     = get_dynamic_qty()
            allowed         = get_trades_allowed_today(week_trade_count, 0)
            notify(
                f"New day: {today}\n"
                f"QTY: {qty} | Equity: ${equity:.2f}\n"
                f"Target: {allowed} trade(s) | "
                f"Week: {week_trade_count}/3"
            )
            check_go_live_recommendation()

        # ── WEEKLY REPORT — Monday 9 AM ──
        if is_monday_morning() and not report_sent:
            all_trades = read_all_trades()
            send_weekly_report(all_trades, equity, qty)
            report_sent = True

        if not is_market_open():
            print(f"Market closed — "
                  f"{now_et.strftime('%Y-%m-%d %H:%M ET')} — sleeping 60s")
            time.sleep(60)
            continue

        # ── EOD SUMMARY ──
        if is_market_close_time() and not eod_sent:
            send_eod_summary(
                regime, watchlist, trades_today,
                weekly_pnl, week_trade_count, no_trade_reason
            )
            eod_sent = True

        # ── BUILD WATCHLIST ──
        if not watchlist and is_orb_window_complete() and not scanner_failed:
            regime = get_market_regime()
            result = scan_symbols(regime)
            if result is None:
                scanner_failed = True
                print("Scanner returned None — retry at 9:50 AM")
            else:
                watchlist      = result
                scanner_failed = False

        # ── SCANNER RETRY at 9:50 AM ──
        if scanner_failed and not watchlist and is_scanner_retry_time():
            print("Retrying scanner...")
            regime = get_market_regime()
            result = scan_symbols(regime)
            if result is not None:
                watchlist      = result
                scanner_failed = False
            else:
                watchlist      = FALLBACK_SYMBOLS
                scanner_failed = False
                notify(f"Scanner failed — fallback: {', '.join(watchlist)}")

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
        if (now_et.weekday() == 3 and now_et.hour == 9
                and now_et.minute == 45 and week_trade_count == 0):
            notify("⚠️ Thursday — 0 trades this week\n"
                   "Attempting all 3 today/tomorrow")

        pdt_used      = get_day_trade_count()
        allowed_today = get_trades_allowed_today(
            week_trade_count, trades_today
        )

        # ── FORCE EXIT NEAR CLOSE ──
        if is_near_market_close():
            for sym, pos in list(positions.items()):
                try:
                    exit_side  = (OrderSide.SELL if pos["side"] == "LONG"
                                  else OrderSide.BUY)
                    exit_price = get_data(sym)["close"].iloc[-1]
                    place_order(sym, exit_side, pos["qty"])
                    pnl, result = log_trade(
                        sym, pos["side"], pos["entry"],
                        exit_price, pos["qty"], regime, "EOD forced close"
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
                    entry_times.pop(sym, None)
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
                    pos        = positions[symbol]
                    entry_time = entry_times.get(symbol, now_et)

                    # Update trailing stop
                    pos = update_trailing_stop(pos, current_price)
                    positions[symbol] = pos

                    gain = ((current_price - pos["entry"]) * pos["qty"]
                            if pos["side"] == "LONG"
                            else (pos["entry"] - current_price) * pos["qty"])

                    exit_side = (OrderSide.SELL if pos["side"] == "LONG"
                                 else OrderSide.BUY)

                    # Check exits in priority order:

                    # 1. Take profit hit
                    if gain >= pos["tp"]:
                        place_order(symbol, exit_side, pos["qty"])
                        pnl, result = log_trade(
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

                    # 2. Trailing stop hit
                    elif is_trailing_stop_hit(pos, current_price):
                        place_order(symbol, exit_side, pos["qty"])
                        pnl, result = log_trade(
                            symbol, pos["side"], pos["entry"],
                            current_price, pos["qty"], regime,
                            f"Trailing stop @ ${pos['trail_price']:.2f}"
                        )
                        weekly_pnl       += pnl
                        week_trade_count += 1
                        trades_today     += 1
                        notify(
                            f"🔒 TRAIL STOP {symbol} @ ${current_price:.2f}\n"
                            f"${pnl:+.2f} | Locked profit | "
                            f"Week: {week_trade_count}/3"
                        )
                        del positions[symbol]
                        entry_times.pop(symbol, None)

                    # 3. Smart exit — best time to sell detected
                    else:
                        should_exit, exit_reason = should_smart_exit(
                            df, pos, current_price, entry_time
                        )
                        if should_exit:
                            place_order(symbol, exit_side, pos["qty"])
                            pnl, result = log_trade(
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

                        # 4. Hard stop loss
                        elif gain <= -pos["sl"]:
                            place_order(symbol, exit_side, pos["qty"])
                            pnl, result = log_trade(
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
                        log_skip(symbol,
                                 f"Price ${current_price:.2f} out of range")
                        continue

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

                    if (regime in ["bullish", "choppy"]
                            and current_price > high * (1 + ORB_BUFFER)):
                        pending_orders.add(symbol)
                        place_order(symbol, OrderSide.BUY, trade_qty)
                        positions[symbol] = {
                            "side":        "LONG",
                            "entry":       current_price,
                            "tp":          tp_amt,
                            "sl":          sl_amt,
                            "qty":         trade_qty,
                            "peak_price":  current_price,
                        }
                        entry_times[symbol] = now_et
                        pdt_used        += 1
                        allowed_today   -= 1
                        no_trade_reason  = ""
                        notify(
                            f"📈 BUY {symbol} x{trade_qty} "
                            f"@ ${current_price:.2f}\n"
                            f"TP: +${tp_amt:.2f} | SL: -${sl_amt:.2f}\n"
                            f"Trail activates at +${TRAIL_ACTIVATION:.2f}\n"
                            f"Week: {week_trade_count+1}/3"
                        )
                    else:
                        reason = ("Bearish — longs disabled"
                                  if regime == "bearish"
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
