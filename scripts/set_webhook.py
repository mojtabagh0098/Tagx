"""Configure or remove the Telegram webhook for a deployed Vercel project."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import Bot, BotCommand, Update

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "base_url",
        nargs="?",
        help="HTTPS Vercel deployment URL, e.g. https://your-project.vercel.app",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Remove the current webhook (use this before returning to polling).",
    )
    return parser.parse_args()


async def configure(args: argparse.Namespace) -> None:
    token = os.getenv("BOT_TOKEN", "").strip()
    secret = os.getenv("WEBHOOK_SECRET", "").strip()
    if not token:
        raise SystemExit("Set BOT_TOKEN in the local .env file first.")
    if not args.delete:
        if not args.base_url:
            raise SystemExit("Provide the Vercel HTTPS base URL, or use --delete.")
        parsed = urlparse(args.base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise SystemExit("The Vercel base URL must start with https://.")
        if not secret:
            raise SystemExit(
                "Set WEBHOOK_SECRET in .env and Vercel Environment Variables."
            )

    bot = Bot(token)
    await bot.initialize()
    try:
        if args.delete:
            await bot.delete_webhook(drop_pending_updates=False)
            print("Telegram webhook deleted.")
            return

        webhook_url = args.base_url.rstrip("/") + "/api/webhook"
        await bot.set_webhook(
            url=webhook_url,
            secret_token=secret,
            max_connections=1,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )
        await bot.set_my_commands(
            [
                BotCommand("tg", "بازکردن منوی تگ در گروه"),
                BotCommand("panel", "پنل مالک / استعلام گروه‌ها"),
                BotCommand("reset", "بازنشانی تنظیمات گروه (مالک)"),
            ]
        )
        print(f"Telegram webhook configured: {webhook_url}")
    finally:
        await bot.shutdown()


def main() -> None:
    try:
        asyncio.run(configure(parse_args()))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
