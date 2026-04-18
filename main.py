import os
import time
import threading
import requests
from pybit.unified_trading import HTTP, WebSocket

# =========================
# ENV / CONFIG
# =========================
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "true").lower() == "true"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "").strip()
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "").strip()

# MAINNET READY:
# false = mainnet
# true  = testnet
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"

# linear = perpetual/futures style symbols like BTCUSDT
# spot   = spot market
BYBIT_CATEGORY = os.getenv("BYBIT_CATEGORY", "linear").strip().lower()

# account type used for wallet balance checks
# common values: UNIFIED, CONTRACT, SPOT
BYBIT_ACCOUNT_TYPE = os.getenv("BYBIT_ACCOUNT_TYPE", "UNIFIED").strip().upper()

# one-way mode default
BYBIT_POSITION_IDX = int(os.getenv("BYBIT_POSITION_IDX", "0"))

# comma-separated symbols, example: BTCUSDT,ETHUSDT,SOLUSDT
SYMBOLS = [
    s.strip().upper()
    for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",")
    if s.strip()
]

HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "300"))
WS_DEBUG = os.getenv("WS_DEBUG", "false").lower() == "true"

# RSI settings
RSI_LENGTH = int(os.getenv("RSI_LENGTH", "14"))
RSI_BUY = float(os.getenv("RSI_BUY", "15"))
RSI_SELL = float(os.getenv("RSI_SELL", "85"))

# special signal settings
SPECIAL_BUY_SIGNAL_ENABLED = os.getenv("SPECIAL_BUY_SIGNAL_ENABLED", "true").lower() == "true"
SPECIAL_SELL_SIGNAL_ENABLED = os.getenv("SPECIAL_SELL_SIGNAL_ENABLED", "true").lower() == "true"
SPECIAL_BUY_RSI = float(os.getenv("SPECIAL_BUY_RSI", "11"))
SPECIAL_SELL_RSI = float(os.getenv("SPECIAL_SELL_RSI", "91"))

# history / stream
KLINE_INTERVAL = os.getenv("KLINE_INTERVAL", "5")  # 5-minute RSI stream
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "200"))

# safety
BYBIT_TRADING_ENABLED = os.getenv("BYBIT_TRADING_ENABLED", "false").lower() == "true"

# =========================
# GLOBALS
# =========================
session = HTTP(
    testnet=BYBIT_TESTNET,
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET,
)

data = {}
last_alert_candle = {}
telegram_offset = None
last_ws_message_at = 0

# =========================
# TELEGRAM
# =========================
def send_telegram(message: str):
    print("Trying to send Telegram:", message)

    if not TELEGRAM_ENABLED:
        print("Telegram disabled")
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
def safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def should_alert_once_per_candle(alert_type: str, symbol: str, candle_start: str) -> bool:
    key = f"{alert_type}:{symbol}"
    last = last_alert_candle.get(key)
    if last == candle_start:
        return False
    last_alert_candle[key] = candle_start
    return True


def heartbeat():
    while True:
        print("BOT ALIVE ✅")
        send_telegram("BOT ALIVE ✅")
        time.sleep(HEARTBEAT_SECONDS)


def trading_stop_supported() -> bool:
    return BYBIT_CATEGORY in {"linear", "inverse"}

# =========================
# RSI
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
# HISTORY LOADING
# =========================
def fetch_history(symbol: str):
    try:
        response = session.get_kline(
            category=BYBIT_CATEGORY,
            symbol=symbol,
            interval=KLINE_INTERVAL,
            limit=HISTORY_LIMIT,
        )

        rows = response.get("result", {}).get("list", [])
        if not rows:
            print(f"No history rows for {symbol}")
            return

        rows.reverse()
        closes = []

        for row in rows:
            close = safe_float(row[4])
            if close is not None:
                closes.append(close)

        if closes:
            data[symbol] = closes
            print(f"Loaded history for {symbol}: {len(closes)} candles")

    except Exception as e:
        print(f"History fetch error for {symbol}: {e}")

# =========================
# SIGNAL PROCESSING
# =========================
def process_symbol(symbol: str, close_price: float, candle_start: str):
    if symbol not in data:
        data[symbol] = []

    data[symbol].append(close_price)

    if len(data[symbol]) > HISTORY_LIMIT:
        data[symbol].pop(0)

    r = rsi(data[symbol], RSI_LENGTH)
    if r is None:
        return

    print(f"{symbol} RSI={r:.2f}")

    if r <= RSI_BUY:
        if should_alert_once_per_candle("buy", symbol, candle_start):
            send_telegram(
                f"🟢 BUY SIGNAL\n"
                f"Symbol: {symbol}\n"
                f"TF: {KLINE_INTERVAL}m\n"
                f"RSI: {r:.2f}\n"
                f"Trigger: RSI <= {RSI_BUY}"
            )

    if r >= RSI_SELL:
        if should_alert_once_per_candle("sell", symbol, candle_start):
            send_telegram(
                f"🔴 SELL SIGNAL\n"
                f"Symbol: {symbol}\n"
                f"TF: {KLINE_INTERVAL}m\n"
                f"RSI: {r:.2f}\n"
                f"Trigger: RSI >= {RSI_SELL}"
            )

    if SPECIAL_BUY_SIGNAL_ENABLED and r <= SPECIAL_BUY_RSI:
        if should_alert_once_per_candle("special_buy", symbol, candle_start):
            send_telegram(
                f"🟢🔥 EXTREME BUY SIGNAL\n"
                f"Symbol: {symbol}\n"
                f"TF: {KLINE_INTERVAL}m\n"
                f"RSI: {r:.2f}\n"
                f"Trigger: RSI <= {SPECIAL_BUY_RSI}"
            )

    if SPECIAL_SELL_SIGNAL_ENABLED and r >= SPECIAL_SELL_RSI:
        if should_alert_once_per_candle("special_sell", symbol, candle_start):
            send_telegram(
                f"🔴🔥 EXTREME SELL SIGNAL\n"
                f"Symbol: {symbol}\n"
                f"TF: {KLINE_INTERVAL}m\n"
                f"RSI: {r:.2f}\n"
                f"Trigger: RSI >= {SPECIAL_SELL_RSI}"
            )

# =========================
# BYBIT API HELPERS
# =========================
def test_bybit_connection():
    try:
        info = session.get_account_info()
        result = info.get("result", {})
        send_telegram(
            "✅ Bybit connected\n"
            f"Account info fetched successfully\n"
            f"Raw result: {result}"
        )
        print("Bybit account info OK:", info)
    except Exception as e:
        send_telegram(f"❌ Bybit connection failed\n{e}")
        print("Bybit connection failed:", e)


def bybit_balance_text():
    try:
        result = session.get_wallet_balance(accountType=BYBIT_ACCOUNT_TYPE)
        accounts = result.get("result", {}).get("list", [])
        lines = [f"💰 Bybit balance ({BYBIT_ACCOUNT_TYPE})"]

        found = False
        for acct in accounts:
            for coin in acct.get("coin", []):
                wallet_balance = safe_float(coin.get("walletBalance"), 0.0)
                if wallet_balance and wallet_balance != 0:
                    found = True
                    lines.append(f"{coin.get('coin')}: {wallet_balance}")

        if not found:
            lines.append("No non-zero balances found.")

        return "\n".join(lines[:30])
    except Exception as e:
        return f"❌ Balance check failed\n{e}"


def bybit_positions_text():
    try:
        if BYBIT_CATEGORY not in {"linear", "inverse"}:
            return "Positions command is for linear/inverse only."

        result = session.get_positions(category=BYBIT_CATEGORY, settleCoin="USDT")
        rows = result.get("result", {}).get("list", [])

        lines = [f"📦 Bybit positions ({BYBIT_CATEGORY})"]
        found = False

        for row in rows:
            size = safe_float(row.get("size"), 0.0)
            if size and size != 0:
                found = True
                lines.append(
                    f"{row.get('symbol')} | side={row.get('side')} | "
                    f"size={row.get('size')} | avg={row.get('avgPrice')} | "
                    f"tp={row.get('takeProfit')} | sl={row.get('stopLoss')}"
                )

        if not found:
            lines.append("No open positions.")

        return "\n".join(lines[:30])
    except Exception as e:
        return f"❌ Position check failed\n{e}"


def place_bybit_market_order(symbol: str, side: str, qty: str):
    if not BYBIT_TRADING_ENABLED:
        raise Exception("BYBIT_TRADING_ENABLED is false")

    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        raise Exception("Missing BYBIT_API_KEY / BYBIT_API_SECRET")

    side = side.capitalize()
    if side not in {"Buy", "Sell"}:
        raise Exception("side must be Buy or Sell")

    result = session.place_order(
        category=BYBIT_CATEGORY,
        symbol=symbol,
        side=side,
        orderType="Market",
        qty=str(qty),
    )
    return result


def set_bybit_trading_stop(symbol: str, take_profit: str = None, stop_loss: str = None):
    if not BYBIT_TRADING_ENABLED:
        raise Exception("BYBIT_TRADING_ENABLED is false")

    if not trading_stop_supported():
        raise Exception("Trading stop is only supported here for linear/inverse.")

    params = {
        "category": BYBIT_CATEGORY,
        "symbol": symbol,
        "tpslMode": "Full",
        "positionIdx": BYBIT_POSITION_IDX,
    }

    if take_profit is not None:
        params["takeProfit"] = str(take_profit)

    if stop_loss is not None:
        params["stopLoss"] = str(stop_loss)

    result = session.set_trading_stop(**params)
    return result


def clear_bybit_trading_stop(symbol: str, clear_tp: bool = False, clear_sl: bool = False):
    if not BYBIT_TRADING_ENABLED:
        raise Exception("BYBIT_TRADING_ENABLED is false")

    if not trading_stop_supported():
        raise Exception("Trading stop is only supported here for linear/inverse.")

    params = {
        "category": BYBIT_CATEGORY,
        "symbol": symbol,
        "tpslMode": "Full",
        "positionIdx": BYBIT_POSITION_IDX,
    }

    if clear_tp:
        params["takeProfit"] = "0"

    if clear_sl:
        params["stopLoss"] = "0"

    result = session.set_trading_stop(**params)
    return result

# =========================
# WS HANDLER
# =========================
def handle_kline(msg):
    global last_ws_message_at

    try:
        last_ws_message_at = int(time.time())

        if WS_DEBUG:
            print("RAW WS MESSAGE:", msg)

        topic = str(msg.get("topic", ""))
        data_list = msg.get("data", [])

        if not isinstance(data_list, list):
            return

        topic_symbol = ""
        if topic.startswith("kline."):
            parts = topic.split(".")
            if len(parts) >= 3:
                topic_symbol = parts[2].upper()

        for item in data_list:
            symbol = str(item.get("symbol") or topic_symbol).upper()
            close = safe_float(item.get("close"))
            start_time = str(item.get("start"))
            confirm = bool(item.get("confirm", False))

            if not symbol or close is None:
                continue

            process_symbol(symbol, close, start_time)

            if confirm:
                print(f"Confirmed candle: {symbol} start={start_time}")

    except Exception as e:
        print("WS handler error:", e)

# =========================
# TELEGRAM COMMANDS
# =========================
def telegram_loop():
    global telegram_offset
    global RSI_BUY, RSI_SELL
    global SPECIAL_BUY_RSI, SPECIAL_SELL_RSI
    global SPECIAL_BUY_SIGNAL_ENABLED, SPECIAL_SELL_SIGNAL_ENABLED

    if not TELEGRAM_ENABLED:
        print("Telegram loop disabled")
        return

    while True:
        try:
            updates = get_telegram_updates(offset=telegram_offset, timeout=20)

            for upd in updates:
                telegram_offset = upd["update_id"] + 1

                message = upd.get("message", {})
                text = str(message.get("text", "")).strip()

                if not text:
                    continue

                print("Telegram command:", text)

                if text == "/help":
                    send_telegram(
                        "Commands:\n"
                        "/help\n"
                        "/status\n"
                        "/showsignal\n"
                        "/setrsibuy 15\n"
                        "/setrsisell 85\n"
                        "/setspecialbuy 11\n"
                        "/setspecialsell 91\n"
                        "/specialbuyon\n"
                        "/specialbuyoff\n"
                        "/specialsellon\n"
                        "/specialselloff\n"
                        "/bybitping\n"
                        "/balance\n"
                        "/positions\n"
                        "/bybitbuy BTCUSDT 0.001\n"
                        "/bybitsell BTCUSDT 0.001\n"
                        "/settp BTCUSDT 95000\n"
                        "/setsl BTCUSDT 84000\n"
                        "/setrisk BTCUSDT 84000 95000\n"
                        "/cleartp BTCUSDT\n"
                        "/clearsl BTCUSDT"
                    )

                elif text == "/status":
                    send_telegram(
                        f"✅ Bot running\n"
                        f"Bybit category: {BYBIT_CATEGORY}\n"
                        f"Bybit testnet: {BYBIT_TESTNET}\n"
                        f"Bybit account type: {BYBIT_ACCOUNT_TYPE}\n"
                        f"Position idx: {BYBIT_POSITION_IDX}\n"
                        f"Symbols: {', '.join(SYMBOLS)}\n"
                        f"RSI buy <= {RSI_BUY}\n"
                        f"RSI sell >= {RSI_SELL}\n"
                        f"Special buy <= {SPECIAL_BUY_RSI} ({SPECIAL_BUY_SIGNAL_ENABLED})\n"
                        f"Special sell >= {SPECIAL_SELL_RSI} ({SPECIAL_SELL_SIGNAL_ENABLED})\n"
                        f"Trading enabled: {BYBIT_TRADING_ENABLED}"
                    )

                elif text == "/showsignal":
                    send_telegram(
                        f"📡 Signals\n"
                        f"Buy RSI <= {RSI_BUY}\n"
                        f"Sell RSI >= {RSI_SELL}\n"
                        f"Extreme buy RSI <= {SPECIAL_BUY_RSI} ({SPECIAL_BUY_SIGNAL_ENABLED})\n"
                        f"Extreme sell RSI >= {SPECIAL_SELL_RSI} ({SPECIAL_SELL_SIGNAL_ENABLED})"
                    )

                elif text.startswith("/setrsibuy "):
                    try:
                        RSI_BUY = float(text.split()[1])
                        send_telegram(f"✅ Buy RSI set to {RSI_BUY}")
                    except Exception as e:
                        send_telegram(f"❌ Invalid value\n{e}")

                elif text.startswith("/setrsisell "):
                    try:
                        RSI_SELL = float(text.split()[1])
                        send_telegram(f"✅ Sell RSI set to {RSI_SELL}")
                    except Exception as e:
                        send_telegram(f"❌ Invalid value\n{e}")

                elif text.startswith("/setspecialbuy "):
                    try:
                        SPECIAL_BUY_RSI = float(text.split()[1])
                        send_telegram(f"✅ Extreme buy RSI set to {SPECIAL_BUY_RSI}")
                    except Exception as e:
                        send_telegram(f"❌ Invalid value\n{e}")

                elif text.startswith("/setspecialsell "):
                    try:
                        SPECIAL_SELL_RSI = float(text.split()[1])
                        send_telegram(f"✅ Extreme sell RSI set to {SPECIAL_SELL_RSI}")
                    except Exception as e:
                        send_telegram(f"❌ Invalid value\n{e}")

                elif text == "/specialbuyon":
                    SPECIAL_BUY_SIGNAL_ENABLED = True
                    send_telegram("✅ Extreme buy signal enabled")

                elif text == "/specialbuyoff":
                    SPECIAL_BUY_SIGNAL_ENABLED = False
                    send_telegram("⏸ Extreme buy signal disabled")

                elif text == "/specialsellon":
                    SPECIAL_SELL_SIGNAL_ENABLED = True
                    send_telegram("✅ Extreme sell signal enabled")

                elif text == "/specialselloff":
                    SPECIAL_SELL_SIGNAL_ENABLED = False
                    send_telegram("⏸ Extreme sell signal disabled")

                elif text == "/bybitping":
                    test_bybit_connection()

                elif text == "/balance":
                    send_telegram(bybit_balance_text())

                elif text == "/positions":
                    send_telegram(bybit_positions_text())

                elif text.startswith("/bybitbuy "):
                    try:
                        parts = text.split()
                        symbol = parts[1].upper()
                        qty = parts[2]
                        result = place_bybit_market_order(symbol, "Buy", qty)
                        send_telegram(
                            f"✅ Bybit BUY sent\n"
                            f"Symbol: {symbol}\n"
                            f"Qty: {qty}\n"
                            f"Result: {result.get('retMsg', 'OK')}"
                        )
                    except Exception as e:
                        send_telegram(f"❌ Bybit BUY failed\n{e}")

                elif text.startswith("/bybitsell "):
                    try:
                        parts = text.split()
                        symbol = parts[1].upper()
                        qty = parts[2]
                        result = place_bybit_market_order(symbol, "Sell", qty)
                        send_telegram(
                            f"✅ Bybit SELL sent\n"
                            f"Symbol: {symbol}\n"
                            f"Qty: {qty}\n"
                            f"Result: {result.get('retMsg', 'OK')}"
                        )
                    except Exception as e:
                        send_telegram(f"❌ Bybit SELL failed\n{e}")

                elif text.startswith("/settp "):
                    try:
                        parts = text.split()
                        symbol = parts[1].upper()
                        tp = parts[2]
                        result = set_bybit_trading_stop(symbol=symbol, take_profit=tp, stop_loss=None)
                        send_telegram(
                            f"✅ Take profit set\n"
                            f"Symbol: {symbol}\n"
                            f"TP: {tp}\n"
                            f"Result: {result.get('retMsg', 'OK')}"
                        )
                    except Exception as e:
                        send_telegram(f"❌ Set TP failed\n{e}")

                elif text.startswith("/setsl "):
                    try:
                        parts = text.split()
                        symbol = parts[1].upper()
                        sl = parts[2]
                        result = set_bybit_trading_stop(symbol=symbol, take_profit=None, stop_loss=sl)
                        send_telegram(
                            f"✅ Stop loss set\n"
                            f"Symbol: {symbol}\n"
                            f"SL: {sl}\n"
                            f"Result: {result.get('retMsg', 'OK')}"
                        )
                    except Exception as e:
                        send_telegram(f"❌ Set SL failed\n{e}")

                elif text.startswith("/setrisk "):
                    try:
                        parts = text.split()
                        symbol = parts[1].upper()
                        sl = parts[2]
                        tp = parts[3]
                        result = set_bybit_trading_stop(symbol=symbol, take_profit=tp, stop_loss=sl)
                        send_telegram(
                            f"✅ TP/SL set\n"
                            f"Symbol: {symbol}\n"
                            f"SL: {sl}\n"
                            f"TP: {tp}\n"
                            f"Result: {result.get('retMsg', 'OK')}"
                        )
                    except Exception as e:
                        send_telegram(f"❌ Set TP/SL failed\n{e}")

                elif text.startswith("/cleartp "):
                    try:
                        parts = text.split()
                        symbol = parts[1].upper()
                        result = clear_bybit_trading_stop(symbol=symbol, clear_tp=True, clear_sl=False)
                        send_telegram(
                            f"✅ Take profit cleared\n"
                            f"Symbol: {symbol}\n"
                            f"Result: {result.get('retMsg', 'OK')}"
                        )
                    except Exception as e:
                        send_telegram(f"❌ Clear TP failed\n{e}")

                elif text.startswith("/clearsl "):
                    try:
                        parts = text.split()
                        symbol = parts[1].upper()
                        result = clear_bybit_trading_stop(symbol=symbol, clear_tp=False, clear_sl=True)
                        send_telegram(
                            f"✅ Stop loss cleared\n"
                            f"Symbol: {symbol}\n"
                            f"Result: {result.get('retMsg', 'OK')}"
                        )
                    except Exception as e:
                        send_telegram(f"❌ Clear SL failed\n{e}")

            time.sleep(1)

        except Exception as e:
            print("Telegram loop error:", e)
            time.sleep(5)

# =========================
# STARTUP
# =========================
def load_history():
    for symbol in SYMBOLS:
        fetch_history(symbol)
        time.sleep(0.2)


def start_ws():
    ws = WebSocket(testnet=BYBIT_TESTNET, channel_type=BYBIT_CATEGORY)

    for symbol in SYMBOLS:
        ws.kline_stream(
            interval=int(KLINE_INTERVAL),
            symbol=symbol,
            callback=handle_kline,
        )
        print(f"Subscribed: {symbol} {KLINE_INTERVAL}m")
        time.sleep(0.1)

    return ws


def main():
    print("Starting Bybit RSI bot...")
    print("BYBIT_TESTNET =", BYBIT_TESTNET)
    print("BYBIT_CATEGORY =", BYBIT_CATEGORY)
    print("BYBIT_ACCOUNT_TYPE =", BYBIT_ACCOUNT_TYPE)
    print("BYBIT_POSITION_IDX =", BYBIT_POSITION_IDX)
    print("SYMBOLS =", SYMBOLS)
    print("BYBIT_TRADING_ENABLED =", BYBIT_TRADING_ENABLED)

    send_telegram("🚀 Bybit RSI bot started")
    load_history()
    test_bybit_connection()

    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(target=telegram_loop, daemon=True).start()

    start_ws()

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
