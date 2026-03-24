import re
import asyncio
from .utils import STS
from database import db
from config import temp
from translation import Translation
from plugins.lang import t, _tx
from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait
from pyrogram.errors.exceptions.not_acceptable_406 import ChannelPrivate as PrivateChat
from pyrogram.errors.exceptions.bad_request_400 import ChannelInvalid, ChatAdminRequired, UsernameInvalid, UsernameNotModified, ChannelPrivate
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
 
#===================Run Function===================#

@Client.on_message(filters.private & filters.command(["fwd", "forward"]))
async def run(bot, message):
    buttons = []
    btn_data = {}
    user_id = message.from_user.id
    _bot = await db.get_bot(user_id)
    if not _bot:
        return await message.reply(await t(user_id, 'no_bot'))
    channels = await db.get_user_channels(user_id)
    if not channels:
        return await message.reply_text(await t(user_id, 'no_channel'))
    if len(channels) > 1:
        for channel in channels:
            buttons.append([KeyboardButton(f"{channel['title']}")])
            btn_data[channel['title']] = channel['chat_id']
        buttons.append([KeyboardButton("cancel")])
        _toid = await bot.ask(message.chat.id, await t(user_id, 'TO_MSG'), reply_markup=ReplyKeyboardMarkup(buttons, one_time_keyboard=True, resize_keyboard=True))
        if _toid.text.startswith(('/', 'cancel')):
            return await message.reply_text(await t(user_id, 'CANCEL'), reply_markup=ReplyKeyboardRemove())
        to_title = _toid.text
        toid = btn_data.get(to_title)
        if not toid:
            return await message.reply_text("wrong channel choosen !", reply_markup=ReplyKeyboardRemove())
    else:
        toid = channels[0]['chat_id']
        to_title = channels[0]['title']
    fromid = await bot.ask(message.chat.id, await t(user_id, 'FROM_MSG'), reply_markup=ReplyKeyboardRemove())
    if fromid.text and fromid.text.startswith('/'):
        await message.reply(await t(user_id, 'CANCEL'))
        return

    continuous = False

    # Handle "Saved Messages" input
    if fromid.text and fromid.text.lower() in ["me", "saved"]:
        if _bot.get('is_bot'):
            return await message.reply("<b>You cannot forward from Saved Messages using a Bot. Please add a Userbot session via /settings to use this feature.</b>")

        chat_id = "me"
        title = "Saved Messages"

        # Ask for mode: Batch vs Live
        mode_btn = ReplyKeyboardMarkup([
            [KeyboardButton("Batch"), KeyboardButton("Live")]
        ], resize_keyboard=True, one_time_keyboard=True)
        mode_msg = await bot.ask(message.chat.id, await t(user_id, 'SAVED_MSG_MODE'), reply_markup=mode_btn)
        if mode_msg.text.startswith('/'):
            await message.reply(await t(user_id, 'CANCEL'), reply_markup=ReplyKeyboardRemove())
            return

        if "live" in mode_msg.text.lower() or "2" in mode_msg.text:
            continuous = True
            last_msg_id = 1000000
        else:
            limit_msg = await bot.ask(message.chat.id, await t(user_id, 'SAVED_MSG_LIMIT'), reply_markup=ReplyKeyboardRemove())
            if limit_msg.text.startswith('/'):
                await message.reply(await t(user_id, 'CANCEL'))
                return

            if limit_msg.text.lower() == "all":
                 last_msg_id = 0 # 0 usually means no limit in some contexts, but let's use a very high number if iter logic relies on it?
                 # iter_messages: if limit > 0, it iterates until limit.
                 # If we want ALL, we should use a high number or verify how 0 is handled.
                 # In test.py: new_diff = min(200, limit - current).
                 # If limit is 0, 0-0 = 0. new_diff <= 0. Return.
                 # So we need a high number.
                 last_msg_id = 10000000
            elif not limit_msg.text.isdigit():
                 await message.reply("Invalid number.")
                 return
            else:
                 last_msg_id = int(limit_msg.text) # Using last_msg_id as limit/count

    elif fromid.text and not fromid.forward_date:
        regex = re.compile(r"(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$")
        match = regex.match(fromid.text.replace("?single", ""))
        if match:
            chat_id = match.group(4)
            last_msg_id = int(match.group(5))
            if chat_id.isnumeric():
                chat_id  = int(("-100" + chat_id))
        else:
            chat_id = fromid.text.strip()
            if chat_id.lstrip('-').isdigit():
                chat_id = int(chat_id)
            mode_btn = ReplyKeyboardMarkup([
                [KeyboardButton("Batch"), KeyboardButton("Live")]
            ], resize_keyboard=True, one_time_keyboard=True)
            mode_msg = await bot.ask(message.chat.id, await t(user_id, 'SAVED_MSG_MODE'), reply_markup=mode_btn)
            if mode_msg.text.startswith('/'):
                await message.reply(await t(user_id, 'CANCEL'), reply_markup=ReplyKeyboardRemove())
                return
            if "live" in mode_msg.text.lower() or "2" in mode_msg.text:
                continuous = True
                last_msg_id = 10000000
            else:
                limit_msg = await bot.ask(message.chat.id, await t(user_id, 'SAVED_MSG_LIMIT'), reply_markup=ReplyKeyboardRemove())
                if limit_msg.text.startswith('/'):
                    await message.reply(await t(user_id, 'CANCEL'))
                    return
                if limit_msg.text.lower() == "all":
                     last_msg_id = 10000000
                elif not limit_msg.text.isdigit():
                     await message.reply("Invalid number.")
                     return
                else:
                     last_msg_id = int(limit_msg.text)
    elif fromid.forward_from_chat and fromid.forward_from_chat.type in [enums.ChatType.CHANNEL]:
        last_msg_id = fromid.forward_from_message_id
        chat_id = fromid.forward_from_chat.username or fromid.forward_from_chat.id
        if last_msg_id == None:
           return await message.reply_text("**This may be a forwarded message from a group and sended by anonymous admin. instead of this please send last message link from group**")
    else:
        await message.reply_text("**invalid !**")
        return 

    if chat_id != "me":
        try:
            title = (await bot.get_chat(chat_id)).title
      #  except ChannelInvalid:
            #return await fromid.reply("**Given source chat is copyrighted channel/group. you can't forward messages from there**")
        except (PrivateChat, ChannelPrivate, ChannelInvalid):
            title = "private" if fromid.text else fromid.forward_from_chat.title
        except (UsernameInvalid, UsernameNotModified):
            return await message.reply('Invalid Link specified.')
        except Exception as e:
            # Main bot might not have access to the user/bot chat, but the userbot might.
            # We bypass the error so the userbot can try during the actual forwarding.
            title = str(chat_id)

    # ----- NEW EXPLICIT ACCOUNT SELECTION LOGIC -----
    accounts = await db.get_bots(user_id)
    if not accounts:
        return await message.reply("You haven't added any accounts yet. Go to /settings -> Accounts.")
        
    account_buttons = []
    for acc in accounts:
        acc_type = "Bot" if acc.get('is_bot', True) else "Userbot"
        acc_name = acc.get('username') or acc.get('name', 'Unknown')
        btn_text = f"{acc_type}: {acc_name} [{acc['id']}]"
        account_buttons.append([KeyboardButton(btn_text)])
        
    acc_markup = ReplyKeyboardMarkup(account_buttons, resize_keyboard=True, one_time_keyboard=True)
    acc_msg = await bot.ask(message.chat.id, await t(user_id, 'choose_account'), reply_markup=acc_markup)

    if acc_msg.text.startswith('/'):
        await message.reply(await t(user_id, 'CANCEL'), reply_markup=ReplyKeyboardRemove())
        return

    # Extract bot_id from the button text format "Type: Name [12345678]"
    selected_bot_id = None
    if "[" in acc_msg.text and "]" in acc_msg.text:
       try:
           selected_bot_id = int(acc_msg.text.split('[')[-1].split(']')[0])
       except ValueError:
           pass
           
    if not selected_bot_id:
        await message.reply("Invalid account selection.", reply_markup=ReplyKeyboardRemove())
        return
    # ------------------------------------------------

    order_btn = ReplyKeyboardMarkup([
        [KeyboardButton("Old to New"), KeyboardButton("New to Old")]
    ], resize_keyboard=True, one_time_keyboard=True)
    order_msg = await bot.ask(message.chat.id, await t(user_id, 'choose_order'), reply_markup=order_btn)
    if order_msg.text.startswith('/'):
        await message.reply(await t(user_id, 'CANCEL'), reply_markup=ReplyKeyboardRemove())
        return

    reverse_order = True if "New to Old" in order_msg.text else False

    # ── Smart Order toggle ──────────────────────────────────────────────────
    smart_btn = ReplyKeyboardMarkup([
        [KeyboardButton("✅ Smart Order ON"), KeyboardButton("❌ Smart Order OFF")]
    ], resize_keyboard=True, one_time_keyboard=True)
    smart_msg = await bot.ask(
        message.chat.id,
        "<b>🧠 Smart Order</b>\n\nShould the bot automatically fix any out-of-order messages from the source channel?\n\n"
        "• <b>ON</b> — Bot collects messages in batches of 10 and sorts them by ID before sending (fixes minor source-level mismatches like 42,43,45,44 → 42,43,44,45)\n"
        "• <b>OFF</b> — Messages are forwarded exactly as received (faster)\n\n"
        "<i>Recommended: ON if your source channel sometimes has files out of order.</i>",
        reply_markup=smart_btn
    )
    if smart_msg.text.startswith('/'):
        await message.reply(await t(user_id, 'CANCEL'), reply_markup=ReplyKeyboardRemove())
        return
    smart_order = "OFF" not in smart_msg.text  # True = ON

    skipno = await bot.ask(message.chat.id, await t(user_id, 'SKIP_MSG'), reply_markup=ReplyKeyboardRemove())
    if skipno.text.startswith('/'):
        await message.reply(await t(user_id, 'CANCEL'))
        return
    forward_id = f"{user_id}-{skipno.id}"
    buttons = [[
        InlineKeyboardButton('Yes', callback_data=f"start_public_{forward_id}"),
        InlineKeyboardButton('No', callback_data="close_btn")
    ]]
    reply_markup = InlineKeyboardMarkup(buttons)

    # Fetch the selected account and user configs for the detailed summary
    selected_acc = await db.get_bot(user_id, selected_bot_id)
    if not selected_acc:
        selected_acc = _bot
    acc_is_bot     = selected_acc.get('is_bot', True)
    acc_type_label = "🤖 Bot" if acc_is_bot else "👤 Userbot"
    acc_name       = selected_acc.get('name', 'Unknown')
    acc_username   = selected_acc.get('username', '')

    configs        = await db.get_configs(user_id)
    active_filters = await db.get_filters(user_id)  # list of disabled types
    all_types      = ['text','audio','voice','video','photo','document','animation','sticker','poll']
    enabled_types  = [t for t in all_types if t not in active_filters]
    disabled_types = [t for t in all_types if t in active_filters]

    fwd_mode  = "🔄 Forward (tag on)" if configs.get('forward_tag') else "📋 Copy (no tag)"
    caption_m = "✂️ Removed" if configs.get('filters', {}).get('rm_caption') else "📝 Kept"
    dl_mode   = "⬇️ Download mode ON" if configs.get('download') else "📤 Direct copy"
    order_lbl = "🔽 New to Old" if reverse_order else "🔼 Old to New"
    mode_lbl  = "🔁 Live (continuous)" if continuous else "📦 Batch"
    skip_lbl  = skipno.text if skipno.text.isdigit() else "0"
    filter_str = (', '.join(f'❌{t}' for t in disabled_types) or '✅ All allowed')
    smart_lbl = "🧠 ON" if smart_order else "⚡ OFF (raw)"

    if acc_is_bot:
        hints = (
            f"<b>│</b> ⚠️ <b>{acc_name}</b> (@{acc_username}) must be <b>admin</b> in TARGET\n"
            f"<b>│</b> ⚠️ If SOURCE is private, bot must be <b>admin</b> there too\n"
            f"<b>└──────────────────────────────────</b>\n"
        )
    else:
        hints = (
            f"<b>│</b> ⚠️ Userbot <b>{acc_name}</b> must be a <b>member</b> of SOURCE\n"
            f"<b>│</b> ⚠️ Userbot must be <b>admin</b> in TARGET channel\n"
            f"<b>└──────────────────────────────────</b>\n"
        )

    # Calculate if this needs the SLOW MODE warning
    is_private_source = title == "private" or title == "Saved Messages" or str(title).lstrip('-').isdigit()
    needs_download = configs.get('download') or (is_private_source and not configs.get('forward_tag'))
    
    warning_box = ""
    if not acc_is_bot or needs_download or reverse_order:
        warning_box = (
            f"<b>┌─────❮ ⚠️ 𝐒𝐋𝐎𝐖 𝐌𝐎𝐃𝐄 𝐖𝐀𝐑𝐍𝐈𝐍𝐆 ❯─────</b>\n"
            f"<b>│</b> ⊸ Forwarding will be slow (Telegram restrictions)\n"
            f"<b>│</b> ⊸ Bot relies on parsing or downloading/re-uploading\n"
            f"<b>│</b> ⊸ High data usage & slower speeds expected. Be patient.\n"
            f"<b>└──────────────────────────────────</b>\n\n"
        )

    check_text = (
        f"<b>╭──────❰ ⚠️ 𝐃𝐎𝐔𝐁𝐋𝐄 𝐂𝐇𝐄𝐂𝐊 ❱──────╮</b>\n"
        f"<b>┃</b>\n"
        f"<b>┣⊸ ◈ 𝐀𝐂𝐂𝐎𝐔𝐍𝐓 ({acc_type_label}):</b> {acc_name}\n"
        f"<b>┣⊸ ◈ 𝐒𝐎𝐔𝐑𝐂𝐄  :</b> <code>{title}</code>\n"
        f"<b>┣⊸ ◈ 𝐓𝐀𝐑𝐆𝐄𝐓  :</b> <code>{to_title}</code>\n"
        f"<b>┣⊸ ◈ 𝐒𝐊𝐈𝐏    :</b> <code>{skip_lbl}</code>\n"
        f"<b>┃</b>\n"
        f"<b>┌──────❮ ⚙️ 𝐒𝐞𝐭𝐭𝐢𝐧𝐠𝐬 ❯────────────</b>\n"
        f"<b>│</b> ⊸ <b>Mode:</b> {mode_lbl}\n"
        f"<b>│</b> ⊸ <b>Order:</b> {order_lbl}\n"
        f"<b>│</b> ⊸ <b>Smart Order:</b> {smart_lbl}\n"
        f"<b>│</b> ⊸ <b>Status:</b> {fwd_mode}\n"
        f"<b>│</b> ⊸ <b>Caption:</b> {caption_m}\n"
        f"<b>│</b> ⊸ <b>Transfer:</b> {dl_mode}\n"
        f"<b>│</b> ⊸ <b>Filters:</b> {filter_str}\n"
        f"<b>└──────────────────────────────────</b>\n\n"
        f"{warning_box}"
        f"<b>┌──────❮ 💡 𝐑𝐞𝐦𝐢𝐧𝐝𝐞𝐫𝐬 ❯───────────</b>\n"
        f"{hints}\n"
        f"<b>╰─── 𝐈𝐟 𝐯𝐞𝐫𝐢𝐟𝐢𝐞𝐝, 𝐜𝐥𝐢𝐜𝐤 𝐘𝐞𝐬 𝐁𝐞𝐥𝐨𝐰 ───╯</b>"
    )

    await message.reply_text(
        text=check_text,
        disable_web_page_preview=True,
        reply_markup=reply_markup
    )
    STS(forward_id).store(chat_id, toid, int(skipno.text) if skipno.text.isdigit() else 0,
                          int(last_msg_id), continuous=continuous,
                          reverse_order=reverse_order, bot_id=selected_bot_id, smart_order=smart_order)


