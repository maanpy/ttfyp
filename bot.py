"""
TikTok FYP Scraper — Telegram Bot Controller
=============================================
Control your TikTok scraper entirely from Telegram.
Deployed on Railway.
"""

import os
import csv
import re
import time
import asyncio
import logging
import threading
from io import StringIO
from datetime import datetime
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── ENV CONFIG ───────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USERS_RAW = os.environ.get("ALLOWED_USERS", "")  # comma-separated telegram user IDs
ALLOWED_USERS = set(
    int(x.strip()) for x in ALLOWED_USERS_RAW.split(",") if x.strip()
)

def load_tiktok_cookies() -> list[dict] | None:
    """Load TikTok cookies from TIKTOK_COOKIES env var (JSON array)."""
    raw = os.environ.get("TIKTOK_COOKIES", "").strip()
    if not raw:
        return None
    try:
        import json
        cookies = json.loads(raw)
        # Normalize: Playwright needs 'sameSite' as title-case
        samesite_map = {"strict": "Strict", "lax": "Lax", "no_restriction": "None", "none": "None"}
        for c in cookies:
            # Remove keys Playwright doesn't accept
            for key in ["hostOnly", "session", "storeId", "id"]:
                c.pop(key, None)
            # Fix sameSite value
            if "sameSite" in c:
                c["sameSite"] = samesite_map.get(c["sameSite"].lower(), "Lax")
            else:
                c["sameSite"] = "Lax"
            # Ensure domain is correct
            if not c.get("domain", "").endswith("tiktok.com"):
                c["domain"] = ".tiktok.com"
        logger.info(f"Loaded {len(cookies)} TikTok cookies from env.")
        return cookies
    except Exception as e:
        logger.warning(f"Failed to parse TIKTOK_COOKIES: {e}")
        return None

TIKTOK_COOKIES = load_tiktok_cookies()
# ──────────────────────────────────────────────────────────────────────────────

# Per-user scrape state
user_state: dict[int, dict] = defaultdict(lambda: {
    "running": False,
    "target": 50,
    "pause": 2.5,
    "results": [],
    "thread": None,
    "stop_event": None,
})

TIKTOK_VIDEO_PATTERN = re.compile(
    r"https://www\.tiktok\.com/@[\w.]+/video/\d+"
)

# ─── AUTH HELPER ──────────────────────────────────────────────────────────────
def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True  # open to all if no whitelist set
    return user_id in ALLOWED_USERS

def auth_required(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not is_allowed(uid):
            await update.effective_message.reply_text("⛔ You are not authorized to use this bot.")
            return
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper

# ─── SCRAPER (runs in background thread) ──────────────────────────────────────
def run_scraper(user_id: int, target: int, pause: float,
                stop_event: threading.Event,
                on_progress, on_done, on_error):
    try:
        from playwright.sync_api import sync_playwright
        import json as _json

        collected = []
        seen = set()
        api_urls = []  # collected from network interception

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ]
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                java_script_enabled=True,
                locale="en-US",
                timezone_id="America/New_York",
            )

            # Hide automation fingerprints
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                window.chrome = { runtime: {} };
            """)

            # ── Intercept TikTok API responses ──────────────────────────────
            def handle_response(response):
                try:
                    url = response.url
                    # TikTok FYP API endpoint
                    if "recommend/item_list" in url or "feed" in url or "aweme/v1" in url:
                        try:
                            body = response.json()
                            # TikTok API returns aweme_list with video objects
                            items = (
                                body.get("aweme_list") or
                                body.get("itemList") or
                                body.get("item_list") or
                                []
                            )
                            for item in items:
                                aweme_id = (
                                    item.get("aweme_id") or
                                    item.get("id") or
                                    item.get("video", {}).get("id")
                                )
                                # Try every possible field for the real username
                                author_obj = item.get("author") or {}
                                author = (
                                    author_obj.get("unique_id") or        # e.g. "charlidamelio"
                                    author_obj.get("nickname") or         # display name fallback
                                    item.get("authorMeta", {}).get("name") or
                                    item.get("music", {}).get("author") or
                                    None
                                )
                                if aweme_id and author and author != "user":
                                    video_url = f"https://www.tiktok.com/@{author}/video/{aweme_id}"
                                    api_urls.append(video_url)
                                elif aweme_id:
                                    # Store just the ID — we'll resolve username from page HTML
                                    api_urls.append(f"__ID__{aweme_id}")
                        except Exception:
                            pass
                except Exception:
                    pass

            page = context.new_page()
            page.on("response", handle_response)

            # Inject cookies
            if TIKTOK_COOKIES:
                context.add_cookies(TIKTOK_COOKIES)
                logger.info("Cookies injected.")

            logger.info("Loading TikTok FYP...")
            page.goto("https://www.tiktok.com/foryou",
                      wait_until="domcontentloaded", timeout=45_000)
            time.sleep(10)

            # Dismiss popups
            for selector in [
                "button:has-text('Accept all')",
                "button:has-text('I am 18+')",
                "[data-e2e='cookie-banner-accept']",
                "[data-e2e='modal-close-inner-button']",
            ]:
                try:
                    page.click(selector, timeout=2000)
                    time.sleep(0.5)
                except Exception:
                    pass

            # Wait for the FYP video container to appear
            try:
                page.wait_for_selector(
                    "[class*='DivItemContainer'], [class*='video-feed'], a[href*='/video/']",
                    timeout=15_000
                )
                logger.info("Video container found on page")
                time.sleep(3)
            except Exception:
                logger.warning("Video container not found - proceeding anyway")

            scroll_attempts = 0
            max_attempts = target * 5
            last_count = 0
            stuck_count = 0

            while len(collected) < target and scroll_attempts < max_attempts:
                if stop_event.is_set():
                    break

                # ── Method 1: From intercepted API calls ──────────────────
                pending_ids = []
                for url in list(api_urls):
                    if url.startswith("__ID__"):
                        pending_ids.append(url[6:])
                        continue
                    clean = url.split("?")[0]
                    if clean not in seen and TIKTOK_VIDEO_PATTERN.match(clean):
                        seen.add(clean)
                        collected.append({
                            "index": len(collected) + 1,
                            "url": clean,
                            "scraped_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                        })
                        if len(collected) % 10 == 0 or len(collected) == target:
                            on_progress(len(collected), target)
                        if len(collected) >= target:
                            break
                api_urls.clear()

                # Resolve any bare video IDs by finding them in page HTML
                if pending_ids:
                    page_html_for_ids = page.content()
                    for vid_id in pending_ids:
                        # Find the matching full URL in page source
                        import re as _re
                        pattern = r'https://www\.tiktok\.com/@([\w\.]+)/video/' + vid_id
                        match = _re.search(pattern,
                            page_html_for_ids
                        )
                        if match:
                            clean = f"https://www.tiktok.com/@{match.group(1)}/video/{vid_id}"
                        else:
                            # Last resort: use the video ID with a placeholder we can note
                            clean = f"https://www.tiktok.com/video/{vid_id}"
                        if clean not in seen:
                            seen.add(clean)
                            collected.append({
                                "index": len(collected) + 1,
                                "url": clean,
                                "scraped_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                            })
                            if len(collected) % 10 == 0 or len(collected) == target:
                                on_progress(len(collected), target)

                # ── Method 2: Scan page HTML ──────────────────────────────
                page_html = page.content()
                for url in TIKTOK_VIDEO_PATTERN.findall(page_html):
                    clean = url.split("?")[0]
                    if clean not in seen:
                        seen.add(clean)
                        collected.append({
                            "index": len(collected) + 1,
                            "url": clean,
                            "scraped_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                        })
                        if len(collected) % 10 == 0 or len(collected) == target:
                            on_progress(len(collected), target)
                        if len(collected) >= target:
                            break

                # ── Method 3: Anchor tags ──────────────────────────────────
                try:
                    links = page.eval_on_selector_all(
                        "a[href*='/video/']",
                        "els => els.map(e => e.href)"
                    )
                    for url in links:
                        clean = url.split("?")[0]
                        if clean not in seen and TIKTOK_VIDEO_PATTERN.match(clean):
                            seen.add(clean)
                            collected.append({
                                "index": len(collected) + 1,
                                "url": clean,
                                "scraped_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                            })
                            if len(collected) % 10 == 0 or len(collected) == target:
                                on_progress(len(collected), target)
                            if len(collected) >= target:
                                break
                except Exception:
                    pass

                # Detect if stuck
                if len(collected) == last_count:
                    stuck_count += 1
                else:
                    stuck_count = 0
                last_count = len(collected)

                # TikTok FYP is a vertical video player - use arrow key like a real user
                try:
                    # Click center of page first to make sure it has focus
                    if scroll_attempts == 0:
                        page.mouse.click(640, 450)
                        time.sleep(1)
                    # Press down arrow to go to next video
                    page.keyboard.press("ArrowDown")
                except Exception:
                    page.evaluate("window.scrollBy(0, window.innerHeight)")

                time.sleep(max(pause, 3.0))  # wait for next video to load

                # Every 10 videos also do a scroll just in case
                if scroll_attempts % 10 == 0:
                    page.evaluate("window.scrollBy(0, window.innerHeight)")
                    time.sleep(1)

                # If stuck for 15 attempts, try clicking the down arrow button on screen
                if stuck_count == 15:
                    logger.warning(f"Stuck at {len(collected)} results after {scroll_attempts} scrolls")
                    try:
                        # Try the on-screen next video button
                        page.click("[data-e2e='arrow-down'], [class*='ButtonDown'], .swiper-button-next", timeout=2000)
                        time.sleep(2)
                    except Exception:
                        pass

                scroll_attempts += 1

            # Debug screenshot if 0 results
            if not collected:
                try:
                    page.screenshot(path="/tmp/tiktok_debug.png", full_page=False)
                    logger.warning("0 results — screenshot saved to /tmp/tiktok_debug.png — use /debug command")
                except Exception:
                    pass

            browser.close()

        user_state[user_id]["results"] = collected
        on_done(collected)

    except Exception as e:
        logger.exception("Scraper error")
        on_error(str(e))


# ─── BOT COMMANDS ─────────────────────────────────────────────────────────────

@auth_required
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.first_name
    text = (
        f"👋 Welcome, *{uid}*!\n\n"
        "🎵 *TikTok FYP Scraper Bot*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Use the commands below to control the scraper:\n\n"
        "▶️ /scrape — Start scraping\n"
        "⏹ /stop — Stop current scrape\n"
        "⚙️ /settings — View & change settings\n"
        "📥 /download — Download last results\n"
        "📊 /status — Check scraper status\n"
        "❓ /help — Show this message\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@auth_required
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


@auth_required
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_state[uid]
    if state["running"]:
        count = len(state["results"])
        target = state["target"]
        pct = int(count / target * 100) if target else 0
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        msg = (
            f"🟢 *Scraper is running*\n\n"
            f"Progress: `{bar}` {pct}%\n"
            f"Collected: {count} / {target} videos\n\n"
            f"Use /stop to halt."
        )
    else:
        count = len(state["results"])
        msg = (
            f"⚪ *Scraper is idle*\n\n"
            f"Last run collected: {count} videos\n"
            f"Use /scrape to start a new run."
        )
    await update.message.reply_text(msg, parse_mode="Markdown")


@auth_required
async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_state[uid]
    keyboard = [
        [
            InlineKeyboardButton("🎯 Target: 25",  callback_data="set_target_25"),
            InlineKeyboardButton("🎯 Target: 50",  callback_data="set_target_50"),
            InlineKeyboardButton("🎯 Target: 100", callback_data="set_target_100"),
        ],
        [
            InlineKeyboardButton("⏱ Pause: 1.5s", callback_data="set_pause_1.5"),
            InlineKeyboardButton("⏱ Pause: 2.5s", callback_data="set_pause_2.5"),
            InlineKeyboardButton("⏱ Pause: 4s",   callback_data="set_pause_4"),
        ],
        [
            InlineKeyboardButton("📄 Format: CSV",  callback_data="set_fmt_csv"),
            InlineKeyboardButton("📄 Format: TXT",  callback_data="set_fmt_txt"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = (
        f"⚙️ *Settings*\n\n"
        f"• Target videos: `{state['target']}`\n"
        f"• Scroll pause: `{state['pause']}s`\n"
        f"• Output format: `{state.get('fmt', 'csv').upper()}`\n\n"
        f"Tap a button to change:"
    )
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=reply_markup)


@auth_required
async def cmd_scrape(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_state[uid]

    if state["running"]:
        await update.message.reply_text(
            "⚠️ Scraper is already running! Use /stop to halt it first."
        )
        return

    target = state["target"]
    pause = state["pause"]

    state["running"] = True
    state["results"] = []
    stop_event = threading.Event()
    state["stop_event"] = stop_event

    await update.message.reply_text(
        f"🚀 *Scraper started!*\n\n"
        f"🎯 Target: {target} videos\n"
        f"⏱ Scroll pause: {pause}s\n\n"
        f"I'll update you every 10 videos. Use /stop to cancel.",
        parse_mode="Markdown"
    )

    loop = asyncio.get_event_loop()

    def on_progress(count, total):
        asyncio.run_coroutine_threadsafe(
            ctx.bot.send_message(
                chat_id=uid,
                text=f"📊 Progress: *{count}/{total}* videos collected...",
                parse_mode="Markdown",
            ),
            loop,
        )

    def on_done(results):
        state["running"] = False
        asyncio.run_coroutine_threadsafe(
            ctx.bot.send_message(
                chat_id=uid,
                text=(
                    f"✅ *Scrape complete!*\n\n"
                    f"Collected *{len(results)}* video links.\n"
                    f"Use /download to get the file."
                ),
                parse_mode="Markdown",
            ),
            loop,
        )

    def on_error(err):
        state["running"] = False
        asyncio.run_coroutine_threadsafe(
            ctx.bot.send_message(
                chat_id=uid,
                text=f"❌ *Scraper error:*\n`{err}`",
                parse_mode="Markdown",
            ),
            loop,
        )

    t = threading.Thread(
        target=run_scraper,
        args=(uid, target, pause, stop_event, on_progress, on_done, on_error),
        daemon=True,
    )
    state["thread"] = t
    t.start()


@auth_required
async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_state[uid]

    if not state["running"]:
        await update.message.reply_text("ℹ️ No scraper is currently running.")
        return

    if state["stop_event"]:
        state["stop_event"].set()

    state["running"] = False
    count = len(state["results"])
    await update.message.reply_text(
        f"⏹ *Scraper stopped.*\n\n"
        f"Collected {count} videos so far.\n"
        f"Use /download to grab the partial results.",
        parse_mode="Markdown"
    )


@auth_required
async def cmd_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_state[uid]
    results = state["results"]

    if not results:
        await update.message.reply_text(
            "📭 No results yet. Run /scrape first!"
        )
        return

    fmt = state.get("fmt", "csv")

    if fmt == "csv":
        buf = StringIO()
        writer = csv.DictWriter(buf, fieldnames=["index", "url", "scraped_at"])
        writer.writeheader()
        writer.writerows(results)
        buf.seek(0)
        filename = f"tiktok_fyp_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
        await update.message.reply_document(
            document=buf.getvalue().encode("utf-8"),
            filename=filename,
            caption=f"🎵 {len(results)} TikTok video links — CSV format",
        )
    else:
        content = "\n".join(r["url"] for r in results)
        filename = f"tiktok_fyp_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
        await update.message.reply_document(
            document=content.encode("utf-8"),
            filename=filename,
            caption=f"🎵 {len(results)} TikTok video links — TXT format",
        )


# ─── INLINE KEYBOARD CALLBACKS ────────────────────────────────────────────────
@auth_required
async def cmd_cookiestatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show whether TikTok cookies are loaded."""
    if TIKTOK_COOKIES:
        names = [c.get("name", "?") for c in TIKTOK_COOKIES[:8]]
        await update.message.reply_text(
            f"🍪 *Cookies loaded:* {len(TIKTOK_COOKIES)} cookies\n"
            f"Keys: `{', '.join(names)}{'...' if len(TIKTOK_COOKIES) > 8 else ''}`\n\n"
            f"✅ Bot will log in automatically when scraping.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "⚠️ *No cookies loaded.*\n\n"
            "Scraper will run without login — TikTok may show a login wall.\n\n"
            "To add cookies:\n"
            "1️⃣ Log into tiktok.com in Chrome\n"
            "2️⃣ Install *Cookie-Editor* extension\n"
            "3️⃣ Export as JSON\n"
            "4️⃣ Add `TIKTOK_COOKIES` env var in Railway with the JSON value\n"
            "5️⃣ Redeploy",
            parse_mode="Markdown"
        )


async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    await query.answer()

    if not is_allowed(uid):
        return

    data = query.data
    state = user_state[uid]

    if data.startswith("set_target_"):
        val = int(data.split("_")[-1])
        state["target"] = val
        await query.edit_message_text(f"✅ Target set to *{val}* videos.", parse_mode="Markdown")

    elif data.startswith("set_pause_"):
        val = float(data.split("_")[-1])
        state["pause"] = val
        await query.edit_message_text(f"✅ Scroll pause set to *{val}s*.", parse_mode="Markdown")

    elif data.startswith("set_fmt_"):
        val = data.split("_")[-1]
        state["fmt"] = val
        await query.edit_message_text(f"✅ Output format set to *{val.upper()}*.", parse_mode="Markdown")


@auth_required
async def cmd_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Take a screenshot of what TikTok shows the bot and send it."""
    uid = update.effective_user.id
    await update.message.reply_text("📸 Taking a screenshot of TikTok... please wait ~15s")

    def take_screenshot():
        try:
            from playwright.sync_api import sync_playwright
            import base64

            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu"]
                )
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 900},
                )
                context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
                if TIKTOK_COOKIES:
                    context.add_cookies(TIKTOK_COOKIES)
                page = context.new_page()
                page.goto("https://www.tiktok.com/foryou", wait_until="domcontentloaded", timeout=30_000)
                import time
                time.sleep(5)
                path = "/tmp/tiktok_debug.png"
                page.screenshot(path=path, full_page=False)
                browser.close()
            return path
        except Exception as e:
            return str(e)

    loop = asyncio.get_event_loop()

    def run():
        result = take_screenshot()
        async def send():
            if result.endswith(".png"):
                with open(result, "rb") as f:
                    await ctx.bot.send_photo(
                        chat_id=uid,
                        photo=f,
                        caption="🖥 This is what TikTok shows the scraper. If you see a login wall or CAPTCHA, cookies may have expired."
                    )
            else:
                await ctx.bot.send_message(chat_id=uid, text=f"❌ Screenshot failed: {result}")
        asyncio.run_coroutine_threadsafe(send(), loop)

    threading.Thread(target=run, daemon=True).start()


# ─── UNKNOWN COMMAND ──────────────────────────────────────────────────────────
async def unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ Unknown command. Use /help to see available commands."
    )


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("scrape",   cmd_scrape))
    app.add_handler(CommandHandler("stop",     cmd_stop))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("download", cmd_download))
    app.add_handler(CommandHandler("cookies",  cmd_cookiestatus))
    app.add_handler(CommandHandler("debug",    cmd_debug))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info("Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
