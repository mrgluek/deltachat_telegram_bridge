# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Sub-admin system** for private mode. The bot owner can designate sub-admins (`/adminadd`, `/adminremove`, `/admins`) who can independently bridge groups and channels but only see and manage their own resources.
- Telegram-side `/bridge` command that auto-creates a Delta Chat group (with the same name and avatar), links it, and sends an invite link.
- Telegram-side `/unbridge` command.
- Support for bridging **private Telegram channels** (without a public `@username`) using numeric IDs.
- `my_chat_member` auto-notifications: bot notifies owner when added as admin to a channel.
- Telegram channel → Delta Chat broadcast bridging (one-way relay).
- `/invite` and `/inviteqr` commands on Telegram side for generating DC join links/QRs.
- `/stats` command for bridge statistics (group-specific in groups, summary in private chat).
- Automatic handling of Telegram group → supergroup migration.
- Retry logic with exponential backoff for Telegram API timeouts.
- Bidirectional message reaction proxying.
- Native quoting/reply support using `quoted_message_id`.
- Dynamic help text showing **Mode: Private** or **Mode: Public**.
- Support for bridging Telegram polls.
- Two-way media bridging (images, videos, voice, gifs, stickers, docs).

### Changed

- Refactored database to use SQLite (`bridge.db`).
- Improved bot greeting and help formatting.

### Fixed

- `/bridge` command behavior in private chats.
- Media relaying stability.

## [Legacy History]

- **2026-03-17**: Initial implementation of rate limiting and Docker Compose support.
