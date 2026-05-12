import time
import requests
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
import os
ALPACA_API_KEY = os.getenv("PKJ3X44PXFSCE6PKMC65QNYERM")
ALPACA_SECRET_KEY = os.getenv("5Podu3X7pPXYNy3UVNCoKdSsPpRVSVS7VJ5izRp4mofX")
PUSHOVER_USER = os.getenv("ukge5ehvpq4hqqk4outq8bkxbo6yv3")
PUSHOVER_TOKEN = os.getenv("ayei96ywnhsnbzpyhaz1gr7bfz3oud")

SYMBOL = "SPY"
QTY = 1

TAKE_PROFIT = 0.20   # +20%
STOP_LOSS = 0.10     # -10%

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
    try:
        requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": PUSHOVER_TOKEN,
                "user": PUSHOVER_USER,
                "message": message
            }
        )
    except:
        print("Notification failed")

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
    low = first_15["low"].min()
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

position = None
entry_price = None

notify("🚀 Bot started")

# =========================
# MAIN LOOP
# =========================

while True:
    try:
        now = datetime.now()

        # Only trade during market hours
        if now.weekday() >= 5:
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
                position = "LONG"
                entry_price = current_price

                notify(f"📈 BUY {SYMBOL} @ {current_price}")

            elif current_price < low:
                place_order(OrderSide.SELL)
                position = "SHORT"
                entry_price = current_price

                notify(f"📉 SHORT {SYMBOL} @ {current_price}")

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