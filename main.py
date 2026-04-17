import os
import time
from pybit.unified_trading import HTTP, WebSocket

# ===== CONFIG =====
SYMBOLS = os.getenv("SYMBOLS", "BTCUSDT").split(",")
TIMEFRAMES = os.getenv("TIMEFRAMES", "1,5,60").split(",")

RSI_ENABLED = os.getenv("RSI_ENABLED", "true").lower() == "true"
EMA_ENABLED = os.getenv("EMA_ENABLED", "true").lower() == "true"

# ===== STATE =====
data = {}


def init_state():
    for symbol in SYMBOLS:
        data[symbol] = {}
        for tf in TIMEFRAMES:
            data[symbol][tf] = []


# ===== INDICATORS =====
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

    for i in range(1, length + 1):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))

    avg_gain = sum(gains) / length
    avg_loss = sum(losses) / length

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ===== HANDLER =====
def handle(msg):
    try:
        print("RAW MESSAGE:", msg)

        data_list = msg.get("data", [])

        for item in data_list:
            symbol = item.get("symbol")
            interval = item.get("interval")
            close = item.get("close")
            confirm = item.get("confirm")

            # Skip if any key missing
            if symbol is None or interval is None or close is None:
                continue

            tf = str(interval)
            close = float(close)

            # Skip unknown symbol/timeframe
            if symbol not in data or tf not in data[symbol]:
                continue

            arr = data[symbol][tf]

            if confirm:
                arr.append(close)

                if len(arr) > 300:
                    arr.pop(0)

                # ===== RSI =====
                if RSI_ENABLED:
                    r = rsi(arr)
                    if r is not None:
                        print(f"{symbol} {tf} RSI: {r:.2f}")

                # ===== EMA DISTANCE =====
                if EMA_ENABLED:
                    e = ema(arr, 200)
                    if e is not None:
                        dist = (close - e) / e * 100
                        print(f"{symbol} {tf} EMA200 DIST: {dist:.2f}%")

    except Exception as e:
        print("error:", e)


# ===== MAIN =====
def main():
    init_state()

    print("Starting bot...")

    # HTTP (for later use)
    session = HTTP(testnet=False)

    # WebSocket
    ws = WebSocket(testnet=False, channel_type="linear")

    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            ws.kline_stream(
                interval=int(tf),
                symbol=symbol,
                callback=handle
            )

    print("Bot is running...")

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
