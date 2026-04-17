import os
import time
import threading
import requests
from pybit.unified_trading import HTTP

# =========================
# CONFIG
# =========================
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CATEGORY = os.getenv("BYBIT_CATEGORY", "linear")
TOP_N = int(os.getenv("TOP_N", "30"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "300"))
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "300"))
ALERT_COOLDOWN = int(os.getenv("ALERT_COOLDOWN", "900"))
SHORTLIST_REFRESH_SECONDS = int(os.getenv("SHORTLIST_REFRESH_SECONDS", "300"))
REQUEST_SLEEP_SECONDS = float(os.getenv("REQUEST_SLEEP_SECONDS", "0.12"))
HEARTBEAT_TO_TELEGRAM = os.getenv("HEARTBEAT_TO_TELEGRAM", "true").lower() == "true"

TIMEFRAMES = ["1", "5", "60"]

# Same settings apply to all symbols
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
# GLOBALS
# =========================
session = HTTP(testnet=False)
data = {}
last_alert_time = {}
shortlist = set()
shortlist_lock = threading.Lock()


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
    except Exception as e:
        print("Telegram error:", e)


# =========================
# HELPERS
# =========================
def can_alert(key: str) -> bool:
    now = time.time()
    last = last_alert_time.get(key, 0)
    if now - last >= ALERT_COOLDOWN:
        last_alert_time[key] = now
        return True
    return False


def heartbeat():
    while True:
        print("BOT ALIVE ✅")
        if HEARTBEAT_TO_TELEGRAM:
            send_telegram("BOT ALIVE ✅")
        time.sleep(HEARTBEAT_SECONDS)


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


def ensure_symbol_state(symbol: str):
    if symbol not in data:
        data[symbol] = {}
    for tf in TIMEFRAMES:
        if tf not in data[symbol]:
            data[symbol][tf] = []


def safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# =========================
# MARKET DISCOVERY
# =========================
def get_all_linear_symbols():
    symbols = []
    cursor = None

    while True:
        kwargs = {"category": CATEGORY, "limit": 1000}
        if cursor:
            kwargs["cursor"] = cursor

        response = session.get_instruments_info(**kwargs)
        result = response.get("result", {})
        items = result.get("list", [])

        for item in items:
            symbol = item.get("symbol")
            status = item.get("status")
            quote_coin = item.get("quoteCoin")

            if not symbol:
                continue

            if status != "Trading":
                continue

            if quote_coin != "USDT":
                continue

            symbols.append(symbol)

        cursor = result.get("nextPageCursor")
        if not cursor:
            break

    symbols = sorted(set(symbols))
    print(f"Loaded {len(symbols)} symbols")
    return symbols


def get_tickers():
    response = session.get_tickers(category=CATEGORY)
    return response.get("result", {}).get("list", [])


def build_shortlist_from_tickers():
    tickers = get_tickers()

    one_min_candidates = []
    five_min_candidates = []
    one_hour_candidates = []

    for item in tickers:
        symbol = item.get("symbol")
        if not symbol:
            continue

        last_price = safe_float(item.get("lastPrice"))
        prev_price_1h = safe_float(item.get("prevPrice1h"))
        turnover_24h = safe_float(item.get("turnover24h"), 0.0)

        if last_price is None or last_price <= 0:
            continue

        # Filter out dead / illiquid instruments
        if turnover_24h is None or turnover_24h <= 0:
            continue

        # 1h gainers can be estimated directly if prevPrice1h exists
        if prev_price_1h and prev_price_1h > 0:
            change_1h = ((last_price - prev_price_1h) / prev_price_1h) * 100
            one_hour_candidates.append((symbol, change_1h, turnover_24h))
        else:
            one_hour_candidates.append((symbol, 0.0, turnover_24h))

        # For 1m/5m, use 24h-turnover as a cheap prefilter and refine later with klines
        one_min_candidates.append((symbol, turnover_24h))
        five_min_candidates.append((symbol, turnover_24h))

    # For 1m and 5m we first choose liquid symbols only
    one_min_candidates.sort(key=lambda x: x[1], reverse=True)
    five_min_candidates.sort(key=lambda x: x[1], reverse=True)
    one_hour_candidates.sort(key=lambda x: x[1], reverse=True)

    liquid_1m = [s for s, _ in one_min_candidates[: max(TOP_N * 3, 60)]]
    liquid_5m = [s for s, _ in five_min_candidates[: max(TOP_N * 3, 60)]]
    top_1h = [s for s, _, _ in one_hour_candidates[:TOP_N]]

    symbols = set(liquid_1m) | set(liquid_5m) | set(top_1h)
    return sorted(symbols)


# =========================
# HISTORY
# =========================
def fetch_history(symbol: str, tf: str):
    try:
        ensure_symbol_state(symbol)

        response = session.get_kline(
            category=CATEGORY,
            symbol=symbol,
            interval=tf,
            limit=HISTORY_LIMIT
        )

        rows = response.get("result", {}).get("list", [])
        if not rows:
            return

        rows.reverse()
        closes = []

        for row in rows:
            if len(row) < 5:
                continue
            close = safe_float(row[4])
            if close is None:
                continue
            closes.append(close)

        if closes:
            data[symbol][tf] = closes

    except Exception as e:
        print(f"History fetch error {symbol} {tf}: {e}")


def refresh_shortlist_and_history():
    global shortlist

    print("Refreshing shortlist...")
    symbols = build_shortlist_from_tickers()
    print(f"Shortlist size: {len(symbols)}")

    new_shortlist = set(symbols)

    for symbol in symbols:
        ensure_symbol_state(symbol)
        for tf in TIMEFRAMES:
            fetch_history(symbol, tf)
            time.sleep(REQUEST_SLEEP_SECONDS)

    with shortlist_lock:
        shortlist = new_shortlist

    print("Shortlist and history refreshed")


# =========================
# RANKING FROM LOADED DATA
# =========================
def get_top_gainers_from_history(tf: str, top_n: int):
    changes = []

    with shortlist_lock:
        current_shortlist = list(shortlist)

    for symbol in current_shortlist:
        arr = data.get(symbol, {}).get(tf, [])
        if len(arr) < 2:
            continue

        prev_close = arr[-2]
        last_close = arr[-1]

        if prev_close <= 0:
            continue

        change = ((last_close - prev_close) / prev_close) * 100
        changes.append((symbol, change))

    changes.sort(key=lambda x: x[1], reverse=True)
    return [symbol for symbol, _ in changes[:top_n]]


# =========================
# SIGNALS
# =========================
def process_indicators(symbol: str, tf: str):
    arr = data.get(symbol, {}).get(tf, [])
    if not arr:
        return

    close = arr[-1]
    cfg = TIMEFRAME_SETTINGS.get(tf, {})
    rsi_cfg = cfg.get("rsi", {})
    ema_cfg = cfg.get("ema_distance", {})

    if rsi_cfg.get("enabled", False):
        r = rsi(arr, int(rsi_cfg.get("length", 14)))
        if r is not None:
            print(f"{symbol} {tf} RSI: {r:.2f}")

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

    if ema_cfg.get("enabled", False):
        ema_length = int(ema_cfg.get("ema_length", 200))
        ema_value = ema(arr, ema_length)

        if ema_value is not None and ema_value != 0:
            dist = ((close - ema_value) / ema_value) * 100
            print(f"{symbol} {tf} EMA{ema_length} DIST: {dist:.2f}%")

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
# BACKGROUND LOOPS
# =========================
def shortlist_refresh_loop():
    while True:
        try:
            refresh_shortlist_and_history()
        except Exception as e:
            print("Shortlist refresh error:", e)

        time.sleep(SHORTLIST_REFRESH_SECONDS)


def scan_loop():
    first_run = True

    while True:
        try:
            with shortlist_lock:
                ready = len(shortlist) > 0

            if not ready:
                if first_run:
                    print("Waiting for shortlist to finish loading...")
                    first_run = False
                time.sleep(5)
                continue

            top_1m = get_top_gainers_from_history("1", TOP_N)
            top_5m = get_top_gainers_from_history("5", TOP_N)
            top_1h = get_top_gainers_from_history("60", TOP_N)

            print("Top 1m gainers:", top_1m[:10])
            print("Top 5m gainers:", top_5m[:10])
            print("Top 1h gainers:", top_1h[:10])

            for symbol in top_1m:
                process_indicators(symbol, "1")

            for symbol in top_5m:
                process_indicators(symbol, "5")

            for symbol in top_1h:
                process_indicators(symbol, "60")

            time.sleep(30)

        except Exception as e:
            print("Scan loop error:", e)
            time.sleep(5)


# =========================
# MAIN
# =========================
def main():
    print("Starting scanner...")
    send_telegram("🚀 Scanner started")

    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(target=shortlist_refresh_loop, daemon=True).start()

    scan_loop()


if __name__ == "__main__":
    main()
