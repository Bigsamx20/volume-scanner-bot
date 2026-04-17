import os
import json
import time
import asyncio
import logging
from collections import defaultdict, deque
from typing import Dict, List, Tuple, Optional, Set

import aiohttp
import aiosqlite
from telegram import Update
from telegram.error import RetryAfter, TimedOut, NetworkError
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

BINANCE_REST = "https://api.binance.com"
BINANCE_WS = "wss://stream.binance.com:9443/stream"

DB_FILE = "scanner.db"

VALID_INTERVALS = {"5m", "1h"}
DEFAULT_BASELINE_CANDLES = 20
STREAM_CHUNK_SIZE = 120
MAX_CANDLE_HISTORY = 300
SYMBOL_REFRESH_SECONDS = 3600

MIN_TELEGRAM_SEND_DELAY_SECONDS = 1.2
MAX_TELEGRAM_RETRIES = 5

INTERVAL_TO_SECONDS = {
    "5m": 5 * 60,
    "1h": 60 * 60,
}

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("volume_scanner_bot")

# ============================================================
# GLOBAL STATE
# ============================================================
candle_cache: Dict[Tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=MAX_CANDLE_HISTORY))
alerted_candles: Set[Tuple[int, str, str, int]] = set()
ws_tasks: List[asyncio.Task] = []

maintenance_task: Optional[asyncio.Task] = None
telegram_sender_task: Optional[asyncio.Task] = None

tracked_symbols_by_interval: Dict[str, Set[str]] = defaultdict(set)

all_usdt_symbols_cache: List[str] = []
all_usdt_symbols_last_refresh = 0

shared_http_session: Optional[aiohttp.ClientSession] = None

telegram_send_queue: "asyncio.Queue[Tuple[int, str]]" = asyncio.Queue()

# ============================================================
# DATABASE
# ============================================================
CREATE_RULES_SQL = """
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    interval TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
    threshold_percent REAL NOT NULL,
    baseline_candles INTEGER NOT NULL,
    symbols_mode TEXT NOT NULL CHECK(symbols_mode IN ('ALL_USDT', 'CUSTOM')),
    symbols_json TEXT
);
"""

async def init_db() -> None:
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(CREATE_RULES_SQL)
        await db.commit()

async def get_all_rules() -> List[dict]:
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("""
            SELECT id, chat_id, enabled, interval, side, threshold_percent,
                   baseline_candles, symbols_mode, symbols_json
            FROM rules
            WHERE enabled = 1
        """)
        rows = await cur.fetchall()

    rules = []
    for row in rows:
        rules.append({
            "id": int(row[0]),
            "chat_id": int(row[1]),
            "enabled": bool(row[2]),
            "interval": row[3],
            "side": row[4],
            "threshold_percent": float(row[5]),
            "baseline_candles": int(row[6]),
            "symbols_mode": row[7],
            "symbols": json.loads(row[8]) if row[8] else [],
        })
    return rules

async def get_chat_rules(chat_id: int) -> List[dict]:
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("""
            SELECT id, enabled, interval, side, threshold_percent,
                   baseline_candles, symbols_mode, symbols_json
            FROM rules
            WHERE chat_id = ?
            ORDER BY id DESC
        """, (chat_id,))
        rows = await cur.fetchall()

    rules = []
    for row in rows:
        rules.append({
            "id": int(row[0]),
            "enabled": bool(row[1]),
            "interval": row[2],
            "side": row[3],
            "threshold_percent": float(row[4]),
            "baseline_candles": int(row[5]),
            "symbols_mode": row[6],
            "symbols": json.loads(row[7]) if row[7] else [],
        })
    return rules

# ============================================================
# HELPERS
# ============================================================
def chunked(items: List, size: int) -> List[List]:
    return [items[i:i + size] for i in range(0, len(items), size)]

def normalize_symbol_list(symbols_csv: str) -> List[str]:
    return [s.strip().upper() for s in symbols_csv.split(",") if s.strip()]

def parse_kline_rest_row(row: list) -> dict:
    total_volume = float(row[5])
    taker_buy_base = float(row[9])
    buy_volume = taker_buy_base
    sell_volume = max(total_volume - taker_buy_base, 0.0)
    return {
        "open_time": int(row[0]),
        "close_time": int(row[6]),
        "buy": buy_volume,
        "sell": sell_volume,
        "close": float(row[4]),
    }

def compute_spike_percent(current_value: float, historical_values: List[float]) -> Tuple[Optional[float], Optional[float]]:
    if not historical_values:
        return None, None
    avg_value = sum(historical_values) / len(historical_values)
    if avg_value <= 0:
        return None, None
    spike_percent = ((current_value - avg_value) / avg_value) * 100.0
    return spike_percent, avg_value

def human_elapsed_in_candle(open_time_ms: int, interval: str) -> str:
    now_ms = int(time.time() * 1000)
    elapsed_sec = max((now_ms - open_time_ms) // 1000, 0)
    max_sec = INTERVAL_TO_SECONDS.get(interval, elapsed_sec)
    elapsed_sec = min(elapsed_sec, max_sec)
    mins = elapsed_sec // 60
    secs = elapsed_sec % 60
    return f"{mins:02d}m {secs:02d}s"

async def ensure_http_session() -> aiohttp.ClientSession:
    global shared_http_session
    if shared_http_session is None or shared_http_session.closed:
        shared_http_session = aiohttp.ClientSession()
    return shared_http_session

# ============================================================
# TELEGRAM SEND QUEUE / RATE LIMIT PROTECTION
# ============================================================
async def enqueue_telegram_message(chat_id: int, text: str) -> None:
    await telegram_send_queue.put((chat_id, text))

async def telegram_sender_loop(application) -> None:
    logger.info("Telegram sender loop started.")
    while True:
        try:
            chat_id, text = await telegram_send_queue.get()

            sent = False
            attempt = 0

            while not sent and attempt < MAX_TELEGRAM_RETRIES:
                attempt += 1
                try:
                    await application.bot.send_message(chat_id=chat_id, text=text)
                    sent = True
                    logger.info("Telegram alert sent to chat_id=%s", chat_id)
                    await asyncio.sleep(MIN_TELEGRAM_SEND_DELAY_SECONDS)

                except RetryAfter as e:
                    retry_after = int(getattr(e, "retry_after", 5))
                    logger.warning(
                        "Telegram rate limit hit for chat_id=%s. Waiting %s seconds before retry.",
                        chat_id,
                        retry_after,
                    )
                    await asyncio.sleep(retry_after + 1)

                except (TimedOut, NetworkError) as e:
                    logger.warning(
                        "Temporary Telegram network error for chat_id=%s: %s. Retrying in 3 seconds.",
                        chat_id,
                        e,
                    )
                    await asyncio.sleep(3)

                except Exception as e:
                    logger.warning(
                        "Failed sending Telegram alert to chat_id=%s on attempt %s: %s",
                        chat_id,
                        attempt,
                        e,
                    )
                    await asyncio.sleep(2)

            if not sent:
                logger.error("Dropped Telegram alert after %s attempts for chat_id=%s", MAX_TELEGRAM_RETRIES, chat_id)

            telegram_send_queue.task_done()

        except asyncio.CancelledError:
            logger.info("Telegram sender loop cancelled.")
            raise
        except Exception as e:
            logger.exception("Unexpected error in telegram_sender_loop: %s", e)
            await asyncio.sleep(2)

async def reply_text_safe(update: Update, text: str) -> None:
    if update.message is None:
        return

    try:
        await update.message.reply_text(text)
    except RetryAfter as e:
        retry_after = int(getattr(e, "retry_after", 5))
        logger.warning("Direct reply rate-limited. Waiting %s seconds.", retry_after)
        await asyncio.sleep(retry_after + 1)
        await update.message.reply_text(text)
    except Exception as e:
        logger.warning("Failed sending direct reply: %s", e)

# ============================================================
# BINANCE DATA
# ============================================================
async def fetch_all_usdt_symbols(force: bool = False) -> List[str]:
    global all_usdt_symbols_cache, all_usdt_symbols_last_refresh

    now = time.time()
    if (not force) and all_usdt_symbols_cache and (now - all_usdt_symbols_last_refresh < SYMBOL_REFRESH_SECONDS):
        return all_usdt_symbols_cache

    session = await ensure_http_session()
    async with session.get(f"{BINANCE_REST}/api/v3/exchangeInfo") as resp:
        resp.raise_for_status()
        data = await resp.json()

    symbols = []
    for s in data.get("symbols", []):
        if (
            s.get("status") == "TRADING"
            and s.get("quoteAsset") == "USDT"
            and s.get("isSpotTradingAllowed") is True
        ):
            symbols.append(s["symbol"])

    symbols = sorted(set(symbols))
    all_usdt_symbols_cache = symbols
    all_usdt_symbols_last_refresh = now
    logger.info("Loaded %d USDT spot symbols", len(symbols))
    return symbols

async def fetch_klines(symbol: str, interval: str, limit: int) -> List[list]:
    session = await ensure_http_session()
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    }
    async with session.get(f"{BINANCE_REST}/api/v3/klines", params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data

async def preload_symbol_history(symbol: str, interval: str, needed_closed_candles: int) -> None:
    key = (symbol, interval)
    existing = candle_cache[key]
    if len(existing) >= needed_closed_candles + 2:
        return

    try:
        rows = await fetch_klines(symbol, interval, limit=max(needed_closed_candles + 5, 30))
        parsed = [parse_kline_rest_row(r) for r in rows]
        dq = candle_cache[key]
        dq.clear()
        dq.extend(parsed)
    except Exception as e:
        logger.warning("Preload failed for %s %s: %s", symbol, interval, e)

# ============================================================
# RULE RESOLUTION
# ============================================================
async def resolve_rule_symbols(rule: dict) -> List[str]:
    if rule["symbols_mode"] == "ALL_USDT":
        return await fetch_all_usdt_symbols()
    return [s.upper() for s in rule["symbols"]]

async def rebuild_tracking_map() -> None:
    tracked_symbols_by_interval.clear()
    rules = await get_all_rules()
    if not rules:
        return

    await fetch_all_usdt_symbols()

    max_needed_per_interval: Dict[str, int] = defaultdict(lambda: DEFAULT_BASELINE_CANDLES)

    for rule in rules:
        interval = rule["interval"]
        if interval not in VALID_INTERVALS:
            continue

        max_needed_per_interval[interval] = max(max_needed_per_interval[interval], rule["baseline_candles"])
        symbols = await resolve_rule_symbols(rule)
        for sym in symbols:
            tracked_symbols_by_interval[interval].add(sym)

    preload_tasks = []
    for interval, symbols in tracked_symbols_by_interval.items():
        needed = max_needed_per_interval[interval]
        for sym in symbols:
            preload_tasks.append(preload_symbol_history(sym, interval, needed))

    if preload_tasks:
        for batch in chunked(preload_tasks, 150):
            await asyncio.gather(*batch, return_exceptions=True)

# ============================================================
# ALERT EVALUATION
# ============================================================
async def evaluate_live_candle(application, symbol: str, interval: str, live_candle: dict) -> None:
    rules = await get_all_rules()
    if not rules:
        return

    for rule in rules:
        if rule["interval"] != interval:
            continue

        if rule["symbols_mode"] == "CUSTOM":
            if symbol not in [s.upper() for s in rule["symbols"]]:
                continue

        side = rule["side"]
        threshold = rule["threshold_percent"]
        baseline_candles = rule["baseline_candles"]

        key = (symbol, interval)
        candles = list(candle_cache[key])

        if len(candles) < baseline_candles + 1:
            continue

        current = candles[-1]
        previous = candles[-(baseline_candles + 1):-1]

        current_value = current[side]
        previous_values = [c[side] for c in previous]

        spike_pct, avg_value = compute_spike_percent(current_value, previous_values)
        if spike_pct is None or avg_value is None:
            continue

        alert_key = (rule["id"], symbol, interval, current["open_time"])
        if alert_key in alerted_candles:
            continue

        if spike_pct >= threshold:
            elapsed = human_elapsed_in_candle(current["open_time"], interval)
            msg = (
                f"📣 {side.upper()} VOLUME SPIKE\n\n"
                f"Symbol: {symbol}\n"
                f"Interval: {interval}\n"
                f"Threshold: {threshold:.2f}%\n"
                f"Baseline: last {baseline_candles} candles\n"
                f"Live {side} volume: {current_value:,.6f}\n"
                f"Average {side} volume: {avg_value:,.6f}\n"
                f"Spike: +{spike_pct:.2f}%\n"
                f"Price: {current['close']}\n"
                f"Elapsed in candle: {elapsed}\n"
                f"Mode: once per candle"
            )

            try:
                alerted_candles.add(alert_key)
                await enqueue_telegram_message(rule["chat_id"], msg)

                logger.info(
                    "Alert queued | rule=%s symbol=%s interval=%s side=%s spike=%.2f%%",
                    rule["id"], symbol, interval, side, spike_pct
                )
            except Exception as e:
                alerted_candles.discard(alert_key)
                logger.warning("Failed queueing Telegram alert: %s", e)

def purge_old_alert_keys() -> None:
    now_ms = int(time.time() * 1000)
    to_remove = set()

    for rule_id, symbol, interval, candle_open_time in alerted_candles:
        interval_ms = INTERVAL_TO_SECONDS.get(interval, 300) * 1000
        if now_ms - candle_open_time > interval_ms * 3:
            to_remove.add((rule_id, symbol, interval, candle_open_time))

    if to_remove:
        alerted_candles.difference_update(to_remove)

# ============================================================
# WEBSOCKET STREAMS
# ============================================================
async def websocket_worker(application, interval: str, symbols: List[str]) -> None:
    if not symbols:
        return

    streams = "/".join(f"{symbol.lower()}@kline_{interval}" for symbol in symbols)
    ws_url = f"{BINANCE_WS}?streams={streams}"

    while True:
        try:
            session = await ensure_http_session()
            logger.info("WS start | interval=%s | symbols=%d", interval, len(symbols))

            async with session.ws_connect(ws_url, heartbeat=20) as ws:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue

                    payload = json.loads(msg.data)
                    data = payload.get("data", {})
                    k = data.get("k")

                    if not k:
                        continue

                    symbol = k["s"]
                    open_time = int(k["t"])
                    close_time = int(k["T"])
                    close_price = float(k["c"])

                    total_volume = float(k["v"])
                    taker_buy_base = float(k["V"])

                    buy_volume = taker_buy_base
                    sell_volume = max(total_volume - taker_buy_base, 0.0)

                    live_candle = {
                        "open_time": open_time,
                        "close_time": close_time,
                        "buy": buy_volume,
                        "sell": sell_volume,
                        "close": close_price,
                    }

                    cache_key = (symbol, interval)
                    dq = candle_cache[cache_key]

                    if dq and dq[-1]["open_time"] == open_time:
                        dq[-1] = live_candle
                    else:
                        dq.append(live_candle)

                    await evaluate_live_candle(application, symbol, interval, live_candle)

        except asyncio.CancelledError:
            logger.info("WS cancelled | interval=%s | symbols=%d", interval, len(symbols))
            raise
        except Exception as e:
            logger.warning("WS reconnecting | interval=%s | err=%s", interval, e)
            await asyncio.sleep(5)

async def restart_streams(application) -> None:
    global ws_tasks

    for task in ws_tasks:
        task.cancel()

    if ws_tasks:
        await asyncio.gather(*ws_tasks, return_exceptions=True)

    ws_tasks = []

    await rebuild_tracking_map()

    if not tracked_symbols_by_interval:
        logger.info("No active rules. No streams started.")
        return

    for interval, symbols_set in tracked_symbols_by_interval.items():
        symbols = sorted(symbols_set)
        for group in chunked(symbols, STREAM_CHUNK_SIZE):
            task = asyncio.create_task(websocket_worker(application, interval, group))
            ws_tasks.append(task)

    logger.info("Started %d websocket task(s)", len(ws_tasks))

# ============================================================
# TELEGRAM COMMANDS
# ============================================================
HELP_TEXT = """
Volume Spike Scanner Bot

Commands:

/start
/help
/add_rule <interval> <side> <threshold_percent> <baseline_candles> <ALL_USDT or symbols>
/list_rules
/delete_rule <rule_id>
/reload

Examples:

/add_rule 5m buy 10 20 ALL_USDT
/add_rule 1h buy 7.5 20 ALL_USDT
/add_rule 5m sell 12 15 BTCUSDT,ETHUSDT,SOLUSDT

Notes:
- Alerts are live, not close-candle only
- Alerts fire once per candle only
- Next alert for the same rule/symbol comes only from the next candle
"""

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_text_safe(update, HELP_TEXT.strip())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_text_safe(update, HELP_TEXT.strip())

async def add_rule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return

    chat_id = update.effective_chat.id
    args = context.args

    if len(args) < 5:
        await reply_text_safe(
            update,
            "Usage:\n"
            "/add_rule <interval> <side> <threshold_percent> <baseline_candles> <ALL_USDT or symbols>\n\n"
            "Examples:\n"
            "/add_rule 5m buy 10 20 ALL_USDT\n"
            "/add_rule 1h sell 12.5 20 BTCUSDT,ETHUSDT"
        )
        return

    interval = args[0].strip()
    side = args[1].strip().lower()

    try:
        threshold_percent = float(args[2].strip())
        baseline_candles = int(args[3].strip())
    except ValueError:
        await reply_text_safe(update, "Threshold must be a number and baseline_candles must be an integer.")
        return

    symbol_input = " ".join(args[4:]).strip()

    if interval not in VALID_INTERVALS:
        await reply_text_safe(update, f"Interval must be one of: {', '.join(sorted(VALID_INTERVALS))}")
        return

    if side not in {"buy", "sell"}:
        await reply_text_safe(update, "Side must be either 'buy' or 'sell'.")
        return

    if threshold_percent <= 0:
        await reply_text_safe(update, "Threshold must be greater than 0.")
        return

    if baseline_candles < 1:
        await reply_text_safe(update, "Baseline candles must be at least 1.")
        return

    if symbol_input.upper() == "ALL_USDT":
        symbols_mode = "ALL_USDT"
        symbols_json = None
    else:
        symbols = normalize_symbol_list(symbol_input)
        if not symbols:
            await reply_text_safe(update, "Please provide at least one symbol or use ALL_USDT.")
            return

        # Skip live Binance validation here to avoid command hanging
        symbols_mode = "CUSTOM"
        symbols_json = json.dumps(symbols)

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            INSERT INTO rules (
                chat_id, enabled, interval, side, threshold_percent,
                baseline_candles, symbols_mode, symbols_json
            )
            VALUES (?, 1, ?, ?, ?, ?, ?, ?)
        """, (
            chat_id,
            interval,
            side,
            threshold_percent,
            baseline_candles,
            symbols_mode,
            symbols_json
        ))
        await db.commit()

    scope_text = "ALL_USDT" if symbols_mode == "ALL_USDT" else symbol_input.upper()
    await reply_text_safe(
        update,
        "Rule added.\n\n"
        f"Interval: {interval}\n"
        f"Side: {side}\n"
        f"Threshold: {threshold_percent}%\n"
        f"Baseline candles: {baseline_candles}\n"
        f"Symbols: {scope_text}\n"
        f"Mode: once per candle"
    )

    asyncio.create_task(restart_streams(context.application))

async def list_rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return

    chat_id = update.effective_chat.id
    rules = await get_chat_rules(chat_id)

    if not rules:
        await reply_text_safe(update, "No rules found.")
        return

    lines = []
    for r in rules:
        symbols_text = "ALL_USDT" if r["symbols_mode"] == "ALL_USDT" else ",".join(r["symbols"])
        enabled_text = "ON" if r["enabled"] else "OFF"
        lines.append(
            f"ID {r['id']} | {enabled_text} | {r['interval']} | {r['side']} | "
            f"{r['threshold_percent']}% | baseline {r['baseline_candles']} | {symbols_text}"
        )

    await reply_text_safe(update, "\n".join(lines))

async def delete_rule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return

    chat_id = update.effective_chat.id
    args = context.args

    if len(args) != 1:
        await reply_text_safe(update, "Usage: /delete_rule <rule_id>")
        return

    try:
        rule_id = int(args[0])
    except ValueError:
        await reply_text_safe(update, "Rule ID must be a number.")
        return

    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("DELETE FROM rules WHERE id = ? AND chat_id = ?", (rule_id, chat_id))
        await db.commit()
        deleted = cur.rowcount

    if deleted == 0:
        await reply_text_safe(update, "Rule not found.")
        return

    to_remove = {item for item in alerted_candles if item[0] == rule_id}
    if to_remove:
        alerted_candles.difference_update(to_remove)

    await reply_text_safe(update, f"Deleted rule {rule_id}.")
    asyncio.create_task(restart_streams(context.application))

async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_text_safe(update, "Scanner reloading...")
    asyncio.create_task(restart_streams(context.application))

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception: %s", context.error)

# ============================================================
# BACKGROUND MAINTENANCE
# ============================================================
async def maintenance_loop(application) -> None:
    while True:
        try:
            purge_old_alert_keys()
            await fetch_all_usdt_symbols(force=False)
        except Exception as e:
            logger.warning("Maintenance loop error: %s", e)

        await asyncio.sleep(300)

# ============================================================
# APP LIFECYCLE
# ============================================================
async def post_init(application) -> None:
    global maintenance_task, telegram_sender_task

    await init_db()
    await fetch_all_usdt_symbols(force=True)

    telegram_sender_task = asyncio.create_task(telegram_sender_loop(application))
    await restart_streams(application)

    maintenance_task = asyncio.create_task(maintenance_loop(application))
    logger.info("Bot initialized.")

async def shutdown() -> None:
    global shared_http_session, maintenance_task, telegram_sender_task

    for task in ws_tasks:
        task.cancel()
    if ws_tasks:
        await asyncio.gather(*ws_tasks, return_exceptions=True)

    if maintenance_task:
        maintenance_task.cancel()
        await asyncio.gather(maintenance_task, return_exceptions=True)

    if telegram_sender_task:
        telegram_sender_task.cancel()
        await asyncio.gather(telegram_sender_task, return_exceptions=True)

    if shared_http_session and not shared_http_session.closed:
        await shared_http_session.close()

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Please set TELEGRAM_BOT_TOKEN in your environment first.")

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("add_rule", add_rule_cmd))
    application.add_handler(CommandHandler("list_rules", list_rules_cmd))
    application.add_handler(CommandHandler("delete_rule", delete_rule_cmd))
    application.add_handler(CommandHandler("reload", reload_cmd))
    application.add_error_handler(error_handler)

    try:
        application.run_polling()
    finally:
        try:
            asyncio.run(shutdown())
        except RuntimeError:
            pass

if __name__ == "__main__":
    main()
