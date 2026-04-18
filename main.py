import os
import time
import math
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
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

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
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "").strip()
BINANCE_BASE_URL = os.getenv("BINANCE_BASE_URL", "https://testnet.binance.vision").rstrip("/")
BINANCE_SYMBOL = os.getenv("BINANCE_SYMBOL", "BTCUSDT").strip().upper()

BINANCE_TRADING_ENABLED = os.getenv("BINANCE_TRADING_ENABLED", "false").lower() == "true"
BINANCE_BUY_QUOTE_QTY = os.getenv("BINANCE_BUY_QUOTE_QTY", "20").strip()

# =========================
# AUTO STRATEGY DEFAULTS
# =========================
DEFAULT_STOP_LOSS_PCT = float(os.getenv("DEFAULT_STOP_LOSS_PCT", "2.0"))
DEFAULT_TAKE_PROFIT_PCT = float(os.getenv("DEFAULT_TAKE_PROFIT_PCT", "3.0"))
DEFAULT_TRAILING_STOP_PCT = float(os.getenv("DEFAULT_TRAILING_STOP_PCT", "0"))
DEFAULT_TRAILING_START_PCT = float(os.getenv("DEFAULT_TRAILING_START_PCT", "0"))
DEFAULT_RSI_BUY = float(os.getenv("DEFAULT_RSI_BUY", "15"))
DEFAULT_RSI_SELL = float(os.getenv("DEFAULT_RSI_SELL", "85"))
DEFAULT_MAX_OPEN_POSITIONS = int(os.getenv("DEFAULT_MAX_OPEN_POSITIONS", "5"))
DEFAULT_SYMBOL_COOLDOWN_SECONDS = int(os.getenv("DEFAULT_SYMBOL_COOLDOWN_SECONDS", "300"))
RISK_CHECK_INTERVAL_SECONDS = float(os.getenv("RISK_CHECK_INTERVAL_SECONDS", "5"))
AUTO_SCAN_INTERVAL_SECONDS = float(os.getenv("AUTO_SCAN_INTERVAL_SECONDS", "10"))

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
    },
    "60": {
        "rsi": {
            "enabled": True,
            "length": 14,
            "overbought": 83,
            "oversold": 17,
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
positions_lock = threading.Lock()
binance_symbols_lock = threading.Lock()

ws_connections = {}
current_candle_start = {}
last_closed_candle_start = {}

telegram_offset = 0
last_command_time = 0
last_ws_message_at = 0
last_shortlist_refresh_at = 0

binance_time_offset_ms = 0
binance_spot_symbols = set()

# =========================
# LIVE AUTO SETTINGS
# =========================
AUTO_TRADING_ENABLED = False
STOP_LOSS_PCT = DEFAULT_STOP_LOSS_PCT
TAKE_PROFIT_PCT = DEFAULT_TAKE_PROFIT_PCT
TRAILING_STOP_PCT = DEFAULT_TRAILING_STOP_PCT
TRAILING_START_PCT = DEFAULT_TRAILING_START_PCT
RSI_BUY_THRESHOLD = DEFAULT_RSI_BUY
RSI_SELL_THRESHOLD = DEFAULT_RSI_SELL
MAX_OPEN_POSITIONS = DEFAULT_MAX_OPEN_POSITIONS
SYMBOL_COOLDOWN_SECONDS = DEFAULT_SYMBOL_COOLDOWN_SECONDS

# positions per symbol
positions = {}
# cooldown per symbol
symbol_cooldowns = {}

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


def pct_text(value: float) -> str:
    return "OFF" if value == 0 else f"{value:.2f}%"

# =========================
# INDICATORS
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

    for i in range(length, len(gains)):
        avg_gain = ((avg_gain * (length - 1)) + gains[i]) / length
        avg_loss = ((avg_loss * (length - 1)) + losses[i]) / length

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# =========================
# BINANCE TESTNET HELPERS
# =========================
def binance_headers():
    return {
        "X-MBX-APIKEY": BINANCE_API_KEY
    }


def sign_binance_query_string(query_string: str) -> str:
    return hmac.new(
        BINANCE_SECRET_KEY.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def binance_public_get(path: str, params=None):
    if params is None:
        params = {}

    url = f"{BINANCE_BASE_URL}{path}"
    response = requests.get(url, params=params, timeout=15)

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

    print("Binance time sync:")
    print(" server_time =", server_time)
    print(" local_time  =", local_time)
    print(" offset_ms   =", binance_time_offset_ms)


def get_binance_timestamp():
    return int(time.time() * 1000) + binance_time_offset_ms


def binance_signed_get(path: str, params=None):
    if params is None:
        params = {}

    params = dict(params)
    params["timestamp"] = get_binance_timestamp()
    params["recvWindow"] = 10000

    query_string = urlencode(params, doseq=True)
    signature = sign_binance_query_string(query_string)
    full_query = f"{query_string}&signature={signature}"

    url = f"{BINANCE_BASE_URL}{path}?{full_query}"

    response = requests.get(
        url,
        headers=binance_headers(),
        timeout=15
    )

    try:
        payload = response.json()
    except Exception:
        payload = {"raw_text": response.text}

    if not response.ok:
        raise Exception(f"Binance signed GET failed {response.status_code}: {payload}")

    return payload


def binance_signed_post(path: str, params=None):
    if params is None:
        params = {}

    params = dict(params)
    params["timestamp"] = get_binance_timestamp()
    params["recvWindow"] = 10000

    query_string = urlencode(params, doseq=True)
    signature = sign_binance_query_string(query_string)
    full_query = f"{query_string}&signature={signature}"

    url = f"{BINANCE_BASE_URL}{path}?{full_query}"

    response = requests.post(
        url,
        headers=binance_headers(),
        timeout=15
    )

    try:
        payload = response.json()
    except Exception:
        payload = {"raw_text": response.text}

    if not response.ok:
        raise Exception(f"Binance signed POST failed {response.status_code}: {payload}")

    return payload


def get_binance_account():
    sync_binance_time()
    return binance_signed_get("/api/v3/account")


def get_binance_nonzero_balances_text():
    account = get_binance_account()
    balances = account.get("balances", [])

    rows = []
    for b in balances:
        free_amt = safe_float(b.get("free"), 0.0)
        locked_amt = safe_float(b.get("locked"), 0.0)
        if free_amt > 0 or locked_amt > 0:
            rows.append(f"{b.get('asset')}: free={free_amt}, locked={locked_amt}")

    if not rows:
        return "No non-zero Binance testnet balances found."

    return "💰 Binance balances\n" + "\n".join(rows[:20])


def refresh_binance_spot_symbols():
    global binance_spot_symbols

    try:
        info = binance_public_get("/api/v3/exchangeInfo")
        symbols = info.get("symbols", [])
        valid = set()

        for s in symbols:
            if s.get("status") != "TRADING":
                continue
            if not s.get("isSpotTradingAllowed", False):
                continue
            symbol_name = str(s.get("symbol", "")).upper()
            if symbol_name.endswith("USDT"):
                valid.add(symbol_name)

        with binance_symbols_lock:
            binance_spot_symbols = valid

        print(f"Loaded Binance tradable spot symbols: {len(valid)}")
    except Exception as e:
        print("Failed to refresh Binance spot symbols:", e)


def is_symbol_supported_on_binance(symbol: str) -> bool:
    with binance_symbols_lock:
        return symbol in binance_spot_symbols


def get_binance_symbol_info(symbol: str):
    info = binance_public_get("/api/v3/exchangeInfo", {"symbol": symbol})
    symbols = info.get("symbols", [])
    if not symbols:
        raise Exception(f"No exchange info found for {symbol}")
    return symbols[0]


def get_filter_value(filters, filter_type, key):
    for f in filters:
        if f.get("filterType") == filter_type:
            return f.get(key)
    return None


def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


def format_quantity(qty: float, step_size_str: str) -> str:
    if "." in step_size_str:
        trimmed = step_size_str.rstrip("0")
        decimals = len(trimmed.split(".")[1]) if "." in trimmed else 0
    else:
        decimals = 0
    formatted = f"{qty:.{decimals}f}"
    return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted


def get_sellable_base_asset_balance(symbol: str) -> float:
    if not symbol.endswith("USDT"):
        return 0.0

    base_asset = symbol[:-4]
    account = binance_signed_get("/api/v3/account")
    balances = account.get("balances", [])

    for b in balances:
        if b.get("asset") == base_asset:
            return safe_float(b.get("free"), 0.0)

    return 0.0


def get_binance_last_price(symbol: str) -> float:
    ticker = binance_public_get("/api/v3/ticker/price", {"symbol": symbol})
    price = safe_float(ticker.get("price"))
    if price is None or price <= 0:
        raise Exception(f"Could not read valid last price for {symbol}")
    return price


def compute_average_fill_price(order: dict):
    executed_qty = safe_float(order.get("executedQty"), 0.0)
    cummulative_quote_qty = safe_float(order.get("cummulativeQuoteQty"), 0.0)

    if executed_qty and executed_qty > 0 and cummulative_quote_qty and cummulative_quote_qty > 0:
        return cummulative_quote_qty / executed_qty

    fills = order.get("fills", [])
    if fills:
        total_qty = 0.0
        total_quote = 0.0
        for f in fills:
            p = safe_float(f.get("price"), 0.0)
            q = safe_float(f.get("qty"), 0.0)
            total_qty += q
            total_quote += p * q
        if total_qty > 0:
            return total_quote / total_qty

    return None

# =========================
# POSITION / AUTO STATE
# =========================
def get_open_positions_count():
    with positions_lock:
        return len(positions)


def symbol_in_position(symbol: str) -> bool:
    with positions_lock:
        return symbol in positions


def set_symbol_cooldown(symbol: str):
    symbol_cooldowns[symbol] = int(time.time())


def symbol_on_cooldown(symbol: str) -> bool:
    last = symbol_cooldowns.get(symbol)
    if not last:
        return False
    return (time.time() - last) < SYMBOL_COOLDOWN_SECONDS


def compute_risk_prices(entry_price: float):
    stop_loss_price = None if STOP_LOSS_PCT == 0 else entry_price * (1 - (STOP_LOSS_PCT / 100.0))
    take_profit_price = None if TAKE_PROFIT_PCT == 0 else entry_price * (1 + (TAKE_PROFIT_PCT / 100.0))
    return stop_loss_price, take_profit_price


def compute_trailing_stop_price(highest_price: float):
    if TRAILING_STOP_PCT == 0 or highest_price is None:
        return None
    return highest_price * (1 - (TRAILING_STOP_PCT / 100.0))


def trailing_should_be_active(entry_price: float, highest_price: float) -> bool:
    if TRAILING_STOP_PCT == 0:
        return False
    if TRAILING_START_PCT == 0:
        return True
    activation_price = entry_price * (1 + (TRAILING_START_PCT / 100.0))
    return highest_price >= activation_price


def set_position(symbol: str, entry_price: float, quantity: float, source: str):
    stop_loss_price, take_profit_price = compute_risk_prices(entry_price)

    trailing_active = trailing_should_be_active(entry_price, entry_price)
    trailing_stop_price = compute_trailing_stop_price(entry_price) if trailing_active else None

    with positions_lock:
        positions[symbol] = {
            "symbol": symbol,
            "entry_price": entry_price,
            "quantity": quantity,
            "stop_loss_price": stop_loss_price,
            "take_profit_price": take_profit_price,
            "highest_price": entry_price,
            "trailing_stop_price": trailing_stop_price,
            "trailing_active": trailing_active,
            "source": source,
            "opened_at": int(time.time()),
        }


def remove_position(symbol: str):
    with positions_lock:
        if symbol in positions:
            del positions[symbol]
    set_symbol_cooldown(symbol)


def refresh_all_position_risk_levels():
    with positions_lock:
        for symbol, pos in positions.items():
            entry = pos["entry_price"]
            stop_loss_price, take_profit_price = compute_risk_prices(entry)
            pos["stop_loss_price"] = stop_loss_price
            pos["take_profit_price"] = take_profit_price

            highest = pos.get("highest_price")
            active = trailing_should_be_active(entry, highest)
            pos["trailing_active"] = active
            pos["trailing_stop_price"] = compute_trailing_stop_price(highest) if active else None


def update_position_high_water(symbol: str, current_price: float):
    with positions_lock:
        pos = positions.get(symbol)
        if not pos:
            return

        previous_high = pos.get("highest_price")
        if previous_high is None or current_price > previous_high:
            pos["highest_price"] = current_price

        active = trailing_should_be_active(pos["entry_price"], pos["highest_price"])
        pos["trailing_active"] = active
        pos["trailing_stop_price"] = compute_trailing_stop_price(pos["highest_price"]) if active else None


def get_positions_text():
    with positions_lock:
        if not positions:
            return "📭 No open tracked positions."

        lines = ["📦 Open tracked positions"]
        for symbol, pos in positions.items():
            sl_text = "OFF" if pos["stop_loss_price"] is None else f"{pos['stop_loss_price']:.8f}"
            tp_text = "OFF" if pos["take_profit_price"] is None else f"{pos['take_profit_price']:.8f}"
            ts_text = "OFF" if pos.get("trailing_stop_price") is None else f"{pos['trailing_stop_price']:.8f}"
            hp_text = "N/A" if pos.get("highest_price") is None else f"{pos['highest_price']:.8f}"
            ta_text = "ON" if pos.get("trailing_active") else "OFF"
            lines.append(
                f"{symbol} | entry={pos['entry_price']:.8f} | qty={pos['quantity']} | "
                f"sl={sl_text} | tp={tp_text} | high={hp_text} | tsl={ts_text} | ta={ta_text}"
            )
        return "\n".join(lines[:30])


def get_strategy_text():
    return (
        f"🤖 Auto strategy\n"
        f"Auto trading: {AUTO_TRADING_ENABLED}\n"
        f"Buy rule: 5m RSI <= {RSI_BUY_THRESHOLD:.2f}\n"
        f"Sell rule: 5m RSI >= {RSI_SELL_THRESHOLD:.2f} OR SL/TP/Trailing\n"
        f"Stop loss: {pct_text(STOP_LOSS_PCT)}\n"
        f"Take profit: {pct_text(TAKE_PROFIT_PCT)}\n"
        f"Trailing stop: {pct_text(TRAILING_STOP_PCT)}\n"
        f"Trailing activation: {pct_text(TRAILING_START_PCT)}\n"
        f"Max open positions: {MAX_OPEN_POSITIONS}\n"
        f"Symbol cooldown: {SYMBOL_COOLDOWN_SECONDS}s\n"
        f"Buy quote qty per trade: {BINANCE_BUY_QUOTE_QTY}\n"
        f"Current open positions: {get_open_positions_count()}"
    )

# =========================
# BINANCE CONNECTION / ORDERS
# =========================
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

        refresh_binance_spot_symbols()

        send_telegram(
            f"✅ Binance Testnet connected\n"
            f"Symbol: {BINANCE_SYMBOL}\n"
            f"Offset: {binance_time_offset_ms} ms"
        )

    except Exception as e:
        print("Binance testnet connection failed:", e)
        send_telegram(f"❌ Binance Testnet connection failed\n{e}")


def place_binance_market_buy(symbol: str):
    if not BINANCE_ENABLED:
        raise Exception("BINANCE_ENABLED is false")

    if not BINANCE_TRADING_ENABLED:
        raise Exception("BINANCE_TRADING_ENABLED is false")

    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        raise Exception("Binance keys missing")

    if symbol_in_position(symbol):
        raise Exception(f"Already in position for {symbol}")

    if get_open_positions_count() >= MAX_OPEN_POSITIONS:
        raise Exception("Max open positions reached")

    sync_binance_time()

    params = {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quoteOrderQty": BINANCE_BUY_QUOTE_QTY,
    }

    order = binance_signed_post("/api/v3/order", params)

    avg_price = compute_average_fill_price(order)
    qty = safe_float(order.get("executedQty"), 0.0)

    if avg_price and qty and qty > 0:
        set_position(symbol, avg_price, qty, "buy_fill")
    else:
        fallback_price = get_binance_last_price(symbol)
        fallback_qty = get_sellable_base_asset_balance(symbol)
        if fallback_qty > 0:
            set_position(symbol, fallback_price, fallback_qty, "buy_fallback_market")
        else:
            raise Exception(f"Buy filled but could not determine resulting balance for {symbol}")

    return order


def place_binance_market_sell(symbol: str):
    if not BINANCE_ENABLED:
        raise Exception("BINANCE_ENABLED is false")

    if not BINANCE_TRADING_ENABLED:
        raise Exception("BINANCE_TRADING_ENABLED is false")

    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        raise Exception("Binance keys missing")

    sync_binance_time()

    symbol_info = get_binance_symbol_info(symbol)
    filters = symbol_info.get("filters", [])

    step_size_str = get_filter_value(filters, "LOT_SIZE", "stepSize")
    min_qty_str = get_filter_value(filters, "LOT_SIZE", "minQty")

    if not step_size_str or not min_qty_str:
        raise Exception(f"Could not read LOT_SIZE filters for {symbol}")

    step_size = float(step_size_str)
    min_qty = float(min_qty_str)

    base_balance = get_sellable_base_asset_balance(symbol)

    if base_balance <= 0:
        raise Exception(f"No base asset balance to sell for {symbol}")

    sell_qty = floor_to_step(base_balance, step_size)

    if sell_qty < min_qty or sell_qty <= 0:
        raise Exception(
            f"Sell quantity too small after rounding. balance={base_balance}, "
            f"rounded={sell_qty}, minQty={min_qty}"
        )

    quantity_str = format_quantity(sell_qty, step_size_str)

    params = {
        "symbol": symbol,
        "side": "SELL",
        "type": "MARKET",
        "quantity": quantity_str,
    }

    order = binance_signed_post("/api/v3/order", params)
    remove_position(symbol)
    return order

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
# SIGNALS / DIAGNOSTICS
# =========================
def process_indicators(symbol: str, tf: str, candle_start: int):
    arr = data.get(symbol, {}).get(tf, [])
    if not arr:
        return

    cfg = get_effective_settings(symbol, tf)
    rsi_cfg = cfg.get("rsi", {})

    if rsi_cfg.get("enabled", False):
        rsi_length = int(rsi_cfg.get("length", 14))
        r = rsi(arr, rsi_length)

        if r is not None:
            overbought = float(rsi_cfg.get("overbought", 70))
            oversold = float(rsi_cfg.get("oversold", 30))

            if r >= overbought:
                if should_alert_once_per_candle("rsi_overbought", symbol, tf, candle_start):
                    send_telegram(
                        f"🚨 RSI OVERBOUGHT\n"
                        f"Symbol: {symbol}\n"
                        f"Timeframe: {tf}\n"
                        f"RSI: {r:.2f}\n"
                        f"Threshold: {overbought}"
                    )

            if r <= oversold:
                if should_alert_once_per_candle("rsi_oversold", symbol, tf, candle_start):
                    send_telegram(
                        f"🚨 RSI OVERSOLD\n"
                        f"Symbol: {symbol}\n"
                        f"Timeframe: {tf}\n"
                        f"RSI: {r:.2f}\n"
                        f"Threshold: {oversold}"
                    )

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
            return

        topic = str(msg.get("topic", "")).strip()
        data_list = msg.get("data")

        if not isinstance(data_list, list):
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

            if not symbol or not interval or close is None or start_time is None:
                continue

            if symbol not in current_live_symbols:
                continue

            if interval not in TIMEFRAMES:
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
            else:
                if not arr:
                    arr.append(close)
                else:
                    arr[-1] = close

            process_indicators(symbol, interval, start_time)

            if confirm:
                last_closed_candle_start[symbol][interval] = start_time

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
# AUTO TRADING
# =========================
def get_symbol_rsi(symbol: str, tf: str = "5", length: int = 14):
    arr = data.get(symbol, {}).get(tf, [])
    if len(arr) < length + 1:
        return None
    return rsi(arr, length)


def auto_entry_loop():
    while True:
        try:
            if not AUTO_TRADING_ENABLED:
                time.sleep(AUTO_SCAN_INTERVAL_SECONDS)
                continue

            if not BINANCE_ENABLED or not BINANCE_TRADING_ENABLED:
                time.sleep(AUTO_SCAN_INTERVAL_SECONDS)
                continue

            if get_open_positions_count() >= MAX_OPEN_POSITIONS:
                time.sleep(AUTO_SCAN_INTERVAL_SECONDS)
                continue

            with shortlist_lock:
                candidates = list(live_symbols)

            if not candidates:
                time.sleep(AUTO_SCAN_INTERVAL_SECONDS)
                continue

            symbol_rsi_list = []
            for symbol in candidates:
                if not symbol.endswith("USDT"):
                    continue
                if symbol_in_position(symbol):
                    continue
                if symbol_on_cooldown(symbol):
                    continue
                if not is_symbol_supported_on_binance(symbol):
                    continue

                value = get_symbol_rsi(symbol, "5", 14)
                if value is None:
                    continue

                if value <= RSI_BUY_THRESHOLD:
                    symbol_rsi_list.append((symbol, value))

            symbol_rsi_list.sort(key=lambda x: x[1])

            for symbol, value in symbol_rsi_list:
                if get_open_positions_count() >= MAX_OPEN_POSITIONS:
                    break

                if symbol_in_position(symbol):
                    continue

                try:
                    order = place_binance_market_buy(symbol)
                    avg_price = compute_average_fill_price(order)
                    msg = (
                        f"🟢 AUTO BUY\n"
                        f"Symbol: {symbol}\n"
                        f"RSI(5m): {value:.2f}\n"
                        f"Order ID: {order.get('orderId')}\n"
                        f"Status: {order.get('status')}"
                    )

                    with positions_lock:
                        pos = positions.get(symbol)
                        if pos:
                            sl_text = "OFF" if pos["stop_loss_price"] is None else f"{pos['stop_loss_price']:.8f}"
                            tp_text = "OFF" if pos["take_profit_price"] is None else f"{pos['take_profit_price']:.8f}"
                            ts_text = "OFF" if pos["trailing_stop_price"] is None else f"{pos['trailing_stop_price']:.8f}"
                            msg += (
                                f"\nEntry: {pos['entry_price']:.8f}"
                                f"\nStop loss: {sl_text}"
                                f"\nTake profit: {tp_text}"
                                f"\nTrailing stop: {ts_text}"
                            )

                    if avg_price:
                        msg += f"\nAvg fill: {avg_price:.8f}"

                    send_telegram(msg)
                    time.sleep(1.0)
                except Exception as e:
                    print(f"Auto buy failed for {symbol}: {e}")

        except Exception as e:
            print("Auto entry loop error:", e)

        time.sleep(AUTO_SCAN_INTERVAL_SECONDS)


def auto_exit_loop():
    while True:
        try:
            with positions_lock:
                current_positions = list(positions.items())

            if not current_positions:
                time.sleep(RISK_CHECK_INTERVAL_SECONDS)
                continue

            for symbol, pos in current_positions:
                try:
                    current_price = get_binance_last_price(symbol)
                    update_position_high_water(symbol, current_price)

                    with positions_lock:
                        refreshed = positions.get(symbol)
                        if not refreshed:
                            continue
                        stop_loss_price = refreshed["stop_loss_price"]
                        take_profit_price = refreshed["take_profit_price"]
                        trailing_stop_price = refreshed.get("trailing_stop_price")
                        highest_price = refreshed.get("highest_price")
                        trailing_active = refreshed.get("trailing_active", False)
                        entry_price = refreshed["entry_price"]
                        qty = refreshed["quantity"]

                    current_rsi = get_symbol_rsi(symbol, "5", 14)

                    print(
                        f"AUTO EXIT CHECK -> {symbol} current={current_price} "
                        f"entry={entry_price} sl={stop_loss_price} tp={take_profit_price} "
                        f"high={highest_price} trailing_active={trailing_active} "
                        f"tsl={trailing_stop_price} rsi={current_rsi}"
                    )

                    if stop_loss_price is not None and current_price <= stop_loss_price:
                        order = place_binance_market_sell(symbol)
                        send_telegram(
                            f"🛑 STOP LOSS HIT\n"
                            f"Symbol: {symbol}\n"
                            f"Entry: {entry_price:.8f}\n"
                            f"Exit trigger: {current_price:.8f}\n"
                            f"Tracked qty: {qty}\n"
                            f"Order ID: {order.get('orderId')}\n"
                            f"Status: {order.get('status')}"
                        )
                        continue

                    if take_profit_price is not None and current_price >= take_profit_price:
                        order = place_binance_market_sell(symbol)
                        send_telegram(
                            f"🎯 TAKE PROFIT HIT\n"
                            f"Symbol: {symbol}\n"
                            f"Entry: {entry_price:.8f}\n"
                            f"Exit trigger: {current_price:.8f}\n"
                            f"Tracked qty: {qty}\n"
                            f"Order ID: {order.get('orderId')}\n"
                            f"Status: {order.get('status')}"
                        )
                        continue

                    if trailing_active and trailing_stop_price is not None and current_price <= trailing_stop_price:
                        order = place_binance_market_sell(symbol)
                        send_telegram(
                            f"🔒 TRAILING STOP HIT\n"
                            f"Symbol: {symbol}\n"
                            f"Entry: {entry_price:.8f}\n"
                            f"Highest: {highest_price:.8f}\n"
                            f"Trailing stop: {trailing_stop_price:.8f}\n"
                            f"Exit trigger: {current_price:.8f}\n"
                            f"Tracked qty: {qty}\n"
                            f"Order ID: {order.get('orderId')}\n"
                            f"Status: {order.get('status')}"
                        )
                        continue

                    if current_rsi is not None and current_rsi >= RSI_SELL_THRESHOLD:
                        order = place_binance_market_sell(symbol)
                        send_telegram(
                            f"🔴 AUTO SELL (RSI EXIT)\n"
                            f"Symbol: {symbol}\n"
                            f"RSI(5m): {current_rsi:.2f}\n"
                            f"Entry: {entry_price:.8f}\n"
                            f"Exit trigger: {current_price:.8f}\n"
                            f"Tracked qty: {qty}\n"
                            f"Order ID: {order.get('orderId')}\n"
                            f"Status: {order.get('status')}"
                        )
                        continue

                except Exception as e:
                    print(f"Auto exit failed for {symbol}: {e}")

        except Exception as e:
            print("Auto exit loop error:", e)

        time.sleep(RISK_CHECK_INTERVAL_SECONDS)

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
        f"Binance trading enabled: {BINANCE_TRADING_ENABLED}\n"
        f"Auto trading enabled: {AUTO_TRADING_ENABLED}\n"
        f"Current open positions: {get_open_positions_count()}\n"
        f"Max open positions: {MAX_OPEN_POSITIONS}\n"
        f"Buy RSI <= {RSI_BUY_THRESHOLD:.2f}\n"
        f"Sell RSI >= {RSI_SELL_THRESHOLD:.2f}\n"
        f"SL: {pct_text(STOP_LOSS_PCT)}\n"
        f"TP: {pct_text(TAKE_PROFIT_PCT)}\n"
        f"Trailing: {pct_text(TRAILING_STOP_PCT)}\n"
        f"Trail start: {pct_text(TRAILING_START_PCT)}\n"
        f"Binance time offset: {binance_time_offset_ms} ms"
    )


def get_symbols_text():
    with shortlist_lock:
        syms = sorted(list(live_symbols))

    preview = syms[:40]
    return f"📊 Live symbol count: {len(syms)}\nSample:\n" + "\n".join(preview)


def get_diag_text():
    now = int(time.time())
    last_ws_age = (now - last_ws_message_at) if last_ws_message_at else -1
    last_refresh_age = (now - last_shortlist_refresh_at) if last_shortlist_refresh_at else -1

    with ws_lock:
        ws_keys = list(ws_connections.keys())

    with binance_symbols_lock:
        binance_count = len(binance_spot_symbols)

    return (
        f"🛠 DIAG\n"
        f"ws_keys: {ws_keys}\n"
        f"last_ws_message_age_sec: {last_ws_age}\n"
        f"last_shortlist_refresh_age_sec: {last_refresh_age}\n"
        f"WS_DEBUG: {WS_DEBUG}\n"
        f"LIVE_WS_SYMBOLS: {LIVE_WS_SYMBOLS}\n"
        f"BINANCE_ENABLED: {BINANCE_ENABLED}\n"
        f"BINANCE_TRADING_ENABLED: {BINANCE_TRADING_ENABLED}\n"
        f"AUTO_TRADING_ENABLED: {AUTO_TRADING_ENABLED}\n"
        f"RSI_BUY_THRESHOLD: {RSI_BUY_THRESHOLD}\n"
        f"RSI_SELL_THRESHOLD: {RSI_SELL_THRESHOLD}\n"
        f"STOP_LOSS_PCT: {STOP_LOSS_PCT}\n"
        f"TAKE_PROFIT_PCT: {TAKE_PROFIT_PCT}\n"
        f"TRAILING_STOP_PCT: {TRAILING_STOP_PCT}\n"
        f"TRAILING_START_PCT: {TRAILING_START_PCT}\n"
        f"MAX_OPEN_POSITIONS: {MAX_OPEN_POSITIONS}\n"
        f"SUPPORTED_BINANCE_USDT_SYMBOLS: {binance_count}\n"
        f"BINANCE_TIME_OFFSET_MS: {binance_time_offset_ms}"
    )


def telegram_command_loop():
    global telegram_offset, last_command_time
    global AUTO_TRADING_ENABLED, STOP_LOSS_PCT, TAKE_PROFIT_PCT
    global TRAILING_STOP_PCT, TRAILING_START_PCT
    global RSI_BUY_THRESHOLD, RSI_SELL_THRESHOLD, MAX_OPEN_POSITIONS, SYMBOL_COOLDOWN_SECONDS

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

                elif text == "/bbal":
                    try:
                        send_telegram(get_binance_nonzero_balances_text())
                    except Exception as e:
                        send_telegram(f"❌ Binance balance check failed\n{e}")

                elif text == "/buy":
                    try:
                        order = place_binance_market_buy(BINANCE_SYMBOL)
                        avg_price = compute_average_fill_price(order)
                        msg = (
                            f"✅ Binance TESTNET BUY placed\n"
                            f"Symbol: {order.get('symbol')}\n"
                            f"Order ID: {order.get('orderId')}\n"
                            f"Status: {order.get('status')}\n"
                            f"Side: {order.get('side')}\n"
                            f"Type: {order.get('type')}"
                        )
                        if avg_price:
                            msg += f"\nAvg entry: {avg_price:.8f}"
                        send_telegram(msg)
                    except Exception as e:
                        send_telegram(f"❌ Binance TESTNET BUY failed\n{e}")

                elif text == "/sell":
                    try:
                        order = place_binance_market_sell(BINANCE_SYMBOL)
                        send_telegram(
                            f"✅ Binance TESTNET SELL placed\n"
                            f"Symbol: {order.get('symbol')}\n"
                            f"Order ID: {order.get('orderId')}\n"
                            f"Status: {order.get('status')}\n"
                            f"Side: {order.get('side')}\n"
                            f"Type: {order.get('type')}"
                        )
                    except Exception as e:
                        send_telegram(f"❌ Binance TESTNET SELL failed\n{e}")

                elif text == "/autoon":
                    AUTO_TRADING_ENABLED = True
                    send_telegram("✅ Auto trading enabled")

                elif text == "/autooff":
                    AUTO_TRADING_ENABLED = False
                    send_telegram("⏸ Auto trading disabled")

                elif text == "/showstrategy":
                    send_telegram(get_strategy_text())

                elif text == "/showpositions":
                    send_telegram(get_positions_text())

                elif text.startswith("/setrsibuy "):
                    try:
                        value = float(text.split(maxsplit=1)[1].strip())
                        RSI_BUY_THRESHOLD = value
                        send_telegram(f"✅ Buy RSI threshold updated to {RSI_BUY_THRESHOLD:.2f}")
                    except Exception as e:
                        send_telegram(f"❌ Failed to set buy RSI threshold\n{e}")

                elif text.startswith("/setrsisell "):
                    try:
                        value = float(text.split(maxsplit=1)[1].strip())
                        RSI_SELL_THRESHOLD = value
                        send_telegram(f"✅ Sell RSI threshold updated to {RSI_SELL_THRESHOLD:.2f}")
                    except Exception as e:
                        send_telegram(f"❌ Failed to set sell RSI threshold\n{e}")

                elif text.startswith("/setsl "):
                    try:
                        value = float(text.split(maxsplit=1)[1].strip())
                        if value < 0:
                            raise Exception("Stop loss cannot be negative")
                        STOP_LOSS_PCT = value
                        refresh_all_position_risk_levels()
                        send_telegram(
                            f"✅ Stop loss updated\n"
                            f"Stop loss: {pct_text(STOP_LOSS_PCT)}\n"
                            f"Take profit: {pct_text(TAKE_PROFIT_PCT)}\n"
                            f"Trailing stop: {pct_text(TRAILING_STOP_PCT)}\n"
                            f"Trail start: {pct_text(TRAILING_START_PCT)}"
                        )
                    except Exception as e:
                        send_telegram(f"❌ Failed to set stop loss\n{e}")

                elif text.startswith("/settp "):
                    try:
                        value = float(text.split(maxsplit=1)[1].strip())
                        if value < 0:
                            raise Exception("Take profit cannot be negative")
                        TAKE_PROFIT_PCT = value
                        refresh_all_position_risk_levels()
                        send_telegram(
                            f"✅ Take profit updated\n"
                            f"Stop loss: {pct_text(STOP_LOSS_PCT)}\n"
                            f"Take profit: {pct_text(TAKE_PROFIT_PCT)}\n"
                            f"Trailing stop: {pct_text(TRAILING_STOP_PCT)}\n"
                            f"Trail start: {pct_text(TRAILING_START_PCT)}"
                        )
                    except Exception as e:
                        send_telegram(f"❌ Failed to set take profit\n{e}")

                elif text.startswith("/settrailing "):
                    try:
                        value = float(text.split(maxsplit=1)[1].strip())
                        if value < 0:
                            raise Exception("Trailing stop cannot be negative")
                        TRAILING_STOP_PCT = value
                        refresh_all_position_risk_levels()
                        send_telegram(
                            f"✅ Trailing stop updated\n"
                            f"Stop loss: {pct_text(STOP_LOSS_PCT)}\n"
                            f"Take profit: {pct_text(TAKE_PROFIT_PCT)}\n"
                            f"Trailing stop: {pct_text(TRAILING_STOP_PCT)}\n"
                            f"Trail start: {pct_text(TRAILING_START_PCT)}"
                        )
                    except Exception as e:
                        send_telegram(f"❌ Failed to set trailing stop\n{e}")

                elif text.startswith("/settrailstart "):
                    try:
                        value = float(text.split(maxsplit=1)[1].strip())
                        if value < 0:
                            raise Exception("Trailing activation cannot be negative")
                        TRAILING_START_PCT = value
                        refresh_all_position_risk_levels()
                        send_telegram(
                            f"✅ Trailing activation updated\n"
                            f"Stop loss: {pct_text(STOP_LOSS_PCT)}\n"
                            f"Take profit: {pct_text(TAKE_PROFIT_PCT)}\n"
                            f"Trailing stop: {pct_text(TRAILING_STOP_PCT)}\n"
                            f"Trail start: {pct_text(TRAILING_START_PCT)}"
                        )
                    except Exception as e:
                        send_telegram(f"❌ Failed to set trailing activation\n{e}")

                elif text.startswith("/setmaxcoins "):
                    try:
                        value = int(text.split(maxsplit=1)[1].strip())
                        if value <= 0:
                            raise Exception("Max coins must be greater than 0")
                        MAX_OPEN_POSITIONS = value
                        send_telegram(f"✅ Max open positions updated to {MAX_OPEN_POSITIONS}")
                    except Exception as e:
                        send_telegram(f"❌ Failed to set max coins\n{e}")

                elif text.startswith("/setcooldown "):
                    try:
                        value = int(text.split(maxsplit=1)[1].strip())
                        if value < 0:
                            raise Exception("Cooldown cannot be negative")
                        SYMBOL_COOLDOWN_SECONDS = value
                        send_telegram(f"✅ Symbol cooldown updated to {SYMBOL_COOLDOWN_SECONDS} seconds")
                    except Exception as e:
                        send_telegram(f"❌ Failed to set cooldown\n{e}")

                elif text == "/help":
                    send_telegram(
                        "Available commands:\n"
                        "/test - Telegram test\n"
                        "/force - force fake alerts\n"
                        "/status - bot status\n"
                        "/symbols - live symbol sample\n"
                        "/diag - websocket diagnostics\n"
                        "/binance - test Binance connection\n"
                        "/bbal - Binance balances\n"
                        "/buy - manual Binance testnet market buy for BINANCE_SYMBOL\n"
                        "/sell - manual Binance testnet market sell for BINANCE_SYMBOL\n"
                        "/autoon - enable auto trading\n"
                        "/autooff - disable auto trading\n"
                        "/showstrategy - show auto strategy\n"
                        "/showpositions - show tracked positions\n"
                        "/setrsibuy 15 - set buy threshold\n"
                        "/setrsisell 85 - set sell threshold\n"
                        "/setsl 0 - set stop loss percent (0 = OFF)\n"
                        "/settp 3 - set take profit percent (0 = OFF)\n"
                        "/settrailing 2 - set trailing stop percent (0 = OFF)\n"
                        "/settrailstart 1 - set trailing activation percent (0 = immediate)\n"
                        "/setmaxcoins 5 - set max simultaneous open positions\n"
                        "/setcooldown 300 - set symbol cooldown seconds\n"
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
            refresh_binance_spot_symbols()
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
    print("BINANCE_TRADING_ENABLED =", BINANCE_TRADING_ENABLED)
    print("BINANCE_SYMBOL =", BINANCE_SYMBOL)
    print("BINANCE_BASE_URL =", BINANCE_BASE_URL)
    print("BINANCE_BUY_QUOTE_QTY =", BINANCE_BUY_QUOTE_QTY)
    print("DEFAULT_STOP_LOSS_PCT =", DEFAULT_STOP_LOSS_PCT)
    print("DEFAULT_TAKE_PROFIT_PCT =", DEFAULT_TAKE_PROFIT_PCT)
    print("DEFAULT_TRAILING_STOP_PCT =", DEFAULT_TRAILING_STOP_PCT)
    print("DEFAULT_TRAILING_START_PCT =", DEFAULT_TRAILING_START_PCT)
    print("DEFAULT_RSI_BUY =", DEFAULT_RSI_BUY)
    print("DEFAULT_RSI_SELL =", DEFAULT_RSI_SELL)
    print("DEFAULT_MAX_OPEN_POSITIONS =", DEFAULT_MAX_OPEN_POSITIONS)

    send_telegram("🚀 Scanner started")
    send_telegram("✅ Telegram test message")

    test_binance_connection()

    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(target=shortlist_refresh_loop, daemon=True).start()
    threading.Thread(target=telegram_command_loop, daemon=True).start()
    threading.Thread(target=auto_entry_loop, daemon=True).start()
    threading.Thread(target=auto_exit_loop, daemon=True).start()

    scan_loop()


if __name__ == "__main__":
    main()
