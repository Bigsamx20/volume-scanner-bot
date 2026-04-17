import os
import time
import threading
from pybit.unified_trading import HTTP, WebSocket

# ===== CONFIG =====
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT").split(",") if s.strip()]
TIMEFRAMES = [tf.strip() for tf in os.getenv("TIMEFRAMES", "1,5,60").split(",") if tf.strip()]

RSI_ENABLED = os.getenv("RSI_ENABLED", "true").lower() == "true"
EMA_ENABLED = os.getenv("EMA_ENABLED", "true").lower() == "true"

HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "300"))
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "60"))

# ===== STATE =====
data = {}
session = None


def init_state():
    for symbol in SYMBOLS:
        data[symbol] = {}
        for tf in TIMEFRAMES:
            data[symbol][tf] = []


def ema(values, length=200):
    if len(values) < length:
        return None

    k = 2 / (length + 1)
    ema_val = sum(values[:length]) / length

    for value in values[length:]:
        ema_val = value * k + ema_val * (1 - k)

    return ema_val


def rsi(values, length=14):
    if len(values) < length + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))

    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length

    for i in range(length, len(gains)):
        avg_gain = ((avg_gain * (length - 1)) + gains[i]) / length
        avg_loss = ((avg_loss * (length - 1)) + losses[i]) / length

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def heartbeat():
    while True:
        print("BOT ALIVE ✅")
        time.sleep(HEARTBEAT_SECONDS))


def fetch_history(symbol, tf):
    try:
        response = session.get_kline(
            category="linear",
            symbol=symbol,
            interval=tf,
            limit=HISTORY_LIMIT
        )

        rows = response.get("result", {}).get("list", [])
        if not rows:
            print(f"No history found for {symbol} {tf}")
            return

        rows.reverse()  # oldest -> newest
        closes = []

        for row in rows:
            try:
                closes.append(float(row[4]))
            except (TypeError, ValueError, IndexError):
                continue

        data[symbol][tf] = closes
        print(f"Loaded history: {symbol} {tf} ({len(closes)} candles)")

        if closes:
            process_indicators(symbol, tf, closes[-1], source="history")

    except Exception as e:
        print(f"History fetch error {symbol} {tf}: {e}")


def process_indicators(symbol, tf, close, source="live"):
    arr = data[symbol][tf]

    print(f"TEST SIGNAL: {symbol} {tf} close={close} source={source}")

    if RSI_ENABLED:
        r = rsi(arr)
        if r is not None:
            print(f"{symbol} {tf} RSI: {r:.2f}")

    if EMA_ENABLED:
        e = ema(arr, 200)
        if e is not None:
            dist = ((close - e) / e) * 100
            print(f"{symbol} {tf} EMA200 DIST: {dist:.2f}%")


def handle(msg):
    try:
        if not isinstance(msg, dict):
            return

        if "data" not in msg:
            return

        data_list = msg["data"]

        if not isinstance(data_list, list):
            return

        for item in data_list:
            if not isinstance(item, dict):
                continue

            required_keys = ["symbol", "interval", "close", "confirm"]
            if not all(key in item for key in required_keys):
                continue

            symbol = str(item["symbol"]).upper()
            tf = str(item["interval"])
            confirm = bool(item["confirm"])

            try:
                close = float(item["close"])
            except (TypeError, ValueError):
                continue

            if symbol not in data or tf not in data[symbol]:
                continue

            if not confirm:
                continue

            arr = data[symbol][tf]
            arr.append(close)

            if len(arr) > HISTORY_LIMIT:
                arr.pop(0)

            process_indicators(symbol, tf, close, source="live")

    except Exception as e:
        print("error:", e)


def main():
    global session

    init_state()

    print("Starting bot...")
    print("TEST ALERT: bot started successfully")

    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()

    session = HTTP(testnet=False)

    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            fetch_history(symbol, tf)

    ws = WebSocket(testnet=False, channel_type="linear")

    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            ws.kline_stream(
                interval=int(tf),
                symbol=symbol,
                callback=handle
            )
            print(f"Subscribed: {symbol} {tf}")

    print("Bot is running...")

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
