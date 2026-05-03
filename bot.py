"""
TikTok FYP Scraper Bot
- Set TIKTOK_COOKIES in Railway as JSON array from Cookie-Editor
- Or send a .json cookie file directly to the bot -> it replies with the Railway value
"""

import os, csv, re, time, asyncio, logging, threading, json
from io import StringIO
from datetime import datetime
from collections import defaultdict

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USERS_RAW  = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USERS      = set(int(x.strip()) for x in ALLOWED_USERS_RAW.split(",") if x.strip())
TIKTOK_COOKIES_RAW = os.environ.get("TIKTOK_COOKIES", "").strip()

TIKTOK_VIDEO_RE = re.compile(r'https://www\.tiktok\.com/@[\w.]+/video/\d+')
SAMESITE_MAP    = {"no_restriction": "None", "lax": "Lax", "strict": "Strict", "none": "None"}

# ─── COOKIE HELPERS ───────────────────────────────────────────────────────────
def clean_cookies(arr: list) -> list:
    """Normalize a raw cookie array into Playwright-ready format."""
    cleaned, seen = [], set()
    for c in arr:
        name  = c.get("name", "")
        value = c.get("value", "")
        if not name or name in seen:
            continue
        seen.add(name)
        raw_ss = (c.get("sameSite") or "lax").lower()
        out = {
            "name":     name,
            "value":    str(value),
            "domain":   ".tiktok.com",
            "path":     "/",
            "secure":   bool(c.get("secure", False)),
            "httpOnly": bool(c.get("httpOnly", False)),
            "sameSite": SAMESITE_MAP.get(raw_ss, "Lax"),
        }
        exp = c.get("expirationDate") or c.get("expires")
        if exp:
            try:
                out["expires"] = int(float(exp))
            except Exception:
                pass
        cleaned.append(out)
    return cleaned


def load_cookies_from_raw(raw: str) -> list:
    """Parse TIKTOK_COOKIES env var — expects a JSON array string."""
    if not raw:
        return []
    try:
        arr = json.loads(raw)
        if isinstance(arr, list):
            return clean_cookies(arr)
    except Exception as e:
        logger.warning("Failed to parse TIKTOK_COOKIES: %s", e)
    return []


def process_cookie_file(raw_text: str):
    """
    Parse a cookie JSON file uploaded by user.
    Returns (railway_value_string, summary_text, error_text)
    """
    try:
        arr = json.loads(raw_text.strip())
    except Exception as e:
        return None, None, f"Could not parse JSON: {e}"

    if not isinstance(arr, list):
        return None, None, "Expected a JSON array (starting with [). Export again from Cookie-Editor."

    cleaned = clean_cookies(arr)
    if not cleaned:
        return None, None, "No cookies found after parsing. Try exporting again."

    names       = [c["name"] for c in cleaned]
    key_names   = ["sessionid", "sid_tt", "uid_tt", "odin_tt", "msToken"]
    found       = [k for k in key_names if k in names]
    missing     = [k for k in key_names if k not in names]
    has_session = "sessionid" in names
    sid_preview = next((c["value"][:16] + "..." for c in cleaned if c["name"] == "sessionid"), "not found")

    summary = (
        ("✅" if has_session else "⚠️") + " *Cookie File Processed*\n\n"
        "Total cookies: `" + str(len(cleaned)) + "`\n"
        "Found: `" + ", ".join(found) + "`\n"
        + ("Missing: `" + ", ".join(missing) + "`\n" if missing else "")
        + "sessionid: `" + sid_preview + "`\n\n"
        + ("✅ Ready to use!" if has_session else "⚠️ No sessionid — may not work!")
    )

    railway_value = json.dumps(cleaned, separators=(",", ":"))
    return railway_value, summary, None


# Load cookies at startup from env var
COOKIES: list = load_cookies_from_raw(TIKTOK_COOKIES_RAW)
logger.info("Loaded %d cookies from TIKTOK_COOKIES env var", len(COOKIES))

# ─── AUTH ─────────────────────────────────────────────────────────────────────
def is_allowed(uid: int) -> bool:
    return not ALLOWED_USERS or uid in ALLOWED_USERS

def auth_required(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update.effective_user.id):
            await update.effective_message.reply_text("Not authorized.")
            return
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper

# ─── STATE ────────────────────────────────────────────────────────────────────
user_state = defaultdict(lambda: {
    "running": False, "results": [], "stop_event": None,
    "fmt": "csv", "status_msg_id": None, "target": 0,
})

# ─── SCRAPER ──────────────────────────────────────────────────────────────────
def run_scraper(user_id, target, stop_event, on_update, on_done, on_error):
    try:
        from playwright.sync_api import sync_playwright

        collected, seen, api_buffer = [], set(), []

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox", "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage", "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                    "--window-size=1280,900",
                ]
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                timezone_id="America/New_York",
            )
            context.add_init_script("""
                Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
                Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
                Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
                window.chrome={runtime:{}};
            """)

            if COOKIES:
                context.add_cookies(COOKIES)

            def on_response(response):
                try:
                    url = response.url
                    if any(k in url for k in [
                        "recommend/item_list", "/feed", "aweme/v1/feed",
                        "item_list", "/api/recommend", "homepage/recommend"
                    ]):
                        try:
                            body  = response.json()
                            items = (
                                body.get("aweme_list") or
                                body.get("itemList") or
                                body.get("item_list") or []
                            )
                            for item in items:
                                vid_id = item.get("aweme_id") or item.get("id")
                                author = (item.get("author") or {}).get("unique_id")
                                if vid_id and author:
                                    api_buffer.append(
                                        "https://www.tiktok.com/@" + str(author) + "/video/" + str(vid_id)
                                    )
                        except Exception:
                            pass
                except Exception:
                    pass

            page = context.new_page()
            page.on("response", on_response)

            page.goto("https://www.tiktok.com/foryou", wait_until="domcontentloaded", timeout=45000)
            time.sleep(8)

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

            try:
                page.wait_for_selector("a[href*='/video/']", timeout=15000)
                time.sleep(2)
            except Exception:
                pass

            try:
                page.mouse.click(640, 450)
                time.sleep(1)
            except Exception:
                pass

            scroll_attempts  = 0
            max_attempts     = target * 10
            stuck_count      = 0
            last_count       = 0
            last_update_sent = -1

            def flush():
                for url in list(api_buffer):
                    clean = url.split("?")[0]
                    if TIKTOK_VIDEO_RE.match(clean) and clean not in seen:
                        seen.add(clean)
                        collected.append({
                            "index":      len(collected) + 1,
                            "url":        clean,
                            "scraped_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                        })
                        if len(collected) >= target:
                            break
                api_buffer.clear()
                if len(collected) < target:
                    try:
                        for url in TIKTOK_VIDEO_RE.findall(page.content()):
                            clean = url.split("?")[0]
                            if clean not in seen:
                                seen.add(clean)
                                collected.append({
                                    "index":      len(collected) + 1,
                                    "url":        clean,
                                    "scraped_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                                })
                                if len(collected) >= target:
                                    break
                    except Exception:
                        pass

            while len(collected) < target and scroll_attempts < max_attempts:
                if stop_event.is_set():
                    break

                flush()

                if len(collected) != last_update_sent:
                    on_update(len(collected), target)
                    last_update_sent = len(collected)

                if len(collected) >= target:
                    break

                stuck_count = 0 if len(collected) > last_count else stuck_count + 1
                last_count  = len(collected)

                try:
                    page.keyboard.press("ArrowDown")
                except Exception:
                    pass
                time.sleep(3)
                scroll_attempts += 1

                if stuck_count >= 8:
                    logger.warning("Stuck at %d — running recovery", len(collected))
                    try:
                        page.mouse.click(640, 450)
                        time.sleep(1)
                        page.keyboard.press("ArrowDown")
                        time.sleep(2)
                        page.evaluate("window.scrollBy(0, 500)")
                        time.sleep(1)
                    except Exception:
                        pass
                    stuck_count = 0

                if scroll_attempts % 20 == 0:
                    try:
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        time.sleep(2)
                        page.evaluate("window.scrollTo(0, 0)")
                        time.sleep(1)
                    except Exception:
                        pass

            flush()

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


# ─── COMMANDS ─────────────────────────────────────────────────────────────────

@auth_required
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cookie_status = "✅ " + str(len(COOKIES)) + " cookies loaded" if COOKIES else "❌ No cookies — set TIKTOK_COOKIES in Railway"
    await update.message.reply_text(
        "🎵 *TikTok FYP Scraper Bot*\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "🍪 Cookies: " + cookie_status + "\n\n"
        "▶️ /scrape `<amount>` — Start scraping\n"
        "⏹ /stop — Stop scrape\n"
        "📊 /status — Check progress\n"
        "📥 /download — Download results\n"
        "✅ /check — Verify session works\n"
        "🖥 /debug — Screenshot what bot sees\n\n"
        "📎 *To update cookies:* send a `.json` file\n"
        "   exported from Cookie\\-Editor extension\n"
        "   → bot replies with Railway\\-ready value",
        parse_mode="Markdown"
    )

@auth_required
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)

@auth_required
async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    msg  = await update.message.reply_text("🔍 Checking session...")
    loop = asyncio.get_event_loop()

    def _run():
        if not COOKIES:
            text = (
                "❌ *No cookies loaded*\n\n"
                "Set `TIKTOK_COOKIES` in Railway variables.\n"
                "Send a `.json` cookie file here to get the value."
            )
            asyncio.run_coroutine_threadsafe(
                ctx.bot.edit_message_text(chat_id=uid, message_id=msg.message_id, text=text, parse_mode="Markdown"), loop
            )
            return

        sid = next((c["value"] for c in COOKIES if c["name"] == "sessionid"), None)
        if not sid:
            asyncio.run_coroutine_threadsafe(
                ctx.bot.edit_message_text(
                    chat_id=uid, message_id=msg.message_id,
                    text="❌ *No sessionid in cookies*\n\nSend a fresh `.json` cookie file to get a new Railway value.",
                    parse_mode="Markdown"
                ), loop
            )
            return

        try:
            import requests as req
            cookie_dict = {c["name"]: c["value"] for c in COOKIES}
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                "Referer": "https://www.tiktok.com/",
            }
            resp  = req.get(
                "https://www.tiktok.com/api/recommend/item_list/?count=1&aid=1988",
                headers=headers, cookies=cookie_dict, timeout=15
            )
            data  = resp.json()
            items = data.get("itemList") or data.get("aweme_list") or []
            scode = data.get("statusCode", data.get("status_code", -1))
            if items or scode == 0:
                text = (
                    "✅ *Session valid!*\n\n"
                    "🍪 Cookies: `" + str(len(COOKIES)) + "`\n"
                    "🔑 sessionid: `" + sid[:16] + "...`\n\n"
                    "Ready! Use /scrape `<amount>`"
                )
            else:
                text = "⚠️ *Session may be expired* (status: " + str(scode) + ")\n\nSend a fresh `.json` cookie file."
        except Exception as e:
            text = "❌ Check failed: `" + str(e) + "`"

        asyncio.run_coroutine_threadsafe(
            ctx.bot.edit_message_text(chat_id=uid, message_id=msg.message_id, text=text, parse_mode="Markdown"), loop
        )

    threading.Thread(target=_run, daemon=True).start()

@auth_required
async def cmd_scrape(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = user_state[uid]

    if state["running"]:
        await update.message.reply_text("⚠️ Already running! Use /stop first.")
        return
    if not COOKIES:
        await update.message.reply_text("❌ No cookies loaded!\n\nSet `TIKTOK_COOKIES` in Railway, or send a `.json` cookie file here.", parse_mode="Markdown")
        return

    try:
        target = max(1, min(500, int(ctx.args[0]))) if ctx.args else 20
    except (ValueError, IndexError):
        await update.message.reply_text("Usage: /scrape `<amount>`\nExample: /scrape 50", parse_mode="Markdown")
        return

    state["running"]    = True
    state["results"]    = []
    state["target"]     = target
    stop_event          = threading.Event()
    state["stop_event"] = stop_event

    status_msg = await update.message.reply_text(
        "🔄 *Scraping 0 / " + str(target) + " videos...*", parse_mode="Markdown"
    )
    state["status_msg_id"] = status_msg.message_id
    loop = asyncio.get_event_loop()

    def on_update(count, total):
        asyncio.run_coroutine_threadsafe(
            ctx.bot.edit_message_text(
                chat_id=uid, message_id=state["status_msg_id"],
                text="🔄 *Scraping " + str(count) + " / " + str(total) + " videos...*",
                parse_mode="Markdown"
            ), loop
        )

    def on_done(results):
        state["running"] = False
        asyncio.run_coroutine_threadsafe(
            ctx.bot.edit_message_text(
                chat_id=uid, message_id=state["status_msg_id"],
                text="✅ *Done!*\n\n🎵 Collected *" + str(len(results)) + " / " + str(target) + "* videos\n📥 Use /download to get your file",
                parse_mode="Markdown"
            ), loop
        )

    def on_error(err):
        state["running"] = False
        asyncio.run_coroutine_threadsafe(
            ctx.bot.edit_message_text(
                chat_id=uid, message_id=state["status_msg_id"],
                text="❌ *Error:* `" + str(err) + "`\n\nTry /debug to see what's happening.",
                parse_mode="Markdown"
            ), loop
        )

    threading.Thread(target=run_scraper, args=(uid, target, stop_event, on_update, on_done, on_error), daemon=True).start()

@auth_required
async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = user_state[uid]
    if not state["running"]:
        await update.message.reply_text("Nothing is running.")
        return
    state["stop_event"].set()
    state["running"] = False
    count = len(state["results"])
    text  = "⏹ *Stopped!*\n\nCollected *" + str(count) + "* videos.\nUse /download to get them."
    if state.get("status_msg_id"):
        try:
            await ctx.bot.edit_message_text(chat_id=uid, message_id=state["status_msg_id"], text=text, parse_mode="Markdown")
            return
        except Exception:
            pass
    await update.message.reply_text(text, parse_mode="Markdown")

@auth_required
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = user_state[uid]
    count = len(state["results"])
    if state["running"]:
        await update.message.reply_text("🔄 *Running*\n\nCollected: *" + str(count) + " / " + str(state["target"]) + "* videos", parse_mode="Markdown")
    else:
        await update.message.reply_text("💤 *Idle*\n\nLast run: *" + str(count) + "* videos\nUse /scrape `<amount>` to start", parse_mode="Markdown")

@auth_required
async def cmd_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    results = user_state[uid]["results"]
    if not results:
        await update.message.reply_text("No results yet. Run /scrape `<amount>` first.", parse_mode="Markdown")
        return
    ts  = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    buf = StringIO()
    writer = csv.DictWriter(buf, fieldnames=["index", "url", "scraped_at"])
    writer.writeheader()
    writer.writerows(results)
    await update.message.reply_document(
        document=buf.getvalue().encode(),
        filename="tiktok_" + ts + ".csv",
        caption="🎵 " + str(len(results)) + " TikTok video links"
    )

@auth_required
async def cmd_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    loop = asyncio.get_event_loop()
    await update.message.reply_text("📸 Taking screenshot (~15s)...")

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
                        caption="✅ FYP = cookies working!\n❌ Login wall = cookies expired — send a fresh .json file."
                    ), loop
                )
        except Exception as e:
            asyncio.run_coroutine_threadsafe(
                ctx.bot.send_message(chat_id=uid, text="❌ Screenshot failed: " + str(e)), loop
            )

    threading.Thread(target=_run, daemon=True).start()


# ─── JSON FILE HANDLER ────────────────────────────────────────────────────────
@auth_required
async def handle_cookie_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User sends a .json cookie file → bot replies with the Railway-ready value."""
    uid = update.effective_user.id
    doc = update.message.document

    if not (doc.file_name or "").lower().endswith(".json"):
        await update.message.reply_text("Please send a `.json` file exported from Cookie-Editor.", parse_mode="Markdown")
        return

    processing_msg = await update.message.reply_text("⏳ Processing cookie file...")

    try:
        tg_file   = await ctx.bot.get_file(doc.file_id)
        raw_bytes = await tg_file.download_as_bytearray()
        raw_text  = raw_bytes.decode("utf-8")
    except Exception as e:
        await ctx.bot.edit_message_text(
            chat_id=uid, message_id=processing_msg.message_id,
            text="❌ Failed to read file: " + str(e)
        )
        return

    railway_value, summary, error = process_cookie_file(raw_text)

    if error:
        await ctx.bot.edit_message_text(
            chat_id=uid, message_id=processing_msg.message_id,
            text="❌ " + error
        )
        return

    await ctx.bot.edit_message_text(
        chat_id=uid, message_id=processing_msg.message_id,
        text=summary, parse_mode="Markdown"
    )

    # Send the Railway value as a downloadable .txt file (no size issues)
    await update.message.reply_document(
        document=railway_value.encode(),
        filename="TIKTOK_COOKIES_railway.txt",
        caption=(
            "📋 *How to use this file:*\n\n"
            "1️⃣ Open the file and copy ALL contents\n"
            "2️⃣ Go to Railway → your service → Variables\n"
            "3️⃣ Add variable: `TIKTOK_COOKIES`\n"
            "4️⃣ Paste the file contents as the value\n"
            "5️⃣ Redeploy the service\n\n"
            "Then use /check to verify it works!"
        ),
        parse_mode="Markdown"
    )


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
    app.add_handler(CommandHandler("download", cmd_download))
    app.add_handler(CommandHandler("check",    cmd_check))
    app.add_handler(CommandHandler("debug",    cmd_debug))
    # JSON file upload handler
    app.add_handler(MessageHandler(filters.Document.FileExtension("json"), handle_cookie_file))
    app.add_handler(MessageHandler(filters.Document.MimeType("application/json"), handle_cookie_file))
    # Catch-all for unknown commands
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    logger.info("Bot started! Cookies loaded: %d", len(COOKIES))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
