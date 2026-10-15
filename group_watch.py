"""Bookkeeping for group messages that arrive through the Telegram Bot API.

Every new message in a group is registered when PTB starts processing its
update (handler group -1) and released when processing is finished (handler
group 99). handle_tg_message reports what it did with the message, so each
group message gets exactly one log line saying whether it was relayed or why
it was skipped. Before this, several early returns were silent and a lost
message left no trace at all.

The watchdog in bot.py calls check_stuck() every minute: a message that is
still being processed after STUCK_AFTER seconds, or an update queue that has
updates waiting while nothing gets processed, is logged as a warning and
counted for /status.
"""
import logging
import time
from collections import Counter

import database

logger = logging.getLogger("tg_dc_bridge")

STUCK_AFTER = 120  # seconds a single group message may take before we warn

# update_id -> {"chat", "msg", "kind", "since", "outcome", "warned"}
_inflight: dict[int, dict] = {}
_stats = {
    "since": time.time(),
    "received": 0,
    "relayed": 0,
    "skipped": Counter(),
    "stuck": 0,
    "unhandled": 0,
    "queue_stalls": 0,
}
_processed_updates = 0  # every update PTB started processing (progress marker for the queue check)
_queue_check = {"processed": 0, "waiting_since": None, "warned": False}

# Attribute name on telegram.Message -> label, checked in this order.
_KINDS = (
    ("paid_media", "paid media"), ("photo", "photo"), ("video", "video"),
    ("animation", "GIF"), ("voice", "voice"), ("audio", "audio"),
    ("document", "file"), ("sticker", "sticker"), ("video_note", "video note"),
    ("story", "story"), ("poll", "poll"), ("dice", "dice"), ("game", "game"),
    ("contact", "contact"), ("venue", "venue"), ("location", "location"),
    ("invoice", "invoice"), ("giveaway", "giveaway"),
    ("giveaway_winners", "giveaway winners"), ("text", "text"),
)


def message_kind(msg) -> str:
    """Short human-readable type of a Telegram message, e.g. 'photo (forwarded)'."""
    kind = "other"
    for attr, label in _KINDS:
        if getattr(msg, attr, None):
            kind = label
            break
    if getattr(msg, "forward_origin", None):
        kind += " (forwarded)"
    return kind


def _group_message(update):
    """The new (not edited) message of an update, if it was sent in a group."""
    msg = getattr(update, "message", None)
    chat = getattr(update, "effective_chat", None)
    if msg is None or chat is None or chat.type == "private":
        return None
    return msg


async def on_update_start(update, context=None):
    """Handler group -1: runs first for every update PTB processes."""
    global _processed_updates
    _processed_updates += 1
    msg = _group_message(update)
    if msg is None or not database.get_dc_chats(update.effective_chat.id):
        # Private chats and groups that are not bridged are not tracked.
        return
    entry = {
        "chat": update.effective_chat.id,
        "msg": msg.message_id,
        "kind": message_kind(msg),
        "since": time.monotonic(),
        "outcome": None,
        "warned": False,
    }
    _inflight[update.update_id] = entry
    logger.info(f"Bot API: Received group msg {entry['msg']} in {entry['chat']} "
                f"(update {update.update_id}, {entry['kind']})")


def note_outcome(update, outcome: str, reason: str = ""):
    """Called by handle_tg_message: outcome is 'relayed' or 'skipped' (with a reason)."""
    entry = _inflight.get(getattr(update, "update_id", None))
    msg = getattr(update, "message", None)
    chat_id = entry["chat"] if entry else getattr(getattr(update, "effective_chat", None), "id", "?")
    msg_id = entry["msg"] if entry else getattr(msg, "message_id", "?")
    if entry:
        entry["outcome"] = outcome
    if outcome == "relayed":
        _stats["relayed"] += 1
    else:
        _stats["skipped"][reason] += 1
        logger.info(f"Bot API: Skipped group msg {msg_id} in {chat_id}: {reason}")


async def on_update_end(update, context=None):
    """Handler group 99: runs after all relay handlers have finished with the update."""
    entry = _inflight.pop(getattr(update, "update_id", None), None)
    if entry is None:
        return
    _stats["received"] += 1
    took = time.monotonic() - entry["since"]
    if entry["warned"]:
        logger.warning(f"Bot API: Group msg {entry['msg']} in {entry['chat']} finished after {took:.0f}s "
                       f"(outcome: {entry['outcome'] or 'none'})")
    if entry["outcome"] is None:
        # No relay handler matched the message type, or the handler raised
        # (then the error handler has logged the exception just before this).
        _stats["unhandled"] += 1
        logger.warning(f"Bot API: Group msg {entry['msg']} in {entry['chat']} ({entry['kind']}) "
                       f"was not relayed: no handler finished it")


def check_stuck(queue_size: int, now: float | None = None) -> list[str]:
    """Return (and log) warnings for messages or an update queue that stopped moving.

    Called from the watchdog loop about once a minute.
    """
    now = time.monotonic() if now is None else now
    warnings = []
    for update_id, entry in _inflight.items():
        age = now - entry["since"]
        if age > STUCK_AFTER and not entry["warned"]:
            entry["warned"] = True
            _stats["stuck"] += 1
            warnings.append(f"Bot API: Group msg {entry['msg']} in {entry['chat']} ({entry['kind']}, "
                            f"update {update_id}) is still being processed after {age:.0f}s")

    # Updates waiting in PTB's queue while no new update was started since the
    # last check: the processing loop is blocked (e.g. a handler that hangs).
    q = _queue_check
    progressed = _processed_updates != q["processed"]
    q["processed"] = _processed_updates
    if queue_size > 0 and not progressed:
        if q["waiting_since"] is None:
            q["waiting_since"] = now
        elif now - q["waiting_since"] > STUCK_AFTER and not q["warned"]:
            q["warned"] = True
            _stats["queue_stalls"] += 1
            warnings.append(f"Bot API: {queue_size} updates are waiting but none was processed for "
                            f"{now - q['waiting_since']:.0f}s; update processing is blocked")
    else:
        q["waiting_since"] = None
        q["warned"] = False

    for w in warnings:
        logger.warning(w)
    return warnings


def status_line() -> str:
    """One line for /status: what happened to group messages since the bot started."""
    s = _stats
    since = time.strftime("%Y-%m-%d %H:%M", time.gmtime(s["since"]))
    skipped = sum(s["skipped"].values())
    line = f"{s['received']} received, {s['relayed']} relayed, {skipped} skipped"
    if skipped:
        line += " (" + ", ".join(f"{reason}: {n}" for reason, n in s["skipped"].most_common(3)) + ")"
    problems = []
    if s["unhandled"]:
        problems.append(f"{s['unhandled']} not relayed")
    if s["stuck"]:
        problems.append(f"{s['stuck']} stuck over {STUCK_AFTER // 60} min")
    if s["queue_stalls"]:
        problems.append(f"update queue blocked {s['queue_stalls']}x")
    if problems:
        line += " ⚠️ " + ", ".join(problems)
    return f"{line} (since {since} UTC)"
