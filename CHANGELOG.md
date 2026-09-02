## [2.13.0] - 2026-09-02
- **Channel Reconciliation & Catchup Improvements**:
  - **Robust Entity Resolution with Fallbacks**: Added `_resolve_userbot_entity` that attempts resolution via numeric Telegram ID, then falls back to `@username`, and finally to invite links. Resolves entity lookup failures for channels not previously cached in the session database.
  - **Automatic Channel Auto-Joining**: Added auto-join check in `reconcile_channel` to automatically subscribe the Userbot (`JoinChannelRequest`) to bridged channels if `left=True`, restoring MTProto real-time push update delivery.
  - **Chronological Reverse Pagination (`reverse=True`)**: Fixed Telethon `get_messages(min_id=last_id, limit=N, reverse=True)` pagination to fetch missed posts in chronological ascending order, preventing skipped message gaps when more than 50 posts were missed.
  - **Zero `last_msg_id` Initialization**: Automatically initializes `last_msg_id` to the latest post for newly bridged channels (`Post #None`), allowing reconciliation to start tracking them immediately.
  - **Reduced Reconciliation Interval**: Decreased background reconciliation check interval from 10 minutes (600s) to 3 minutes (180s).
- **Manual `/catchup` and `/reconcile` Commands**:
  - Added `/catchup` command for administrators in both Delta Chat and Telegram.
  - Supports catching up a specific channel (e.g. `/catchup @pezduzalive` or `/catchup <id>`) or all bridged channels (`/catchup`), providing an immediate status report of queued missed messages.
- **Database Schema & Migrations**:
  - Added `tg_participants_count` column and migration to the `channels` table.

## [2.12.0] - 2026-08-26
- **Automatic Bridge Cleanup & Reconciliation Worker (`cleanup_stale_bridges`)**:
  - Implemented automatic startup and daily background cleanup worker to detect and resolve stale, duplicate, and orphaned bridges.
  - **Orphaned database records**: Scans all `bridges` and `channels` in `bridge.db`, removing records and related `message_map` mappings whose Delta Chat groups no longer exist in Delta Chat core.
  - **Duplicate bridge deduplication**: Automatically identifies duplicate bridges mapping to the same Telegram chat ID (`tg_chat_id`), preserving active bridges with subscribers/messages while deleting empty duplicate chats (`0 👤`) from Delta Chat and purging them from SQLite.
  - **Dead ghost bridge cleanup**: Safely removes empty placeholder bridges (`Bridge -100...` / `TG Group -100...` with 0 subscribers and 0 messages) when the corresponding Telegram group is inaccessible.
  - **Manual `/cleanup` command**: Added owner/admin `/cleanup` command in both Telegram and Delta Chat with detailed summary reports.
- **Database Migrations & Schema**:
  - Added `created_at` timestamp column migration to the `bridges` table.
  - Added `remove_bridge_pair` function to securely clean up specific bridge pairs and message maps.

## [2.11.0] - 2026-08-17
- **Telegram Rich Formatting & New Post Format Support**:
  - Implemented comprehensive Telegram entity-to-Markdown formatting engine (`_format_telegram_entities`) across both Bot API and Userbot pipelines.
  - Full conversion of Telegram rich styling: bold (`**text**`), italic (`*text*`), underline (`__text__`), strikethrough (`~text~`), spoiler (`||text||`), inline code (`` `code` ``), code blocks with language syntax highlighting (```` ```lang\ncode\n``` ````), blockquotes (`> text`), expandable blockquotes, text links (`[text](url)`), text mentions, and headers.
  - Added accurate UTF-16 code unit offset calculation (`_utf16_to_py_indices`) to prevent formatting shifts and string corruption on messages with multi-byte Unicode characters and emojis.
- **Public Post Web Recovery & `MessageMediaUnsupported` Fallback**:
  - Added automatic web preview fallback extraction (`_extract_public_tg_post`) for public channel posts encountering unsupported MTProto media types or new rich message formats.
  - Automatically parses HTML message bodies to Delta Chat Markdown and fetches high-resolution media attachments.
  - Replaced cryptic `[MessageMediaUnsupported]` placeholders with descriptive, user-friendly labels pointing to the original Telegram post.

## [2.10.1] - 2026-08-10
- **Reduced Channel Reconciliation Interval to 10 Minutes**:
  - Decreased `reconcile_channels_loop` check interval from 15 minutes (900s) to 10 minutes (600s), ensuring faster recovery of missed posts after temporary outages or restarts.

## [2.10.0] - 2026-08-10
- **Abort Reconciliation Loop on Userbot Disconnect**:
  - Implemented immediate abort (`break`) of `reconcile_channels_loop` when `userbot_client` disconnects or is reconnecting, preventing 53 consecutive `'NoneType' object has no attribute 'get_entity'` warning messages.
  - Added disconnection patterns (`Cannot send requests while disconnected`, `'NoneType' object has no attribute`) to `_TRANSIENT_POLLING_ERRORS` filters.

## [2.9.9] - 2026-08-10
- **10-Minute In-Memory Caching for `/channels` Command**:
  - Added 10-minute in-memory caching (`_channels_cache`) for the `/channels` report output per user. Repeated requests for `/channels` now respond instantly with 0ms delay.
  - Automatic cache invalidation (`invalidate_channels_cache`) triggered whenever a channel bridge is added or removed (`/channeladd`, `/channelremove`).

## [2.9.8] - 2026-08-10
- **Asynchronous `/channels` Command Data Gathering & Reply Retries**:
  - Offloaded the 53-channel synchronous RPC query loop (`get_basic_chat_info` & `get_chat_contacts`) in `tg_channels_command` to a background thread pool executor (`loop.run_in_executor`), completely eliminating main asyncio event loop freezing and socket timeouts.
  - Wrapped Telegram command reply calls (`reply_text`) in `retry_async` (up to 3 retries with 2s delay) to safely handle transient network connection timeouts.

## [2.9.7] - 2026-08-10
- **Fix `Updater.start_polling` Unexpected Keyword Argument**:
  - Removed invalid `read_timeout` argument from `Updater.start_polling()` calls (in PTB v20+, `read_timeout` is configured on `get_updates_request` via `ApplicationBuilder`).

## [2.9.6] - 2026-08-10
- **Asynchronous Delta Chat Admin Alert Delivery**:
  - Offloaded Delta Chat log notification sending in `AdminLogHandler` to non-blocking background threads (`_send_admin_dc_message_bg`) to prevent JSON-RPC pipe lock deadlocks (`RpcProxy._lock`).
  - Added thread-safe in-memory caching (`_admin_dc_chat_id_cache`) for the administrator chat ID, eliminating redundant `create_contact` and `create_chat_by_contact_id` RPC calls on every log emission.

## [2.9.5] - 2026-08-10
- **Watchdog Telegram Polling Restart Retry Loop**:
  - Added automatic retry loop (3 attempts with 5s delay) when the Watchdog detects an offline/unhealthy Telegram Bot API polling connection and initiates `start_polling()`.
  - Downgraded intermediate restart failures to `warning` and added explicit logging indicating that watchdog will retry on the next 60s cycle if the network is persistently down.

## [2.9.4] - 2026-08-10
- **Retries and Contextual Error Reporting for DC → Telegram Relay**:
  - Wrapped text-only and media fallback `send_message` / `edit_message_*` calls to Telegram in `retry_async` (3 attempts with exponential backoff: 2s, 4s, 8s).
  - Enhanced Telegram relay error logs with full context: Delta Chat message ID (`msg_id`), Delta Chat chat ID (`dc_chat_id`), Telegram chat ID (`tg_chat_id`), and explicit mention of retries attempted ("after 3 retries").
  - Moved success logging inside `async_relay_to_tg` after confirmation of message transmission and DB mapping.

## [2.9.3] - 2026-08-10
- **Userbot Media Download Retries & Contextual Error Reporting**:
  - Added exponential backoff retries (`retry_async`, up to 3 attempts) for Telethon/Userbot and Bot API media downloads.
  - Enhanced media download error logs with full context details: channel/chat ID, post/message ID, file name, media type, and size in bytes.
  - Added automatic cleanup of orphaned temporary files (`unlink`) when media downloads fail mid-way.

## [2.9.2] - 2026-08-10
- **Tuned `get_updates_request` Timeouts and Suppressed `get_updates` Shutdown Errors**:
  - Explicitly configured `get_updates_request` via `ApplicationBuilder` with dedicated HTTPX timeouts (`read_timeout=60s`, `connect_timeout=30s`) and passed `read_timeout=30s` to `start_polling` calls to match long-polling cycles and avoid TCP `httpcore.ConnectTimeout` exceptions.
  - Added `"Error while calling \`get_updates\`"` and `"Error while calling get_updates"` to `_TRANSIENT_POLLING_ERRORS` log filters to suppress harmless python-telegram-bot cleanup warnings during network blips or updater restarts.

## [2.9.1] - 2026-07-29
- **Silence Asyncio Task Destruction Warnings**:
  - Added `"Task was destroyed but it is pending"` to `_TRANSIENT_POLLING_ERRORS` in `bot.py` to suppress harmless asyncio / Telethon garbage collection logs from spamming the admin error log channel during reconnects.

## [2.9.0] - 2026-07-21
- **Telegram Paid Media & Extended Post Types Support**:
  - Added support for Telegram Paid Media (`paid_media` / `MessageMediaPaidMedia` — posts/photos/videos locked with Telegram Stars, like https://t.me/Finindie/2874). The bot now relays the text, caption, star price label (`[⭐ Paid Media (X ⭐)]`), and downloadable media/previews instead of skipping the post.
  - Added full support for Stories (`story` / `MessageMediaStory`), Giveaways (`giveaway` / `MessageMediaGiveaway`), Polls (`poll` / `MessageMediaPoll`), Contacts (`contact` / `MessageMediaContact`), Invoices (`invoice`), and fallback handling for unrecognized Telegram media types.
  - Updated content hashing (`_get_content_hash`) and media size calculation (`_get_ptb_media_size`, `_get_media_size`) for both Bot API and Telethon userbot modes.

## [2.8.0] - 2026-07-20
- **Latency Optimization & last_msg_id Caching**:
  - Implemented in-memory caching (`_last_msg_id_cache`) for the last relayed post ID of each bridged channel, dramatically reducing SQLite database reads/writes during message relay and de-duplication checks.
  - Optimized the Telethon author lookup mechanism inside `_relay_userbot_message`: for channel messages, the sender title is resolved without slow, blocking MTProto network calls (`msg.get_sender()`), and for group messages, a quick 3.0s timeout is enforced. This prevents channel history and event processing queues from stalling.
  - Removed the automatic 3 recent posts preview when calling `/channelN` command, preventing connection bottlenecks in Delta Chat private chats.
- **Admin Command `/status`**:
  - Added a new `/status` administrative command for both Delta Chat and Telegram (private chat, admin-only).
  - The status report displays active Telegram Bot API status, Userbot connection details, Delta Chat primary account settings, active message queue worker states, and recent activity stats (last 5 transfers with relative time formats).
  - Displays each bridged channel's last post number and direct link (e.g. `https://t.me/channel/27`) for easy monitoring.

- **Self-Healing Channel Message Reconciliation**:
  - Added a background reconciliation loop (`reconcile_channels_loop`) that runs every 15 minutes, comparing the local `last_msg_id` with the absolute latest post ID on Telegram.
  - Automatically fetches and relays any missed posts (up to 50 per channel per loop run) in sequential chronological order, ensuring zero message loss during bot restarts or network downtime.

- **Fix for Hidden-Link/Webpage-Only Posts**:
  - Implemented automatic title, description, and link extraction from webpage previews (`MessageMediaWebPage`) when the message text is empty (useful for native articles and hidden link posts in channels).
  - Automatically downloads and relays the webpage preview photo to Delta Chat to match the Telegram visual layout.

## [2.7.0] - 2026-07-11
- **Sequential Channel Event Queuing (Perfect Order Delivery)**:
  - Implemented per-channel event queues (`_channel_queues`) and sequential background workers (`_channel_queue_worker`) in `bot.py` for processing new messages, edits, and deletions.
  - This ensures that posts from each channel are processed and delivered to Delta Chat in the exact chronological order they occurred in Telegram (e.g. text-only posts are no longer sent before media-heavy posts that take longer to download).
  - Queue worker tasks are correctly tracked and cancelled during userbot client restarts to prevent memory leaks and zombie processes.

## [2.6.5] - 2026-07-11
- **Fix Userbot Session DB Lock Leak and Silence Forbidden/BadRequest Telegram Errors**:
  - Explicitly close the SQLite session database connection via `session.close()` and trigger Python garbage collection (`gc.collect()`) when disconnecting or restarting the Telethon client, preventing loop hangs from causing persistent `database is locked` error loops.
  - Downgraded `telegram.error.Forbidden` (e.g., bot blocked by user) and `telegram.error.BadRequest` exceptions in `tg_error_handler` from `logger.error` to `logger.warning` to stop unnecessary email alert spam.
  - Logged userbot client startup failures as warning instead of error since they are safely retried by the watchdog.

## [2.6.4] - 2026-07-06
- **Fix Dependency Conflict/NameError:** Pinned `deltabot-cli==8.1.2` and `deltachat2[full]<1.0.0` in `requirements.txt` to resolve dependency conflicts and avoid the `ChatType` NameError/ImportError bugs introduced in newer, incompatible versions of `deltachat2`.

## [2.6.3] - 2026-07-06
- **Fix Userbot Zombie Reconnect Hangs**:
  - Modified the watchdog health check in `bot.py` to trigger a real, non-cached updates state request (`GetStateRequest`) instead of using cached `get_me()`.
  - This ensures that if the Telethon client is in a zombie state (connected to `None` or has a destroyed sender task loop), the API call will time out, allowing the watchdog to correctly detect the unhealthy status and restart the client.

## [2.6.2] - 2026-07-03
- **Zombie Process Reaping:** Enabled `init: true` in Docker Compose to automatically reap zombie processes in the bot container, preventing PID limit exhaustion.

## [2.6.1] - 2026-06-30
- **Resolve Telegram Username Resolution API Flood limits**:
  - Restructured the Userbot channel synchronization loop in `bot.py` to prioritize numeric ID lookups.
  - Avoid redundant API calls (`ResolveUsernameRequest`) on Telegram servers for already joined/cached channels, preventing account rate limits (`FloodWaitError` / Connect limits).
  - Added a larger (2.0s) delay between fallback username resolution attempts.

## [2.6.0] - 2026-06-30
- **Sequential Message ID Deduplication for Channels**:
  - Added `last_msg_id` column to `channels` database table and automatic SQLite migration.
  - Track the highest relayed Telegram message ID for each channel.
  - Automatically discard incoming old/duplicate channel posts (both from Bot API and Userbot event polling) if their message ID is less than or equal to the last forwarded message ID.
  - Exclude edits and history previews from deduplication checks to ensure updates and queries remain functional.
  - Added database helper functions and unit tests verifying the deduplication state persistence.

## [2.5.0] - 2026-06-29
- **Versioning, Unit Tests, and GitHub Actions CI**:
  - Added unit test suite in `tests/test_bridge.py` covering database operations, rate limits, and formatting/truncation helper functions.
  - Set up automated CI workflows via GitHub Actions in `.github/workflows/tests.yml`.
  - Added `VERSION = "2.5.0"` tracking to `bot.py` and logged version details during startup.
  - Updated and calculated SemVer version numbers for all historical changelog releases.

## [2.4.2] - 2026-06-29
- **Robust Userbot watchdog and timeout protection**:
  - Added a concurrency guard (`_is_starting_userbot`) to prevent overlapping userbot client startups.
  - Wrapped `userbot_client.start()` in a 60-second timeout to handle socket/handshake hangs.
  - Wrapped the watchdog's API health check in a 15-second timeout, preventing connection hangs from freezing the entire bridge's main loop.
  - Applied `asyncio.wait_for` timeouts to all direct Telethon client calls (including `get_me`, `get_entity`, `get_messages`, `download_media`, `download_profile_photo`, `LeaveChannelRequest`, `JoinChannelRequest`, `CheckChatInviteRequest`, `ImportChatInviteRequest`, `DeleteChatUserRequest`, `GetFullChannelRequest`, and `GetFullChatRequest`) to ensure the bridge remains fully responsive and self-heals under MTProto security desync errors.


## [2.4.1] - 2026-06-25
- **Userbot Sync Stabilization**:
  - Added a 10-second delay before auto-sync triggers after a new userbot account is detected, allowing the Telethon connection to fully stabilize after a reconnect (fixes `Could not resolve @username` errors caused by reconnects).
  - Added a 0.5-second delay between each `get_entity()` call in the sync loop to avoid `ResolveUsername` rate-limiting.
  - Preserved the original Telethon exception when resolution fails to make debugging easier instead of swallowing it.


## [2.4.0] - 2026-06-25
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
  - Suppressed transient connection/read exceptions (such as `httpcore.ReadError`, `httpx.ReadError`, and general polling warning logs) from forwarding notifications to the admin chat.

## [2.3.1] - 2026-06-22
- **Edit Relay Optimizations**:
  - Prevent edit relaying for channels/groups with more than 10,000 subscribers/members to save traffic.
  - Skip edit relaying for messages with attached files larger than 1 MB.
  - Ignore message edits for messages older than 7 days (1 week).
- **Subscriber Count Updates**:
  - Automatically update Telegram subscriber count in the database during `/channelssync` runs.
- **Fix Subscriber Count Calculation**:
  - Dynamically check if the bot's own contact ID is present in the group contacts list before subtracting 1, fixing the reporting of 0 subscribers for broadcast channels (where the bot's own contact is typically excluded from the contacts list).


## [2.3.0] - 2026-06-18
- **Bidirectional In-place Message Edits**:
  - Implemented bidirectional in-place message edits between Telegram and Delta Chat.
  - Telegram → Delta Chat edits are processed via `send_edit_request` (with delete-and-resend fallback). Fixed a bug where edits within 120s of creation were ignored due to processed cache duplication.
  - Delta Chat → Telegram edits are processed via the core's `MSGS_CHANGED` event, supporting both text and media captions (with text/caption editing failover).
- **Admin Command `/channelssync`**:
  - Added a dedicated `/channelssync` administrative command in Delta Chat to force-refresh all bridged channel names and avatars from Telegram (supporting fallback to Userbot API).

## [2.2.0] - 2026-06-16
- **Robust E2E Failover Loops & Key Fallbacks**:
  - Added fallback support for both `chat_id` and `chatId` keys when extracting details from raw RPC message snapshots.
  - Downgraded permanent E2E failure and resend logs to `WARNING` to prevent them from triggering the admin error email handler.
  - Filtered out loop-prone failover keywords from `AdminLogHandler.emit()` to completely avoid infinite logging/emailing loops when the admin's E2E key is missing.
  - Removed administrative failover alert emails completely, relying entirely on structured logging to prevent any potential loop risks.
- **Telegram Startup Resilience**:
  - Configured custom HTTPX request timeouts (30s) and pool sizes to handle API congestion.
  - Wrapped the startup `tg_app.initialize()` call in a 5-attempt retry loop with backoff to handle transient network hiccups on boot.
- **Automatic Transport Failover:** Implemented a robust, event-driven transport failover mechanism. The bot now listens to the core's `MSG_FAILED` event. When a message fails to deliver, it automatically switches `configured_addr` to the next configured backup transport, and schedules a resend of the message using exponential backoff (5s, 10s, 20s, 40s...) via an asynchronous timer thread. The failover process is limited to a maximum of 10 attempts per message to prevent infinite loops, and the administrator is alerted only on the first failure.


## [2.1.1] - 2026-06-09
- **Animation Relay Fix**: Corrected forwarding of Telegram animation/GIF files. Telegram encodes animations as silent MP4 video files under the hood. By changing the temporary file suffix from `.gif` to `.mp4`, the files are correctly identified as `video/mp4` in Delta Chat, allowing clients to play them rather than displaying a broken image.

## [2.1.0] - 2026-06-05
- **DPI Bypass Hack**: Integrated a patched `deltachat-rpc-server` binary into the Docker setup to bypass SSL DPI connection blocks when communicating with chatmail.
- **Resilient Sending Mode**: Added `/resilient` admin command to configure resilient mode (accepts `on`/`off`/`1`/`0`/`true`/`false`, or no arguments to query current status). When enabled, each outgoing message is sent through all configured mail relays using resending mechanism in a non-blocking background thread to bypass chatmail blocking issues without causing UI delays, while ensuring deduplication into a single message bubble on the recipient client.

## [2.0.1] - 2026-06-04
- **Thread-safe RPC Proxy**: Implemented `RpcProxy` to serialize Delta Chat JSON-RPC calls via a thread lock, preventing deadlocks and concurrent access hangs.
- **Improved Userbot Reliability**: Added a 15-second timeout to userbot history fetches via `asyncio.wait_for` to prevent hanging on slow network requests.
- **Configurable Limits via Environment Variables**:
  - Allowed configuring local Delta Chat message retention duration (`DELETE_DEVICE_AFTER`, default 7 days) and maximum downloaded attachment size (`MAX_ATTACHMENT_SIZE_MB`, default 50 MB) through the `.env` file and `docker-compose.yml`.
- **Database Cleanup Utility**: Added `cleanup_db.py` to allow administrators to safely purge local Delta Chat message history using dynamic column resolution and SQLite IDs fallback, making it easy to run `VACUUM` and shrink bloated databases.
- **Exclude Webpage Link Previews**: Prevented download and relay of webpage link previews (`MessageMediaWebPage`) from Telegram userbot.

## [2.0.0] - 2026-06-03
- **Breaking Change: Root Execution & Centralized Data Directory**:
  - Centralized all persistent files under a single host `./data` directory (database, Userbot session, and Delta Chat config/accounts).
  - Modified the Docker setup to run the bridge bot as `root` user inside the container, standardizing privilege levels with other bots (like `deltachat_bouncer`).
  - Added environment variable configuration in `docker-compose.yml`: `DB_PATH`, `USERBOT_SESSION_PATH`, and `XDG_CONFIG_HOME`.
  - Removed host non-root UID/GID mapping and host-level user check in `update.sh`.

## [1.11.0] - 2026-06-02
- **Adaptation for New Core History Resending**: 
  - Increased history relay limit from 3 to 10 when bridging a channel to seed the broadcast group. This enables the new deltachat-core / chatmail core to automatically resend the last 10 messages to newly connected subscribers.
  - Removed manual history relay on new member join events, since this is now handled natively by the core/server.
  - Removed the redundant `*(The last 3 posts have been relayed as history)*` notice from the channel creation output.

## [1.10.2] - 2026-05-22
- **Standardized Welcome Greeting**: Refactored the welcome greeting to be exactly identical to the output of the `/help` command instead of a custom welcome prefix message.
- **Fixed Greeting Arguments Bug**: Resolved a calling parameter bug where the welcome greeting method call to `get_dc_help_text` had incorrect positional arguments, which previously caused the greeting check to fail silently in logs.

## [1.10.1] - 2026-05-21
- **Suppressed Telethon Reconnection Logs**: Avoided spamming the admin chat with internal Telethon connection errors and `AttributeError` tracebacks. Since the built-in connection watchdog handles reconnection automatically, these transient logs are now suppressed from chat notifications while still being logged to the console/system logs.

## [1.10.0] - 2026-05-02
- **Multi-transport Support (Backup Relays)**: Added support for multiple email transports on a single account for high availability.
  - Core automatically fails over to backup relays if the primary server (`chat.gluek.info`) is down.
  - New admin command `/transports` to view configured relays, connectivity status, and usage statistics.
  - New admin commands `/addtransport` and `/rmtransport` to manage relays from the chat.
  - New CLI command `python bot.py init transport` for manual relay setup.
- **Transport Statistics Tracking**: The bot now tracks the number of messages sent and received per transport address.

## [1.9.0] - 2026-05-01
- **Telegram Login Code Forwarding**: Automatically detects login/verification codes from Telegram's service account (ID 777000) and forwards them to the bot admin via both Telegram and Delta Chat. This solves the "locked account" problem where Telegram sends login codes to the account itself.
- **New `/userbotjoin` Command**: Added a command to join channels or groups via an invite link (supports private `t.me/+hash`, public links, and `@usernames`). Available on both Telegram and Delta Chat for admins.
- **Invite Link Persistence**: The `/userbotjoin` command saves the provided invite link in the database, allowing the bot to automatically re-sync and rejoin the channel even if its ID or username changes in the future.

## [1.8.1] - 2026-04-29
- **Improved Edit Handling**: When a Telegram message is edited, the bot now deletes the previous version in Delta Chat before sending the updated one (with the `✏️ [Edited]` prefix), preventing message duplication and clutter.
- **Smarter Deletion Rate Limiting**: Technical deletions (like replacing an old message with an edit) are now exempt from the deletion safety limit, ensuring they don't block legitimate user-initiated deletions.

## [1.8.0] - 2026-04-27
- **Bidirectional Deletion Sync**: Messages deleted in Telegram are now removed from Delta Chat, and vice versa.
- **Deletion Safety Guard**: Implemented a safety limit (5 deletions per 60 seconds) to prevent accidental bulk-deletions. The bot notifies the admin when the limit is reached.
- **Improved History Relay**: Resolved an issue where join events were not detected for Delta Chat broadcast channels. The bot now proactively sends a history preview (last 3 posts) directly to the user's private chat when they request the invite link.
- **Enhanced Admin Visibility**: The `/channels` command in Delta Chat now displays all bridged channels to the bot owner, including private ones or those without a public username.
- **Global Rate Limiting**: Added a global outgoing message limit for Delta Chat (60 messages per minute) to ensure compatibility with chatmail server limits, including automated admin warnings.
- **Formatting Fixes**: Improved HTML/Markdown compatibility for history relay notifications.

## [1.7.1] - 2026-04-20
- **Userbot Watchdog**: Implemented a background health check that automatically restarts the Userbot client if it faces fatal connection errors or internal failures.
- **Historical Message Bridging**: When a new channel or group is bridged, the bot now automatically relays the last 3 posts from Telegram to the new Delta Chat chat to provide immediate context. Fixed "Could not find input entity" error during history fetch.
- **Improved Relay Logic**: Refactored the core Userbot message relayer for better consistency and maintainability.

## [1.7.0] - 2026-04-18

### Added

- **Real Telegram Stats**: The bridge now fetches real subscriber counts from Telegram via Userbot.
- **Improved `/channels` list**: Shows both Telegram and Delta Chat subscriber/member counts.
- **DC Admin Commands**: Added `/channeladd` and `/channelremove` to Delta Chat (restricted to `admin_dc_email`).
- **Donate Command**: Added `/donate` command to get links for supporting the project development.
- **Code Refactoring**: Unified channel bridging logic for better maintainability.

All notable changes to this project will be documented in this file.

## [1.6.0] - 2026-04-16

### Added

- **Delta Chat Channel Discovery:** Added `/channels` command to the Delta Chat bot for browsing public Telegram channels.
- **Easy Subscriptions:** Added support for `/channelN` (link) and `/channelNqr` (QR code) commands in Delta Chat.
- **Improved Statistics:** Removed reaction counts (🙂) from channel stats as they are not currently relevant for broadcast bridges.
- **Better Formatting:** Switched to `t.me/username` format in channel lists.
- **QR Code Support:** Integrated `qrcode` library to generate invite link images.

## [1.5.1] - 2026-04-15

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.0] - 2026-04-13

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

## [1.4.3] - 2026-04-12

### Changed

- **Message Deletion:** Messages are now automatically deleted from the bot's database after 7 days (instead of 1 hour) to prevent "message does not exist" errors for reactions and replies.
- **Edit Debounce:** Added a 60-second debounce for edited messages to suppress Telegram's automatic link-preview "edits" and reduce log spam.
- **Update Script:** Enhanced `update.sh` to automatically check for new Git commits and rebuild the Docker container only if changes are found. Added support for crontab-based automatic updates.

## [1.4.2] - 2026-04-09

### Changed

- **Telegram → Delta Chat sender display:** Messages bridged from Telegram now show the original author's name as the sender (via `override_sender_name`) instead of appearing as sent by the bot with the name prefixed in the text. Applies to both regular messages and edited messages.

## [1.4.1] - 2026-03-22

### Added

- **Large Video Fallback:** The bot now automatically downgrades and relays videos larger than 20 MB using lower available resolutions (e.g., 720p, 480p) provided by the Telegram Bot API, preventing silent drops and timeouts. A note is appended to the message in DC when this happens.
- Updated `python-telegram-bot` to version 22.7 for extended Bot API features (`VideoQuality` support).

## [1.4.0] - 2026-03-21

### Added

- Support for bridging **video notes** (video circles), **locations**, and **live-locations** / venues from Telegram to Delta Chat.
- **Live Location On-Demand Updates:** When a live location is active, simply reply with `/locupdate` in Delta Chat to receive the real-time position without spamming the chat log.
- **Live Location Auto-End:** When a live location broadcast is manually stopped or expires in Telegram, the bot will now automatically send a final "🛑 Live Location Ended" message with the last known coordinates to Delta Chat.

## [1.3.0] - 2026-03-20

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

## [1.2.0] - 2026-03-19

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

## [1.1.0] - 2026-03-18

### Added

- Automatic handling of Telegram group → supergroup migration.
- Retry logic with exponential backoff for Telegram API timeouts.
- Bidirectional message reaction proxying (emoji syncing).
- Native quoting/reply support using `quoted_message_id`.
- Dynamic help text showing **Mode: Private** or **Mode: Public**.

### Fixed

- `/bridge` command behavior in private chats.
- Media relaying stability.

## [1.0.0] - 2026-03-17

### Added

- Support for bridging Telegram polls (including final results).
- Two-way media bridging (images, videos, voice, gifs, stickers, docs).
- Docker Compose support.
- Rate limiting (30 msgs/min per chat).

### Changed

- Refactored database to use SQLite (`bridge.db`).
