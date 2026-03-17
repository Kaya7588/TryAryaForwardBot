import os
from config import Config

class Translation(object):
  START_TXT = """<b>╭──────❰ ✦ 𝐀𝐮𝐭𝐨 𝐅𝐨𝐫𝐰𝐚𝐫𝐝𝐞𝐫 ✦ ❱──────╮
┃
┣⊸ 𝐇𝐞𝐥𝐥𝐨 {}
┃
┣⊸ 🤖 Aryᴀ Bᴏᴛ [ ᴩᴏwᴇʀғᴜʟ Fᴏʀᴡᴀʀᴅ Tᴏᴏʟ ]
┃
┣⊸ <i>ɪ ᴄᴀɴ ғᴏʀᴡᴀʀᴅ ᴀʟʟ ᴍᴇssᴀɢᴇs ғʀᴏᴍ ᴏɴᴇ
┃  ᴄʜᴀɴɴᴇʟ ᴛᴏ ᴀɴᴏᴛʜᴇʀ ᴄʜᴀɴɴᴇʟ ᴡɪᴛʜ
┃  ᴍᴏʀᴇ ғᴇᴀᴛᴜʀᴇs.</i>
┃
╰────────────────────────────────╯</b>
"""


  HELP_TXT = """<b><u>🔆 HELP — Aryᴀ Bᴏᴛ</u></b>

<b>📌 Commands:</b>
<code>/start</code>  — Check if I'm alive
<code>/forward</code>  — Start batch forwarding
<code>/jobs</code>  — Manage Live Jobs (background forwarding)
<code>/cleanmsg</code>  — Bulk delete messages from chats
<code>/settings</code>  — Configure all settings
<code>/reset</code>  — Reset settings to default

<b>⚡ Features:</b>
<b>►</b> Forward from public channels — no admin needed
<b>►</b> Forward from private channels — via bot/userbot admin
<b>►</b> Multi-Account: up to 2 Bots + 2 Userbots
<b>►</b> Live Jobs — background tasks, run parallel to batch forwards
<b>►</b> New→Old &amp; Old→New forwarding order
<b>►</b> Filters — skip audio/video/photo/text/sticker/poll etc.
<b>►</b> Custom caption / remove caption / add buttons
<b>►</b> Skip duplicate messages
<b>►</b> Extension / Keyword / Size filters
<b>►</b> Download mode — bypasses forward restrictions
<b>►</b> Clean MSG — bulk delete from target channels
"""
  
  HOW_USE_TXT = """<b><u>📍 How to Use — Aryᴀ Bᴏᴛ</u></b>

<b>1️⃣ Add an Account</b>
  ‣ Go to /settings → ⚙️ Accounts
  ‣ Add a Bot (send its token) or a Userbot (send session string)
  ‣ You can add up to 2 Bots + 2 Userbots

<b>2️⃣ Add a Target Channel</b>
  ‣ Go to /settings → 📣 Channels
  ‣ Your Bot/Userbot must be <b>admin</b> in the target

<b>3️⃣ Configure Settings</b>
  ‣ <b>Filters</b> — choose what types of messages to skip
  ‣ <b>Caption</b> — custom caption or remove it
  ‣ <b>Forward Tag</b> — show or hide forwarded-from label
  ‣ <b>Download Mode</b> — re-upload files (bypasses restrictions)
  ‣ <b>Duplicate Skip</b> — avoid re-forwarding same content

<b>4️⃣ Batch Forward (/forward)</b>
  ‣ Choose account → select target → send source link/ID
  ‣ Choose order (Old→New / New→Old) → set skip count
  ‣ Verify DOUBLE CHECK → click Yes

<b>5️⃣ Live Jobs (/jobs)</b>
  ‣ Creates a <b>background job</b> that auto-forwards new messages
  ‣ Works alongside batch forwarding simultaneously
  ‣ Supports channels, groups, bot private chats, saved messages
  ‣ Respects your Filters settings
  ‣ Stop/Start/Delete any job anytime from /jobs

<b>6️⃣ Clean MSG (/cleanmsg)</b>
  ‣ Select account + target chat(s) + message type
  ‣ Bulk deletes messages in one go

<b>⚠️ Notes:</b>
  ‣ Bot account: needs admin in TARGET (and SOURCE if private)
  ‣ Userbot: needs membership in SOURCE + admin in TARGET
  ‣ For public channels, a normal Bot works fine
  ‣ For private/restricted sources, use a Userbot
"""
  
  ABOUT_TXT = """<b>╭──────❰ 🤖 𝐁𝐨𝐭 𝐃𝐞𝐭𝐚𝐢𝐥𝐬 ❱──────╮
┃ 
┣⊸ 🤖 Mʏ Nᴀᴍᴇ   : <a href=https://t.me/MeJeetX>Aryᴀ Bᴏᴛ</a>
┣⊸ 👨‍💻 ᴅᴇᴠᴇʟᴏᴘᴇʀ : <a href=https://t.me/MeJeetX>MeJeetX</a>
┣⊸ 📢 ᴄʜᴀɴɴᴇʟ   : <a href=https://t.me/MeJeetX>Updates</a>
┣⊸ 💬 sᴜᴘᴘᴏʀᴛ   : <a href=https://t.me/+1p2hcQ4ZaupjNjI1>Support Group</a>
┃ 
┣⊸ 🗣️ ʟᴀɴɢᴜᴀɢᴇ  : ᴘʏᴛʜᴏɴ 3 
┃  {python_version}
┣⊸ 📚 ʟɪʙʀᴀʀʏ   : ᴘʏʀᴏɢʀᴀᴍ  
┃
╰─────────────────────────────╯</b>"""
  
  STATUS_TXT = """<b>╭──────❰ 🤖 𝐁𝐨𝐭 𝐒𝐭𝐚𝐭𝐮𝐬 ❱──────╮
┃
┣⊸ 👨 ᴜsᴇʀs   : <code>{}</code>
┣⊸ 🤖 ʙᴏᴛs    : <code>{}</code>
┣⊸ 📡 ғᴏʀᴡᴀʀᴅ : <code>{}</code>
┣⊸ 📣 ᴄʜᴀɴɴᴇʟ : <code>{}</code>
┣⊸ 🚫 ʙᴀɴɴᴇᴅ  : <code>{}</code>
┃
╰─────────────────────────────╯</b>""" 
  
  FROM_MSG = "<b>❪ SET SOURCE CHAT ❫\n\nForward the last message or link.\nType username/ID (e.g. <code>@somebot</code> or <code>123456</code>) for bot/private chat.\nType <code>me</code> for Saved Messages.\n/cancel - to cancel</b>"
  TO_MSG = "<b>❪ CHOOSE TARGET CHAT ❫\n\nChoose your target chat from the given buttons.\n/cancel - Cancel this process</b>"
  SAVED_MSG_MODE = "<b>❪ SELECT MODE ❫\n\nChoose forwarding mode:\n1. <code>batch</code> - Forward existing messages.\n2. <code>live</code> - Continuous (wait for new messages).</b>"
  SAVED_MSG_LIMIT = "<b>❪ NUMBER OF MESSAGES ❫\n\nHow many messages to forward?\nEnter a number or <code>all</code>.</b>"
  SKIP_MSG = "<b>❪ SET MESSAGE SKIPING NUMBER ❫</b>\n\n<b>Skip the message as much as you enter the number and the rest of the message will be forwarded\nDefault Skip Number =</b> <code>0</code>\n<code>eg: You enter 0 = 0 message skiped\n You enter 5 = 5 message skiped</code>\n/cancel <b>- cancel this process</b>"
  CANCEL = "<b>Process Cancelled Succefully !</b>"
  BOT_DETAILS = "<b><u>📄 BOT DETAILS</b></u>\n\n<b>➣ NAME:</b> <code>{}</code>\n<b>➣ BOT ID:</b> <code>{}</code>\n<b>➣ USERNAME:</b> @{}"
  USER_DETAILS = "<b><u>📄 USERBOT DETAILS</b></u>\n\n<b>➣ NAME:</b> <code>{}</code>\n<b>➣ USER ID:</b> <code>{}</code>\n<b>➣ USERNAME:</b> @{}"  
         
  TEXT = """<b>╭──────❰ ✦ 𝐀𝐮𝐭𝐨 𝐅𝐨𝐫𝐰𝐚𝐫𝐝𝐞𝐫 ✦ ❱──────╮
┃
┣⊸ ◈ 𝐅𝐞𝐭𝐜𝐡𝐞𝐝     : <code>{}</code>
┣⊸ ◈ 𝐅𝐨𝐫𝐰𝐚𝐫𝐝𝐞𝐝   : <code>{}</code>
┣⊸ ◈ 𝐃𝐮𝐩𝐥𝐢𝐜𝐚𝐭𝐞   : <code>{}</code>
┣⊸ ◈ 𝐒𝐤𝐢𝐩𝐩𝐞𝐝     : <code>{}</code>
┣⊸ ◈ 𝐃𝐞𝐥𝐞𝐭𝐞𝐝     : <code>{}</code>
┃
┣⊸ ◈ 𝐒𝐭𝐚𝐭𝐮𝐬      : <code>{}</code>
┣⊸ ◈ 𝐄𝐓𝐀         : <code>{}</code>
┃
╰────────────────────────────────╯</b>"""

  TEXT1 = TEXT

  DUPLICATE_TEXT = """<b>╭──────❰ ✦ 𝐔𝐧𝐞𝐪𝐮𝐢𝐟𝐲 𝐒𝐭𝐚𝐭𝐮𝐬 ✦ ❱──────╮
┃
┣⊸ ◈ 𝐅𝐞𝐭𝐜𝐡𝐞𝐝     : <code>{}</code>
┣⊸ ◈ 𝐃𝐮𝐩𝐥𝐢𝐜𝐚𝐭𝐞𝐬  : <code>{}</code>
┃
╰───────────────── {} ────╯</b>"""
