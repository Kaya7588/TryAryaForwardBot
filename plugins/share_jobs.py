"""
Share Batch Links Automator
===========================
Generates File-Sharing deep links from a hidden database channel
and automatically posts the grouped batch buttons into a Public Channel.
"""
import uuid
import math
import asyncio
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
from database import db
from plugins.test import CLIENT
from plugins.jobs import _ask

_CLIENT = CLIENT()

new_share_job = {}

async def _create_share_flow(bot, user_id):
    try:
        new_share_job[user_id] = {}
        bots = await db.get_bots(user_id)
        if not bots:
            return await bot.send_message(user_id, "<b>❌ No accounts. Add one in /settings → Accounts first.</b>")
            
        kb = []
        share_token = await db.get_share_bot_token()
        if share_token:
            kb.append(["🤖 (Dedicated) Share Bot"])
            
        for b in bots:
            typ = "🤖" if b.get('is_bot', True) else "👤"
            kb.append([f"{typ} {b['name']}"])
            
        kb.append(["❌ Cancel"])
        
        msg = await _ask(bot, user_id, 
            "<b>❪ SHARE LINKS: SELECT ACCOUNT ❫</b>\n\nChoose the account that has Admin access to both the Source Database Channel and Target Channel:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True)
        )
        if not msg.text or msg.text == "/cancel" or "Cancel" in msg.text:
            return await bot.send_message(user_id, "<b>Cancelled.</b>", reply_markup=ReplyKeyboardRemove())
            
        if "Share Bot" in msg.text:
            new_share_job[user_id]['bot_id'] = "SHAREBOT"
        else:
            sel_name = msg.text.split(" ", 1)[1] if " " in msg.text else msg.text
            acc = next((a for a in bots if a["name"] == sel_name), None)
            if not acc:
                return await bot.send_message(user_id, "<b>❌ Account not found.</b>", reply_markup=ReplyKeyboardRemove())
            new_share_job[user_id]['bot_id'] = acc['id']

        chans = await db.get_user_channels(user_id)
        if not chans:
            return await bot.send_message(user_id, "<b>❌ No channels added in /settings.</b>", reply_markup=ReplyKeyboardRemove())
            
        ch_kb = [[f"📢 {ch['title']}"] for ch in chans]
        ch_kb.append(["❌ Cancel"])
        msg = await _ask(bot, user_id, 
            "<b>❪ STEP 2: SOURCE DATABASE ❫</b>\n\nWhere are the files stored securely?", 
            reply_markup=ReplyKeyboardMarkup(ch_kb, resize_keyboard=True, one_time_keyboard=True)
        )
        if not msg.text or msg.text == "/cancel" or "Cancel" in msg.text:
            return await bot.send_message(user_id, "<b>Cancelled.</b>", reply_markup=ReplyKeyboardRemove())
            
        title = msg.text.replace("📢 ", "").strip()
        ch = next((c for c in chans if c["title"] == title), None)
        if not ch:
            return await bot.send_message(user_id, "<b>❌ Source Channel not found.</b>", reply_markup=ReplyKeyboardRemove())
        new_share_job[user_id]['source'] = int(ch['chat_id'])
        
        msg = await _ask(bot, user_id, 
            "<b>❪ STEP 3: TARGET PUBLIC CHANNEL ❫</b>\n\nWhere should I post the Share Links?", 
            reply_markup=ReplyKeyboardMarkup(ch_kb, resize_keyboard=True, one_time_keyboard=True)
        )
        if not msg.text or msg.text == "/cancel" or "Cancel" in msg.text:
            return await bot.send_message(user_id, "<b>Cancelled.</b>", reply_markup=ReplyKeyboardRemove())
            
        title = msg.text.replace("📢 ", "").strip()
        ch = next((c for c in chans if c["title"] == title), None)
        if not ch:
            return await bot.send_message(user_id, "<b>❌ Target Channel not found.</b>", reply_markup=ReplyKeyboardRemove())
        new_share_job[user_id]['target'] = int(ch['chat_id'])

        markup = ReplyKeyboardMarkup([[KeyboardButton("/cancel")]], resize_keyboard=True, one_time_keyboard=True)
            
        def parse_id(text: str) -> int:
            text = text.strip().rstrip('/')
            if text.isdigit(): return int(text)
            if "t.me/" in text:
                parts = text.split('/')
                if parts[-1].isdigit(): return int(parts[-1])
            raise ValueError("Invalid Message ID or Link")
            
        msg_story = await _ask(bot, user_id, 
            "<b>❪ STEP 4: STORY NAME ❫</b>\n\nEnter the clean name of the Series/Story (e.g. <code>TDMB</code>):", 
            reply_markup=markup
        )
        if msg_story.text == "/cancel": return await bot.send_message(user_id, "Cancelled.", reply_markup=ReplyKeyboardRemove())
        new_share_job[user_id]['story'] = msg_story.text.strip()
        
        msg_start = await _ask(bot, user_id, 
            "<b>❪ STEP 5: START MESSAGE ❫</b>\n\nForward the first message, send its Message ID, or paste its Link (e.g. <code>https://t.me/c/123/456</code>):", 
            reply_markup=markup
        )
        if msg_start.text == "/cancel": return await bot.send_message(user_id, "Cancelled.", reply_markup=ReplyKeyboardRemove())
        start_id = parse_id(msg_start.text)
        new_share_job[user_id]['start_id'] = start_id
        
        msg_end = await _ask(bot, user_id, 
            "<b>❪ STEP 6: LAST MESSAGE ❫</b>\n\nForward the last message, send its Msg ID, or paste its Link:", 
            reply_markup=markup
        )
        if msg_end.text == "/cancel": return await bot.send_message(user_id, "Cancelled.", reply_markup=ReplyKeyboardRemove())
        end_id = parse_id(msg_end.text)
        new_share_job[user_id]['end_id'] = end_id
        
        if start_id > end_id:
            start_id, end_id = end_id, start_id
            new_share_job[user_id]['start_id'] = start_id
            new_share_job[user_id]['end_id'] = end_id
            
        msg_batch = await _ask(bot, user_id, 
            "<b>❪ STEP 7: EPISODES PER LINK ❫</b>\n\nHow many files should be grouped in one link button?\nExample: <code>20</code>", 
            reply_markup=markup
        )
        if msg_batch.text == "/cancel": return await bot.send_message(user_id, "Cancelled.", reply_markup=ReplyKeyboardRemove())
        
        batch_size = int(msg_batch.text.strip())
        if batch_size < 1: batch_size = 20
        new_share_job[user_id]['batch_size'] = batch_size
        
        sj = new_share_job[user_id]
        total_msgs = (sj['end_id'] - sj['start_id']) + 1
        total_links = math.ceil(total_msgs / sj['batch_size'])
        total_posts = math.ceil(total_links / 10)
        
        btn = [[InlineKeyboardButton("🚀 Generate & Group Links", callback_data="sl#build")]]
        await bot.send_message(
            user_id,
            f"<b>📋 CONFIRM SHARE BATCH</b>\n\n"
            f"<b>Story Name:</b> {sj['story']}\n"
            f"<b>Source ID:</b> <code>{sj['source']}</code>\n"
            f"<b>Target ID:</b> <code>{sj['target']}</code>\n"
            f"<b>Range:</b> {sj['start_id']} to {sj['end_id']} ({total_msgs} files)\n"
            f"<b>Batch Size:</b> {sj['batch_size']} files per link\n"
            f"<b>Total Buttons to create:</b> {total_links}\n"
            f"<b>Total Grouped Posts (10 btns each):</b> {total_posts}\n",
            reply_markup=InlineKeyboardMarkup(btn)
        )
    except Exception as e:
        await bot.send_message(user_id, f"<b>Error during link setup:</b> {e}", reply_markup=ReplyKeyboardRemove())

@Client.on_callback_query(filters.regex(r'^sl#'))
async def sl_callback(bot, query):
    user_id = query.from_user.id
    data = query.data.split('#')
    cmd = data[1]

    if cmd == "start":
        await query.message.delete()
        asyncio.create_task(_create_share_flow(bot, user_id))

    elif cmd == "build":
        sj = new_share_job.get(user_id)
        if not sj: return await query.answer("Expired session.", show_alert=True)
        
        # Check token aggressively
        token = await db.get_share_bot_token()
        if not token:
            return await query.answer("❌ You must set the Share Bot Token in /settings first!", show_alert=True)
        
        import plugins.share_bot as share_mod
        if not share_mod.share_client or not getattr(share_mod.share_client, 'is_connected', False):
            try:
                await share_mod.start_share_bot(token)
            except Exception: pass
            
        if not share_mod.share_client:
            return await query.answer("❌ Share Bot failed to start. Review terminal logs.", show_alert=True)
            
        bot_usr = share_mod.share_client.me.username if share_mod.share_client.me else "ShareBot"
        
        if sj['bot_id'] == "SHAREBOT":
            worker = share_mod.share_client
        else:
            from plugins.test import start_clone_bot
            worker = await start_clone_bot(_CLIENT, sj['bot_id'])
            
        if not worker:
            return await query.message.edit_text("❌ Failed to start worker account.")

        await query.message.edit_text("<i>⏳ Scanning database and generating mathematically grouped batches...</i>")
        
        try:
            current_id = sj['start_id']
            end_ep = sj['end_id']
            chunk_size = sj['batch_size']
            
            # Phase 1: Scan and create raw buttons
            raw_buttons = []
            
            while current_id <= end_ep:
                chunk_end = min(current_id + chunk_size - 1, end_ep)
                msg_ids = list(range(current_id, chunk_end + 1))
                
                valid_ids = []
                messages = await worker.get_messages(sj['source'], msg_ids)
                for m in messages:
                    if m.empty or m.service: continue
                    valid_ids.append(m.id)
                
                if valid_ids:
                    uuid_str = str(uuid.uuid4()).replace('-', '')[:16]
                    await db.save_share_link(uuid_str, valid_ids, sj['source'])
                    
                    url = f"https://t.me/{bot_usr}?start={uuid_str}"
                    # Format e.g., "1–20"
                    btn_text = f"{valid_ids[0]} - {valid_ids[-1]}" if len(valid_ids) > 1 else str(valid_ids[0])
                    btn = InlineKeyboardButton(btn_text, url=url)
                    
                    raw_buttons.append({
                        "btn": btn,
                        "start_id": valid_ids[0],
                        "end_id": valid_ids[-1]
                    })
                    
                current_id = chunk_end + 1
                await asyncio.sleep(1) # Floodwaits
                
            # Phase 2: Group and Post in batches of 10
            post_count = 0
            for i in range(0, len(raw_buttons), 10):
                chunk_btns = raw_buttons[i:i+10]
                
                first_ep = chunk_btns[0]["start_id"]
                last_ep = chunk_btns[-1]["end_id"]
                
                # Title uses Story Name + Range
                txt = f"<b>{sj['story'].upper()} EPS {first_ep} - {last_ep}</b>"
                
                keyboard = []
                # 2 buttons per row
                for j in range(0, len(chunk_btns), 2):
                    row = [cb["btn"] for cb in chunk_btns[j:j+2]]
                    keyboard.append(row)
                    
                # Permanent footer row
                keyboard.append([
                    InlineKeyboardButton("Tutorial 🎥", url="https://t.me/StoriesLinkopningguide"),
                    InlineKeyboardButton("Issue ?", url="https://t.me/+EAc-6v1bmZ1iMDBl")
                ])
                
                await worker.send_message(
                    chat_id=sj['target'],
                    text=txt,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                post_count += 1
                await asyncio.sleep(1)
                
            await query.message.edit_text(f"<b>✅ Completed!</b>\n\nGenerated ({post_count}) structured posts containing {len(raw_buttons)} protected links mapped to @{bot_usr}.")
            
        except Exception as e:
            await query.message.reply_text(f"<b>Error during linking:</b>\n<code>{e}</code>")
        finally:
            del new_share_job[user_id]
