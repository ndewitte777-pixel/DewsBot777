import time
import requests
from datetime import datetime
import pytz
import os

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# =========================
# CONFIG (from environment)
# =========================

ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
PUSHOVER_USER_KEY = os.environ.get("PUSHOVER_USER_KEY")
PUSHOVER_API_TOKEN = os.environ.get("PUSHOVER_API_TOKEN")

SYMBOL      = "SPY"
QTY         = 1
TAKE_PROFIT = 0.20   # +20%
STOP_LOSS   = 0.10   # -10%

ET = pytz.timezone("America/New_York")

# =========================
# INIT CLIENTS
# =========================

trading_client = TradingClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    paper=True
)

data_client = StockHistoricalDataClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY
)

# =========================
# NOTIFICATIONS
# =========================

def notify(message):
    print(message)  # always log to Railway console too
    try:
        requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": PUSHOVER_API_TOKEN,   # FIXED: was PUSHOVER_TOKEN
                "user": PUSHOVER_USER_KEY,     # FIXED: was PUSHOVER_USER
                "message": message
            },
            timeout=10
        )
    except Exception as e:
        print(f"Notification failed: {e}")

# =========================
# MARKET HOURS CHECK
# =========================

def is_market_open():
    now_et = datetime.now(ET)
    # Monday=0, Friday=4
    if now_et.weekday() > 4:
        return False
    market_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now_et < market_close

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
    first_15 = df.iloc[:15]
    high = first_15["high"].max()
    low  = first_15["low"].min()
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

position    = None
entry_price = None

notify("🚀 Bot started")

# =========================
# MAIN LOOP
# =========================

while True:
    try:
        if not is_market_open():
            print(f"Market closed — {datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')} — sleeping 60s")
            time.sleep(60)
            continue

        df = get_data()
        current_price = df["close"].iloc[-1]
        high, low = get_orb_levels(df)

        # =========================
        # ENTRY LOGIC
        # =========================

        if position is None:

            if current_price > high:
                place_order(OrderSide.BUY)
                position    = "LONG"
                entry_price = current_price
                notify(f"📈 BUY {SYMBOL} @ {current_price:.2f}")

            elif current_price < low:
                place_order(OrderSide.SELL)
                position    = "SHORT"
                entry_price = current_price
                notify(f"📉 SHORT {SYMBOL} @ {current_price:.2f}")

        # =========================
        # EXIT LOGIC
        # =========================

        elif position == "LONG":
            change = (current_price - entry_price) / entry_price

            if change >= TAKE_PROFIT:
                place_order(OrderSide.SELL)
                notify(f"💰 EXIT LONG {SYMBOL} +{change:.2%}")
                position = None

            elif change <= -STOP_LOSS:
                place_order(OrderSide.SELL)
                notify(f"🛑 STOP LONG {SYMBOL} {change:.2%}")
                position = None

        elif position == "SHORT":
            change = (entry_price - current_price) / entry_price

            if change >= TAKE_PROFIT:
                place_order(OrderSide.BUY)
                notify(f"💰 EXIT SHORT {SYMBOL} +{change:.2%}")
                position = None

            elif change <= -STOP_LOSS:
                place_order(OrderSide.BUY)
                notify(f"🛑 STOP SHORT {SYMBOL} {change:.2%}")
                position = None

        time.sleep(60)

    except Exception as e:
        notify(f"⚠️ ERROR: {e}")
        print("Error:", e)
        time.sleep(60)
