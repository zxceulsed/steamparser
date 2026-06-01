#!/usr/bin/env python3
"""Aiogram bot for monitoring Steam Market listings per Telegram user."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from steam_market_parser import (
    DEFAULT_EXPECT_CURRENCY_ID,
    ParserError,
    cookie_json_to_header,
    fetch_market_page,
)


STEAM_URL_RE = re.compile(r"https?://steamcommunity\.com/market/listings/[^\s]+")
BUTTON_UPDATE_COOKIES = "Обновить cookies"
BUTTON_ADD_SKIN = "Добавить скин"
BUTTON_LIST = "Мои отслеживания"
BUTTON_CHECK = "Проверить сейчас"
BUTTON_STATS = "Статистика"
BUTTON_PAUSE = "Пауза watch"
BUTTON_RESUME = "Включить watch"
BUTTON_DELETE = "Удалить watch"
BUTTON_HELP = "Помощь"


@dataclass(frozen=True)
class Settings:
    bot_token: str
    db_path: str = "steam_bot.sqlite3"
    check_interval_seconds: int = 300
    stats_interval_seconds: int = 3600
    error_alert_threshold: int = 3
    error_alert_interval_seconds: int = 3600
    expected_currency_id: int | None = DEFAULT_EXPECT_CURRENCY_ID
    default_limit: int = 20


def now_ts() -> int:
    return int(time.time())


def escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=BUTTON_UPDATE_COOKIES),
                KeyboardButton(text=BUTTON_ADD_SKIN),
            ],
            [
                KeyboardButton(text=BUTTON_LIST),
                KeyboardButton(text=BUTTON_CHECK),
            ],
            [
                KeyboardButton(text=BUTTON_STATS),
                KeyboardButton(text=BUTTON_PAUSE),
            ],
            [
                KeyboardButton(text=BUTTON_RESUME),
                KeyboardButton(text=BUTTON_DELETE),
            ],
            [
                KeyboardButton(text=BUTTON_HELP),
            ],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие или отправь Steam-ссылку",
    )


def parse_float_arg(value: str, name: str) -> float:
    try:
        return float(value.replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"{name} должен быть числом") from exc


def parse_int_arg(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} должен быть целым числом") from exc
    if parsed < 1:
        raise ValueError(f"{name} должен быть больше 0")
    return parsed


def load_dotenv(path: str = ".env") -> None:
    try:
        with open(path, "r", encoding="utf-8") as file:
            lines = file.readlines()
    except FileNotFoundError:
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()

        key, separator, value = line.partition("=")
        if not separator:
            continue

        key = key.strip()
        value = value.strip()
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        os.environ.setdefault(key, value)


def load_settings() -> Settings:
    load_dotenv()
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("Set BOT_TOKEN in .env or as an environment variable")

    expected_currency_raw = os.environ.get("EXPECT_CURRENCY_ID", str(DEFAULT_EXPECT_CURRENCY_ID))
    expected_currency_id: int | None
    if expected_currency_raw.lower() in {"any", "none"}:
        expected_currency_id = None
    else:
        expected_currency_id = int(expected_currency_raw)

    return Settings(
        bot_token=token,
        db_path=os.environ.get("STEAM_BOT_DB", "steam_bot.sqlite3"),
        check_interval_seconds=int(os.environ.get("CHECK_INTERVAL_SECONDS", "300")),
        stats_interval_seconds=int(os.environ.get("STATS_INTERVAL_SECONDS", "3600")),
        error_alert_threshold=int(os.environ.get("ERROR_ALERT_THRESHOLD", "3")),
        error_alert_interval_seconds=int(os.environ.get("ERROR_ALERT_INTERVAL_SECONDS", "3600")),
        expected_currency_id=expected_currency_id,
        default_limit=int(os.environ.get("DEFAULT_LISTING_LIMIT", "20")),
    )


class Database:
    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                cookie_json TEXT,
                cookie_header TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                last_stats_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS watches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                url TEXT NOT NULL,
                item_name TEXT,
                max_float REAL NOT NULL,
                max_markup_percent REAL NOT NULL,
                listing_limit INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_checked_at INTEGER,
                last_status TEXT,
                checks_count INTEGER NOT NULL DEFAULT 0,
                matches_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                consecutive_error_count INTEGER NOT NULL DEFAULT 0,
                last_error_alert_at INTEGER,
                last_error TEXT
            );

            CREATE TABLE IF NOT EXISTS seen_matches (
                watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
                listing_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (watch_id, listing_id)
            );
            """
        )
        self.conn.commit()
        self.migrate_schema()

    def migrate_schema(self) -> None:
        watch_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(watches)").fetchall()
        }
        if "consecutive_error_count" not in watch_columns:
            self.conn.execute(
                "ALTER TABLE watches ADD COLUMN consecutive_error_count INTEGER NOT NULL DEFAULT 0"
            )
        if "last_error_alert_at" not in watch_columns:
            self.conn.execute("ALTER TABLE watches ADD COLUMN last_error_alert_at INTEGER")
        self.conn.commit()

    def touch_user(self, telegram_id: int) -> None:
        ts = now_ts()
        self.conn.execute(
            """
            INSERT INTO users (
                telegram_id, created_at, updated_at, last_seen_at, last_stats_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                updated_at = excluded.updated_at
            """,
            (telegram_id, ts, ts, ts, ts),
        )
        self.conn.commit()

    def upsert_cookie(self, telegram_id: int, cookie_json: str, cookie_header: str) -> None:
        ts = now_ts()
        self.conn.execute(
            """
            INSERT INTO users (
                telegram_id, cookie_json, cookie_header,
                created_at, updated_at, last_seen_at, last_stats_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                cookie_json = excluded.cookie_json,
                cookie_header = excluded.cookie_header,
                updated_at = excluded.updated_at,
                last_seen_at = excluded.last_seen_at
            """,
            (telegram_id, cookie_json, cookie_header, ts, ts, ts, ts),
        )
        self.conn.commit()

    def get_user(self, telegram_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()

    def add_watch(
        self,
        telegram_id: int,
        url: str,
        item_name: str,
        max_float: float,
        max_markup_percent: float,
        listing_limit: int,
    ) -> int:
        ts = now_ts()
        cursor = self.conn.execute(
            """
            INSERT INTO watches (
                telegram_id, url, item_name, max_float, max_markup_percent,
                listing_limit, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_id,
                url,
                item_name,
                max_float,
                max_markup_percent,
                listing_limit,
                ts,
                ts,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def list_watches(self, telegram_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT * FROM watches
                WHERE telegram_id = ?
                ORDER BY is_active DESC, id ASC
                """,
                (telegram_id,),
            )
        )

    def set_watch_active(self, telegram_id: int, watch_id: int, is_active: bool) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE watches
            SET is_active = ?, updated_at = ?
            WHERE telegram_id = ? AND id = ?
            """,
            (1 if is_active else 0, now_ts(), telegram_id, watch_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def delete_watch(self, telegram_id: int, watch_id: int) -> bool:
        cursor = self.conn.execute(
            """
            DELETE FROM watches
            WHERE telegram_id = ? AND id = ?
            """,
            (telegram_id, watch_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def active_watches(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT
                    watches.*,
                    users.cookie_header
                FROM watches
                JOIN users ON users.telegram_id = watches.telegram_id
                WHERE watches.is_active = 1
                    AND users.cookie_header IS NOT NULL
                    AND users.cookie_header != ''
                ORDER BY watches.last_checked_at ASC NULLS FIRST, watches.id ASC
                """
            )
        )

    def mark_watch_ok(self, watch_id: int, item_name: str, matches_count: int) -> None:
        self.conn.execute(
            """
            UPDATE watches
            SET item_name = ?,
                checks_count = checks_count + 1,
                matches_count = matches_count + ?,
                consecutive_error_count = 0,
                last_checked_at = ?,
                last_status = 'ok',
                last_error = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (item_name, matches_count, now_ts(), now_ts(), watch_id),
        )
        self.conn.commit()

    def mark_watch_error(self, watch_id: int, error: str) -> sqlite3.Row | None:
        self.conn.execute(
            """
            UPDATE watches
            SET checks_count = checks_count + 1,
                error_count = error_count + 1,
                consecutive_error_count = consecutive_error_count + 1,
                last_checked_at = ?,
                last_status = 'error',
                last_error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (now_ts(), error[:500], now_ts(), watch_id),
        )
        self.conn.commit()
        return self.conn.execute(
            "SELECT * FROM watches WHERE id = ?",
            (watch_id,),
        ).fetchone()

    def mark_error_alert_sent(self, watch_id: int) -> None:
        self.conn.execute(
            """
            UPDATE watches
            SET last_error_alert_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now_ts(), now_ts(), watch_id),
        )
        self.conn.commit()

    def remember_match(self, watch_id: int, listing_id: str) -> bool:
        try:
            self.conn.execute(
                """
                INSERT INTO seen_matches (watch_id, listing_id, created_at)
                VALUES (?, ?, ?)
                """,
                (watch_id, listing_id, now_ts()),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def due_stats_users(self, interval_seconds: int) -> list[sqlite3.Row]:
        threshold = now_ts() - interval_seconds
        return list(
            self.conn.execute(
                """
                SELECT users.telegram_id
                FROM users
                WHERE users.last_stats_at <= ?
                    AND EXISTS (
                        SELECT 1
                        FROM watches
                        WHERE watches.telegram_id = users.telegram_id
                            AND watches.is_active = 1
                    )
                ORDER BY users.last_stats_at ASC
                """,
                (threshold,),
            )
        )

    def mark_stats_sent(self, telegram_id: int) -> None:
        self.conn.execute(
            "UPDATE users SET last_stats_at = ?, updated_at = ? WHERE telegram_id = ?",
            (now_ts(), now_ts(), telegram_id),
        )
        self.conn.commit()

    def user_stats(self, telegram_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT
                COUNT(*) AS total_watches,
                COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0) AS active_watches,
                COALESCE(SUM(checks_count), 0) AS checks_count,
                COALESCE(SUM(matches_count), 0) AS matches_count,
                COALESCE(SUM(error_count), 0) AS error_count,
                MAX(last_checked_at) AS last_checked_at
            FROM watches
            WHERE telegram_id = ?
            """,
            (telegram_id,),
        ).fetchone()
        return dict(row) if row else {}


def format_help() -> str:
    return (
        "<b>Steam Market watcher</b>\n\n"
        "1. Отправь мне <code>cookies.json</code> файлом.\n"
        "2. Добавь скин:\n"
        "<code>/add URL max_float max_markup_percent [limit]</code>\n\n"
        "Пример:\n"
        "<code>/add https://steamcommunity.com/market/listings/730/G1807209A023004?appid=730&amp;category_730_Exterior=tag_WearCategory2 0.35 2 20</code>\n\n"
        "Команды:\n"
        "<code>/list</code> - список отслеживаний\n"
        "<code>/pause ID</code> - поставить на паузу\n"
        "<code>/resume ID</code> - включить обратно\n"
        "<code>/delete ID</code> - удалить отслеживание\n"
        "<code>/check</code> - проверить сейчас\n"
        "<code>/stats</code> - статистика\n"
        "<code>/help</code> - помощь\n\n"
        "Reply-кнопки снизу дублируют основные команды."
    )


def format_add_prompt() -> str:
    return (
        "Отправь скин в формате:\n"
        "<code>URL max_float max_markup_percent [limit]</code>\n\n"
        "Пример:\n"
        "<code>https://steamcommunity.com/market/listings/730/G1807209A023004?appid=730&amp;category_730_Exterior=tag_WearCategory2 0.35 2 20</code>\n\n"
        "<code>0.35</code> - max float, <code>2</code> - max наценка % от autobuy."
    )


def parse_add_args(args: str | None, default_limit: int) -> tuple[str, float, float, int]:
    parts = (args or "").split()
    if len(parts) not in {3, 4}:
        raise ValueError("Формат: /add URL max_float max_markup_percent [limit]")

    url = parts[0]
    if not STEAM_URL_RE.fullmatch(url):
        raise ValueError("URL должен быть ссылкой steamcommunity.com/market/listings/...")

    max_float = parse_float_arg(parts[1], "max_float")
    max_markup_percent = parse_float_arg(parts[2], "max_markup_percent")
    limit = parse_int_arg(parts[3], "limit") if len(parts) == 4 else default_limit

    if max_float < 0 or max_float > 1:
        raise ValueError("max_float должен быть от 0 до 1")
    if max_markup_percent < -100:
        raise ValueError("max_markup_percent не может быть меньше -100")

    return url, max_float, max_markup_percent, limit


async def add_watch_from_args(
    message: Message,
    db: Database,
    settings: Settings,
    args: str | None,
) -> None:
    user = db.get_user(message.from_user.id)
    if not user or not user["cookie_header"]:
        await message.answer("Сначала отправь cookies.json файлом.")
        return

    try:
        url, max_float, max_markup_percent, limit = parse_add_args(args, settings.default_limit)
        market_data = await asyncio.to_thread(
            fetch_market_page,
            url,
            user["cookie_header"],
            limit,
            settings.expected_currency_id,
        )
    except (ValueError, ParserError, OSError, RuntimeError) as exc:
        await message.answer(f"Не смог добавить отслеживание: <code>{escape(exc)}</code>")
        return

    watch_id = db.add_watch(
        message.from_user.id,
        url,
        str(market_data["item_name"]),
        max_float,
        max_markup_percent,
        limit,
    )
    await message.answer(
        f"Добавил watch <code>#{watch_id}</code>\n"
        f"item: <b>{escape(market_data['item_name'])}</b>\n"
        f"autobuy: <code>{market_data['autobuy_price']}</code>\n"
        f"float &lt;= <code>{max_float}</code>, markup &lt;= <code>{max_markup_percent}%</code>",
        reply_markup=main_keyboard(),
    )


def parse_watch_id(args: str | None) -> int:
    if not args:
        raise ValueError("Укажи ID отслеживания")
    return parse_int_arg(args.strip(), "ID")


def render_listing(row: sqlite3.Row) -> str:
    state = "active" if row["is_active"] else "paused"
    item_name = row["item_name"] or "unknown item"
    last_status = row["last_status"] or "never checked"
    return (
        f"<b>#{row['id']}</b> {escape(item_name)} [{state}]\n"
        f"float &lt;= <code>{row['max_float']}</code>, "
        f"markup &lt;= <code>{row['max_markup_percent']}%</code>, "
        f"limit <code>{row['listing_limit']}</code>\n"
        f"checks: <code>{row['checks_count']}</code>, "
        f"matches: <code>{row['matches_count']}</code>, "
        f"errors: <code>{row['error_count']}</code>, "
        f"streak: <code>{row['consecutive_error_count']}</code>, "
        f"last: <code>{escape(last_status)}</code>"
    )


def render_stats(stats: dict[str, Any]) -> str:
    last_checked = stats.get("last_checked_at")
    last_checked_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_checked)) if last_checked else "never"
    return (
        "<b>Bot stats</b>\n"
        f"active watches: <code>{stats.get('active_watches', 0)}</code>\n"
        f"total watches: <code>{stats.get('total_watches', 0)}</code>\n"
        f"checks: <code>{stats.get('checks_count', 0)}</code>\n"
        f"matches: <code>{stats.get('matches_count', 0)}</code>\n"
        f"errors: <code>{stats.get('error_count', 0)}</code>\n"
        f"last check: <code>{escape(last_checked_text)}</code>\n"
        f"sent at: <code>{escape(time.strftime('%Y-%m-%d %H:%M:%S'))}</code>"
    )


def find_matches(market_data: dict[str, Any], max_float: float, max_markup_percent: float) -> list[dict[str, Any]]:
    autobuy_price = market_data.get("autobuy_price")
    if not isinstance(autobuy_price, (int, float)) or autobuy_price <= 0:
        return []

    matches: list[dict[str, Any]] = []
    max_price = autobuy_price * (1 + max_markup_percent / 100)
    for listing in market_data.get("listings", []):
        if not isinstance(listing, dict):
            continue
        listing_float = listing.get("float")
        price = listing.get("price")
        if not isinstance(listing_float, (int, float)) or not isinstance(price, (int, float)):
            continue
        if listing_float <= max_float and price <= max_price:
            markup_percent = ((price - autobuy_price) / autobuy_price) * 100
            match = dict(listing)
            match["markup_percent"] = markup_percent
            matches.append(match)
    return matches


async def fetch_market_data(row: sqlite3.Row, expected_currency_id: int | None) -> dict[str, Any]:
    return await asyncio.to_thread(
        fetch_market_page,
        row["url"],
        row["cookie_header"],
        row["listing_limit"],
        expected_currency_id,
    )


async def notify_match(bot: Bot, row: sqlite3.Row, market_data: dict[str, Any], match: dict[str, Any]) -> None:
    text = (
        "<b>Match found</b>\n"
        f"watch: <code>#{row['id']}</code>\n"
        f"item: <b>{escape(market_data.get('item_name'))}</b>\n"
        f"price: <code>{escape(match.get('price_text') or match.get('price'))}</code>\n"
        f"autobuy: <code>{market_data.get('autobuy_price')}</code>\n"
        f"markup: <code>{match.get('markup_percent', 0):.2f}%</code>\n"
        f"float: <code>{match.get('float')}</code>\n"
        f"pattern: <code>{escape(match.get('pattern'))}</code>\n"
        f"listing: <code>{escape(match.get('listing_id'))}</code>\n"
        f"{escape(row['url'])}"
    )
    await bot.send_message(row["telegram_id"], text, reply_markup=main_keyboard())


def should_send_error_alert(row: sqlite3.Row, settings: Settings) -> bool:
    if row["consecutive_error_count"] < settings.error_alert_threshold:
        return False

    last_alert_at = row["last_error_alert_at"]
    if not last_alert_at:
        return True
    return now_ts() - last_alert_at >= settings.error_alert_interval_seconds


async def notify_watch_error(bot: Bot, row: sqlite3.Row, settings: Settings) -> None:
    if not should_send_error_alert(row, settings):
        return

    text = (
        "<b>Watch error alert</b>\n"
        f"watch: <code>#{row['id']}</code>\n"
        f"item: <b>{escape(row['item_name'] or 'unknown item')}</b>\n"
        f"errors in a row: <code>{row['consecutive_error_count']}</code>\n"
        f"total errors: <code>{row['error_count']}</code>\n"
        f"last error: <code>{escape(row['last_error'])}</code>\n\n"
        "Бот жив, но этот watch не проверяется нормально. "
        "Чаще всего это cookies, валюта, бан/rate limit или изменение ответа Steam."
    )
    await bot.send_message(row["telegram_id"], text, reply_markup=main_keyboard())


async def check_watch(bot: Bot, db: Database, row: sqlite3.Row, settings: Settings) -> None:
    try:
        market_data = await fetch_market_data(row, settings.expected_currency_id)
        matches = find_matches(market_data, row["max_float"], row["max_markup_percent"])
        new_matches = 0
        for match in matches:
            listing_id = match.get("listing_id")
            if not listing_id:
                continue
            if db.remember_match(row["id"], str(listing_id)):
                new_matches += 1
                await notify_match(bot, row, market_data, match)
        db.mark_watch_ok(row["id"], str(market_data.get("item_name") or row["item_name"]), new_matches)
    except (ParserError, OSError, RuntimeError) as exc:
        logging.exception("Watch %s failed", row["id"])
        error_row = db.mark_watch_error(row["id"], str(exc))
        if error_row is not None:
            await notify_watch_error(bot, error_row, settings)
            if should_send_error_alert(error_row, settings):
                db.mark_error_alert_sent(row["id"])


async def run_checks(bot: Bot, db: Database, settings: Settings) -> None:
    for row in db.active_watches():
        await check_watch(bot, db, row, settings)


async def send_due_stats(bot: Bot, db: Database, settings: Settings) -> None:
    for row in db.due_stats_users(settings.stats_interval_seconds):
        telegram_id = row["telegram_id"]
        await bot.send_message(
            telegram_id,
            render_stats(db.user_stats(telegram_id)),
            reply_markup=main_keyboard(),
        )
        db.mark_stats_sent(telegram_id)


async def send_cookie_prompt(message: Message) -> None:
    await message.answer(
        "Отправь <code>cookies.json</code> как файл. Я сохраню cookies в SQLite.",
        reply_markup=main_keyboard(),
    )


async def send_watch_list(message: Message, db: Database) -> None:
    watches = db.list_watches(message.from_user.id)
    if not watches:
        await message.answer(
            "Пока нет отслеживаний. Добавь через кнопку или /add.",
            reply_markup=main_keyboard(),
        )
        return
    await message.answer(
        "\n\n".join(render_listing(row) for row in watches),
        reply_markup=main_keyboard(),
    )


async def run_user_check(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    watches = [
        row for row in db.active_watches()
        if row["telegram_id"] == message.from_user.id
    ]
    if not watches:
        await message.answer("Нет активных отслеживаний.", reply_markup=main_keyboard())
        return
    await message.answer(
        f"Проверяю активные watches: <code>{len(watches)}</code>",
        reply_markup=main_keyboard(),
    )
    for row in watches:
        await check_watch(bot, db, row, settings)
    await message.answer("Проверка завершена.", reply_markup=main_keyboard())


async def scheduler(bot: Bot, db: Database, settings: Settings) -> None:
    while True:
        started = now_ts()
        try:
            await run_checks(bot, db, settings)
            await send_due_stats(bot, db, settings)
        except Exception:
            logging.exception("Scheduler iteration failed")

        elapsed = now_ts() - started
        await asyncio.sleep(max(5, settings.check_interval_seconds - elapsed))


def build_router(db: Database, settings: Settings) -> Router:
    router = Router()

    @router.message(Command("start", "help"))
    async def cmd_start(message: Message) -> None:
        db.touch_user(message.from_user.id)
        await message.answer(format_help(), reply_markup=main_keyboard())

    @router.message(Command("setcookies"))
    async def cmd_setcookies(message: Message) -> None:
        db.touch_user(message.from_user.id)
        await send_cookie_prompt(message)

    @router.message(F.document)
    async def handle_document(message: Message, bot: Bot) -> None:
        db.touch_user(message.from_user.id)
        document = message.document
        if not document.file_name or not document.file_name.lower().endswith(".json"):
            await message.answer("Нужен именно JSON-файл с cookies.", reply_markup=main_keyboard())
            return

        downloaded = await bot.download(document)
        if downloaded is None:
            await message.answer("Не смог скачать файл.", reply_markup=main_keyboard())
            return

        downloaded.seek(0)
        raw = downloaded.read()
        try:
            cookie_data = json.loads(raw.decode("utf-8"))
            cookie_header = cookie_json_to_header(cookie_data)
        except (UnicodeDecodeError, json.JSONDecodeError, ParserError) as exc:
            await message.answer(
                f"Не смог разобрать cookies.json: <code>{escape(exc)}</code>",
                reply_markup=main_keyboard(),
            )
            return

        db.upsert_cookie(
            message.from_user.id,
            json.dumps(cookie_data, ensure_ascii=False),
            cookie_header,
        )
        await message.answer(
            "Cookies сохранены в SQLite. Теперь можно добавлять отслеживания через кнопку или /add.",
            reply_markup=main_keyboard(),
        )

    @router.message(Command("add"))
    async def cmd_add(message: Message, command: CommandObject) -> None:
        db.touch_user(message.from_user.id)
        await add_watch_from_args(message, db, settings, command.args)

    @router.message(Command("list"))
    async def cmd_list(message: Message) -> None:
        db.touch_user(message.from_user.id)
        await send_watch_list(message, db)

    @router.message(Command("pause"))
    async def cmd_pause(message: Message, command: CommandObject) -> None:
        db.touch_user(message.from_user.id)
        try:
            watch_id = parse_watch_id(command.args)
        except ValueError as exc:
            await message.answer(str(exc), reply_markup=main_keyboard())
            return
        if db.set_watch_active(message.from_user.id, watch_id, False):
            await message.answer(
                f"Watch <code>#{watch_id}</code> поставлен на паузу.",
                reply_markup=main_keyboard(),
            )
        else:
            await message.answer("Не нашел такой watch.", reply_markup=main_keyboard())

    @router.message(Command("resume"))
    async def cmd_resume(message: Message, command: CommandObject) -> None:
        db.touch_user(message.from_user.id)
        try:
            watch_id = parse_watch_id(command.args)
        except ValueError as exc:
            await message.answer(str(exc), reply_markup=main_keyboard())
            return
        if db.set_watch_active(message.from_user.id, watch_id, True):
            await message.answer(
                f"Watch <code>#{watch_id}</code> снова активен.",
                reply_markup=main_keyboard(),
            )
        else:
            await message.answer("Не нашел такой watch.", reply_markup=main_keyboard())

    @router.message(Command("delete", "remove"))
    async def cmd_delete(message: Message, command: CommandObject) -> None:
        db.touch_user(message.from_user.id)
        try:
            watch_id = parse_watch_id(command.args)
        except ValueError as exc:
            await message.answer(str(exc), reply_markup=main_keyboard())
            return
        if db.delete_watch(message.from_user.id, watch_id):
            await message.answer(
                f"Watch <code>#{watch_id}</code> удален из отслеживаемых.",
                reply_markup=main_keyboard(),
            )
        else:
            await message.answer("Не нашел такой watch.", reply_markup=main_keyboard())

    @router.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        db.touch_user(message.from_user.id)
        await message.answer(
            render_stats(db.user_stats(message.from_user.id)),
            reply_markup=main_keyboard(),
        )

    @router.message(Command("check"))
    async def cmd_check(message: Message, bot: Bot) -> None:
        db.touch_user(message.from_user.id)
        await run_user_check(message, bot, db, settings)

    @router.message(F.text)
    async def handle_text(message: Message, bot: Bot) -> None:
        db.touch_user(message.from_user.id)
        text = message.text or ""

        if text == BUTTON_UPDATE_COOKIES:
            await send_cookie_prompt(message)
            return
        if text == BUTTON_ADD_SKIN:
            await message.answer(format_add_prompt(), reply_markup=main_keyboard())
            return
        if text == BUTTON_LIST:
            await send_watch_list(message, db)
            return
        if text == BUTTON_CHECK:
            await run_user_check(message, bot, db, settings)
            return
        if text == BUTTON_STATS:
            await message.answer(
                render_stats(db.user_stats(message.from_user.id)),
                reply_markup=main_keyboard(),
            )
            return
        if text == BUTTON_PAUSE:
            await message.answer(
                "Чтобы поставить watch на паузу, отправь:\n<code>/pause ID</code>",
                reply_markup=main_keyboard(),
            )
            return
        if text == BUTTON_RESUME:
            await message.answer(
                "Чтобы включить watch обратно, отправь:\n<code>/resume ID</code>",
                reply_markup=main_keyboard(),
            )
            return
        if text == BUTTON_DELETE:
            await message.answer(
                "Чтобы удалить watch из отслеживаемых, отправь:\n<code>/delete ID</code>\n\n"
                "ID можно посмотреть через кнопку «Мои отслеживания».",
                reply_markup=main_keyboard(),
            )
            return
        if text == BUTTON_HELP:
            await message.answer(format_help(), reply_markup=main_keyboard())
            return

        match = STEAM_URL_RE.search(text)
        if match:
            try:
                parse_add_args(text, settings.default_limit)
            except ValueError:
                await message.answer(format_add_prompt(), reply_markup=main_keyboard())
                return

            await add_watch_from_args(message, db, settings, text)
            return

        await message.answer("Не понял сообщение. Нажми кнопку или напиши /help.", reply_markup=main_keyboard())

    return router


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    db = Database(settings.db_path)
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(db, settings))

    scheduler_task = asyncio.create_task(scheduler(bot, db, settings))
    try:
        await dispatcher.start_polling(bot)
    finally:
        scheduler_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
