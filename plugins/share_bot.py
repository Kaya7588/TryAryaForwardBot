"""
Share Bot — Delivery Agent
==========================
Handles deep-link delivery of batched episodes to users.
Features: Multi-channel FSub (up to 6), join-request mode, global auto-delete, content protection.
"""
import logging
import asyncio
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import UserNotParticipant
from database import db
from config import Config

logger = logging.getLogger(__name__)

share_client = None  # global reference

# ── Helpers ──────────────────────────────────────────────────────────────────

async def delete_later(client, chat_id, msg_ids: list, notice_id: int, delay_secs: int):
    """Delete delivered files after delay_secs seconds."""
    await asyncio.sleep(delay_secs)
    for mid in msg_ids:
        try:
            await client.delete_messages(chat_id, mid)
        except Exception:
            pass
    try:
        await client.delete_messages(chat_id, notice_id)
    except Exception:
        pass


async def check_all_subscriptions(client, user_id: int, fsub_channels: list) -> list:
    """Returns list of channel dicts the user has NOT joined."""
    not_joined = []
    for ch in fsub_channels:
        chat_id = ch.get('chat_id')
        if not chat_id:
            continue
        try:
            member = await client.get_chat_member(int(chat_id), user_id)
            from pyrogram import enums
            if member.status in (enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED):
                not_joined.append(ch)
        except UserNotParticipant:
            not_joined.append(ch)
        except Exception:
            pass  # don't block delivery on lookup errors
    return not_joined


async def start_share_bot(token: str = None):
    """Start the Share Bot client. If token is None, loads from DB."""
    global share_client
    if not token:
        token = await db.get_share_bot_token()
    if not token:
        logger.warning("Share Bot token not set — skipping startup.")
        return
    if share_client:
        try:
            await share_client.stop()
        except Exception:
            pass
    share_client = Client(
        name="share_bot_session",
        bot_token=token,
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        in_memory=True,
    )
    # Register handlers BEFORE starting so Pyrogram picks them up
    register_share_handlers(share_client)
    await share_client.start()
    logger.info(f"Share Bot started: @{share_client.me.username}")


# ── Handlers ─────────────────────────────────────────────────────────────────

def register_share_handlers(app: Client):
    """Attach all /start handlers to the given Client instance."""

    @app.on_message(filters.private & filters.command("start"))
    async def process_start(client, message):
        user_id = message.from_user.id
        args    = message.command

        # Plain /start — welcome message
        if len(args) < 2:
            bot_name = client.me.first_name
            await message.reply_text(
                f"<b>👋 Welcome to {bot_name}!</b>\n\n"
                "I'm a secure file-delivery bot. Click a link button from the channel "
                "to receive your episodes directly here in DM.\n\n"
                "<i>If you ended up here by mistake, go back to the channel and click a button.</i>"
            )
            return

        uuid_str = args[1].strip()

        # 1. Fetch link record
        link_data = await db.get_share_link(uuid_str)
        if not link_data:
            await message.reply_text(
                "<b>❌ Link Expired or Invalid</b>\n\n"
                "This batch link no longer exists. Go back to the channel and click the latest link."
            )
            return

        msg_ids      = link_data.get('message_ids', [])
        source_chat  = link_data.get('source_chat')
        protect_flag = link_data.get('protect', True)

        if not msg_ids or not source_chat:
            await message.reply_text("<b>❌ Database Error:</b> Missing file references.")
            return

        # 2. Multi-channel Force-Subscribe check
        fsub_channels = await db.get_share_fsub_channels()
        if fsub_channels:
            not_joined = await check_all_subscriptions(client, user_id, fsub_channels)
            if not_joined:
                buttons = []
                for ch in not_joined:
                    label = "📨 Request to Join" if ch.get('join_request') else "📢 Join Channel"
                    invite = ch.get('invite_link', '')
                    if invite:
                        buttons.append([InlineKeyboardButton(label, url=invite)])
                buttons.append([
                    InlineKeyboardButton(
                        "✅ I've Joined — Try Again!",
                        url=f"https://t.me/{client.me.username}?start={uuid_str}"
                    )
                ])
                ch_list = "\n".join(
                    f"• {ch.get('title', 'Channel')} "
                    + ("(send a join request)" if ch.get('join_request') else "")
                    for ch in not_joined
                )
                await message.reply_text(
                    f"<b>🔒 Join Required!</b>\n\n"
                    f"You must join {len(not_joined)} channel(s) to access this content:\n\n"
                    f"{ch_list}\n\n"
                    "<i>After joining, click <b>Try Again</b> below.</i>",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
                return

        # 3. Inject DB channel peer into Share Bot's in-memory cache (access_hash from MongoDB)
        access_hash = link_data.get('access_hash', 0)
        if access_hash and source_chat < 0:
            try:
                from pyrogram.raw.types import InputPeerChannel as _IPC
                raw_channel_id = abs(source_chat) - 1000000000000
                await client.storage.update_peers([
                    (raw_channel_id, access_hash, "channel", None, None)
                ])
            except Exception as peer_err:
                logger.warning(f"Peer injection failed (non-fatal): {peer_err}")

        # 4. Read auto-delete from GLOBAL config (not per-link) so setting changes apply everywhere
        auto_delete_mins = await db.get_share_autodelete_global()

        # 5. Deliver files one by one with copy_message (singular — guaranteed in Pyrogram 2.x)
        sts = await message.reply_text("<i>⏳ Fetching your files securely, please wait...</i>")

        sent_ids   = []
        fail_count = 0
        try:
            for msg_id in msg_ids:
                try:
                    sent = await client.copy_message(
                        chat_id=user_id,
                        from_chat_id=source_chat,
                        message_id=msg_id,
                        protect_content=protect_flag
                    )
                    sent_ids.append(sent.id)
                except Exception as copy_err:
                    logger.warning(f"Failed to copy msg {msg_id}: {copy_err}")
                    fail_count += 1

            total = len(sent_ids)
            if total == 0:
                await sts.edit_text(
                    "<b>❌ Delivery Failed</b>\n\n"
                    "Could not copy any files. "
                    "Ensure the Share Bot is an <b>admin</b> in the Database Channel."
                )
                return

            fail_note = f"\n<i>({fail_count} file(s) could not be copied)</i>" if fail_count else ""

            if auto_delete_mins > 0:
                hrs   = auto_delete_mins // 60
                mins_r= auto_delete_mins % 60
                del_str = (f"{hrs}h {mins_r}m" if hrs and mins_r
                           else (f"{hrs}h" if hrs else f"{auto_delete_mins}m"))
                notice = await sts.edit_text(
                    f"<b>✅ {total} file(s) delivered!</b>\n\n"
                    f"<i>⚠️ These files will <b>auto-delete</b> in <b>{del_str}</b>. "
                    f"Save them before they disappear!</i>{fail_note}"
                )
                asyncio.create_task(
                    delete_later(client, user_id, sent_ids, notice.id, auto_delete_mins * 60)
                )
            else:
                await sts.edit_text(
                    f"<b>✅ {total} file(s) delivered!</b>{fail_note}"
                )

        except Exception as e:
            await sts.edit_text(
                f"<b>❌ Delivery Failed</b>\n\n<code>{e}</code>\n\n"
                f"<i>The Share Bot must be an admin in the Database Channel to deliver files.</i>"
            )
