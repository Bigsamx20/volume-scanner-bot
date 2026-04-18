import os
import time
import threading
import requests
import hmac
import hashlib
from urllib.parse import urlencode
from copy import deepcopy
from pybit.unified_trading import HTTP, WebSocket

# =========================
# CONFIG
# =========================
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CATEGORY = os.getenv("BYBIT_CATEGORY", "linear")
TOP_N = int(os.getenv("TOP_N", "500"))
LIVE_WS_SYMBOLS = int(os.getenv("LIVE_WS_SYMBOLS", "80"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "300"))
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "300"))
SHORTLIST_REFRESH_SECONDS = int(os.getenv("SHORTLIST_REFRESH_SECONDS", "300"))
REQUEST_SLEEP_SECONDS = float(os.getenv("REQUEST_SLEEP_SECONDS", "0.12"))
HEARTBEAT_TO_TELEGRAM = os.getenv("HEARTBEAT_TO_TELEGRAM", "true").lower() == "true"
WS_DEBUG = os.getenv("WS_DEBUG", "true").lower() == "true"

TIMEFRAMES = ["5", "60"]

# =========================
# BINANCE TESTNET CONFIG
# =========================
BINANCE_ENABLED = os.getenv("BINANCE_ENABLED", "false").lower() == "true"
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
BINANCE_BASE_URL = os.getenv("BINANCE_BASE_URL", "https://testnet.binance.vision")
BINANCE_SYMBOL = os.getenv("BINANCE_SYMBOL", "BTCUSDT")

# =========================
# DEFAULT SETTINGS
# =========================
TIMEFRAME_DEFAULTS = {
    "5": {
        "rsi": {
            "enabled": True,
            "length": 14,
            "overbought": 83,
            "oversold": 17,
        },
        "ema_distance": {
            "enabled": True,
            "ema_length": 50,
            "above_percent": 2.0,
            "below_percent": -2.0,
        },
    },
    "60": {
        "rsi": {
            "enabled": True,
            "length": 14,
            "overbought": 83,
            "oversold": 17,
        },
        "ema_distance": {
            "enabled": True,
            "ema_length": 100,
            "above_percent": 2.0,
            "below_percent": -2.0,
        },
    },
}

SYMBOL_OVERRIDES = {}

# =========================
# GLOBALS
# =========================
session = HTTP(testnet=False)

data = {}
last_alert_candle = {}
shortlist = set()
live_symbols = set()

shortlist_lock = threading.Lock()
ws_lock = threading.Lock()

ws_connections = {}
current_candle_start = {}
last_closed_candle_start = {}

telegram_offset = 0
last_command_time = 0
last_ws_message_at = 0
last_shortlist_refresh_at = 0

# Binance time sync offset (ms)
binance_time_offset_ms = 0

# =========================
# TELEGRAM
# =========================
def send_telegram(message: str):
    print("Trying to send Telegram:", message)

    if not TELEGRAM_ENABLED:
        print("Telegram disabled (TELEGRAM_ENABLED is not true)")
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
        print("Telegram body:", response.text)
        response.raise_for_status()
    except Exception as e:
        print("Telegram error:", e)


def get_telegram_updates(offset=None, timeout=20):
    if not TELEGRAM_ENABLED or not BOT_TOKEN:
        return []

    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset

        response = requests.get(url, params=params, timeout=timeout + 5)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            return []
        return payload.get("result", [])
    except Exception as e:
        print("Telegram getUpdates error:", e)
        return []

# =========================
# HELPERS
# =========================
def heartbeat():
    while True:
        print("BOT ALIVE ✅")
        if HEARTBEAT_TO_TELEGRAM:
            send_telegram("BOT ALIVE ✅")
        time.sleep(HEARTBEAT_SECONDS)


def safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_interval(interval_value):
    s = str(interval_value).strip().lower()
    mapping = {
        "5": "5",
        "5m": "5",
        "60": "60",
        "60m": "60",
        "1h": "60",
    }
    return mapping.get(s, s)


def ensure_symbol_state(symbol: str):
    if symbol not in data:
        data[symbol] = {}
    if symbol not in current_candle_start:
        current_candle_start[symbol] = {}
    if symbol not in last_closed_candle_start:
        last_closed_candle_start[symbol] = {}

    for tf in TIMEFRAMES:
        if tf not in data[symbol]:
            data[symbol][tf] = []


def deep_merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def get_effective_settings(symbol: str, tf: str) -> dict:
    defaults = TIMEFRAME_DEFAULTS.get(tf, {})
    symbol_cfg = SYMBOL_OVERRIDES.get(symbol, {})
    tf_override = symbol_cfg.get(tf, {})
    return deep_merge(defaults, tf_override)


def should_alert_once_per_candle(alert_type: str, symbol: str, tf: str, candle_start: int) -> bool:
    key = f"{alert_type}:{symbol}:{tf}"
    last = last_alert_candle.get(key)

    if last == candle_start:
        return False

    last_alert_candle[key] = candle_start
    return True

# =========================
# BINANCE TESTNET HELPERS
# =========================
def binance_headers():
    return {
        "X-MBX-APIKEY": BINANCE_API_KEY
    }


def sign_binance_params(params: dict) -> str:
    query_string = urlencode(params)
    return hmac.new(
        BINANCE_SECRET_KEY.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def binance_public_get(path: str, params=None):
    if params is None:
        params = {}

    url = f"{BINANCE_BASE_URL}{path}"
    response = requests.get(url, params=params, timeout=10)

    try:
        payload = response.json()
    except Exception:
        payload = {"raw_text": response.text}

    if not response.ok:
        raise Exception(f"Binance public GET failed {response.status_code}: {payload}")

    return payload


def sync_binance_time():
    global binance_time_offset_ms

    server_time_data = binance_public_get("/api/v3/time")
    server_time = int(server_time_data["serverTime"])
    local_time = int(time.time() * 1000)
    binance_time_offset_ms = server_time - local_time

    print(
        "Binance time sync complete:",
        {
            "serverTime": server_time,
            "localTime": local_time,
            "offsetMs": binance_time_offset_ms,
        }
    )


def get_binance_timestamp():
    return int(time.time() * 1000) + binance_time_offset_ms


def binance_signed_get(path: str, params=None):
    if params is None:
        params = {}

    params = dict(params)
    params["timestamp"] = get_binance_timestamp()
    params["recvWindow"] = 10000
    params["signature"] = sign_binance_params(params)

    url = f"{BINANCE_BASE_URL}{path}"
    response = requests.get(
        url,
        params=params,
        headers=binance_headers(),
        timeout=10
    )

    try:
        payload = response.json()
    except Exception:
        payload = {"raw_text": response.text}

    if not response.ok:
        raise Exception(f"Binance signed GET failed {response.status_code}: {payload}")

    return payload


def test_binance_connection():
    if not BINANCE_ENABLED:
        print("Binance testnet disabled")
        return

    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        print("Binance keys missing")
        send_telegram("❌ Binance Testnet keys missing")
        return

    try:
        print("Testing Binance public ping...")
        ping = binance_public_get("/api/v3/ping")
        print("Binance ping OK:", ping)

        print("Syncing Binance server time...")
        sync_binance_time()

        print(f"Testing Binance ticker for {BINANCE_SYMBOL}...")
        ticker = binance_public_get("/api/v3/ticker/price", {"symbol": BINANCE_SYMBOL})
        print("Binance ticker OK:", ticker)

        print("Testing Binance signed account endpoint...")
        account = binance_signed_get("/api/v3/account")

        balances = account.get("balances", [])
        non_zero = []

        for b in balances:
            free_amt = safe_float(b.get("free"), 0.0)
            locked_amt = safe_float(b.get("locked"), 0.0)
            if free_amt > 0 or locked_amt > 0:
                non_zero.append(f"{b.get('asset')}: free={free_amt}, locked={locked_amt}")

        print("Binance account OK")
        if non_zero:
            print("Non-zero balances:")
            for row in non_zero[:10]:
                print(" -", row)
        else:
            print("No non-zero balances found on testnet account")

        send_telegram(
            f"✅ Binance Testnet connected\n"
            f"Symbol: {BINANCE_SYMBOL}\n"
            f"Time offset: {binance_time_offset_ms} ms"
        )

    except Exception as e:
        print("Binance testnet connection failed:", e)
        send_telegram(f"❌ Binance Testnet connection failed\n{e}")

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
# MARKET DISCOVERY
# =========================
def get_tickers():
    response = session.get_tickers(category=CATEGORY)
    return response.get("result", {}).get("list", [])


def build_shortlist_from_tickers():
    tickers = get_tickers()
    candidates = []

    for item in tickers:
        symbol = str(item.get("symbol", "")).upper()
        if not symbol.endswith("USDT"):
            continue

        last_price = safe_float(item.get("lastPrice"))
        prev_price_1h = safe_float(item.get("prevPrice1h"))
        turnover_24h = safe_float(item.get("turnover24h"), 0.0)

        if last_price is None or last_price <= 0:
            continue

        if turnover_24h is None or turnover_24h <= 0:
            continue

        if prev_price_1h and prev_price_1h > 0:
            change_1h = ((last_price - prev_price_1h) / prev_price_1h) * 100
        else:
            change_1h = 0.0

        candidates.append((symbol, abs(change_1h), turnover_24h))

    candidates.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return [s for s, _, _ in candidates[:TOP_N]]


def pick_live_symbols(symbols):
    selected = list(symbols[:LIVE_WS_SYMBOLS])
    return selected

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
            print(f"No history rows for {symbol} {tf}")
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
            print(f"Loaded history: {symbol} {tf} candles={len(closes)}")

    except Exception as e:
        print(f"History fetch error {symbol} {tf}: {e}")


def refresh_shortlist_and_history():
    global shortlist, live_symbols, last_shortlist_refresh_at

    print("Refreshing shortlist...")
    symbols = build_shortlist_from_tickers()
    print(f"Shortlist size: {len(symbols)}")

    selected_live_symbols = pick_live_symbols(symbols)
    print(f"Live WS symbols: {len(selected_live_symbols)}")

    new_shortlist = set(symbols)
    new_live_symbols = set(selected_live_symbols)

    for symbol in selected_live_symbols:
        ensure_symbol_state(symbol)
        for tf in TIMEFRAMES:
            fetch_history(symbol, tf)
            time.sleep(REQUEST_SLEEP_SECONDS)

    with shortlist_lock:
        shortlist = new_shortlist
        live_symbols = new_live_symbols

    last_shortlist_refresh_at = int(time.time())
    print("Shortlist and history refreshed")

# =========================
# RANKING
# =========================
def get_top_gainers_from_history(tf: str, top_n: int):
    changes = []

    with shortlist_lock:
        current_live = list(live_symbols)

    for symbol in current_live:
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
def process_indicators(symbol: str, tf: str, candle_start: int):
    arr = data.get(symbol, {}).get(tf, [])
    if not arr:
        print(f"No data for {symbol} {tf}")
        return

    close = arr[-1]
    cfg = get_effective_settings(symbol, tf)

    rsi_cfg = cfg.get("rsi", {})
    ema_cfg = cfg.get("ema_distance", {})

    print(f"DEBUG {symbol} {tf} candles={len(arr)} close={close}")

    if rsi_cfg.get("enabled", False):
        rsi_length = int(rsi_cfg.get("length", 14))
        r = rsi(arr, rsi_length)

        if r is not None:
            overbought = float(rsi_cfg.get("overbought", 70))
            oversold = float(rsi_cfg.get("oversold", 30))

            print(f"{symbol} {tf} RSI: {r:.2f} | OB={overbought} OS={oversold}")

            if r >= overbought:
                print(f"RSI overbought condition met: {symbol} {tf}")
                if should_alert_once_per_candle("rsi_overbought", symbol, tf, candle_start):
                    send_telegram(
                        f"🚨 RSI OVERBOUGHT\n"
                        f"Symbol: {symbol}\n"
                        f"Timeframe: {tf}\n"
                        f"RSI: {r:.2f}\n"
                        f"Threshold: {overbought}"
                    )

            if r <= oversold:
                print(f"RSI oversold condition met: {symbol} {tf}")
                if should_alert_once_per_candle("rsi_oversold", symbol, tf, candle_start):
                    send_telegram(
                        f"🚨 RSI OVERSOLD\n"
                        f"Symbol: {symbol}\n"
                        f"Timeframe: {tf}\n"
                        f"RSI: {r:.2f}\n"
                        f"Threshold: {oversold}"
                    )

    if ema_cfg.get("enabled", False):
        ema_length = int(ema_cfg.get("ema_length", 50))
        ema_value = ema(arr, ema_length)

        if ema_value is not None and ema_value != 0:
            dist = ((close - ema_value) / ema_value) * 100
            above_percent = float(ema_cfg.get("above_percent", 0.5))
            below_percent = float(ema_cfg.get("below_percent", -0.5))

            print(
                f"{symbol} {tf} EMA{ema_length} DIST: {dist:.2f}% | "
                f"above={above_percent} below={below_percent}"
            )

            if dist >= above_percent:
                print(f"EMA above condition met: {symbol} {tf}")
                if should_alert_once_per_candle("ema_above", symbol, tf, candle_start):
                    send_telegram(
                        f"📈 PRICE ABOVE EMA{ema_length}\n"
                        f"Symbol: {symbol}\n"
                        f"Timeframe: {tf}\n"
                        f"Distance: {dist:.2f}%\n"
                        f"Threshold: {above_percent}%"
                    )

            if dist <= below_percent:
                print(f"EMA below condition met: {symbol} {tf}")
                if should_alert_once_per_candle("ema_below", symbol, tf, candle_start):
                    send_telegram(
                        f"📉 PRICE BELOW EMA{ema_length}\n"
                        f"Symbol: {symbol}\n"
                        f"Timeframe: {tf}\n"
                        f"Distance: {dist:.2f}%\n"
                        f"Threshold: {below_percent}%"
                    )
        else:
            print(f"Not enough candles for EMA{ema_length}: {symbol} {tf} len={len(arr)}")

# =========================
# LIVE KLINE HANDLER
# =========================
def handle_kline(msg):
    global last_ws_message_at

    try:
        last_ws_message_at = int(time.time())

        if WS_DEBUG:
            print("RAW WS MESSAGE:", msg)

        if not isinstance(msg, dict):
            print("WS skipped: message is not dict")
            return

        topic = str(msg.get("topic", "")).strip()
        data_list = msg.get("data")

        if not isinstance(data_list, list):
            print("WS skipped: no data list", msg)
            return

        topic_symbol = ""
        topic_interval = ""

        if topic.startswith("kline."):
            parts = topic.split(".")
            if len(parts) >= 3:
                topic_interval = normalize_interval(parts[1])
                topic_symbol = parts[2].upper()

        with shortlist_lock:
            current_live_symbols = set(live_symbols)

        for item in data_list:
            if not isinstance(item, dict):
                continue

            symbol = str(item.get("symbol") or topic_symbol or "").upper()
            interval = normalize_interval(item.get("interval") or topic_interval or "")
            close = safe_float(item.get("close"))
            start_time = safe_int(item.get("start"))
            confirm = bool(item.get("confirm", False))

            print(
                f"WS kline -> symbol={symbol} interval={interval} "
                f"close={close} start={start_time} confirm={confirm}"
            )

            if not symbol or not interval or close is None or start_time is None:
                print(
                    f"WS skipped: missing fields | topic={topic} "
                    f"symbol={symbol} interval={interval} close={close} start={start_time}"
                )
                continue

            if symbol not in current_live_symbols:
                print(f"WS skipped: {symbol} not in live_symbols")
                continue

            if interval not in TIMEFRAMES:
                print(f"WS skipped: interval {interval} not in TIMEFRAMES {TIMEFRAMES}")
                continue

            ensure_symbol_state(symbol)
            arr = data[symbol][interval]

            previous_start = current_candle_start[symbol].get(interval)
            new_candle_started = previous_start is None or start_time != previous_start

            if new_candle_started:
                current_candle_start[symbol][interval] = start_time
                arr.append(close)

                if len(arr) > HISTORY_LIMIT:
                    arr.pop(0)

                print(f"New candle started: {symbol} {interval} start={start_time}")
            else:
                if not arr:
                    arr.append(close)
                else:
                    arr[-1] = close

            process_indicators(symbol, interval, start_time)

            if confirm:
                last_closed_candle_start[symbol][interval] = start_time
                print(f"Confirmed candle: {symbol} {interval} start={start_time}")

    except Exception as e:
        print("WebSocket handler error:", e)

# =========================
# WEBSOCKET SUBSCRIPTIONS
# =========================
def stop_all_websockets():
    global ws_connections

    with ws_lock:
        for key, ws in ws_connections.items():
            try:
                if hasattr(ws, "exit"):
                    ws.exit()
                    print(f"Closed WebSocket: {key}")
            except Exception as e:
                print(f"Error closing WebSocket {key}: {e}")

        ws_connections = {}


def start_websocket_for_timeframe(tf: str):
    print(f"Starting WebSocket for timeframe {tf}...")
    ws = WebSocket(testnet=False, channel_type=CATEGORY)

    def callback(msg):
        handle_kline(msg)

    with shortlist_lock:
        symbols = sorted(list(live_symbols))

    if not symbols:
        print(f"No symbols to subscribe for tf={tf}")
        return None

    print(f"Subscribing {len(symbols)} symbols for tf={tf}")

    for i, symbol in enumerate(symbols, start=1):
        try:
            ws.kline_stream(
                interval=int(tf),
                symbol=symbol,
                callback=callback
            )
            print(f"Subscribed live: {symbol} {tf} ({i}/{len(symbols)})")
            time.sleep(0.03)
        except Exception as e:
            print(f"Subscription error {symbol} {tf}: {e}")

    return ws


def restart_websockets():
    global ws_connections

    print("Restarting WebSocket subscriptions...")
    stop_all_websockets()

    new_connections = {}
    for tf in TIMEFRAMES:
        ws = start_websocket_for_timeframe(tf)
        if ws is not None:
            new_connections[tf] = ws

    with ws_lock:
        ws_connections = new_connections

    print("WebSocket subscriptions refreshed")

# =========================
# TELEGRAM COMMANDS
# =========================
def force_test_alerts():
    send_telegram(
        "🚨 FORCE TEST ALERT\n"
        "Symbol: BTCUSDT\n"
        "Timeframe: 5\n"
        "RSI: 72.50\n"
        "Threshold: 70"
    )
    send_telegram(
        "📈 FORCE TEST EMA ALERT\n"
        "Symbol: BTCUSDT\n"
        "Timeframe: 60\n"
        "Distance: 1.23%\n"
        "Threshold: 1.0%"
    )


def get_status_text():
    with shortlist_lock:
        symbols = list(shortlist)
        live = list(live_symbols)

    return (
        f"✅ Bot running\n"
        f"Category: {CATEGORY}\n"
        f"TOP_N: {TOP_N}\n"
        f"LIVE_WS_SYMBOLS: {LIVE_WS_SYMBOLS}\n"
        f"Timeframes: {', '.join(TIMEFRAMES)}\n"
        f"Shortlist size: {len(symbols)}\n"
        f"Live symbols: {len(live)}\n"
        f"History limit: {HISTORY_LIMIT}\n"
        f"Telegram enabled: {TELEGRAM_ENABLED}\n"
        f"Binance enabled: {BINANCE_ENABLED}\n"
        f"Binance symbol: {BINANCE_SYMBOL}\n"
        f"Binance time offset: {binance_time_offset_ms} ms"
    )


def get_symbols_text():
    with shortlist_lock:
        syms = sorted(list(live_symbols))

    preview = syms[:40]
    return (
        f"📊 Live symbol count: {len(syms)}\n"
        f"Sample:\n" + "\n".join(preview)
    )


def get_diag_text():
    now = int(time.time())
    last_ws_age = (now - last_ws_message_at) if last_ws_message_at else -1
    last_refresh_age = (now - last_shortlist_refresh_at) if last_shortlist_refresh_at else -1

    with ws_lock:
        ws_keys = list(ws_connections.keys())

    return (
        f"🛠 DIAG\n"
        f"ws_keys: {ws_keys}\n"
        f"last_ws_message_age_sec: {last_ws_age}\n"
        f"last_shortlist_refresh_age_sec: {last_refresh_age}\n"
        f"WS_DEBUG: {WS_DEBUG}\n"
        f"LIVE_WS_SYMBOLS: {LIVE_WS_SYMBOLS}\n"
        f"BINANCE_ENABLED: {BINANCE_ENABLED}\n"
        f"BINANCE_SYMBOL: {BINANCE_SYMBOL}\n"
        f"BINANCE_TIME_OFFSET_MS: {binance_time_offset_ms}"
    )


def telegram_command_loop():
    global telegram_offset, last_command_time

    if not TELEGRAM_ENABLED:
        print("Telegram command loop disabled")
        return

    print("Telegram command loop started")

    while True:
        try:
            updates = get_telegram_updates(offset=telegram_offset, timeout=20)

            for upd in updates:
                telegram_offset = upd["update_id"] + 1

                message = upd.get("message", {})
                chat = message.get("chat", {})
                text = str(message.get("text", "")).strip()
                from_chat_id = str(chat.get("id", ""))

                if not text:
                    continue

                if CHAT_ID and from_chat_id != str(CHAT_ID):
                    print(f"Ignoring command from unauthorized chat: {from_chat_id}")
                    continue

                print(f"Telegram command received: {text}")

                now = time.time()
                if now - last_command_time < 1:
                    time.sleep(1)
                last_command_time = now

                if text == "/test":
                    send_telegram("✅ Telegram command test OK")

                elif text == "/force":
                    force_test_alerts()

                elif text == "/status":
                    send_telegram(get_status_text())

                elif text == "/symbols":
                    send_telegram(get_symbols_text())

                elif text == "/diag":
                    send_telegram(get_diag_text())

                elif text == "/binance":
                    test_binance_connection()

                elif text == "/help":
                    send_telegram(
                        "Available commands:\n"
                        "/test - Telegram test\n"
                        "/force - force fake alerts\n"
                        "/status - bot status\n"
                        "/symbols - live symbol sample\n"
                        "/diag - websocket diagnostics\n"
                        "/binance - test Binance connection\n"
                        "/help - command list"
                    )

            time.sleep(1)

        except Exception as e:
            print("Telegram command loop error:", e)
            time.sleep(5)

# =========================
# LOOPS
# =========================
def shortlist_refresh_loop():
    while True:
        try:
            refresh_shortlist_and_history()
            restart_websockets()
        except Exception as e:
            print("Shortlist refresh error:", e)

        time.sleep(SHORTLIST_REFRESH_SECONDS)


def scan_loop():
    first_run = True

    while True:
        try:
            with shortlist_lock:
                ready = len(live_symbols) > 0

            if not ready:
                if first_run:
                    print("Waiting for live symbols to finish loading...")
                    first_run = False
                time.sleep(5)
                continue

            top_5m = get_top_gainers_from_history("5", min(LIVE_WS_SYMBOLS, 20))
            top_1h = get_top_gainers_from_history("60", min(LIVE_WS_SYMBOLS, 20))

            print("Top 5m gainers:", top_5m[:10])
            print("Top 1h gainers:", top_1h[:10])

            time.sleep(30)

        except Exception as e:
            print("Scan loop error:", e)
            time.sleep(5)

# =========================
# MAIN
# =========================
def main():
    print("Starting scanner...")
    print("TELEGRAM_ENABLED =", TELEGRAM_ENABLED)
    print("BOT_TOKEN set =", bool(BOT_TOKEN))
    print("CHAT_ID set =", bool(CHAT_ID))
    print("CATEGORY =", CATEGORY)
    print("TOP_N =", TOP_N)
    print("LIVE_WS_SYMBOLS =", LIVE_WS_SYMBOLS)
    print("TIMEFRAMES =", TIMEFRAMES)
    print("WS_DEBUG =", WS_DEBUG)
    print("BINANCE_ENABLED =", BINANCE_ENABLED)
    print("BINANCE_SYMBOL =", BINANCE_SYMBOL)

    send_telegram("🚀 Scanner started")
    send_telegram("✅ Telegram test message")

    test_binance_connection()

    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(target=shortlist_refresh_loop, daemon=True).start()
    threading.Thread(target=telegram_command_loop, daemon=True).start()

    scan_loop()


if __name__ == "__main__":
    main()
