# Delta Chat ↔ Telegram Bridge Bot

A bot that acts as a bridge between Delta Chat groups and Telegram groups. It relays messages sent in mapped Telegram groups to corresponding Delta Chat groups, and vice-versa.

Built using `deltabot-cli-py` and `python-telegram-bot` (`asyncio`).

## Prerequisites

- **Python 3.9+** (for local/venv setup)
- **Telegram Bot token** (create a new bot and obtain one from [@BotFather](https://t.me/BotFather))
- **Delta Chat account** (can be automatically created during setup by providing a valid email address, e.g. from one of [public chatmail relays](https://chatmail.at/relays), and a random password)

## Installation

1. **Clone or navigate** to this directory:

   ```bash
   cd deltachat_telegram_bridge
   ```

2. **Create a virtual environment and install dependencies**:

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

## Setup and Running (Local / venv)

1. **Initialize the Delta Chat account**
   Configure your bot's Delta Chat account using `init dc`:

   ```bash
   venv/bin/python bot.py init dc YOUR_DELTA_CHAT_EMAIL YOUR_EMAIL_PASSWORD
   ```

   *This saves the account configuration locally, by default in your OS config directory under `tgbridge` (e.g. on linux it is located at `~/.config/tgbridge`, on macOS it is located at `~/Library/Application\ Support/tgbridge` etc).*

2. **Initialize the Telegram Token**
   Configure your bot's Telegram API token. You can pass it as an argument or enter it interactively to keep it out of your command history:

   ```bash
   venv/bin/python bot.py init tg
   ```

   *This saves the token locally in the `bridge.db` database.*

3. **Start the bridge bot**
   Once initialized, run the `serve` command to start the relay:

   ```bash
   venv/bin/python bot.py serve
   ```

## Setup with Docker Compose (Recommended)

Instead of using a local virtual environment, you can run the bot using Docker Compose:

1. Initialise the database and accounts using `docker-compose run`:

   ```bash
   docker-compose run --rm bridge python bot.py init dc me@example.com MyPassword
   docker-compose run --rm bridge python bot.py init tg
   ```

2. Start the bridge:

   ```bash
   docker-compose up -d
   ```

3. **To update the bot after pulling new code**, simply run:

   ```bash
   docker-compose up -d --build
   ```

   Also check `update.sh` for an example of how to automatically update the bot.

   *Your configuration and message history will be preserved since they are stored in the mounted `bridge.db` and configuration volumes.*

## Usage: Bridging Groups

The bot needs to be added to both the Telegram group and the Delta Chat group.

> **Note:** By default, management commands (`/bridge`, `/unbridge`, `/id`) are restricted to **group admins** (or the group creator in Delta Chat). However, if you configure a global admin using `init admin_dc` or `init admin_tg`, **only** the configured bot owner will be able to manage the bot on that platform. The bot's help message will indicate its current state as **Mode: Private (bot owner only)** or **Mode: Public (group admins only)**.
>
> **Important:** You must disable **Group Privacy** for your Telegram bot via @BotFather → Bot Settings → Group Privacy → Turn off. Otherwise the bot cannot read normal group messages. After changing this, re-add the bot to the group.

1. **Get the Telegram Group ID**:
   - Add your Telegram bot to the target Telegram group.
   - Send `/id` in the group (admin only). The bot will reply with the chat ID (e.g. `-100123456789`).
2. **Bridge the Delta Chat Group**:
   - Add your Delta Chat bot to the target Delta Chat group.
   - Send `/bridge -100123456789` (admin only, replace with the Telegram group ID you got in step 1).
3. **Unbridge**:
   - To remove a bridge, send `/unbridge` in the Delta Chat group (admin only).

*Note: Group mappings are saved locally in the `bridge.db` SQLite file.*

## Security Notes

- The `bridge.db` file contains your **Telegram bot token** in plaintext. Protect it with appropriate file permissions (e.g. `chmod 600 bridge.db`).
- Management commands (`/bridge`, `/unbridge`, `/id`) are restricted to group admins or bot owner (see below).
- Messages are rate-limited to **30 messages per minute per chat** to prevent flooding.
- Sender names are HTML-escaped before being sent to Telegram to prevent injection.
- Bot messages from both sides are filtered out to prevent echo loops.

## Logging and Admin Control

- **Set Global Admin (Telegram)**: Configures a Telegram user ID to receive all bot error logs via direct message. (You can find your Telegram ID by sending `/start` to the bot in a private message).
  **Important Note:** Setting `admin_tg` locks the Telegram management commands so that *only* this user can manage the bot (the help text will dynamically change to **Mode: Private (bot owner only)**).
  
  ```bash
  docker-compose exec bridge python bot.py init admin_tg YOUR_TELEGRAM_ID
  ```

- **Set Global Admin (Delta Chat)**: Configures a Delta Chat email to receive all bot error logs via direct message.
  **Important Note:** Setting `admin_dc` locks the `/bridge` and `/unbridge` commands so that *only* this user can use them (the help text will dynamically change to **Mode: Private (bot owner only)**).
  
  ```bash
  docker-compose exec bridge python bot.py init admin_dc admin@example.com
  ```

## Other Commands

Since the bot depends on `deltabot-cli-py`, you have access to a variety of other management commands. These commands do **not** require the `--telegram-token` argument.

If you are using **Docker**, run these commands using `docker-compose exec bridge python bot.py ...` (if the bot is running) or `docker-compose run --rm bridge python bot.py ...` (if stopped).
If you are running **locally**, use `venv/bin/python bot.py ...`.

Here are the commands (shown for Docker, assuming the container is running):

- **Get Invite Link (QR Code data)**: Print the bot's invitation link so you can add it to Delta Chat groups.
  
  ```bash
  docker-compose exec bridge python bot.py link
  ```

- **List accounts**: View the IDs and addresses of configured Delta Chat accounts.
  
  ```bash
  docker-compose exec bridge python bot.py list
  ```

- **Config**: View or set configuration options for the bot account.
  
  ```bash
  docker-compose exec bridge python bot.py config
  ```

- **Remove account**: Remove a specific Delta Chat account if you accidentally created multiple. Replace `ID` with the account number (e.g. `2`).
  
  ```bash
  docker-compose exec bridge python bot.py --account ID remove
  ```

- **Admin**: Generates a setup QR code to join an Admin control group where you can manage the bot remotely.
  
  ```bash
  docker-compose exec bridge python bot.py admin
  ```

*For more details on management commands, see the [deltabot-cli-py repository](https://github.com/deltachat-bot/deltabot-cli-py).*

## Changelog

- **2026-03-18**: Added retry logic with exponential backoff for Telegram API timeouts during media relaying. Improved network error logging.
- **2026-03-18**: Implemented bidirectional message reaction proxying. Standard emojis are now synced between DeltaChat and Telegram.
- **2026-03-18**: Native quoting support added. Replaced text-based citations ("↩") with native app replies using `quoted_message_id` and corresponding Telegram reply IDs.
- **2026-03-18**: Enhanced bot greeting and `/help` command with dynamic **Mode: Private (bot owner only)** / **Mode: Public (group admins only)** formatting based on configuration.
- **2026-03-18**: Fixed `/bridge` command behavior in private chats to consistently display error messages like `/id`.
- **2026-03-17**: Added support for bridging Telegram polls. Formats polls and sends final vote results to Delta Chat upon poll closing.
- **2026-03-17**: Implemented full two-way media bridging support for images, videos, voice notes, gifs, stickers, and documents.
- **2026-03-17**: Refactored database to use SQLite (`bridge.db`), added Docker Compose support, and implemented rate limiting.
