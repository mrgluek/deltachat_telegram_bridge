# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2026-03-20]

### Added

- **Sub-admin system** for private mode. The bot owner can designate sub-admins (`/adminadd`, `/adminremove`, `/admins`) who can independently bridge groups and channels but only see and manage their own resources.
- Telegram-side `/bridge` command that auto-creates a Delta Chat group (with the same name and avatar), links it, and sends an invite link.
- Telegram-side `/unbridge` command.
- Support for bridging **private Telegram channels** (without a public `@username`) using numeric IDs.
- `my_chat_member` auto-notifications: bot notifies owner when added as admin to a channel.

### Fixed

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
