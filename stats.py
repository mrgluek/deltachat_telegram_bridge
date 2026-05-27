import logging
import html

import database
from app_context import app_ctx

logger = logging.getLogger("tg_dc_bridge")


class StatsService:
    def build_dc_stats(self, bot, accid, chat_id, is_private: bool, from_id: int) -> str:
        if is_private:
            bridges = database.get_all_bridges()
            if not bridges:
                return "📊 No bridges configured."
            lines = [f"📊 Bridge Statistics ({len(bridges)} bridge{'s' if len(bridges) != 1 else ''})\n"]
            for row in bridges:
                dc_cid, tg_cid, r_count = row if len(row) == 3 else (row[0], row[1], 0)
                m_count = database.get_bridge_message_count(dc_cid, tg_cid)
                try:
                    title = bot.rpc.get_basic_chat_info(accid, dc_cid).get("name", "Unknown Group")
                except Exception:
                    title = "Unknown Group"
                lines.append(f"• DC {dc_cid} ↔ TG {tg_cid} ({title}) — {m_count} 💬 {r_count} 🙂")
            return "\n".join(lines)
        else:
            tg_chats = database.get_tg_chats(chat_id)
            if not tg_chats:
                return "📊 This group is not bridged."
            lines = ["📊 Bridge Statistics\n"]
            for tg_cid in tg_chats:
                m_count = database.get_bridge_message_count(chat_id, tg_cid)
                try:
                    title = bot.rpc.get_basic_chat_info(accid, chat_id).get("name", "this group")
                except Exception:
                    title = "this group"
                r_count = database.get_bridge_reaction_count(chat_id, tg_cid)
                lines.append(f"• TG Group {tg_cid} ({title}) — {m_count} 💬 {r_count} 🙂")
            return "\n".join(lines)

    async def build_tg_stats_text(self, update, user_id: int) -> str:
        chat = update.effective_chat
        user = update.effective_user

        if chat.type == "private":
            admin_tg_id = database.get_config("admin_tg_id")
            if admin_tg_id:
                if database.is_owner(user.id):
                    bridges = database.get_all_bridges()
                elif database.is_admin(user.id):
                    bridges = database.get_bridges_by_creator(user.id)
                else:
                    return None
            else:
                bridges = database.get_all_bridges()

            if not bridges:
                return "📊 No bridges configured."

            lines = [f"📊 <b>Bridge Statistics</b> ({len(bridges)} bridge{'s' if len(bridges) != 1 else ''})\n"]
            for row in bridges:
                dc_cid, tg_cid, r_count = row if len(row) == 3 else (row[0], row[1], 0)
                m_count = database.get_bridge_message_count(dc_cid, tg_cid)
                try:
                    chat_info = app_ctx.dc_bot.rpc.get_basic_chat_info(app_ctx.dc_accid, dc_cid)
                    title = chat_info.get("name", "Unknown Group")
                    contacts = app_ctx.dc_bot.rpc.get_chat_contacts(app_ctx.dc_accid, dc_cid)
                    sub_count = len(contacts) - 1 if contacts else 0
                except Exception:
                    title = "Unknown Group"
                    sub_count = "?"
                lines.append(f"• DC <code>{dc_cid}</code> ↔ TG <code>{tg_cid}</code> ({html.escape(title)}) — {sub_count} 👤 {m_count} 💬 {r_count} 🙂")
            return "\n".join(lines)
        else:
            admin_tg_id = database.get_config("admin_tg_id")
            if admin_tg_id:
                is_privileged = database.is_owner(user.id)
                if not is_privileged and database.is_admin(user.id):
                    creator = database.get_bridge_creator_by_tg(chat.id)
                    is_privileged = (creator is not None and creator == user.id)
                if not is_privileged:
                    try:
                        member = await chat.get_member(user.id)
                        if member.status not in ("administrator", "creator"):
                            return None
                    except Exception:
                        pass

            dc_chats = database.get_dc_chats(chat.id)
            if not dc_chats:
                return "📊 This group is not bridged."

            lines = ["📊 <b>Bridge Statistics</b>\n"]
            for dc_cid in dc_chats:
                m_count = database.get_bridge_message_count(dc_cid, chat.id)
                r_count = database.get_bridge_reaction_count(dc_cid, chat.id)
                try:
                    title = app_ctx.dc_bot.rpc.get_basic_chat_info(app_ctx.dc_accid, dc_cid).get("name", "this group")
                except Exception:
                    title = "this group"
                lines.append(f"• DC <code>{dc_cid}</code> ({html.escape(title)}) — {m_count} 💬 {r_count} 🙂")
            return "\n".join(lines)


stats_service = StatsService()
