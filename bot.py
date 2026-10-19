import os
import re
import json
import uuid
import shutil
import logging
import asyncio
from html import escape
from datetime import datetime, timedelta

import yt_dlp
import requests
import instaloader
from urllib.parse import urlparse
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Load environment variables
load_dotenv()

# ==================== CONFIGURATION ====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
COOKIES_FILE = os.getenv("COOKIES_FILE")  # optional: path to Instagram cookies.txt

# For Railway: store the cookies file as a base64 env var (IG_COOKIES_B64) instead of committing it
_cookies_b64 = os.getenv("IG_COOKIES_B64")
if _cookies_b64:
    import base64
    COOKIES_FILE = COOKIES_FILE or "cookies.txt"
    with open(COOKIES_FILE, "wb") as _f:
        _f.write(base64.b64decode(_cookies_b64))
DOWNLOAD_DIR = "downloads"
LINKS_LOG_FILE = os.getenv("LINKS_LOG_FILE", "links_log.json")  # point at a volume on Railway
MAX_FILE_SIZE = 50 * 1024 * 1024  # Telegram bot upload limit

# ==================== LOGGING SETUP ====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx logs full request URLs, which include the bot token. Silence it.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ==================== INSTAGRAM PATTERN ====================
INSTAGRAM_RE = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/(p|reel|reels)/([A-Za-z0-9_-]+)"
)


def extract_instagram(text: str):
    """Return (clean_url, shortcode) or (None, None)."""
    match = INSTAGRAM_RE.search(text)
    if not match:
        return None, None
    kind, shortcode = match.group(1), match.group(2)
    return f"https://www.instagram.com/{kind}/{shortcode}/", shortcode


# ==================== LOG FUNCTIONS ====================
def load_links_log() -> list:
    try:
        with open(LINKS_LOG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_link_log(entry: dict) -> None:
    logs = load_links_log()
    logs.append(entry)
    logs = logs[-5000:]
    try:
        with open(LINKS_LOG_FILE, "w") as f:
            json.dump(logs, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save log: {e}")


def is_admin(user_id: int) -> bool:
    return ADMIN_USER_ID != 0 and user_id == ADMIN_USER_ID


# ==================== DOWNLOAD FUNCTION ====================
def download_instaloader(shortcode: str, out_dir: str) -> list:
    """Handles photos, videos and mixed carousels. Blocking."""
    os.makedirs(out_dir, exist_ok=True)
    try:
        # max_connection_attempts=1: fail fast instead of hammering Instagram on 401s
        L = instaloader.Instaloader(
            quiet=True, save_metadata=False, max_connection_attempts=1
        )

        ig_user = os.getenv("IG_USERNAME")
        session_file = os.getenv("IG_SESSION_FILE")

        if COOKIES_FILE and os.path.exists(COOKIES_FILE):
            # Reuse the same browser cookies.txt that yt-dlp uses
            from http.cookiejar import MozillaCookieJar

            jar = MozillaCookieJar(COOKIES_FILE)
            jar.load(ignore_discard=True, ignore_expires=True)
            cookies = {c.name: c.value for c in jar if "instagram.com" in c.domain}
            if cookies.get("sessionid"):
                L.context.load_session(ig_user or "cookie_user", cookies)
        elif ig_user and session_file and os.path.exists(session_file):
            L.load_session_from_file(ig_user, session_file)

        post = instaloader.Post.from_shortcode(L.context, shortcode)

        media_urls = []
        if post.typename == "GraphSidecar":
            for node in post.get_sidecar_nodes():
                media_urls.append(node.video_url if node.is_video else node.display_url)
        else:
            media_urls.append(post.video_url if post.is_video else post.url)

        files = []
        for i, media_url in enumerate(media_urls, 1):
            ext = os.path.splitext(urlparse(media_url).path)[1] or ".jpg"
            path = os.path.join(out_dir, f"{shortcode}_{i}{ext}")
            r = requests.get(media_url, timeout=60)
            r.raise_for_status()
            with open(path, "wb") as f:
                f.write(r.content)
            files.append(path)
        return files
    except Exception as e:
        logger.error(f"Instaloader error: {e}")
        return []


def download_sync(url: str, shortcode: str, out_dir: str) -> list:
    """Blocking. Run with asyncio.to_thread.
    /p/ posts (photos, carousels): instaloader first, yt-dlp as fallback.
    Reels: yt-dlp first, instaloader as fallback."""
    if "/p/" in url:
        methods = [
            lambda: download_instaloader(shortcode, out_dir),
            lambda: download_ytdlp(url, out_dir),
        ]
    else:
        methods = [
            lambda: download_ytdlp(url, out_dir),
            lambda: download_instaloader(shortcode, out_dir),
        ]

    for method in methods:
        files = method()
        if files:
            return files
    return []


def download_ytdlp(url: str, out_dir: str) -> list:
    os.makedirs(out_dir, exist_ok=True)

    ydl_opts = {
        "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
        "format": "best[filesize<50M]/best",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "noplaylist": False,
        "ignoreerrors": True,  # one failed carousel item shouldn't kill the rest
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            files = []
            if info and "entries" in info:
                for entry in info["entries"]:
                    if entry:
                        filepath = ydl.prepare_filename(entry)
                        if os.path.exists(filepath):
                            files.append(filepath)
            elif info:
                filepath = ydl.prepare_filename(info)
                if os.path.exists(filepath):
                    files.append(filepath)
            return files
    except Exception as e:
        logger.error(f"Download error: {e}")
        return []


# ==================== BOT COMMANDS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user

    welcome_text = (
        f"👋 Hello {escape(user.first_name or 'there')}!\n\n"
        f"🤖 <b>Instagram Downloader Bot</b>\n\n"
        f"Send me any <b>public</b> Instagram post URL and I'll download it!\n\n"
        f"✅ Photos\n"
        f"✅ Videos &amp; Reels\n"
        f"✅ Carousels (albums)\n\n"
        f"❌ Private accounts not supported\n\n"
        f"⚠️ <i>Note: All submitted links are logged for quality &amp; security purposes.</i>"
    )

    await update.message.reply_text(welcome_text, parse_mode="HTML")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    message_text = update.message.text.strip()
    user = update.message.from_user
    user_id = user.id
    chat = update.message.chat

    url, shortcode = extract_instagram(message_text)

    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "user_id": user_id,
        "username": user.username or "N/A",
        "first_name": user.first_name or "N/A",
        "last_name": user.last_name or "N/A",
        "link": message_text[:500],
        "shortcode": shortcode,
        "valid": shortcode is not None,
        "chat_id": chat.id,
        "chat_type": chat.type,
        "chat_title": chat.title if chat.type != "private" else "Private",
        "success": False,
    }

    logger.info(f"Link received: user {user_id} - {message_text[:50]}")

    # ==================== ADMIN NOTIFICATION ====================
    if ADMIN_USER_ID:
        try:
            admin_msg = (
                f"📊 <b>New Link Received</b>\n\n"
                f"👤 <b>User:</b> {escape(user.first_name or '')} {escape(user.last_name or '')}\n"
                f"🔗 <b>Username:</b> @{escape(user.username or 'N/A')}\n"
                f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
                f"💬 <b>Chat:</b> {escape(chat.type)}\n"
                f"📌 <b>Chat ID:</b> <code>{chat.id}</code>\n"
                f"🔗 <b>Link:</b> <code>{escape(message_text[:80])}</code>\n"
                f"✅ <b>Valid:</b> {'✓ Yes' if shortcode else '✗ No'}\n"
                f"🕐 <b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            await context.bot.send_message(
                chat_id=ADMIN_USER_ID, text=admin_msg, parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to notify admin: {e}")

    # ==================== INVALID LINK ====================
    if not shortcode:
        save_link_log(log_entry)
        await update.message.reply_text(
            "❌ <b>Invalid Instagram URL</b>\n\n"
            "Please send a valid link:\n"
            "• <code>instagram.com/p/...</code>\n"
            "• <code>instagram.com/reel/...</code>\n"
            "• <code>instagram.com/reels/...</code>",
            parse_mode="HTML",
        )
        return

    # ==================== DOWNLOAD ====================
    processing_msg = await update.message.reply_text(
        "⏳ <b>Downloading...</b>", parse_mode="HTML"
    )

    # Unique folder per request so parallel requests never clash
    request_dir = os.path.join(DOWNLOAD_DIR, f"{user_id}_{uuid.uuid4().hex[:8]}")

    try:
        files = await asyncio.to_thread(download_sync, url, shortcode, request_dir)

        if not files:
            await processing_msg.edit_text(
                "❌ <b>Download Failed</b>\n\n"
                "Possible reasons:\n"
                "• Post is private\n"
                "• Post was deleted\n"
                "• Instagram rate limit / login required\n"
                "• Invalid URL",
                parse_mode="HTML",
            )
            return

        sent = 0
        for file_path in files:
            if not os.path.exists(file_path):
                continue

            file_size = os.path.getsize(file_path)

            if file_size > MAX_FILE_SIZE:
                await update.message.reply_text(
                    f"⚠️ <b>File too large</b>\n\n"
                    f"Size: {file_size / 1024 / 1024:.1f} MB\n"
                    f"Limit: 50 MB",
                    parse_mode="HTML",
                )
                continue

            try:
                with open(file_path, "rb") as f:
                    if file_path.lower().endswith((".mp4", ".mov", ".webm", ".mkv")):
                        await update.message.reply_video(
                            video=f, caption="🎥 Video", supports_streaming=True
                        )
                    elif file_path.lower().endswith(
                        (".jpg", ".jpeg", ".png", ".webp", ".gif")
                    ):
                        await update.message.reply_photo(photo=f, caption="📷 Photo")
                    else:
                        await update.message.reply_document(document=f, caption="📎 File")
                sent += 1
            except Exception as e:
                logger.error(f"Error sending file: {e}")
                await update.message.reply_text(
                    f"❌ Error sending file: {escape(str(e)[:100])}"
                )

        try:
            await processing_msg.delete()
        except Exception:
            pass

        log_entry["success"] = sent > 0
        log_entry["files_count"] = sent

    except Exception as e:
        logger.error(f"Processing error: {e}")
        log_entry["error"] = str(e)[:200]
        try:
            await processing_msg.edit_text(
                f"❌ <b>Error</b>\n\n<code>{escape(str(e)[:200])}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass

    finally:
        shutil.rmtree(request_dir, ignore_errors=True)
        save_link_log(log_entry)  # saved exactly once per message


# ==================== ADMIN COMMANDS ====================
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.message.from_user.id):
        await update.message.reply_text("❌ Unauthorized")
        return

    logs = load_links_log()

    total_links = len(logs)
    valid_links = sum(1 for log in logs if log.get("valid"))
    successful = sum(1 for log in logs if log.get("success"))
    unique_users = len(set(log["user_id"] for log in logs))
    unique_chats = len(set(log["chat_id"] for log in logs))

    now = datetime.now()
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)
    recent_logs = [l for l in logs if datetime.fromisoformat(l["timestamp"]) > day_ago]
    week_logs = [l for l in logs if datetime.fromisoformat(l["timestamp"]) > week_ago]

    stats_msg = (
        f"📈 <b>Bot Statistics</b>\n\n"
        f"📊 <b>Total Links:</b> {total_links}\n"
        f"✅ <b>Valid Links:</b> {valid_links}\n"
        f"✅ <b>Successful Downloads:</b> {successful}\n"
        f"👥 <b>Unique Users:</b> {unique_users}\n"
        f"💬 <b>Unique Chats:</b> {unique_chats}\n\n"
        f"📅 <b>Last 24 Hours:</b> {len(recent_logs)} links\n"
        f"📅 <b>Last 7 Days:</b> {len(week_logs)} links\n\n"
        f"🕐 <b>Current Time:</b> {now.strftime('%Y-%m-%d %H:%M:%S')}"
    )

    await update.message.reply_text(stats_msg, parse_mode="HTML")


async def recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.message.from_user.id):
        await update.message.reply_text("❌ Unauthorized")
        return

    try:
        count = int(context.args[0]) if context.args else 10
        count = max(1, min(count, 20))  # keeps message under Telegram's 4096 limit
    except ValueError:
        count = 10

    logs = load_links_log()
    recent_logs = logs[-count:]

    if not recent_logs:
        await update.message.reply_text("📋 No links logged yet")
        return

    msg = f"📋 <b>Last {len(recent_logs)} Links:</b>\n\n"

    for i, log in enumerate(reversed(recent_logs), 1):
        status = "✅" if log.get("success") else ("⏳" if log.get("valid") else "❌")
        time_str = log["timestamp"][11:19]
        date_str = log["timestamp"][5:10]

        msg += (
            f"{i}. {status} <code>{escape(log['link'][:40])}</code>\n"
            f"   👤 {escape(log['first_name'])} (@{escape(log['username'])})\n"
            f"   📅 {date_str} {time_str}\n\n"
        )

    await update.message.reply_text(msg, parse_mode="HTML")


async def userinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.message.from_user.id):
        await update.message.reply_text("❌ Unauthorized")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/userinfo &lt;user_id&gt;</code>", parse_mode="HTML"
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID")
        return

    logs = load_links_log()
    user_logs = [log for log in logs if log["user_id"] == target_id]

    if not user_logs:
        await update.message.reply_text(
            f"📋 No logs found for user <code>{target_id}</code>", parse_mode="HTML"
        )
        return

    user = user_logs[-1]

    msg = (
        f"👤 <b>User Information</b>\n\n"
        f"🆔 <b>User ID:</b> <code>{target_id}</code>\n"
        f"📛 <b>Name:</b> {escape(user['first_name'])} {escape(user.get('last_name', ''))}\n"
        f"🔗 <b>Username:</b> @{escape(user['username'])}\n\n"
        f"📊 <b>Activity:</b>\n"
        f"• Total Links: {len(user_logs)}\n"
        f"• Valid Links: {sum(1 for l in user_logs if l.get('valid'))}\n"
        f"• Successful: {sum(1 for l in user_logs if l.get('success'))}\n"
        f"• First Seen: {user_logs[0]['timestamp'][:10]}\n"
        f"• Last Active: {user_logs[-1]['timestamp'][:10]}"
    )

    await update.message.reply_text(msg, parse_mode="HTML")


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.message.from_user.id):
        await update.message.reply_text("❌ Unauthorized")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/broadcast &lt;message&gt;</code>", parse_mode="HTML"
        )
        return

    message = " ".join(context.args)
    logs = load_links_log()
    unique_users = list(set(log["user_id"] for log in logs))

    status_msg = await update.message.reply_text(
        f"📢 Broadcasting to {len(unique_users)} users..."
    )

    sent = 0
    failed = 0

    for uid in unique_users:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"📢 <b>Broadcast</b>\n\n{escape(message)}",
                parse_mode="HTML",
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            failed += 1
            logger.error(f"Broadcast failed to {uid}: {e}")

    await status_msg.edit_text(
        f"📢 <b>Broadcast Complete</b>\n\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}\n"
        f"👥 Total: {len(unique_users)}",
        parse_mode="HTML",
    )


async def clearlogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.message.from_user.id):
        await update.message.reply_text("❌ Unauthorized")
        return

    if not context.args or context.args[0] != "confirm":
        await update.message.reply_text(
            "⚠️ <b>Warning:</b> This will delete all logs!\n\n"
            "To confirm, use: <code>/clearlogs confirm</code>",
            parse_mode="HTML",
        )
        return

    try:
        os.remove(LINKS_LOG_FILE)
        await update.message.reply_text("✅ All logs cleared")
    except FileNotFoundError:
        await update.message.reply_text("📋 No logs to clear")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def exportlogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.message.from_user.id):
        await update.message.reply_text("❌ Unauthorized")
        return

    if not os.path.exists(LINKS_LOG_FILE):
        await update.message.reply_text("📋 No logs to export")
        return

    try:
        count = len(load_links_log())
        with open(LINKS_LOG_FILE, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=f"instagram_bot_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                caption=f"📊 Export of {count} log entries",
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


# ==================== ERROR HANDLER ====================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error", exc_info=context.error)


# ==================== MAIN FUNCTION ====================
async def run_bot():
    if not TELEGRAM_TOKEN:
        print("❌ Error: TELEGRAM_TOKEN not set in .env")
        return

    if not ADMIN_USER_ID:
        print("⚠️ Warning: ADMIN_USER_ID not set, admin features disabled")

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Public handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Admin handlers
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("recent", recent))
    app.add_handler(CommandHandler("userinfo", userinfo))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("clearlogs", clearlogs))
    app.add_handler(CommandHandler("exportlogs", exportlogs))

    app.add_error_handler(on_error)

    print("🤖 Bot started with admin logging...")
    print(f"👑 Admin ID: {ADMIN_USER_ID}")

    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    try:
        await asyncio.Event().wait()  # run until cancelled
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        pass
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped")