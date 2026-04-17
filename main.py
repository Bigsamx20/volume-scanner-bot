import os
import time
from pybit.unified_trading import HTTP, WebSocket

# ===== CONFIG FROM ENV =====
SYMBOLS = os.getenv("SYMBOLS", "BTCUSDT").split(",")
TIMEFRAMES = os.getenv("TIMEFRAMES", "1,5,60").split(",")

RSI_ENABLED = os.getenv("RSI_ENABLED", "true").lower() == "true"
EMA_ENABLED = os.getenv("EMA_ENABLED", "true").lower() == "true"

# ===== SIMPLE STATE =====
data = {}

def init_state():
    for s in SYMBOLS:
        data[s] = {}
        for tf in TIMEFRAMES:
            data[s][tf] = []

def ema(values, length=200):
    if len(values) < length:
        return None
    k = 2 / (length + 1)
    ema_val = sum(values[:length]) / length
    for v in values[length:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val

def rsi(values, length=14):
    if len(values) < length + 1:
        return None
    gains, losses = [], []
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

def handle(msg):
    try:
        for item in msg.get("data", []):
            symbol = item["symbol"]
            tf = str(item["interval"])
            close = float(item["close"])
            confirm = item["confirm"]

            arr = data[symbol][tf]

            if confirm:
                arr.append(close)
                if len(arr) > 300:
                    arr.pop(0)

                # ===== CALCULATE =====
                if RSI_ENABLED:
                    r = rsi(arr)
                    if r:
                        print(f"{symbol} {tf} RSI: {r:.2f}")

                if EMA_ENABLED:
                    e = ema(arr, 200)
                    if e:
                        dist = (close - e) / e * 100
                        print(f"{symbol} {tf} EMA200 DIST: {dist:.2f}%")

    except Exception as e:
        print("error:", e)

def main():
    init_state()

   ws = WebSocket(testnet=False, channel_type="linear")

    for s in SYMBOLS:
        for tf in TIMEFRAMES:
            ws.kline_stream(
                interval=int(tf),
                symbol=s,
                callback=handle
            )

    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()
