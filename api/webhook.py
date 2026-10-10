"""Vercel's Python Serverless Function entry point for Telegram webhooks."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler
from typing import Any

# Vercel's function filesystem is not durable. This file is only a disposable
# cache; the authoritative JSON document is stored in Postgres JSONB.
os.environ.setdefault("BOT_DATABASE_PATH", "/tmp/bot_database.json")
os.environ.setdefault("CODE87_SERVERLESS", "1")

import psycopg
from psycopg.types.json import Jsonb
from telegram import Update

from bot import BOT_TOKEN, DB, build_application

logger = logging.getLogger("Code 87 v1.vercel")
MAX_REQUEST_BYTES = 1_000_000
EMPTY_DATABASE: dict[str, Any] = {"version": 1, "groups": {}}


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not configured.")
    return url


def _ensure_state_table(cursor: Any) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS code87_bot_state (
            id SMALLINT PRIMARY KEY CHECK (id = 1),
            document JSONB NOT NULL
        )
        """
    )
    cursor.execute(
        """
        INSERT INTO code87_bot_state (id, document)
        VALUES (1, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (Jsonb(EMPTY_DATABASE),),
    )


def _load_remote_database() -> dict[str, Any]:
    with (
        psycopg.connect(_database_url(), connect_timeout=8) as connection,
        connection.cursor() as cursor,
    ):
        _ensure_state_table(cursor)
        cursor.execute("SELECT document FROM code87_bot_state WHERE id = 1")
        row = cursor.fetchone()
    if row is None:
        return dict(EMPTY_DATABASE)
    document = row[0]
    if isinstance(document, str):
        document = json.loads(document)
    if not isinstance(document, dict) or not isinstance(
        document.get("groups", {}), dict
    ):
        raise TypeError("The code87_bot_state row contains invalid JSON data.")
    document.setdefault("version", 1)
    document.setdefault("groups", {})
    return document


def _save_remote_database(document: dict[str, Any]) -> None:
    with (
        psycopg.connect(_database_url(), connect_timeout=8) as connection,
        connection.cursor() as cursor,
    ):
        _ensure_state_table(cursor)
        cursor.execute(
            """
            INSERT INTO code87_bot_state (id, document)
            VALUES (1, %s)
            ON CONFLICT (id) DO UPDATE SET document = EXCLUDED.document
            """,
            (Jsonb(document),),
        )


def _restore_panel_states(raw_states: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_states, dict):
        return {}
    restored: dict[str, dict[str, Any]] = {}
    for key, raw_state in raw_states.items():
        if not isinstance(raw_state, dict):
            continue
        state = dict(raw_state)
        try:
            state["creator_id"] = int(state["creator_id"])
            state["selected"] = {int(user_id) for user_id in state.get("selected", [])}
            state["page"] = int(state.get("page", 0))
            state["last_used"] = int(state.get("last_used", 0))
        except (KeyError, TypeError, ValueError):
            continue
        state["sending"] = bool(state.get("sending", False))
        restored[str(key)] = state
    return restored


def _serialize_panel_states(states: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(states, dict):
        return {}
    serialized: dict[str, dict[str, Any]] = {}
    for key, state in states.items():
        if not isinstance(state, dict):
            continue
        try:
            serialized[str(key)] = {
                "creator_id": int(state["creator_id"]),
                "selected": sorted(
                    {int(user_id) for user_id in state.get("selected", set())}
                ),
                "page": int(state.get("page", 0)),
                "sending": bool(state.get("sending", False)),
                "last_used": int(state.get("last_used", 0)),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return serialized


async def _process_update(payload: dict[str, Any]) -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured.")

    # Telegram's webhook is configured with max_connections=1, so each update
    # reloads and saves the latest JSON document without competing bot workers.
    remote_data = await asyncio.to_thread(_load_remote_database)
    panel_states = _restore_panel_states(remote_data.pop("_tag_panels", {}))
    DB.replace_data(remote_data)
    original_database = DB.export_data()
    original_panels = _serialize_panel_states(panel_states)
    application = build_application(run_post_init=False)
    application.bot_data["tag_panels"] = panel_states
    initialized = False
    try:
        await application.initialize()
        initialized = True
        update = Update.de_json(payload, application.bot)
        if update is None:
            raise ValueError("Telegram sent an invalid update payload.")
        await application.process_update(update)
        snapshot = DB.export_data()
        serialized_panels = _serialize_panel_states(
            application.bot_data.get("tag_panels", {})
        )
        if snapshot != original_database or serialized_panels != original_panels:
            snapshot["_tag_panels"] = serialized_panels
            await asyncio.to_thread(_save_remote_database, snapshot)
    finally:
        if initialized:
            try:
                await application.shutdown()
            except Exception:
                logger.exception("Failed to shut down the Telegram application cleanly")


class handler(BaseHTTPRequestHandler):
    """Vercel Python runtime entry point. Telegram POSTs updates to this URL."""

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._respond(200, {"ok": True, "service": "Code 87 v1 Telegram webhook"})

    def do_POST(self) -> None:
        expected_secret = os.getenv("WEBHOOK_SECRET", "")
        supplied_secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not expected_secret or not hmac.compare_digest(
            supplied_secret, expected_secret
        ):
            self._respond(403, {"ok": False, "error": "Forbidden"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._respond(400, {"ok": False, "error": "Invalid content length"})
            return
        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            self._respond(413, {"ok": False, "error": "Invalid request size"})
            return

        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if not isinstance(payload, dict) or "update_id" not in payload:
                raise ValueError("Missing Telegram update_id")
            asyncio.run(_process_update(payload))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            logger.info("Rejected malformed Telegram webhook payload: %s", exc)
            self._respond(400, {"ok": False, "error": "Invalid update"})
        except Exception:
            logger.exception("Could not process Telegram webhook update")
            self._respond(500, {"ok": False, "error": "Webhook processing failed"})
        else:
            self._respond(200, {"ok": True})

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("Vercel request: " + format, *args)
