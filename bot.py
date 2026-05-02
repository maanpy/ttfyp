"""
TikTok FYP Scraper Bot — Telegram Controller
Clean rebuild: simple session login, custom amount, accurate scraping
"""

import os, csv, re, time, asyncio, logging, threading
from io import StringIO
from datetime import datetime
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters,
)

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USERS_RAW = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USERS     = set(int(x.strip()) for x in ALLOWED_USERS_RAW.split(",") if x.strip())
# TIKTOK_SESSION accepts TWO formats:
#   1. Just the sessionid value:   abc123def456...
#   2. Full JSON cookie array:     [{"name":"sessionid","value":"..."},...]
TIKTOK_SESSION = os.environ.get("TIKTOK_SESSION", "").strip()
# ──────────────────────────────────────────────────────────────────────────────

TIKTOK_VIDEO_RE = re.compile(r'https://www\.tiktok\.com/@[\w.]+/video/\d+')

# ─── PARSE SESSION ────────────────────────────────────────────────────────────
def parse_session() -> list:
    raw = TIKTOK_SESSION
    if not raw:
        return []
    if raw.startswith("["):
        import json
        try:
            cookies = json.loads(raw)
            SAMESITE = {"no_restriction": "None", "lax": "Lax", "strict": "Strict"}
            cleaned, seen = [], set()
            for c in cookies:
                name = c.get("name", "")
                if name in seen:
                    continue
                seen.add(name)
                out = {
                    "name": name,
                    "value": c.get("value", ""),
                    "domain": ".tiktok.com",
                    "path": "/",
                    "secure": bool(c.get("secure", False)),
                    "httpOnly": bool(c.get("httpOnly", False)),
                    "sameSite": SAMESITE.get((c.get("sameSite") or "lax").lower(), "Lax"),
                }
                exp = c.get("expirationDate") or c.get("expires")
                if exp:
                    out["expires"] = int(exp)
                cleaned.append(out)
            return cleaned
        except Exception as e:
            logger.warning("JSON cookie parse failed: %s", e)
            return []
    # Raw sessionid value — build minimal cookie set
    return [
        {"name": "sessionid",    "value": raw, "domain": ".tiktok.com", "path": "/", "secure": True, "httpOnly": True,  "sameSite": "Lax"},
        {"name": "sessionid_ss", "value": raw, "domain": ".tiktok.com", "path": "/", "secure": True, "httpOnly": True,  "sameSite": "None"},
        {"name": "sid_tt",       "value": raw, "domain": ".tiktok.com", "path": "/", "secure": True, "httpOnly": True,  "sameSite": "Lax"},
    ]

COOKIES = parse_session()

# ─── AUTH ─────────────────────────────────────────────────────────────────────
def is_allowed(uid):
    return not ALLOWED_USERS or uid in ALLOWED_USERS

def auth_required(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update.effective_user.id):
            await update.effective_message.reply_text("Not authorized.")
            return
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper

# ─── PER-USER STATE ───────────────────────────────────────────────────────────
user_state = defaultdict(lambda: {
    "running": False, "results": [], "stop_event": None, "fmt": "csv", "status_msg_id": None
})

# ─── SCRAPER ──────────────────────────────────────────────────────────────────
def run_scraper(user_id, target, stop_event, on_progress, on_done, on_error):
    try:
        from playwright.sync_api import sync_playwright

        collected, seen, api_urls = [], set(), []

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-dev-shm-usage", "--disable-gpu",
                      "--disable-blink-features=AutomationControlled"]
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            context.add_init_script("""
                Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
                Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
                Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
                window.chrome={runtime:{}};
            """)

            if COOKIES:
                context.add_cookies(COOKIES)

            # Intercept TikTok API responses to get video IDs + real usernames
            def on_response(response):
                try:
                    url = response.url
                    if any(k in url for k in ["recommend/item_list", "feed", "aweme/v1", "item_list"]):
                        try:
                            body = response.json()
                            items = (body.get("aweme_list") or
                                     body.get("itemList") or
                                     body.get("item_list") or [])
                            for item in items:
                                aweme_id = item.get("aweme_id") or item.get("id")
                                author = (item.get("author") or {}).get("unique_id")
                                if aweme_id and author:
                                    api_urls.append(
                                        "https://www.tiktok.com/@" + str(author) + "/video/" + str(aweme_id)
                                    )
                        except Exception:
                            pass
                except Exception:
                    pass

            page = context.new_page()
            page.on("response", on_response)
            page.goto("https://www.tiktok.com/foryou", wait_until="domcontentloaded", timeout=45000)
            time.sleep(8)

            # Dismiss popups
            for sel in [
                "button:has-text('Accept all')",
                "[data-e2e='cookie-banner-accept']",
                "[data-e2e='modal-close-inner-button']",
            ]:
                try:
                    page.click(sel, timeout=2000)
                    time.sleep(0.5)
                except Exception:
                    pass

            # Wait for first video
            try:
                page.wait_for_selector("a[href*='/video/']", timeout=12000)
            except Exception:
                pass

            # Click center for keyboard focus
            try:
                page.mouse.click(640, 450)
                time.sleep(1)
            except Exception:
                pass

            scroll_attempts = 0
            stuck = 0
            last_count = 0

            while len(collected) < target and scroll_attempts < target * 8:
                if stop_event.is_set():
                    break

                # Method 1: API interception (most accurate — real usernames)
                for url in list(api_urls):
                    clean = url.split("?")[0]
                    if TIKTOK_VIDEO_RE.match(clean) and clean not in seen:
                        seen.add(clean)
                        collected.append({
                            "index": len(collected) + 1,
                            "url": clean,
                            "scraped_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                        })
                        if True:  # update every video for live counter
                            on_progress(len(collected), target)
                        if len(collected) >= target:
                            break
                api_urls.clear()

                # Method 2: Page HTML scan (fallback)
                if len(collected) < target:
                    for url in TIKTOK_VIDEO_RE.findall(page.content()):
                        clean = url.split("?")[0]
                        if clean not in seen:
                            seen.add(clean)
                            collected.append({
                                "index": len(collected) + 1,
                                "url": clean,
                                "scraped_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                            })
                            if True:  # update every video for live counter
                                on_progress(len(collected), target)
                            if len(collected) >= target:
                                break

                if len(collected) >= target:
                    break

                stuck = 0 if len(collected) > last_count else stuck + 1
                last_count = len(collected)

                # Press ArrowDown to go to next video (TikTok FYP navigation)
                page.keyboard.press("ArrowDown")
                time.sleep(3)
                scroll_attempts += 1

                # If stuck for 5 cycles, try clicking to regain focus
                if stuck >= 5:
                    try:
                        page.mouse.click(640, 450)
                        time.sleep(1)
                    except Exception:
                        pass
                    stuck = 0

            # Save debug screenshot if nothing was collected
            if not collected:
                try:
                    page.screenshot(path="/tmp/debug.png")
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
    await update.message.reply_text(
        "🎵 *TikTok FYP Scraper*\n\n"
        "/scrape `<amount>` — Start scraping (e.g. /scrape 20)\n"
        "/stop — Stop current scrape\n"
        "/status — Check progress\n"
        "/format — Switch CSV or TXT output\n"
        "/download — Download results\n"
        "/cookies — Check session status\n"
        "/debug — Screenshot what the bot sees",
        parse_mode="Markdown"
    )

@auth_required
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)

@auth_required
async def cmd_cookies(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not COOKIES:
        await update.message.reply_text(
            "No session loaded.\n\n"
            "Set TIKTOK_SESSION in Railway as either:\n"
            "Option A — Just your sessionid value:\n"
            "abc123def456...\n\n"
            "Option B — Full JSON cookie array:\n"
            '[{"name":"sessionid","value":"..."},...]'
        )
        return
    sid = next((c["value"][:12] + "..." for c in COOKIES if c["name"] == "sessionid"), "not found")
    await update.message.reply_text(
        "Session loaded\n"
        "Cookies: " + str(len(COOKIES)) + "\n"
        "sessionid: " + sid
    )

@auth_required
async def cmd_scrape(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_state[uid]

    if state["running"]:
        await update.message.reply_text("Already running! Use /stop first.")
        return

    args = ctx.args
    try:
        target = max(1, min(500, int(args[0]))) if args else 20
    except (ValueError, IndexError):
        await update.message.reply_text("Usage: /scrape <amount>\nExample: /scrape 30")
        return

    state["running"] = True
    state["results"] = []
    stop_event = threading.Event()
    state["stop_event"] = stop_event

    # Send ONE status message and keep editing it
    status_msg = await update.message.reply_text(
        "🔄 Scraping 0/" + str(target) + " videos..."
    )
    state["status_msg_id"] = status_msg.message_id

    loop = asyncio.get_event_loop()

    def on_progress(count, total):
        asyncio.run_coroutine_threadsafe(
            ctx.bot.edit_message_text(
                chat_id=uid,
                message_id=state["status_msg_id"],
                text="🔄 Scraping " + str(count) + "/" + str(total) + " videos..."
            ), loop,
        )

    def on_done(results):
        state["running"] = False
        asyncio.run_coroutine_threadsafe(
            ctx.bot.edit_message_text(
                chat_id=uid,
                message_id=state["status_msg_id"],
                text="✅ Done! Collected " + str(len(results)) + "/" + str(target) + " videos.\nUse /download to get the file."
            ), loop,
        )

    def on_error(err):
        state["running"] = False
        asyncio.run_coroutine_threadsafe(
            ctx.bot.edit_message_text(
                chat_id=uid,
                message_id=state["status_msg_id"],
                text="❌ Error: " + str(err)
            ), loop,
        )

    threading.Thread(
        target=run_scraper,
        args=(uid, target, stop_event, on_progress, on_done, on_error),
        daemon=True
    ).start()

@auth_required
async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_state[uid]
    if not state["running"]:
        await update.message.reply_text("Nothing is running.")
        return
    state["stop_event"].set()
    state["running"] = False
    count = len(state["results"])
    # Edit the running status message if it exists
    if state.get("status_msg_id"):
        try:
            await ctx.bot.edit_message_text(
                chat_id=uid,
                message_id=state["status_msg_id"],
                text="⏹ Stopped at " + str(count) + " videos. Use /download to get them."
            )
            return
        except Exception:
            pass
    await update.message.reply_text("Stopped. Collected " + str(count) + " videos. Use /download to get them.")

@auth_required
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_state[uid]
    if state["running"]:
        await update.message.reply_text("Running — " + str(len(state["results"])) + " videos collected so far.")
    else:
        await update.message.reply_text("Idle — last run collected " + str(len(state["results"])) + " videos.")

@auth_required
async def cmd_format(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    keyboard = [[
        InlineKeyboardButton("CSV", callback_data="fmt_csv"),
        InlineKeyboardButton("TXT", callback_data="fmt_txt"),
    ]]
    await update.message.reply_text(
        "Current format: " + user_state[uid]["fmt"].upper() + "\nChoose output:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

@auth_required
async def cmd_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    results = user_state[uid]["results"]
    if not results:
        await update.message.reply_text("No results yet. Run /scrape <amount> first.")
        return

    fmt = user_state[uid]["fmt"]
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    if fmt == "csv":
        buf = StringIO()
        csv.DictWriter(buf, fieldnames=["index", "url", "scraped_at"]).writeheader()
        csv.DictWriter(buf, fieldnames=["index", "url", "scraped_at"]).writerows(results)
        await update.message.reply_document(
            document=buf.getvalue().encode(),
            filename="tiktok_" + ts + ".csv",
            caption=str(len(results)) + " TikTok video links"
        )
    else:
        content = "\n".join(r["url"] for r in results)
        await update.message.reply_document(
            document=content.encode(),
            filename="tiktok_" + ts + ".txt",
            caption=str(len(results)) + " TikTok video links"
        )

@auth_required
async def cmd_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text("Taking screenshot, please wait ~15s...")
    loop = asyncio.get_event_loop()

    def _run():
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
                )
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 900}
                )
                context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
                if COOKIES:
                    context.add_cookies(COOKIES)
                page = context.new_page()
                page.goto("https://www.tiktok.com/foryou", wait_until="domcontentloaded", timeout=30000)
                time.sleep(6)
                page.screenshot(path="/tmp/debug.png")
                browser.close()
            with open("/tmp/debug.png", "rb") as f:
                asyncio.run_coroutine_threadsafe(
                    ctx.bot.send_photo(
                        chat_id=uid, photo=f,
                        caption="What TikTok shows the bot. If you see a login wall, cookies expired."
                    ), loop,
                )
        except Exception as e:
            asyncio.run_coroutine_threadsafe(
                ctx.bot.send_message(chat_id=uid, text="Screenshot failed: " + str(e)), loop,
            )

    threading.Thread(target=_run, daemon=True).start()

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    await query.answer()
    if not is_allowed(uid):
        return
    if query.data == "fmt_csv":
        user_state[uid]["fmt"] = "csv"
        await query.edit_message_text("Output format set to CSV.")
    elif query.data == "fmt_txt":
        user_state[uid]["fmt"] = "txt"
        await query.edit_message_text("Output format set to TXT.")

async def unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Unknown command. Use /help.")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("scrape",   cmd_scrape))
    app.add_handler(CommandHandler("stop",     cmd_stop))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("format",   cmd_format))
    app.add_handler(CommandHandler("download", cmd_download))
    app.add_handler(CommandHandler("debug",    cmd_debug))
    app.add_handler(CommandHandler("cookies",  cmd_cookies))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
