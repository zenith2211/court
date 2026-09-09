"""Post messages to a Telegram group on a fixed interval.

Commands:
    python bot.py loop      run forever, sending every INTERVAL (default)
    python bot.py once      send a single message and exit (for cron)
    python bot.py check     verify the token and chat id are usable
    python bot.py chat-id   list chats the bot has seen, to find your group id

Configuration comes from environment variables (or a .env file next to this
script). See .env.example for the full list.
"""

from __future__ import annotations

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

import requests

ROOT = Path(__file__).resolve().parent
API_BASE = "https://api.telegram.org"

log = logging.getLogger("telegram-scheduler")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Load KEY=VALUE lines from a .env file without overriding real env vars."""
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
    """Turn '30', '30s', '15m', '2h' or '1d' into a number of seconds."""
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
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.thread_id = os.environ.get("TELEGRAM_THREAD_ID", "").strip()
        self.interval = parse_interval(os.environ.get("INTERVAL", "1h"))
        self.messages_file = ROOT / os.environ.get("MESSAGES_FILE", "messages.txt")
        self.single_message = os.environ.get("MESSAGE", "")
        self.rotation = os.environ.get("ROTATION", "sequential").strip().lower()
        self.parse_mode = os.environ.get("PARSE_MODE", "HTML").strip()
        self.silent = env_flag("SILENT", False)
        self.send_on_start = env_flag("SEND_ON_START", True)
        self.state_file = ROOT / os.environ.get("STATE_FILE", ".state.json")
        self.port = os.environ.get("PORT", "").strip()

    def require_token(self) -> str:
        if not self.token:
            raise SystemExit(
                "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather, then put "
                "the token in your .env file or in your host's environment variables."
            )
        return self.token

    def require_chat_id(self) -> str:
        if not self.chat_id:
            raise SystemExit(
                "TELEGRAM_CHAT_ID is not set. Run `python bot.py chat-id` to find it."
            )
        return self.chat_id


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #

def load_messages(cfg: Config) -> list[str]:
    """Read the message pool. Entries in the file are separated by a `---` line."""
    if cfg.single_message:
        return [cfg.single_message.replace("\\n", "\n")]

    if not cfg.messages_file.exists():
        raise SystemExit(
            f"No messages found: set MESSAGE, or create {cfg.messages_file.name} "
            "with one or more messages separated by a line containing ---"
        )

    # Split on lines that are exactly `---`, dropping comment lines as we go. A
    # `#` only starts a comment in column 0, so an indented line keeps its
    # leading hashtag - that is the escape hatch for messages full of hashtags.
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
    except OSError as exc:  # a read-only disk should not stop us sending
        log.warning("Could not save state to %s: %s", cfg.state_file.name, exc)


def pick_message(cfg: Config, messages: list[str]) -> str:
    """Choose the next message, advancing the saved position when sequential."""
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
# Telegram API
# --------------------------------------------------------------------------- #

def call_api(cfg: Config, method: str, payload: dict | None = None, attempts: int = 4) -> dict:
    """Call a Bot API method, retrying on rate limits and transient failures."""
    url = f"{API_BASE}/bot{cfg.require_token()}/{method}"

    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(url, json=payload or {}, timeout=30)
        except requests.RequestException as exc:
            if attempt == attempts:
                raise RuntimeError(f"network error calling {method}: {exc}") from exc
            delay = 2 ** attempt
            log.warning("Network error on %s (%s). Retrying in %ss.", method, exc, delay)
            time.sleep(delay)
            continue

        try:
            body = response.json()
        except ValueError:
            body = {}

        if body.get("ok"):
            return body["result"]

        description = body.get("description", response.text[:200])

        if response.status_code == 429:
            delay = int(body.get("parameters", {}).get("retry_after", 30))
            log.warning("Rate limited by Telegram. Waiting %ss.", delay)
            time.sleep(delay)
            continue

        if response.status_code >= 500 and attempt < attempts:
            delay = 2 ** attempt
            log.warning("Telegram returned %s. Retrying in %ss.", response.status_code, delay)
            time.sleep(delay)
            continue

        raise RuntimeError(f"{method} failed ({response.status_code}): {description}")

    raise RuntimeError(f"{method} failed after {attempts} attempts")


def send_message(cfg: Config, text: str) -> dict:
    payload: dict = {
        "chat_id": cfg.require_chat_id(),
        "text": text,
        "disable_notification": cfg.silent,
    }
    if cfg.parse_mode and cfg.parse_mode.lower() != "none":
        payload["parse_mode"] = cfg.parse_mode
    if cfg.thread_id:
        payload["message_thread_id"] = int(cfg.thread_id)
    return call_api(cfg, "sendMessage", payload)


# --------------------------------------------------------------------------- #
# Health endpoint (only used when the host provides a PORT)
# --------------------------------------------------------------------------- #

def start_health_server(port: int, status: dict) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - name required by BaseHTTPRequestHandler
            body = json.dumps(status).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass  # keep health checks out of the logs

    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health endpoint listening on port %s", port)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_once(cfg: Config) -> int:
    messages = load_messages(cfg)
    text = pick_message(cfg, messages)
    result = send_message(cfg, text)
    log.info("Sent message %s to chat %s", result.get("message_id"), cfg.chat_id)
    return 0


def cmd_loop(cfg: Config) -> int:
    messages = load_messages(cfg)
    log.info(
        "Starting: %d message(s), every %ss, rotation=%s, chat=%s",
        len(messages), cfg.interval, cfg.rotation, cfg.require_chat_id(),
    )

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    status = {"state": "starting", "sent": 0, "interval_seconds": cfg.interval}
    if cfg.port:
        start_health_server(int(cfg.port), status)

    next_run = time.monotonic()
    if not cfg.send_on_start:
        next_run += cfg.interval
        log.info("SEND_ON_START is off; first message in %ss", cfg.interval)

    status["state"] = "running"

    while not stop.is_set():
        wait_for = next_run - time.monotonic()
        if wait_for > 0:
            if stop.wait(wait_for):
                break

        try:
            text = pick_message(cfg, messages)
            result = send_message(cfg, text)
            status["sent"] += 1
            status["last_sent_at"] = time.strftime("%Y-%m-%d %H:%M:%S%z")
            status.pop("last_error", None)
            log.info("Sent message %s (%d total)", result.get("message_id"), status["sent"])
        except RuntimeError as exc:
            status["last_error"] = str(exc)
            log.error("Send failed: %s", exc)

        # Anchor on the schedule rather than on when the send finished, and skip
        # any slots that a long outage caused us to miss.
        now = time.monotonic()
        while next_run <= now:
            next_run += cfg.interval

    log.info("Stopped after sending %d message(s).", status["sent"])
    return 0


def cmd_check(cfg: Config) -> int:
    me = call_api(cfg, "getMe")
    log.info("Token OK: @%s (%s)", me.get("username"), me.get("first_name"))

    if not cfg.chat_id:
        log.warning("TELEGRAM_CHAT_ID is not set. Run `python bot.py chat-id` next.")
        return 1

    chat = call_api(cfg, "getChat", {"chat_id": cfg.chat_id})
    log.info(
        "Chat OK: %s (%s, id %s)",
        chat.get("title") or chat.get("username"), chat.get("type"), chat.get("id"),
    )

    messages = load_messages(cfg)
    log.info("Loaded %d message(s); sending every %ss.", len(messages), cfg.interval)
    log.info("Next message would be:\n%s", messages[read_state(cfg).get("index", 0) % len(messages)])
    return 0


def cmd_chat_id(cfg: Config) -> int:
    """Show chats from recent updates so you can copy the group's id."""
    updates = call_api(cfg, "getUpdates", {"limit": 100})
    seen: dict[int, str] = {}
    for update in updates:
        for key in ("message", "channel_post", "my_chat_member", "edited_message"):
            chat = (update.get(key) or {}).get("chat")
            if chat:
                label = chat.get("title") or chat.get("username") or chat.get("first_name", "")
                seen[chat["id"]] = f"{label} ({chat.get('type')})"

    if not seen:
        log.info(
            "No chats seen yet. Add the bot to your group, send any message there, "
            "then run this again. If the bot has privacy mode on, make it an admin "
            "or send it a message that starts with /."
        )
        return 1

    log.info("Chats the bot can see:")
    for chat_id, label in seen.items():
        log.info("  %-16s %s", chat_id, label)
    log.info("Copy the id of your group into TELEGRAM_CHAT_ID (groups look like -100...).")
    return 0


COMMANDS = {"loop": cmd_loop, "once": cmd_once, "check": cmd_check, "chat-id": cmd_chat_id}


def main() -> int:
    # Windows consoles default to a legacy code page, which raises on emoji.
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

    try:
        return command(Config())
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
