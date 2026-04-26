import os
import time
import math
import queue
import threading
import traceback
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, InvalidOperation

import requests
from pybit.unified_trading import HTTP, WebSocket

# =========================================================
# CONFIG
# =========================================================
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GROUP_CHAT_ID = os.getenv("TELEGRAM_GROUP_CHAT_ID", "").strip()

CATEGORY = os.getenv("BYBIT_CATEGORY", "linear").strip().lower()
TOP_N = int(os.getenv("TOP_N", "150"))
LIVE_WS_SYMBOLS = int(os.getenv("LIVE_WS_SYMBOLS", "150"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "160"))
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "300"))
SHORTLIST_REFRESH_SECONDS = int(os.getenv("SHORTLIST_REFRESH_SECONDS", "60"))
REQUEST_SLEEP_SECONDS = float(os.getenv("REQUEST_SLEEP_SECONDS", "0.02"))
HEARTBEAT_TO_TELEGRAM = os.getenv("HEARTBEAT_TO_TELEGRAM", "false").lower() == "true"
WS_DEBUG = os.getenv("WS_DEBUG", "false").lower() == "true"
TIMEFRAMES = [x.strip() for x in os.getenv("TIMEFRAMES", "5").split(",") if x.strip()]

# =========================================================
# BYBIT MAINNET CONFIG
# =========================================================
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "").strip()
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "").strip()
BYBIT_TRADING_ENABLED = os.getenv("BYBIT_TRADING_ENABLED", "false").lower() == "true"
BYBIT_SYMBOL = os.getenv("BYBIT_SYMBOL", "BTCUSDT").strip().upper()
BYBIT_TRADE_SIZE_USDT = float(os.getenv("BYBIT_TRADE_SIZE_USDT", "20"))
BYBIT_LEVERAGE = int(os.getenv("BYBIT_LEVERAGE", "1"))

# =========================================================
# AUTO TRADING CONFIG
# =========================================================
DEFAULT_STOP_LOSS_PCT = float(os.getenv("DEFAULT_STOP_LOSS_PCT", "2.0"))
DEFAULT_TAKE_PROFIT_PCT = float(os.getenv("DEFAULT_TAKE_PROFIT_PCT", "3.0"))
DEFAULT_TRAILING_STOP_PCT = float(os.getenv("DEFAULT_TRAILING_STOP_PCT", "0"))
DEFAULT_TRAILING_START_PCT = float(os.getenv("DEFAULT_TRAILING_START_PCT", "0"))
DEFAULT_BREAK_EVEN_ENABLED = os.getenv("DEFAULT_BREAK_EVEN_ENABLED", "true").lower() == "true"
DEFAULT_BREAK_EVEN_TRIGGER_PCT = float(os.getenv("DEFAULT_BREAK_EVEN_TRIGGER_PCT", "0"))
DEFAULT_BREAK_EVEN_OFFSET_PCT = float(os.getenv("DEFAULT_BREAK_EVEN_OFFSET_PCT", "0"))
DEFAULT_RSI_BUY = float(os.getenv("DEFAULT_RSI_BUY", "5"))
DEFAULT_RSI_SELL = float(os.getenv("DEFAULT_RSI_SELL", "95"))
DEFAULT_MAX_OPEN_POSITIONS = int(os.getenv("DEFAULT_MAX_OPEN_POSITIONS", "5"))
DEFAULT_SYMBOL_COOLDOWN_SECONDS = int(os.getenv("DEFAULT_SYMBOL_COOLDOWN_SECONDS", "300"))
RISK_CHECK_INTERVAL_SECONDS = float(os.getenv("RISK_CHECK_INTERVAL_SECONDS", "5"))
AUTO_SCAN_INTERVAL_SECONDS = float(os.getenv("AUTO_SCAN_INTERVAL_SECONDS", "10"))

# =========================================================
# YOUR RSI ALERT 1 / 2 / 3 SETUP
# =========================================================
RSI_ALERTS_ENABLED = os.getenv("RSI_ALERTS_ENABLED", "true").lower() == "true"
ALERT1_RSI_BUY = float(os.getenv("ALERT1_RSI_BUY", "10"))
ALERT1_RSI_SELL = float(os.getenv("ALERT1_RSI_SELL", "85"))
ALERT2_RSI_BUY = float(os.getenv("ALERT2_RSI_BUY", "8"))
ALERT2_RSI_SELL = float(os.getenv("ALERT2_RSI_SELL", "93"))
ALERT3_RSI_BUY = float(os.getenv("ALERT3_RSI_BUY", "5"))
ALERT3_RSI_SELL = float(os.getenv("ALERT3_RSI_SELL", "95"))
SEND_ALERT1_TO_GROUP = os.getenv("SEND_ALERT1_TO_GROUP", "false").lower() == "true"
SEND_ALERT2_TO_GROUP = os.getenv("SEND_ALERT2_TO_GROUP", "false").lower() == "true"
SEND_ALERT3_TO_GROUP = os.getenv("SEND_ALERT3_TO_GROUP", "true").lower() == "true"

# =========================================================
# BOLLINGER BAND CONFIG
# =========================================================
BB_ALERTS_ENABLED = os.getenv("BB_ALERTS_ENABLED", "true").lower() == "true"
BB_LENGTH = int(os.getenv("BB_LENGTH", "20"))
BB_STD_MULT = float(os.getenv("BB_STD_MULT", "2"))
BB_SQUEEZE_WIDTH_PCT = float(os.getenv("BB_SQUEEZE_WIDTH_PCT", "1.5"))
BB_EXPANSION_WIDTH_PCT = float(os.getenv("BB_EXPANSION_WIDTH_PCT", "4.0"))
BB_RANK_LIMIT_DEFAULT = int(os.getenv("BB_RANK_LIMIT_DEFAULT", "10"))
SEND_BB_SQUEEZE_TO_GROUP = os.getenv("SEND_BB_SQUEEZE_TO_GROUP", "false").lower() == "true"
SEND_BB_EXPANSION_TO_GROUP = os.getenv("SEND_BB_EXPANSION_TO_GROUP", "false").lower() == "true"

TRADES_HISTORY_LIMIT = int(os.getenv("TRADES_HISTORY_LIMIT", "500"))
DAILY_REPORT_ENABLED = os.getenv("DAILY_REPORT_ENABLED", "false").lower() == "true"
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "21"))

# =========================================================
# SESSION / GLOBALS
# =========================================================
def create_session():
    if BYBIT_API_KEY and BYBIT_API_SECRET:
        return HTTP(testnet=False, api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)
    return HTTP(testnet=False)

session = create_session()

data = {}
last_alert_candle = {}
shortlist = set()
live_symbols = set()
shortlist_lock = threading.Lock()
ws_lock = threading.Lock()
positions_lock = threading.Lock()
bybit_symbols_lock = threading.Lock()
trades_lock = threading.Lock()
ws_connections = {}
current_candle_start = {}
last_ws_message_at = 0
last_shortlist_refresh_at = 0
telegram_offset = 0
last_command_time = 0
bybit_linear_symbols = set()
telegram_queue = queue.Queue(maxsize=5000)

AUTO_TRADING_ENABLED = False
NOTIFICATIONS_MUTED = False
STOP_LOSS_PCT = DEFAULT_STOP_LOSS_PCT
TAKE_PROFIT_PCT = DEFAULT_TAKE_PROFIT_PCT
TRAILING_STOP_PCT = DEFAULT_TRAILING_STOP_PCT
TRAILING_START_PCT = DEFAULT_TRAILING_START_PCT
BREAK_EVEN_ENABLED = DEFAULT_BREAK_EVEN_ENABLED
BREAK_EVEN_TRIGGER_PCT = DEFAULT_BREAK_EVEN_TRIGGER_PCT
BREAK_EVEN_OFFSET_PCT = DEFAULT_BREAK_EVEN_OFFSET_PCT
RSI_BUY_THRESHOLD = DEFAULT_RSI_BUY
RSI_SELL_THRESHOLD = DEFAULT_RSI_SELL
MAX_OPEN_POSITIONS = DEFAULT_MAX_OPEN_POSITIONS
SYMBOL_COOLDOWN_SECONDS = DEFAULT_SYMBOL_COOLDOWN_SECONDS
TRADE_SIZE_USDT = BYBIT_TRADE_SIZE_USDT
LEVERAGE = BYBIT_LEVERAGE
positions = {}
symbol_cooldowns = {}
closed_trades = []
last_daily_report_date = None

# =========================================================
# SAFE HELPERS
# =========================================================
def log_error(where, exc):
    print(f"ERROR in {where}: {exc}")
    print(traceback.format_exc())

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

def normalize_symbol(symbol):
    symbol = str(symbol).upper().strip().replace("/", "")
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    return symbol

def normalize_interval(interval_value):
    s = str(interval_value).strip().lower()
    mapping = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "60m": "60", "1h": "60", "4h": "240", "1d": "D"}
    return mapping.get(s, s)

def pct_text(value):
    return "OFF" if value == 0 else f"{value:.2f}%"

def bool_text(value):
    return "ON" if value else "OFF"

def now_ts():
    return int(time.time())

def ts_to_text(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

def today_str_from_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

def parse_on_off(value):
    v = str(value).strip().lower()
    if v in ("on", "true", "1", "yes"):
        return True
    if v in ("off", "false", "0", "no"):
        return False
    raise ValueError("Use on or off")

def parse_float_arg(text):
    return float(text.split(maxsplit=1)[1].strip())

def ensure_symbol_state(symbol):
    symbol = normalize_symbol(symbol)
    if symbol not in data:
        data[symbol] = {}
    if symbol not in current_candle_start:
        current_candle_start[symbol] = {}
    for tf in TIMEFRAMES:
        if tf not in data[symbol]:
            data[symbol][tf] = []

def should_alert_once_per_candle(alert_type, symbol, tf, candle_start):
    key = f"{alert_type}:{symbol}:{tf}"
    if last_alert_candle.get(key) == candle_start:
        return False
    last_alert_candle[key] = candle_start
    return True

def decimal_places_from_step(step_str):
    s = str(step_str)
    if "." not in s:
        return 0
    trimmed = s.rstrip("0")
    if "." not in trimmed:
        return 0
    return len(trimmed.split(".")[1])

def floor_to_step_str(value, step_str):
    try:
        value_dec = Decimal(str(value))
        step_dec = Decimal(str(step_str))
        if step_dec <= 0:
            return str(value_dec)
        floored = (value_dec / step_dec).to_integral_value(rounding=ROUND_DOWN) * step_dec
        decimals = decimal_places_from_step(step_str)
        if decimals <= 0:
            return str(floored.quantize(Decimal("1")))
        fmt = "1." + ("0" * decimals)
        return format(floored.quantize(Decimal(fmt)), "f")
    except (InvalidOperation, ValueError):
        return str(value)

def fmt_qty_or_price(value):
    s = str(value)
    return s.rstrip("0").rstrip(".") if "." in s else s

# =========================================================
# TELEGRAM
# =========================================================
def _send_telegram_http(chat_id, message):
    if not TELEGRAM_ENABLED or not BOT_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(url, json={"chat_id": chat_id, "text": str(message)[:3900]}, timeout=12)
    response.raise_for_status()

def enqueue_telegram(chat_id, message):
    if not TELEGRAM_ENABLED or not chat_id:
        return
    try:
        telegram_queue.put_nowait((chat_id, str(message)))
    except queue.Full:
        print("Telegram queue full. Dropping message.")

def send_telegram(message, force=False):
    if NOTIFICATIONS_MUTED and not force:
        return
    enqueue_telegram(CHAT_ID, message)

def send_telegram_group(message, force=False):
    if NOTIFICATIONS_MUTED and not force:
        return
    enqueue_telegram(GROUP_CHAT_ID, message)

def send_to_private_and_optional_group(message, group_enabled=False):
    send_telegram(message)
    if group_enabled:
        send_telegram_group(message)

def reply_telegram(message):
    send_telegram(message, force=True)

def telegram_sender_loop():
    while True:
        try:
            chat_id, message = telegram_queue.get()
            try:
                _send_telegram_http(chat_id, message)
                time.sleep(0.05)
            except Exception as e:
                log_error("telegram_sender_loop send", e)
            finally:
                telegram_queue.task_done()
        except Exception as e:
            log_error("telegram_sender_loop", e)
            time.sleep(2)

def get_telegram_updates(offset=None, timeout=20):
    if not TELEGRAM_ENABLED or not BOT_TOKEN:
        return []
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        response = requests.get(url, params=params, timeout=timeout + 8)
        response.raise_for_status()
        payload = response.json()
        return payload.get("result", []) if payload.get("ok") else []
    except Exception as e:
        log_error("get_telegram_updates", e)
        return []

# =========================================================
# INDICATORS
# =========================================================
def rsi(values, length=14):
    if len(values) < length + 1:
        return None
    gains, losses = [], []
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

def bollinger_bands(values, length=20, std_mult=2.0):
    if len(values) < length:
        return None
    window = values[-length:]
    mid = sum(window) / length
    variance = sum((x - mid) ** 2 for x in window) / length
    std = math.sqrt(variance)
    upper = mid + (std_mult * std)
    lower = mid - (std_mult * std)
    width_pct = 0.0 if mid == 0 else ((upper - lower) / mid) * 100.0
    return {"upper": upper, "middle": mid, "lower": lower, "width_pct": width_pct}

def process_rsi_tier_alerts(symbol, tf, candle_start, rsi_value):
    if not RSI_ALERTS_ENABLED:
        return

    if rsi_value >= ALERT3_RSI_SELL:
        if should_alert_once_per_candle("alert3_rsi_sell", symbol, tf, candle_start):
            msg = "🚨 ALERT 3 - RSI SELL NOW\nSymbol: {}\nTimeframe: {}m\nRSI: {:.2f}\nThreshold: >= {:.2f}".format(symbol, tf, rsi_value, ALERT3_RSI_SELL)
            send_to_private_and_optional_group(msg, SEND_ALERT3_TO_GROUP)
        return

    if rsi_value >= ALERT2_RSI_SELL:
        if should_alert_once_per_candle("alert2_rsi_sell", symbol, tf, candle_start):
            msg = "🔥 ALERT 2 - EXTREME SELL\nSymbol: {}\nTimeframe: {}m\nRSI: {:.2f}\nThreshold: >= {:.2f}".format(symbol, tf, rsi_value, ALERT2_RSI_SELL)
            send_to_private_and_optional_group(msg, SEND_ALERT2_TO_GROUP)
        return

    if rsi_value >= ALERT1_RSI_SELL:
        if should_alert_once_per_candle("alert1_rsi_sell", symbol, tf, candle_start):
            msg = "🚨 ALERT 1 - RSI SELL\nSymbol: {}\nTimeframe: {}m\nRSI: {:.2f}\nThreshold: >= {:.2f}".format(symbol, tf, rsi_value, ALERT1_RSI_SELL)
            send_to_private_and_optional_group(msg, SEND_ALERT1_TO_GROUP)
        return

    if rsi_value <= ALERT3_RSI_BUY:
        if should_alert_once_per_candle("alert3_rsi_buy", symbol, tf, candle_start):
            msg = "🚨 ALERT 3 - RSI BUY NOW\nSymbol: {}\nTimeframe: {}m\nRSI: {:.2f}\nThreshold: <= {:.2f}".format(symbol, tf, rsi_value, ALERT3_RSI_BUY)
            send_to_private_and_optional_group(msg, SEND_ALERT3_TO_GROUP)
        return

    if rsi_value <= ALERT2_RSI_BUY:
        if should_alert_once_per_candle("alert2_rsi_buy", symbol, tf, candle_start):
            msg = "🔥 ALERT 2 - EXTREME BUY\nSymbol: {}\nTimeframe: {}m\nRSI: {:.2f}\nThreshold: <= {:.2f}".format(symbol, tf, rsi_value, ALERT2_RSI_BUY)
            send_to_private_and_optional_group(msg, SEND_ALERT2_TO_GROUP)
        return

    if rsi_value <= ALERT1_RSI_BUY:
        if should_alert_once_per_candle("alert1_rsi_buy", symbol, tf, candle_start):
            msg = "🚨 ALERT 1 - RSI BUY\nSymbol: {}\nTimeframe: {}m\nRSI: {:.2f}\nThreshold: <= {:.2f}".format(symbol, tf, rsi_value, ALERT1_RSI_BUY)
            send_to_private_and_optional_group(msg, SEND_ALERT1_TO_GROUP)
        return

def process_bb_alerts(symbol, tf, candle_start, bb):
    if not BB_ALERTS_ENABLED:
        return
    width = bb["width_pct"]
    if width <= BB_SQUEEZE_WIDTH_PCT:
        if should_alert_once_per_candle("bb_squeeze", symbol, tf, candle_start):
            msg = "🟡 BB CLOSE / SQUEEZE ALERT\nSymbol: {}\nTimeframe: {}m\nBB width: {:.2f}%\nThreshold: <= {:.2f}%".format(symbol, tf, width, BB_SQUEEZE_WIDTH_PCT)
            send_to_private_and_optional_group(msg, SEND_BB_SQUEEZE_TO_GROUP)
        return
    if width >= BB_EXPANSION_WIDTH_PCT:
        if should_alert_once_per_candle("bb_expansion", symbol, tf, candle_start):
            msg = "🟢 BB WIDE EXPANSION ALERT\nSymbol: {}\nTimeframe: {}m\nBB width: {:.2f}%\nThreshold: >= {:.2f}%".format(symbol, tf, width, BB_EXPANSION_WIDTH_PCT)
            send_to_private_and_optional_group(msg, SEND_BB_EXPANSION_TO_GROUP)
        return

# =========================================================
# BYBIT MARKET / HISTORY
# =========================================================
def get_tickers():
    return session.get_tickers(category=CATEGORY).get("result", {}).get("list", [])

def build_shortlist_from_tickers():
    candidates = []
    for item in get_tickers():
        symbol = str(item.get("symbol", "")).upper()
        if not symbol.endswith("USDT"):
            continue
        last_price = safe_float(item.get("lastPrice"))
        prev_price_1h = safe_float(item.get("prevPrice1h"))
        turnover_24h = safe_float(item.get("turnover24h"), 0.0)
        if not last_price or last_price <= 0 or not turnover_24h or turnover_24h <= 0:
            continue
        change_1h = ((last_price - prev_price_1h) / prev_price_1h) * 100 if prev_price_1h and prev_price_1h > 0 else 0.0
        candidates.append((symbol, abs(change_1h), turnover_24h))
    candidates.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return [s for s, _, _ in candidates[:TOP_N]]

def fetch_history(symbol, tf):
    try:
        symbol = normalize_symbol(symbol)
        tf = normalize_interval(tf)
        ensure_symbol_state(symbol)
        response = session.get_kline(category=CATEGORY, symbol=symbol, interval=tf, limit=HISTORY_LIMIT)
        rows = response.get("result", {}).get("list", [])
        rows.reverse()
        closes = []
        for row in rows:
            if len(row) >= 5:
                close = safe_float(row[4])
                if close is not None:
                    closes.append(close)
        if closes:
            data[symbol][tf] = closes
    except Exception as e:
        log_error(f"fetch_history {symbol} {tf}", e)

def refresh_shortlist_and_history():
    global shortlist, live_symbols, last_shortlist_refresh_at
    symbols = build_shortlist_from_tickers()
    selected = symbols[:LIVE_WS_SYMBOLS]
    new_shortlist = set(symbols)
    new_live = set(selected)

    with shortlist_lock:
        old_live = set(live_symbols)

    to_fetch = sorted(list(new_live - old_live))
    required = max(20, BB_LENGTH + 1, 15)

    for symbol in to_fetch:
        ensure_symbol_state(symbol)
        for tf in TIMEFRAMES:
            fetch_history(symbol, tf)
            time.sleep(REQUEST_SLEEP_SECONDS)

    for symbol in selected:
        ensure_symbol_state(symbol)
        for tf in TIMEFRAMES:
            if len(data.get(symbol, {}).get(tf, [])) < required:
                fetch_history(symbol, tf)
                time.sleep(REQUEST_SLEEP_SECONDS)

    with shortlist_lock:
        shortlist = new_shortlist
        live_symbols = new_live
    last_shortlist_refresh_at = now_ts()
    print("Shortlist refreshed: shortlist={} live={}".format(len(shortlist), len(live_symbols)))

def get_symbol_rsi(symbol, tf="5", length=14):
    symbol = normalize_symbol(symbol)
    tf = normalize_interval(tf)
    arr = data.get(symbol, {}).get(tf, [])
    if len(arr) < length + 1:
        return None
    return rsi(arr, length)

def get_symbol_bb(symbol, tf="5", length=None, std_mult=None):
    symbol = normalize_symbol(symbol)
    tf = normalize_interval(tf)
    length = length or BB_LENGTH
    std_mult = std_mult or BB_STD_MULT
    arr = data.get(symbol, {}).get(tf, [])
    if len(arr) < length:
        return None
    return bollinger_bands(arr, length, std_mult)

def get_any_symbol_rsi_text(symbol, tf="5", length=14):
    try:
        symbol = normalize_symbol(symbol)
        tf = normalize_interval(tf)
        ensure_symbol_state(symbol)
        if len(data.get(symbol, {}).get(tf, [])) < length + 1:
            fetch_history(symbol, tf)
        value = get_symbol_rsi(symbol, tf, length)
        if value is None:
            return "⚠️ Not enough candle data for {} {}m.".format(symbol, tf)
        state = "ALERT 3 BUY" if value <= ALERT3_RSI_BUY else "ALERT 2 BUY" if value <= ALERT2_RSI_BUY else "ALERT 1 BUY" if value <= ALERT1_RSI_BUY else "ALERT 3 SELL" if value >= ALERT3_RSI_SELL else "ALERT 2 SELL" if value >= ALERT2_RSI_SELL else "ALERT 1 SELL" if value >= ALERT1_RSI_SELL else "NORMAL"
        return "📊 RSI CHECK\nSymbol: {}\nTimeframe: {}m\nRSI({}): {:.2f}\nState: {}\nA1 buy/sell: <= {:.2f} / >= {:.2f}\nA2 buy/sell: <= {:.2f} / >= {:.2f}\nA3 buy/sell: <= {:.2f} / >= {:.2f}".format(symbol, tf, length, value, state, ALERT1_RSI_BUY, ALERT1_RSI_SELL, ALERT2_RSI_BUY, ALERT2_RSI_SELL, ALERT3_RSI_BUY, ALERT3_RSI_SELL)
    except Exception as e:
        log_error("get_any_symbol_rsi_text", e)
        return "❌ Failed to check RSI for {}\n{}".format(symbol, e)

def get_any_symbol_bb_text(symbol, tf="5"):
    try:
        symbol = normalize_symbol(symbol)
        tf = normalize_interval(tf)
        ensure_symbol_state(symbol)
        if len(data.get(symbol, {}).get(tf, [])) < BB_LENGTH:
            fetch_history(symbol, tf)
        bb = get_symbol_bb(symbol, tf)
        if bb is None:
            return "⚠️ Not enough candle data for BB on {} {}m.".format(symbol, tf)
        width = bb["width_pct"]
        state = "CLOSE / SQUEEZE" if width <= BB_SQUEEZE_WIDTH_PCT else "WIDE EXPANSION" if width >= BB_EXPANSION_WIDTH_PCT else "NORMAL"
        return "📊 BB WIDTH CHECK\nSymbol: {}\nTimeframe: {}m\nBB({}, {})\nUpper: {:.8f}\nMiddle: {:.8f}\nLower: {:.8f}\nWidth: {:.2f}%\nState: {}\nClose/squeeze <= {:.2f}%\nWide expansion >= {:.2f}%".format(symbol, tf, BB_LENGTH, BB_STD_MULT, bb["upper"], bb["middle"], bb["lower"], width, state, BB_SQUEEZE_WIDTH_PCT, BB_EXPANSION_WIDTH_PCT)
    except Exception as e:
        log_error("get_any_symbol_bb_text", e)
        return "❌ Failed to check BB for {}\n{}".format(symbol, e)

def get_rsi_rankings_text(mode="low", limit=10):
    with shortlist_lock:
        symbols = sorted(list(live_symbols))
    rows = []
    for symbol in symbols:
        value = get_symbol_rsi(symbol, "5", 14)
        if value is not None:
            rows.append((symbol, value))
    if not rows:
        return "⚠️ No RSI values available yet."
    reverse = mode in ("high", "highest")
    rows.sort(key=lambda x: x[1], reverse=reverse)
    title = "🔥 HIGHEST RSI COINS" if reverse else "🧊 LOWEST RSI COINS"
    lines = [title, "Timeframe: 5m", ""]
    for i, (symbol, value) in enumerate(rows[:limit], start=1):
        lines.append("{}. {} — RSI: {:.2f}".format(i, symbol, value))
    return "\n".join(lines)

def get_bb_rankings_text(mode="low", limit=10):
    with shortlist_lock:
        symbols = sorted(list(live_symbols))
    rows = []
    for symbol in symbols:
        bb = get_symbol_bb(symbol, "5")
        if bb is not None:
            rows.append((symbol, bb["width_pct"]))
    if not rows:
        return "⚠️ No BB width values available yet."
    reverse = mode in ("high", "highest", "wide", "widest", "expansion")
    rows.sort(key=lambda x: x[1], reverse=reverse)
    title = "🟢 WIDEST BB EXPANSION" if reverse else "🟡 CLOSEST BB / SQUEEZE"
    lines = [title, "Timeframe: 5m", "BB: length={}, std={}".format(BB_LENGTH, BB_STD_MULT), ""]
    for i, (symbol, width) in enumerate(rows[:limit], start=1):
        lines.append("{}. {} — BB width: {:.2f}%".format(i, symbol, width))
    return "\n".join(lines)

# =========================================================
# WEBSOCKET
# =========================================================
def process_indicators(symbol, tf, candle_start):
    arr = data.get(symbol, {}).get(tf, [])
    if not arr:
        return
    rsi_value = rsi(arr, 14)
    if rsi_value is not None:
        process_rsi_tier_alerts(symbol, tf, candle_start, rsi_value)
    bb = bollinger_bands(arr, BB_LENGTH, BB_STD_MULT)
    if bb is not None:
        process_bb_alerts(symbol, tf, candle_start, bb)

def handle_kline(msg):
    global last_ws_message_at
    try:
        last_ws_message_at = now_ts()
        if WS_DEBUG:
            print("RAW WS:", msg)
        if not isinstance(msg, dict):
            return
        topic = str(msg.get("topic", ""))
        parts = topic.split(".")
        topic_interval = normalize_interval(parts[1]) if len(parts) >= 2 else ""
        topic_symbol = parts[2].upper() if len(parts) >= 3 else ""
        data_list = msg.get("data")
        if not isinstance(data_list, list):
            return
        with shortlist_lock:
            current_live = set(live_symbols)
        for item in data_list:
            if not isinstance(item, dict):
                continue
            symbol = normalize_symbol(item.get("symbol") or topic_symbol)
            tf = normalize_interval(item.get("interval") or topic_interval)
            close = safe_float(item.get("close"))
            start_time = safe_int(item.get("start"))
            if not symbol or not tf or close is None or start_time is None:
                continue
            if symbol not in current_live or tf not in TIMEFRAMES:
                continue
            ensure_symbol_state(symbol)
            arr = data[symbol][tf]
            prev_start = current_candle_start[symbol].get(tf)
            if prev_start is None or start_time != prev_start:
                current_candle_start[symbol][tf] = start_time
                arr.append(close)
                if len(arr) > HISTORY_LIMIT:
                    arr.pop(0)
            else:
                if arr:
                    arr[-1] = close
                else:
                    arr.append(close)
            process_indicators(symbol, tf, start_time)
    except Exception as e:
        log_error("handle_kline", e)

def stop_all_websockets():
    global ws_connections
    with ws_lock:
        for key, ws in ws_connections.items():
            try:
                if hasattr(ws, "exit"):
                    ws.exit()
                print("Closed WebSocket:", key)
            except Exception as e:
                log_error("stop websocket", e)
        ws_connections = {}

def start_websocket_for_timeframe(tf):
    ws = WebSocket(testnet=False, channel_type=CATEGORY)
    with shortlist_lock:
        symbols = sorted(list(live_symbols))
    if not symbols:
        return None
    print("Subscribing {} symbols for tf={}".format(len(symbols), tf))
    for symbol in symbols:
        try:
            ws.kline_stream(interval=int(tf), symbol=symbol, callback=handle_kline)
            time.sleep(min(REQUEST_SLEEP_SECONDS, 0.03))
        except Exception as e:
            log_error("subscribe {} {}".format(symbol, tf), e)
    return ws

def restart_websockets():
    global ws_connections
    stop_all_websockets()
    new_connections = {}
    for tf in TIMEFRAMES:
        try:
            ws = start_websocket_for_timeframe(tf)
            if ws is not None:
                new_connections[tf] = ws
        except Exception as e:
            log_error("restart websocket tf={}".format(tf), e)
    with ws_lock:
        ws_connections = new_connections

# =========================================================
# BYBIT ACCOUNT / TRADING
# =========================================================
def bybit_keys_ready():
    return bool(BYBIT_API_KEY and BYBIT_API_SECRET)

def get_bybit_account_wallet():
    return session.get_wallet_balance(accountType="UNIFIED")

def get_bybit_nonzero_balances_text():
    account = get_bybit_account_wallet()
    root_list = account.get("result", {}).get("list", [])
    rows = []
    for acct in root_list:
        for coin in acct.get("coin", []):
            equity = safe_float(coin.get("equity"), 0.0)
            wallet = safe_float(coin.get("walletBalance"), 0.0)
            if equity > 0 or wallet > 0:
                rows.append("{}: equity={}, wallet={}, usd={}".format(coin.get("coin"), equity, wallet, coin.get("usdValue")))
    return "No non-zero Bybit balances found." if not rows else "💰 Bybit balances\n" + "\n".join(rows[:20])

def get_bybit_total_available_balance_usd():
    account = get_bybit_account_wallet()
    root_list = account.get("result", {}).get("list", [])
    if not root_list:
        return 0.0
    return safe_float(root_list[0].get("totalAvailableBalance"), 0.0) or 0.0

def get_bybit_last_price(symbol):
    rows = session.get_tickers(category=CATEGORY, symbol=symbol).get("result", {}).get("list", [])
    if not rows:
        raise Exception("No ticker returned for {}".format(symbol))
    price = safe_float(rows[0].get("lastPrice"))
    if not price or price <= 0:
        raise Exception("Invalid last price for {}".format(symbol))
    return price

def get_bybit_instrument_info(symbol):
    rows = session.get_instruments_info(category=CATEGORY, symbol=symbol).get("result", {}).get("list", [])
    if not rows:
        raise Exception("No instrument info found for {}".format(symbol))
    return rows[0]

def refresh_bybit_linear_symbols():
    global bybit_linear_symbols
    try:
        valid, cursor = set(), None
        while True:
            params = {"category": CATEGORY, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            result = session.get_instruments_info(**params).get("result", {})
            for row in result.get("list", []):
                symbol = str(row.get("symbol", "")).upper()
                if str(row.get("status", "")) == "Trading" and symbol.endswith("USDT"):
                    valid.add(symbol)
            cursor = result.get("nextPageCursor")
            if not cursor:
                break
        with bybit_symbols_lock:
            bybit_linear_symbols = valid
        print("Loaded Bybit symbols:", len(valid))
    except Exception as e:
        log_error("refresh_bybit_linear_symbols", e)

def is_symbol_supported_on_bybit(symbol):
    with bybit_symbols_lock:
        return True if not bybit_linear_symbols else symbol in bybit_linear_symbols

def get_bybit_position(symbol):
    rows = session.get_positions(category=CATEGORY, symbol=symbol).get("result", {}).get("list", [])
    for row in rows:
        size = safe_float(row.get("size"), 0.0)
        side = str(row.get("side", "")).strip()
        if size and size > 0 and side in ("Buy", "Sell"):
            return row
    return None

def get_bybit_all_open_long_positions():
    found, cursor = [], None
    while True:
        params = {"category": CATEGORY, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        result = session.get_positions(**params).get("result", {})
        for row in result.get("list", []):
            size = safe_float(row.get("size"), 0.0)
            side = str(row.get("side", "")).strip()
            symbol = str(row.get("symbol", "")).upper()
            if symbol and side == "Buy" and size and size > 0:
                found.append(row)
        cursor = result.get("nextPageCursor")
        if not cursor:
            break
    return found

def get_open_positions_count():
    with positions_lock:
        return len(positions)

def symbol_in_position(symbol):
    with positions_lock:
        return symbol in positions

def set_symbol_cooldown(symbol):
    symbol_cooldowns[symbol] = now_ts()

def symbol_on_cooldown(symbol):
    last = symbol_cooldowns.get(symbol)
    return bool(last and (time.time() - last) < SYMBOL_COOLDOWN_SECONDS)

def compute_risk_prices(entry_price):
    sl = None if STOP_LOSS_PCT == 0 else entry_price * (1 - STOP_LOSS_PCT / 100.0)
    tp = None if TAKE_PROFIT_PCT == 0 else entry_price * (1 + TAKE_PROFIT_PCT / 100.0)
    return sl, tp

def compute_trailing_stop_price(highest_price):
    return None if TRAILING_STOP_PCT == 0 or highest_price is None else highest_price * (1 - TRAILING_STOP_PCT / 100.0)

def trailing_should_be_active(entry_price, highest_price):
    if TRAILING_STOP_PCT == 0:
        return False
    if TRAILING_START_PCT == 0:
        return True
    return highest_price >= entry_price * (1 + TRAILING_START_PCT / 100.0)

def break_even_should_be_active(entry_price, highest_price):
    if not BREAK_EVEN_ENABLED or highest_price is None or entry_price <= 0:
        return False
    return highest_price >= entry_price * (1 + BREAK_EVEN_TRIGGER_PCT / 100.0)

def compute_break_even_stop_price(entry_price):
    return entry_price * (1 + BREAK_EVEN_OFFSET_PCT / 100.0)

def set_position(symbol, entry_price, quantity, source):
    sl, tp = compute_risk_prices(entry_price)
    be_active = break_even_should_be_active(entry_price, entry_price)
    be_price = compute_break_even_stop_price(entry_price) if be_active else None
    if be_active and (sl is None or be_price > sl):
        sl = be_price
    trailing_active = trailing_should_be_active(entry_price, entry_price)
    with positions_lock:
        positions[symbol] = {
            "symbol": symbol,
            "entry_price": entry_price,
            "quantity": quantity,
            "stop_loss_price": sl,
            "take_profit_price": tp,
            "highest_price": entry_price,
            "trailing_stop_price": compute_trailing_stop_price(entry_price) if trailing_active else None,
            "trailing_active": trailing_active,
            "break_even_active": be_active,
            "break_even_stop_price": be_price,
            "source": source,
            "opened_at": now_ts(),
        }

def sync_local_position_from_exchange(symbol):
    pos = get_bybit_position(symbol)
    if not pos:
        return False
    entry = safe_float(pos.get("avgPrice") or pos.get("avgEntryPrice"))
    qty = safe_float(pos.get("size"), 0.0)
    side = str(pos.get("side", "")).strip()
    if side != "Buy" or not entry or qty <= 0:
        return False
    set_position(symbol, entry, qty, "bybit_sync")
    return True

def record_closed_trade(symbol, exit_price, reason):
    with positions_lock:
        pos = positions.get(symbol)
        if not pos:
            return None
        entry = pos["entry_price"]
        qty = pos["quantity"]
        opened_at = pos.get("opened_at", now_ts())
    pnl_usdt = (exit_price - entry) * qty
    pnl_pct = 0.0 if entry <= 0 else ((exit_price - entry) / entry) * 100.0
    trade = {"symbol": symbol, "entry_price": entry, "exit_price": exit_price, "quantity": qty, "pnl_usdt": pnl_usdt, "pnl_pct": pnl_pct, "reason": reason, "opened_at": opened_at, "closed_at": now_ts()}
    with trades_lock:
        closed_trades.append(trade)
        if len(closed_trades) > TRADES_HISTORY_LIMIT:
            del closed_trades[0:len(closed_trades) - TRADES_HISTORY_LIMIT]
    return trade

def remove_position(symbol):
    with positions_lock:
        positions.pop(symbol, None)
    set_symbol_cooldown(symbol)

def close_local_position_and_record(symbol, exit_price, reason):
    trade = record_closed_trade(symbol, exit_price, reason)
    remove_position(symbol)
    return trade

def refresh_all_position_risk_levels():
    with positions_lock:
        for pos in positions.values():
            entry = pos["entry_price"]
            sl, tp = compute_risk_prices(entry)
            highest = pos.get("highest_price", entry)
            be_active = break_even_should_be_active(entry, highest)
            be_price = compute_break_even_stop_price(entry) if be_active else None
            if be_active and (sl is None or be_price > sl):
                sl = be_price
            trailing_active = trailing_should_be_active(entry, highest)
            pos["stop_loss_price"] = sl
            pos["take_profit_price"] = tp
            pos["break_even_active"] = be_active
            pos["break_even_stop_price"] = be_price
            pos["trailing_active"] = trailing_active
            pos["trailing_stop_price"] = compute_trailing_stop_price(highest) if trailing_active else None

def update_position_high_water(symbol, current_price):
    with positions_lock:
        pos = positions.get(symbol)
        if not pos:
            return
        if current_price > pos.get("highest_price", 0):
            pos["highest_price"] = current_price
    refresh_all_position_risk_levels()

def get_positions_text():
    with positions_lock:
        if not positions:
            return "📭 No open tracked positions."
        lines = ["📦 Open tracked positions"]
        for symbol, pos in positions.items():
            sl = "OFF" if pos.get("stop_loss_price") is None else "{:.8f}".format(pos["stop_loss_price"])
            tp = "OFF" if pos.get("take_profit_price") is None else "{:.8f}".format(pos["take_profit_price"])
            ts = "OFF" if pos.get("trailing_stop_price") is None else "{:.8f}".format(pos["trailing_stop_price"])
            lines.append("{} | entry={:.8f} | qty={} | sl={} | tp={} | high={:.8f} | tsl={}".format(symbol, pos["entry_price"], pos["quantity"], sl, tp, pos.get("highest_price", 0), ts))
        return "\n".join(lines[:30])

def compute_bybit_order_qty(symbol, last_price):
    instrument = get_bybit_instrument_info(symbol)
    lot = instrument.get("lotSizeFilter", {})
    qty_step = str(lot.get("qtyStep", "0.001"))
    min_order_qty = safe_float(lot.get("minOrderQty"), 0.0)
    min_notional = safe_float(lot.get("minNotionalValue"), 0.0)
    max_qty = safe_float(lot.get("maxMktOrderQty"), 0.0)
    raw_qty = (TRADE_SIZE_USDT * LEVERAGE) / last_price
    qty_str = fmt_qty_or_price(floor_to_step_str(raw_qty, qty_step))
    qty = safe_float(qty_str, 0.0)
    if qty <= 0:
        raise Exception("Quantity became 0. Increase size or leverage.")
    if min_order_qty and qty < min_order_qty:
        raise Exception("Qty too small. qty={}, min={}".format(qty, min_order_qty))
    if min_notional and qty * last_price < min_notional:
        raise Exception("Notional too small. notional={}, min={}".format(qty * last_price, min_notional))
    if max_qty and qty > max_qty:
        raise Exception("Qty too large. qty={}, max={}".format(qty, max_qty))
    return qty_str, qty

def clamp_leverage_for_symbol(symbol, requested):
    info = get_bybit_instrument_info(symbol)
    lev_filter = info.get("leverageFilter", {})
    min_lev = safe_int(lev_filter.get("minLeverage"), 1)
    max_lev = safe_int(lev_filter.get("maxLeverage"), requested)
    return max(min_lev, min(requested, max_lev))

def ensure_bybit_leverage(symbol):
    target = clamp_leverage_for_symbol(symbol, LEVERAGE)
    try:
        session.set_leverage(category=CATEGORY, symbol=symbol, buyLeverage=str(target), sellLeverage=str(target))
    except Exception as e:
        msg = str(e).lower()
        if not any(x in msg for x in ["110043", "not modified", "same to the existing leverage"]):
            raise
    return target

def place_bybit_market_buy(symbol):
    symbol = normalize_symbol(symbol)
    if CATEGORY != "linear":
        raise Exception("This bot is built for BYBIT_CATEGORY=linear")
    if not BYBIT_TRADING_ENABLED:
        raise Exception("BYBIT_TRADING_ENABLED is false")
    if not bybit_keys_ready():
        raise Exception("Bybit API keys missing")
    if symbol_in_position(symbol):
        raise Exception("Already in tracked position for {}".format(symbol))
    if get_open_positions_count() >= MAX_OPEN_POSITIONS:
        raise Exception("Max open positions reached")
    if get_bybit_total_available_balance_usd() <= 0:
        raise Exception("No available balance")
    existing = get_bybit_position(symbol)
    if existing and str(existing.get("side", "")) == "Buy":
        raise Exception("Exchange already has open Buy position for {}".format(symbol))
    used_lev = ensure_bybit_leverage(symbol)
    price = get_bybit_last_price(symbol)
    qty_str, qty = compute_bybit_order_qty(symbol, price)
    order = session.place_order(category=CATEGORY, symbol=symbol, side="Buy", orderType="Market", qty=qty_str)
    time.sleep(1)
    if not sync_local_position_from_exchange(symbol):
        set_position(symbol, get_bybit_last_price(symbol), qty, "buy_fallback_market")
    return order, used_lev

def place_bybit_market_sell(symbol, reason="manual_sell"):
    symbol = normalize_symbol(symbol)
    if not BYBIT_TRADING_ENABLED:
        raise Exception("BYBIT_TRADING_ENABLED is false")
    if not bybit_keys_ready():
        raise Exception("Bybit API keys missing")
    pos = get_bybit_position(symbol)
    if not pos:
        raise Exception("No exchange position found for {}".format(symbol))
    if str(pos.get("side", "")).strip() != "Buy":
        raise Exception("No open long to close for {}".format(symbol))
    size = safe_float(pos.get("size"), 0.0)
    info = get_bybit_instrument_info(symbol)
    step = str(info.get("lotSizeFilter", {}).get("qtyStep", "0.001"))
    qty_str = fmt_qty_or_price(floor_to_step_str(size, step))
    order = session.place_order(category=CATEGORY, symbol=symbol, side="Sell", orderType="Market", qty=qty_str, reduceOnly=True)
    time.sleep(1)
    trade = close_local_position_and_record(symbol, get_bybit_last_price(symbol), reason)
    return order, trade

def panic_close_all_positions():
    if not BYBIT_TRADING_ENABLED:
        raise Exception("BYBIT_TRADING_ENABLED is false")
    closed, errors = [], []
    for pos in get_bybit_all_open_long_positions():
        symbol = str(pos.get("symbol", "")).upper()
        try:
            sync_local_position_from_exchange(symbol)
            place_bybit_market_sell(symbol, "panic_close")
            closed.append(symbol)
            time.sleep(0.3)
        except Exception as e:
            errors.append("{}: {}".format(symbol, e))
    return closed, errors

# =========================================================
# AUTO TRADING LOOPS
# =========================================================
def auto_entry_loop():
    while True:
        try:
            if not AUTO_TRADING_ENABLED or not BYBIT_TRADING_ENABLED or not bybit_keys_ready():
                time.sleep(AUTO_SCAN_INTERVAL_SECONDS)
                continue
            if get_open_positions_count() >= MAX_OPEN_POSITIONS:
                time.sleep(AUTO_SCAN_INTERVAL_SECONDS)
                continue
            with shortlist_lock:
                candidates = list(live_symbols)
            signals = []
            for symbol in candidates:
                if symbol_in_position(symbol) or symbol_on_cooldown(symbol) or not is_symbol_supported_on_bybit(symbol):
                    continue
                value = get_symbol_rsi(symbol, "5", 14)
                if value is not None and value <= RSI_BUY_THRESHOLD:
                    signals.append((symbol, value))
            signals.sort(key=lambda x: x[1])
            for symbol, value in signals:
                if get_open_positions_count() >= MAX_OPEN_POSITIONS:
                    break
                try:
                    order, used_lev = place_bybit_market_buy(symbol)
                    msg = "🟢 AUTO BUY\nSymbol: {}\nRSI(5m): {:.2f}\nOrder ID: {}\nMargin: {:.4f} USDT\nLeverage: {}x".format(symbol, value, order.get("result", {}).get("orderId"), TRADE_SIZE_USDT, used_lev)
                    send_telegram(msg)
                except Exception as e:
                    log_error("auto buy {}".format(symbol), e)
        except Exception as e:
            log_error("auto_entry_loop", e)
        time.sleep(AUTO_SCAN_INTERVAL_SECONDS)

def auto_exit_loop():
    while True:
        try:
            with positions_lock:
                current = list(positions.items())
            if not current:
                time.sleep(RISK_CHECK_INTERVAL_SECONDS)
                continue
            for symbol, pos in current:
                try:
                    price = get_bybit_last_price(symbol)
                    update_position_high_water(symbol, price)
                    with positions_lock:
                        p = positions.get(symbol)
                        if not p:
                            continue
                        sl = p.get("stop_loss_price")
                        tp = p.get("take_profit_price")
                        tsl = p.get("trailing_stop_price")
                        trailing_active = p.get("trailing_active", False)
                        entry = p["entry_price"]
                    current_rsi = get_symbol_rsi(symbol, "5", 14)
                    reason = None
                    if sl is not None and price <= sl:
                        reason = "stop_loss"
                    elif tp is not None and price >= tp:
                        reason = "take_profit"
                    elif trailing_active and tsl is not None and price <= tsl:
                        reason = "trailing_stop"
                    elif current_rsi is not None and current_rsi >= RSI_SELL_THRESHOLD:
                        reason = "rsi_exit"
                    if reason:
                        order, trade = place_bybit_market_sell(symbol, reason)
                        pnl = "" if not trade else "\nPnL: {:.8f} USDT ({:.2f}%)".format(trade["pnl_usdt"], trade["pnl_pct"])
                        send_telegram("🔴 AUTO EXIT\nReason: {}\nSymbol: {}\nEntry: {:.8f}\nExit trigger: {:.8f}\nOrder ID: {}{}".format(reason, symbol, entry, price, order.get("result", {}).get("orderId"), pnl))
                except Exception as e:
                    log_error("auto exit {}".format(symbol), e)
        except Exception as e:
            log_error("auto_exit_loop", e)
        time.sleep(RISK_CHECK_INTERVAL_SECONDS)

# =========================================================
# TEXT REPORTS
# =========================================================
def get_pnl_text(day_filter=None):
    with trades_lock:
        trades = list(closed_trades)
    if day_filter:
        trades = [t for t in trades if today_str_from_ts(t["closed_at"]) == day_filter]
    total = len(trades)
    wins = len([t for t in trades if t["pnl_usdt"] > 0])
    losses = len([t for t in trades if t["pnl_usdt"] < 0])
    total_pnl = sum(t["pnl_usdt"] for t in trades)
    win_rate = 0 if total == 0 else wins / total * 100
    label = "TODAY" if day_filter else "ALL TIME"
    return "📊 PNL SUMMARY ({})\nTrades: {}\nWins: {}\nLosses: {}\nWin rate: {:.2f}%\nTotal PnL: {:.8f} USDT".format(label, total, wins, losses, win_rate, total_pnl)

def get_recent_trades_text(limit=10):
    with trades_lock:
        trades = list(closed_trades)[-limit:]
    if not trades:
        return "📭 No closed trades yet."
    trades.reverse()
    lines = ["🧾 RECENT CLOSED TRADES"]
    for t in trades:
        lines.append("{} | pnl={:.8f} USDT ({:.2f}%) | reason={} | closed={}".format(t["symbol"], t["pnl_usdt"], t["pnl_pct"], t["reason"], ts_to_text(t["closed_at"])))
    return "\n".join(lines)

def get_status_text():
    with shortlist_lock:
        short_count = len(shortlist)
        live_count = len(live_symbols)
    return "✅ Bot running\nCategory: {}\nLive symbols: {}\nShortlist: {}\nTelegram: {}\nMuted: {}\nBybit trading: {}\nAuto trading: {}\nOpen positions: {}\nRSI alerts: {}\nA1 buy/sell: <= {:.2f} / >= {:.2f}\nA2 buy/sell: <= {:.2f} / >= {:.2f}\nA3 buy/sell: <= {:.2f} / >= {:.2f}\nBB alerts: {}\nBB close <= {:.2f}%\nBB wide >= {:.2f}%\nSL: {}\nTP: {}\nTrailing: {}\nSize: {:.4f} USDT\nLeverage: {}x".format(CATEGORY, live_count, short_count, TELEGRAM_ENABLED, NOTIFICATIONS_MUTED, BYBIT_TRADING_ENABLED, AUTO_TRADING_ENABLED, get_open_positions_count(), bool_text(RSI_ALERTS_ENABLED), ALERT1_RSI_BUY, ALERT1_RSI_SELL, ALERT2_RSI_BUY, ALERT2_RSI_SELL, ALERT3_RSI_BUY, ALERT3_RSI_SELL, bool_text(BB_ALERTS_ENABLED), BB_SQUEEZE_WIDTH_PCT, BB_EXPANSION_WIDTH_PCT, pct_text(STOP_LOSS_PCT), pct_text(TAKE_PROFIT_PCT), pct_text(TRAILING_STOP_PCT), TRADE_SIZE_USDT, LEVERAGE)

def get_diag_text():
    age = now_ts() - last_ws_message_at if last_ws_message_at else -1
    refresh_age = now_ts() - last_shortlist_refresh_at if last_shortlist_refresh_at else -1
    with ws_lock:
        ws_keys = list(ws_connections.keys())
    return "🛠 DIAG\nws_keys: {}\nlast_ws_message_age_sec: {}\nlast_shortlist_refresh_age_sec: {}\nqueue_size: {}\nkeys_ready: {}".format(ws_keys, age, refresh_age, telegram_queue.qsize(), bybit_keys_ready())

def get_symbols_text():
    with shortlist_lock:
        syms = sorted(list(live_symbols))
    return "📊 Live symbol count: {}\nSample:\n{}".format(len(syms), "\n".join(syms[:60]))

def telegram_help_text():
    return (
        "Commands:\n"
        "/status /diag /symbols /help\n"
        "/rsi BTCUSDT | /rsilow 10 | /rsihigh 10\n"
        "/bb BTCUSDT | /bb close 10 | /bb wide 10\n"
        "/setrsialerts on|off\n"
        "/setalert1buy 10 | /setalert1sell 85\n"
        "/setalert2buy 8 | /setalert2sell 93\n"
        "/setalert3buy 5 | /setalert3sell 95\n"
        "/setbbalerts on|off | /setbbclose 1.5 | /setbbwide 4\n"
        "/autoon /autooff /buy /sell /panicclose\n"
        "/showpositions /pnl /dailyreport /trades\n"
        "/setrsibuy 5 /setrsisell 95 /setsl 0 /settp 3\n"
        "/settrailing 2 /settrailstart 1 /setbe on|off\n"
        "/setbetrigger 1 /setbeoffset 0.1\n"
        "/setmaxcoins 5 /setcooldown 300 /setsize 20 /setlev 3\n"
        "/mute /unmute /force /bybit /bbal"
    )

def force_test_alerts():
    send_telegram("🚨 ALERT 1 - RSI SELL\nSymbol: BTCUSDT\nRSI: {:.2f}".format(ALERT1_RSI_SELL))
    send_telegram("🔥 ALERT 2 - EXTREME SELL\nSymbol: BTCUSDT\nRSI: {:.2f}".format(ALERT2_RSI_SELL))
    send_to_private_and_optional_group("🚨 ALERT 3 - RSI SELL NOW\nSymbol: BTCUSDT\nRSI: {:.2f}".format(ALERT3_RSI_SELL), SEND_ALERT3_TO_GROUP)
    send_telegram("🟢 BB WIDE EXPANSION ALERT\nSymbol: BTCUSDT\nBB width: {:.2f}%".format(BB_EXPANSION_WIDTH_PCT))

# =========================================================
# COMMAND LOOP
# =========================================================
def telegram_command_loop():
    global telegram_offset, last_command_time
    global AUTO_TRADING_ENABLED, NOTIFICATIONS_MUTED
    global STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_STOP_PCT, TRAILING_START_PCT
    global BREAK_EVEN_ENABLED, BREAK_EVEN_TRIGGER_PCT, BREAK_EVEN_OFFSET_PCT
    global RSI_BUY_THRESHOLD, RSI_SELL_THRESHOLD, MAX_OPEN_POSITIONS, SYMBOL_COOLDOWN_SECONDS, TRADE_SIZE_USDT, LEVERAGE
    global RSI_ALERTS_ENABLED, ALERT1_RSI_BUY, ALERT1_RSI_SELL, ALERT2_RSI_BUY, ALERT2_RSI_SELL, ALERT3_RSI_BUY, ALERT3_RSI_SELL
    global BB_ALERTS_ENABLED, BB_SQUEEZE_WIDTH_PCT, BB_EXPANSION_WIDTH_PCT

    if not TELEGRAM_ENABLED:
        print("Telegram command loop disabled")
        return

    while True:
        try:
            updates = get_telegram_updates(offset=telegram_offset, timeout=20)
            for upd in updates:
                telegram_offset = upd.get("update_id", telegram_offset) + 1
                message = upd.get("message", {})
                chat = message.get("chat", {})
                text = str(message.get("text", "")).strip()
                from_chat_id = str(chat.get("id", ""))
                if not text:
                    continue
                allowed = {str(CHAT_ID).strip(), str(GROUP_CHAT_ID).strip()}
                allowed = {x for x in allowed if x}
                if allowed and from_chat_id not in allowed:
                    print("Ignoring unauthorized chat:", from_chat_id)
                    continue
                if time.time() - last_command_time < 0.8:
                    time.sleep(0.8)
                last_command_time = time.time()
                lower = text.lower().strip()

                try:
                    if lower == "/test":
                        reply_telegram("✅ Telegram command test OK")
                    elif lower == "/mute":
                        NOTIFICATIONS_MUTED = True
                        reply_telegram("🔇 Muted")
                    elif lower == "/unmute":
                        NOTIFICATIONS_MUTED = False
                        reply_telegram("🔔 Unmuted")
                    elif lower == "/force":
                        force_test_alerts()
                        reply_telegram("✅ Force test sent")
                    elif lower == "/status":
                        reply_telegram(get_status_text())
                    elif lower == "/diag":
                        reply_telegram(get_diag_text())
                    elif lower == "/symbols":
                        reply_telegram(get_symbols_text())
                    elif lower == "/help":
                        reply_telegram(telegram_help_text())
                    elif lower == "/bybit":
                        test_bybit_connection(True)
                    elif lower == "/bbal":
                        reply_telegram(get_bybit_nonzero_balances_text())
                    elif lower == "/pnl":
                        reply_telegram(get_pnl_text())
                    elif lower == "/dailyreport":
                        reply_telegram(get_pnl_text(today_str_from_ts(now_ts())))
                    elif lower == "/trades":
                        reply_telegram(get_recent_trades_text())
                    elif lower == "/showpositions":
                        reply_telegram(get_positions_text())
                    elif lower == "/autoon":
                        AUTO_TRADING_ENABLED = True
                        reply_telegram("✅ Auto trading enabled")
                    elif lower == "/autooff":
                        AUTO_TRADING_ENABLED = False
                        reply_telegram("⏸ Auto trading disabled")
                    elif lower == "/buy":
                        order, lev = place_bybit_market_buy(BYBIT_SYMBOL)
                        reply_telegram("✅ BUY placed\nSymbol: {}\nOrder ID: {}\nLeverage: {}x".format(BYBIT_SYMBOL, order.get("result", {}).get("orderId"), lev))
                    elif lower == "/sell":
                        order, trade = place_bybit_market_sell(BYBIT_SYMBOL, "manual_sell")
                        pnl = "" if not trade else "\nPnL: {:.8f} USDT ({:.2f}%)".format(trade["pnl_usdt"], trade["pnl_pct"])
                        reply_telegram("✅ SELL placed\nSymbol: {}\nOrder ID: {}{}".format(BYBIT_SYMBOL, order.get("result", {}).get("orderId"), pnl))
                    elif lower == "/panicclose":
                        closed, errors = panic_close_all_positions()
                        reply_telegram("🚨 PANIC CLOSE\nClosed:\n{}\nErrors:\n{}".format("\n".join(closed) if closed else "None", "\n".join(errors) if errors else "None"))
                    elif lower.startswith("/rsi "):
                        parts = text.split()
                        tf = normalize_interval(parts[2]) if len(parts) >= 3 else "5"
                        reply_telegram(get_any_symbol_rsi_text(parts[1], tf))
                    elif lower.startswith("/rsilow"):
                        parts = text.split()
                        limit = int(parts[1]) if len(parts) >= 2 else 10
                        reply_telegram(get_rsi_rankings_text("low", max(1, min(limit, 50))))
                    elif lower.startswith("/rsihigh"):
                        parts = text.split()
                        limit = int(parts[1]) if len(parts) >= 2 else 10
                        reply_telegram(get_rsi_rankings_text("high", max(1, min(limit, 50))))
                    elif lower.startswith("/bb"):
                        parts = text.split()
                        if len(parts) >= 2 and parts[1].lower() in ("close", "low", "lowest", "squeeze"):
                            limit = int(parts[2]) if len(parts) >= 3 else BB_RANK_LIMIT_DEFAULT
                            reply_telegram(get_bb_rankings_text("low", max(1, min(limit, 50))))
                        elif len(parts) >= 2 and parts[1].lower() in ("wide", "high", "highest", "expansion", "widest"):
                            limit = int(parts[2]) if len(parts) >= 3 else BB_RANK_LIMIT_DEFAULT
                            reply_telegram(get_bb_rankings_text("high", max(1, min(limit, 50))))
                        elif len(parts) >= 2:
                            tf = normalize_interval(parts[2]) if len(parts) >= 3 else "5"
                            reply_telegram(get_any_symbol_bb_text(parts[1], tf))
                        else:
                            reply_telegram("Usage: /bb BTCUSDT or /bb close 10 or /bb wide 10")
                    elif lower.startswith("/setrsialerts "):
                        RSI_ALERTS_ENABLED = parse_on_off(text.split(maxsplit=1)[1])
                        reply_telegram("✅ RSI alerts: {}".format(bool_text(RSI_ALERTS_ENABLED)))
                    elif lower.startswith("/setalert1buy "):
                        v = parse_float_arg(text)
                        if not (ALERT2_RSI_BUY < v <= 100):
                            raise ValueError("Alert 1 buy must be higher than Alert 2 buy")
                        ALERT1_RSI_BUY = v
                        reply_telegram("✅ Alert 1 buy <= {:.2f}".format(v))
                    elif lower.startswith("/setalert1sell "):
                        v = parse_float_arg(text)
                        if not (0 <= v < ALERT2_RSI_SELL):
                            raise ValueError("Alert 1 sell must be lower than Alert 2 sell")
                        ALERT1_RSI_SELL = v
                        reply_telegram("✅ Alert 1 sell >= {:.2f}".format(v))
                    elif lower.startswith("/setalert2buy "):
                        v = parse_float_arg(text)
                        if not (ALERT3_RSI_BUY < v < ALERT1_RSI_BUY):
                            raise ValueError("Alert 2 buy must be between Alert 3 and Alert 1 buy")
                        ALERT2_RSI_BUY = v
                        reply_telegram("✅ Alert 2 buy <= {:.2f}".format(v))
                    elif lower.startswith("/setalert2sell "):
                        v = parse_float_arg(text)
                        if not (ALERT1_RSI_SELL < v < ALERT3_RSI_SELL):
                            raise ValueError("Alert 2 sell must be between Alert 1 and Alert 3 sell")
                        ALERT2_RSI_SELL = v
                        reply_telegram("✅ Alert 2 sell >= {:.2f}".format(v))
                    elif lower.startswith("/setalert3buy "):
                        v = parse_float_arg(text)
                        if not (0 <= v < ALERT2_RSI_BUY):
                            raise ValueError("Alert 3 buy must be lower than Alert 2 buy")
                        ALERT3_RSI_BUY = v
                        reply_telegram("✅ Alert 3 buy <= {:.2f}".format(v))
                    elif lower.startswith("/setalert3sell "):
                        v = parse_float_arg(text)
                        if not (ALERT2_RSI_SELL < v <= 100):
                            raise ValueError("Alert 3 sell must be higher than Alert 2 sell")
                        ALERT3_RSI_SELL = v
                        reply_telegram("✅ Alert 3 sell >= {:.2f}".format(v))
                    elif lower.startswith("/setbbalerts "):
                        BB_ALERTS_ENABLED = parse_on_off(text.split(maxsplit=1)[1])
                        reply_telegram("✅ BB alerts: {}".format(bool_text(BB_ALERTS_ENABLED)))
                    elif lower.startswith("/setbbclose ") or lower.startswith("/setbbwidthlow "):
                        v = parse_float_arg(text)
                        if v < 0 or v >= BB_EXPANSION_WIDTH_PCT:
                            raise ValueError("BB close must be lower than BB wide")
                        BB_SQUEEZE_WIDTH_PCT = v
                        reply_telegram("✅ BB close/squeeze <= {:.2f}%".format(v))
                    elif lower.startswith("/setbbwide ") or lower.startswith("/setbbwidthhigh "):
                        v = parse_float_arg(text)
                        if v <= BB_SQUEEZE_WIDTH_PCT:
                            raise ValueError("BB wide must be higher than BB close")
                        BB_EXPANSION_WIDTH_PCT = v
                        reply_telegram("✅ BB wide expansion >= {:.2f}%".format(v))
                    elif lower.startswith("/setrsibuy "):
                        RSI_BUY_THRESHOLD = parse_float_arg(text)
                        reply_telegram("✅ Auto buy RSI <= {:.2f}".format(RSI_BUY_THRESHOLD))
                    elif lower.startswith("/setrsisell "):
                        RSI_SELL_THRESHOLD = parse_float_arg(text)
                        reply_telegram("✅ Auto sell RSI >= {:.2f}".format(RSI_SELL_THRESHOLD))
                    elif lower.startswith("/setsl "):
                        STOP_LOSS_PCT = max(0, parse_float_arg(text))
                        refresh_all_position_risk_levels()
                        reply_telegram("✅ SL: {}".format(pct_text(STOP_LOSS_PCT)))
                    elif lower.startswith("/settp "):
                        TAKE_PROFIT_PCT = max(0, parse_float_arg(text))
                        refresh_all_position_risk_levels()
                        reply_telegram("✅ TP: {}".format(pct_text(TAKE_PROFIT_PCT)))
                    elif lower.startswith("/settrailing "):
                        TRAILING_STOP_PCT = max(0, parse_float_arg(text))
                        refresh_all_position_risk_levels()
                        reply_telegram("✅ Trailing: {}".format(pct_text(TRAILING_STOP_PCT)))
                    elif lower.startswith("/settrailstart "):
                        TRAILING_START_PCT = max(0, parse_float_arg(text))
                        refresh_all_position_risk_levels()
                        reply_telegram("✅ Trail start: {}".format(pct_text(TRAILING_START_PCT)))
                    elif lower.startswith("/setbe "):
                        BREAK_EVEN_ENABLED = parse_on_off(text.split(maxsplit=1)[1])
                        refresh_all_position_risk_levels()
                        reply_telegram("✅ Break-even: {}".format(bool_text(BREAK_EVEN_ENABLED)))
                    elif lower.startswith("/setbetrigger "):
                        BREAK_EVEN_TRIGGER_PCT = max(0, parse_float_arg(text))
                        refresh_all_position_risk_levels()
                        reply_telegram("✅ BE trigger: {}".format(pct_text(BREAK_EVEN_TRIGGER_PCT)))
                    elif lower.startswith("/setbeoffset "):
                        BREAK_EVEN_OFFSET_PCT = max(0, parse_float_arg(text))
                        refresh_all_position_risk_levels()
                        reply_telegram("✅ BE offset: {}".format(pct_text(BREAK_EVEN_OFFSET_PCT)))
                    elif lower.startswith("/setmaxcoins "):
                        MAX_OPEN_POSITIONS = max(1, int(text.split(maxsplit=1)[1].strip()))
                        reply_telegram("✅ Max positions: {}".format(MAX_OPEN_POSITIONS))
                    elif lower.startswith("/setcooldown "):
                        SYMBOL_COOLDOWN_SECONDS = max(0, int(text.split(maxsplit=1)[1].strip()))
                        reply_telegram("✅ Cooldown: {}s".format(SYMBOL_COOLDOWN_SECONDS))
                    elif lower.startswith("/setsize "):
                        TRADE_SIZE_USDT = max(0.01, parse_float_arg(text))
                        reply_telegram("✅ Size: {:.4f} USDT".format(TRADE_SIZE_USDT))
                    elif lower.startswith("/setlev "):
                        LEVERAGE = max(1, int(text.split(maxsplit=1)[1].strip()))
                        reply_telegram("✅ Leverage: {}x".format(LEVERAGE))
                    else:
                        reply_telegram("Unknown command. Use /help")
                except Exception as e:
                    log_error("command {}".format(text), e)
                    reply_telegram("❌ Command failed\n{}".format(e))
            time.sleep(1)
        except Exception as e:
            log_error("telegram_command_loop", e)
            time.sleep(5)

# =========================================================
# BACKGROUND LOOPS
# =========================================================
def heartbeat_loop():
    while True:
        try:
            print("BOT ALIVE ✅")
            if HEARTBEAT_TO_TELEGRAM:
                send_telegram("BOT ALIVE ✅")
        except Exception as e:
            log_error("heartbeat_loop", e)
        time.sleep(HEARTBEAT_SECONDS)

def shortlist_refresh_loop():
    last_snapshot = set()
    while True:
        try:
            refresh_shortlist_and_history()
            with shortlist_lock:
                current = set(live_symbols)
            if current != last_snapshot:
                restart_websockets()
                last_snapshot = set(current)
            refresh_bybit_linear_symbols()
        except Exception as e:
            log_error("shortlist_refresh_loop", e)
        time.sleep(SHORTLIST_REFRESH_SECONDS)

def daily_report_loop():
    global last_daily_report_date
    while True:
        try:
            if DAILY_REPORT_ENABLED and TELEGRAM_ENABLED:
                now = datetime.now()
                day = now.strftime("%Y-%m-%d")
                if now.hour == DAILY_REPORT_HOUR and last_daily_report_date != day:
                    send_telegram(get_pnl_text(day_filter=day))
                    last_daily_report_date = day
        except Exception as e:
            log_error("daily_report_loop", e)
        time.sleep(30)

def scan_loop():
    while True:
        try:
            with shortlist_lock:
                ready = bool(live_symbols)
            if not ready:
                print("Waiting for live symbols...")
                time.sleep(5)
                continue
            time.sleep(20)
        except Exception as e:
            log_error("scan_loop", e)
            time.sleep(5)

def test_bybit_connection(force_reply=False):
    sender = reply_telegram if force_reply else send_telegram
    if not bybit_keys_ready():
        sender("❌ Bybit keys missing")
        return
    try:
        session.get_tickers(category=CATEGORY, symbol=BYBIT_SYMBOL)
        get_bybit_account_wallet()
        refresh_bybit_linear_symbols()
        sender("✅ Bybit MAINNET connected\nCategory: {}\nSymbol: {}\nTrading enabled: {}".format(CATEGORY, BYBIT_SYMBOL, BYBIT_TRADING_ENABLED))
    except Exception as e:
        log_error("test_bybit_connection", e)
        sender("❌ Bybit connection failed\n{}".format(e))

# =========================================================
# MAIN
# =========================================================
def start_daemon(target, name):
    t = threading.Thread(target=target, name=name, daemon=True)
    t.start()
    print("Started thread:", name)
    return t

def main():
    print("Starting production-safe scanner...")
    print("TELEGRAM_ENABLED =", TELEGRAM_ENABLED)
    print("CHAT_ID set =", bool(CHAT_ID))
    print("GROUP_CHAT_ID set =", bool(GROUP_CHAT_ID))
    print("CATEGORY =", CATEGORY)
    print("TIMEFRAMES =", TIMEFRAMES)
    print("BYBIT_TRADING_ENABLED =", BYBIT_TRADING_ENABLED)
    print("BYBIT_KEYS_READY =", bybit_keys_ready())
    print("RSI TIERS:", ALERT1_RSI_BUY, ALERT1_RSI_SELL, ALERT2_RSI_BUY, ALERT2_RSI_SELL, ALERT3_RSI_BUY, ALERT3_RSI_SELL)
    print("BB:", BB_LENGTH, BB_STD_MULT, BB_SQUEEZE_WIDTH_PCT, BB_EXPANSION_WIDTH_PCT)

    start_daemon(telegram_sender_loop, "telegram_sender")
    send_telegram("🚀 Scanner started")
    test_bybit_connection()

    start_daemon(heartbeat_loop, "heartbeat")
    start_daemon(shortlist_refresh_loop, "shortlist_refresh")
    start_daemon(telegram_command_loop, "telegram_commands")
    start_daemon(auto_entry_loop, "auto_entry")
    start_daemon(auto_exit_loop, "auto_exit")
    start_daemon(daily_report_loop, "daily_report")

    scan_loop()

if __name__ == "__main__":
    main()
