import logging

import database

logger = logging.getLogger("tg_dc_bridge")


def _inline_links(text: str, entities) -> str:
    if not entities or not text:
        return text
    links = []
    for ent in entities:
        if ent.type == "text_link" and ent.url:
            links.append((ent.offset, ent.offset + ent.length, ent.url))
    if not links:
        return text
    links.sort(key=lambda x: x[0], reverse=True)
    for start, end, url in links:
        display_text = text[start:end]
        if url not in display_text:
            text = text[:end] + f" ( {url} )" + text[end:]
    return text


def to_dc_markdown(text: str) -> str:
    if not text:
        return ""
    return (text.replace("<b>", "**").replace("</b>", "**")
            .replace("<i>", "*").replace("</i>", "*")
            .replace("<code>", "`").replace("</code>", "`")
            .replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))


def get_tg_help_text(name: str, user_id: int) -> str:
    admin_tg = database.get_config("admin_tg_id")
    mode = "Private (bot owner only)" if admin_tg else "Public (group admins only)"
    lines = [
        f"👋 Hi {name} (<code>{user_id}</code>)!\n",
        f"I'm the DC Bridge bot. Current mode: <b>{mode}</b>\n",
        f"I relay messages between Telegram and Delta Chat groups.\n",
        f"Commands:",
        f"/help — Show this help message",
        f"/id — Show group's chat ID",
        f"/bridge — Bridge this TG group to a new DC group",
        f"/unbridge — Remove the bridge from this TG group",
        f"/stats — Show bridge statistics",
        f"/invite — Get Delta Chat bot/group invite link",
        f"/inviteqr — Get Delta Chat bot/group invite QR code",
        f"/donate — Support bot development ❤️",
    ]
    if database.is_owner(user_id):
        lines.append(f"\n<b>⚙️ Channel & Userbot (Owner):</b>")
        lines.append(f"/channeladd @name or ID — Bridge a channel/group (bot must be admin OR use Userbot)")
        lines.append(f"/userbotjoin link — Join channel/group via Userbot (no admin needed; use before /channeladd)")
        lines.append(f"/channels — List bridged channels")
        lines.append(f"/groups — List Userbot groups to bridge")
        lines.append(f"/channel N — Get channel invite link")
        lines.append(f"/channelqr N — Get channel QR invite")
        lines.append(f"/channelremove N [tg|dc] — Remove a channel bridge (add dc for DC→TG channels)")
        lines.append(f"/userbotsync — Force Userbot re-sync")
        lines.append(f"\n<b>👥 Sub-admins (Owner):</b>")
        lines.append(f"/adminadd <i>user_id</i> — Add a sub-admin")
        lines.append(f"/adminremove <i>user_id</i> — Remove a sub-admin")
        lines.append(f"/admins — List sub-admins")
    else:
        lines.append(f"\n<b>📡 Channels (Public):</b>")
        lines.append(f"/channeladd @name or ID — Bridge a channel/group")
        lines.append(f"/userbotjoin link — Join channel/group via Userbot")
        lines.append(f"/channels — List bridged channels")
        lines.append(f"/channel N — Get channel invite link")
        lines.append(f"/channelqr N — Get channel QR invite")
        lines.append(f"/channelremove N [tg|dc] — Remove a channel bridge (add dc for DC→TG channels)")
    lines.append(f"\nTo get started, add me to a Telegram group and use /bridge to connect it to Delta Chat.")
    lines.append(f"\nℹ️ Make sure Group Privacy is turned off in @BotFather → Bot Settings.")
    lines.append(f"\nRun your own bot: https://github.com/mrgluek/deltachat_telegram_bridge")
    return "\n".join(lines)
