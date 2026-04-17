import os
import time
import threading
import requests
from pybit.unified_trading import HTTP, WebSocket

# =========================
# BASIC CONFIG
# =========================
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT").split(",") if s.strip()]
TIMEFRAMES = [tf.strip() for tf in os.getenv("TIMEFRAMES", "1,5,60").split(",") if tf.strip()]

TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "300"))
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "300"))
ALERT_COOLDOWN = int(os.getenv("ALERT_COOLDOWN", "900"))

# Set to true if you want BOT ALIVE messages on Telegram
HEARTBEAT_TO_TELEGRAM = os.getenv("HEARTBEAT_TO_TELEGRAM", "true").lower() == "true"

# =========================
# SETTINGS FOR ALL SYMBOLS
# Edit only this block
# =========================
TIMEFRAME_SETTINGS = {
    "1": {
        "rsi": {
            "enabled": True,
            "length": 14,
            "overbought": 70,
            "oversold": 30,
        },
        "ema_distance": {
            "enabled": True,
            "ema_length": 200,
            "above_percent": 2.0,
            "below_percent": -2.0,
        },
    },
    "5": {
        "rsi": {
            "enabled": True,
            "length": 14,
            "overbought": 72,
            "oversold": 28,
        },
        "ema_distance": {
            "enabled": True,
            "ema_length": 200,
            "above_percent": 4.0,
            "below_percent": -4.0,
        },
    },
    "60": {
        "rsi": {
            "enabled": True,
            "length": 14,
            "overbought": 75,
            "oversold": 25,
        },
        "ema_distance": {
            "enabled": True,
            "ema_length": 200,
            "above_percent": 20.0,
            "below_percent": -20.0,
        },
    },
}

# =========================
# GLOBAL STATE
# =========================
data = {}
session = None
last_alert_time = {}


# =========================
# TELEGRAM
# =========================
def send_telegram(message: str):
    if not TELEGRAM_ENABLED:
        return

    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram config missing")
        return

    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        response = requests.post(
            url,
            json={"chat_id": CHAT_ID, "text": message},
            timeout=10,
        )
        print("Telegram status:", response.status_code)
        print("Telegram response:", response.text)
    except Exception as e:
        print("Telegram error:", e)


# =========================
# ALERT COOLDOWN
# =========================
def can_alert(key: str) -> bool:
    now = time.time()
    last = last_alert_time.get(key, 0)

    if now - last >= ALERT_COOLDOWN:
        last_alert_time[key] = now
        return True

    return False


# =========================
# HEARTBEAT
# =========================
def heartbeat():
    while True:
        print("BOT ALIVE ✅")
        if HEARTBEAT_TO_TELEGRAM:
            send_telegram("BOT ALIVE ✅")
        time.sleep(HEARTBEAT_SECONDS)


# =========================
# STATE INIT
# =========================
def init_state():
    for symbol in SYMBOLS:
        data[symbol] = {}
        for tf in TIMEFRAMES:
            data[symbol][tf] = []


# =========================
# INDICATORS
# =========================
def ema(values, length=200):
    if len(values) < length:
        return None

    multiplier = 2 / (length + 1)
    ema_value = sum(values[:length]) / length

    for value in values[length:]:
        ema_value = ((value - ema_value) * multiplier) + ema_value

    return ema_value


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


# =========================
# SETTINGS HELPERS
# =========================
def get_tf_settings(tf: str) -> dict:
    return TIMEFRAME_SETTINGS.get(tf, {})


# =========================
# HISTORY LOADING
# =========================
def fetch_history(symbol: str, tf: str):
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


# =========================
# INDICATOR PROCESSING
# =========================
def process_indicators(symbol: str, tf: str, close: float, source="live"):
    arr = data[symbol][tf]
    cfg = get_tf_settings(tf)

    rsi_cfg = cfg.get("rsi", {})
    ema_cfg = cfg.get("ema_distance", {})

    # ---------- RSI ----------
    if rsi_cfg.get("enabled", False):
        rsi_length = int(rsi_cfg.get("length", 14))
        r = rsi(arr, rsi_length)

        if r is not None:
            print(f"{symbol} {tf} RSI: {r:.2f} ({source})")

            overbought = float(rsi_cfg.get("overbought", 70))
            oversold = float(rsi_cfg.get("oversold", 30))

            if r >= overbought:
                key = f"rsi_overbought:{symbol}:{tf}"
                if can_alert(key):
                    send_telegram(
                        f"🚨 RSI OVERBOUGHT\n"
                        f"Symbol: {symbol}\n"
                        f"Timeframe: {tf}\n"
                        f"RSI: {r:.2f}\n"
                        f"Threshold: {overbought}"
                    )

            if r <= oversold:
                key = f"rsi_oversold:{symbol}:{tf}"
                if can_alert(key):
                    send_telegram(
                        f"🚨 RSI OVERSOLD\n"
                        f"Symbol: {symbol}\n"
                        f"Timeframe: {tf}\n"
                        f"RSI: {r:.2f}\n"
                        f"Threshold: {oversold}"
                    )

    # ---------- EMA DISTANCE ----------
    if ema_cfg.get("enabled", False):
        ema_length = int(ema_cfg.get("ema_length", 200))
        ema_value = ema(arr, ema_length)

        if ema_value is not None:
            dist = ((close - ema_value) / ema_value) * 100
            print(f"{symbol} {tf} EMA{ema_length} DIST: {dist:.2f}% ({source})")

            above_percent = float(ema_cfg.get("above_percent", 2.0))
            below_percent = float(ema_cfg.get("below_percent", -2.0))

            if dist >= above_percent:
                key = f"ema_above:{symbol}:{tf}"
                if can_alert(key):
                    send_telegram(
                        f"📈 PRICE ABOVE EMA{ema_length}\n"
                        f"Symbol: {symbol}\n"
                        f"Timeframe: {tf}\n"
                        f"Distance: {dist:.2f}%\n"
                        f"Threshold: {above_percent}%"
                    )

            if dist <= below_percent:
                key = f"ema_below:{symbol}:{tf}"
                if can_alert(key):
                    send_telegram(
                        f"📉 PRICE BELOW EMA{ema_length}\n"
                        f"Symbol: {symbol}\n"
                        f"Timeframe: {tf}\n"
                        f"Distance: {dist:.2f}%\n"
                        f"Threshold: {below_percent}%"
                    )


# =========================
# WEBSOCKET HANDLER
# =========================
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
        print("Handler error:", e)


# =========================
# MAIN
# =========================
def main():
    global session

    init_state()

    print("Starting bot...")
    send_telegram("🚀 Bot started")

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
