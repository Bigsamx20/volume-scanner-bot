import os
import time
import threading
import requests
import queue
import math
from copy import deepcopy
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from datetime import datetime
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
SHORTLIST_REFRESH_SECONDS = int(os.getenv("SHORTLIST_REFRESH_SECONDS", "30"))
REQUEST_SLEEP_SECONDS = float(os.getenv("REQUEST_SLEEP_SECONDS", "0.01"))
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
# AUTO STRATEGY DEFAULTS
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
# PREFERRED ALERT VALUES
# Bot sends alerts ONLY when these preferred values are hit.
# You can change them from Telegram commands.
# =========================================================
RSI_ALERTS_ENABLED = os.getenv("RSI_ALERTS_ENABLED", "true").lower() == "true"
PREFERRED_RSI_BUY = float(os.getenv("PREFERRED_RSI_BUY", "5"))
PREFERRED_RSI_SELL = float(os.getenv("PREFERRED_RSI_SELL", "95"))

BB_ALERTS_ENABLED = os.getenv("BB_ALERTS_ENABLED", "true").lower() == "true"
BB_LENGTH = int(os.getenv("BB_LENGTH", "20"))
BB_STD_MULT = float(os.getenv("BB_STD_MULT", "2"))
PREFERRED_BB_SQUEEZE_WIDTH_PCT = float(os.getenv("PREFERRED_BB_SQUEEZE_WIDTH_PCT", "1.5"))
PREFERRED_BB_EXPANSION_WIDTH_PCT = float(os.getenv("PREFERRED_BB_EXPANSION_WIDTH_PCT", "4.0"))
BB_RANK_LIMIT_DEFAULT = int(os.getenv("BB_RANK_LIMIT_DEFAULT", "10"))

# Alert routing
SEND_RSI_BUY_TO_GROUP = os.getenv("SEND_RSI_BUY_TO_GROUP", "false").lower() == "true"
SEND_RSI_SELL_TO_GROUP = os.getenv("SEND_RSI_SELL_TO_GROUP", "false").lower() == "true"
SEND_BB_SQUEEZE_TO_GROUP = os.getenv("SEND_BB_SQUEEZE_TO_GROUP", "false").lower() == "true"
SEND_BB_EXPANSION_TO_GROUP = os.getenv("SEND_BB_EXPANSION_TO_GROUP", "false").lower() == "true"

# =========================================================
# PNL / REPORTING CONFIG
# =========================================================
TRADES_HISTORY_LIMIT = int(os.getenv("TRADES_HISTORY_LIMIT", "500"))
DAILY_REPORT_ENABLED = os.getenv("DAILY_REPORT_ENABLED", "false").lower() == "true"
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "21"))

TIMEFRAME_DEFAULTS = {
    tf: {
        "rsi": {"enabled": True, "length": 14},
        "bb": {"enabled": True, "length": BB_LENGTH, "std_mult": BB_STD_MULT},
    }
    for tf in TIMEFRAMES
}
SYMBOL_OVERRIDES = {}

# =========================================================
# SESSION
# =========================================================
def create_session():
    if BYBIT_API_KEY and BYBIT_API_SECRET:
        return HTTP(testnet=False, api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)
    return HTTP(testnet=False)

session = create_session()

# =========================================================
# GLOBALS
# =========================================================
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
last_closed_candle_start = {}
telegram_offset = 0
last_command_time = 0
last_ws_message_at = 0
last_shortlist_refresh_at = 0
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
# TELEGRAM
# =========================================================
def _send_telegram_http(chat_id: str, message: str):
    if not TELEGRAM_ENABLED or not BOT_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
    response.raise_for_status()

def enqueue_telegram(chat_id: str, message: str):
    if not TELEGRAM_ENABLED or not chat_id:
        return
    try:
        telegram_queue.put_nowait((chat_id, message))
    except queue.Full:
        print(f"Telegram queue full. Dropping message for {chat_id}")

def send_telegram(message: str, force: bool = False):
    if NOTIFICATIONS_MUTED and not force:
        print("Telegram muted. Dropping private message.")
        return
    enqueue_telegram(CHAT_ID, message)

def send_telegram_group(message: str, force: bool = False):
    if NOTIFICATIONS_MUTED and not force:
        print("Telegram muted. Dropping group message.")
        return
    enqueue_telegram(GROUP_CHAT_ID, message)

def send_to_private_and_optional_group(message: str, group_enabled: bool = False):
    send_telegram(message)
    if group_enabled:
        send_telegram_group(message)

def reply_telegram(message: str):
    send_telegram(message, force=True)

def telegram_sender_loop():
    while True:
        try:
            chat_id, message = telegram_queue.get()
            try:
                _send_telegram_http(chat_id, message)
            except Exception as e:
                print(f"Telegram send error for {chat_id}: {e}")
            finally:
                telegram_queue.task_done()
        except Exception as e:
            print("telegram_sender_loop error:", e)
            time.sleep(1)

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
        return payload.get("result", []) if payload.get("ok") else []
    except Exception as e:
        print("Telegram getUpdates error:", e)
        return []

# =========================================================
# HELPERS
# =========================================================
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
    mapping = {"1": "1", "1m": "1", "3": "3", "3m": "3", "5": "5", "5m": "5", "15": "15", "15m": "15", "30": "30", "30m": "30", "60": "60", "60m": "60", "1h": "60", "240": "240", "4h": "240", "d": "D", "1d": "D"}
    return mapping.get(s, s)

def normalize_symbol(symbol: str) -> str:
    symbol = symbol.upper().strip().replace("/", "")
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    return symbol

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
    return deep_merge(defaults, symbol_cfg.get(tf, {}))

def should_alert_once_per_candle(alert_type: str, symbol: str, tf: str, candle_start: int) -> bool:
    key = f"{alert_type}:{symbol}:{tf}"
    if last_alert_candle.get(key) == candle_start:
        return False
    last_alert_candle[key] = candle_start
    return True

def pct_text(value: float) -> str:
    return "OFF" if value == 0 else f"{value:.2f}%"

def bool_text(value: bool) -> str:
    return "ON" if value else "OFF"

def decimal_places_from_step(step_str: str) -> int:
    s = str(step_str)
    if "." not in s:
        return 0
    trimmed = s.rstrip("0")
    if "." not in trimmed:
        return 0
    return len(trimmed.split(".")[1])

def floor_to_step_str(value: float, step_str: str) -> str:
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

def fmt_qty_or_price(value) -> str:
    s = str(value)
    return s.rstrip("0").rstrip(".") if "." in s else s

def now_ts():
    return int(time.time())

def ts_to_text(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

def today_str_from_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

def parse_float_command(text: str) -> float:
    return float(text.split(maxsplit=1)[1].strip())

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

def process_preferred_rsi_alerts(symbol: str, tf: str, candle_start: int, rsi_value: float):
    if not RSI_ALERTS_ENABLED:
        return
    if rsi_value <= PREFERRED_RSI_BUY and should_alert_once_per_candle("preferred_rsi_buy", symbol, tf, candle_start):
        msg = (
            f"🟢 RSI BUY ALERT\n"
            f"Symbol: {symbol}\nTimeframe: {tf}m\n"
            f"RSI: {rsi_value:.2f}\nYour buy value: <= {PREFERRED_RSI_BUY:.2f}"
        )
        send_to_private_and_optional_group(msg, SEND_RSI_BUY_TO_GROUP)
    if rsi_value >= PREFERRED_RSI_SELL and should_alert_once_per_candle("preferred_rsi_sell", symbol, tf, candle_start):
        msg = (
            f"🔴 RSI SELL ALERT\n"
            f"Symbol: {symbol}\nTimeframe: {tf}m\n"
            f"RSI: {rsi_value:.2f}\nYour sell value: >= {PREFERRED_RSI_SELL:.2f}"
        )
        send_to_private_and_optional_group(msg, SEND_RSI_SELL_TO_GROUP)

def process_preferred_bb_alerts(symbol: str, tf: str, candle_start: int, bb: dict):
    if not BB_ALERTS_ENABLED:
        return
    width = bb["width_pct"]
    if width <= PREFERRED_BB_SQUEEZE_WIDTH_PCT and should_alert_once_per_candle("preferred_bb_squeeze", symbol, tf, candle_start):
        msg = (
            f"🟡 BB SQUEEZE ALERT\n"
            f"Symbol: {symbol}\nTimeframe: {tf}m\n"
            f"BB width: {width:.2f}%\nYour squeeze value: <= {PREFERRED_BB_SQUEEZE_WIDTH_PCT:.2f}%"
        )
        send_to_private_and_optional_group(msg, SEND_BB_SQUEEZE_TO_GROUP)
    if width >= PREFERRED_BB_EXPANSION_WIDTH_PCT and should_alert_once_per_candle("preferred_bb_expansion", symbol, tf, candle_start):
        msg = (
            f"🟢 BB EXPANSION ALERT\n"
            f"Symbol: {symbol}\nTimeframe: {tf}m\n"
            f"BB width: {width:.2f}%\nYour expansion value: >= {PREFERRED_BB_EXPANSION_WIDTH_PCT:.2f}%"
        )
        send_to_private_and_optional_group(msg, SEND_BB_EXPANSION_TO_GROUP)

# =========================================================
# BYBIT HELPERS
# =========================================================
def bybit_keys_ready() -> bool:
    return bool(BYBIT_API_KEY and BYBIT_API_SECRET)

def get_bybit_account_wallet():
    return session.get_wallet_balance(accountType="UNIFIED")

def get_bybit_nonzero_balances_text():
    account = get_bybit_account_wallet()
    root_list = account.get("result", {}).get("list", [])
    if not root_list:
        return "No Bybit wallet data returned."
    rows = []
    for acct in root_list:
        for coin in acct.get("coin", []):
            equity = safe_float(coin.get("equity"), 0.0)
            wallet_balance = safe_float(coin.get("walletBalance"), 0.0)
            if equity > 0 or wallet_balance > 0:
                rows.append(f"{coin.get('coin')}: equity={equity}, wallet={wallet_balance}, usd={coin.get('usdValue')}")
    return "No non-zero Bybit balances found." if not rows else "💰 Bybit balances\n" + "\n".join(rows[:20])

def get_bybit_total_available_balance_usd():
    account = get_bybit_account_wallet()
    root_list = account.get("result", {}).get("list", [])
    if not root_list:
        return 0.0
    return safe_float(root_list[0].get("totalAvailableBalance"), 0.0) or 0.0

def get_bybit_instrument_info(symbol: str):
    info = session.get_instruments_info(category=CATEGORY, symbol=symbol)
    symbols = info.get("result", {}).get("list", [])
    if not symbols:
        raise Exception(f"No instrument info found for {symbol}")
    return symbols[0]

def get_bybit_last_price(symbol: str) -> float:
    ticker = session.get_tickers(category=CATEGORY, symbol=symbol)
    rows = ticker.get("result", {}).get("list", [])
    if not rows:
        raise Exception(f"No ticker returned for {symbol}")
    price = safe_float(rows[0].get("lastPrice"))
    if price is None or price <= 0:
        raise Exception(f"Could not read valid last price for {symbol}")
    return price

def refresh_bybit_linear_symbols():
    global bybit_linear_symbols
    try:
        valid, cursor = set(), None
        while True:
            params = {"category": CATEGORY, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            response = session.get_instruments_info(**params)
            result = response.get("result", {})
            for row in result.get("list", []):
                symbol_name = str(row.get("symbol", "")).upper()
                if str(row.get("status", "")) == "Trading" and symbol_name.endswith("USDT"):
                    valid.add(symbol_name)
            cursor = result.get("nextPageCursor")
            if not cursor:
                break
        with bybit_symbols_lock:
            bybit_linear_symbols = valid
        print(f"Loaded Bybit tradable {CATEGORY} symbols: {len(valid)}")
    except Exception as e:
        print("Failed to refresh Bybit symbols:", e)

def is_symbol_supported_on_bybit(symbol: str) -> bool:
    with bybit_symbols_lock:
        return True if not bybit_linear_symbols else symbol in bybit_linear_symbols

def get_bybit_position(symbol: str):
    response = session.get_positions(category=CATEGORY, symbol=symbol)
    rows = response.get("result", {}).get("list", [])
    for row in rows:
        size = safe_float(row.get("size"), 0.0)
        side = str(row.get("side", "")).strip()
        if size and size > 0 and side in ("Buy", "Sell"):
            return row
    return None

def get_bybit_all_open_long_positions():
    positions_found, cursor = [], None
    while True:
        params = {"category": CATEGORY, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        response = session.get_positions(**params)
        result = response.get("result", {})
        for row in result.get("list", []):
            size = safe_float(row.get("size"), 0.0)
            side = str(row.get("side", "")).strip()
            symbol = str(row.get("symbol", "")).upper()
            if symbol and side == "Buy" and size and size > 0:
                positions_found.append(row)
        cursor = result.get("nextPageCursor")
        if not cursor:
            break
    return positions_found

def sync_local_position_from_exchange(symbol: str):
    pos = get_bybit_position(symbol)
    if not pos:
        return False
    entry_price = safe_float(pos.get("avgPrice") or pos.get("avgEntryPrice"))
    quantity = safe_float(pos.get("size"), 0.0)
    side = str(pos.get("side", "")).strip()
    if side != "Buy" or not entry_price or quantity <= 0:
        return False
    set_position(symbol, entry_price, quantity, "bybit_sync")
    return True

def compute_bybit_order_qty(symbol: str, last_price: float):
    if last_price <= 0:
        raise Exception(f"Invalid last price for {symbol}")
    instrument = get_bybit_instrument_info(symbol)
    lot = instrument.get("lotSizeFilter", {})
    qty_step = str(lot.get("qtyStep", "0.001"))
    min_order_qty = safe_float(lot.get("minOrderQty"), 0.0)
    min_notional_value = safe_float(lot.get("minNotionalValue"), 0.0)
    max_mkt_order_qty = safe_float(lot.get("maxMktOrderQty"), 0.0)
    notional_usdt = TRADE_SIZE_USDT * LEVERAGE
    raw_qty = notional_usdt / last_price
    qty_str = fmt_qty_or_price(floor_to_step_str(raw_qty, qty_step))
    qty = safe_float(qty_str, 0.0)
    if qty <= 0:
        raise Exception(f"Rounded quantity became 0 for {symbol}. Increase BYBIT_TRADE_SIZE_USDT or leverage.")
    if min_order_qty and qty < min_order_qty:
        raise Exception(f"Qty too small for {symbol}. qty={qty}, minOrderQty={min_order_qty}.")
    if min_notional_value and qty * last_price < min_notional_value:
        raise Exception(f"Notional too small for {symbol}. notional={qty * last_price:.8f}, minNotionalValue={min_notional_value}.")
    if max_mkt_order_qty and qty > max_mkt_order_qty:
        raise Exception(f"Qty too large for {symbol}. qty={qty}, maxMktOrderQty={max_mkt_order_qty}.")
    return qty_str, qty, instrument

def clamp_leverage_for_symbol(symbol: str, requested_leverage: int) -> int:
    instrument = get_bybit_instrument_info(symbol)
    lev_filter = instrument.get("leverageFilter", {})
    min_lev = safe_int(lev_filter.get("minLeverage"), 1)
    max_lev = safe_int(lev_filter.get("maxLeverage"), requested_leverage)
    return max(min_lev, min(requested_leverage, max_lev))

def ensure_bybit_leverage(symbol: str):
    target = clamp_leverage_for_symbol(symbol, LEVERAGE)
    try:
        response = session.set_leverage(category=CATEGORY, symbol=symbol, buyLeverage=str(target), sellLeverage=str(target))
        print(f"Set leverage {symbol} -> {target}x:", response)
        return target
    except Exception as e:
        msg = str(e).lower()
        if any(token in msg for token in ["110043", "not modified", "same to the existing leverage"]):
            print(f"Leverage already set for {symbol}: {e}")
            return target
        raise

# =========================================================
# POSITION / PNL HELPERS
# =========================================================
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
    return False if not last else (time.time() - last) < SYMBOL_COOLDOWN_SECONDS

def compute_risk_prices(entry_price: float):
    sl = None if STOP_LOSS_PCT == 0 else entry_price * (1 - STOP_LOSS_PCT / 100.0)
    tp = None if TAKE_PROFIT_PCT == 0 else entry_price * (1 + TAKE_PROFIT_PCT / 100.0)
    return sl, tp

def compute_trailing_stop_price(highest_price: float):
    if TRAILING_STOP_PCT == 0 or highest_price is None:
        return None
    return highest_price * (1 - TRAILING_STOP_PCT / 100.0)

def trailing_should_be_active(entry_price: float, highest_price: float) -> bool:
    if TRAILING_STOP_PCT == 0:
        return False
    if TRAILING_START_PCT == 0:
        return True
    return highest_price >= entry_price * (1 + TRAILING_START_PCT / 100.0)

def break_even_should_be_active(entry_price: float, highest_price: float) -> bool:
    if not BREAK_EVEN_ENABLED:
        return False
    if highest_price is None or entry_price <= 0:
        return False
    trigger_price = entry_price * (1 + BREAK_EVEN_TRIGGER_PCT / 100.0)
    return highest_price >= trigger_price

def compute_break_even_stop_price(entry_price: float) -> float:
    return entry_price * (1 + BREAK_EVEN_OFFSET_PCT / 100.0)

def apply_break_even_to_stop_loss(pos: dict):
    if not pos.get("break_even_active"):
        return
    be_price = pos.get("break_even_stop_price")
    if be_price is None:
        return
    current_sl = pos.get("stop_loss_price")
    if current_sl is None or be_price > current_sl:
        pos["stop_loss_price"] = be_price

def set_position(symbol: str, entry_price: float, quantity: float, source: str):
    sl, tp = compute_risk_prices(entry_price)
    trailing_active = trailing_should_be_active(entry_price, entry_price)
    break_even_active = break_even_should_be_active(entry_price, entry_price)
    break_even_stop_price = compute_break_even_stop_price(entry_price) if break_even_active else None
    if break_even_active and (sl is None or break_even_stop_price > sl):
        sl = break_even_stop_price
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
            "break_even_active": break_even_active,
            "break_even_stop_price": break_even_stop_price,
            "source": source,
            "opened_at": int(time.time()),
        }

def record_closed_trade(symbol: str, exit_price: float, reason: str):
    with positions_lock:
        pos = positions.get(symbol)
        if not pos:
            return None
        entry_price = pos["entry_price"]
        quantity = pos["quantity"]
        opened_at = pos.get("opened_at", now_ts())
    pnl_usdt = (exit_price - entry_price) * quantity
    pnl_pct = 0.0 if entry_price <= 0 else ((exit_price - entry_price) / entry_price) * 100.0
    trade = {"symbol": symbol, "entry_price": entry_price, "exit_price": exit_price, "quantity": quantity, "pnl_usdt": pnl_usdt, "pnl_pct": pnl_pct, "reason": reason, "opened_at": opened_at, "closed_at": now_ts()}
    with trades_lock:
        closed_trades.append(trade)
        if len(closed_trades) > TRADES_HISTORY_LIMIT:
            del closed_trades[0:len(closed_trades) - TRADES_HISTORY_LIMIT]
    return trade

def remove_position(symbol: str):
    with positions_lock:
        positions.pop(symbol, None)
    set_symbol_cooldown(symbol)

def close_local_position_and_record(symbol: str, exit_price: float, reason: str):
    trade = record_closed_trade(symbol, exit_price, reason)
    remove_position(symbol)
    return trade

def refresh_all_position_risk_levels():
    with positions_lock:
        for _, pos in positions.items():
            entry = pos["entry_price"]
            sl, tp = compute_risk_prices(entry)
            pos["stop_loss_price"] = sl
            pos["take_profit_price"] = tp
            highest = pos.get("highest_price")
            be_active = break_even_should_be_active(entry, highest)
            pos["break_even_active"] = be_active
            pos["break_even_stop_price"] = compute_break_even_stop_price(entry) if be_active else None
            apply_break_even_to_stop_loss(pos)
            active = trailing_should_be_active(entry, highest)
            pos["trailing_active"] = active
            pos["trailing_stop_price"] = compute_trailing_stop_price(highest) if active else None

def update_position_high_water(symbol: str, current_price: float):
    with positions_lock:
        pos = positions.get(symbol)
        if not pos:
            return
        if pos.get("highest_price") is None or current_price > pos.get("highest_price"):
            pos["highest_price"] = current_price
        be_active = break_even_should_be_active(pos["entry_price"], pos["highest_price"])
        pos["break_even_active"] = be_active
        pos["break_even_stop_price"] = compute_break_even_stop_price(pos["entry_price"]) if be_active else None
        apply_break_even_to_stop_loss(pos)
        active = trailing_should_be_active(pos["entry_price"], pos["highest_price"])
        pos["trailing_active"] = active
        pos["trailing_stop_price"] = compute_trailing_stop_price(pos["highest_price"]) if active else None

def get_positions_text():
    with positions_lock:
        if not positions:
            return "📭 No open tracked positions."
        lines = ["📦 Open tracked positions"]
        for symbol, pos in positions.items():
            sl = "OFF" if pos["stop_loss_price"] is None else f"{pos['stop_loss_price']:.8f}"
            tp = "OFF" if pos["take_profit_price"] is None else f"{pos['take_profit_price']:.8f}"
            ts = "OFF" if pos.get("trailing_stop_price") is None else f"{pos['trailing_stop_price']:.8f}"
            hp = "N/A" if pos.get("highest_price") is None else f"{pos['highest_price']:.8f}"
            ta = "ON" if pos.get("trailing_active") else "OFF"
            be = "ON" if pos.get("break_even_active") else "OFF"
            be_price = "OFF" if pos.get("break_even_stop_price") is None else f"{pos['break_even_stop_price']:.8f}"
            lines.append(f"{symbol} | entry={pos['entry_price']:.8f} | qty={pos['quantity']} | sl={sl} | tp={tp} | high={hp} | tsl={ts} | ta={ta} | be={be} | be_stop={be_price}")
        return "\n".join(lines[:30])

def get_strategy_text():
    return (
        f"🤖 Auto strategy\nAuto trading: {AUTO_TRADING_ENABLED}\nExchange trading: Bybit MAINNET ({CATEGORY})\nNotifications muted: {NOTIFICATIONS_MUTED}\n"
        f"Timeframes: {', '.join(TIMEFRAMES)}\nLive symbols watched: {LIVE_WS_SYMBOLS}\n"
        f"Auto buy rule: 5m RSI <= {RSI_BUY_THRESHOLD:.2f}\nAuto sell rule: 5m RSI >= {RSI_SELL_THRESHOLD:.2f} OR SL/TP/Trailing\n"
        f"Preferred RSI alerts: {bool_text(RSI_ALERTS_ENABLED)}\nRSI buy alert <= {PREFERRED_RSI_BUY:.2f}\nRSI sell alert >= {PREFERRED_RSI_SELL:.2f}\n"
        f"Preferred BB alerts: {bool_text(BB_ALERTS_ENABLED)}\nBB length: {BB_LENGTH}\nBB std mult: {BB_STD_MULT:.2f}\nBB squeeze alert <= {PREFERRED_BB_SQUEEZE_WIDTH_PCT:.2f}%\nBB expansion alert >= {PREFERRED_BB_EXPANSION_WIDTH_PCT:.2f}%\n"
        f"Stop loss: {pct_text(STOP_LOSS_PCT)}\nTake profit: {pct_text(TAKE_PROFIT_PCT)}\nTrailing stop: {pct_text(TRAILING_STOP_PCT)}\n"
        f"Trailing activation: {pct_text(TRAILING_START_PCT)}\nBreak-even stop: {BREAK_EVEN_ENABLED}\nBreak-even trigger: {pct_text(BREAK_EVEN_TRIGGER_PCT)}\nBreak-even offset: {pct_text(BREAK_EVEN_OFFSET_PCT)}\nMax open positions: {MAX_OPEN_POSITIONS}\nSymbol cooldown: {SYMBOL_COOLDOWN_SECONDS}s\n"
        f"Trade size margin: {TRADE_SIZE_USDT:.4f} USDT\nLeverage: {LEVERAGE}x\nApprox position notional/trade: {(TRADE_SIZE_USDT * LEVERAGE):.4f} USDT\n"
        f"Daily report enabled: {DAILY_REPORT_ENABLED}\nDaily report hour: {DAILY_REPORT_HOUR}\nCurrent open positions: {get_open_positions_count()}"
    )

def get_pnl_summary_data(day_filter=None):
    with trades_lock:
        trades = list(closed_trades)
    if day_filter:
        trades = [t for t in trades if today_str_from_ts(t["closed_at"]) == day_filter]
    total = len(trades)
    wins = len([t for t in trades if t["pnl_usdt"] > 0])
    losses = len([t for t in trades if t["pnl_usdt"] < 0])
    flat = total - wins - losses
    total_pnl = sum(t["pnl_usdt"] for t in trades)
    return {"trades": trades, "total": total, "wins": wins, "losses": losses, "flat": flat, "total_pnl": total_pnl, "best_trade": max(trades, key=lambda x: x["pnl_usdt"]) if trades else None, "worst_trade": min(trades, key=lambda x: x["pnl_usdt"]) if trades else None, "win_rate": 0.0 if total == 0 else wins / total * 100.0}

def get_pnl_text(day_filter=None):
    summary = get_pnl_summary_data(day_filter=day_filter)
    label = "TODAY" if day_filter else "ALL TIME"
    lines = [f"📊 PNL SUMMARY ({label})", f"Trades: {summary['total']}", f"Wins: {summary['wins']}", f"Losses: {summary['losses']}", f"Flat: {summary['flat']}", f"Win rate: {summary['win_rate']:.2f}%", f"Total PnL: {summary['total_pnl']:.8f} USDT"]
    if summary["best_trade"]:
        t = summary["best_trade"]
        lines.append(f"Best: {t['symbol']} {t['pnl_usdt']:.8f} USDT ({t['pnl_pct']:.2f}%)")
    if summary["worst_trade"]:
        t = summary["worst_trade"]
        lines.append(f"Worst: {t['symbol']} {t['pnl_usdt']:.8f} USDT ({t['pnl_pct']:.2f}%)")
    return "\n".join(lines)

def get_recent_trades_text(limit=10):
    with trades_lock:
        trades = list(closed_trades)[-limit:]
    if not trades:
        return "📭 No closed trades yet."
    trades.reverse()
    lines = ["🧾 RECENT CLOSED TRADES"]
    for t in trades:
        lines.append(f"{t['symbol']} | pnl={t['pnl_usdt']:.8f} USDT ({t['pnl_pct']:.2f}%) | reason={t['reason']} | entry={t['entry_price']:.8f} | exit={t['exit_price']:.8f} | qty={t['quantity']} | closed={ts_to_text(t['closed_at'])}")
    return "\n".join(lines[:25])

# =========================================================
# MARKET DISCOVERY / HISTORY
# =========================================================
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
        if last_price is None or last_price <= 0 or turnover_24h is None or turnover_24h <= 0:
            continue
        change_1h = ((last_price - prev_price_1h) / prev_price_1h) * 100 if prev_price_1h and prev_price_1h > 0 else 0.0
        candidates.append((symbol, abs(change_1h), turnover_24h))
    candidates.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return [s for s, _, _ in candidates[:TOP_N]]

def pick_live_symbols(symbols):
    return list(symbols[:LIVE_WS_SYMBOLS])

def fetch_history(symbol: str, tf: str):
    try:
        symbol = normalize_symbol(symbol)
        tf = normalize_interval(tf)
        ensure_symbol_state(symbol)
        if tf not in data[symbol]:
            data[symbol][tf] = []
        response = session.get_kline(category=CATEGORY, symbol=symbol, interval=tf, limit=HISTORY_LIMIT)
        rows = response.get("result", {}).get("list", [])
        if not rows:
            print(f"No history rows for {symbol} {tf}")
            return
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
        print(f"History fetch error {symbol} {tf}: {e}")

def refresh_shortlist_and_history():
    global shortlist, live_symbols, last_shortlist_refresh_at
    print("Refreshing shortlist...")
    symbols = build_shortlist_from_tickers()
    selected_live_symbols = pick_live_symbols(symbols)
    new_shortlist, new_live_symbols = set(symbols), set(selected_live_symbols)
    with shortlist_lock:
        old_live = set(live_symbols)
    added_symbols = sorted(list(new_live_symbols - old_live))
    for symbol in added_symbols:
        ensure_symbol_state(symbol)
        for tf in TIMEFRAMES:
            fetch_history(symbol, tf)
            if REQUEST_SLEEP_SECONDS > 0:
                time.sleep(REQUEST_SLEEP_SECONDS)
    required = max(20, BB_LENGTH + 1, 15)
    for symbol in selected_live_symbols:
        ensure_symbol_state(symbol)
        for tf in TIMEFRAMES:
            if len(data[symbol][tf]) < required:
                fetch_history(symbol, tf)
    with shortlist_lock:
        shortlist = new_shortlist
        live_symbols = new_live_symbols
    last_shortlist_refresh_at = int(time.time())
    print(f"Shortlist refreshed. shortlist={len(new_shortlist)} live={len(new_live_symbols)}")

def get_top_gainers_from_history(tf: str, top_n: int):
    changes = []
    with shortlist_lock:
        current_live = list(live_symbols)
    for symbol in current_live:
        arr = data.get(symbol, {}).get(tf, [])
        if len(arr) < 2:
            continue
        prev_close, last_close = arr[-2], arr[-1]
        if prev_close > 0:
            changes.append((symbol, ((last_close - prev_close) / prev_close) * 100))
    changes.sort(key=lambda x: x[1], reverse=True)
    return [symbol for symbol, _ in changes[:top_n]]

# =========================================================
# RSI / BB QUERY COMMANDS
# =========================================================
def get_symbol_rsi(symbol: str, tf: str = "5", length: int = 14):
    symbol = normalize_symbol(symbol)
    tf = normalize_interval(tf)
    arr = data.get(symbol, {}).get(tf, [])
    if len(arr) < length + 1:
        return None
    return rsi(arr, length)

def get_symbol_bb(symbol: str, tf: str = "5", length: int = BB_LENGTH, std_mult: float = BB_STD_MULT):
    symbol = normalize_symbol(symbol)
    tf = normalize_interval(tf)
    arr = data.get(symbol, {}).get(tf, [])
    if len(arr) < length:
        return None
    return bollinger_bands(arr, length, std_mult)

def get_any_symbol_rsi_text(symbol: str, tf: str = "5", length: int = 14):
    symbol = normalize_symbol(symbol)
    tf = normalize_interval(tf)
    try:
        ensure_symbol_state(symbol)
        if tf not in data[symbol]:
            data[symbol][tf] = []
        if len(data.get(symbol, {}).get(tf, [])) < length + 1:
            fetch_history(symbol, tf)
        arr = data.get(symbol, {}).get(tf, [])
        if len(arr) < length + 1:
            return f"⚠️ Not enough candle data for {symbol} {tf}m."
        value = rsi(arr, length)
        if value is None:
            return f"⚠️ RSI unavailable for {symbol}."
        price = arr[-1]
        state = "BUY ZONE" if value <= PREFERRED_RSI_BUY else "SELL ZONE" if value >= PREFERRED_RSI_SELL else "NORMAL"
        return f"📊 RSI CHECK\nSymbol: {symbol}\nTimeframe: {tf}m\nRSI({length}): {value:.2f}\nLast close: {price}\nState: {state}\nYour buy alert: <= {PREFERRED_RSI_BUY:.2f}\nYour sell alert: >= {PREFERRED_RSI_SELL:.2f}"
    except Exception as e:
        return f"❌ Failed to check RSI for {symbol}\n{e}"

def get_any_symbol_bb_text(symbol: str, tf: str = "5", length: int = BB_LENGTH, std_mult: float = BB_STD_MULT):
    symbol = normalize_symbol(symbol)
    tf = normalize_interval(tf)
    try:
        ensure_symbol_state(symbol)
        if tf not in data[symbol]:
            data[symbol][tf] = []
        if len(data.get(symbol, {}).get(tf, [])) < length:
            fetch_history(symbol, tf)
        arr = data.get(symbol, {}).get(tf, [])
        bb = bollinger_bands(arr, length, std_mult)
        if bb is None:
            return f"⚠️ Not enough candle data for BB on {symbol} {tf}m."
        state = "CLOSE EXPANSION / SQUEEZE" if bb["width_pct"] <= PREFERRED_BB_SQUEEZE_WIDTH_PCT else "WIDEST EXPANSION" if bb["width_pct"] >= PREFERRED_BB_EXPANSION_WIDTH_PCT else "NORMAL"
        return (
            f"📊 BB WIDTH CHECK\nSymbol: {symbol}\nTimeframe: {tf}m\nBB({length}, {std_mult:g})\n"
            f"Upper: {bb['upper']:.8f}\nMiddle: {bb['middle']:.8f}\nLower: {bb['lower']:.8f}\nWidth: {bb['width_pct']:.2f}%\nState: {state}\n"
            f"Your close expansion/squeeze: <= {PREFERRED_BB_SQUEEZE_WIDTH_PCT:.2f}%\nYour widest expansion: >= {PREFERRED_BB_EXPANSION_WIDTH_PCT:.2f}%"
        )
    except Exception as e:
        return f"❌ Failed to check BB for {symbol}\n{e}"

def get_rsi_rankings_text(mode: str = "low", limit: int = 10):
    mode = mode.lower().strip()
    with shortlist_lock:
        symbols = sorted(list(live_symbols))
    if not symbols:
        return "⚠️ No live symbols loaded yet."
    rows = []
    for symbol in symbols:
        value = get_symbol_rsi(symbol, "5", 14)
        if value is not None:
            rows.append((symbol, value))
    if not rows:
        return "⚠️ No RSI values available yet."
    reverse = mode in ("high", "highest")
    rows.sort(key=lambda x: x[1], reverse=reverse)
    selected = rows[:limit]
    title = "🔥 HIGHEST RSI COINS" if reverse else "🧊 LOWEST RSI COINS"
    lines = [title, "Timeframe: 5m", "RSI Length: 14", f"Your buy alert <= {PREFERRED_RSI_BUY:.2f} | sell alert >= {PREFERRED_RSI_SELL:.2f}", ""]
    for i, (symbol, value) in enumerate(selected, start=1):
        flag = " ✅ ALERT ZONE" if (value <= PREFERRED_RSI_BUY or value >= PREFERRED_RSI_SELL) else ""
        lines.append(f"{i}. {symbol} — RSI: {value:.2f}{flag}")
    return "\n".join(lines)

def get_bb_rankings_text(mode: str = "low", limit: int = BB_RANK_LIMIT_DEFAULT):
    mode = mode.lower().strip()
    with shortlist_lock:
        symbols = sorted(list(live_symbols))
    if not symbols:
        return "⚠️ No live symbols loaded yet."
    rows = []
    for symbol in symbols:
        bb = get_symbol_bb(symbol, "5", BB_LENGTH, BB_STD_MULT)
        if bb is not None:
            rows.append((symbol, bb["width_pct"]))
    if not rows:
        return "⚠️ No BB width values available yet."
    reverse = mode in ("high", "highest", "expansion", "wide", "widest")
    rows.sort(key=lambda x: x[1], reverse=reverse)
    selected = rows[:limit]
    title = "🟢 WIDEST BB EXPANSION" if reverse else "🟡 CLOSEST BB EXPANSION / SQUEEZE"
    lines = [title, "Timeframe: 5m", f"BB: length={BB_LENGTH}, std={BB_STD_MULT:g}", f"Your squeeze <= {PREFERRED_BB_SQUEEZE_WIDTH_PCT:.2f}% | expansion >= {PREFERRED_BB_EXPANSION_WIDTH_PCT:.2f}%", ""]
    for i, (symbol, width) in enumerate(selected, start=1):
        flag = " ✅ ALERT ZONE" if (width <= PREFERRED_BB_SQUEEZE_WIDTH_PCT or width >= PREFERRED_BB_EXPANSION_WIDTH_PCT) else ""
        lines.append(f"{i}. {symbol} — BB width: {width:.2f}%{flag}")
    return "\n".join(lines)

# =========================================================
# SIGNALS / WEBSOCKET
# =========================================================
def process_indicators(symbol: str, tf: str, candle_start: int):
    arr = data.get(symbol, {}).get(tf, [])
    if not arr:
        return
    cfg = get_effective_settings(symbol, tf)
    rsi_cfg = cfg.get("rsi", {})
    if rsi_cfg.get("enabled", False):
        value = rsi(arr, int(rsi_cfg.get("length", 14)))
        if value is not None:
            process_preferred_rsi_alerts(symbol, tf, candle_start, value)
    bb_cfg = cfg.get("bb", {})
    if bb_cfg.get("enabled", True):
        bb = bollinger_bands(arr, int(bb_cfg.get("length", BB_LENGTH)), float(bb_cfg.get("std_mult", BB_STD_MULT)))
        if bb is not None:
            process_preferred_bb_alerts(symbol, tf, candle_start, bb)

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
        topic_symbol, topic_interval = "", ""
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
            symbol = normalize_symbol(str(item.get("symbol") or topic_symbol or ""))
            interval = normalize_interval(item.get("interval") or topic_interval or "")
            close = safe_float(item.get("close"))
            start_time = safe_int(item.get("start"))
            confirm = bool(item.get("confirm", False))
            if not symbol or not interval or close is None or start_time is None:
                continue
            if symbol not in current_live_symbols or interval not in TIMEFRAMES:
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
    with shortlist_lock:
        symbols = sorted(list(live_symbols))
    if not symbols:
        print(f"No symbols to subscribe for tf={tf}")
        return None
    print(f"Subscribing {len(symbols)} symbols for tf={tf}")
    for symbol in symbols:
        try:
            ws.kline_stream(interval=int(tf), symbol=symbol, callback=handle_kline)
            if REQUEST_SLEEP_SECONDS > 0:
                time.sleep(min(REQUEST_SLEEP_SECONDS, 0.02))
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

# =========================================================
# ORDER HELPERS
# =========================================================
def place_bybit_market_buy(symbol: str):
    symbol = normalize_symbol(symbol)
    if CATEGORY != "linear":
        raise Exception("This version is built for BYBIT_CATEGORY=linear")
    if not BYBIT_TRADING_ENABLED:
        raise Exception("BYBIT_TRADING_ENABLED is false")
    if not bybit_keys_ready():
        raise Exception("Bybit API keys missing")
    if symbol_in_position(symbol):
        raise Exception(f"Already in tracked position for {symbol}")
    exchange_pos = get_bybit_position(symbol)
    if exchange_pos and str(exchange_pos.get("side", "")) == "Buy":
        raise Exception(f"Exchange already shows an open Buy position for {symbol}")
    if get_open_positions_count() >= MAX_OPEN_POSITIONS:
        raise Exception("Max open positions reached")
    if get_bybit_total_available_balance_usd() <= 0:
        raise Exception("No available balance on Bybit account")
    target_leverage = ensure_bybit_leverage(symbol)
    last_price = get_bybit_last_price(symbol)
    qty_str, qty, _ = compute_bybit_order_qty(symbol, last_price)
    order = session.place_order(category=CATEGORY, symbol=symbol, side="Buy", orderType="Market", qty=qty_str)
    time.sleep(1.0)
    if not sync_local_position_from_exchange(symbol):
        set_position(symbol, get_bybit_last_price(symbol), qty, "buy_fallback_market")
    return order, target_leverage

def place_bybit_market_sell(symbol: str, reason: str = "manual_sell"):
    symbol = normalize_symbol(symbol)
    if CATEGORY != "linear":
        raise Exception("This version is built for BYBIT_CATEGORY=linear")
    if not BYBIT_TRADING_ENABLED:
        raise Exception("BYBIT_TRADING_ENABLED is false")
    if not bybit_keys_ready():
        raise Exception("Bybit API keys missing")
    pos = get_bybit_position(symbol)
    if not pos:
        raise Exception(f"No open exchange position found for {symbol}")
    side = str(pos.get("side", "")).strip()
    size = safe_float(pos.get("size"), 0.0)
    if side != "Buy" or size <= 0:
        raise Exception(f"No open long position to close for {symbol}")
    instrument = get_bybit_instrument_info(symbol)
    qty_step = str(instrument.get("lotSizeFilter", {}).get("qtyStep", "0.001"))
    qty_str = fmt_qty_or_price(floor_to_step_str(size, qty_step))
    if safe_float(qty_str, 0.0) <= 0:
        raise Exception(f"Rounded close quantity became 0 for {symbol}")
    order = session.place_order(category=CATEGORY, symbol=symbol, side="Sell", orderType="Market", qty=qty_str, reduceOnly=True)
    time.sleep(1.0)
    trade = close_local_position_and_record(symbol, get_bybit_last_price(symbol), reason)
    return order, trade

def panic_close_all_positions():
    if CATEGORY != "linear":
        raise Exception("This version is built for BYBIT_CATEGORY=linear")
    if not BYBIT_TRADING_ENABLED:
        raise Exception("BYBIT_TRADING_ENABLED is false")
    if not bybit_keys_ready():
        raise Exception("Bybit API keys missing")
    exchange_positions = get_bybit_all_open_long_positions()
    if not exchange_positions:
        return [], []
    closed_symbols, errors = [], []
    for pos in exchange_positions:
        symbol = str(pos.get("symbol", "")).upper()
        try:
            sync_local_position_from_exchange(symbol)
            place_bybit_market_sell(symbol, reason="panic_close")
            closed_symbols.append(symbol)
            time.sleep(0.3)
        except Exception as e:
            errors.append(f"{symbol}: {e}")
    return closed_symbols, errors

# =========================================================
# AUTO TRADING
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
            symbol_rsi_list = []
            for symbol in candidates:
                if not symbol.endswith("USDT") or symbol_in_position(symbol) or symbol_on_cooldown(symbol) or not is_symbol_supported_on_bybit(symbol):
                    continue
                exchange_pos = get_bybit_position(symbol)
                if exchange_pos and str(exchange_pos.get("side", "")).strip() == "Buy":
                    continue
                value = get_symbol_rsi(symbol, "5", 14)
                if value is not None and value <= RSI_BUY_THRESHOLD:
                    symbol_rsi_list.append((symbol, value))
            symbol_rsi_list.sort(key=lambda x: x[1])
            for symbol, value in symbol_rsi_list:
                if get_open_positions_count() >= MAX_OPEN_POSITIONS:
                    break
                if symbol_in_position(symbol):
                    continue
                try:
                    order, used_lev = place_bybit_market_buy(symbol)
                    msg = f"🟢 AUTO BUY BYBIT MAINNET\nSymbol: {symbol}\nRSI(5m): {value:.2f}\nOrder ID: {order.get('result', {}).get('orderId')}\nTrade size margin: {TRADE_SIZE_USDT:.4f} USDT\nLeverage used: {used_lev}x\nApprox notional: {(TRADE_SIZE_USDT * used_lev):.4f} USDT"
                    with positions_lock:
                        pos = positions.get(symbol)
                        if pos:
                            sl = "OFF" if pos["stop_loss_price"] is None else f"{pos['stop_loss_price']:.8f}"
                            tp = "OFF" if pos["take_profit_price"] is None else f"{pos['take_profit_price']:.8f}"
                            ts = "OFF" if pos["trailing_stop_price"] is None else f"{pos['trailing_stop_price']:.8f}"
                            msg += f"\nEntry: {pos['entry_price']:.8f}\nQty: {pos['quantity']}\nStop loss: {sl}\nTake profit: {tp}\nTrailing stop: {ts}"
                    send_telegram(msg)
                    time.sleep(0.5)
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
                    current_price = get_bybit_last_price(symbol)
                    update_position_high_water(symbol, current_price)
                    with positions_lock:
                        refreshed = positions.get(symbol)
                        if not refreshed:
                            continue
                        sl = refreshed["stop_loss_price"]
                        tp = refreshed["take_profit_price"]
                        tsl = refreshed.get("trailing_stop_price")
                        highest = refreshed.get("highest_price")
                        trailing_active = refreshed.get("trailing_active", False)
                        entry = refreshed["entry_price"]
                        qty = refreshed["quantity"]
                    current_rsi = get_symbol_rsi(symbol, "5", 14)
                    if sl is not None and current_price <= sl:
                        order, trade = place_bybit_market_sell(symbol, reason="stop_loss")
                        pnl_text = "" if not trade else f"\nPnL: {trade['pnl_usdt']:.8f} USDT ({trade['pnl_pct']:.2f}%)"
                        send_telegram(f"🛑 STOP LOSS HIT\nSymbol: {symbol}\nEntry: {entry:.8f}\nExit trigger: {current_price:.8f}\nTracked qty: {qty}\nOrder ID: {order.get('result', {}).get('orderId')}{pnl_text}")
                        continue
                    if tp is not None and current_price >= tp:
                        order, trade = place_bybit_market_sell(symbol, reason="take_profit")
                        pnl_text = "" if not trade else f"\nPnL: {trade['pnl_usdt']:.8f} USDT ({trade['pnl_pct']:.2f}%)"
                        send_telegram(f"🎯 TAKE PROFIT HIT\nSymbol: {symbol}\nEntry: {entry:.8f}\nExit trigger: {current_price:.8f}\nTracked qty: {qty}\nOrder ID: {order.get('result', {}).get('orderId')}{pnl_text}")
                        continue
                    if trailing_active and tsl is not None and current_price <= tsl:
                        order, trade = place_bybit_market_sell(symbol, reason="trailing_stop")
                        pnl_text = "" if not trade else f"\nPnL: {trade['pnl_usdt']:.8f} USDT ({trade['pnl_pct']:.2f}%)"
                        send_telegram(f"🔒 TRAILING STOP HIT\nSymbol: {symbol}\nEntry: {entry:.8f}\nHighest: {highest:.8f}\nTrailing stop: {tsl:.8f}\nExit trigger: {current_price:.8f}\nTracked qty: {qty}\nOrder ID: {order.get('result', {}).get('orderId')}{pnl_text}")
                        continue
                    if current_rsi is not None and current_rsi >= RSI_SELL_THRESHOLD:
                        order, trade = place_bybit_market_sell(symbol, reason="rsi_exit")
                        pnl_text = "" if not trade else f"\nPnL: {trade['pnl_usdt']:.8f} USDT ({trade['pnl_pct']:.2f}%)"
                        send_telegram(f"🔴 AUTO SELL RSI EXIT\nSymbol: {symbol}\nRSI(5m): {current_rsi:.2f}\nEntry: {entry:.8f}\nExit trigger: {current_price:.8f}\nTracked qty: {qty}\nOrder ID: {order.get('result', {}).get('orderId')}{pnl_text}")
                        continue
                except Exception as e:
                    print(f"Auto exit failed for {symbol}: {e}")
        except Exception as e:
            print("Auto exit loop error:", e)
        time.sleep(RISK_CHECK_INTERVAL_SECONDS)

# =========================================================
# TELEGRAM COMMANDS
# =========================================================
def force_test_alerts():
    send_telegram(f"🟢 RSI BUY ALERT\nSymbol: BTCUSDT\nTimeframe: 5m\nRSI: {PREFERRED_RSI_BUY:.2f}\nYour buy value: <= {PREFERRED_RSI_BUY:.2f}")
    send_telegram(f"🔴 RSI SELL ALERT\nSymbol: BTCUSDT\nTimeframe: 5m\nRSI: {PREFERRED_RSI_SELL:.2f}\nYour sell value: >= {PREFERRED_RSI_SELL:.2f}")
    send_telegram(f"🟡 BB SQUEEZE ALERT\nSymbol: BTCUSDT\nTimeframe: 5m\nBB width: {PREFERRED_BB_SQUEEZE_WIDTH_PCT:.2f}%\nYour squeeze value: <= {PREFERRED_BB_SQUEEZE_WIDTH_PCT:.2f}%")
    send_telegram(f"🟢 BB EXPANSION ALERT\nSymbol: BTCUSDT\nTimeframe: 5m\nBB width: {PREFERRED_BB_EXPANSION_WIDTH_PCT:.2f}%\nYour expansion value: >= {PREFERRED_BB_EXPANSION_WIDTH_PCT:.2f}%")

def get_status_text():
    with shortlist_lock:
        symbols = list(shortlist)
        live = list(live_symbols)
    return (
        f"✅ Bot running\nCategory: {CATEGORY}\nTOP_N: {TOP_N}\nLIVE_WS_SYMBOLS: {LIVE_WS_SYMBOLS}\nTimeframes: {', '.join(TIMEFRAMES)}\n"
        f"Shortlist size: {len(symbols)}\nLive symbols: {len(live)}\nHistory limit: {HISTORY_LIMIT}\nTelegram enabled: {TELEGRAM_ENABLED}\nNotifications muted: {NOTIFICATIONS_MUTED}\n"
        f"Bybit trading enabled: {BYBIT_TRADING_ENABLED}\nBybit keys loaded: {bybit_keys_ready()}\nAuto trading enabled: {AUTO_TRADING_ENABLED}\nDaily report enabled: {DAILY_REPORT_ENABLED}\n"
        f"Current open positions: {get_open_positions_count()}\nMax open positions: {MAX_OPEN_POSITIONS}\nAuto buy RSI <= {RSI_BUY_THRESHOLD:.2f}\nAuto sell RSI >= {RSI_SELL_THRESHOLD:.2f}\n"
        f"Preferred RSI alerts: {bool_text(RSI_ALERTS_ENABLED)}\nPreferred RSI buy <= {PREFERRED_RSI_BUY:.2f}\nPreferred RSI sell >= {PREFERRED_RSI_SELL:.2f}\n"
        f"Preferred BB alerts: {bool_text(BB_ALERTS_ENABLED)}\nBB length: {BB_LENGTH}\nBB std mult: {BB_STD_MULT:.2f}\nPreferred close expansion/squeeze <= {PREFERRED_BB_SQUEEZE_WIDTH_PCT:.2f}%\nPreferred widest expansion >= {PREFERRED_BB_EXPANSION_WIDTH_PCT:.2f}%\n"
        f"SL: {pct_text(STOP_LOSS_PCT)}\nTP: {pct_text(TAKE_PROFIT_PCT)}\nTrailing: {pct_text(TRAILING_STOP_PCT)}\nTrail start: {pct_text(TRAILING_START_PCT)}\n"
        f"Break-even: {BREAK_EVEN_ENABLED}\nBE trigger: {pct_text(BREAK_EVEN_TRIGGER_PCT)}\nBE offset: {pct_text(BREAK_EVEN_OFFSET_PCT)}\n"
        f"Symbol cooldown: {SYMBOL_COOLDOWN_SECONDS}s\nTrade size margin: {TRADE_SIZE_USDT:.4f} USDT\nLeverage: {LEVERAGE}x\nApprox notional/trade: {(TRADE_SIZE_USDT * LEVERAGE):.4f} USDT"
    )

def get_symbols_text():
    with shortlist_lock:
        syms = sorted(list(live_symbols))
    return f"📊 Live symbol count: {len(syms)}\nSample:\n" + "\n".join(syms[:60])

def get_diag_text():
    now = int(time.time())
    last_ws_age = now - last_ws_message_at if last_ws_message_at else -1
    last_refresh_age = now - last_shortlist_refresh_at if last_shortlist_refresh_at else -1
    with ws_lock:
        ws_keys = list(ws_connections.keys())
    with bybit_symbols_lock:
        bybit_count = len(bybit_linear_symbols)
    with trades_lock:
        trades_count = len(closed_trades)
    return (
        f"🛠 DIAG\nws_keys: {ws_keys}\nlast_ws_message_age_sec: {last_ws_age}\nlast_shortlist_refresh_age_sec: {last_refresh_age}\nWS_DEBUG: {WS_DEBUG}\n"
        f"LIVE_WS_SYMBOLS: {LIVE_WS_SYMBOLS}\nBYBIT_TRADING_ENABLED: {BYBIT_TRADING_ENABLED}\nBYBIT_KEYS_READY: {bybit_keys_ready()}\nAUTO_TRADING_ENABLED: {AUTO_TRADING_ENABLED}\nNOTIFICATIONS_MUTED: {NOTIFICATIONS_MUTED}\n"
        f"RSI_ALERTS_ENABLED: {RSI_ALERTS_ENABLED}\nPREFERRED_RSI_BUY: {PREFERRED_RSI_BUY}\nPREFERRED_RSI_SELL: {PREFERRED_RSI_SELL}\n"
        f"BB_ALERTS_ENABLED: {BB_ALERTS_ENABLED}\nBB_LENGTH: {BB_LENGTH}\nBB_STD_MULT: {BB_STD_MULT}\nPREFERRED_BB_SQUEEZE_WIDTH_PCT: {PREFERRED_BB_SQUEEZE_WIDTH_PCT}\nPREFERRED_BB_EXPANSION_WIDTH_PCT: {PREFERRED_BB_EXPANSION_WIDTH_PCT}\n"
        f"STOP_LOSS_PCT: {STOP_LOSS_PCT}\nTAKE_PROFIT_PCT: {TAKE_PROFIT_PCT}\nTRAILING_STOP_PCT: {TRAILING_STOP_PCT}\nTRAILING_START_PCT: {TRAILING_START_PCT}\nBREAK_EVEN_ENABLED: {BREAK_EVEN_ENABLED}\nBREAK_EVEN_TRIGGER_PCT: {BREAK_EVEN_TRIGGER_PCT}\nBREAK_EVEN_OFFSET_PCT: {BREAK_EVEN_OFFSET_PCT}\nMAX_OPEN_POSITIONS: {MAX_OPEN_POSITIONS}\nTRADE_SIZE_USDT: {TRADE_SIZE_USDT}\n"
        f"LEVERAGE: {LEVERAGE}\nCLOSED_TRADES_COUNT: {trades_count}\nSUPPORTED_BYBIT_USDT_SYMBOLS: {bybit_count}\nTELEGRAM_QUEUE_SIZE: {telegram_queue.qsize()}"
    )

def telegram_help_text():
    return (
        "Available commands:\n"
        "/test - Telegram test\n/force - force fake RSI and BB alerts\n/status - bot status with all settings\n/symbols - live symbol sample\n/diag - websocket diagnostics\n"
        "/mute - mute bot notifications and alerts\n/unmute - unmute bot notifications and alerts\n"
        "/bybit - test Bybit connection\n/bbal - Bybit balances\n/buy - manual Bybit market buy for BYBIT_SYMBOL\n/sell - manual Bybit market sell for BYBIT_SYMBOL\n"
        "/panicclose - close all open long positions now\n/pnl - show all-time PnL summary\n/dailyreport - show today's PnL summary\n/trades - show recent closed trades\n"
        "/autoon - enable auto trading\n/autooff - disable auto trading\n/showstrategy - show auto strategy\n/showpositions - show tracked positions\n"
        "/rsi BTCUSDT - check RSI for any coin\n/rsi BTCUSDT 5 - check RSI for coin/timeframe\n/rsilow 10 - show lowest RSI coins watched\n/rsihigh 10 - show highest RSI coins watched\n"
        "/bb BTCUSDT - check BB width of any coin\n/bb BTCUSDT 5 - check BB width coin/timeframe\n/bb close 10 - closest BB expansion/squeeze list\n/bb wide 10 - widest BB expansion list\n"
        "/setrsialerts on|off - enable or disable RSI alerts\n/setrsibuyalert 5 - set preferred RSI buy alert value\n/setrsisellalert 95 - set preferred RSI sell alert value\n"
        "/setbbalerts on|off - enable or disable BB alerts\n/setbbclose 1.5 - set close expansion/squeeze BB width %\n/setbbwide 4.0 - set widest expansion BB width %\n"
        "/setrsibuy 5 - set auto-trade buy threshold\n/setrsisell 95 - set auto-trade sell threshold\n/setsl 0 - set stop loss percent, 0 OFF\n/settp 3 - set take profit percent, 0 OFF\n"
        "/settrailing 2 - set trailing stop percent, 0 OFF\n/settrailstart 1 - set trailing activation percent\n/setbe on|off - break-even stop\n/setbetrigger 1 - break-even trigger %\n/setbeoffset 0.1 - break-even offset %\n"
        "/setmaxcoins 5 - max simultaneous open positions\n/setcooldown 300 - symbol cooldown seconds\n/setsize 20 - trade size margin USDT\n/setlev 3 - leverage\n/help - command list"
    )

def parse_on_off(value: str) -> bool:
    v = value.strip().lower()
    if v in ("on", "true", "1", "yes"):
        return True
    if v in ("off", "false", "0", "no"):
        return False
    raise Exception("Use on or off")

def telegram_command_loop():
    global telegram_offset, last_command_time
    global AUTO_TRADING_ENABLED, NOTIFICATIONS_MUTED, STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_STOP_PCT, TRAILING_START_PCT
    global BREAK_EVEN_ENABLED, BREAK_EVEN_TRIGGER_PCT, BREAK_EVEN_OFFSET_PCT
    global RSI_BUY_THRESHOLD, RSI_SELL_THRESHOLD, MAX_OPEN_POSITIONS, SYMBOL_COOLDOWN_SECONDS, TRADE_SIZE_USDT, LEVERAGE
    global RSI_ALERTS_ENABLED, PREFERRED_RSI_BUY, PREFERRED_RSI_SELL, BB_ALERTS_ENABLED, PREFERRED_BB_SQUEEZE_WIDTH_PCT, PREFERRED_BB_EXPANSION_WIDTH_PCT
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
                allowed_chat_ids = {str(CHAT_ID).strip(), str(GROUP_CHAT_ID).strip()}
                allowed_chat_ids = {x for x in allowed_chat_ids if x}
                if allowed_chat_ids and from_chat_id not in allowed_chat_ids:
                    print(f"Ignoring command from unauthorized chat: {from_chat_id}")
                    continue
                print(f"Telegram command received: {text}")
                now = time.time()
                if now - last_command_time < 1:
                    time.sleep(1)
                last_command_time = now
                lower = text.lower().strip()

                if lower == "/test":
                    reply_telegram("✅ Telegram command test OK")
                elif lower == "/mute":
                    NOTIFICATIONS_MUTED = True
                    reply_telegram("🔇 Bot notifications muted. Commands still reply. Use /unmute to turn alerts back on.")
                elif lower == "/unmute":
                    NOTIFICATIONS_MUTED = False
                    reply_telegram("🔔 Bot notifications unmuted.")
                elif lower == "/force":
                    force_test_alerts()
                    reply_telegram("✅ Force test sent. If muted, only this confirmation may appear until /unmute.")
                elif lower == "/status":
                    reply_telegram(get_status_text())
                elif lower == "/symbols":
                    reply_telegram(get_symbols_text())
                elif lower == "/diag":
                    reply_telegram(get_diag_text())
                elif lower in ("/bybit", "/binance"):
                    test_bybit_connection(force_reply=True)
                elif lower == "/bbal":
                    try:
                        reply_telegram(get_bybit_nonzero_balances_text())
                    except Exception as e:
                        reply_telegram(f"❌ Bybit balance check failed\n{e}")
                elif lower == "/buy":
                    try:
                        order, used_lev = place_bybit_market_buy(BYBIT_SYMBOL)
                        sync_local_position_from_exchange(BYBIT_SYMBOL)
                        msg = f"✅ Bybit MAINNET BUY placed\nSymbol: {BYBIT_SYMBOL}\nOrder ID: {order.get('result', {}).get('orderId')}\nTrade size margin: {TRADE_SIZE_USDT:.4f} USDT\nLeverage used: {used_lev}x\nApprox notional: {(TRADE_SIZE_USDT * used_lev):.4f} USDT"
                        with positions_lock:
                            pos = positions.get(BYBIT_SYMBOL)
                            if pos:
                                msg += f"\nAvg entry: {pos['entry_price']:.8f}\nQty: {pos['quantity']}"
                        reply_telegram(msg)
                    except Exception as e:
                        reply_telegram(f"❌ Bybit MAINNET BUY failed\n{e}")
                elif lower == "/sell":
                    try:
                        order, trade = place_bybit_market_sell(BYBIT_SYMBOL, reason="manual_sell")
                        pnl_text = "" if not trade else f"\nPnL: {trade['pnl_usdt']:.8f} USDT ({trade['pnl_pct']:.2f}%)"
                        reply_telegram(f"✅ Bybit MAINNET SELL placed\nSymbol: {BYBIT_SYMBOL}\nOrder ID: {order.get('result', {}).get('orderId')}{pnl_text}")
                    except Exception as e:
                        reply_telegram(f"❌ Bybit MAINNET SELL failed\n{e}")
                elif lower == "/panicclose":
                    try:
                        closed_symbols, errors = panic_close_all_positions()
                        if not closed_symbols and not errors:
                            reply_telegram("📭 PANIC CLOSE: No open long positions found.")
                        else:
                            msg = "🚨 PANIC CLOSE ACTIVATED\n"
                            if closed_symbols:
                                msg += "Closed:\n" + "\n".join(closed_symbols[:50])
                            if errors:
                                msg += "\n\nErrors:\n" + "\n".join(errors[:20])
                            reply_telegram(msg)
                    except Exception as e:
                        reply_telegram(f"❌ PANIC CLOSE failed\n{e}")
                elif lower == "/pnl":
                    reply_telegram(get_pnl_text())
                elif lower == "/dailyreport":
                    reply_telegram(get_pnl_text(day_filter=today_str_from_ts(now_ts())))
                elif lower == "/trades":
                    reply_telegram(get_recent_trades_text())
                elif lower == "/autoon":
                    AUTO_TRADING_ENABLED = True
                    reply_telegram("✅ Auto trading enabled")
                elif lower == "/autooff":
                    AUTO_TRADING_ENABLED = False
                    reply_telegram("⏸ Auto trading disabled")
                elif lower == "/showstrategy":
                    reply_telegram(get_strategy_text())
                elif lower == "/showpositions":
                    reply_telegram(get_positions_text())
                elif lower.startswith("/rsi "):
                    try:
                        parts = text.split()
                        symbol = parts[1].strip().upper()
                        tf = normalize_interval(parts[2]) if len(parts) >= 3 else "5"
                        reply_telegram(get_any_symbol_rsi_text(symbol, tf=tf, length=14))
                    except Exception as e:
                        reply_telegram(f"❌ Usage: /rsi BTCUSDT\n{e}")
                elif lower.startswith("/rsilow"):
                    try:
                        parts = text.split()
                        limit = int(parts[1]) if len(parts) >= 2 else 10
                        reply_telegram(get_rsi_rankings_text(mode="low", limit=max(1, min(limit, 50))))
                    except Exception as e:
                        reply_telegram(f"❌ Usage: /rsilow 10\n{e}")
                elif lower.startswith("/rsihigh"):
                    try:
                        parts = text.split()
                        limit = int(parts[1]) if len(parts) >= 2 else 10
                        reply_telegram(get_rsi_rankings_text(mode="high", limit=max(1, min(limit, 50))))
                    except Exception as e:
                        reply_telegram(f"❌ Usage: /rsihigh 10\n{e}")
                elif lower.startswith("/bb"):
                    try:
                        parts = text.split()
                        if len(parts) >= 2 and parts[1].lower() in ("low", "lowest", "squeeze", "close", "closed", "narrow"):
                            limit = int(parts[2]) if len(parts) >= 3 else BB_RANK_LIMIT_DEFAULT
                            reply_telegram(get_bb_rankings_text(mode="low", limit=max(1, min(limit, 50))))
                        elif len(parts) >= 2 and parts[1].lower() in ("high", "highest", "expansion", "wide", "widest"):
                            limit = int(parts[2]) if len(parts) >= 3 else BB_RANK_LIMIT_DEFAULT
                            reply_telegram(get_bb_rankings_text(mode="high", limit=max(1, min(limit, 50))))
                        elif len(parts) >= 2:
                            symbol = parts[1].strip().upper()
                            tf = normalize_interval(parts[2]) if len(parts) >= 3 else "5"
                            reply_telegram(get_any_symbol_bb_text(symbol, tf=tf, length=BB_LENGTH, std_mult=BB_STD_MULT))
                        else:
                            reply_telegram("❌ Usage:\n/bb BTCUSDT\n/bb BTCUSDT 5\n/bb close 10\n/bb wide 10")
                    except Exception as e:
                        reply_telegram(f"❌ BB command failed\nUsage: /bb close 10 or /bb wide 10 or /bb BTCUSDT\n{e}")
                elif lower.startswith("/setrsialerts "):
                    try:
                        RSI_ALERTS_ENABLED = parse_on_off(text.split(maxsplit=1)[1])
                        reply_telegram(f"✅ RSI alerts: {bool_text(RSI_ALERTS_ENABLED)}")
                    except Exception as e:
                        reply_telegram(f"❌ Usage: /setrsialerts on or /setrsialerts off\n{e}")
                elif lower.startswith("/setrsibuyalert "):
                    try:
                        value = parse_float_command(text)
                        if not 0 <= value <= 100:
                            raise Exception("RSI value must be between 0 and 100")
                        if value >= PREFERRED_RSI_SELL:
                            raise Exception(f"Buy alert must be lower than sell alert ({PREFERRED_RSI_SELL:.2f})")
                        PREFERRED_RSI_BUY = value
                        reply_telegram(f"✅ Preferred RSI buy alert updated\nBuy alert <= {PREFERRED_RSI_BUY:.2f}\nSell alert >= {PREFERRED_RSI_SELL:.2f}")
                    except Exception as e:
                        reply_telegram(f"❌ Usage: /setrsibuyalert 5\n{e}")
                elif lower.startswith("/setrsisellalert "):
                    try:
                        value = parse_float_command(text)
                        if not 0 <= value <= 100:
                            raise Exception("RSI value must be between 0 and 100")
                        if value <= PREFERRED_RSI_BUY:
                            raise Exception(f"Sell alert must be higher than buy alert ({PREFERRED_RSI_BUY:.2f})")
                        PREFERRED_RSI_SELL = value
                        reply_telegram(f"✅ Preferred RSI sell alert updated\nBuy alert <= {PREFERRED_RSI_BUY:.2f}\nSell alert >= {PREFERRED_RSI_SELL:.2f}")
                    except Exception as e:
                        reply_telegram(f"❌ Usage: /setrsisellalert 95\n{e}")
                elif lower.startswith("/setbbalerts "):
                    try:
                        BB_ALERTS_ENABLED = parse_on_off(text.split(maxsplit=1)[1])
                        reply_telegram(f"✅ BB alerts: {bool_text(BB_ALERTS_ENABLED)}")
                    except Exception as e:
                        reply_telegram(f"❌ Usage: /setbbalerts on or /setbbalerts off\n{e}")
                elif lower.startswith("/setbbclose ") or lower.startswith("/setbbwidthlow "):
                    try:
                        value = parse_float_command(text)
                        if value < 0:
                            raise Exception("BB close/squeeze width cannot be negative")
                        if value >= PREFERRED_BB_EXPANSION_WIDTH_PCT:
                            raise Exception(f"Close/squeeze width must be lower than wide expansion width ({PREFERRED_BB_EXPANSION_WIDTH_PCT:.2f}%)")
                        PREFERRED_BB_SQUEEZE_WIDTH_PCT = value
                        reply_telegram(f"✅ BB close expansion/squeeze updated\nClose/squeeze <= {PREFERRED_BB_SQUEEZE_WIDTH_PCT:.2f}%\nWidest expansion >= {PREFERRED_BB_EXPANSION_WIDTH_PCT:.2f}%")
                    except Exception as e:
                        reply_telegram(f"❌ Usage: /setbbclose 1.5\n{e}")
                elif lower.startswith("/setbbwide ") or lower.startswith("/setbbwidthhigh "):
                    try:
                        value = parse_float_command(text)
                        if value < 0:
                            raise Exception("BB wide expansion width cannot be negative")
                        if value <= PREFERRED_BB_SQUEEZE_WIDTH_PCT:
                            raise Exception(f"Wide expansion width must be higher than close/squeeze width ({PREFERRED_BB_SQUEEZE_WIDTH_PCT:.2f}%)")
                        PREFERRED_BB_EXPANSION_WIDTH_PCT = value
                        reply_telegram(f"✅ BB widest expansion updated\nClose/squeeze <= {PREFERRED_BB_SQUEEZE_WIDTH_PCT:.2f}%\nWidest expansion >= {PREFERRED_BB_EXPANSION_WIDTH_PCT:.2f}%")
                    except Exception as e:
                        reply_telegram(f"❌ Usage: /setbbwide 4.0\n{e}")
                elif lower.startswith("/setrsibuy "):
                    try:
                        RSI_BUY_THRESHOLD = parse_float_command(text)
                        reply_telegram(f"✅ Auto-trade buy RSI threshold updated to {RSI_BUY_THRESHOLD:.2f}")
                    except Exception as e:
                        reply_telegram(f"❌ Failed to set auto-trade buy RSI threshold\n{e}")
                elif lower.startswith("/setrsisell "):
                    try:
                        RSI_SELL_THRESHOLD = parse_float_command(text)
                        reply_telegram(f"✅ Auto-trade sell RSI threshold updated to {RSI_SELL_THRESHOLD:.2f}")
                    except Exception as e:
                        reply_telegram(f"❌ Failed to set auto-trade sell RSI threshold\n{e}")
                elif lower.startswith("/setsl "):
                    try:
                        value = parse_float_command(text)
                        if value < 0:
                            raise Exception("Stop loss cannot be negative")
                        STOP_LOSS_PCT = value
                        refresh_all_position_risk_levels()
                        reply_telegram(f"✅ Stop loss updated\nStop loss: {pct_text(STOP_LOSS_PCT)}\nTake profit: {pct_text(TAKE_PROFIT_PCT)}\nTrailing stop: {pct_text(TRAILING_STOP_PCT)}\nTrail start: {pct_text(TRAILING_START_PCT)}")
                    except Exception as e:
                        reply_telegram(f"❌ Failed to set stop loss\n{e}")
                elif lower.startswith("/settp "):
                    try:
                        value = parse_float_command(text)
                        if value < 0:
                            raise Exception("Take profit cannot be negative")
                        TAKE_PROFIT_PCT = value
                        refresh_all_position_risk_levels()
                        reply_telegram(f"✅ Take profit updated\nStop loss: {pct_text(STOP_LOSS_PCT)}\nTake profit: {pct_text(TAKE_PROFIT_PCT)}\nTrailing stop: {pct_text(TRAILING_STOP_PCT)}\nTrail start: {pct_text(TRAILING_START_PCT)}")
                    except Exception as e:
                        reply_telegram(f"❌ Failed to set take profit\n{e}")
                elif lower.startswith("/settrailing "):
                    try:
                        value = parse_float_command(text)
                        if value < 0:
                            raise Exception("Trailing stop cannot be negative")
                        TRAILING_STOP_PCT = value
                        refresh_all_position_risk_levels()
                        reply_telegram(f"✅ Trailing stop updated\nStop loss: {pct_text(STOP_LOSS_PCT)}\nTake profit: {pct_text(TAKE_PROFIT_PCT)}\nTrailing stop: {pct_text(TRAILING_STOP_PCT)}\nTrail start: {pct_text(TRAILING_START_PCT)}")
                    except Exception as e:
                        reply_telegram(f"❌ Failed to set trailing stop\n{e}")
                elif lower.startswith("/settrailstart "):
                    try:
                        value = parse_float_command(text)
                        if value < 0:
                            raise Exception("Trailing activation cannot be negative")
                        TRAILING_START_PCT = value
                        refresh_all_position_risk_levels()
                        reply_telegram(f"✅ Trailing activation updated\nStop loss: {pct_text(STOP_LOSS_PCT)}\nTake profit: {pct_text(TAKE_PROFIT_PCT)}\nTrailing stop: {pct_text(TRAILING_STOP_PCT)}\nTrail start: {pct_text(TRAILING_START_PCT)}")
                    except Exception as e:
                        reply_telegram(f"❌ Failed to set trailing activation\n{e}")
                elif lower.startswith("/setbe "):
                    try:
                        BREAK_EVEN_ENABLED = parse_on_off(text.split(maxsplit=1)[1])
                        refresh_all_position_risk_levels()
                        reply_telegram(f"✅ Break-even stop updated\nBreak-even stop: {BREAK_EVEN_ENABLED}\nTrigger: {pct_text(BREAK_EVEN_TRIGGER_PCT)}\nOffset: {pct_text(BREAK_EVEN_OFFSET_PCT)}")
                    except Exception as e:
                        reply_telegram(f"❌ Failed to set break-even stop\n{e}")
                elif lower.startswith("/setbetrigger "):
                    try:
                        value = parse_float_command(text)
                        if value < 0:
                            raise Exception("Break-even trigger cannot be negative")
                        BREAK_EVEN_TRIGGER_PCT = value
                        refresh_all_position_risk_levels()
                        reply_telegram(f"✅ Break-even trigger updated\nBreak-even stop: {BREAK_EVEN_ENABLED}\nTrigger: {pct_text(BREAK_EVEN_TRIGGER_PCT)}\nOffset: {pct_text(BREAK_EVEN_OFFSET_PCT)}")
                    except Exception as e:
                        reply_telegram(f"❌ Failed to set break-even trigger\n{e}")
                elif lower.startswith("/setbeoffset "):
                    try:
                        value = parse_float_command(text)
                        if value < 0:
                            raise Exception("Break-even offset cannot be negative")
                        BREAK_EVEN_OFFSET_PCT = value
                        refresh_all_position_risk_levels()
                        reply_telegram(f"✅ Break-even offset updated\nBreak-even stop: {BREAK_EVEN_ENABLED}\nTrigger: {pct_text(BREAK_EVEN_TRIGGER_PCT)}\nOffset: {pct_text(BREAK_EVEN_OFFSET_PCT)}")
                    except Exception as e:
                        reply_telegram(f"❌ Failed to set break-even offset\n{e}")
                elif lower.startswith("/setmaxcoins "):
                    try:
                        value = int(text.split(maxsplit=1)[1].strip())
                        if value <= 0:
                            raise Exception("Max coins must be greater than 0")
                        MAX_OPEN_POSITIONS = value
                        reply_telegram(f"✅ Max open positions updated to {MAX_OPEN_POSITIONS}")
                    except Exception as e:
                        reply_telegram(f"❌ Failed to set max coins\n{e}")
                elif lower.startswith("/setcooldown "):
                    try:
                        value = int(text.split(maxsplit=1)[1].strip())
                        if value < 0:
                            raise Exception("Cooldown cannot be negative")
                        SYMBOL_COOLDOWN_SECONDS = value
                        reply_telegram(f"✅ Symbol cooldown updated to {SYMBOL_COOLDOWN_SECONDS} seconds")
                    except Exception as e:
                        reply_telegram(f"❌ Failed to set cooldown\n{e}")
                elif lower.startswith("/setsize "):
                    try:
                        value = parse_float_command(text)
                        if value <= 0:
                            raise Exception("Trade size must be greater than 0")
                        TRADE_SIZE_USDT = value
                        reply_telegram(f"✅ Trade size updated\nTrade size margin: {TRADE_SIZE_USDT:.4f} USDT\nLeverage: {LEVERAGE}x\nApprox notional/trade: {(TRADE_SIZE_USDT * LEVERAGE):.4f} USDT")
                    except Exception as e:
                        reply_telegram(f"❌ Failed to set trade size\n{e}")
                elif lower.startswith("/setlev "):
                    try:
                        value = int(text.split(maxsplit=1)[1].strip())
                        if value < 1:
                            raise Exception("Leverage must be at least 1")
                        LEVERAGE = value
                        reply_telegram(f"✅ Leverage updated\nTrade size margin: {TRADE_SIZE_USDT:.4f} USDT\nLeverage: {LEVERAGE}x\nApprox notional/trade: {(TRADE_SIZE_USDT * LEVERAGE):.4f} USDT\nNote: actual applied leverage may be capped per symbol by Bybit.")
                    except Exception as e:
                        reply_telegram(f"❌ Failed to set leverage\n{e}")
                elif lower == "/help":
                    reply_telegram(telegram_help_text())
            time.sleep(1)
        except Exception as e:
            print("Telegram command loop error:", e)
            time.sleep(5)

# =========================================================
# DAILY REPORT / LOOPS / MAIN
# =========================================================
def daily_report_loop():
    global last_daily_report_date
    while True:
        try:
            if DAILY_REPORT_ENABLED and TELEGRAM_ENABLED:
                now = datetime.now()
                current_date = now.strftime("%Y-%m-%d")
                if now.hour == DAILY_REPORT_HOUR and last_daily_report_date != current_date:
                    send_telegram(get_pnl_text(day_filter=current_date))
                    last_daily_report_date = current_date
        except Exception as e:
            print("Daily report loop error:", e)
        time.sleep(30)

def shortlist_refresh_loop():
    last_live_snapshot = set()
    while True:
        try:
            refresh_shortlist_and_history()
            with shortlist_lock:
                current_live = set(live_symbols)
            if current_live != last_live_snapshot:
                restart_websockets()
                last_live_snapshot = set(current_live)
            refresh_bybit_linear_symbols()
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
                time.sleep(3)
                continue
            top_5m = get_top_gainers_from_history("5", min(LIVE_WS_SYMBOLS, 20))
            print("Top 5m gainers:", top_5m[:10])
            time.sleep(20)
        except Exception as e:
            print("Scan loop error:", e)
            time.sleep(5)

def test_bybit_connection(force_reply: bool = False):
    sender = reply_telegram if force_reply else send_telegram
    if CATEGORY != "linear":
        print(f"Warning: current CATEGORY={CATEGORY}, but this bot is intended for linear.")
    if not bybit_keys_ready():
        print("Bybit keys missing")
        sender("❌ Bybit mainnet keys missing")
        return
    try:
        ticker = session.get_tickers(category=CATEGORY, symbol=BYBIT_SYMBOL)
        print("Bybit ticker OK:", bool(ticker))
        account = get_bybit_account_wallet()
        print("Bybit wallet OK:", bool(account))
        refresh_bybit_linear_symbols()
        sender(f"✅ Bybit MAINNET connected\nCategory: {CATEGORY}\nSymbol: {BYBIT_SYMBOL}\nTrading enabled: {BYBIT_TRADING_ENABLED}")
    except Exception as e:
        print("Bybit mainnet connection failed:", e)
        sender(f"❌ Bybit MAINNET connection failed\n{e}")

def main():
    print("Starting scanner...")
    print("TELEGRAM_ENABLED =", TELEGRAM_ENABLED)
    print("BOT_TOKEN set =", bool(BOT_TOKEN))
    print("CHAT_ID set =", bool(CHAT_ID))
    print("GROUP_CHAT_ID set =", bool(GROUP_CHAT_ID))
    print("CATEGORY =", CATEGORY)
    print("TOP_N =", TOP_N)
    print("LIVE_WS_SYMBOLS =", LIVE_WS_SYMBOLS)
    print("HISTORY_LIMIT =", HISTORY_LIMIT)
    print("SHORTLIST_REFRESH_SECONDS =", SHORTLIST_REFRESH_SECONDS)
    print("TIMEFRAMES =", TIMEFRAMES)
    print("WS_DEBUG =", WS_DEBUG)
    print("BYBIT_TRADING_ENABLED =", BYBIT_TRADING_ENABLED)
    print("BYBIT_SYMBOL =", BYBIT_SYMBOL)
    print("BYBIT_KEYS_READY =", bybit_keys_ready())
    print("BYBIT_TRADE_SIZE_USDT =", BYBIT_TRADE_SIZE_USDT)
    print("BYBIT_LEVERAGE =", BYBIT_LEVERAGE)
    print("DEFAULT_STOP_LOSS_PCT =", DEFAULT_STOP_LOSS_PCT)
    print("DEFAULT_TAKE_PROFIT_PCT =", DEFAULT_TAKE_PROFIT_PCT)
    print("DEFAULT_TRAILING_STOP_PCT =", DEFAULT_TRAILING_STOP_PCT)
    print("DEFAULT_TRAILING_START_PCT =", DEFAULT_TRAILING_START_PCT)
    print("DEFAULT_BREAK_EVEN_ENABLED =", DEFAULT_BREAK_EVEN_ENABLED)
    print("DEFAULT_BREAK_EVEN_TRIGGER_PCT =", DEFAULT_BREAK_EVEN_TRIGGER_PCT)
    print("DEFAULT_BREAK_EVEN_OFFSET_PCT =", DEFAULT_BREAK_EVEN_OFFSET_PCT)
    print("DEFAULT_RSI_BUY =", DEFAULT_RSI_BUY)
    print("DEFAULT_RSI_SELL =", DEFAULT_RSI_SELL)
    print("RSI_ALERTS_ENABLED =", RSI_ALERTS_ENABLED)
    print("PREFERRED_RSI_BUY =", PREFERRED_RSI_BUY)
    print("PREFERRED_RSI_SELL =", PREFERRED_RSI_SELL)
    print("BB_ALERTS_ENABLED =", BB_ALERTS_ENABLED)
    print("BB_LENGTH =", BB_LENGTH)
    print("BB_STD_MULT =", BB_STD_MULT)
    print("PREFERRED_BB_SQUEEZE_WIDTH_PCT =", PREFERRED_BB_SQUEEZE_WIDTH_PCT)
    print("PREFERRED_BB_EXPANSION_WIDTH_PCT =", PREFERRED_BB_EXPANSION_WIDTH_PCT)
    print("TRADES_HISTORY_LIMIT =", TRADES_HISTORY_LIMIT)
    print("DAILY_REPORT_ENABLED =", DAILY_REPORT_ENABLED)
    print("DAILY_REPORT_HOUR =", DAILY_REPORT_HOUR)
    print("DEFAULT_MAX_OPEN_POSITIONS =", DEFAULT_MAX_OPEN_POSITIONS)

    threading.Thread(target=telegram_sender_loop, daemon=True).start()
    send_telegram("🚀 Scanner started")
    test_bybit_connection()
    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(target=shortlist_refresh_loop, daemon=True).start()
    threading.Thread(target=telegram_command_loop, daemon=True).start()
    threading.Thread(target=auto_entry_loop, daemon=True).start()
    threading.Thread(target=auto_exit_loop, daemon=True).start()
    threading.Thread(target=daily_report_loop, daemon=True).start()
    scan_loop()

if __name__ == "__main__":
    main()
