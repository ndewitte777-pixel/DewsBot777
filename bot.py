import time
import requests
import os
import pytz
from datetime import datetime

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.requests import MarketOrderRequest, GetAssetsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass
from alpaca.broker.client import BrokerClient

# =========================
# CONFIG
# =========================

ALPACA_API_KEY     = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY  = os.environ.get("ALPACA_SECRET_KEY")
PUSHOVER_USER_KEY  = os.environ.get("PUSHOVER_USER_KEY")
PUSHOVER_API_TOKEN = os.environ.get("PUSHOVER_API_TOKEN")

QTY            = 1       # shares per trade (raise this when going live)
TAKE_PROFIT    = 0.005   # +0.5%
STOP_LOSS      = 0.003   # -0.3%
MAX_DAY_TRADES = 3       # PDT limit — keep at 3 if account < $25k
MAX_POSITIONS  = 3       # max simultaneous open positions
TOP_N_SYMBOLS  = 10      # how many stocks to scan each morning

# No new entries after this time ET
NO_ENTRY_AFTER_HOUR   = 15
NO_ENTRY_AFTER_MINUTE = 30

ET = pytz.timezone("America/New_York")

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
        print("WARNING: Pushover env vars missing — skipping notification")
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
    now = datetime.now(ET)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    diff = (market_close - now).total_seconds()
    return 0 < diff <= 300

# =========================
# PDT CHECK
# =========================

def get_day_trade_count():
    try:
        account = trading_client.get_account()
        return int(account.daytrade_count)
    except Exception as e:
        print(f"Could not fetch day trade count: {e}")
        return 0

# =========================
# STOCK SCANNER
# Picks top stocks by volume each morning
# =========================

# Fallback watchlist if scanner fails
FALLBACK_SYMBOLS = ["SPY", "QQQ", "AAPL", "TSLA", "NVDA", "MSFT", "AMD", "META", "AMZN", "GOOGL"]

def scan_top_symbols():
    """
    Fetches snapshots for a broad list of liquid stocks and ranks
    them by today's volume to find the most active movers.
    """
    try:
        candidates = [
            "SPY", "QQQ", "AAPL", "TSLA", "NVDA", "MSFT", "AMD",
            "META", "AMZN", "GOOGL", "NFLX", "BABA", "SOFI", "PLTR",
            "RIVN", "NIO", "F", "GM", "BAC", "JPM", "XOM", "CVX",
            "INTC", "MU", "SNAP", "UBER", "LYFT", "COIN", "SQ", "SHOP"
        ]

        req = StockSnapshotRequest(symbol_or_symbols=candidates)
        snapshots = data_client.get_stock_snapshot(req)

        # Score each stock: volume * abs(daily % change) = high activity + movement
        scored = []
        for sym, snap in snapshots.items():
            try:
                volume     = snap.daily_bar.volume if snap.daily_bar else 0
                prev_close = snap.prev_daily_bar.close if snap.prev_daily_bar else None
                cur_close  = snap.daily_bar.close if snap.daily_bar else None
                if prev_close and cur_close and prev_close > 0:
                    pct_change = abs((cur_close - prev_close) / prev_close)
                    score = volume * pct_change
                    scored.append((sym, score))
            except Exception:
                continue

        scored.sort(key=lambda x: x[1], reverse=True)
        top = [s[0] for s in scored[:TOP_N_SYMBOLS]]
        print(f"Scanned symbols for today: {top}")
        notify(f"Today's watchlist: {', '.join(top)}")
        return top

    except Exception as e:
        print(f"Scanner failed, using fallback: {e}")
        return FALLBACK_SYMBOLS[:TOP_N_SYMBOLS]

# =========================
# GET DATA
# =========================

def get_data(symbol):
    request = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame.Minute,
        limit=100
    )
    bars = data_client.get_stock_bars(request)
    df = bars.df.reset_index()
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
# ORDER EXECUTION
# =========================

def place_order(symbol, side):
    order = MarketOrderRequest(
        symbol=symbol,
        qty=QTY,
        side=side,
        time_in_force=TimeInForce.DAY
    )
    trading_client.submit_order(order)

# =========================
# STATE
# positions = {
#   "AAPL": {"side": "LONG", "entry": 175.00, "trades_today": 1},
#   ...
# }
# =========================

positions    = {}   # currently open positions per symbol
trades_today = {}   # trade count per symbol today
watchlist    = []   # today's scanned symbols
last_date    = None

notify("Bot started and running on Railway")

# =========================
# MAIN LOOP
# =========================

while True:
    try:
        now_et = datetime.now(ET)
        today  = now_et.date()

        # Reset daily state at start of new trading day
        if last_date != today:
            trades_today = {}
            positions    = {}
            watchlist    = []
            last_date    = today
            print(f"New trading day: {today}")

        if not is_market_open():
            print(f"Market closed — {now_et.strftime('%Y-%m-%d %H:%M ET')} — sleeping 60s")
            time.sleep(60)
            continue

        # Scan for today's symbols once after ORB window is done
        if not watchlist and is_orb_window_complete():
            watchlist = scan_top_symbols()

        if not watchlist:
            print(f"Waiting for ORB window (9:45 AM ET) — {now_et.strftime('%H:%M ET')}")
            time.sleep(60)
            continue

        # Check total PDT usage
        pdt_used = get_day_trade_count()

        # Force-exit all positions near market close
        if is_near_market_close():
            for sym, pos in list(positions.items()):
                try:
                    side = OrderSide.SELL if pos["side"] == "LONG" else OrderSide.BUY
                    place_order(sym, side)
                    notify(f"EOD CLOSE {pos['side']} {sym} — forced exit before close")
                    del positions[sym]
                except Exception as e:
                    print(f"EOD close failed for {sym}: {e}")
            time.sleep(60)
            continue

        # Loop through each symbol on watchlist
        for symbol in watchlist:
            try:
                df = get_data(symbol)
                if df.empty:
                    continue

                current_price = df["close"].iloc[-1]
                high, low     = get_orb_levels(df)

                # ---- MANAGE OPEN POSITION ----
                if symbol in positions:
                    pos    = positions[symbol]
                    entry  = pos["entry"]
                    change = ((current_price - entry) / entry
                              if pos["side"] == "LONG"
                              else (entry - current_price) / entry)

                    if change >= TAKE_PROFIT:
                        exit_side = OrderSide.SELL if pos["side"] == "LONG" else OrderSide.BUY
                        place_order(symbol, exit_side)
                        notify(f"EXIT {pos['side']} {symbol} @ ${current_price:.2f} | +{change:.2%} profit")
                        del positions[symbol]

                    elif change <= -STOP_LOSS:
                        exit_side = OrderSide.SELL if pos["side"] == "LONG" else OrderSide.BUY
                        place_order(symbol, exit_side)
                        notify(f"STOP {pos['side']} {symbol} @ ${current_price:.2f} | {change:.2%} loss")
                        del positions[symbol]
                        # Moderate: allow re-entry later today (position removed, not blocked)

                # ---- LOOK FOR NEW ENTRY ----
                elif (not is_after_no_entry_time()
                      and is_orb_window_complete()
                      and pdt_used < MAX_DAY_TRADES
                      and len(positions) < MAX_POSITIONS):

                    sym_trades = trades_today.get(symbol, 0)

                    if current_price > high:
                        place_order(symbol, OrderSide.BUY)
                        positions[symbol] = {"side": "LONG", "entry": current_price}
                        trades_today[symbol] = sym_trades + 1
                        pdt_used += 1
                        notify(f"BUY {symbol} @ ${current_price:.2f} | ORB high: ${high:.2f} | PDT used: {pdt_used}/3")

                    elif current_price < low:
                        place_order(symbol, OrderSide.SELL)
                        positions[symbol] = {"side": "SHORT", "entry": current_price}
                        trades_today[symbol] = sym_trades + 1
                        pdt_used += 1
                        notify(f"SHORT {symbol} @ ${current_price:.2f} | ORB low: ${low:.2f} | PDT used: {pdt_used}/3")

            except Exception as e:
                print(f"Error processing {symbol}: {e}")
                continue

        time.sleep(60)

    except Exception as e:
        notify(f"ERROR: {e}")
        print(f"Error: {e}")
        time.sleep(60)
