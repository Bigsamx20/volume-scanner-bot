import os
import time
import threading
import requests
from pybit.unified_trading import HTTP, WebSocket

# =========================
# CONFIG
# =========================
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TOP_N = int(os.getenv("TOP_N", "30"))  # top gainers to track
HISTORY_LIMIT = 300
ALERT_COOLDOWN = 600

# indicator settings (global)
RSI_LENGTH = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30

EMA_LENGTH = 200
EMA_THRESHOLD = 5.0  # % distance

# =========================
data = {}
last_alert_time = {}
session = HTTP(testnet=False)

# =========================
# TELEGRAM
# =========================
def send_telegram(msg):
    if not TELEGRAM_ENABLED:
        return

    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print("Telegram error:", e)


# =========================
def can_alert(key):
    now = time.time()
    last = last_alert_time.get(key, 0)

    if now - last >= ALERT_COOLDOWN:
        last_alert_time[key] = now
        return True
    return False


# =========================
# GET ALL SYMBOLS
# =========================
def get_symbols():
    res = session.get_instruments_info(category="linear")
    symbols = []

    for item in res["result"]["list"]:
        symbols.append(item["symbol"])

    print(f"Loaded {len(symbols)} symbols")
    return symbols


# =========================
# EMA
# =========================
def ema(values, length):
    if len(values) < length:
        return None

    k = 2 / (length + 1)
    ema_val = sum(values[:length]) / length

    for v in values[length:]:
        ema_val = v * k + ema_val * (1 - k)

    return ema_val


# =========================
# RSI
# =========================
def rsi(values, length):
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
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# =========================
# FETCH HISTORY
# =========================
def fetch_history(symbol, tf):
    try:
        res = session.get_kline(
            category="linear",
            symbol=symbol,
            interval=tf,
            limit=HISTORY_LIMIT
        )

        rows = res["result"]["list"]
        rows.reverse()

        closes = [float(r[4]) for r in rows]

        if symbol not in data:
            data[symbol] = {}

        data[symbol][tf] = closes

    except:
        pass


# =========================
# TOP GAINERS
# =========================
def get_top_gainers(tf):
    changes = []

    for symbol in data:
        arr = data[symbol].get(tf, [])
        if len(arr) < 2:
            continue

        change = (arr[-1] - arr[-2]) / arr[-2] * 100
        changes.append((symbol, change))

    changes.sort(key=lambda x: x[1], reverse=True)

    return [s[0] for s in changes[:TOP_N]]


# =========================
# PROCESS SIGNALS
# =========================
def process(symbol, tf):
    arr = data[symbol][tf]
    close = arr[-1]

    # RSI
    r = rsi(arr, RSI_LENGTH)
    if r:
        if r >= RSI_OVERBOUGHT:
            key = f"rsi_high_{symbol}_{tf}"
            if can_alert(key):
                send_telegram(f"🚨 RSI HIGH {symbol} {tf} = {r:.2f}")

        if r <= RSI_OVERSOLD:
            key = f"rsi_low_{symbol}_{tf}"
            if can_alert(key):
                send_telegram(f"🚨 RSI LOW {symbol} {tf} = {r:.2f}")

    # EMA
    e = ema(arr, EMA_LENGTH)
    if e:
        dist = (close - e) / e * 100

        if dist >= EMA_THRESHOLD:
            key = f"ema_up_{symbol}_{tf}"
            if can_alert(key):
                send_telegram(f"📈 {symbol} {tf} +{dist:.2f}% above EMA")

        if dist <= -EMA_THRESHOLD:
            key = f"ema_down_{symbol}_{tf}"
            if can_alert(key):
                send_telegram(f"📉 {symbol} {tf} {dist:.2f}% below EMA")


# =========================
# MAIN LOOP
# =========================
def run_scanner():
    symbols = get_symbols()

    print("Loading history...")

    for s in symbols:
        for tf in ["1", "5", "60"]:
            fetch_history(s, tf)

    print("Scanner started")
    send_telegram("🚀 Scanner started")

    while True:
        try:
            for tf in ["1", "5", "60"]:
                top = get_top_gainers(tf)

                print(f"Top {TOP_N} gainers {tf}m:", top[:5])

                for s in top:
                    process(s, tf)

            time.sleep(30)

        except Exception as e:
            print("scanner error:", e)
            time.sleep(5)


# =========================
if __name__ == "__main__":
    run_scanner()
