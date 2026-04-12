# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- `/invite` and `/inviteqr` commands on Telegram side for generating DC join links/QRs.
- `/stats` command for bridge statistics (group-specific in groups, summary in private chat).

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
