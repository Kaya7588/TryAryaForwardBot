"""
Live Jobs Plugin — Complete Rewrite (Fixed)
============================================
Bugs fixed vs v1:
  1. last_seen_id=0 caused forwarding ALL old messages. Now on first init we
     capture current latest ID without forwarding anything.
  2. Userbot history (newest-first) was forwarded in wrong order. Now reversed.
  3. Filters from user settings are now applied before forwarding.
  4. forwarded counter bug (stale snapshot). Now uses $inc atomic update.
  5. "me" / bot private chat supported (userbot only).
  6. copy_message failures now fall back to forward_messages for restricted chats.
  7. Full UI with source type selection (Channel/Group vs Bot Private Chat).

Flow:
  /jobs → list → ➕ Create → Step1(account) → Step2(source) → Step3(dest) → job starts
"""
import time
import asyncio
import logging
from database import db
from .test import CLIENT, start_clone_bot
from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)

logger = logging.getLogger(__name__)
_CLIENT = CLIENT()

# In-memory: job_id → asyncio.Task
_job_tasks: dict[str, asyncio.Task] = {}


# ══════════════════════════════════════════════════════════════════════════════
# DB helpers
# ══════════════════════════════════════════════════════════════════════════════

async def _save_job(job: dict):
    await db.db.jobs.replace_one({"job_id": job["job_id"]}, job, upsert=True)

async def _get_job(job_id: str) -> dict | None:
    return await db.db.jobs.find_one({"job_id": job_id})

async def _list_jobs(user_id: int) -> list[dict]:
    return [j async for j in db.db.jobs.find({"user_id": user_id})]

async def _delete_job_db(job_id: str):
    await db.db.jobs.delete_one({"job_id": job_id})

async def _update_job(job_id: str, **kwargs):
    await db.db.jobs.update_one({"job_id": job_id}, {"$set": kwargs})

async def _inc_forwarded(job_id: str, n: int = 1):
    """Atomic increment on forwarded count."""
    await db.db.jobs.update_one({"job_id": job_id}, {"$inc": {"forwarded": n}})


# ══════════════════════════════════════════════════════════════════════════════
# Filter helper — applies user's Filter settings to a message
# ══════════════════════════════════════════════════════════════════════════════

def _passes_filters(msg, disabled_types: list[str]) -> bool:
    """Return True if message should be forwarded (not filtered out)."""
    if msg.empty or msg.service:
        return False
    if 'text'      in disabled_types and msg.text and not msg.media:
        return False
    if 'audio'     in disabled_types and msg.audio:
        return False
    if 'voice'     in disabled_types and msg.voice:
        return False
    if 'video'     in disabled_types and msg.video:
        return False
    if 'photo'     in disabled_types and msg.photo:
        return False
    if 'document'  in disabled_types and msg.document:
        return False
    if 'animation' in disabled_types and msg.animation:
        return False
    if 'sticker'   in disabled_types and msg.sticker:
        return False
    if 'poll'      in disabled_types and msg.poll:
        return False
    return True


async def _forward_message(client, msg, to_chat: int, remove_caption: bool):
    """Try copy_message; fall back to forward_messages for restricted chats."""
    try:
        if remove_caption and msg.media:
            await client.copy_message(
                chat_id=to_chat,
                from_chat_id=msg.chat.id,
                message_id=msg.id,
                caption=""
            )
        else:
            await client.copy_message(
                chat_id=to_chat,
                from_chat_id=msg.chat.id,
                message_id=msg.id
            )
    except Exception:
        try:
            await client.forward_messages(
                chat_id=to_chat,
                from_chat_id=msg.chat.id,
                message_ids=msg.id
            )
        except Exception as e:
            logger.debug(f"[Job forward] Failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Find current latest message ID (called once on job init to avoid re-forwarding old msgs)
# ══════════════════════════════════════════════════════════════════════════════

async def _get_latest_id(client, chat_id, is_bot: bool) -> int:
    """Return the current latest message ID in chat without forwarding."""
    try:
        if not is_bot:
            # Userbot: get_chat_history gives newest-first
            async for msg in client.get_chat_history(chat_id, limit=1):
                return msg.id
        else:
            # Bot: binary search for the top ID (same approach as iter_messages)
            lo, hi = 1, 9_999_999
            BATCH = 50
            for _ in range(25):
                if hi - lo <= BATCH:
                    break
                mid = (lo + hi) // 2
                try:
                    probe = await client.get_messages(chat_id, [mid])
                    if not isinstance(probe, list): probe = [probe]
                    if any(m and not m.empty for m in probe):
                        lo = mid
                    else:
                        hi = mid
                except Exception:
                    hi = mid
            return hi
    except Exception:
        pass
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# Core job runner — runs as independent asyncio.Task
# ══════════════════════════════════════════════════════════════════════════════

async def _run_job(job_id: str, user_id: int):
    job = await _get_job(job_id)
    if not job:
        return

    acc = client = None
    try:
        acc = await db.get_bot(user_id, job["account_id"])
        if not acc:
            await _update_job(job_id, status="error", error="Account not found")
            return

        client = await start_clone_bot(_CLIENT.client(acc))
        is_bot = acc.get("is_bot", True)

        from_chat  = job["from_chat"]
        to_chat    = job["to_chat"]
        last_seen  = job.get("last_seen_id", 0)

        # ── First-run initialisation: capture latest ID without forwarding ──
        if last_seen == 0:
            last_seen = await _get_latest_id(client, from_chat, is_bot)
            await _update_job(job_id, last_seen_id=last_seen)
            logger.info(f"[Job {job_id}] Initialised at msg ID {last_seen}")

        logger.info(f"[Job {job_id}] Polling started. last_seen={last_seen}")

        while True:
            # ── Stop check ────────────────────────────────────────────────
            fresh = await _get_job(job_id)
            if not fresh or fresh.get("status") != "running":
                break

            # ── Load user Filters ─────────────────────────────────────────
            disabled_types: list[str] = await db.get_filters(user_id)
            configs = await db.get_configs(user_id)
            remove_caption = 'rm_caption' in disabled_types

            # ── Fetch new messages ────────────────────────────────────────
            new_msgs: list = []

            try:
                if not is_bot:
                    # USERBOT: get_chat_history is newest-first
                    collected = []
                    async for msg in client.get_chat_history(from_chat, limit=50):
                        if msg.id <= last_seen:
                            break
                        collected.append(msg)
                    # Reverse so we forward oldest-first
                    new_msgs = list(reversed(collected))

                else:
                    # BOT: iterate by ID above last_seen
                    probe = last_seen + 1
                    while True:
                        batch_ids = list(range(probe, probe + 50))
                        try:
                            msgs = await client.get_messages(from_chat, batch_ids)
                        except FloodWait as fw:
                            await asyncio.sleep(fw.value + 1)
                            continue
                        except Exception:
                            break
                        if not isinstance(msgs, list):
                            msgs = [msgs]
                        valid = [m for m in msgs if m and not m.empty and not m.service]
                        if not valid:
                            break
                        valid.sort(key=lambda m: m.id)
                        new_msgs.extend(valid)
                        probe = valid[-1].id + 1
                        # If we got a partial batch, no more messages yet
                        if len(valid) < 49:
                            break

            except FloodWait as fw:
                await asyncio.sleep(fw.value + 1)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[Job {job_id}] Fetch error: {e}")
                await asyncio.sleep(15)
                continue

            # ── Forward filtered messages ─────────────────────────────────
            fwd_count = 0
            for msg in new_msgs:
                if not _passes_filters(msg, disabled_types):
                    last_seen = max(last_seen, msg.id)
                    continue
                try:
                    await _forward_message(client, msg, to_chat, remove_caption)
                    fwd_count += 1
                except FloodWait as fw:
                    await asyncio.sleep(fw.value + 1)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug(f"[Job {job_id}] Forward error: {e}")
                last_seen = max(last_seen, msg.id)
                await asyncio.sleep(1)

            if new_msgs:
                await _update_job(job_id, last_seen_id=last_seen)
            if fwd_count > 0:
                await _inc_forwarded(job_id, fwd_count)

            # ── Wait before next poll ─────────────────────────────────────
            sleep_secs = configs.get("duration", 5) or 5
            await asyncio.sleep(max(5, sleep_secs))

    except asyncio.CancelledError:
        logger.info(f"[Job {job_id}] Cancelled")
    except Exception as e:
        logger.error(f"[Job {job_id}] Fatal: {e}")
        await _update_job(job_id, status="error", error=str(e))
    finally:
        _job_tasks.pop(job_id, None)
        if client:
            try:
                await client.stop()
            except Exception:
                pass


def _start_job_task(job_id: str, user_id: int) -> asyncio.Task:
    task = asyncio.create_task(_run_job(job_id, user_id))
    _job_tasks[job_id] = task
    return task


# ══════════════════════════════════════════════════════════════════════════════
# Resume all running jobs (called on startup)
# ══════════════════════════════════════════════════════════════════════════════

async def resume_live_jobs(user_id: int = None):
    query: dict = {"status": "running"}
    if user_id:
        query["user_id"] = user_id
    async for job in db.db.jobs.find(query):
        jid = job["job_id"]
        uid = job["user_id"]
        if jid not in _job_tasks:
            _start_job_task(jid, uid)
            logger.info(f"[Jobs] Resumed job {jid} for user {uid}")


# ══════════════════════════════════════════════════════════════════════════════
# UI helpers
# ══════════════════════════════════════════════════════════════════════════════

def _status_emoji(status: str) -> str:
    return {"running": "🟢", "stopped": "🔴", "error": "⚠️"}.get(status, "❓")


async def _render_jobs_list(bot, user_id: int, message_or_query):
    jobs = await _list_jobs(user_id)
    is_cb = hasattr(message_or_query, 'message')

    if not jobs:
        text = (
            "<b>📋 Live Jobs</b>\n\n"
            "<i>No jobs yet. A Live Job continuously watches a source chat\n"
            "and forwards new messages to your target — running in the background.\n\n"
            "👇 Create your first job below!</i>"
        )
        btns = InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Create New Job", callback_data="job#new")
        ]])
    else:
        lines = ["<b>📋 Your Live Jobs</b>\n"]
        for j in jobs:
            st  = _status_emoji(j.get("status", "stopped"))
            fwd = j.get("forwarded", 0)
            err = f" [{j.get('error','')}]" if j.get("status") == "error" else ""
            lines.append(
                f"{st} <b>{j.get('from_title','?')} → {j.get('to_title','?')}</b>"
                f"  <code>[{j['job_id'][-6:]}]</code>  ✅{fwd}{err}"
            )
        text = "\n".join(lines)

        btns_list = []
        for j in jobs:
            st  = j.get("status", "stopped")
            jid = j["job_id"]
            short = jid[-6:]
            row = []
            if st == "running":
                row.append(InlineKeyboardButton(f"⏹ Stop [{short}]",  callback_data=f"job#stop#{jid}"))
            else:
                row.append(InlineKeyboardButton(f"▶️ Start [{short}]", callback_data=f"job#start#{jid}"))
            row.append(InlineKeyboardButton(f"ℹ️ [{short}]",  callback_data=f"job#info#{jid}"))
            row.append(InlineKeyboardButton(f"🗑 [{short}]",   callback_data=f"job#del#{jid}"))
            btns_list.append(row)

        btns_list.append([InlineKeyboardButton("➕ Create New Job", callback_data="job#new")])
        btns_list.append([InlineKeyboardButton("🔄 Refresh",        callback_data="job#list")])
        btns = InlineKeyboardMarkup(btns_list)

    try:
        if is_cb:
            await message_or_query.message.edit_text(text, reply_markup=btns)
        else:
            await message_or_query.reply_text(text, reply_markup=btns)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# /jobs command
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.private & filters.command("jobs"))
async def jobs_cmd(bot, message):
    await _render_jobs_list(bot, message.from_user.id, message)


# ══════════════════════════════════════════════════════════════════════════════
# Callbacks
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex(r'^job#list$'))
async def job_list_cb(bot, query):
    await _render_jobs_list(bot, query.from_user.id, query)


@Client.on_callback_query(filters.regex(r'^job#info#'))
async def job_info_cb(bot, query):
    job_id = query.data.split("#", 2)[2]
    job = await _get_job(job_id)
    if not job:
        return await query.answer("Job not found!", show_alert=True)

    import datetime
    created = datetime.datetime.fromtimestamp(job.get("created", 0)).strftime("%Y-%m-%d %H:%M")
    st = _status_emoji(job.get("status", "stopped"))
    text = (
        f"<b>📋 Job Info</b>\n\n"
        f"<b>ID:</b> <code>{job_id[-6:]}</code>\n"
        f"<b>Status:</b> {st} {job.get('status','?')}\n"
        f"<b>Source:</b> {job.get('from_title','?')}\n"
        f"<b>Target:</b> {job.get('to_title','?')}\n"
        f"<b>Forwarded:</b> {job.get('forwarded', 0)}\n"
        f"<b>Last Msg ID:</b> {job.get('last_seen_id', 0)}\n"
        f"<b>Created:</b> {created}\n"
    )
    if job.get("error"):
        text += f"\n<b>⚠️ Error:</b> <code>{job['error']}</code>"

    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("↩ Back", callback_data="job#list")
    ]]))


@Client.on_callback_query(filters.regex(r'^job#stop#'))
async def job_stop_cb(bot, query):
    job_id  = query.data.split("#", 2)[2]
    user_id = query.from_user.id

    # Security: verify ownership
    job = await _get_job(job_id)
    if not job or job.get("user_id") != user_id:
        return await query.answer("⛔ Unauthorized.", show_alert=True)

    task = _job_tasks.pop(job_id, None)
    if task and not task.done():
        task.cancel()

    await _update_job(job_id, status="stopped")
    await query.answer("⏹ Job stopped.", show_alert=False)
    await _render_jobs_list(bot, user_id, query)


@Client.on_callback_query(filters.regex(r'^job#start#'))
async def job_start_cb(bot, query):
    job_id  = query.data.split("#", 2)[2]
    user_id = query.from_user.id

    # Security: verify ownership
    job = await _get_job(job_id)
    if not job or job.get("user_id") != user_id:
        return await query.answer("⛔ Unauthorized.", show_alert=True)

    if job_id in _job_tasks and not _job_tasks[job_id].done():
        return await query.answer("Already running!", show_alert=True)

    await _update_job(job_id, status="running")
    _start_job_task(job_id, user_id)
    await query.answer("▶️ Job started.", show_alert=False)
    await _render_jobs_list(bot, user_id, query)


@Client.on_callback_query(filters.regex(r'^job#del#'))
async def job_del_cb(bot, query):
    job_id  = query.data.split("#", 2)[2]
    user_id = query.from_user.id

    # Security: verify ownership
    job = await _get_job(job_id)
    if not job or job.get("user_id") != user_id:
        return await query.answer("⛔ Unauthorized.", show_alert=True)

    task = _job_tasks.pop(job_id, None)
    if task and not task.done():
        task.cancel()

    await _delete_job_db(job_id)
    await query.answer("🗑 Job deleted.", show_alert=False)
    await _render_jobs_list(bot, user_id, query)


# ══════════════════════════════════════════════════════════════════════════════
# Create Job — Interactive flow (bot.ask)
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex(r'^job#new$'))
async def job_new_cb(bot, query):
    user_id = query.from_user.id
    await query.message.delete()
    await _create_job_flow(bot, user_id)


@Client.on_message(filters.private & filters.command("newjob"))
async def newjob_cmd(bot, message):
    await _create_job_flow(bot, message.from_user.id)


async def _create_job_flow(bot, user_id: int):
    # ── Step 1: Account ──────────────────────────────────────────────────────
    accounts = await db.get_bots(user_id)
    if not accounts:
        return await bot.send_message(user_id,
            "<b>❌ No accounts. Add one in /settings → Accounts first.</b>")

    acc_btns = [[KeyboardButton(
        f"{'🤖 Bot' if a.get('is_bot', True) else '👤 Userbot'}: "
        f"{a.get('username') or a.get('name', 'Unknown')} [{a['id']}]"
    )] for a in accounts]
    acc_btns.append([KeyboardButton("/cancel")])

    acc_r = await bot.ask(user_id,
        "<b>🔧 Create Live Job — Step 1/3</b>\n\n"
        "Choose which account to use for this job\n"
        "<i>(Userbot required for private chats & saved messages)</i>:",
        reply_markup=ReplyKeyboardMarkup(acc_btns, resize_keyboard=True, one_time_keyboard=True))

    if "/cancel" in acc_r.text:
        return await acc_r.reply("<b>Cancelled.</b>", reply_markup=ReplyKeyboardRemove())

    acc_id = None
    if "[" in acc_r.text and "]" in acc_r.text:
        try:
            acc_id = int(acc_r.text.split('[')[-1].split(']')[0])
        except Exception:
            pass
    sel_acc = (await db.get_bot(user_id, acc_id)) if acc_id else accounts[0]
    is_bot = sel_acc.get("is_bot", True)

    # ── Step 2: Source ───────────────────────────────────────────────────────
    src_r = await bot.ask(user_id,
        "<b>Step 2/3 — Source Chat</b>\n\n"
        "Send one of the following:\n"
        "• <code>@username</code> or channel/group link\n"
        "• Numeric ID (e.g. <code>-1001234567890</code>)\n"
        "• <code>me</code> for Saved Messages <i>(userbot only)</i>\n"
        "• A bot's username for bot private chat <i>(userbot only)</i>\n\n"
        "/cancel to abort",
        reply_markup=ReplyKeyboardRemove())

    if src_r.text.strip().startswith("/cancel"):
        return await src_r.reply("<b>Cancelled.</b>")

    from_chat_raw = src_r.text.strip()

    # Parse source
    if from_chat_raw.lower() in ("me", "saved"):
        if is_bot:
            return await src_r.reply(
                "<b>❌ Saved Messages require a Userbot. Please choose a Userbot account.</b>")
        from_chat = "me"
        from_title = "Saved Messages"
    else:
        from_chat = from_chat_raw
        if from_chat.lstrip('-').isdigit():
            from_chat = int(from_chat)
        try:
            chat_obj   = await bot.get_chat(from_chat)
            from_title = getattr(chat_obj, "title", None) or getattr(chat_obj, "first_name", str(from_chat))
        except Exception:
            from_title = str(from_chat)

    # ── Step 3: Destination ──────────────────────────────────────────────────
    channels = await db.get_user_channels(user_id)
    if not channels:
        return await bot.send_message(user_id,
            "<b>❌ No target channels saved. Add via /settings → Channels.</b>",
            reply_markup=ReplyKeyboardRemove())

    ch_btns = [[KeyboardButton(ch['title'])] for ch in channels]
    ch_btns.append([KeyboardButton("/cancel")])

    ch_r = await bot.ask(user_id,
        "<b>Step 3/3 — Target Chat</b>\n\nChoose where to forward new messages:",
        reply_markup=ReplyKeyboardMarkup(ch_btns, resize_keyboard=True, one_time_keyboard=True))

    if "/cancel" in ch_r.text:
        return await ch_r.reply("<b>Cancelled.</b>", reply_markup=ReplyKeyboardRemove())

    to_chat, to_title = None, ch_r.text.strip()
    for ch in channels:
        if ch['title'] == to_title:
            to_chat  = ch['chat_id']
            to_title = ch['title']
            break

    if not to_chat:
        return await bot.send_message(user_id, "<b>Invalid selection. Cancelled.</b>",
                                      reply_markup=ReplyKeyboardRemove())

    # ── Save & Start ─────────────────────────────────────────────────────────
    job_id = f"{user_id}-{int(time.time())}"
    job = {
        "job_id":       job_id,
        "user_id":      user_id,
        "account_id":   sel_acc["id"],
        "from_chat":    from_chat,
        "from_title":   from_title,
        "to_chat":      to_chat,
        "to_title":     to_title,
        "status":       "running",
        "created":      int(time.time()),
        "forwarded":    0,
        "last_seen_id": 0,   # 0 means "initialise on first poll"
    }
    await _save_job(job)
    _start_job_task(job_id, user_id)

    await bot.send_message(
        user_id,
        f"<b>✅ Live Job Created &amp; Started!</b>\n\n"
        f"🟢 Watching <b>{from_title}</b> → <b>{to_title}</b>\n"
        f"<b>Account:</b> {'🤖 Bot' if is_bot else '👤 Userbot'}: "
        f"{sel_acc.get('name','?')}\n"
        f"<b>Filters:</b> respects your /settings → Filters\n"
        f"<b>Job ID:</b> <code>{job_id[-6:]}</code>\n\n"
        f"<i>New messages will be forwarded automatically in the background.\n"
        f"Use /jobs to manage or stop this job.</i>",
        reply_markup=ReplyKeyboardRemove()
    )
