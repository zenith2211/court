# Telegram interval poster

Sends a message to a Telegram group every N minutes/hours, cycling through a
list of messages. Runs as a long-lived loop, or as a one-shot for cron.

## 1. Create the bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`,
   follow the prompts.
2. Copy the token it gives you (`123456789:AA...`).
3. Add the bot to your group, then **promote it to admin**. Non-admin bots have
   privacy mode on and cannot see the group's messages, which makes step 3 below
   harder — and some groups block posting by non-admin bots outright.

## 2. Configure

```bash
cp .env.example .env
```

Put your token in `.env` as `TELEGRAM_BOT_TOKEN`, then find your group id:

```bash
pip install -r requirements.txt
python bot.py chat-id
```

Send any message in the group first so it shows up. Copy the group's id (a
supergroup id looks like `-1001234567890`) into `TELEGRAM_CHAT_ID`.

Edit `messages.txt` to say what you want. Messages are separated by a line
containing only `---`, and `#` lines are ignored. For a single fixed message,
set `MESSAGE=` in `.env` instead and the file is skipped.

Verify everything before you let it run:

```bash
python bot.py check
```

## 3. Run

```bash
python bot.py loop
```

Sends immediately, then every `INTERVAL`. `INTERVAL` accepts `90s`, `15m`,
`2h`, `1d`, or a bare number of seconds. Ctrl-C stops it.

Other commands:

| Command | What it does |
| --- | --- |
| `python bot.py loop` | Run forever, sending on the interval (default) |
| `python bot.py once` | Send one message and exit — use this from cron |
| `python bot.py check` | Confirm the token, chat, and message list are valid |
| `python bot.py chat-id` | List chats the bot can see, to find the group id |

## Settings

All of these are environment variables, read from `.env` or the host's config.

| Variable | Default | Notes |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | — | Required. From @BotFather. |
| `TELEGRAM_CHAT_ID` | — | Required. The group id. |
| `TELEGRAM_THREAD_ID` | — | Optional. Post into one topic of a forum group. |
| `INTERVAL` | `1h` | `90s`, `15m`, `2h`, `1d`, or seconds. |
| `MESSAGES_FILE` | `messages.txt` | Message pool, split on `---`. |
| `MESSAGE` | — | A single message; overrides `MESSAGES_FILE`. |
| `ROTATION` | `sequential` | `sequential` cycles in order, `random` shuffles. |
| `PARSE_MODE` | `HTML` | `HTML`, `Markdown`, `MarkdownV2`, or `none`. |
| `SILENT` | `false` | `true` sends without a notification sound. |
| `SEND_ON_START` | `true` | `false` waits one full interval before the first send. |
| `PORT` | — | If set, serves a JSON status page. Render sets this for you. |

Sequential rotation stores its position in `.state.json`. On a host with an
ephemeral disk that file resets on redeploy, so rotation restarts from the top —
use `ROTATION=random` if that bothers you.

## Deploying to Render

`render.yaml` defines a **background worker**, which is the right shape for this:
no inbound traffic, it just runs the loop. Render has no free worker tier
(starter is $7/month).

To stay free instead, use the commented-out web service in `render.yaml`. Render
suspends free web services after 15 minutes without inbound requests, which
stops the loop — so point a free external pinger ([UptimeRobot](https://uptimerobot.com),
[cron-job.org](https://cron-job.org)) at the service URL every 5–10 minutes.
`bot.py` serves a status JSON on `$PORT` for that purpose.

Either way, set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the Render
dashboard under **Environment** — never commit them.

### Free alternative: GitHub Actions

If the interval is 5 minutes or longer, a scheduled workflow costs nothing and
needs no server. Add `.github/workflows/post.yml`:

```yaml
on:
  schedule:
    - cron: "0 * * * *" # hourly
  workflow_dispatch:
jobs:
  post:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements.txt
      - run: python bot.py once
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          ROTATION: random
```

GitHub delays scheduled runs under load, so treat the timing as approximate.

## Notes

- Telegram rate-limits bots to roughly 20 messages/minute per group. The script
  backs off and retries when it hits `429`.
- Frequent identical messages read as spam to members and can get a bot reported.
  A handful of rotating messages on a slow interval works better than one message
  every minute.
