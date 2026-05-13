import time
import requests
import os
import pytz
from datetime import datetime

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

QTY            = 1      # shares per trade
TAKE_PROFIT    = 1.50   # dollar target per trade
STOP_LOSS      = 1.00   # dollar stop per trade
MAX_DAY_TRADES = 3      # PDT limit — keep at 3 if account < $25k
MAX_POSITIONS  = 3      # max simultaneous open positions
MAX_PRICE      = 300    # skip stocks above this price

# Minimum % moves so stops aren't too tight on expensive stocks
MIN_TP_PCT = 0.004
MIN_SL_PCT = 0.002

# No new entries after this time ET
NO_ENTRY_AFTER_HOUR   = 15
NO_ENTRY_AFTER_MINUTE = 30

ET = pytz.timezone("America/New_York")

# =========================
# SECTOR ETFs — used to detect which sectors are hot
# =========================

SECTOR_ETFS = {
    "Technology":    "XLK",
    "Energy":        "XLE",
    "Financials":    "XLF",
    "Healthcare":    "XLV",
    "ConsumerDisc":  "XLY",
    "Industrials":   "XLI",
    "Materials":     "XLB",
    "Utilities":     "XLU",
    "RealEstate":    "XLRE",
    "ConsumerStap":  "XLP",
}

# Stocks mapped to each sector — bot focuses on strongest sectors
SECTOR_STOCKS = {
    "Technology":   ["AAPL", "MSFT", "NVDA", "AMD", "INTC", "MU", "PLTR", "SNOW"],
    "Energy":       ["XOM", "CVX", "OXY", "SLB", "HAL"],
    "Financials":   ["JPM", "BAC", "GS", "MS", "SOFI", "COIN"],
    "Healthcare":   ["UNH", "PFE", "MRNA", "ABT", "CVS"],
    "ConsumerDisc": ["TSLA", "AMZN", "NKE", "F", "GM", "RIVN"],
    "Industrials":  ["BA", "CAT", "GE", "HON", "UPS"],
    "Materials":    ["FCX", "NEM", "AA", "CLF"],
    "Utilities":    ["NEE", "DUK", "SO"],
    "RealEstate":   ["AMT", "PLD", "SPG"],
    "ConsumerStap": ["WMT", "PG", "KO", "COST"],
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
# MARKET REGIME DETECTION
# Reads SPY trend to decide if we go long, short, or sit out
# =========================

def get_market_regime():
    """
    Analyzes SPY to determine overall market direction.
    Returns: 'bullish', 'bearish', or 'choppy'
    """
    try:
        req = StockBarsRequest(
            symbol_or_symbols=["SPY"],
            timeframe=TimeFrame.Minute,
            limit=100
        )
        df = data_client.get_stock_bars(req).df.reset_index()

        open_price    = df["open"].iloc[0]
        current_price = df["close"].iloc[-1]
        high_of_day   = df["high"].max()
        low_of_day    = df["low"].min()

        daily_change  = (current_price - open_price) / open_price

        # Check if SPY is making higher highs and higher lows (uptrend)
        mid           = len(df) // 2
        first_half_high  = df["high"].iloc[:mid].max()
        second_half_high = df["high"].iloc[mid:].max()
        first_half_low   = df["low"].iloc[:mid].min()
        second_half_low  = df["low"].iloc[mid:].min()

        higher_highs = second_half_high > first_half_high
        higher_lows  = second_half_low  > first_half_low
        lower_highs  = second_half_high < first_half_high
        lower_lows   = second_half_low  < first_half_low

        if daily_change > 0.003 and higher_highs and higher_lows:
            return "bullish"
        elif daily_change < -0.003 and lower_highs and lower_lows:
            return "bearish"
        else:
            return "choppy"

    except Exception as e:
        print(f"Market regime check failed: {e}")
        return "choppy"

# =========================
# SECTOR ROTATION SCANNER
# Finds the top 2 performing sectors and picks stocks from them
# =========================

def get_top_sectors():
    """Returns the top 2 sectors by today's performance."""
    try:
        etf_list = list(SECTOR_ETFS.values())
        req      = StockSnapshotRequest(symbol_or_symbols=etf_list)
        snaps    = data_client.get_stock_snapshot(req)

        scored = []
        for sector, etf in SECTOR_ETFS.items():
            snap = snaps.get(etf)
            if not snap:
                continue
            try:
                prev  = snap.prev_daily_bar.close
                cur   = snap.daily_bar.close
                vol   = snap.daily_bar.volume
                pct   = (cur - prev) / prev
                scored.append((sector, etf, pct, vol))
            except Exception:
                continue

        scored.sort(key=lambda x: x[2], reverse=True)
        top = scored[:2]
        print(f"Top sectors: {[(s[0], f'{s[2]:.2%}') for s in top]}")
        return [s[0] for s in top]

    except Exception as e:
        print(f"Sector scan failed: {e}")
        return list(SECTOR_STOCKS.keys())[:2]

# =========================
# STOCK SCANNER
# Picks best stocks from the top sectors
# =========================

def scan_symbols(regime):
    """
    Finds top stocks from the strongest sectors,
    filtered by price, volume, relative strength vs SPY,
    and earnings gap filter.
    """
    try:
        # Get SPY's daily change as benchmark
        spy_req  = StockSnapshotRequest(symbol_or_symbols=["SPY"])
        spy_snap = data_client.get_stock_snapshot(spy_req).get("SPY")
        spy_change = 0
        if spy_snap:
            spy_change = (spy_snap.daily_bar.close - spy_snap.prev_daily_bar.close) / spy_snap.prev_daily_bar.close

        # Get top sectors and their stocks
        top_sectors = get_top_sectors()

        # In bearish regime also include short candidates from weak sectors
        if regime == "bearish":
            candidates = []
            for sector, stocks in SECTOR_STOCKS.items():
                candidates.extend(stocks)
        else:
            candidates = []
            for sector in top_sectors:
                candidates.extend(SECTOR_STOCKS.get(sector, []))

        # Remove duplicates
        candidates = list(set(candidates))

        req   = StockSnapshotRequest(symbol_or_symbols=candidates)
        snaps = data_client.get_stock_snapshot(req)

        scored = []
        for sym, snap in snaps.items():
            try:
                prev  = snap.prev_daily_bar.close
                cur   = snap.daily_bar.close
                vol   = snap.daily_bar.volume
                op    = snap.daily_bar.open

                # Price filter — skip expensive stocks
                if cur > MAX_PRICE:
                    continue

                # Earnings gap filter — skip stocks that gapped >5% overnight
                overnight_gap = abs((op - prev) / prev)
                if overnight_gap > 0.05:
                    print(f"Skipping {sym} — likely earnings gap ({overnight_gap:.1%})")
                    continue

                # Relative strength vs SPY
                stock_change      = (cur - prev) / prev
                relative_strength = stock_change - spy_change

                # Volume surge check (need historical avg — use today's vol as proxy)
                score = vol * abs(relative_strength)

                # Boost stocks moving WITH the regime
                if regime == "bullish" and stock_change > 0:
                    score *= 1.5
                elif regime == "bearish" and stock_change < 0:
                    score *= 1.5

                scored.append((sym, score, cur))

            except Exception:
                continue

        scored.sort(key=lambda x: x[1], reverse=True)
        top = [s[0] for s in scored[:10]]
        print(f"Today's watchlist ({regime} market): {top}")
        notify(f"Market: {regime.upper()} | Watchlist: {', '.join(top)}")
        return top

    except Exception as e:
        print(f"Stock scanner failed: {e}")
        return ["SPY", "QQQ", "AAPL", "TSLA", "AMD"]

# =========================
# GET BARS
# =========================

def get_data(symbol):
    req = StockBarsRequest(
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
# Only enter if breakout candle has above-average volume
# =========================

def has_volume_confirmation(df):
    avg_vol     = df["volume"].mean()
    latest_vol  = df["volume"].iloc[-1]
    return latest_vol > avg_vol * 1.5

# =========================
# DOLLAR-BASED TP/SL
# Uses $1.50/$1.00 with a minimum % floor
# =========================

def get_tp_sl(entry_price):
    tp = max(TAKE_PROFIT, entry_price * MIN_TP_PCT)
    sl = max(STOP_LOSS,   entry_price * MIN_SL_PCT)
    return tp, sl

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
# STATE
# =========================

positions  = {}   # { "AAPL": {"side": "LONG", "entry": 175.00, "tp": 1.50, "sl": 1.00} }
watchlist  = []
regime     = "choppy"
last_date  = None

notify("Bot started and running on Railway")

# =========================
# MAIN LOOP
# =========================

while True:
    try:
        now_et = datetime.now(ET)
        today  = now_et.date()

        # Reset daily state
        if last_date != today:
            positions = {}
            watchlist = []
            regime    = "choppy"
            last_date = today
            print(f"New trading day: {today}")

        if not is_market_open():
            print(f"Market closed — {now_et.strftime('%Y-%m-%d %H:%M ET')} — sleeping 60s")
            time.sleep(60)
            continue

        # Build watchlist once after ORB window
        if not watchlist and is_orb_window_complete():
            regime    = get_market_regime()
            watchlist = scan_symbols(regime)

        if not watchlist:
            print(f"Waiting for ORB window (9:45 AM ET) — {now_et.strftime('%H:%M ET')}")
            time.sleep(60)
            continue

        # Refresh market regime every 30 minutes
        if now_et.minute % 30 == 0:
            new_regime = get_market_regime()
            if new_regime != regime:
                notify(f"Market regime changed: {regime.upper()} → {new_regime.upper()}")
                regime = new_regime

        # Sit out completely if market is choppy and no open positions
        if regime == "choppy" and len(positions) == 0:
            print(f"Choppy market — sitting out new entries")
            time.sleep(60)
            continue

        pdt_used = get_day_trade_count()

        # Force-exit all positions near close
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

        # Process each symbol
        for symbol in watchlist:
            try:
                df = get_data(symbol)
                if df.empty or len(df) < 20:
                    continue

                current_price = df["close"].iloc[-1]
                high, low     = get_orb_levels(df)

                # ---- MANAGE OPEN POSITION ----
                if symbol in positions:
                    pos    = positions[symbol]
                    entry  = pos["entry"]
                    tp     = pos["tp"]
                    sl     = pos["sl"]

                    gain = (current_price - entry) if pos["side"] == "LONG" else (entry - current_price)

                    if gain >= tp:
                        exit_side = OrderSide.SELL if pos["side"] == "LONG" else OrderSide.BUY
                        place_order(symbol, exit_side)
                        notify(f"PROFIT {pos['side']} {symbol} @ ${current_price:.2f} | +${gain:.2f}")
                        del positions[symbol]

                    elif gain <= -sl:
                        exit_side = OrderSide.SELL if pos["side"] == "LONG" else OrderSide.BUY
                        place_order(symbol, exit_side)
                        notify(f"STOP {pos['side']} {symbol} @ ${current_price:.2f} | -${abs(gain):.2f}")
                        del positions[symbol]
                        # Moderate: position removed so re-entry is allowed later today

                # ---- LOOK FOR NEW ENTRY ----
                elif (not is_after_no_entry_time()
                      and is_orb_window_complete()
                      and pdt_used < MAX_DAY_TRADES
                      and len(positions) < MAX_POSITIONS
                      and has_volume_confirmation(df)):

                    tp_amt, sl_amt = get_tp_sl(current_price)

                    # Bullish regime — only longs
                    if regime == "bullish" and current_price > high:
                        place_order(symbol, OrderSide.BUY)
                        positions[symbol] = {"side": "LONG", "entry": current_price, "tp": tp_amt, "sl": sl_amt}
                        pdt_used += 1
                        notify(f"BUY {symbol} @ ${current_price:.2f} | TP: +${tp_amt:.2f} SL: -${sl_amt:.2f} | PDT: {pdt_used}/3")

                    # Bearish regime — only shorts
                    elif regime == "bearish" and current_price < low:
                        place_order(symbol, OrderSide.SELL)
                        positions[symbol] = {"side": "SHORT", "entry": current_price, "tp": tp_amt, "sl": sl_amt}
                        pdt_used += 1
                        notify(f"SHORT {symbol} @ ${current_price:.2f} | TP: +${tp_amt:.2f} SL: -${sl_amt:.2f} | PDT: {pdt_used}/3")

            except Exception as e:
                print(f"Error processing {symbol}: {e}")
                continue

        time.sleep(60)

    except Exception as e:
        notify(f"ERROR: {e}")
        print(f"Error: {e}")
        time.sleep(60)
