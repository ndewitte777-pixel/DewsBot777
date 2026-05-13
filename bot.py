import time
import requests
import os
import pytz
from datetime import datetime

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# =========================
# CONFIG
# =========================

ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
PUSHOVER_USER_KEY  = os.environ.get("PUSHOVER_USER_KEY")
PUSHOVER_API_TOKEN = os.environ.get("PUSHOVER_API_TOKEN")

SYMBOL      = "SPY"
QTY         = 1
TAKE_PROFIT = 0.005  # +0.5% — realistic for SPY intraday
STOP_LOSS   = 0.003  # -0.3% — tight stop to limit downside

# No new trades after this time (ET)
NO_ENTRY_AFTER_HOUR   = 15
NO_ENTRY_AFTER_MINUTE = 30

# PDT rule: max 3 day trades per 5 days if account < $25k
MAX_DAY_TRADES = 3

ET = pytz.timezone("America/New_York")

# =========================
# INIT CLIENTS
# =========================

trading_client = TradingClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    paper=True   # <-- Change to False when going live
)

data_client = StockHistoricalDataClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY
)

# =========================
# NOTIFICATIONS
# =========================

def notify(message):
    print(message)  # always log to Railway console
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

def is_near_market_close():
    """True within 5 minutes of market close — used to force-exit positions."""
    now = datetime.now(ET)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    diff = (market_close - now).total_seconds()
    return 0 < diff <= 300

def is_orb_window_complete():
    """Only trade after the first 15 min of market open (9:45 AM ET)."""
    now = datetime.now(ET)
    orb_done = now.replace(hour=9, minute=45, second=0, microsecond=0)
    return now >= orb_done

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
# GET DATA
# =========================

def get_data():
    request = StockBarsRequest(
        symbol_or_symbols=[SYMBOL],
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
    """Use only 9:30–9:45 AM ET bars for the true opening range."""
    try:
        df["timestamp_et"] = df["timestamp"].dt.tz_convert(ET)
        open_time  = df["timestamp_et"].iloc[0].replace(hour=9,  minute=30, second=0, microsecond=0)
        close_time = df["timestamp_et"].iloc[0].replace(hour=9,  minute=45, second=0, microsecond=0)
        orb_bars = df[(df["timestamp_et"] >= open_time) & (df["timestamp_et"] < close_time)]
        if len(orb_bars) < 5:
            orb_bars = df.iloc[:15]
    except Exception:
        orb_bars = df.iloc[:15]

    high = orb_bars["high"].max()
    low  = orb_bars["low"].min()
    return high, low

# =========================
# ORDER EXECUTION
# =========================

def place_order(side):
    order = MarketOrderRequest(
        symbol=SYMBOL,
        qty=QTY,
        side=side,
        time_in_force=TimeInForce.DAY
    )
    trading_client.submit_order(order)

# =========================
# STATE
# =========================

position     = None
entry_price  = None
trades_today = 0
last_date    = None

notify("Bot started and running on Railway")

# =========================
# MAIN LOOP
# =========================

while True:
    try:
        now_et = datetime.now(ET)
        today  = now_et.date()

        # Reset daily trade counter at start of each new day
        if last_date != today:
            trades_today = 0
            last_date    = today
            print(f"New trading day: {today}")

        if not is_market_open():
            print(f"Market closed — {now_et.strftime('%Y-%m-%d %H:%M ET')} — sleeping 60s")
            time.sleep(60)
            continue

        # Force-exit any open position near close (3:55 PM ET)
        if is_near_market_close() and position is not None:
            if position == "LONG":
                place_order(OrderSide.SELL)
            elif position == "SHORT":
                place_order(OrderSide.BUY)
            notify(f"EOD CLOSE {position} {SYMBOL} — forced exit before market close")
            position = None
            time.sleep(60)
            continue

        df = get_data()
        current_price = df["close"].iloc[-1]
        high, low = get_orb_levels(df)

        # =========================
        # ENTRY LOGIC
        # =========================

        if position is None:

            if is_after_no_entry_time():
                print(f"No new entries after 3:30 PM — {now_et.strftime('%H:%M ET')}")
                time.sleep(60)
                continue

            if not is_orb_window_complete():
                print(f"Waiting for ORB window to complete (9:45 AM) — {now_et.strftime('%H:%M ET')}")
                time.sleep(60)
                continue

            pdt_count = get_day_trade_count()
            if pdt_count >= MAX_DAY_TRADES:
                print(f"PDT limit reached ({pdt_count}/3) — no new trades today")
                time.sleep(60)
                continue

            if current_price > high:
                place_order(OrderSide.BUY)
                position    = "LONG"
                entry_price = current_price
                trades_today += 1
                notify(f"BUY {SYMBOL} @ ${current_price:.2f} | ORB high: ${high:.2f} | Trade #{trades_today} today")

            elif current_price < low:
                place_order(OrderSide.SELL)
                position    = "SHORT"
                entry_price = current_price
                trades_today += 1
                notify(f"SHORT {SYMBOL} @ ${current_price:.2f} | ORB low: ${low:.2f} | Trade #{trades_today} today")

        # =========================
        # EXIT LOGIC
        # =========================

        elif position == "LONG":
            change = (current_price - entry_price) / entry_price

            if change >= TAKE_PROFIT:
                place_order(OrderSide.SELL)
                notify(f"EXIT LONG {SYMBOL} @ ${current_price:.2f} | +{change:.2%} profit")
                position = None

            elif change <= -STOP_LOSS:
                place_order(OrderSide.SELL)
                notify(f"STOP LONG {SYMBOL} @ ${current_price:.2f} | {change:.2%} loss")
                position = None

        elif position == "SHORT":
            change = (entry_price - current_price) / entry_price

            if change >= TAKE_PROFIT:
                place_order(OrderSide.BUY)
                notify(f"EXIT SHORT {SYMBOL} @ ${current_price:.2f} | +{change:.2%} profit")
                position = None

            elif change <= -STOP_LOSS:
                place_order(OrderSide.BUY)
                notify(f"STOP SHORT {SYMBOL} @ ${current_price:.2f} | {change:.2%} loss")
                position = None

        time.sleep(60)

    except Exception as e:
        notify(f"ERROR: {e}")
        print(f"Error: {e}")
        time.sleep(60)
