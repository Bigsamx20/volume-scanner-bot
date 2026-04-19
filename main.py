import os
import time
import math
import threading
import requests
from copy import deepcopy
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from datetime import datetime
from pybit.unified_trading import HTTP, WebSocket

# =========================
# CONFIG
# =========================
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

CATEGORY = os.getenv("BYBIT_CATEGORY", "linear").strip().lower()
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
# BYBIT MAINNET CONFIG
# =========================
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "").strip()
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "").strip()
BYBIT_TRADING_ENABLED = os.getenv("BYBIT_TRADING_ENABLED", "false").lower() == "true"
BYBIT_SYMBOL = os.getenv("BYBIT_SYMBOL", "BTCUSDT").strip().upper()

BYBIT_TRADE_SIZE_USDT = float(os.getenv("BYBIT_TRADE_SIZE_USDT", "20"))
BYBIT_LEVERAGE = int(os.getenv("BYBIT_LEVERAGE", "1"))

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
# EXTREME RSI ALERT DEFAULTS
# =========================
DEFAULT_EXTREME_ALERTS_ENABLED = os.getenv("EXTREME_ALERTS_ENABLED", "true").lower() == "true"
DEFAULT_EXTREME_RSI_BUY = float(os.getenv("EXTREME_RSI_BUY", "10"))
DEFAULT_EXTREME_RSI_SELL = float(os.getenv("EXTREME_RSI_SELL", "90"))

# =========================
# PNL / REPORTING CONFIG
# =========================
TRADES_HISTORY_LIMIT = int(os.getenv("TRADES_HISTORY_LIMIT", "500"))
DAILY_REPORT_ENABLED = os.getenv("DAILY_REPORT_ENABLED", "false").lower() == "true"
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "21"))

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
# HELPERS: SESSION
# =========================
def create_session():
    if BYBIT_API_KEY and BYBIT_API_SECRET:
        return HTTP(
            testnet=False,
            api_key=BYBIT_API_KEY,
            api_secret=BYBIT_API_SECRET,
        )
    return HTTP(testnet=False)


session = create_session()

# =========================
# GLOBALS
# =========================
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

TRADE_SIZE_USDT = BYBIT_TRADE_SIZE_USDT
LEVERAGE = BYBIT_LEVERAGE

EXTREME_ALERTS_ENABLED = DEFAULT_EXTREME_ALERTS_ENABLED
EXTREME_RSI_BUY = DEFAULT_EXTREME_RSI_BUY
EXTREME_RSI_SELL = DEFAULT_EXTREME_RSI_SELL

# positions per symbol
positions = {}
# cooldown per symbol
symbol_cooldowns = {}

# closed trades
closed_trades = []
last_daily_report_date = None

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
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def now_ts():
    return int(time.time())


def ts_to_text(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def today_str_from_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

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


def process_extreme_rsi_alerts(symbol: str, tf: str, candle_start: int, rsi_value: float):
    if not EXTREME_ALERTS_ENABLED:
        return

    if rsi_value >= EXTREME_RSI_SELL:
        if should_alert_once_per_candle("extreme_rsi_sell", symbol, tf, candle_start):
            send_telegram(
                f"🔥 EXTREME SELL NOW\n"
                f"Symbol: {symbol}\n"
                f"Timeframe: {tf}\n"
                f"RSI: {rsi_value:.2f}\n"
                f"Extreme threshold: {EXTREME_RSI_SELL:.2f}"
            )

    if rsi_value <= EXTREME_RSI_BUY:
        if should_alert_once_per_candle("extreme_rsi_buy", symbol, tf, candle_start):
            send_telegram(
                f"🔥 EXTREME BUY NOW\n"
                f"Symbol: {symbol}\n"
                f"Timeframe: {tf}\n"
                f"RSI: {rsi_value:.2f}\n"
                f"Extreme threshold: {EXTREME_RSI_BUY:.2f}"
            )

# =========================
# BYBIT HELPERS
# =========================
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
                rows.append(
                    f"{coin.get('coin')}: equity={equity}, wallet={wallet_balance}, usd={coin.get('usdValue')}"
                )

    if not rows:
        return "No non-zero Bybit balances found."

    return "💰 Bybit balances\n" + "\n".join(rows[:20])


def get_bybit_total_available_balance_usd():
    account = get_bybit_account_wallet()
    root_list = account.get("result", {}).get("list", [])
    if not root_list:
        return 0.0

    total_available = safe_float(root_list[0].get("totalAvailableBalance"), 0.0)
    return total_available if total_available is not None else 0.0


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
        valid = set()
        cursor = None

        while True:
            params = {
                "category": CATEGORY,
                "limit": 1000,
            }
            if cursor:
                params["cursor"] = cursor

            response = session.get_instruments_info(**params)
            result = response.get("result", {})
            rows = result.get("list", [])

            for row in rows:
                symbol_name = str(row.get("symbol", "")).upper()
                status = str(row.get("status", ""))
                if status != "Trading":
                    continue
                if symbol_name.endswith("USDT"):
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
        if not bybit_linear_symbols:
            return True
        return symbol in bybit_linear_symbols


def get_bybit_position(symbol: str):
    response = session.get_positions(category=CATEGORY, symbol=symbol)
    rows = response.get("result", {}).get("list", [])

    if not rows:
        return None

    for row in rows:
        size = safe_float(row.get("size"), 0.0)
        side = str(row.get("side", "")).strip()
        if size and size > 0 and side in ("Buy", "Sell"):
            return row

    return None


def get_bybit_all_open_long_positions():
    positions_found = []
    cursor = None

    while True:
        params = {
            "category": CATEGORY,
            "limit": 200,
        }
        if cursor:
            params["cursor"] = cursor

        response = session.get_positions(**params)
        result = response.get("result", {})
        rows = result.get("list", [])

        for row in rows:
            size = safe_float(row.get("size"), 0.0)
            side = str(row.get("side", "")).strip()
            symbol = str(row.get("symbol", "")).upper()
            if symbol and side == "Buy" and size and size > 0:
                positions_found.append(row)

        cursor = result.get("nextPageCursor")
        if not cursor:
            break

    return positions_found


def get_bybit_position_size(symbol: str) -> float:
    pos = get_bybit_position(symbol)
    if not pos:
        return 0.0
    return safe_float(pos.get("size"), 0.0)


def get_bybit_position_entry_price(symbol: str):
    pos = get_bybit_position(symbol)
    if not pos:
        return None
    return safe_float(pos.get("avgPrice") or pos.get("avgEntryPrice"))


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
        raise Exception(
            f"Rounded quantity became 0 for {symbol}. Increase BYBIT_TRADE_SIZE_USDT or reduce precision issue."
        )

    if min_order_qty and qty < min_order_qty:
        raise Exception(
            f"Qty too small for {symbol}. qty={qty}, minOrderQty={min_order_qty}. "
            f"Increase BYBIT_TRADE_SIZE_USDT or leverage."
        )

    notional_check = qty * last_price
    if min_notional_value and notional_check < min_notional_value:
        raise Exception(
            f"Notional too small for {symbol}. notional={notional_check:.8f}, "
            f"minNotionalValue={min_notional_value}. Increase BYBIT_TRADE_SIZE_USDT or leverage."
        )

    if max_mkt_order_qty and qty > max_mkt_order_qty:
        raise Exception(
            f"Qty too large for {symbol}. qty={qty}, maxMktOrderQty={max_mkt_order_qty}. "
            f"Reduce BYBIT_TRADE_SIZE_USDT or leverage."
        )

    return qty_str, qty, instrument


def clamp_leverage_for_symbol(symbol: str, requested_leverage: int) -> int:
    instrument = get_bybit_instrument_info(symbol)
    lev_filter = instrument.get("leverageFilter", {})
    min_lev = safe_int(lev_filter.get("minLeverage"), 1)
    max_lev = safe_int(lev_filter.get("maxLeverage"), requested_leverage)

    leverage = requested_leverage
    if leverage < min_lev:
        leverage = min_lev
    if leverage > max_lev:
        leverage = max_lev
    return leverage


def ensure_bybit_leverage(symbol: str):
    target = clamp_leverage_for_symbol(symbol, LEVERAGE)

    try:
        response = session.set_leverage(
            category=CATEGORY,
            symbol=symbol,
            buyLeverage=str(target),
            sellLeverage=str(target),
        )
        print(f"Set leverage {symbol} -> {target}x:", response)
        return target
    except Exception as e:
        msg = str(e).lower()

        harmless_tokens = [
            "110043",
            "not modified",
            "same to the existing leverage",
        ]
        if any(token in msg for token in harmless_tokens):
            print(f"Leverage already set for {symbol}: {e}")
            return target

        raise

# =========================
# POSITION / PNL HELPERS
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
    closed_at = now_ts()

    trade = {
        "symbol": symbol,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "quantity": quantity,
        "pnl_usdt": pnl_usdt,
        "pnl_pct": pnl_pct,
        "reason": reason,
        "opened_at": opened_at,
        "closed_at": closed_at,
    }

    with trades_lock:
        closed_trades.append(trade)
        if len(closed_trades) > TRADES_HISTORY_LIMIT:
            del closed_trades[0:len(closed_trades) - TRADES_HISTORY_LIMIT]

    return trade


def remove_position(symbol: str):
    with positions_lock:
        if symbol in positions:
            del positions[symbol]
    set_symbol_cooldown(symbol)


def close_local_position_and_record(symbol: str, exit_price: float, reason: str):
    trade = record_closed_trade(symbol, exit_price, reason)
    remove_position(symbol)
    return trade


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
        f"Exchange trading: Bybit MAINNET ({CATEGORY})\n"
        f"Buy rule: 5m RSI <= {RSI_BUY_THRESHOLD:.2f}\n"
        f"Sell rule: 5m RSI >= {RSI_SELL_THRESHOLD:.2f} OR SL/TP/Trailing\n"
        f"Extreme alerts enabled: {EXTREME_ALERTS_ENABLED}\n"
        f"Extreme buy RSI <= {EXTREME_RSI_BUY:.2f}\n"
        f"Extreme sell RSI >= {EXTREME_RSI_SELL:.2f}\n"
        f"Stop loss: {pct_text(STOP_LOSS_PCT)}\n"
        f"Take profit: {pct_text(TAKE_PROFIT_PCT)}\n"
        f"Trailing stop: {pct_text(TRAILING_STOP_PCT)}\n"
        f"Trailing activation: {pct_text(TRAILING_START_PCT)}\n"
        f"Max open positions: {MAX_OPEN_POSITIONS}\n"
        f"Symbol cooldown: {SYMBOL_COOLDOWN_SECONDS}s\n"
        f"Trade size (margin): {TRADE_SIZE_USDT:.4f} USDT\n"
        f"Leverage: {LEVERAGE}x\n"
        f"Approx position notional/trade: {(TRADE_SIZE_USDT * LEVERAGE):.4f} USDT\n"
        f"Daily report enabled: {DAILY_REPORT_ENABLED}\n"
        f"Daily report hour: {DAILY_REPORT_HOUR}\n"
        f"Current open positions: {get_open_positions_count()}"
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

    best_trade = max(trades, key=lambda x: x["pnl_usdt"]) if trades else None
    worst_trade = min(trades, key=lambda x: x["pnl_usdt"]) if trades else None
    win_rate = 0.0 if total == 0 else (wins / total) * 100.0

    return {
        "trades": trades,
        "total": total,
        "wins": wins,
        "losses": losses,
        "flat": flat,
        "total_pnl": total_pnl,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "win_rate": win_rate,
    }


def get_pnl_text(day_filter=None):
    summary = get_pnl_summary_data(day_filter=day_filter)
    label = "TODAY" if day_filter else "ALL TIME"

    lines = [
        f"📊 PNL SUMMARY ({label})",
        f"Trades: {summary['total']}",
        f"Wins: {summary['wins']}",
        f"Losses: {summary['losses']}",
        f"Flat: {summary['flat']}",
        f"Win rate: {summary['win_rate']:.2f}%",
        f"Total PnL: {summary['total_pnl']:.8f} USDT",
    ]

    if summary["best_trade"]:
        bt = summary["best_trade"]
        lines.append(
            f"Best: {bt['symbol']} {bt['pnl_usdt']:.8f} USDT ({bt['pnl_pct']:.2f}%)"
        )

    if summary["worst_trade"]:
        wt = summary["worst_trade"]
        lines.append(
            f"Worst: {wt['symbol']} {wt['pnl_usdt']:.8f} USDT ({wt['pnl_pct']:.2f}%)"
        )

    return "\n".join(lines)


def get_recent_trades_text(limit=10):
    with trades_lock:
        trades = list(closed_trades)[-limit:]

    if not trades:
        return "📭 No closed trades yet."

    trades.reverse()
    lines = ["🧾 RECENT CLOSED TRADES"]
    for t in trades:
        lines.append(
            f"{t['symbol']} | pnl={t['pnl_usdt']:.8f} USDT ({t['pnl_pct']:.2f}%) | "
            f"reason={t['reason']} | entry={t['entry_price']:.8f} | exit={t['exit_price']:.8f} | "
            f"qty={t['quantity']} | closed={ts_to_text(t['closed_at'])}"
        )

    return "\n".join(lines[:25])

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

            process_extreme_rsi_alerts(symbol, tf, candle_start, r)

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
# ORDER HELPERS
# =========================
def place_bybit_market_buy(symbol: str):
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

    available_balance = get_bybit_total_available_balance_usd()
    if available_balance <= 0:
        raise Exception("No available balance on Bybit account")

    target_leverage = ensure_bybit_leverage(symbol)
    last_price = get_bybit_last_price(symbol)
    qty_str, qty, _ = compute_bybit_order_qty(symbol, last_price)

    order = session.place_order(
        category=CATEGORY,
        symbol=symbol,
        side="Buy",
        orderType="Market",
        qty=qty_str,
    )

    time.sleep(1.0)

    if not sync_local_position_from_exchange(symbol):
        fallback_entry = get_bybit_last_price(symbol)
        set_position(symbol, fallback_entry, qty, "buy_fallback_market")

    return order, target_leverage


def place_bybit_market_sell(symbol: str, reason: str = "manual_sell"):
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
    qty_val = safe_float(qty_str, 0.0)

    if qty_val <= 0:
        raise Exception(f"Rounded close quantity became 0 for {symbol}")

    order = session.place_order(
        category=CATEGORY,
        symbol=symbol,
        side="Sell",
        orderType="Market",
        qty=qty_str,
        reduceOnly=True,
    )

    time.sleep(1.0)
    exit_price = get_bybit_last_price(symbol)
    trade = close_local_position_and_record(symbol, exit_price, reason)
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

    closed_symbols = []
    errors = []

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

            if not BYBIT_TRADING_ENABLED or not bybit_keys_ready():
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
                if not is_symbol_supported_on_bybit(symbol):
                    continue

                exchange_pos = get_bybit_position(symbol)
                if exchange_pos and str(exchange_pos.get("side", "")).strip() == "Buy":
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
                    order, used_lev = place_bybit_market_buy(symbol)
                    msg = (
                        f"🟢 AUTO BUY (BYBIT MAINNET)\n"
                        f"Symbol: {symbol}\n"
                        f"RSI(5m): {value:.2f}\n"
                        f"Order ID: {order.get('result', {}).get('orderId')}\n"
                        f"Trade size (margin): {TRADE_SIZE_USDT:.4f} USDT\n"
                        f"Leverage used: {used_lev}x\n"
                        f"Approx notional: {(TRADE_SIZE_USDT * used_lev):.4f} USDT"
                    )

                    with positions_lock:
                        pos = positions.get(symbol)
                        if pos:
                            sl_text = "OFF" if pos["stop_loss_price"] is None else f"{pos['stop_loss_price']:.8f}"
                            tp_text = "OFF" if pos["take_profit_price"] is None else f"{pos['take_profit_price']:.8f}"
                            ts_text = "OFF" if pos["trailing_stop_price"] is None else f"{pos['trailing_stop_price']:.8f}"
                            msg += (
                                f"\nEntry: {pos['entry_price']:.8f}"
                                f"\nQty: {pos['quantity']}"
                                f"\nStop loss: {sl_text}"
                                f"\nTake profit: {tp_text}"
                                f"\nTrailing stop: {ts_text}"
                            )

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
                    current_price = get_bybit_last_price(symbol)
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
                        order, trade = place_bybit_market_sell(symbol, reason="stop_loss")
                        pnl_text = "" if not trade else f"\nPnL: {trade['pnl_usdt']:.8f} USDT ({trade['pnl_pct']:.2f}%)"
                        send_telegram(
                            f"🛑 STOP LOSS HIT\n"
                            f"Symbol: {symbol}\n"
                            f"Entry: {entry_price:.8f}\n"
                            f"Exit trigger: {current_price:.8f}\n"
                            f"Tracked qty: {qty}\n"
                            f"Order ID: {order.get('result', {}).get('orderId')}{pnl_text}"
                        )
                        continue

                    if take_profit_price is not None and current_price >= take_profit_price:
                        order, trade = place_bybit_market_sell(symbol, reason="take_profit")
                        pnl_text = "" if not trade else f"\nPnL: {trade['pnl_usdt']:.8f} USDT ({trade['pnl_pct']:.2f}%)"
                        send_telegram(
                            f"🎯 TAKE PROFIT HIT\n"
                            f"Symbol: {symbol}\n"
                            f"Entry: {entry_price:.8f}\n"
                            f"Exit trigger: {current_price:.8f}\n"
                            f"Tracked qty: {qty}\n"
                            f"Order ID: {order.get('result', {}).get('orderId')}{pnl_text}"
                        )
                        continue

                    if trailing_active and trailing_stop_price is not None and current_price <= trailing_stop_price:
                        order, trade = place_bybit_market_sell(symbol, reason="trailing_stop")
                        pnl_text = "" if not trade else f"\nPnL: {trade['pnl_usdt']:.8f} USDT ({trade['pnl_pct']:.2f}%)"
                        send_telegram(
                            f"🔒 TRAILING STOP HIT\n"
                            f"Symbol: {symbol}\n"
                            f"Entry: {entry_price:.8f}\n"
                            f"Highest: {highest_price:.8f}\n"
                            f"Trailing stop: {trailing_stop_price:.8f}\n"
                            f"Exit trigger: {current_price:.8f}\n"
                            f"Tracked qty: {qty}\n"
                            f"Order ID: {order.get('result', {}).get('orderId')}{pnl_text}"
                        )
                        continue

                    if current_rsi is not None and current_rsi >= RSI_SELL_THRESHOLD:
                        order, trade = place_bybit_market_sell(symbol, reason="rsi_exit")
                        pnl_text = "" if not trade else f"\nPnL: {trade['pnl_usdt']:.8f} USDT ({trade['pnl_pct']:.2f}%)"
                        send_telegram(
                            f"🔴 AUTO SELL (RSI EXIT)\n"
                            f"Symbol: {symbol}\n"
                            f"RSI(5m): {current_rsi:.2f}\n"
                            f"Entry: {entry_price:.8f}\n"
                            f"Exit trigger: {current_price:.8f}\n"
                            f"Tracked qty: {qty}\n"
                            f"Order ID: {order.get('result', {}).get('orderId')}{pnl_text}"
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
        f"Bybit trading enabled: {BYBIT_TRADING_ENABLED}\n"
        f"Bybit keys loaded: {bybit_keys_ready()}\n"
        f"Auto trading enabled: {AUTO_TRADING_ENABLED}\n"
        f"Extreme alerts enabled: {EXTREME_ALERTS_ENABLED}\n"
        f"Daily report enabled: {DAILY_REPORT_ENABLED}\n"
        f"Current open positions: {get_open_positions_count()}\n"
        f"Max open positions: {MAX_OPEN_POSITIONS}\n"
        f"Buy RSI <= {RSI_BUY_THRESHOLD:.2f}\n"
        f"Sell RSI >= {RSI_SELL_THRESHOLD:.2f}\n"
        f"Extreme buy RSI <= {EXTREME_RSI_BUY:.2f}\n"
        f"Extreme sell RSI >= {EXTREME_RSI_SELL:.2f}\n"
        f"SL: {pct_text(STOP_LOSS_PCT)}\n"
        f"TP: {pct_text(TAKE_PROFIT_PCT)}\n"
        f"Trailing: {pct_text(TRAILING_STOP_PCT)}\n"
        f"Trail start: {pct_text(TRAILING_START_PCT)}\n"
        f"Trade size (margin): {TRADE_SIZE_USDT:.4f} USDT\n"
        f"Leverage: {LEVERAGE}x\n"
        f"Approx notional/trade: {(TRADE_SIZE_USDT * LEVERAGE):.4f} USDT"
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

    with bybit_symbols_lock:
        bybit_count = len(bybit_linear_symbols)

    with trades_lock:
        trades_count = len(closed_trades)

    return (
        f"🛠 DIAG\n"
        f"ws_keys: {ws_keys}\n"
        f"last_ws_message_age_sec: {last_ws_age}\n"
        f"last_shortlist_refresh_age_sec: {last_refresh_age}\n"
        f"WS_DEBUG: {WS_DEBUG}\n"
        f"LIVE_WS_SYMBOLS: {LIVE_WS_SYMBOLS}\n"
        f"BYBIT_TRADING_ENABLED: {BYBIT_TRADING_ENABLED}\n"
        f"BYBIT_KEYS_READY: {bybit_keys_ready()}\n"
        f"AUTO_TRADING_ENABLED: {AUTO_TRADING_ENABLED}\n"
        f"EXTREME_ALERTS_ENABLED: {EXTREME_ALERTS_ENABLED}\n"
        f"DAILY_REPORT_ENABLED: {DAILY_REPORT_ENABLED}\n"
        f"RSI_BUY_THRESHOLD: {RSI_BUY_THRESHOLD}\n"
        f"RSI_SELL_THRESHOLD: {RSI_SELL_THRESHOLD}\n"
        f"EXTREME_RSI_BUY: {EXTREME_RSI_BUY}\n"
        f"EXTREME_RSI_SELL: {EXTREME_RSI_SELL}\n"
        f"STOP_LOSS_PCT: {STOP_LOSS_PCT}\n"
        f"TAKE_PROFIT_PCT: {TAKE_PROFIT_PCT}\n"
        f"TRAILING_STOP_PCT: {TRAILING_STOP_PCT}\n"
        f"TRAILING_START_PCT: {TRAILING_START_PCT}\n"
        f"MAX_OPEN_POSITIONS: {MAX_OPEN_POSITIONS}\n"
        f"TRADE_SIZE_USDT: {TRADE_SIZE_USDT}\n"
        f"LEVERAGE: {LEVERAGE}\n"
        f"CLOSED_TRADES_COUNT: {trades_count}\n"
        f"SUPPORTED_BYBIT_USDT_SYMBOLS: {bybit_count}"
    )


def telegram_command_loop():
    global telegram_offset, last_command_time
    global AUTO_TRADING_ENABLED, STOP_LOSS_PCT, TAKE_PROFIT_PCT
    global TRAILING_STOP_PCT, TRAILING_START_PCT
    global RSI_BUY_THRESHOLD, RSI_SELL_THRESHOLD, MAX_OPEN_POSITIONS, SYMBOL_COOLDOWN_SECONDS
    global TRADE_SIZE_USDT, LEVERAGE
    global EXTREME_ALERTS_ENABLED, EXTREME_RSI_BUY, EXTREME_RSI_SELL

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

                elif text in ("/bybit", "/binance"):
                    test_bybit_connection()

                elif text == "/bbal":
                    try:
                        send_telegram(get_bybit_nonzero_balances_text())
                    except Exception as e:
                        send_telegram(f"❌ Bybit balance check failed\n{e}")

                elif text == "/buy":
                    try:
                        order, used_lev = place_bybit_market_buy(BYBIT_SYMBOL)
                        sync_local_position_from_exchange(BYBIT_SYMBOL)

                        msg = (
                            f"✅ Bybit MAINNET BUY placed\n"
                            f"Symbol: {BYBIT_SYMBOL}\n"
                            f"Order ID: {order.get('result', {}).get('orderId')}\n"
                            f"Trade size (margin): {TRADE_SIZE_USDT:.4f} USDT\n"
                            f"Leverage used: {used_lev}x\n"
                            f"Approx notional: {(TRADE_SIZE_USDT * used_lev):.4f} USDT"
                        )

                        with positions_lock:
                            pos = positions.get(BYBIT_SYMBOL)
                            if pos:
                                msg += f"\nAvg entry: {pos['entry_price']:.8f}\nQty: {pos['quantity']}"

                        send_telegram(msg)
                    except Exception as e:
                        send_telegram(f"❌ Bybit MAINNET BUY failed\n{e}")

                elif text == "/sell":
                    try:
                        order, trade = place_bybit_market_sell(BYBIT_SYMBOL, reason="manual_sell")
                        pnl_text = "" if not trade else f"\nPnL: {trade['pnl_usdt']:.8f} USDT ({trade['pnl_pct']:.2f}%)"
                        send_telegram(
                            f"✅ Bybit MAINNET SELL placed\n"
                            f"Symbol: {BYBIT_SYMBOL}\n"
                            f"Order ID: {order.get('result', {}).get('orderId')}{pnl_text}"
                        )
                    except Exception as e:
                        send_telegram(f"❌ Bybit MAINNET SELL failed\n{e}")

                elif text == "/panicclose":
                    try:
                        closed_symbols, errors = panic_close_all_positions()
                        if not closed_symbols and not errors:
                            send_telegram("📭 PANIC CLOSE: No open long positions found.")
                        else:
                            msg = "🚨 PANIC CLOSE ACTIVATED\n"
                            if closed_symbols:
                                msg += "Closed:\n" + "\n".join(closed_symbols[:50])
                            if errors:
                                msg += "\n\nErrors:\n" + "\n".join(errors[:20])
                            send_telegram(msg)
                    except Exception as e:
                        send_telegram(f"❌ PANIC CLOSE failed\n{e}")

                elif text == "/pnl":
                    send_telegram(get_pnl_text())

                elif text == "/dailyreport":
                    send_telegram(get_pnl_text(day_filter=today_str_from_ts(now_ts())))

                elif text == "/trades":
                    send_telegram(get_recent_trades_text())

                elif text == "/autoon":
                    AUTO_TRADING_ENABLED = True
                    send_telegram("✅ Auto trading enabled")

                elif text == "/autooff":
                    AUTO_TRADING_ENABLED = False
                    send_telegram("⏸ Auto trading disabled")

                elif text == "/extremeon":
                    EXTREME_ALERTS_ENABLED = True
                    send_telegram(
                        f"✅ Extreme RSI alerts enabled\n"
                        f"Extreme buy RSI <= {EXTREME_RSI_BUY:.2f}\n"
                        f"Extreme sell RSI >= {EXTREME_RSI_SELL:.2f}"
                    )

                elif text == "/extremeoff":
                    EXTREME_ALERTS_ENABLED = False
                    send_telegram("⏸ Extreme RSI alerts disabled")

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

                elif text.startswith("/setextremebuy "):
                    try:
                        value = float(text.split(maxsplit=1)[1].strip())
                        if value < 0 or value > 100:
                            raise Exception("Extreme buy RSI must be between 0 and 100")
                        EXTREME_RSI_BUY = value
                        send_telegram(
                            f"✅ Extreme BUY RSI updated\n"
                            f"Extreme alerts enabled: {EXTREME_ALERTS_ENABLED}\n"
                            f"Extreme buy RSI <= {EXTREME_RSI_BUY:.2f}\n"
                            f"Extreme sell RSI >= {EXTREME_RSI_SELL:.2f}"
                        )
                    except Exception as e:
                        send_telegram(f"❌ Failed to set extreme buy RSI\n{e}")

                elif text.startswith("/setextremesell "):
                    try:
                        value = float(text.split(maxsplit=1)[1].strip())
                        if value < 0 or value > 100:
                            raise Exception("Extreme sell RSI must be between 0 and 100")
                        EXTREME_RSI_SELL = value
                        send_telegram(
                            f"✅ Extreme SELL RSI updated\n"
                            f"Extreme alerts enabled: {EXTREME_ALERTS_ENABLED}\n"
                            f"Extreme buy RSI <= {EXTREME_RSI_BUY:.2f}\n"
                            f"Extreme sell RSI >= {EXTREME_RSI_SELL:.2f}"
                        )
                    except Exception as e:
                        send_telegram(f"❌ Failed to set extreme sell RSI\n{e}")

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

                elif text.startswith("/setsize "):
                    try:
                        value = float(text.split(maxsplit=1)[1].strip())
                        if value <= 0:
                            raise Exception("Trade size must be greater than 0")
                        TRADE_SIZE_USDT = value
                        send_telegram(
                            f"✅ Trade size updated\n"
                            f"Trade size (margin): {TRADE_SIZE_USDT:.4f} USDT\n"
                            f"Leverage: {LEVERAGE}x\n"
                            f"Approx notional/trade: {(TRADE_SIZE_USDT * LEVERAGE):.4f} USDT"
                        )
                    except Exception as e:
                        send_telegram(f"❌ Failed to set trade size\n{e}")

                elif text.startswith("/setlev "):
                    try:
                        value = int(text.split(maxsplit=1)[1].strip())
                        if value < 1:
                            raise Exception("Leverage must be at least 1")
                        LEVERAGE = value
                        send_telegram(
                            f"✅ Leverage updated\n"
                            f"Trade size (margin): {TRADE_SIZE_USDT:.4f} USDT\n"
                            f"Leverage: {LEVERAGE}x\n"
                            f"Approx notional/trade: {(TRADE_SIZE_USDT * LEVERAGE):.4f} USDT\n"
                            f"Note: actual applied leverage may be capped per symbol by Bybit."
                        )
                    except Exception as e:
                        send_telegram(f"❌ Failed to set leverage\n{e}")

                elif text == "/help":
                    send_telegram(
                        "Available commands:\n"
                        "/test - Telegram test\n"
                        "/force - force fake alerts\n"
                        "/status - bot status\n"
                        "/symbols - live symbol sample\n"
                        "/diag - websocket diagnostics\n"
                        "/bybit - test Bybit connection\n"
                        "/bbal - Bybit balances\n"
                        "/buy - manual Bybit market buy for BYBIT_SYMBOL\n"
                        "/sell - manual Bybit market sell for BYBIT_SYMBOL\n"
                        "/panicclose - close all open long positions now\n"
                        "/pnl - show all-time PnL summary\n"
                        "/dailyreport - show today's PnL summary\n"
                        "/trades - show recent closed trades\n"
                        "/autoon - enable auto trading\n"
                        "/autooff - disable auto trading\n"
                        "/extremeon - enable extreme RSI alerts\n"
                        "/extremeoff - disable extreme RSI alerts\n"
                        "/showstrategy - show auto strategy\n"
                        "/showpositions - show tracked positions\n"
                        "/setrsibuy 15 - set buy threshold\n"
                        "/setrsisell 85 - set sell threshold\n"
                        "/setextremebuy 10 - set extreme buy RSI threshold\n"
                        "/setextremesell 90 - set extreme sell RSI threshold\n"
                        "/setsl 0 - set stop loss percent (0 = OFF)\n"
                        "/settp 3 - set take profit percent (0 = OFF)\n"
                        "/settrailing 2 - set trailing stop percent (0 = OFF)\n"
                        "/settrailstart 1 - set trailing activation percent (0 = immediate)\n"
                        "/setmaxcoins 5 - set max simultaneous open positions\n"
                        "/setcooldown 300 - set symbol cooldown seconds\n"
                        "/setsize 20 - set trade size margin in USDT\n"
                        "/setlev 3 - set leverage\n"
                        "/help - command list"
                    )

            time.sleep(1)

        except Exception as e:
            print("Telegram command loop error:", e)
            time.sleep(5)

# =========================
# DAILY REPORT LOOP
# =========================
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

# =========================
# LOOPS
# =========================
def shortlist_refresh_loop():
    while True:
        try:
            refresh_shortlist_and_history()
            restart_websockets()
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
def test_bybit_connection():
    if CATEGORY != "linear":
        print(f"Warning: current CATEGORY={CATEGORY}, but this bot is intended for linear.")

    if not bybit_keys_ready():
        print("Bybit keys missing")
        send_telegram("❌ Bybit mainnet keys missing")
        return

    try:
        print("Testing Bybit market data...")
        ticker = session.get_tickers(category=CATEGORY, symbol=BYBIT_SYMBOL)
        print("Bybit ticker OK:", ticker)

        print("Testing Bybit wallet balance endpoint...")
        account = get_bybit_account_wallet()
        print("Bybit wallet OK:", account)

        refresh_bybit_linear_symbols()

        send_telegram(
            f"✅ Bybit MAINNET connected\n"
            f"Category: {CATEGORY}\n"
            f"Symbol: {BYBIT_SYMBOL}\n"
            f"Trading enabled: {BYBIT_TRADING_ENABLED}"
        )
    except Exception as e:
        print("Bybit mainnet connection failed:", e)
        send_telegram(f"❌ Bybit MAINNET connection failed\n{e}")


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
    print("BYBIT_TRADING_ENABLED =", BYBIT_TRADING_ENABLED)
    print("BYBIT_SYMBOL =", BYBIT_SYMBOL)
    print("BYBIT_KEYS_READY =", bybit_keys_ready())
    print("BYBIT_TRADE_SIZE_USDT =", BYBIT_TRADE_SIZE_USDT)
    print("BYBIT_LEVERAGE =", BYBIT_LEVERAGE)
    print("DEFAULT_STOP_LOSS_PCT =", DEFAULT_STOP_LOSS_PCT)
    print("DEFAULT_TAKE_PROFIT_PCT =", DEFAULT_TAKE_PROFIT_PCT)
    print("DEFAULT_TRAILING_STOP_PCT =", DEFAULT_TRAILING_STOP_PCT)
    print("DEFAULT_TRAILING_START_PCT =", DEFAULT_TRAILING_START_PCT)
    print("DEFAULT_RSI_BUY =", DEFAULT_RSI_BUY)
    print("DEFAULT_RSI_SELL =", DEFAULT_RSI_SELL)
    print("DEFAULT_EXTREME_ALERTS_ENABLED =", DEFAULT_EXTREME_ALERTS_ENABLED)
    print("DEFAULT_EXTREME_RSI_BUY =", DEFAULT_EXTREME_RSI_BUY)
    print("DEFAULT_EXTREME_RSI_SELL =", DEFAULT_EXTREME_RSI_SELL)
    print("TRADES_HISTORY_LIMIT =", TRADES_HISTORY_LIMIT)
    print("DAILY_REPORT_ENABLED =", DAILY_REPORT_ENABLED)
    print("DAILY_REPORT_HOUR =", DAILY_REPORT_HOUR)
    print("DEFAULT_MAX_OPEN_POSITIONS =", DEFAULT_MAX_OPEN_POSITIONS)

    send_telegram("🚀 Scanner started")
    send_telegram("✅ Telegram test message")

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

