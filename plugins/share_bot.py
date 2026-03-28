import asyncio
import logging
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import UserNotParticipant
from database import db
from config import Config

logger = logging.getLogger(__name__)

# Global Share Bot Client Instance
share_client = None

async def is_subscribed(client, user_id):
    if not Config.FSUB_ID:
        return True
    try:
        user = await client.get_chat_member(Config.FSUB_ID, user_id)
        if user.status in [enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.KICKED]:
            return False
        return True
    except UserNotParticipant:
        return False
    except Exception as e:
        logger.error(f"FSub check error in Share Bot: {e}")
        return True

async def delete_later(client, chat_id, msg_ids, text_msg_id, secs: int):
    await asyncio.sleep(secs)
    try:
        # Delete the delivered files
        await client.delete_messages(chat_id, msg_ids)
        # Delete the indicator text message
        await client.delete_messages(chat_id, text_msg_id)
    except Exception as e:
        logger.error(f"Auto-delete failed: {e}")

async def process_start(client, message):
    user_id = message.from_user.id
    if len(message.command) < 2:
        return await message.reply_text("<b>Hello! 👋</b>\n\nI am the secure delivery agent for this service.\nSend me a valid URL to receive your files.")

    uuid_str = message.command[1]
    
    # 1. Fetch Link from DB (Do this first so we know if it's protecting/auto-deleting)
    link_data = await db.get_share_link(uuid_str)
    if not link_data:
        return await message.reply_text("<b>❌ Link Expired or Invalid.</b>\n\nThe batch has been deleted or never existed.")

    msg_ids = link_data.get('message_ids', [])
    source_chat = link_data.get('source_chat')
    protect_flag = link_data.get('protect', True)
    auto_delete_mins = link_data.get('auto_delete', 0)

    if not msg_ids or not source_chat:
        return await message.reply_text("<b>❌ Database error: Missing file references.</b>")

    # 2. Check FSub (Force Sub)
    if Config.FSUB_ID:
        is_sub = await is_subscribed(client, user_id)
        if not is_sub:
            try:
                invite_link = await client.export_chat_invite_link(Config.FSUB_ID)
                btn = [[InlineKeyboardButton("Join Channel 📢", url=invite_link)],
                       [InlineKeyboardButton("Try Again 🔄", url=f"https://t.me/{client.me.username}?start={uuid_str}")]]
                return await message.reply_text(
                    "<b>🔒 Access Denied!</b>\n\nYou must join our backup channel to receive these files.",
                    reply_markup=InlineKeyboardMarkup(btn)
                )
            except Exception as e:
                logger.error(f"Failed to generate FSub link: {e}")

    # 3. Deliver Messages
    sts = await message.reply_text("<i>⏳ Fetching files securely...</i>")
    
    try:
        # Delivery Agent directly copies the files!
        sent_msgs = await client.copy_messages(
            chat_id=user_id,
            from_chat_id=source_chat,
            message_ids=msg_ids,
            protect_content=protect_flag
        )
        
        # Prepare success text
        del_note = f"\n\n<i>⏱ These files will auto-delete in {auto_delete_mins} minutes.</i>" if auto_delete_mins > 0 else ""
        text_msg = await sts.edit_text(f"<b>✅ Successfully delivered {len(sent_msgs)} files!</b>{del_note}")
        
        # Schedule Auto-Delete if configured
        if auto_delete_mins > 0:
            sent_ids = [m.id for m in sent_msgs]
            asyncio.create_task(delete_later(client, user_id, sent_ids, text_msg.id, auto_delete_mins * 60))
            
    except Exception as e:
        await sts.edit_text(f"<b>❌ Error delivering files:</b>\n<code>{e}</code>\n\n(Make sure this File-Sharing bot is an admin in the hidden Database Channel!)")


async def start_share_bot(token=None):
    global share_client
    if share_client:
        try:
            await share_client.stop()
        except: pass
        share_client = None

    if not token:
        token = await db.get_share_bot_token()
    
    if not token:
        logger.info("Share Bot token not set. Skipping File Sharing Bot startup.")
        return

    # Aggressively clean the token of any accidental clipboard artifacts
    token = token.strip().replace(" ", "").replace("\n", "").replace("\r", "")
    tk_masked = f"{token[:10]}...{token[-4:]}" if len(token) > 15 else "INVALID_LENGTH"
    
    logger.info(f"Starting Secondary Share Bot [Token: {tk_masked}]...")
    
    import uuid
    dynamic_name = f"share_bot_mem_{uuid.uuid4().hex[:8]}"
    
    try:
        share_client = Client(
            name=dynamic_name,
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            bot_token=token,
            in_memory=True
        )

        # Register Handlers locally
        @share_client.on_message(filters.command("start") & filters.private)
        async def on_start(c, m):
            await process_start(c, m)

        await share_client.start()
        logger.info("✅ Secondary Share Bot successfully started and listening.")
    except Exception as e:
        logger.error(f"❌ Failed to start Share Bot: {e}")
        share_client = None
