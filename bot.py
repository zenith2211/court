"""Post messages to Telegram groups on a fixed interval using a user account.

Commands:
    python bot.py loop      run forever, sending every INTERVAL (default)
    python bot.py once      send a single message and exit
    python bot.py check     verify the session and list target groups
    python bot.py groups    list all groups/channels you've joined

First run will prompt for your phone number and a login code from Telegram.
After that the session is saved and login is automatic.

Configuration comes from environment variables (or a .env file next to this
script). See .env.example for the full list.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError, ChatWriteForbiddenError, ChannelPrivateError

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("telegram-scheduler")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def parse_interval(raw: str) -> int:
    text = raw.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    multiplier = 1
    if text[-1:] in units:
        multiplier = units[text[-1]]
        text = text[:-1]
    try:
        seconds = int(float(text) * multiplier)
    except ValueError:
        raise SystemExit(f"INTERVAL is not a valid duration: {raw!r}")
    if seconds < 1:
        raise SystemExit(f"INTERVAL must be at least 1 second, got {raw!r}")
    return seconds


def env_flag(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self) -> None:
        self.api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
        self.api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
        self.phone = os.environ.get("TELEGRAM_PHONE", "").strip()
        self.session_name = os.environ.get("SESSION_NAME", "user_session").strip()
        raw_chat_ids = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.chat_ids = [cid.strip() for cid in raw_chat_ids.split(",") if cid.strip()]
        self.interval = parse_interval(os.environ.get("INTERVAL", "1h"))
        self.messages_file = ROOT / os.environ.get("MESSAGES_FILE", "messages.txt")
        self.single_message = os.environ.get("MESSAGE", "")
        self.rotation = os.environ.get("ROTATION", "sequential").strip().lower()
        self.send_on_start = env_flag("SEND_ON_START", True)
        self.state_file = ROOT / os.environ.get("STATE_FILE", ".state.json")
        self.port = os.environ.get("PORT", "").strip()

    def require_api_credentials(self) -> tuple[int, str]:
        if not self.api_id or not self.api_hash:
            raise SystemExit(
                "TELEGRAM_API_ID and TELEGRAM_API_HASH are not set.\n"
                "Get them from https://my.telegram.org/apps and put them in your .env file."
            )
        return int(self.api_id), self.api_hash

    def require_chat_ids(self) -> list[str]:
        if not self.chat_ids:
            raise SystemExit(
                "TELEGRAM_CHAT_ID is not set. Run `python bot.py groups` to find group ids."
            )
        return self.chat_ids

    async def get_client(self) -> TelegramClient:
        api_id, api_hash = self.require_api_credentials()
        session_path = str(ROOT / self.session_name)

        secret_path = Path("/etc/secrets/user_session.session")
        if secret_path.exists() and not Path(session_path + ".session").exists():
            import shutil
            shutil.copy2(secret_path, session_path + ".session")
            log.info("Restored session from secret file.")

        session_b64 = os.environ.get("TELEGRAM_SESSION", "").strip()
        if session_b64 and not Path(session_path + ".session").exists():
            import zlib
            raw = base64.b64decode(session_b64)
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                pass
            Path(session_path + ".session").write_bytes(raw)
            log.info("Restored session from TELEGRAM_SESSION env var.")

        client = TelegramClient(session_path, api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            phone = self.phone or input("Enter your phone number (with country code, e.g. +91...): ").strip()
            await client.send_code_request(phone)
            code = input("Enter the code Telegram sent you: ").strip()
            try:
                await client.sign_in(phone, code)
            except Exception:
                password = input("Two-factor password required: ").strip()
                await client.sign_in(password=password)
            log.info("Login successful. Session saved.")
        return client


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #

def load_messages(cfg: Config) -> list[str]:
    if cfg.single_message:
        return [cfg.single_message.replace("\\n", "\n")]

    if not cfg.messages_file.exists():
        raise SystemExit(
            f"No messages found: set MESSAGE, or create {cfg.messages_file.name} "
            "with one or more messages separated by a line containing ---"
        )

    messages: list[str] = []
    current: list[str] = []
    for line in cfg.messages_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            continue
        if line.strip() == "---":
            messages.append("\n".join(current).strip())
            current = []
        else:
            current.append(line)
    messages.append("\n".join(current).strip())

    messages = [m for m in messages if m]
    if not messages:
        raise SystemExit(f"{cfg.messages_file.name} has no messages in it.")
    return messages


def read_state(cfg: Config) -> dict:
    try:
        return json.loads(cfg.state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(cfg: Config, state: dict) -> None:
    try:
        cfg.state_file.write_text(json.dumps(state), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not save state to %s: %s", cfg.state_file.name, exc)


def pick_message(cfg: Config, messages: list[str]) -> str:
    if len(messages) == 1:
        return messages[0]
    if cfg.rotation == "random":
        return random.choice(messages)

    state = read_state(cfg)
    index = state.get("index", 0) % len(messages)
    state["index"] = (index + 1) % len(messages)
    write_state(cfg, state)
    return messages[index]


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #

async def ensure_connected(client: TelegramClient) -> bool:
    if not client.is_connected():
        log.warning("Client disconnected, reconnecting...")
        await client.connect()
        if not await client.is_user_authorized():
            log.error("Session expired after reconnect.")
            return False
        log.info("Reconnected successfully.")
    return True


async def send_to_all(client: TelegramClient, cfg: Config, text: str) -> int:
    succeeded = 0
    for chat_id in cfg.require_chat_ids():
        try:
            entity = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
            await client.send_message(entity, text, parse_mode="html")
            log.info("Sent to %s", chat_id)
            succeeded += 1
        except FloodWaitError as e:
            log.warning("Flood wait %ss for %s, sleeping...", e.seconds, chat_id)
            await asyncio.sleep(e.seconds)
            try:
                await client.send_message(entity, text, parse_mode="html")
                log.info("Sent to %s (after wait)", chat_id)
                succeeded += 1
            except Exception as exc:
                log.error("Failed to send to %s after flood wait: %s", chat_id, exc)
        except ConnectionError:
            log.warning("Disconnected while sending to %s, reconnecting...", chat_id)
            if await ensure_connected(client):
                try:
                    await client.send_message(entity, text, parse_mode="html")
                    log.info("Sent to %s (after reconnect)", chat_id)
                    succeeded += 1
                except Exception as exc:
                    log.error("Failed to send to %s after reconnect: %s", chat_id, exc)
        except (ChatWriteForbiddenError, ChannelPrivateError) as exc:
            log.error("Cannot send to %s: %s", chat_id, exc)
        except Exception as exc:
            log.error("Failed to send to %s: %s", chat_id, exc)
    return succeeded


# --------------------------------------------------------------------------- #
# Health endpoint
# --------------------------------------------------------------------------- #

def start_self_ping(url: str, interval: int = 300) -> None:
    """Ping our own public URL to prevent Render free tier from sleeping."""
    import urllib.request

    def _ping() -> None:
        while True:
            time.sleep(interval)
            try:
                urllib.request.urlopen(url, timeout=30)
            except Exception:
                pass

    threading.Thread(target=_ping, daemon=True).start()
    log.info("Self-ping every %ds to %s", interval, url)


def start_health_server(port: int, status: dict) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps(status).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health endpoint listening on port %s", port)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

async def cmd_once(cfg: Config) -> int:
    messages = load_messages(cfg)
    text = pick_message(cfg, messages)
    client = await cfg.get_client()
    try:
        sent = await send_to_all(client, cfg, text)
        log.info("Sent to %d/%d group(s)", sent, len(cfg.chat_ids))
    finally:
        await client.disconnect()
    return 0


async def cmd_loop(cfg: Config) -> int:
    messages = load_messages(cfg)
    chat_ids = cfg.require_chat_ids()
    log.info(
        "Starting: %d message(s), every %ss, rotation=%s, %d group(s)",
        len(messages), cfg.interval, cfg.rotation, len(chat_ids),
    )

    status = {"state": "starting", "sent": 0, "interval_seconds": cfg.interval}
    if cfg.port:
        start_health_server(int(cfg.port), status)
        service_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
        if service_url:
            start_self_ping(service_url)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stop.set())

    next_run = time.monotonic()
    if not cfg.send_on_start:
        next_run += cfg.interval
        log.info("SEND_ON_START is off; first message in %ss", cfg.interval)

    status["state"] = "running"

    while not stop.is_set():
        wait_for = next_run - time.monotonic()
        if wait_for > 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait_for)
                break
            except asyncio.TimeoutError:
                pass

        try:
            client = await cfg.get_client()
            try:
                text = pick_message(cfg, messages)
                sent = await send_to_all(client, cfg, text)
                status["sent"] += sent
                status["last_sent_at"] = time.strftime("%Y-%m-%d %H:%M:%S%z")
                log.info("Sent to %d/%d group(s) (%d total)", sent, len(chat_ids), status["sent"])
            finally:
                await client.disconnect()
        except Exception as exc:
            log.error("Send cycle failed: %s", exc)
            status["last_error"] = str(exc)

        now = time.monotonic()
        while next_run <= now:
            next_run += cfg.interval

    log.info("Stopped after sending %d message(s).", status["sent"])
    return 0


async def cmd_check(cfg: Config) -> int:
    client = await cfg.get_client()
    try:
        me = await client.get_me()
        log.info("Logged in as: %s %s (id %s)", me.first_name, me.last_name or "", me.id)

        if not cfg.chat_ids:
            log.warning("TELEGRAM_CHAT_ID is not set. Run `python bot.py groups` to find ids.")
            return 1

        for chat_id in cfg.chat_ids:
            try:
                entity = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
                chat = await client.get_entity(entity)
                title = getattr(chat, "title", None) or getattr(chat, "username", chat_id)
                log.info("Group OK: %s (id %s)", title, chat_id)
            except Exception as exc:
                log.error("Group %s failed: %s", chat_id, exc)

        messages = load_messages(cfg)
        log.info("Loaded %d message(s); sending every %ss.", len(messages), cfg.interval)
    finally:
        await client.disconnect()
    return 0


async def cmd_groups(cfg: Config) -> int:
    client = await cfg.get_client()
    try:
        log.info("Groups and channels you've joined:")
        async for dialog in client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                log.info("  %-16s %s", dialog.entity.id, dialog.title)
        log.info("Copy the id(s) into TELEGRAM_CHAT_ID (comma-separated for multiple).")
    finally:
        await client.disconnect()
    return 0


COMMANDS = {
    "loop": cmd_loop,
    "once": cmd_once,
    "check": cmd_check,
    "groups": cmd_groups,
}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    load_dotenv()

    name = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MODE", "loop")).strip()
    command = COMMANDS.get(name)
    if command is None:
        print(__doc__)
        print(f"Unknown command: {name!r}")
        return 2

    cfg = Config()
    try:
        return asyncio.run(command(cfg))
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
