import os
import time
import threading
import requests
from pybit.unified_trading import HTTP, WebSocket

# ===== CONFIG =====
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT").split(",") if s.strip()]
TIMEFRAMES = [tf.strip() for tf in os.getenv("TIMEFRAMES", "1,5,60").split(",") if tf.strip()]

RSI_ENABLED = os.getenv("RSI_ENABLED", "true").lower() == "true"
EMA_ENABLED = os.getenv("EMA_ENABLED", "true").lower() == "true"

TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

HISTORY_LIMIT = 300
HEARTBEAT_SECONDS = 60

data = {}
session = None


# ===== TELEGRAM =====
def send_telegram(msg):
    if not TELEGRAM_ENABLED:
        return

    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print("Telegram error:", e)


# ===== HEARTBEAT =====
def heartbeat():
    while True:
        print("BOT ALIVE ✅")
        send_telegram("BOT ALIVE ✅")
        time.sleep(HEARTBEAT_SECONDS)


# ===== INDICATORS =====
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
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0))
        losses.append(abs(min(d, 0)))

    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length

    for i in range(length, len(gains)):
        avg_gain = ((avg_gain * (length - 1)) + gains[i]) / length
        avg_loss = ((avg_loss * (length - 1)) + losses[i]) / length

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ===== STATE =====
def init_state():
    for s in SYMBOLS:
        data[s] = {}
        for tf in TIMEFRAMES:
            data[s][tf] = []


# ===== PROCESS =====
def process(symbol, tf, close):
    arr = data[symbol][tf]

    if RSI_ENABLED:
        r = rsi(arr)
        if r is not None:
            msg = f"{symbol} {tf} RSI: {r:.2f}"
            print(msg)
            send_telegram(msg)

    if EMA_ENABLED:
        e = ema(arr, 200)
        if e is not None:
            dist = ((close - e) / e) * 100
            msg = f"{symbol} {tf} EMA200 DIST: {dist:.2f}%"
            print(msg)
            send_telegram(msg)


# ===== WS HANDLER =====
def handle(msg):
    try:
        if "data" not in msg:
            return

        for item in msg["data"]:
            if not all(k in item for k in ["symbol", "interval", "close", "confirm"]):
                continue

            symbol = item["symbol"]
            tf = str(item["interval"])
            close = float(item["close"])
            confirm = item["confirm"]

            if not confirm:
                continue

            arr = data[symbol][tf]
            arr.append(close)

            if len(arr) > HISTORY_LIMIT:
                arr.pop(0)

            process(symbol, tf, close)

    except Exception as e:
        print("error:", e)


# ===== MAIN =====
def main():
    global session

    print("Starting bot...")
    send_telegram("🚀 Bot started")

    init_state()

    threading.Thread(target=heartbeat, daemon=True).start()

    session = HTTP(testnet=False)
    ws = WebSocket(testnet=False, channel_type="linear")

    for s in SYMBOLS:
        for tf in TIMEFRAMES:
            ws.kline_stream(interval=int(tf), symbol=s, callback=handle)

    print("Bot is running...")

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
