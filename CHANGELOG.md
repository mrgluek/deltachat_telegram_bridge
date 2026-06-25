## [2026-06-25]
- **Bidirectional Suffix Matching**:
  - Suffix matching is now bidirectional (e.g. `@tg` or `@tgbridge` will match TG Bridge bot, even with partial entries).
- **Smart Group Chat Command Filtering**:
  - The bot now automatically ignores unaddressed general `/help` and `/stats` commands in group chats if other bots are present in the chat.
- **Target-Specific Command Suffixes**:
  - Added support for addressing this bot specifically in group chats using `/command@tg` or `/command@tgbridge` suffixes.
- **Telegram Bot API Watchdog**:
  - Implemented a liveness checker and watchdog for the Telegram Bot API polling loop.
  - Automatically queries `get_me` every 60 seconds with a 10s timeout to detect hung polling connections, stopping and restarting the updater if unresponsive.
  - Fixed an AttributeError (`'Updater' object has no attribute 'is_active'`) in python-telegram-bot v20 by using the correct `running` property.

## [2026-06-22]
- **Edit Relay Optimizations**:
  - Prevent edit relaying for channels/groups with more than 10,000 subscribers/members to save traffic.
  - Skip edit relaying for messages with attached files larger than 1 MB.
  - Ignore message edits for messages older than 7 days (1 week).
- **Subscriber Count Updates**:
  - Automatically update Telegram subscriber count in the database during `/channelssync` runs.
- **Fix Subscriber Count Calculation**:
  - Dynamically check if the bot's own contact ID is present in the group contacts list before subtracting 1, fixing the reporting of 0 subscribers for broadcast channels (where the bot's own contact is typically excluded from the contacts list).


## [2026-06-18]
- **Bidirectional In-place Message Edits**:
  - Implemented bidirectional in-place message edits between Telegram and Delta Chat.
  - Telegram → Delta Chat edits are processed via `send_edit_request` (with delete-and-resend fallback). Fixed a bug where edits within 120s of creation were ignored due to processed cache duplication.
  - Delta Chat → Telegram edits are processed via the core's `MSGS_CHANGED` event, supporting both text and media captions (with text/caption editing failover).
- **Admin Command `/channelssync`**:
  - Added a dedicated `/channelssync` administrative command in Delta Chat to force-refresh all bridged channel names and avatars from Telegram (supporting fallback to Userbot API).

## [2026-06-16]
- **Robust E2E Failover Loops & Key Fallbacks**:
  - Added fallback support for both `chat_id` and `chatId` keys when extracting details from raw RPC message snapshots.
  - Downgraded permanent E2E failure and resend logs to `WARNING` to prevent them from triggering the admin error email handler.
  - Filtered out loop-prone failover keywords from `AdminLogHandler.emit()` to completely avoid infinite logging/emailing loops when the admin's E2E key is missing.
  - Removed administrative failover alert emails completely, relying entirely on structured logging to prevent any potential loop risks.
- **Telegram Startup Resilience**:
  - Configured custom HTTPX request timeouts (30s) and pool sizes to handle API congestion.
  - Wrapped the startup `tg_app.initialize()` call in a 5-attempt retry loop with backoff to handle transient network hiccups on boot.
- **Automatic Transport Failover:** Implemented a robust, event-driven transport failover mechanism. The bot now listens to the core's `MSG_FAILED` event. When a message fails to deliver, it automatically switches `configured_addr` to the next configured backup transport, and schedules a resend of the message using exponential backoff (5s, 10s, 20s, 40s...) via an asynchronous timer thread. The failover process is limited to a maximum of 10 attempts per message to prevent infinite loops, and the administrator is alerted only on the first failure.


## [2026-06-09]
- **Animation Relay Fix**: Corrected forwarding of Telegram animation/GIF files. Telegram encodes animations as silent MP4 video files under the hood. By changing the temporary file suffix from `.gif` to `.mp4`, the files are correctly identified as `video/mp4` in Delta Chat, allowing clients to play them rather than displaying a broken image.

## [2026-06-05]
- **DPI Bypass Hack**: Integrated a patched `deltachat-rpc-server` binary into the Docker setup to bypass SSL DPI connection blocks when communicating with chatmail.
- **Resilient Sending Mode**: Added `/resilient` admin command to configure resilient mode (accepts `on`/`off`/`1`/`0`/`true`/`false`, or no arguments to query current status). When enabled, each outgoing message is sent through all configured mail relays using resending mechanism in a non-blocking background thread to bypass chatmail blocking issues without causing UI delays, while ensuring deduplication into a single message bubble on the recipient client.

## [2026-06-04]
- **Thread-safe RPC Proxy**: Implemented `RpcProxy` to serialize Delta Chat JSON-RPC calls via a thread lock, preventing deadlocks and concurrent access hangs.
- **Improved Userbot Reliability**: Added a 15-second timeout to userbot history fetches via `asyncio.wait_for` to prevent hanging on slow network requests.
- **Configurable Limits via Environment Variables**:
  - Allowed configuring local Delta Chat message retention duration (`DELETE_DEVICE_AFTER`, default 7 days) and maximum downloaded attachment size (`MAX_ATTACHMENT_SIZE_MB`, default 50 MB) through the `.env` file and `docker-compose.yml`.
- **Database Cleanup Utility**: Added `cleanup_db.py` to allow administrators to safely purge local Delta Chat message history using dynamic column resolution and SQLite IDs fallback, making it easy to run `VACUUM` and shrink bloated databases.
- **Exclude Webpage Link Previews**: Prevented download and relay of webpage link previews (`MessageMediaWebPage`) from Telegram userbot.

## [2026-06-03]
- **Breaking Change: Root Execution & Centralized Data Directory**:
  - Centralized all persistent files under a single host `./data` directory (database, Userbot session, and Delta Chat config/accounts).
  - Modified the Docker setup to run the bridge bot as `root` user inside the container, standardizing privilege levels with other bots (like `deltachat_bouncer`).
  - Added environment variable configuration in `docker-compose.yml`: `DB_PATH`, `USERBOT_SESSION_PATH`, and `XDG_CONFIG_HOME`.
  - Removed host non-root UID/GID mapping and host-level user check in `update.sh`.

## [2026-06-02]
- **Adaptation for New Core History Resending**: 
  - Increased history relay limit from 3 to 10 when bridging a channel to seed the broadcast group. This enables the new deltachat-core / chatmail core to automatically resend the last 10 messages to newly connected subscribers.
  - Removed manual history relay on new member join events, since this is now handled natively by the core/server.
  - Removed the redundant `*(The last 3 posts have been relayed as history)*` notice from the channel creation output.

## [2026-05-22]
- **Standardized Welcome Greeting**: Refactored the welcome greeting to be exactly identical to the output of the `/help` command instead of a custom welcome prefix message.
- **Fixed Greeting Arguments Bug**: Resolved a calling parameter bug where the welcome greeting method call to `get_dc_help_text` had incorrect positional arguments, which previously caused the greeting check to fail silently in logs.

## [2026-05-21]
- **Suppressed Telethon Reconnection Logs**: Avoided spamming the admin chat with internal Telethon connection errors and `AttributeError` tracebacks. Since the built-in connection watchdog handles reconnection automatically, these transient logs are now suppressed from chat notifications while still being logged to the console/system logs.

## [2026-05-02]
- **Multi-transport Support (Backup Relays)**: Added support for multiple email transports on a single account for high availability.
  - Core automatically fails over to backup relays if the primary server (`chat.gluek.info`) is down.
  - New admin command `/transports` to view configured relays, connectivity status, and usage statistics.
  - New admin commands `/addtransport` and `/rmtransport` to manage relays from the chat.
  - New CLI command `python bot.py init transport` for manual relay setup.
- **Transport Statistics Tracking**: The bot now tracks the number of messages sent and received per transport address.

## [2026-05-01]
- **Telegram Login Code Forwarding**: Automatically detects login/verification codes from Telegram's service account (ID 777000) and forwards them to the bot admin via both Telegram and Delta Chat. This solves the "locked account" problem where Telegram sends login codes to the account itself.
- **New `/userbotjoin` Command**: Added a command to join channels or groups via an invite link (supports private `t.me/+hash`, public links, and `@usernames`). Available on both Telegram and Delta Chat for admins.
- **Invite Link Persistence**: The `/userbotjoin` command saves the provided invite link in the database, allowing the bot to automatically re-sync and rejoin the channel even if its ID or username changes in the future.

## [2026-04-29]
- **Improved Edit Handling**: When a Telegram message is edited, the bot now deletes the previous version in Delta Chat before sending the updated one (with the `✏️ [Edited]` prefix), preventing message duplication and clutter.
- **Smarter Deletion Rate Limiting**: Technical deletions (like replacing an old message with an edit) are now exempt from the deletion safety limit, ensuring they don't block legitimate user-initiated deletions.

## [2026-04-27]
- **Bidirectional Deletion Sync**: Messages deleted in Telegram are now removed from Delta Chat, and vice versa.
- **Deletion Safety Guard**: Implemented a safety limit (5 deletions per 60 seconds) to prevent accidental bulk-deletions. The bot notifies the admin when the limit is reached.
- **Improved History Relay**: Resolved an issue where join events were not detected for Delta Chat broadcast channels. The bot now proactively sends a history preview (last 3 posts) directly to the user's private chat when they request the invite link.
- **Enhanced Admin Visibility**: The `/channels` command in Delta Chat now displays all bridged channels to the bot owner, including private ones or those without a public username.
- **Global Rate Limiting**: Added a global outgoing message limit for Delta Chat (60 messages per minute) to ensure compatibility with chatmail server limits, including automated admin warnings.
- **Formatting Fixes**: Improved HTML/Markdown compatibility for history relay notifications.

## [2026-04-20]
- **Userbot Watchdog**: Implemented a background health check that automatically restarts the Userbot client if it faces fatal connection errors or internal failures.
- **Historical Message Bridging**: When a new channel or group is bridged, the bot now automatically relays the last 3 posts from Telegram to the new Delta Chat chat to provide immediate context. Fixed "Could not find input entity" error during history fetch.
- **Improved Relay Logic**: Refactored the core Userbot message relayer for better consistency and maintainability.

## [2026-04-18]

### Added

- **Real Telegram Stats**: The bridge now fetches real subscriber counts from Telegram via Userbot.
- **Improved `/channels` list**: Shows both Telegram and Delta Chat subscriber/member counts.
- **DC Admin Commands**: Added `/channeladd` and `/channelremove` to Delta Chat (restricted to `admin_dc_email`).
- **Donate Command**: Added `/donate` command to get links for supporting the project development.
- **Code Refactoring**: Unified channel bridging logic for better maintainability.

All notable changes to this project will be documented in this file.

## [2026-04-16]

### Added

- **Delta Chat Channel Discovery:** Added `/channels` command to the Delta Chat bot for browsing public Telegram channels.
- **Easy Subscriptions:** Added support for `/channelN` (link) and `/channelNqr` (QR code) commands in Delta Chat.
- **Improved Statistics:** Removed reaction counts (🙂) from channel stats as they are not currently relevant for broadcast bridges.
- **Better Formatting:** Switched to `t.me/username` format in channel lists.
- **QR Code Support:** Integrated `qrcode` library to generate invite link images.

## [2026-04-15]

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2026-04-13]

### Added

- **Userbot Support (Telethon):** The bot can now bridge Telegram channels **without being an administrator**. This is achieved by integrating the Telethon MTProto library, allowing the bot to act as a regular subscriber.
- **Double Bridge Protection:** Implemented a deduplication mechanism that prevents duplicate messages if a channel is bridged via both the core bot (as admin) and the userbot.
- **Enhanced `/channeladd`:** The command now automatically falls back to userbot mode if the core bot lacks the necessary permissions to read a channel. **Now supports regular groups** in read-only broadcast mode (Stealth Bridging).
- **Security & Permissions:** Restricted all channel management commands (`/channeladd`, `/channels`, etc.) to the **Bot Owner only** to protect the Userbot account.
- **Auto-Sync / Migration:** Added automatic Userbot subscription synchronization. When switching to a new Telegram account, the bot will automatically re-join all previously bridged channels with a randomized, human-like delay (5-20s).
- **Stealth mode:** bridge any Telegram group as a read-only Delta Chat broadcast channel.
- Added `/groups` command to discover joinable Telegram groups for the technical account.
- Significant latency improvements for Userbot mode (enabled concurrent update processing).
- Fixed "Ghost Edits" in Userbot mode by implementing content-based change detection.
- Fixed media filename preservation and 50MB size detection for Userbot events.

## [2026-04-12]

### Changed

- **Message Deletion:** Messages are now automatically deleted from the bot's database after 7 days (instead of 1 hour) to prevent "message does not exist" errors for reactions and replies.
- **Edit Debounce:** Added a 60-second debounce for edited messages to suppress Telegram's automatic link-preview "edits" and reduce log spam.
- **Update Script:** Enhanced `update.sh` to automatically check for new Git commits and rebuild the Docker container only if changes are found. Added support for crontab-based automatic updates.

## [2026-04-09]

### Changed

- **Telegram → Delta Chat sender display:** Messages bridged from Telegram now show the original author's name as the sender (via `override_sender_name`) instead of appearing as sent by the bot with the name prefixed in the text. Applies to both regular messages and edited messages.

## [2026-03-22]

### Added

- **Large Video Fallback:** The bot now automatically downgrades and relays videos larger than 20 MB using lower available resolutions (e.g., 720p, 480p) provided by the Telegram Bot API, preventing silent drops and timeouts. A note is appended to the message in DC when this happens.
- Updated `python-telegram-bot` to version 22.7 for extended Bot API features (`VideoQuality` support).

## [2026-03-21]

### Added

- Support for bridging **video notes** (video circles), **locations**, and **live-locations** / venues from Telegram to Delta Chat.
- **Live Location On-Demand Updates:** When a live location is active, simply reply with `/locupdate` in Delta Chat to receive the real-time position without spamming the chat log.
- **Live Location Auto-End:** When a live location broadcast is manually stopped or expires in Telegram, the bot will now automatically send a final "🛑 Live Location Ended" message with the last known coordinates to Delta Chat.

## [2026-03-20]

### Changed

- **Detailed statistics** in `/stats` (bridges) and `/channels` (channels): now shows group/channel names, message counts (with 💬 icon), and reaction counts (with 🙂 icon).
- **Sub-admin system** for private mode...
- Telegram-side `/bridge` command that auto-creates a Delta Chat group (with the same name and avatar), links it, and sends an invite link.
- Telegram-side `/unbridge` command.
- Support for bridging **private Telegram channels** (without a public `@username`) using numeric IDs.
- `my_chat_member` auto-notifications: bot notifies owner when added as admin to a channel.

### Fixed

- /bridge command error ("Method not found") by using the correct `create_group_chat` RPC method.
- Reverted channel auto-notifications for sub-admins (now owner-only for privacy).

## [2026-03-19]

### Added

- Telegram channel → Delta Chat broadcast bridging (one-way relay of posts with media/avatar sync).
- These commands are available for use in Delta Chat by any user interacting with the bot.

- `/channels` - List all available public Telegram channels.
- `/channelN` - Get the text invite link for channel number N (e.g., `/channel5`).
- `/channelNqr` - Get the QR code image for channel number N.
- `/donate` - Get links to support bot development.
- `/help` - Show Delta Chat bot help.
- `/stats` - Show bridge statistics for the current chat.
- `/locupdate` - (Reply only) Fetch latest coordinates for a live location message.

### Telegram Management Commands (Owner Only)

## [2026-03-18]

### Added

- Automatic handling of Telegram group → supergroup migration.
- Retry logic with exponential backoff for Telegram API timeouts.
- Bidirectional message reaction proxying (emoji syncing).
- Native quoting/reply support using `quoted_message_id`.
- Dynamic help text showing **Mode: Private** or **Mode: Public**.

### Fixed

- `/bridge` command behavior in private chats.
- Media relaying stability.

## [2026-03-17]

### Added

- Support for bridging Telegram polls (including final results).
- Two-way media bridging (images, videos, voice, gifs, stickers, docs).
- Docker Compose support.
- Rate limiting (30 msgs/min per chat).

### Changed

- Refactored database to use SQLite (`bridge.db`).
