# Delta Chat ↔ Telegram Bridge Bot

A bot that acts as a bridge between Delta Chat groups and Telegram groups. It relays messages sent in mapped Telegram groups to corresponding Delta Chat groups, and vice-versa.

Built using `deltabot-cli-py` and `python-telegram-bot` (`asyncio`).

## Key Features

- **Bidirectional Group Bridging**: Sync messages between Telegram groups and Delta Chat groups.
- **Bidirectional Deletion Sync**: Sync message deletions between both platforms with built-in safety guards.
- **Improved Edit Handling**: Edits in Telegram automatically replace (delete and resend) the old version in Delta Chat to prevent clutter.
- **Public Telegram Channels**: Bridge any public channel to a Delta Chat broadcast group.
- **Historical Context**: Automatically pre-fills newly bridged channels with the last 3 historical posts.
- **Userbot Mode**: Bridge channels without needing administrator permissions.
- **Watchdog Protection**: Automatic detection and recovery from Userbot connection errors.
- **Login Code Forwarding**: Automatic delivery of Telegram login codes to the admin.
- **Rate Limiting & Safety**: Global outgoing limits and bulk-deletion protection with admin notifications.
- **Automatic Updates**: Self-updating via a simple script and cron job.
- **Admin-Only Management**: Securely bridge channels and groups with restricted access.

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

3. **To update the bot manually** after pulling new code:

   ```bash
   docker-compose up -d --build
   ```

   *Your configuration and message history will be preserved since they are stored in the mounted `bridge.db` and configuration volumes.*

## Automatic Updates

The repository includes an `update.sh` script that automates the update process. It performs the following steps:

1. Runs `git fetch` to check for new commits on the remote repository.
2. Compares the local version with the remote version.
3. If new changes are found, it runs `git pull`, rebuilds the Docker container (`--build`), and cleans up old images.

### Setting up a Cron Job

To have the bot automatically check for updates every 15 minutes, you can set up a cron job:

1. Open your crontab:

   ```bash
   crontab -e
   ```

2. Add the following line (replace `/path/to/` with the actual absolute path to the project directory):

   ```cron
   */15 * * * * /path/to/deltachat_telegram_bridge/update.sh >> /path/to/deltachat_telegram_bridge/update.log 2>&1
   ```

> [!TIP]
> You can find the absolute path by running `realpath update.sh` inside the directory.

1. **Maintenance and Cleanup**:

   To stop the bot and remove containers:

   ```bash
   docker compose down
   ```

   To remove old, unused images (reclaim disk space):

   ```bash
   docker image prune -f
   ```

   To view logs in real-time:

   ```bash
   docker compose logs -f
   ```

## Usage: Bridging Groups

The bot needs to be added to the Telegram group. When you bridge from Telegram, the bot automatically creates the corresponding Delta Chat group.

> **Note:** By default, management commands (`/bridge`, `/unbridge`, `/id`) are restricted to **group admins**. However, if you configure a global admin using `init admin_dc` or `init admin_tg`, **only** the configured bot owner (and designated sub-admins) will be able to manage the bot. The bot's help message will indicate its current state as **Mode: Private (bot owner only)** or **Mode: Public (group admins only)**.
>
> **Important:** You must disable **Group Privacy** for your Telegram bot via @BotFather → Bot Settings → Group Privacy → Turn off. Otherwise the bot cannot read normal group messages. After changing this, re-add the bot to the group.

### From Telegram (recommended)

1. **Add your Telegram bot** to the target Telegram group.
2. **Send `/bridge`** in the group (admin only). The bot will automatically create a Delta Chat group with the same name and avatar, link them, and reply with an invite link to join the DC group.
3. **To remove the bridge**, send `/unbridge` in the Telegram group.

### From Delta Chat (advanced)

1. Get the Telegram Group ID by sending `/id` in the TG group.
2. Add your Delta Chat bot to the target Delta Chat group.
3. Send `/bridge -100123456789` in the DC group (owner only in private mode).
4. To remove the bridge, send `/unbridge` in the DC group.

*Note: Group mappings are saved locally in the `bridge.db` SQLite file.*

> **Reactions:** For reaction bridging (TG → DC) to work, the bot must be an **administrator** in the Telegram group. No special permissions are needed — just the admin role. When a basic group is promoted, Telegram may migrate it to a supergroup (changing the chat ID). The bot detects this automatically and updates its stored mappings.

## Security Notes

- The `bridge.db` file contains your **Telegram bot token** in plaintext. Protect it with appropriate file permissions (e.g. `chmod 600 bridge.db`).
- Management commands (`/bridge`, `/unbridge`, `/id`) are restricted to group admins or bot owner (see below).
- Messages are rate-limited to **30 messages per minute per chat** to prevent flooding.
- **Global outgoing limit**: The bot enforces a global limit of **60 messages per minute** across all Delta Chat interactions to stay within chatmail server limits.
- **Deletion Safety Guard**: To prevent accidental data loss, the bot limits automatic deletion sync to **5 messages per 60 seconds**. Bulk deletions are blocked and reported to the admin. Note: technical deletions (like replacing an old message during an edit) are exempt from this limit.
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

## Sub-Admins (Private Mode)

In private mode (`admin_tg` is set), the bot owner can designate **sub-admins** who can independently manage their own bridges and channels.

Sub-admins can:

- Use `/bridge` and `/unbridge` in Telegram groups
- Use `/channeladd` and `/channelremove` for channels
- View their own bridges via `/stats` and `/channels`

Sub-admins **cannot** see or manage resources created by the owner or other sub-admins.

| Command | Description |
|---------|-------------|
| `/adminadd <user_id>` | Add a sub-admin (owner only, private chat) |
| `/adminremove <user_id>` | Remove a sub-admin (owner only, private chat) |
| `/admins` | List all sub-admins (owner only, private chat) |

## Channel Bridging (TG Channel → DC Broadcast)

The bot can bridge **Telegram channels** and **groups** to **Delta Chat broadcast channels** (one-way, read-only).

> **Note:** By default, the bot requires being an **administrator** in a Telegram channel to receive posts. If you cannot make the bot an admin, or you want to bridge a regular group in "stealth" read-only mode, you can enable **Userbot Mode**.

### Setup

1. **Add the bot** as an admin to the Telegram channel you want to bridge.
2. **In a private chat** with the bot, use `/channeladd @channel_username` or `/channeladd -1001234567890` (numeric ID) to create the bridge.

   - *Tip:* When you add the bot as an administrator to a channel, it will automatically send a private message to the configured `admin_tg` with the channel's numeric ID and a ready-to-use bridging command.

3. The bot will create a DC broadcast channel (with the same name and avatar) and return an **invite link** for subscribing.

### Commands (private chat, admin/sub-admin)

| Command | Description |
|---------|-------------|
| `/channeladd <target>` | Create a bridge for a Telegram channel or group |
| `/channelremove <target>` | Remove a channel bridge |
| `/channels` | List bridged channels (as admin, private chat) |
| `/channel N` | Get invite link for channel by its internal number |
| `/channelqr N` | Get QR code image for channel by its internal number |
| `/userbotsync` | Force re-sync Userbot subscriptions |
| `/userbotjoin <link>` | Join a channel/group via Userbot using an invite link |
| `/groups` | List technical account's groups for easy bridging |
| `/donate` | Support bot development ❤️ |

## Delta Chat User Commands

Any Delta Chat user (not just admins) can use these commands in a private chat with the bot or in a group:

- `/channels` — List all available **public** Telegram channels (shows TG and DC stats).
- `/channelN` — Get the text invite link for channel #N (e.g., `/channel5`).
- `/channelNqr` — Get the QR code invite for channel #N.
- `/donate` — Support bot development ❤️

#### Management (Admin only)

- `/channeladd @username` — Bridge a new channel (admin email check).
- `/channelremove N` — Remove bridge for channel #N.
- `/userbotjoin <link>` — Join channel via Userbot (invite link support).
- `/channelNqr` — Get the QR code image for channel #N (for easy sharing/onboarding).
- `/stats` — Show bridge statistics for the current chat.
- `/help` — Show Delta Chat bot help.

## Userbot Mode (Bridging without Admin permissions)

If you want to bridge channels where you cannot add the bot as an administrator, you can configure **Userbot Mode**. This allows the bot daemon to act as a regular Telegram client using your personal account.

### 1. Obtain API Credentials

1. Go to [my.telegram.org](https://my.telegram.org) and log in.
2. Go to **API development tools**.
3. Create a new application (e.g., "DC-TG-Bridge").
4. Copy your **App api_id** and **App api_hash**.

### 2. Configure Credentials

Run these commands inside your environment (or via `docker-compose exec bridge python bot.py ...`):

```bash
python bot.py init api_id YOUR_API_ID
python bot.py init api_hash YOUR_API_HASH
```

### 3. Initialize Interactive Login

This step requires entering your phone number and the SMS/Telegram validation code. If using Docker, you must run it interactively:

```bash
docker-compose exec bridge python bot.py init userbot
```

> [!TIP]
> If Telegram sends a login code to your technical account itself, the bot will automatically capture it and forward it to you via Delta Chat and Telegram.

### 4. Security Risks
>
> [!CAUTION]
> Using your personal account for Userbot Mode comes with risks:
>
> - **Session Security:** A file named `userbot_session.session` will be created. This file is essentially a "master key" to your Telegram account. **Never share it.** Ensure your server has strict file permissions.
> - **Account Activity:** The bot will join channels on your behalf to read posts.
> - **API Limits:** While safe for moderate use, intensive API operations can theoretically lead to temporary rate limits or account flags from Telegram's anti-spam systems. It is recommended to use a dedicated "feeder" account if you plan to bridge a very large number of channels.

---

## Double Bridge Protection

If a channel is bridged via both the core bot (as admin) and Userbot Mode, the bridge will automatically deduplicate messages, ensuring Delta Chat users receive only one copy of each post.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for a detailed history of changes.
