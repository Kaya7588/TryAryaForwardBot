"""
Merger Plugin — v2
==================
Merges media files (ANY audio/video format) from a source channel range
into one combined file using FFmpeg.

Supported audio: MP3, AAC, OGG, FLAC, ALAC, WAV, AIFF, M4A, WMA, OPUS
Supported video: MP4, MKV, AVI, WEBM, MOV, FLV, TS

Features:
  • Strict ordering — files merged exactly as they appear in the channel
  • No format skipping — ALL media files in range are merged
  • Full metadata editing — title, artist, album, genre, year, track, composer, etc.
  • Destination channel — sends merged file to selected destination
  • Multi merge — multiple merge jobs simultaneously
  • Progress bar — real-time download/upload progress with speed + ETA
  • Lossless when possible, high-quality re-encode when codecs differ

Commands:
  /merge  — Open the Merger manager UI
"""
import os
import re
import time
import uuid
import asyncio
import logging
import shutil
import subprocess
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

# ─── In-memory task registry ─────────────────────────────────────────────────
_merge_tasks: dict[str, asyncio.Task] = {}

# ─── Future-based ask() ──────────────────────────────────────────────────────
_merge_waiting: dict[int, asyncio.Future] = {}


@Client.on_message(filters.private, group=-14)
async def _merge_input_router(bot, message):
    uid = message.from_user.id if message.from_user else None
    if uid and uid in _merge_waiting:
        fut = _merge_waiting.pop(uid)
        if not fut.done():
            fut.set_result(message)


async def _merge_ask(bot, user_id: int, text: str, reply_markup=None, timeout: int = 300):
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    old = _merge_waiting.pop(user_id, None)
    if old and not old.done():
        old.cancel()
    _merge_waiting[user_id] = fut
    await bot.send_message(user_id, text, reply_markup=reply_markup)
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _merge_waiting.pop(user_id, None)
        raise


# ══════════════════════════════════════════════════════════════════════════════
# DB helpers
# ══════════════════════════════════════════════════════════════════════════════

COLL = "mergejobs"


async def _mg_save(job: dict):
    await db.db[COLL].replace_one({"job_id": job["job_id"]}, job, upsert=True)


async def _mg_get(job_id: str):
    return await db.db[COLL].find_one({"job_id": job_id})


async def _mg_list(user_id: int):
    return [j async for j in db.db[COLL].find({"user_id": user_id})]


async def _mg_delete(job_id: str):
    await db.db[COLL].delete_one({"job_id": job_id})


async def _mg_update(job_id: str, **kwargs):
    await db.db[COLL].update_one({"job_id": job_id}, {"$set": kwargs})


# ══════════════════════════════════════════════════════════════════════════════
# Progress bar helpers
# ══════════════════════════════════════════════════════════════════════════════

def _progress_bar(current, total, width=20):
    """Generate a text progress bar."""
    if total <= 0:
        return "[" + "░" * width + "] 0%"
    pct = min(100, int(current / total * 100))
    filled = int(width * current / total)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {pct}%"


def _format_size(bytes_val):
    """Format bytes into human-readable size."""
    if bytes_val < 1024:
        return f"{bytes_val} B"
    elif bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f} KB"
    elif bytes_val < 1024 * 1024 * 1024:
        return f"{bytes_val / (1024 * 1024):.1f} MB"
    else:
        return f"{bytes_val / (1024 * 1024 * 1024):.2f} GB"


def _format_speed(bytes_per_sec):
    """Format speed into human-readable."""
    if bytes_per_sec < 1024:
        return f"{bytes_per_sec:.0f} B/s"
    elif bytes_per_sec < 1024 * 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    else:
        return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"


def _format_time(seconds):
    """Format seconds into human-readable duration."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    else:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h {m}m"


# ══════════════════════════════════════════════════════════════════════════════
# FFmpeg helpers
# ══════════════════════════════════════════════════════════════════════════════

def _check_ffmpeg():
    """Return True if ffmpeg is available on the system."""
    return shutil.which("ffmpeg") is not None


def _get_media_info(file_path):
    """Detect media type, codec, and duration using ffprobe."""
    info = {"type": "audio", "codec": "", "duration": 0}
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries",
             "stream=codec_type,codec_name", "-show_entries",
             "format=duration", "-of", "csv=p=0", file_path],
            capture_output=True, text=True, timeout=30
        )
        lines = result.stdout.strip().split("\n")
        for line in lines:
            parts = line.strip().split(",")
            if len(parts) >= 2:
                if parts[1] == "video":
                    info["type"] = "video"
                    info["codec"] = parts[0]
                elif parts[1] == "audio" and not info.get("codec"):
                    info["codec"] = parts[0]
            elif len(parts) == 1:
                try:
                    info["duration"] = float(parts[0])
                except (ValueError, TypeError):
                    pass
    except Exception:
        pass
    # Fallback: use extension
    if not info["codec"]:
        ext = os.path.splitext(file_path)[1].lower()
        if ext in (".mp4", ".mkv", ".avi", ".webm", ".mov", ".flv", ".ts"):
            info["type"] = "video"
        info["codec"] = ext.lstrip(".")
    return info


def _merge_files_ffmpeg(file_list, output_path, metadata=None, media_type="audio"):
    """
    Merge files using FFmpeg.
    Strategy:
      1. Try lossless concat demuxer (-c copy) first
      2. If that fails (codec mismatch), re-encode at highest quality
    Returns (success, error_message).
    """
    list_path = output_path + ".list.txt"
    try:
        with open(list_path, "w", encoding="utf-8") as f:
            for fp in file_list:
                safe = fp.replace("'", "'\\''")
                f.write(f"file '{safe}'\n")

        # ── Strategy 1: Lossless concat ──────────────────────────────────
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
               "-i", list_path, "-c", "copy"]
        if metadata:
            for key, val in metadata.items():
                if val:
                    cmd.extend(["-metadata", f"{key}={val}"])
        cmd.append(output_path)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)

        if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return True, ""

        # ── Strategy 2: High-quality re-encode ───────────────────────────
        logger.warning(f"Concat copy failed, re-encoding: {result.stderr[-300:]}")

        cmd_re = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path]

        if media_type == "video":
            cmd_re.extend([
                "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-c:a", "aac", "-b:a", "320k",
                "-movflags", "+faststart"
            ])
        else:
            # Audio: use 320k MP3 for maximum compatibility + quality
            cmd_re.extend(["-c:a", "libmp3lame", "-b:a", "320k", "-ar", "44100"])

        if metadata:
            for key, val in metadata.items():
                if val:
                    cmd_re.extend(["-metadata", f"{key}={val}"])
        cmd_re.append(output_path)

        result2 = subprocess.run(cmd_re, capture_output=True, text=True, timeout=14400)
        if result2.returncode != 0:
            return False, result2.stderr[-500:]

        return True, ""
    except subprocess.TimeoutExpired:
        return False, "FFmpeg timed out"
    except Exception as e:
        return False, str(e)
    finally:
        if os.path.exists(list_path):
            os.remove(list_path)


# ══════════════════════════════════════════════════════════════════════════════
# Progress tracking for downloads/uploads
# ══════════════════════════════════════════════════════════════════════════════

class ProgressTracker:
    """Track download/upload progress and provide formatted updates."""

    def __init__(self, bot, user_id, job_id, phase="download"):
        self.bot = bot
        self.user_id = user_id
        self.job_id = job_id
        self.phase = phase
        self.status_msg = None
        self.last_edit_time = 0
        self.start_time = time.time()
        self.total_bytes = 0
        self.current_file_idx = 0
        self.total_files = 0

    async def update_file_progress(self, file_idx, total_files, dl_bytes=0, total_bytes=0):
        """Called periodically to update progress for file-level operations."""
        now = time.time()
        if now - self.last_edit_time < 3:  # Rate limit: max 1 edit per 3 seconds
            return

        self.last_edit_time = now
        self.current_file_idx = file_idx
        self.total_files = total_files

        elapsed = now - self.start_time
        speed = dl_bytes / elapsed if elapsed > 0 else 0
        files_remaining = total_files - file_idx
        avg_time = elapsed / max(file_idx, 1)
        eta = files_remaining * avg_time

        bar = _progress_bar(file_idx, total_files)
        icon = "⬇️" if self.phase == "download" else "⬆️"
        phase_label = "Downloading" if self.phase == "download" else "Uploading"

        text = (
            f"<b>{icon} {phase_label}...</b>\n\n"
            f"<code>{bar}</code>\n\n"
            f"📁 <b>Files:</b> {file_idx}/{total_files}\n"
            f"💾 <b>Size:</b> {_format_size(dl_bytes)}\n"
            f"⚡ <b>Speed:</b> {_format_speed(speed)}\n"
            f"⏱ <b>ETA:</b> {_format_time(eta)}"
        )

        try:
            if self.status_msg:
                await self.status_msg.edit_text(text)
            else:
                self.status_msg = await self.bot.send_message(self.user_id, text)
        except Exception:
            pass

    async def final_update(self, text):
        """Send or edit a final status message."""
        try:
            if self.status_msg:
                await self.status_msg.edit_text(text)
            else:
                await self.bot.send_message(self.user_id, text)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Core merge runner
# ══════════════════════════════════════════════════════════════════════════════

BATCH_SIZE = 200

# All supported media extensions — NEVER skip based on format
AUDIO_EXTS = {".mp3", ".aac", ".ogg", ".flac", ".alac", ".wav", ".aiff",
              ".m4a", ".wma", ".opus", ".m4b", ".oga", ".wv"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".webm", ".mov", ".flv", ".ts",
              ".m4v", ".wmv", ".3gp", ".mpg", ".mpeg"}


async def _run_merge_job(job_id, user_id, bot):
    """
    Main coroutine for a Merge Job.
    1. Downloads ALL media files in range (no format skipping)
    2. Merges them with FFmpeg (lossless or high-quality re-encode)
    3. Uploads to destination channel(s) + user
    """
    job = await _mg_get(job_id)
    if not job:
        return

    client = None
    work_dir = f"merge_tmp/{job_id}"
    os.makedirs(work_dir, exist_ok=True)

    try:
        acc = await db.get_bot(user_id, job["account_id"])
        if not acc:
            await _mg_update(job_id, status="error", error="Account not found")
            return

        client = await start_clone_bot(_CLIENT.client(acc))

        from_chat    = job["from_chat"]
        start_id     = job["start_id"]
        end_id       = job["end_id"]
        out_name     = job.get("output_name", "merged")
        metadata     = job.get("metadata", {})
        dest_chats   = job.get("dest_chats", [])  # Destination channel IDs

        await _mg_update(job_id, status="downloading", error="")

        # ── Phase 1: Download ALL media files ─────────────────────────────
        tracker = ProgressTracker(bot, user_id, job_id, "download")
        downloaded_files = []
        current = start_id
        total_range = end_id - start_id + 1
        downloaded_count = 0
        total_dl_bytes = 0
        skipped_text = 0  # Only text messages skipped
        start_time = time.time()

        while current <= end_id:
            # Check if stopped
            fresh = await _mg_get(job_id)
            if not fresh or fresh.get("status") == "stopped":
                return

            batch_end = min(current + BATCH_SIZE - 1, end_id)
            batch_ids = list(range(current, batch_end + 1))

            try:
                msgs = await client.get_messages(from_chat, batch_ids)
                if not isinstance(msgs, list):
                    msgs = [msgs]
            except FloodWait as fw:
                await asyncio.sleep(fw.value + 2)
                continue
            except Exception as e:
                logger.warning(f"[Merge {job_id}] Fetch error at {current}: {e}")
                current += BATCH_SIZE
                continue

            valid = [m for m in msgs if m and not m.empty and not m.service]
            valid.sort(key=lambda m: m.id)  # STRICT ORDER

            for msg in valid:
                # Skip non-media (text-only messages)
                if not msg.media:
                    skipped_text += 1
                    continue

                # Accept ANY media type — photos, stickers excluded for merge
                # but audio/video/document/voice/video_note are ALL included
                media_obj = None
                for attr in ('audio', 'video', 'document', 'voice', 'video_note'):
                    media_obj = getattr(msg, attr, None)
                    if media_obj:
                        break

                if not media_obj:
                    # Photo/sticker/animation — not mergeable into audio/video stream
                    skipped_text += 1
                    continue

                # Get original filename and extension
                original_name = getattr(media_obj, 'file_name', None)
                ext = ""
                if original_name:
                    ext = os.path.splitext(original_name)[1].lower()
                if not ext:
                    # Derive extension from media type
                    if getattr(msg, 'audio', None):
                        mime = getattr(media_obj, 'mime_type', '') or ''
                        if 'mp4' in mime or 'm4a' in mime:
                            ext = ".m4a"
                        elif 'ogg' in mime:
                            ext = ".ogg"
                        elif 'flac' in mime:
                            ext = ".flac"
                        elif 'wav' in mime:
                            ext = ".wav"
                        else:
                            ext = ".mp3"
                    elif getattr(msg, 'voice', None):
                        ext = ".ogg"
                    elif getattr(msg, 'video', None) or getattr(msg, 'video_note', None):
                        ext = ".mp4"
                    elif getattr(msg, 'document', None):
                        mime = getattr(media_obj, 'mime_type', '') or ''
                        if 'audio' in mime:
                            ext = ".mp3"
                        elif 'video' in mime:
                            ext = ".mp4"
                        else:
                            ext = ".bin"  # Still download — FFmpeg will figure it out

                # Sequential naming for strict order
                seq_name = f"{downloaded_count:06d}{ext}"
                dl_path = os.path.join(work_dir, seq_name)

                # Download with retry (5 attempts)
                fp = None
                for dl_attempt in range(5):
                    try:
                        fp = await client.download_media(msg, file_name=dl_path)
                        if fp:
                            break
                    except FloodWait as fw:
                        await asyncio.sleep(fw.value + 2)
                    except Exception as dl_e:
                        err_str = str(dl_e).upper()
                        if "TIMEOUT" in err_str or "CONNECTION" in err_str:
                            await asyncio.sleep(5)
                            continue
                        if dl_attempt < 4:
                            await asyncio.sleep(3)
                            continue
                        logger.warning(f"[Merge {job_id}] DL failed {msg.id}: {dl_e}")
                        break

                if fp and os.path.exists(fp):
                    file_sz = os.path.getsize(fp)
                    total_dl_bytes += file_sz
                    downloaded_files.append(fp)
                    downloaded_count += 1
                    await _mg_update(job_id, downloaded=downloaded_count)

                    # Update progress every 5 files
                    if downloaded_count % 5 == 0:
                        await tracker.update_file_progress(
                            downloaded_count, total_range - skipped_text,
                            total_dl_bytes, 0
                        )

            current = batch_end + 1

        if not downloaded_files:
            await _mg_update(job_id, status="error", error="No media files found in range")
            await tracker.final_update(
                "<b>❌ Merge failed: No media files found in the specified range.</b>"
            )
            return

        # Final download status
        await tracker.final_update(
            f"<b>✅ Download complete!</b>\n\n"
            f"📁 <b>Files:</b> {downloaded_count}\n"
            f"💾 <b>Total:</b> {_format_size(total_dl_bytes)}\n"
            f"⏱ <b>Time:</b> {_format_time(time.time() - start_time)}"
        )

        # ── Phase 2: Merge with FFmpeg ────────────────────────────────────
        await _mg_update(job_id, status="merging", downloaded=downloaded_count)

        # Detect media type from first file
        first_info = _get_media_info(downloaded_files[0])
        media_type = first_info["type"]
        out_ext = ".mp4" if media_type == "video" else ".mp3"
        output_path = os.path.join(work_dir, f"{out_name}{out_ext}")

        # Merge status message
        merge_msg = None
        try:
            merge_msg = await bot.send_message(
                user_id,
                f"<b>🔀 Merging {downloaded_count} files...</b>\n\n"
                f"<b>Type:</b> {'🎬 Video' if media_type == 'video' else '🎵 Audio'}\n"
                f"<b>Output:</b> <code>{out_name}{out_ext}</code>\n\n"
                f"<i>Attempting lossless concat first.\n"
                f"If codecs differ, will re-encode at highest quality (320k audio / CRF 18 video).\n"
                f"This may take several minutes for large files.</i>"
            )
        except Exception:
            pass

        # Run FFmpeg in thread
        loop = asyncio.get_event_loop()
        success, error_msg = await loop.run_in_executor(
            None, _merge_files_ffmpeg, downloaded_files, output_path,
            metadata, media_type
        )

        if not success:
            await _mg_update(job_id, status="error", error=error_msg[:500])
            try:
                if merge_msg:
                    await merge_msg.edit_text(
                        f"<b>❌ Merge failed!</b>\n\n<code>{error_msg[:500]}</code>"
                    )
            except Exception:
                pass
            return

        # Check file size
        file_size = os.path.getsize(output_path)
        file_size_mb = file_size / (1024 * 1024)

        if file_size > 2 * 1024 * 1024 * 1024:  # 2GB
            await _mg_update(job_id, status="error",
                             error=f"Merged file too large: {file_size_mb:.0f}MB")
            try:
                if merge_msg:
                    await merge_msg.edit_text(
                        f"<b>❌ Merged file is {file_size_mb:.0f}MB — exceeds Telegram's 2GB limit.</b>\n"
                        f"<i>Try merging fewer files.</i>"
                    )
            except Exception:
                pass
            return

        try:
            if merge_msg:
                await merge_msg.edit_text(
                    f"<b>✅ Merge complete!</b>\n"
                    f"<b>Size:</b> {_format_size(file_size)}"
                )
        except Exception:
            pass

        # ── Phase 3: Upload to destinations ───────────────────────────────
        await _mg_update(job_id, status="uploading")
        upload_tracker = ProgressTracker(bot, user_id, job_id, "upload")

        caption = (
            f"<b>🔀 {out_name}{out_ext}</b>\n"
            f"<b>Files merged:</b> {downloaded_count}\n"
            f"<b>Size:</b> {_format_size(file_size)}"
        )
        if metadata.get("title"):
            caption += f"\n<b>Title:</b> {metadata['title']}"
        if metadata.get("artist"):
            caption += f"\n<b>Artist:</b> {metadata['artist']}"

        # Build list of destinations: user + any selected channels
        all_dests = [user_id]
        if dest_chats:
            for dc in dest_chats:
                if dc not in all_dests:
                    all_dests.append(dc)

        upload_start = time.time()

        for dest_idx, dest_id in enumerate(all_dests):
            for up_attempt in range(3):
                try:
                    if media_type == "video":
                        await client.send_video(
                            chat_id=dest_id,
                            video=output_path,
                            caption=caption,
                            file_name=f"{out_name}{out_ext}",
                            supports_streaming=True
                        )
                    else:
                        send_kw = {
                            "chat_id": dest_id,
                            "audio": output_path,
                            "caption": caption,
                            "file_name": f"{out_name}{out_ext}",
                        }
                        if metadata.get("title"):
                            send_kw["title"] = metadata["title"]
                        if metadata.get("artist"):
                            send_kw["performer"] = metadata["artist"]
                        await client.send_audio(**send_kw)

                    # Update progress
                    up_elapsed = time.time() - upload_start
                    up_speed = file_size / up_elapsed if up_elapsed > 0 else 0
                    try:
                        await upload_tracker.update_file_progress(
                            dest_idx + 1, len(all_dests), file_size, file_size
                        )
                    except Exception:
                        pass
                    break
                except FloodWait as fw:
                    await asyncio.sleep(fw.value + 2)
                except Exception as up_e:
                    if up_attempt < 2:
                        await asyncio.sleep(5)
                        continue
                    logger.warning(f"[Merge {job_id}] Upload to {dest_id} failed: {up_e}")
                    break

        # ── Done ──────────────────────────────────────────────────────────
        elapsed_total = time.time() - start_time
        await _mg_update(job_id, status="done")

        dest_labels = []
        for dc in dest_chats:
            try:
                chat_info = await client.get_chat(dc)
                dest_labels.append(getattr(chat_info, 'title', str(dc)))
            except Exception:
                dest_labels.append(str(dc))

        dest_text = "\n".join(f" ┣ 📤 {d}" for d in dest_labels) if dest_labels else " ┣ 📤 Sent to you (DM)"

        try:
            await bot.send_message(
                user_id,
                f"<b>✅ Merge Job Complete!</b>\n\n"
                f"<b>📊 Summary:</b>\n"
                f" ┣ <b>Downloaded:</b> {downloaded_count} files\n"
                f" ┣ <b>Skipped:</b> {skipped_text} (text/photo only)\n"
                f" ┣ <b>Output:</b> <code>{out_name}{out_ext}</code>\n"
                f" ┣ <b>Size:</b> {_format_size(file_size)}\n"
                f" ┣ <b>Type:</b> {'🎬 Video' if media_type == 'video' else '🎵 Audio'}\n"
                f" ┗ <b>Time:</b> {_format_time(elapsed_total)}\n\n"
                f"<b>📤 Destinations:</b>\n{dest_text}"
            )
        except Exception:
            pass

    except asyncio.CancelledError:
        logger.info(f"[Merge {job_id}] Cancelled")
        await _mg_update(job_id, status="stopped")
    except Exception as e:
        logger.error(f"[Merge {job_id}] Fatal: {e}")
        await _mg_update(job_id, status="error", error=str(e)[:500])
        try:
            await bot.send_message(user_id,
                f"<b>❌ Merge error:</b>\n<code>{str(e)[:500]}</code>")
        except Exception:
            pass
    finally:
        _merge_tasks.pop(job_id, None)
        try:
            if os.path.exists(work_dir):
                shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass
        if client:
            try:
                await client.stop()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# Parse message link
# ══════════════════════════════════════════════════════════════════════════════

def _parse_msg_link(text):
    """Parse a Telegram message link → (chat_ref, message_id)."""
    text = text.strip()
    if text.isdigit():
        return None, int(text)

    m = re.match(r'https?://t\.me/c/(\d+)/(\d+)', text)
    if m:
        return int(m.group(1)), int(m.group(2))

    m = re.match(r'https?://t\.me/([^/]+)/(\d+)', text)
    if m:
        return m.group(1), int(m.group(2))

    return None, None


# ══════════════════════════════════════════════════════════════════════════════
# Command handler & UI
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.command("merge") & filters.private)
async def merge_cmd(bot, message):
    user_id = message.from_user.id

    if not _check_ffmpeg():
        return await message.reply(
            "<b>❌ FFmpeg is not installed on this server.</b>\n\n"
            "<i>Install it: <code>sudo apt install ffmpeg</code></i>"
        )

    jobs = await _mg_list(user_id)
    active = [j for j in jobs if j.get("status") in ("downloading", "merging", "uploading")]

    buttons = []
    if active:
        buttons.append([InlineKeyboardButton("━━━ 🔄 Active Merges ━━━", callback_data="merge#noop")])
        for j in active:
            st = {"downloading": "⬇️", "merging": "🔀", "uploading": "⬆️"}.get(j["status"], "❓")
            prog = j.get("downloaded", 0)
            label = f"{st} {j.get('output_name', j['job_id'][-6:])} [{prog} files]"
            buttons.append([InlineKeyboardButton(label, callback_data=f"merge#view_{j['job_id']}")])

    recent = [j for j in jobs if j.get("status") in ("done", "error", "stopped")][:5]
    if recent:
        buttons.append([InlineKeyboardButton("━━━ 📜 Recent ━━━", callback_data="merge#noop")])
        for j in recent:
            st = {"done": "✅", "error": "⚠️", "stopped": "🔴"}.get(j["status"], "❓")
            label = f"{st} {j.get('output_name', j['job_id'][-6:])}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"merge#view_{j['job_id']}")])

    buttons.append([InlineKeyboardButton("➕ New Merge Job", callback_data="merge#create")])
    buttons.append([InlineKeyboardButton("⫷ Close", callback_data="merge#close")])

    text = (
        "<b>🔀 Merger — v2</b>\n\n"
        "<b>Merge any media files from a channel range into one file.</b>\n\n"
        "🎵 <i>Audio: MP3, AAC, OGG, FLAC, WAV, AIFF, M4A, ALAC, OPUS</i>\n"
        "🎬 <i>Video: MP4, MKV, AVI, WEBM, MOV, FLV</i>\n\n"
        "✅ Strict order • No format skipping • Full metadata\n"
        "📊 Progress bar • Multi-merge • Channel destinations"
    )

    await message.reply(text, reply_markup=InlineKeyboardMarkup(buttons))


@Client.on_callback_query(filters.regex(r'^merge#'))
async def merge_callback(bot, query):
    user_id = query.from_user.id
    action = query.data.split("#", 1)[1]

    if action == "noop":
        return await query.answer()

    elif action == "close":
        return await query.message.delete()

    elif action.startswith("view_"):
        job_id = action.split("_", 1)[1]
        job = await _mg_get(job_id)
        if not job:
            return await query.answer("Job not found!", show_alert=True)

        st_map = {"downloading": "⬇️", "merging": "🔀", "uploading": "⬆️",
                   "done": "✅", "error": "⚠️", "stopped": "🔴"}
        st = st_map.get(job["status"], "❓")

        text = (
            f"<b>{st} Merge Job Details</b>\n\n"
            f"<b>Output:</b> <code>{job.get('output_name', '?')}</code>\n"
            f"<b>Range:</b> {job.get('start_id')} → {job.get('end_id')}\n"
            f"<b>Downloaded:</b> {job.get('downloaded', 0)} files\n"
            f"<b>Status:</b> {job['status']}\n"
        )

        meta = job.get("metadata", {})
        if meta:
            meta_lines = [f"  {k}: {v}" for k, v in meta.items() if v]
            if meta_lines:
                text += "\n<b>Metadata:</b>\n" + "\n".join(meta_lines) + "\n"

        dests = job.get("dest_chats", [])
        if dests:
            text += f"\n<b>Destinations:</b> {len(dests)} channel(s)\n"

        if job.get("error"):
            text += f"\n<b>Error:</b> <code>{job['error'][:200]}</code>"

        buttons = []
        if job["status"] in ("downloading", "merging", "uploading"):
            buttons.append([InlineKeyboardButton("🛑 Stop", callback_data=f"merge#stop_{job_id}")])
        buttons.append([InlineKeyboardButton("🗑 Delete", callback_data=f"merge#del_{job_id}")])
        buttons.append([InlineKeyboardButton("↩ Back", callback_data="merge#back")])

        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))

    elif action.startswith("stop_"):
        job_id = action.split("_", 1)[1]
        await _mg_update(job_id, status="stopped")
        task = _merge_tasks.get(job_id)
        if task:
            task.cancel()
        await query.answer("Merge job stopped!", show_alert=True)

    elif action.startswith("del_"):
        job_id = action.split("_", 1)[1]
        task = _merge_tasks.get(job_id)
        if task:
            task.cancel()
        await _mg_delete(job_id)
        work_dir = f"merge_tmp/{job_id}"
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
        await query.answer("Job deleted!", show_alert=True)

    elif action == "back":
        # Re-show the main merge menu
        jobs = await _mg_list(user_id)
        active = [j for j in jobs if j.get("status") in ("downloading", "merging", "uploading")]
        buttons = []
        if active:
            buttons.append([InlineKeyboardButton("━━━ 🔄 Active ━━━", callback_data="merge#noop")])
            for j in active:
                st = {"downloading": "⬇️", "merging": "🔀", "uploading": "⬆️"}.get(j["status"], "❓")
                buttons.append([InlineKeyboardButton(
                    f"{st} {j.get('output_name', j['job_id'][-6:])}",
                    callback_data=f"merge#view_{j['job_id']}")])

        recent = [j for j in jobs if j.get("status") in ("done", "error", "stopped")][:5]
        if recent:
            buttons.append([InlineKeyboardButton("━━━ 📜 Recent ━━━", callback_data="merge#noop")])
            for j in recent:
                st = {"done": "✅", "error": "⚠️", "stopped": "🔴"}.get(j["status"], "❓")
                buttons.append([InlineKeyboardButton(
                    f"{st} {j.get('output_name', j['job_id'][-6:])}",
                    callback_data=f"merge#view_{j['job_id']}")])

        buttons.append([InlineKeyboardButton("➕ New Merge Job", callback_data="merge#create")])
        buttons.append([InlineKeyboardButton("⫷ Close", callback_data="merge#close")])
        try:
            await query.message.edit_text(
                "<b>🔀 Merger — v2</b>\n\n<i>Select a job or create a new one.</i>",
                reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            pass

    elif action == "create":
        await query.message.delete()
        await _create_merge_flow(bot, user_id)


# ══════════════════════════════════════════════════════════════════════════════
# Creation flow (7 steps)
# ══════════════════════════════════════════════════════════════════════════════

async def _create_merge_flow(bot, user_id):
    """Interactive flow to create a new merge job."""
    try:
        # ── Step 1: Select Account ────────────────────────────────────────
        accounts = await db.get_bots(user_id)
        if not accounts:
            return await bot.send_message(
                user_id,
                "<b>❌ No accounts found. Add one in /settings → Accounts first.</b>")

        kb = []
        for acc in accounts:
            icon = "🤖" if acc.get("is_bot", True) else "👤"
            kb.append([f"{icon} {acc['name']}"])
        kb.append(["❌ Cancel"])

        msg = await _merge_ask(
            bot, user_id,
            "<b>🔀 New Merge Job</b>\n\n"
            "<b>Step 1/7:</b> Select an account to use:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True)
        )

        if not msg.text or msg.text == "❌ Cancel":
            return await bot.send_message(user_id, "<b>Cancelled.</b>",
                                          reply_markup=ReplyKeyboardRemove())

        sel_name = msg.text.split(" ", 1)[1] if " " in msg.text else msg.text
        sel_acc = next((a for a in accounts if a["name"] == sel_name), None)
        if not sel_acc:
            return await bot.send_message(user_id, "<b>❌ Account not found.</b>",
                                          reply_markup=ReplyKeyboardRemove())

        # ── Step 2: Start file link ───────────────────────────────────────
        msg = await _merge_ask(
            bot, user_id,
            "<b>Step 2/7:</b> Send the <b>start file link</b> from the source channel.\n\n"
            "<i>Example: https://t.me/c/123456/100\n"
            "Or raw message ID if channel is known.</i>",
            reply_markup=ReplyKeyboardRemove()
        )
        if not msg.text or msg.text.lower() == "/cancel":
            return await bot.send_message(user_id, "<b>Cancelled.</b>")

        chat_ref_start, start_id = _parse_msg_link(msg.text)
        if start_id is None:
            return await bot.send_message(user_id, "<b>❌ Could not parse message link.</b>")

        from_chat = None
        if chat_ref_start:
            if isinstance(chat_ref_start, int):
                from_chat = -1000000000000 - chat_ref_start
            else:
                from_chat = chat_ref_start

        # ── Step 3: End file link ─────────────────────────────────────────
        msg = await _merge_ask(
            bot, user_id,
            "<b>Step 3/7:</b> Send the <b>end file link</b> (last file to include).\n\n"
            "<i>All files between start → end will be merged in exact order.</i>"
        )
        if not msg.text or msg.text.lower() == "/cancel":
            return await bot.send_message(user_id, "<b>Cancelled.</b>")

        chat_ref_end, end_id = _parse_msg_link(msg.text)
        if end_id is None:
            return await bot.send_message(user_id, "<b>❌ Could not parse end link.</b>")

        if from_chat is None and chat_ref_end:
            if isinstance(chat_ref_end, int):
                from_chat = -1000000000000 - chat_ref_end
            else:
                from_chat = chat_ref_end

        if from_chat is None:
            return await bot.send_message(user_id,
                "<b>❌ Could not determine source channel. Use full message links.</b>")

        if start_id > end_id:
            start_id, end_id = end_id, start_id

        total = end_id - start_id + 1

        # ── Step 4: Destination channel ───────────────────────────────────
        channels = await db.get_user_channels(user_id)
        dest_chats = []

        if channels:
            ch_kb = []
            for ch in channels:
                ch_kb.append([f"📢 {ch['title']}"])
            ch_kb.append(["⏭ Skip (send to me only)"])
            ch_kb.append(["❌ Cancel"])

            msg = await _merge_ask(
                bot, user_id,
                f"<b>Step 4/7:</b> Select <b>destination channel</b> for the merged file.\n\n"
                f"<b>Range:</b> {start_id} → {end_id} ({total} messages)\n\n"
                f"<i>The merged file will also be sent to you in DM.</i>",
                reply_markup=ReplyKeyboardMarkup(ch_kb, resize_keyboard=True, one_time_keyboard=True)
            )

            if not msg.text or msg.text == "❌ Cancel":
                return await bot.send_message(user_id, "<b>Cancelled.</b>",
                                              reply_markup=ReplyKeyboardRemove())

            if "Skip" not in msg.text:
                ch_title = msg.text.replace("📢 ", "").strip()
                sel_ch = next((c for c in channels if c["title"] == ch_title), None)
                if sel_ch:
                    dest_chats.append(int(sel_ch["chat_id"]))
        else:
            await bot.send_message(
                user_id,
                "<b>Step 4/7:</b> No destination channels configured.\n"
                "<i>Merged file will be sent to you in DM.</i>",
                reply_markup=ReplyKeyboardRemove()
            )
            await asyncio.sleep(1)

        # ── Step 5: Output filename ───────────────────────────────────────
        msg = await _merge_ask(
            bot, user_id,
            f"<b>Step 5/7:</b> Send the <b>output filename</b> (without extension).\n\n"
            f"<i>Example: My_Audiobook_Part1</i>",
            reply_markup=ReplyKeyboardRemove()
        )
        if not msg.text or msg.text.lower() == "/cancel":
            return await bot.send_message(user_id, "<b>Cancelled.</b>")

        output_name = re.sub(r'[<>:"/\\|?*]', '_', msg.text.strip())

        # ── Step 6: Full metadata ─────────────────────────────────────────
        msg = await _merge_ask(
            bot, user_id,
            "<b>Step 6/7:</b> Send <b>metadata</b> (optional).\n\n"
            "<i>Send each field on a new line:\n"
            "<code>title: My Song\n"
            "artist: Artist Name\n"
            "album: Album Name\n"
            "genre: Pop\n"
            "year: 2024\n"
            "track: 1\n"
            "composer: Composer\n"
            "comment: My comment</code>\n\n"
            "Or send <code>skip</code> to use defaults.</i>"
        )

        metadata = {}
        if msg.text and msg.text.lower() not in ("skip", "/cancel"):
            for line in msg.text.strip().split("\n"):
                line = line.strip()
                if ":" in line:
                    key, val = line.split(":", 1)
                    key = key.strip().lower()
                    val = val.strip()
                    if key and val:
                        # Map common names to FFmpeg metadata keys
                        key_map = {
                            "title": "title", "artist": "artist",
                            "album": "album", "genre": "genre",
                            "year": "date", "date": "date",
                            "track": "track", "track_number": "track",
                            "composer": "composer", "comment": "comment",
                            "album_artist": "album_artist",
                            "description": "description",
                            "language": "language", "publisher": "publisher",
                            "performer": "performer", "copyright": "copyright",
                            "encoded_by": "encoded_by", "lyrics": "lyrics",
                        }
                        ffmpeg_key = key_map.get(key, key)
                        metadata[ffmpeg_key] = val

        # ── Step 7: Confirm ───────────────────────────────────────────────
        meta_preview = ""
        if metadata:
            meta_lines = [f"  {k}: {v}" for k, v in list(metadata.items())[:6]]
            meta_preview = "\n".join(meta_lines)
            if len(metadata) > 6:
                meta_preview += f"\n  ... +{len(metadata) - 6} more"

        dest_preview = "DM only"
        if dest_chats:
            dest_names = []
            for dc in dest_chats:
                ch = next((c for c in channels if int(c["chat_id"]) == dc), None)
                dest_names.append(ch["title"] if ch else str(dc))
            dest_preview = ", ".join(dest_names)

        confirm_kb = [["✅ Start Merge"], ["❌ Cancel"]]
        msg = await _merge_ask(
            bot, user_id,
            f"<b>Step 7/7: Confirm</b>\n\n"
            f"<b>Source:</b> <code>{from_chat}</code>\n"
            f"<b>Range:</b> {start_id} → {end_id} ({total} msgs)\n"
            f"<b>Output:</b> <code>{output_name}</code>\n"
            f"<b>Destination:</b> {dest_preview}\n"
            + (f"\n<b>Metadata:</b>\n{meta_preview}\n" if meta_preview else "") +
            f"\n<i>⚠️ All media files in this range will be downloaded and merged.\n"
            f"No file will be skipped regardless of format.</i>",
            reply_markup=ReplyKeyboardMarkup(confirm_kb, resize_keyboard=True, one_time_keyboard=True)
        )

        if not msg.text or "Cancel" in msg.text:
            return await bot.send_message(user_id, "<b>Cancelled.</b>",
                                          reply_markup=ReplyKeyboardRemove())

        # ── Create & start job ────────────────────────────────────────────
        job_id = str(uuid.uuid4())
        job = {
            "job_id": job_id,
            "user_id": user_id,
            "account_id": sel_acc["id"],
            "from_chat": from_chat,
            "start_id": start_id,
            "end_id": end_id,
            "output_name": output_name,
            "metadata": metadata,
            "dest_chats": dest_chats,
            "status": "downloading",
            "downloaded": 0,
            "error": "",
            "created_at": time.time(),
        }
        await _mg_save(job)

        task = asyncio.create_task(_run_merge_job(job_id, user_id, bot))
        _merge_tasks[job_id] = task

        await bot.send_message(
            user_id,
            f"<b>✅ Merge Job Created & Started!</b>\n\n"
            f"<b>Range:</b> {start_id} → {end_id} ({total} msgs)\n"
            f"<b>Output:</b> <code>{output_name}</code>\n"
            f"<b>Destination:</b> {dest_preview}\n"
            f"<b>Job ID:</b> <code>{job_id[-6:]}</code>\n\n"
            f"<i>Use /merge to monitor progress.\n"
            f"Multiple merge jobs can run simultaneously.</i>",
            reply_markup=ReplyKeyboardRemove()
        )

    except asyncio.TimeoutError:
        await bot.send_message(user_id, "<b>⏱ Timed out. Try /merge again.</b>",
                               reply_markup=ReplyKeyboardRemove())
    except Exception as e:
        logger.error(f"[Merge create] Error: {e}")
        await bot.send_message(user_id, f"<b>❌ Error:</b> <code>{e}</code>",
                               reply_markup=ReplyKeyboardRemove())
