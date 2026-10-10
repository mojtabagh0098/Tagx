"""Code 87 v1 — a small, file-backed Telegram group tagging bot.

The bot token is intentionally read from .env instead of being embedded in
source code. See README.md for setup and Telegram API limitations.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    User,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

APP_NAME = "Code 87 v1"
DEFAULT_OWNER_ID = 7365481512
DEFAULT_DATABASE_PATH = Path(__file__).resolve().with_name("bot_database.json")
DOTENV_PATH = Path(__file__).resolve().with_name(".env")
load_dotenv(DOTENV_PATH)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
try:
    OWNER_ID = int(os.getenv("OWNER_ID", str(DEFAULT_OWNER_ID)).strip())
except ValueError as exc:
    raise SystemExit("OWNER_ID must be an integer Telegram user ID.") from exc

DATABASE_PATH = Path(
    os.getenv("BOT_DATABASE_PATH", str(DEFAULT_DATABASE_PATH))
).expanduser()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(APP_NAME)

GROUP_TYPES = {"group", "supergroup"}
ADMIN_STATUSES = {"administrator", "creator"}
COMMAND_RE = re.compile(r"^\s*/?(panel|tg|reset)(?:@\w+)?\s*$", re.IGNORECASE)
ACCESS_PAGE_SIZE = 8
MEMBER_PAGE_SIZE = 8
TAG_BATCH_SIZE = 5
TAG_BATCH_DELAY_SECONDS = 3.05
TAG_PANEL_TTL_SECONDS = 24 * 60 * 60
VERCEL_MAX_TAG_USERS = 50


def is_vercel_runtime() -> bool:
    return (
        os.getenv("VERCEL", "").casefold() == "1"
        or os.getenv("CODE87_SERVERLESS", "").casefold() == "1"
    )


class JsonDatabase:
    """Small JSON database with atomic writes and a process-local lock."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"version": 1, "groups": {}}
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Could not read {self.path}. Keep a backup and repair the JSON before restarting."
                ) from exc
            if not isinstance(loaded, dict) or not isinstance(
                loaded.get("groups", {}), dict
            ):
                raise RuntimeError(
                    f"{self.path} does not contain a valid Code 87 database."
                )
            self._data = loaded
            self._data.setdefault("version", 1)
            self._data.setdefault("groups", {})

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(self.path.name + ".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(self._data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, self.path)

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def export_data(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def replace_data(self, data: Mapping[str, Any]) -> None:
        if not isinstance(data, Mapping) or not isinstance(
            data.get("groups", {}), dict
        ):
            raise TypeError("Database snapshot must contain a groups object.")
        replacement = copy.deepcopy(dict(data))
        replacement.setdefault("version", 1)
        replacement.setdefault("groups", {})
        with self._lock:
            self._data = replacement

    def ensure_group(self, chat_id: int, title: str = "") -> dict[str, Any]:
        key = str(int(chat_id))
        with self._lock:
            groups = self._data.setdefault("groups", {})
            changed = False
            group = groups.get(key)
            if not isinstance(group, dict):
                group = {
                    "chat_id": int(chat_id),
                    "title": title or key,
                    "allowed_admins": [],
                    "target_users": {},
                    "created_at": int(time.time()),
                }
                groups[key] = group
                changed = True
            if "chat_id" not in group:
                group["chat_id"] = int(chat_id)
                changed = True
            if "allowed_admins" not in group:
                group["allowed_admins"] = []
                changed = True
            if "target_users" not in group:
                group["target_users"] = {}
                changed = True
            if "created_at" not in group:
                group["created_at"] = int(time.time())
                changed = True
            if not isinstance(group["allowed_admins"], list):
                group["allowed_admins"] = []
                changed = True
            if not isinstance(group["target_users"], dict):
                group["target_users"] = {}
                changed = True
            if title and group.get("title") != title:
                group["title"] = title
                changed = True
            elif not isinstance(group.get("title"), str):
                group["title"] = title or key
                changed = True
            if changed:
                self._save_locked()
            return copy.deepcopy(group)

    def get_group(self, chat_id: int) -> dict[str, Any] | None:
        with self._lock:
            group = self._data.get("groups", {}).get(str(int(chat_id)))
            return copy.deepcopy(group) if isinstance(group, dict) else None

    def list_groups(self) -> list[dict[str, Any]]:
        with self._lock:
            groups = copy.deepcopy(self._data.get("groups", {}))
        return sorted(
            groups.values(),
            key=lambda item: (
                str(item.get("title", "")).casefold(),
                int(item.get("chat_id", 0)),
            ),
        )

    def update_group_title(self, chat_id: int, title: str) -> None:
        if not title:
            return
        key = str(int(chat_id))
        with self._lock:
            group = self._data.get("groups", {}).get(key)
            if isinstance(group, dict) and group.get("title") != title:
                group["title"] = title
                self._save_locked()

    @staticmethod
    def _user_record(user: User | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(user, User):
            user_id = user.id
            first_name = user.first_name or ""
            last_name = user.last_name
            username = user.username
            is_bot = user.is_bot
        else:
            user_id = int(user.get("user_id", user.get("id", 0)))
            first_name = str(user.get("first_name") or "")
            last_name = user.get("last_name")
            username = user.get("username")
            is_bot = bool(user.get("is_bot", False))
        return {
            "user_id": int(user_id),
            "first_name": first_name,
            "last_name": last_name,
            "username": username,
            "is_bot": bool(is_bot),
        }

    def upsert_user(self, chat_id: int, user: User | Mapping[str, Any]) -> bool:
        record = self._user_record(user)
        if record["user_id"] <= 0 or record["is_bot"]:
            return False
        key = str(int(chat_id))
        user_key = str(record["user_id"])
        now = int(time.time())
        with self._lock:
            group = self._data.get("groups", {}).get(key)
            if not isinstance(group, dict):
                group = self.ensure_group(chat_id)
                # ensure_group returns a copy; retrieve the stored object under the lock.
                group = self._data["groups"][key]
            targets = group.setdefault("target_users", {})
            previous = targets.get(user_key, {})
            updated = dict(record)
            last_seen = (
                int(previous.get("last_seen", 0)) if isinstance(previous, dict) else 0
            )
            updated["last_seen"] = now if now - last_seen >= 900 else last_seen
            changed = not isinstance(previous, dict) or any(
                previous.get(field) != updated.get(field)
                for field in (
                    "user_id",
                    "first_name",
                    "last_name",
                    "username",
                    "is_bot",
                    "last_seen",
                )
            )
            if changed:
                targets[user_key] = updated
                self._save_locked()
            return changed

    def remove_user(self, chat_id: int, user_id: int) -> bool:
        key = str(int(chat_id))
        user_key = str(int(user_id))
        with self._lock:
            group = self._data.get("groups", {}).get(key)
            if not isinstance(group, dict):
                return False
            changed = (
                group.setdefault("target_users", {}).pop(user_key, None) is not None
            )
            admins = group.setdefault("allowed_admins", [])
            filtered = [int(uid) for uid in admins if int(uid) != int(user_id)]
            if filtered != admins:
                group["allowed_admins"] = filtered
                changed = True
            if changed:
                self._save_locked()
            return changed

    def set_allowed_admin(self, chat_id: int, user_id: int, allowed: bool) -> bool:
        user_id = int(user_id)
        if user_id == OWNER_ID or user_id <= 0:
            return False
        key = str(int(chat_id))
        with self._lock:
            group = self._data.get("groups", {}).get(key)
            if not isinstance(group, dict):
                group = self.ensure_group(chat_id)
                group = self._data["groups"][key]
            current = {int(uid) for uid in group.setdefault("allowed_admins", [])}
            updated = set(current)
            if allowed:
                updated.add(user_id)
            else:
                updated.discard(user_id)
            if updated == current:
                return False
            group["allowed_admins"] = sorted(updated)
            self._save_locked()
            return True

    def prune_allowed_admins(self, chat_id: int, valid_user_ids: Iterable[int]) -> bool:
        key = str(int(chat_id))
        valid = {int(uid) for uid in valid_user_ids if int(uid) != OWNER_ID}
        with self._lock:
            group = self._data.get("groups", {}).get(key)
            if not isinstance(group, dict):
                return False
            existing = {int(uid) for uid in group.setdefault("allowed_admins", [])}
            updated = sorted(existing & valid)
            if updated == sorted(existing):
                return False
            group["allowed_admins"] = updated
            self._save_locked()
            return True

    def reset_group_settings(self, chat_id: int, title: str = "") -> int:
        """Reset access settings while retaining the complete target_users map."""
        key = str(int(chat_id))
        with self._lock:
            group = self._data.get("groups", {}).get(key)
            if not isinstance(group, dict):
                group = {
                    "chat_id": int(chat_id),
                    "title": title or key,
                    "allowed_admins": [],
                    "target_users": {},
                    "created_at": int(time.time()),
                }
                self._data.setdefault("groups", {})[key] = group
            if title:
                group["title"] = title
            group["allowed_admins"] = []
            group.setdefault("target_users", {})
            count = len(group["target_users"])
            self._save_locked()
            return count

    def remove_group(self, chat_id: int) -> bool:
        with self._lock:
            removed = (
                self._data.setdefault("groups", {}).pop(str(int(chat_id)), None)
                is not None
            )
            if removed:
                self._save_locked()
            return removed

    def migrate_group(
        self, old_chat_id: int, new_chat_id: int, title: str = ""
    ) -> None:
        old_key, new_key = str(int(old_chat_id)), str(int(new_chat_id))
        if old_key == new_key:
            self.update_group_title(new_chat_id, title)
            return
        with self._lock:
            groups = self._data.setdefault("groups", {})
            old_group = groups.pop(old_key, None)
            new_group = groups.get(new_key)
            if old_group is None and new_group is None:
                new_group = {
                    "chat_id": int(new_chat_id),
                    "title": title or new_key,
                    "allowed_admins": [],
                    "target_users": {},
                    "created_at": int(time.time()),
                }
            elif new_group is None:
                new_group = old_group
            elif old_group is not None:
                new_group["allowed_admins"] = sorted(
                    {int(uid) for uid in new_group.get("allowed_admins", [])}
                    | {int(uid) for uid in old_group.get("allowed_admins", [])}
                )
                new_group.setdefault("target_users", {}).update(
                    old_group.get("target_users", {})
                )
            assert new_group is not None
            new_group["chat_id"] = int(new_chat_id)
            if title:
                new_group["title"] = title
            groups[new_key] = new_group
            self._save_locked()


DB = JsonDatabase(DATABASE_PATH)


def clean_title(title: str | None, fallback: str = "گروه بدون نام") -> str:
    value = " ".join((title or "").replace("\n", " ").replace("\r", " ").split())
    return value[:120] or fallback


def display_name(user: Mapping[str, Any]) -> str:
    name = " ".join(
        part
        for part in (
            str(user.get("first_name") or "").strip(),
            str(user.get("last_name") or "").strip(),
        )
        if part
    )
    return name or f"کاربر {user.get('user_id', user.get('id', '?'))}"


def format_mention(user: Mapping[str, Any]) -> str:
    """Create a MarkdownV2 mention using @username or a tg://user link."""
    user_id = int(user.get("user_id", user.get("id", 0)))
    username = str(user.get("username") or "").lstrip("@")
    if username:
        return escape_markdown(f"@{username}", version=2)
    name = escape_markdown(display_name(user), version=2)
    link = escape_markdown(
        f"tg://user?id={user_id}", version=2, entity_type="text_link"
    )
    return f"[{name}]({link})"


def chunks(items: Sequence[Any], size: int = TAG_BATCH_SIZE) -> list[list[Any]]:
    if size <= 0:
        raise ValueError("size must be positive")
    return [list(items[index : index + size]) for index in range(0, len(items), size)]


def tag_panel_markup(owner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👑 تگ مالک و مدیران گروه", callback_data=f"tg:r:{owner_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "👥 انتخاب اعضا برای تگ", callback_data=f"tg:m:{owner_id}:0"
                )
            ],
            [
                InlineKeyboardButton(
                    "✖️ بستن پنل", callback_data=f"close_panel_{owner_id}"
                )
            ],
        ]
    )


def close_markup(owner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✖️ بستن پنل", callback_data=f"close_panel_{owner_id}")]]
    )


def _tag_panels(application: Application) -> dict[str, dict[str, Any]]:
    return application.bot_data.setdefault("tag_panels", {})


def _panel_key(chat_id: int, message_id: int) -> str:
    return f"{int(chat_id)}:{int(message_id)}"


def _prune_expired_panels(application: Application) -> None:
    panels = _tag_panels(application)
    cutoff = int(time.time()) - TAG_PANEL_TTL_SECONDS
    for key, state in list(panels.items()):
        if int(state.get("last_used", 0)) < cutoff:
            panels.pop(key, None)


def _user_button_label(user: Mapping[str, Any]) -> str:
    name = display_name(user)
    if len(name) > 42:
        name = name[:39] + "…"
    return name


def _is_group_chat(chat: Any) -> bool:
    return bool(chat and getattr(chat, "type", None) in GROUP_TYPES)


def _authorized_in_database(chat_id: int, user_id: int) -> bool:
    group = DB.get_group(chat_id)
    if not group:
        return False
    return int(user_id) in {int(value) for value in group.get("allowed_admins", [])}


async def is_authorized_tagger(user_id: int, chat: Any, bot: Any) -> bool:
    if not _is_group_chat(chat):
        return False
    if int(user_id) == OWNER_ID:
        return True
    if not _authorized_in_database(chat.id, user_id):
        return False
    # Fail closed if the allowlisted manager is no longer a real group admin.
    try:
        member = await bot.get_chat_member(chat.id, int(user_id))
    except TelegramError as exc:
        logger.warning(
            "Could not verify manager %s in chat %s: %s", user_id, chat.id, exc
        )
        return False
    return getattr(member, "status", "") in ADMIN_STATUSES


async def load_group_admins(bot: Any, chat_id: int) -> list[User]:
    """Fetch real Telegram admins, sync their profiles, and prune stale grants."""
    members = await bot.get_chat_administrators(chat_id)
    users: list[User] = []
    valid_ids: set[int] = set()
    for member in members:
        user = member.user
        if user.is_bot or user.id == OWNER_ID:
            continue
        valid_ids.add(user.id)
        DB.upsert_user(chat_id, user)
        users.append(user)
    DB.prune_allowed_admins(chat_id, valid_ids)
    users.sort(key=lambda user: (user.first_name.casefold(), user.id))
    return users


def access_markup(
    users: Sequence[User], chat_id: int, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    page_count = max(1, (len(users) + ACCESS_PAGE_SIZE - 1) // ACCESS_PAGE_SIZE)
    page = max(0, min(page, page_count - 1))
    group = DB.get_group(chat_id) or {}
    allowed = {int(uid) for uid in group.get("allowed_admins", [])}
    selected = users[page * ACCESS_PAGE_SIZE : (page + 1) * ACCESS_PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
    if selected:
        for user in selected:
            mark = "✅" if user.id in allowed else "➕"
            label = _user_button_label(
                {"first_name": user.first_name, "last_name": user.last_name}
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{mark} {label}", callback_data=f"ap:t:{user.id}:{page}"
                    )
                ]
            )
    else:
        rows.append(
            [InlineKeyboardButton("مدیر واجد شرایطی پیدا نشد", callback_data="ap:r:0")]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"ap:p:{page - 1}"))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"ap:p:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton("🔄 تازه‌سازی", callback_data=f"ap:r:{page}"),
            InlineKeyboardButton("✖️ بستن پنل", callback_data=f"close_panel_{OWNER_ID}"),
        ]
    )
    text = (
        "پنل مدیریت دسترسی مدیران گروه\n"
        "✅ یعنی مجاز به اجرای /tg است. برای افزودن/حذف، روی نام مدیر بزنید.\n"
        f"صفحه {page + 1} از {page_count} — مدیران مجاز: {len(allowed)}"
    )
    return text, InlineKeyboardMarkup(rows)


async def send_access_panel(
    message: Any, context: ContextTypes.DEFAULT_TYPE, chat_id: int
) -> None:
    DB.ensure_group(chat_id, clean_title(message.chat.title, str(chat_id)))
    try:
        users = await load_group_admins(context.bot, chat_id)
    except TelegramError as exc:
        logger.warning("Unable to list admins in %s: %s", chat_id, exc)
        await message.reply_text(
            "فهرست مدیران دریافت نشد. ربات را به مدیر گروه ارتقا دهید و دوباره /panel را بزنید."
        )
        return
    text, markup = access_markup(users, chat_id, 0)
    await message.reply_text(text, reply_markup=markup)


async def render_access_panel(
    query: Any, context: ContextTypes.DEFAULT_TYPE, page: int
) -> None:
    chat = query.message.chat
    DB.ensure_group(chat.id, clean_title(chat.title, str(chat.id)))
    users = await load_group_admins(context.bot, chat.id)
    text, markup = access_markup(users, chat.id, page)
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def get_role_targets(bot: Any, chat_id: int) -> list[dict[str, Any]]:
    """Return the bot owner followed by current human Telegram group admins."""
    group = DB.get_group(chat_id) or DB.ensure_group(chat_id)
    targets_by_id: dict[int, dict[str, Any]] = {}
    for member in await bot.get_chat_administrators(chat_id):
        user = member.user
        if user.is_bot:
            continue
        DB.upsert_user(chat_id, user)
        targets_by_id[user.id] = JsonDatabase._user_record(user)

    owner_data = targets_by_id.get(OWNER_ID)
    if owner_data is None:
        owner_record = (group.get("target_users", {}) or {}).get(str(OWNER_ID))
        owner_data = (
            dict(owner_record)
            if isinstance(owner_record, dict)
            else {
                "user_id": OWNER_ID,
                "first_name": "مالک ربات",
                "last_name": None,
                "username": None,
            }
        )
        try:
            owner_member = await bot.get_chat_member(chat_id, OWNER_ID)
            owner_data.update(JsonDatabase._user_record(owner_member.user))
        except TelegramError:
            # The bot owner is included even if they have not joined this group.
            pass
    targets_by_id[OWNER_ID] = owner_data
    # Put the bot owner first, then other human group admins in stable order.
    return [owner_data] + [
        targets_by_id[user_id]
        for user_id in sorted(targets_by_id)
        if user_id != OWNER_ID
    ]


async def send_tag_menu(
    message: Any, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> None:
    _prune_expired_panels(context.application)
    sent = await message.reply_text(
        "منوی تگ سریع — یکی از روش‌ها را انتخاب کنید:",
        reply_markup=tag_panel_markup(user_id),
    )
    _tag_panels(context.application)[_panel_key(sent.chat_id, sent.message_id)] = {
        "creator_id": int(user_id),
        "selected": set(),
        "page": 0,
        "sending": False,
        "last_used": int(time.time()),
    }


def member_selector_markup(
    creator_id: int,
    users: Sequence[Mapping[str, Any]],
    selected_ids: set[int],
    page: int,
) -> tuple[str, InlineKeyboardMarkup, int]:
    page_count = max(1, (len(users) + MEMBER_PAGE_SIZE - 1) // MEMBER_PAGE_SIZE)
    page = max(0, min(page, page_count - 1))
    start = page * MEMBER_PAGE_SIZE
    visible = users[start : start + MEMBER_PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
    for user in visible:
        user_id = int(user.get("user_id", user.get("id", 0)))
        check = "☑️" if user_id in selected_ids else "⬜"
        label = _user_button_label(user)
        rows.append(
            [
                InlineKeyboardButton(
                    f"{check} {label}",
                    callback_data=f"tg:u:{creator_id}:{user_id}:{page}",
                )
            ]
        )
    if len(users) == 0:
        rows.append(
            [
                InlineKeyboardButton(
                    "هنوز عضوی ثبت نشده", callback_data=f"tg:m:{creator_id}:0"
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                "◀️ قبلی", callback_data=f"tg:m:{creator_id}:{page - 1}"
            )
        )
    if page + 1 < page_count:
        nav.append(
            InlineKeyboardButton(
                "بعدی ▶️", callback_data=f"tg:m:{creator_id}:{page + 1}"
            )
        )
    if nav:
        rows.append(nav)
    if users:
        rows.append(
            [
                InlineKeyboardButton(
                    f"📣 تگ {len(selected_ids)} عضو انتخاب‌شده",
                    callback_data=f"tg:s:{creator_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("↩️ بازگشت", callback_data=f"tg:b:{creator_id}"),
            InlineKeyboardButton(
                "✖️ بستن پنل", callback_data=f"close_panel_{creator_id}"
            ),
        ]
    )
    text = (
        "اعضای موردنظر را انتخاب کنید و سپس دکمه تگ را بزنید.\n"
        f"صفحه {page + 1} از {page_count} — انتخاب‌شده: {len(selected_ids)}"
    )
    if not users:
        text += "\nربات نمی‌تواند فهرست کامل اعضای قدیمی گروه را از تلگرام بگیرد؛ اعضا پس از پیام‌دادن یا ورود ثبت می‌شوند."
    return text, InlineKeyboardMarkup(rows), page


async def render_member_selector(
    query: Any, context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], page: int
) -> None:
    chat = query.message.chat
    group = DB.get_group(chat.id) or {}
    user_records = list((group.get("target_users", {}) or {}).values())
    user_records.sort(
        key=lambda user: (display_name(user).casefold(), int(user.get("user_id", 0)))
    )
    available_ids = {int(user.get("user_id", 0)) for user in user_records}
    selected = {
        int(uid) for uid in state.get("selected", set()) if int(uid) in available_ids
    }
    state["selected"] = selected
    state["last_used"] = int(time.time())
    text, markup, actual_page = member_selector_markup(
        int(state["creator_id"]), user_records, selected, page
    )
    state["page"] = actual_page
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def _safe_edit_panel(
    bot: Any,
    chat_id: int,
    message_id: int,
    text: str,
    markup: InlineKeyboardMarkup | None,
) -> None:
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=markup,
        )
    except TelegramError as exc:
        logger.debug("Could not update tag panel %s/%s: %s", chat_id, message_id, exc)


def _retry_delay(exc: RetryAfter) -> float:
    delay = getattr(exc, "retry_after", 1)
    if hasattr(delay, "total_seconds"):
        delay = delay.total_seconds()
    return max(0.0, float(delay)) + 0.25


async def send_tag_batches(
    application: Application,
    chat_id: int,
    message_id: int,
    panel_key: str,
    creator_id: int,
    users: Sequence[Mapping[str, Any]],
) -> None:
    batches = chunks(list(users), TAG_BATCH_SIZE)
    sent_count = 0
    failed = False
    for index, batch in enumerate(batches):
        text = "\n".join(format_mention(user) for user in batch)
        try:
            while True:
                try:
                    await application.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=ParseMode.MARKDOWN_V2,
                        disable_web_page_preview=True,
                    )
                    break
                except RetryAfter as exc:
                    await asyncio.sleep(_retry_delay(exc))
            sent_count += len(batch)
        except TelegramError:
            logger.exception("Failed to send tag batch in chat %s", chat_id)
            failed = True
            break
        if index + 1 < len(batches):
            await asyncio.sleep(TAG_BATCH_DELAY_SECONDS)

    panels = _tag_panels(application)
    state = panels.get(panel_key)
    if state is not None:
        state["sending"] = False
        state["selected"] = set()
        state["last_used"] = int(time.time())
        if failed:
            final_text = f"ارسال متوقف شد؛ تا اینجا {sent_count} تگ ارسال شد. دسترسی و محدودیت‌های ربات را بررسی کنید."
        else:
            final_text = f"✅ ارسال تگ‌ها تمام شد: {sent_count} کاربر در {len(batches)} بستهٔ حداکثر پنج‌تایی."
        await _safe_edit_panel(
            application.bot,
            chat_id,
            message_id,
            final_text,
            tag_panel_markup(creator_id),
        )


async def start_tag_job(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    panel_key: str,
    state: dict[str, Any],
    users: Sequence[Mapping[str, Any]],
) -> None:
    if not users:
        await query.answer(
            "برای این عملیات عضوی برای تگ‌کردن پیدا نشد.", show_alert=True
        )
        return
    if is_vercel_runtime() and len(users) > VERCEL_MAX_TAG_USERS:
        await query.answer(
            f"در Vercel هر بار حداکثر {VERCEL_MAX_TAG_USERS} نفر را تگ کنید؛ انتخاب را کوچک‌تر کنید.",
            show_alert=True,
        )
        return
    state["sending"] = True
    state["last_used"] = int(time.time())
    creator_id = int(state["creator_id"])
    await query.answer("ارسال شروع شد؛ بسته‌ها با فاصله ارسال می‌شوند.")
    try:
        await query.edit_message_text(
            f"⏳ در حال ارسال {len(users)} تگ در بسته‌های پنج‌تایی…",
            reply_markup=close_markup(creator_id),
        )
    except TelegramError as exc:
        if not (
            isinstance(exc, BadRequest)
            and "message is not modified" in str(exc).lower()
        ):
            state["sending"] = False
            logger.warning("Could not switch tag panel to sending state: %s", exc)
            return
    batch_args = (
        context.application,
        query.message.chat.id,
        query.message.message_id,
        panel_key,
        creator_id,
        list(users),
    )
    if is_vercel_runtime():
        # Serverless platforms may freeze unawaited tasks as soon as the webhook
        # response is returned, so complete the bounded job before replying.
        await send_tag_batches(*batch_args)
        return
    context.application.create_task(
        send_tag_batches(*batch_args),
        name=f"tag-batches-{query.message.chat.id}-{query.message.message_id}",
    )


async def handle_close_callback(query: Any, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        creator_id = int(str(query.data).removeprefix("close_panel_"))
    except ValueError:
        await query.answer("دکمه نامعتبر است.", show_alert=True)
        return
    actor = query.from_user
    chat = query.message.chat if query.message else None
    if actor is None or chat is None:
        await query.answer("این پنل دیگر در دسترس نیست.", show_alert=True)
        return
    if actor.id != OWNER_ID and actor.id != creator_id:
        await query.answer(
            "شما فقط می‌توانید پنلی را ببندید که خودتان ساخته‌اید.", show_alert=True
        )
        return
    if actor.id != OWNER_ID and not await is_authorized_tagger(
        actor.id, chat, context.bot
    ):
        await query.answer("دسترسی شما به این پنل لغو شده است.", show_alert=True)
        return
    key = _panel_key(chat.id, query.message.message_id)
    _tag_panels(context.application).pop(key, None)
    await query.answer("پنل بسته شد.")
    try:
        await query.edit_message_text("پنل بسته شد.", reply_markup=None)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.debug("Could not close panel message: %s", exc)


async def handle_access_callback(
    query: Any, context: ContextTypes.DEFAULT_TYPE
) -> None:
    actor = query.from_user
    chat = query.message.chat if query.message else None
    if actor is None or actor.id != OWNER_ID:
        await query.answer(
            "فقط مالک ربات می‌تواند دسترسی مدیران را تغییر دهد.", show_alert=True
        )
        return
    if not _is_group_chat(chat):
        await query.answer("این پنل فقط در گروه قابل استفاده است.", show_alert=True)
        return
    parts = str(query.data).split(":")
    action = parts[1] if len(parts) > 1 else ""
    try:
        if action == "t" and len(parts) == 4:
            user_id, page = int(parts[2]), int(parts[3])
            DB.ensure_group(chat.id, clean_title(chat.title, str(chat.id)))
            users = await load_group_admins(context.bot, chat.id)
            current_user = next((user for user in users if user.id == user_id), None)
            if current_user is None:
                await query.answer(
                    "این کاربر دیگر مدیر این گروه نیست.", show_alert=True
                )
                await render_access_panel(query, context, page)
                return
            was_allowed = _authorized_in_database(chat.id, user_id)
            DB.set_allowed_admin(chat.id, user_id, not was_allowed)
            await query.answer(
                "دسترسی برداشته شد." if was_allowed else "دسترسی /tg داده شد."
            )
            await render_access_panel(query, context, page)
            return
        if action in {"p", "r"} and len(parts) == 3:
            page = max(0, int(parts[2]))
            await query.answer("فهرست به‌روز شد." if action == "r" else None)
            await render_access_panel(query, context, page)
            return
    except (ValueError, TelegramError) as exc:
        logger.warning("Access panel callback failed in %s: %s", chat.id, exc)
        try:
            await query.answer(
                "پنل به‌روزرسانی نشد؛ ربات را مدیر گروه کنید و دوباره تلاش کنید.",
                show_alert=True,
            )
        except TelegramError:
            pass
        return
    await query.answer("دکمه نامعتبر یا منقضی شده است.", show_alert=True)


async def handle_tag_callback(query: Any, context: ContextTypes.DEFAULT_TYPE) -> None:
    actor = query.from_user
    chat = query.message.chat if query.message else None
    if actor is None or not await is_authorized_tagger(actor.id, chat, context.bot):
        await query.answer("شما اجازه استفاده از پنل تگ را ندارید.", show_alert=True)
        return
    parts = str(query.data).split(":")
    if len(parts) < 3:
        await query.answer("دکمه نامعتبر است.", show_alert=True)
        return
    action = parts[1]
    try:
        creator_id = int(parts[2])
    except ValueError:
        await query.answer("دکمه نامعتبر است.", show_alert=True)
        return
    panel_key = _panel_key(chat.id, query.message.message_id)
    state = _tag_panels(context.application).get(panel_key)
    if not state or int(state.get("creator_id", -1)) != creator_id:
        await query.answer(
            "این پنل منقضی شده است؛ دوباره /tg را بزنید.", show_alert=True
        )
        return
    now = int(time.time())
    if now - int(state.get("last_used", 0)) > TAG_PANEL_TTL_SECONDS:
        _tag_panels(context.application).pop(panel_key, None)
        await query.answer(
            "این پنل منقضی شده است؛ دوباره /tg را بزنید.", show_alert=True
        )
        return
    state["last_used"] = now
    if state.get("sending"):
        await query.answer("ارسال این پنل هنوز در حال انجام است.", show_alert=True)
        return

    if action == "r" and len(parts) == 3:
        try:
            users = await get_role_targets(context.bot, chat.id)
        except TelegramError as exc:
            logger.warning("Could not load role targets in %s: %s", chat.id, exc)
            state["sending"] = False
            await query.answer("فهرست مدیران برای تگ دریافت نشد.", show_alert=True)
            try:
                await query.edit_message_text(
                    "فهرست مدیران برای تگ دریافت نشد. ربات را مدیر گروه کنید و دوباره تلاش کنید.",
                    reply_markup=tag_panel_markup(creator_id),
                )
            except TelegramError:
                pass
            return
        await start_tag_job(query, context, panel_key, state, users)
        return
    if action == "b" and len(parts) == 3:
        await query.answer()
        try:
            await query.edit_message_text(
                "منوی تگ سریع — یکی از روش‌ها را انتخاب کنید:",
                reply_markup=tag_panel_markup(creator_id),
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
        return
    if action == "m" and len(parts) == 4:
        try:
            page = max(0, int(parts[3]))
        except ValueError:
            await query.answer("صفحه نامعتبر است.", show_alert=True)
            return
        await query.answer()
        await render_member_selector(query, context, state, page)
        return
    if action == "u" and len(parts) == 5:
        try:
            user_id, page = int(parts[3]), max(0, int(parts[4]))
        except ValueError:
            await query.answer("عضو یا صفحه نامعتبر است.", show_alert=True)
            return
        selected = {int(uid) for uid in state.get("selected", set())}
        if user_id in selected:
            selected.remove(user_id)
        else:
            if is_vercel_runtime() and len(selected) >= VERCEL_MAX_TAG_USERS:
                await query.answer(
                    f"در Vercel حداکثر {VERCEL_MAX_TAG_USERS} نفر را در هر نوبت انتخاب کنید.",
                    show_alert=True,
                )
                return
            selected.add(user_id)
        state["selected"] = selected
        await query.answer("انتخاب به‌روز شد.")
        await render_member_selector(query, context, state, page)
        return
    if action == "s" and len(parts) == 3:
        group = DB.get_group(chat.id) or {}
        target_users = group.get("target_users", {}) or {}
        selected = {int(uid) for uid in state.get("selected", set())}
        users = [
            dict(target_users[str(user_id)])
            for user_id in selected
            if str(user_id) in target_users
        ]
        users.sort(
            key=lambda user: (
                display_name(user).casefold(),
                int(user.get("user_id", 0)),
            )
        )
        await start_tag_job(query, context, panel_key, state, users)
        return
    await query.answer("دکمه نامعتبر یا منقضی شده است.", show_alert=True)


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    data = str(query.data or "")
    try:
        if data.startswith("close_panel_"):
            await handle_close_callback(query, context)
        elif data.startswith("ap:"):
            await handle_access_callback(query, context)
        elif data.startswith("tg:"):
            await handle_tag_callback(query, context)
        else:
            await query.answer("این دکمه شناخته‌شده نیست.", show_alert=True)
    except TelegramError as exc:
        logger.warning("Callback failed: %s", exc)
        try:
            await query.answer("خطایی رخ داد؛ دوباره تلاش کنید.", show_alert=True)
        except TelegramError:
            pass


async def _send_text_in_chunks(message: Any, text: str, max_chars: int = 3500) -> None:
    lines = text.splitlines() or [text]
    current = ""
    for line in lines:
        if len(line) > max_chars:
            line = line[: max_chars - 1] + "…"
        if current and len(current) + len(line) + 1 > max_chars:
            await message.reply_text(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        await message.reply_text(current)


def _definitive_membership_error(exc: TelegramError) -> bool:
    message = str(exc).casefold()
    phrases = (
        "chat not found",
        "bot was kicked",
        "bot is not a member",
        "bot was removed",
        "chat was deleted",
        "group chat was deleted",
        "user not found",
        "kicked from the",
    )
    return any(phrase in message for phrase in phrases)


async def live_check_groups(message: Any, context: ContextTypes.DEFAULT_TYPE) -> None:
    groups = DB.list_groups()
    if not groups:
        await message.reply_text("هنوز گروهی در دیتابیس ثبت نشده است.")
        return
    try:
        bot_id = (await context.bot.get_me()).id
    except TelegramError as exc:
        logger.error("Could not identify the bot during live group check: %s", exc)
        await message.reply_text(
            "استعلام زنده انجام نشد؛ ارتباط ربات با تلگرام را بررسی کنید."
        )
        return

    active: list[tuple[str, int]] = []
    unknown: list[tuple[str, int]] = []
    removed: list[tuple[str, int]] = []
    for group in groups:
        chat_id = int(group.get("chat_id", 0))
        title = clean_title(group.get("title"), str(chat_id))
        try:
            bot_member = await context.bot.get_chat_member(chat_id, bot_id)
        except TelegramError as exc:
            if _definitive_membership_error(exc):
                DB.remove_group(chat_id)
                removed.append((title, chat_id))
            else:
                logger.warning("Live check inconclusive for %s: %s", chat_id, exc)
                unknown.append((title, chat_id))
            continue
        if getattr(bot_member, "status", "") in {"left", "kicked"}:
            DB.remove_group(chat_id)
            removed.append((title, chat_id))
            continue
        try:
            chat = await context.bot.get_chat(chat_id)
            title = clean_title(getattr(chat, "title", None), title)
            DB.update_group_title(chat_id, title)
        except TelegramError as exc:
            logger.info(
                "Group %s is present, but its title could not be refreshed: %s",
                chat_id,
                exc,
            )
        active.append((title, chat_id))

    lines = [
        "استعلام زنده گروه‌ها انجام شد.",
        f"✅ ربات عضو است: {len(active)}",
        f"⚠️ وضعیت نامشخص (در دیتابیس حفظ شد): {len(unknown)}",
        f"🗑 از دیتابیس حذف شد: {len(removed)}",
        "",
    ]
    if active:
        lines.append("گروه‌های فعال:")
        lines.extend(f"• {title} — {chat_id}" for title, chat_id in active)
    if unknown:
        lines.extend(["", "گروه‌هایی که به‌دلیل خطای موقت/API بررسی نشدند:"])
        lines.extend(f"• {title} — {chat_id}" for title, chat_id in unknown)
    if removed:
        lines.extend(["", "گروه‌های حذف‌شده:"])
        lines.extend(f"• {title} — {chat_id}" for title, chat_id in removed)
    await _send_text_in_chunks(message, "\n".join(lines))


def invalidate_group_tag_panels(application: Application, chat_id: int) -> None:
    prefix = f"{int(chat_id)}:"
    panels = _tag_panels(application)
    for key in list(panels):
        if key.startswith(prefix):
            panels.pop(key, None)


async def handle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return
    match = COMMAND_RE.fullmatch(message.text or "")
    if not match:
        return
    mention = re.search(r"@(\w+)\s*$", message.text or "")
    if mention:
        try:
            bot_username = (await context.bot.get_me()).username or ""
        except TelegramError as exc:
            logger.warning("Could not validate command bot mention: %s", exc)
            return
        if mention.group(1).casefold() != bot_username.casefold():
            return
    command = match.group(1).casefold()

    if command == "panel":
        if user.id != OWNER_ID:
            await message.reply_text(
                "⛔ فقط مالک ربات اجازه دسترسی به پنل مدیریت را دارد."
            )
            return
        if chat.type == "private":
            await live_check_groups(message, context)
        elif _is_group_chat(chat):
            await send_access_panel(message, context, chat.id)
        else:
            await message.reply_text(
                "/panel فقط در پیوی مالک یا داخل گروه قابل استفاده است."
            )
        return

    if command == "tg":
        if not _is_group_chat(chat):
            await message.reply_text("/tg فقط داخل گروه قابل استفاده است.")
            return
        if not await is_authorized_tagger(user.id, chat, context.bot):
            await message.reply_text(
                "⛔ شما اجازه استفاده از منوی تگ این گروه را ندارید."
            )
            return
        DB.ensure_group(chat.id, clean_title(chat.title, str(chat.id)))
        await send_tag_menu(message, context, user.id)
        return

    if command == "reset":
        if user.id != OWNER_ID:
            await message.reply_text(
                "⛔ فقط مالک ربات می‌تواند تنظیمات گروه را بازنشانی کند."
            )
            return
        if not _is_group_chat(chat):
            await message.reply_text("/reset فقط در گروه قابل استفاده است.")
            return
        preserved_count = DB.reset_group_settings(
            chat.id, clean_title(chat.title, str(chat.id))
        )
        invalidate_group_tag_panels(context.application, chat.id)
        await message.reply_text(
            "✅ تنظیمات دسترسی این گروه بازنشانی شد؛ همه مدیران مجاز حذف شدند.\n"
            f"حافظه اعضا حفظ شد: {preserved_count} کاربر."
        )


async def track_group_activity(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None or not _is_group_chat(chat):
        return

    if getattr(message, "migrate_to_chat_id", None):
        DB.migrate_group(
            chat.id, message.migrate_to_chat_id, clean_title(chat.title, str(chat.id))
        )
    elif getattr(message, "migrate_from_chat_id", None):
        DB.migrate_group(
            message.migrate_from_chat_id, chat.id, clean_title(chat.title, str(chat.id))
        )
    else:
        DB.ensure_group(chat.id, clean_title(chat.title, str(chat.id)))

    sender = message.from_user
    if sender is not None and not sender.is_bot:
        DB.upsert_user(chat.id, sender)
    for new_user in message.new_chat_members or []:
        DB.upsert_user(chat.id, new_user)
    if message.left_chat_member is not None:
        DB.remove_user(chat.id, message.left_chat_member.id)


async def track_user_membership(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    change = update.chat_member
    if change is None or not _is_group_chat(change.chat):
        return
    DB.ensure_group(change.chat.id, clean_title(change.chat.title, str(change.chat.id)))
    member = change.new_chat_member
    user = member.user
    if user.is_bot:
        return
    status = getattr(member, "status", "")
    still_member = (
        status in ADMIN_STATUSES
        or status == "member"
        or (status == "restricted" and bool(getattr(member, "is_member", False)))
    )
    if still_member:
        DB.upsert_user(change.chat.id, user)
    elif status in {"left", "kicked"} or status == "restricted":
        DB.remove_user(change.chat.id, user.id)


async def track_bot_membership(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    change = update.my_chat_member
    if change is None or not _is_group_chat(change.chat):
        return
    status = getattr(change.new_chat_member, "status", "")
    if status not in {"left", "kicked"}:
        DB.ensure_group(
            change.chat.id, clean_title(change.chat.title, str(change.chat.id))
        )
    # Keep removed groups until the owner's private /panel live check can report
    # and prune them, as requested.


async def post_init(application: Application) -> None:
    DB.save()
    try:
        await application.bot.set_my_commands(
            [
                BotCommand("tg", "بازکردن منوی تگ در گروه"),
                BotCommand("panel", "پنل مالک / استعلام گروه‌ها"),
                BotCommand("reset", "بازنشانی تنظیمات گروه (مالک)"),
            ]
        )
    except TelegramError as exc:
        logger.warning("Could not publish bot command menu: %s", exc)


def build_application(*, run_post_init: bool = True) -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing.")
    builder = ApplicationBuilder().token(BOT_TOKEN)
    if run_post_init:
        builder.post_init(post_init)
    application = builder.build()
    register_handlers(application)
    return application


def register_handlers(application: Application) -> None:
    # The tracker runs before the text command handler, so a command sender is
    # also added/updated in target_users. Handlers in different groups both run.
    application.add_handler(
        MessageHandler(filters.ChatType.GROUPS, track_group_activity),
        group=0,
    )
    application.add_handler(
        ChatMemberHandler(track_user_membership, ChatMemberHandler.CHAT_MEMBER),
        group=0,
    )
    application.add_handler(
        ChatMemberHandler(track_bot_membership, ChatMemberHandler.MY_CHAT_MEMBER),
        group=0,
    )
    application.add_handler(CallbackQueryHandler(callback_router), group=0)
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(COMMAND_RE), handle_command),
        group=1,
    )


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN is missing. Copy .env.example to .env and add a freshly generated token."
        )
    if OWNER_ID <= 0:
        raise SystemExit("OWNER_ID must be a positive Telegram user ID.")

    application = build_application()
    logger.info("Starting %s for owner %s", APP_NAME, OWNER_ID)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
