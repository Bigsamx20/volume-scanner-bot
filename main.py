import os
import time
import threading
import requests
from pybit.unified_trading import HTTP, WebSocket

# =========================
# CONFIG
# =========================
TELEGRAM_ENABLED = True
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CATEGORY = "linear"
TIMEFRAMES = ["5"]

# =========================
# RSI SETTINGS
# =========================
RSI_LENGTH = 14

RSI_BUY = 15
RSI_SELL = 85

# 🔥 EXTREME SIGNALS
SPECIAL_BUY_RSI = 11
SPECIAL_SELL_RSI = 91

SPECIAL_BUY_SIGNAL_ENABLED = True
SPECIAL_SELL_SIGNAL_ENABLED = True

# =========================
# GLOBALS
# =========================
session = HTTP(testnet=False)

data = {}
last_alert = {}

# =========================
# TELEGRAM
# =========================
def send_telegram(msg):
    if not TELEGRAM_ENABLED:
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": msg})


# =========================
# RSI FUNCTION
# =========================
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

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# =========================
# INDICATORS
# =========================
def process(symbol, close):

    if symbol not in data:
        data[symbol] = []

    data[symbol].append(close)

    if len(data[symbol]) > 100:
        data[symbol].pop(0)

    r = rsi(data[symbol], RSI_LENGTH)
    if r is None:
        return

    print(symbol, "RSI:", r)

    # NORMAL SIGNALS
    if r <= RSI_BUY:
        send_telegram(f"🟢 BUY\n{symbol}\nRSI {r:.2f}")

    if r >= RSI_SELL:
        send_telegram(f"🔴 SELL\n{symbol}\nRSI {r:.2f}")

    # 🔥 EXTREME BUY
    if SPECIAL_BUY_SIGNAL_ENABLED and r <= SPECIAL_BUY_RSI:
        send_telegram(
            f"🟢🔥 EXTREME BUY\n"
            f"{symbol}\n"
            f"RSI {r:.2f}"
        )

    # 🔴 EXTREME SELL
    if SPECIAL_SELL_SIGNAL_ENABLED and r >= SPECIAL_SELL_RSI:
        send_telegram(
            f"🔴🔥 EXTREME SELL\n"
            f"{symbol}\n"
            f"RSI {r:.2f}"
        )


# =========================
# WS HANDLER
# =========================
def handle_kline(msg):
    try:
        data_list = msg.get("data", [])

        for k in data_list:
            symbol = k["symbol"]
            close = float(k["close"])
            process(symbol, close)

    except Exception as e:
        print("Error:", e)


# =========================
# WEBSOCKET
# =========================
def start():
    ws = WebSocket(testnet=False, channel_type="linear")

    symbols = ["BTCUSDT", "ETHUSDT"]

    for s in symbols:
        ws.kline_stream(
            symbol=s,
            interval=5,
            callback=handle_kline
        )

    print("Bot running...")

    while True:
        time.sleep(10)


# =========================
# TELEGRAM COMMANDS
# =========================
def telegram_loop():
    offset = None

    global RSI_BUY, RSI_SELL
    global SPECIAL_BUY_RSI, SPECIAL_SELL_RSI
    global SPECIAL_BUY_SIGNAL_ENABLED, SPECIAL_SELL_SIGNAL_ENABLED

    while True:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        res = requests.get(url, params={"offset": offset}).json()

        for r in res.get("result", []):
            offset = r["update_id"] + 1
            text = r["message"]["text"]

            if text.startswith("/setrsibuy"):
                RSI_BUY = float(text.split()[1])
                send_telegram(f"BUY RSI = {RSI_BUY}")

            elif text.startswith("/setrsisell"):
                RSI_SELL = float(text.split()[1])
                send_telegram(f"SELL RSI = {RSI_SELL}")

            elif text.startswith("/setspecialbuy"):
                SPECIAL_BUY_RSI = float(text.split()[1])
                send_telegram(f"EXTREME BUY RSI = {SPECIAL_BUY_RSI}")

            elif text.startswith("/setspecialsell"):
                SPECIAL_SELL_RSI = float(text.split()[1])
                send_telegram(f"EXTREME SELL RSI = {SPECIAL_SELL_RSI}")

            elif text == "/specialbuyon":
                SPECIAL_BUY_SIGNAL_ENABLED = True
                send_telegram("EXTREME BUY ON")

            elif text == "/specialbuyoff":
                SPECIAL_BUY_SIGNAL_ENABLED = False
                send_telegram("EXTREME BUY OFF")

            elif text == "/specialsellon":
                SPECIAL_SELL_SIGNAL_ENABLED = True
                send_telegram("EXTREME SELL ON")

            elif text == "/specialselloff":
                SPECIAL_SELL_SIGNAL_ENABLED = False
                send_telegram("EXTREME SELL OFF")

            elif text == "/showsignal":
                send_telegram(
                    f"Buy RSI: {RSI_BUY}\n"
                    f"Sell RSI: {RSI_SELL}\n"
                    f"Extreme Buy: {SPECIAL_BUY_RSI}\n"
                    f"Extreme Sell: {SPECIAL_SELL_RSI}"
                )

        time.sleep(2)


# =========================
# MAIN
# =========================
if __name__ == "__main__":
    threading.Thread(target=telegram_loop).start()
    start()
