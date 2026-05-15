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
from alpaca.trading.requests import MarketOrderRequest, GetPortfolioHistoryRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# =========================
# CONFIG
# =========================

ALPACA_API_KEY     = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY  = os.environ.get("ALPACA_SECRET_KEY")
PUSHOVER_USER_KEY  = os.environ.get("PUSHOVER_USER_KEY")
PUSHOVER_API_TOKEN = os.environ.get("PUSHOVER_API_TOKEN")

TAKE_PROFIT      = 2.00   # $2.00 target — 2× the stop loss
STOP_LOSS        = 1.00   # $1.00 stop — 2:1 reward/risk ratio
MAX_WEEKLY_LOSS  = 6.00   # circuit breaker
MAX_POSITIONS    = 1      # one position at a time
MIN_PRICE        = 10
MAX_PRICE        = 100
BASE_QTY         = 2
QTY_PER_50       = 1      # add 1 share per $50 growth (was per $100)
MIN_TP_PCT       = 0.004
MIN_SL_PCT       = 0.002

# Loosened entry filters
REGIME_THRESHOLD  = 0.001
VOLUME_MULTIPLIER = 1.2
ORB_BUFFER        = 0.001

# Cash account mode — no PDT limit but trades need settled funds
# Set CASH_ACCOUNT=true in Railway variables when you switch account type
CASH_ACCOUNT = os.environ.get("CASH_ACCOUNT", "false").lower() == "true"

# PDT limit — ignored in cash account mode
MAX_DAY_TRADES = 999 if CASH_ACCOUNT else 3

# Settlement tracking — cash accounts take 2 days to settle
SETTLEMENT_DAYS = 2

NO_ENTRY_AFTER_HOUR   = 15
NO_ENTRY_AFTER_MINUTE = 30

PAPER_START_DATE = os.environ.get("PAPER_START_DATE", "2026-05-13")
LOG_FILE         = "trade_log.csv"
SETTLEMENT_FILE  = "settlement_log.csv"

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

SECTOR_STOCKS = {
    "Technology":   ["AAPL", "AMD", "INTC", "MU", "PLTR", "SNOW", "SOFI"],
    "Energy":       ["XOM", "OXY", "SLB", "HAL"],
    "Financials":   ["BAC", "SOFI", "COIN", "MS"],
    "Healthcare":   ["PFE", "MRNA", "ABT", "CVS"],
    "ConsumerDisc": ["TSLA", "F", "GM", "RIVN", "NIO"],
    "Industrials":  ["GE", "HON", "UPS"],
    "Materials":    ["FCX", "AA", "CLF"],
    "Utilities":    ["NEE", "SO"],
    "RealEstate":   ["SPG"],
    "ConsumerStap": ["WMT", "KO"],
}

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
    if not os.path.exists(SETTLEMENT_FILE):
        with open(SETTLEMENT_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["date", "symbol", "proceeds", "settles_on"])

def log_trade(symbol, side, entry_price, exit_price, qty, regime):
    pnl    = (exit_price - entry_price) * qty if side == "LONG" else (entry_price - exit_price) * qty
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
        dt  = datetime.strptime(trade["date"], "%Y-%m-%d %H:%M")
        iso = dt.isocalendar()
        return (dt.year, iso[1])
    except Exception:
        return (0, 0)

# =========================
# CASH ACCOUNT SETTLEMENT
# Tracks funds that are pending settlement (T+2)
# Prevents trading with unsettled funds
# =========================

def log_settlement(symbol, proceeds, trade_date):
    """Log sale proceeds with their settlement date (T+2 business days)."""
    settle_date = get_settlement_date(trade_date)
    with open(SETTLEMENT_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            trade_date.strftime("%Y-%m-%d"),
            symbol,
            round(proceeds, 2),
            settle_date.strftime("%Y-%m-%d")
        ])

def get_settlement_date(trade_date):
    """Returns the date funds settle — T+2 business days, skipping weekends."""
    settle = trade_date
    days_added = 0
    while days_added < SETTLEMENT_DAYS:
        settle = settle + timedelta(days=1)
        if settle.weekday() < 5:  # skip weekends
            days_added += 1
    return settle

def get_settled_cash():
    """
    Returns how much cash is available to trade today.
    In cash account mode, only settled funds can be used.
    In margin/PDT mode, full buying power is available.
    """
    if not CASH_ACCOUNT:
        try:
            account = trading_client.get_account()
            return float(account.buying_power)
        except Exception:
            return 250.0

    # Cash account — check settled funds
    try:
        account        = trading_client.get_account()
        total_cash     = float(account.cash)
        today          = datetime.now(ET).date()
        unsettled      = 0.0

        if os.path.exists(SETTLEMENT_FILE):
            with open(SETTLEMENT_FILE, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    settle_date = datetime.strptime(row["settles_on"], "%Y-%m-%d").date()
                    if settle_date > today:
                        unsettled += float(row["proceeds"])

        settled = max(0, total_cash - unsettled)
        print(f"Cash account — Total: ${total_cash:.2f} | Unsettled: ${unsettled:.2f} | Available: ${settled:.2f}")
        return settled

    except Exception as e:
        print(f"Settlement check failed: {e}")
        return 0.0

def can_afford_trade(price, qty):
    """Check if we have enough settled cash to place this trade."""
    cost           = price * qty
    available_cash = get_settled_cash()
    if available_cash < cost:
        print(f"Insufficient settled funds — need ${cost:.2f}, have ${available_cash:.2f}")
        return False
    return True

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
# Scales 1 share per $50 growth (faster than before)
# =========================

def get_dynamic_qty():
    try:
        account  = trading_client.get_account()
        equity   = float(account.equity)
        growth   = max(0, equity - 250)
        qty      = BASE_QTY + int(growth / 50) * QTY_PER_50   # every $50 now
        max_safe = int((equity * 0.40) / MAX_PRICE)
        qty      = max(1, min(qty, max_safe))
        mode     = "CASH" if CASH_ACCOUNT else "MARGIN/PDT"
        print(f"Equity: ${equity:.2f} | QTY: {qty} | Mode: {mode}")
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

    # Project account value in 24 months at current win rate
    if all_stats["win_rate"] >= 50:
        projected = equity * (1.05 ** 24)  # rough monthly compounding estimate
        projection_str = f"24mo projection: ~${projected:,.0f}"
    else:
        projection_str = "Win rate below 50% — review strategy"

    if all_stats["win_rate"] >= 60:
        trend = "🔥 Strong"
    elif all_stats["win_rate"] >= 50:
        trend = "✅ On track"
    else:
        trend = "⚠️ Below target"

    max_weekly = week_stats['total'] if not CASH_ACCOUNT else 15
    notify(
        f"📊 WEEKLY REPORT — Week {weeks_running}\n"
        f"Account: ${equity:.2f} | QTY: {qty}\n"
        f"Mode: {'CASH (no PDT)' if CASH_ACCOUNT else 'MARGIN (3/week)'}\n"
        f"─────────────────\n"
        f"THIS WEEK\n"
        f"Trades: {week_stats['total']} | W: {week_stats['wins']} L: {week_stats['losses']}\n"
        f"P&L: ${week_stats['total_pnl']}\n"
        f"─────────────────\n"
        f"ALL TIME\n"
        f"Trades: {all_stats['total']} | Win rate: {all_stats['win_rate']}%\n"
        f"Total P&L: ${all_stats['total_pnl']}\n"
        f"Avg win: ${all_stats['avg_win']} | Avg loss: ${all_stats['avg_loss']}\n"
        f"Profit factor: {all_stats['profit_factor']}\n"
        f"Status: {trend}\n"
        f"{projection_str}"
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
           (now.hour == NO_ENTRY_AFTER_HOUR and now.minute >= NO_ENTRY_AFTER_MINUTE))

def is_orb_window_complete():
    now = datetime.now(ET)
    return now >= now.replace(hour=9, minute=45, second=0, microsecond=0)

def is_near_market_close():
    now          = datetime.now(ET)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    diff         = (market_close - now).total_seconds()
    return 0 < diff <= 300

def is_monday_morning():
    now = datetime.now(ET)
    return now.weekday() == 0 and now.hour == 9 and now.minute < 31

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
        mid           = len(df) // 2
        higher_highs  = df["high"].iloc[mid:].max() > df["high"].iloc[:mid].max()
        higher_lows   = df["low"].iloc[mid:].min()  > df["low"].iloc[:mid].min()
        lower_highs   = df["high"].iloc[mid:].max() < df["high"].iloc[:mid].max()
        lower_lows    = df["low"].iloc[mid:].min()  < df["low"].iloc[:mid].min()
        if daily_change > REGIME_THRESHOLD and (higher_highs or higher_lows):
            return "bullish"
        elif daily_change < -REGIME_THRESHOLD and (lower_highs or lower_lows):
            return "bearish"
        else:
            return "choppy"
    except Exception as e:
        print(f"Regime check failed: {e}")
        return "choppy"

# =========================
# SECTOR SCANNER
# =========================

def get_top_sectors():
    try:
        etf_list = list(SECTOR_ETFS.values())
        req      = StockSnapshotRequest(symbol_or_symbols=etf_list)
        snaps    = data_client.get_stock_snapshot(req)
        scored   = []
        for sector, etf in SECTOR_ETFS.items():
            snap = snaps.get(etf)
            if not snap:
                continue
            try:
                prev = snap.prev_daily_bar.close
                cur  = snap.daily_bar.close
                pct  = (cur - prev) / prev
                scored.append((sector, pct))
            except Exception:
                continue
        scored.sort(key=lambda x: x[1], reverse=True)
        return [s[0] for s in scored[:2]]
    except Exception as e:
        print(f"Sector scan failed: {e}")
        return ["Technology", "ConsumerDisc"]

# =========================
# STOCK SCANNER
# =========================

def scan_symbols(regime):
    try:
        spy_req    = StockSnapshotRequest(symbol_or_symbols=["SPY"])
        spy_snap   = data_client.get_stock_snapshot(spy_req).get("SPY")
        spy_change = 0
        if spy_snap:
            spy_change = (spy_snap.daily_bar.close - spy_snap.prev_daily_bar.close) / spy_snap.prev_daily_bar.close

        top_sectors = get_top_sectors()
        candidates  = []
        for sector in top_sectors:
            candidates.extend(SECTOR_STOCKS.get(sector, []))
        candidates = list(set(candidates))

        req   = StockSnapshotRequest(symbol_or_symbols=candidates)
        snaps = data_client.get_stock_snapshot(req)

        scored = []
        for sym, snap in snaps.items():
            try:
                prev = snap.prev_daily_bar.close
                cur  = snap.daily_bar.close
                vol  = snap.daily_bar.volume
                op   = snap.daily_bar.open
                if cur < MIN_PRICE or cur > MAX_PRICE:
                    continue
                if abs((op - prev) / prev) > 0.05:
                    print(f"Skipping {sym} — earnings gap")
                    continue
                rel_strength = ((cur - prev) / prev) - spy_change
                score        = vol * abs(rel_strength)
                if regime in ["bullish", "choppy"] and cur > prev:
                    score *= 1.5
                elif regime == "bearish" and cur < prev:
                    score *= 1.5
                scored.append((sym, score))
            except Exception:
                continue

        scored.sort(key=lambda x: x[1], reverse=True)
        top = [s[0] for s in scored[:10]]
        print(f"Watchlist ({regime}): {top}")
        notify(f"Market: {regime.upper()} | Scanning: {', '.join(top)}")
        return top
    except Exception as e:
        print(f"Scanner failed: {e}")
        return ["AMD", "SOFI", "F", "PLTR", "BAC"]

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
    df   = bars.df.reset_index()
    return df

# =========================
# ORB LEVELS
# =========================

def get_orb_levels(df):
    try:
        df["timestamp_et"] = df["timestamp"].dt.tz_convert(ET)
        now_et     = datetime.now(ET)
        open_time  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
        close_time = now_et.replace(hour=9,  minute=45, second=0, microsecond=0)
        orb_bars   = df[(df["timestamp_et"] >= open_time) & (df["timestamp_et"] < close_time)]
        if len(orb_bars) < 5:
            orb_bars = df.iloc[:15]
    except Exception:
        orb_bars = df.iloc[:15]
    return orb_bars["high"].max(), orb_bars["low"].min()

# =========================
# VOLUME CONFIRMATION
# =========================

def has_volume_confirmation(df):
    return df["volume"].iloc[-1] > df["volume"].mean() * VOLUME_MULTIPLIER

# =========================
# DYNAMIC TP/SL
# =========================

def get_tp_sl(entry_price, qty):
    tp = max(TAKE_PROFIT, entry_price * MIN_TP_PCT) * qty
    sl = max(STOP_LOSS,   entry_price * MIN_SL_PCT) * qty
    return tp, sl

# =========================
# PDT CHECK
# =========================

def get_day_trade_count():
    if CASH_ACCOUNT:
        return 0  # no PDT limit on cash accounts
    try:
        account = trading_client.get_account()
        return int(account.daytrade_count)
    except Exception as e:
        print(f"Could not fetch PDT count: {e}")
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
# Cash account = 3 trades/day (15/week)
# PDT account  = 3 trades/week with Friday catch-up
# =========================

def get_trades_allowed_today(week_trade_count, trades_today):
    if CASH_ACCOUNT:
        # Cash account — up to 3 trades per day, no weekly cap
        return max(0, 3 - trades_today)

    # PDT account — 3 per week with Friday catch-up
    now         = datetime.now(ET)
    weekday     = now.weekday()
    trades_left = MAX_DAY_TRADES - week_trade_count
    if trades_left <= 0:
        return 0
    if weekday >= 3:  # Thu/Fri — use all remaining
        return trades_left
    return min(trades_left, max(1, trades_left - (4 - weekday)))

# =========================
# STATE
# =========================

init_log()

positions        = {}
watchlist        = []
regime           = "choppy"
last_date        = None
last_week        = None
weekly_pnl       = 0.0
week_trade_count = 0
trades_today     = 0
report_sent      = False
qty              = BASE_QTY
equity           = 250.0

mode_str = "CASH ACCOUNT (no PDT)" if CASH_ACCOUNT else "MARGIN ACCOUNT (PDT: 3/week)"
notify(f"Bot started — {mode_str}\nTP: ${TAKE_PROFIT} | SL: ${STOP_LOSS} | Ratio: 2:1")

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
            print(f"New week — reset")

        # ── NEW DAY RESET ──
        if last_date != today:
            positions    = {}
            watchlist    = []
            regime       = "choppy"
            trades_today = 0
            last_date    = today
            qty, equity  = get_dynamic_qty()
            allowed      = get_trades_allowed_today(week_trade_count, 0)
            print(f"New day: {today} | QTY: {qty} | Equity: ${equity:.2f}")
            notify(
                f"New day: {today}\n"
                f"QTY: {qty} | Equity: ${equity:.2f}\n"
                f"Trades today: {allowed} | Week: {week_trade_count}/{'∞' if CASH_ACCOUNT else '3'}"
            )
            check_go_live_recommendation()

        # ── WEEKLY REPORT — Monday 9 AM ──
        if is_monday_morning() and not report_sent:
            all_trades = read_all_trades()
            send_weekly_report(all_trades, equity, qty)
            report_sent = True

        if not is_market_open():
            print(f"Market closed — {now_et.strftime('%Y-%m-%d %H:%M ET')} — sleeping 60s")
            time.sleep(60)
            continue

        # ── BUILD WATCHLIST ──
        if not watchlist and is_orb_window_complete():
            regime    = get_market_regime()
            watchlist = scan_symbols(regime)

        if not watchlist:
            print(f"Waiting for ORB — {now_et.strftime('%H:%M ET')}")
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
            print(f"Weekly loss limit hit — sitting out")
            time.sleep(60)
            continue

        pdt_used      = get_day_trade_count()
        allowed_today = get_trades_allowed_today(week_trade_count, trades_today)

        # ── FORCE EXIT NEAR CLOSE ──
        if is_near_market_close():
            for sym, pos in list(positions.items()):
                try:
                    exit_side  = OrderSide.SELL if pos["side"] == "LONG" else OrderSide.BUY
                    df_exit    = get_data(sym)
                    exit_price = df_exit["close"].iloc[-1]
                    place_order(sym, exit_side, pos["qty"])
                    pnl, result = log_trade(sym, pos["side"], pos["entry"],
                                            exit_price, pos["qty"], regime)
                    if CASH_ACCOUNT:
                        proceeds = exit_price * pos["qty"]
                        log_settlement(sym, proceeds, now_et)
                    weekly_pnl       += pnl
                    week_trade_count += 1
                    trades_today     += 1
                    notify(
                        f"EOD CLOSE {pos['side']} {sym} | ${pnl:+.2f}\n"
                        f"Week P&L: ${weekly_pnl:.2f} | Trades: {week_trade_count}"
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
                    pos   = positions[symbol]
                    entry = pos["entry"]
                    tp    = pos["tp"]
                    sl    = pos["sl"]
                    gain  = ((current_price - entry) * pos["qty"] if pos["side"] == "LONG"
                             else (entry - current_price) * pos["qty"])

                    if gain >= tp:
                        exit_side = OrderSide.SELL if pos["side"] == "LONG" else OrderSide.BUY
                        place_order(symbol, exit_side, pos["qty"])
                        pnl, result = log_trade(symbol, pos["side"], entry,
                                                current_price, pos["qty"], regime)
                        if CASH_ACCOUNT:
                            log_settlement(symbol, current_price * pos["qty"], now_et)
                        weekly_pnl       += pnl
                        week_trade_count += 1
                        trades_today     += 1
                        notify(
                            f"✅ WIN {pos['side']} {symbol} @ ${current_price:.2f}\n"
                            f"+${pnl:.2f} | Week P&L: ${weekly_pnl:.2f}\n"
                            f"Trades today: {trades_today} | Week: {week_trade_count}"
                        )
                        del positions[symbol]

                    elif gain <= -sl:
                        exit_side = OrderSide.SELL if pos["side"] == "LONG" else OrderSide.BUY
                        place_order(symbol, exit_side, pos["qty"])
                        pnl, result = log_trade(symbol, pos["side"], entry,
                                                current_price, pos["qty"], regime)
                        if CASH_ACCOUNT:
                            log_settlement(symbol, current_price * pos["qty"], now_et)
                        weekly_pnl       += pnl
                        week_trade_count += 1
                        trades_today     += 1
                        notify(
                            f"🛑 LOSS {pos['side']} {symbol} @ ${current_price:.2f}\n"
                            f"${pnl:.2f} | Week P&L: ${weekly_pnl:.2f}\n"
                            f"Trades today: {trades_today} | Week: {week_trade_count}"
                        )
                        del positions[symbol]

                # ── LOOK FOR NEW ENTRY ──
                elif (not is_after_no_entry_time()
                      and is_orb_window_complete()
                      and pdt_used < MAX_DAY_TRADES
                      and allowed_today > 0
                      and len(positions) < MAX_POSITIONS
                      and has_volume_confirmation(df)
                      and can_afford_trade(current_price, qty)):

                    tp_amt, sl_amt = get_tp_sl(current_price, qty)

                    # Bullish or choppy = longs
                    if regime in ["bullish", "choppy"] and current_price > high * (1 + ORB_BUFFER):
                        place_order(symbol, OrderSide.BUY, qty)
                        positions[symbol] = {
                            "side": "LONG", "entry": current_price,
                            "tp": tp_amt, "sl": sl_amt, "qty": qty
                        }
                        pdt_used     += 1
                        allowed_today -= 1
                        notify(
                            f"📈 BUY {symbol} x{qty} @ ${current_price:.2f}\n"
                            f"TP: +${tp_amt:.2f} | SL: -${sl_amt:.2f}\n"
                            f"2:1 ratio | PDT: {pdt_used} | Today: {trades_today+1}"
                        )

                    # Bearish = shorts
                    elif regime == "bearish" and current_price < low * (1 - ORB_BUFFER):
                        place_order(symbol, OrderSide.SELL, qty)
                        positions[symbol] = {
                            "side": "SHORT", "entry": current_price,
                            "tp": tp_amt, "sl": sl_amt, "qty": qty
                        }
                        pdt_used     += 1
                        allowed_today -= 1
                        notify(
                            f"📉 SHORT {symbol} x{qty} @ ${current_price:.2f}\n"
                            f"TP: +${tp_amt:.2f} | SL: -${sl_amt:.2f}\n"
                            f"2:1 ratio | PDT: {pdt_used} | Today: {trades_today+1}"
                        )

            except Exception as e:
                print(f"Error processing {symbol}: {e}")
                continue

        time.sleep(60)

    except Exception as e:
        notify(f"ERROR: {e}")
        print(f"Error: {e}")
        time.sleep(60)
