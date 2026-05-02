"""
TikTok FYP Scraper Bot — Telegram Controller
Supports full JSON cookie export (EditThisCookie, Cookie-Editor, etc.)
"""

import os, csv, re, time, json, asyncio, logging, threading
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

TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USERS_RAW = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USERS     = set(int(x.strip()) for x in ALLOWED_USERS_RAW.split(",") if x.strip())
TIKTOK_SESSION    = os.environ.get("TIKTOK_SESSION", "").strip()

_runtime_cookies = []
_runtime_cookies_lock = threading.Lock()

TIKTOK_VIDEO_RE = re.compile(r'https://www\.tiktok\.com/@[\w.]+/video/\d+')

SAMESITE_MAP = {
    "no_restriction": "None", "none": "None",
    "lax": "Lax", "strict": "Strict",
    "unspecified": "Lax", "": "Lax",
}

def _clean_cookie(c):
    samesite_raw = (c.get("sameSite") or c.get("same_site") or "lax").lower()
    out = {
        "name":     c.get("name", ""),
        "value":    str(c.get("value", "")),
        "domain":   ".tiktok.com",
        "path":     c.get("path", "/") or "/",
        "secure":   bool(c.get("secure", False)),
        "httpOnly": bool(c.get("httpOnly", c.get("http_only", False))),
        "sameSite": SAMESITE_MAP.get(samesite_raw, "Lax"),
    }
    exp = c.get("expirationDate") or c.get("expires") or c.get("expiry")
    if exp:
        try:
            out["expires"] = int(float(exp))
        except (ValueError, TypeError):
            pass
    return out

def parse_cookies(raw):
    raw = raw.strip()
    if not raw:
        return [], "Empty input"
    if raw.startswith("["):
        try:
            arr = json.loads(raw)
        except json.JSONDecodeError as e:
            return [], f"JSON parse error: {e}\n\nMake sure you copied the FULL JSON including [ and ] brackets."
        if not isinstance(arr, list):
            return [], "Expected a JSON array starting with ["
        cleaned, seen = [], set()
        for c in arr:
            if not isinstance(c, dict):
                continue
            name = c.get("name", "")
            if not name or name in seen:
                continue
            seen.add(name)
            cleaned.append(_clean_cookie(c))
        if not cleaned:
            return [], "JSON parsed but no cookies found. Check the format."
        has_session = any(c["name"] == "sessionid" for c in cleaned)
        if not has_session:
            return cleaned, "Warning: no 'sessionid' found - TikTok may not be logged in."
        return cleaned, ""
    if len(raw) > 20:
        cookies = [
            {"name": "sessionid",    "value": raw, "domain": ".tiktok.com", "path": "/", "secure": True, "httpOnly": True, "sameSite": "Lax"},
            {"name": "sessionid_ss", "value": raw, "domain": ".tiktok.com", "path": "/", "secure": True, "httpOnly": True, "sameSite": "None"},
            {"name": "sid_tt",       "value": raw, "domain": ".tiktok.com", "path": "/", "secure": True, "httpOnly": True, "sameSite": "Lax"},
        ]
        return cookies, ""
    return [], "Unrecognized format. Use /setcookies for instructions."

def get_active_cookies():
    with _runtime_cookies_lock:
        if _runtime_cookies:
            return list(_runtime_cookies)
    if TIKTOK_SESSION:
        cookies, _ = parse_cookies(TIKTOK_SESSION)
        return cookies
    return []

def is_allowed(uid):
    return not ALLOWED_USERS or uid in ALLOWED_USERS

def auth_required(func):
    async def wrapper(update, ctx):
        if not is_allowed(update.effective_user.id):
            await update.effective_message.reply_text("Not authorized.")
            return
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper

user_state = defaultdict(lambda: {
    "running": False, "results": [], "stop_event": None,
    "fmt": "csv", "status_msg_id": None
})

def run_scraper(user_id, target, stop_event, on_progress, on_done, on_error):
    try:
        from playwright.sync_api import sync_playwright
        cookies = get_active_cookies()
        collected, seen, api_urls = [], set(), []
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox","--disable-setuid-sandbox",
                      "--disable-dev-shm-usage","--disable-gpu",
                      "--disable-blink-features=AutomationControlled"]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            context.add_init_script("""
                Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
                Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
                Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
                window.chrome={runtime:{}};
            """)
            if cookies:
                context.add_cookies(cookies)
            def on_response(response):
                try:
                    url = response.url
                    if any(k in url for k in [
                        "recommend/item_list","feed","aweme/v1","item_list",
                        "api/recommend","api-h2/feed","tiktok.com/api/",
                        "aweme/v2","mix/item_list","related/item_list"
                    ]):
                        try:
                            body = response.json()
                            items = (body.get("aweme_list") or body.get("itemList") or body.get("item_list") or [])
                            for item in items:
                                aweme_id = item.get("aweme_id") or item.get("id")
                                author = (item.get("author") or {}).get("unique_id")
                                if aweme_id and author:
                                    api_urls.append("https://www.tiktok.com/@" + str(author) + "/video/" + str(aweme_id))
                        except Exception:
                            pass
                except Exception:
                    pass
            page = context.new_page()
            page.on("response", on_response)
            page.goto("https://www.tiktok.com/foryou", wait_until="domcontentloaded", timeout=45000)
            time.sleep(8)
            for sel in ["button:has-text('Accept all')","[data-e2e='cookie-banner-accept']","[data-e2e='modal-close-inner-button']"]:
                try:
                    page.click(sel, timeout=2000)
                    time.sleep(0.5)
                except Exception:
                    pass
            try:
                page.wait_for_selector("a[href*='/video/']", timeout=12000)
            except Exception:
                pass
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
                for url in list(api_urls):
                    clean = url.split("?")[0]
                    if TIKTOK_VIDEO_RE.match(clean) and clean not in seen:
                        seen.add(clean)
                        collected.append({"index": len(collected)+1, "url": clean, "scraped_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")})
                        on_progress(len(collected), target)
                        if len(collected) >= target:
                            break
                api_urls.clear()
                if len(collected) < target:
                    for url in TIKTOK_VIDEO_RE.findall(page.content()):
                        clean = url.split("?")[0]
                        if clean not in seen:
                            seen.add(clean)
                            collected.append({"index": len(collected)+1, "url": clean, "scraped_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")})
                            on_progress(len(collected), target)
                            if len(collected) >= target:
                                break
                if len(collected) >= target:
                    break
                stuck = 0 if len(collected) > last_count else stuck + 1
                last_count = len(collected)
                page.mouse.click(640, 450)
                time.sleep(0.3)
                page.keyboard.press("ArrowDown")
                time.sleep(2)
                page.evaluate("window.scrollBy(0, 600)")
                time.sleep(1)
                scroll_attempts += 1
                if stuck >= 5:
                    try:
                        page.mouse.click(640, 450)
                        time.sleep(1)
                    except Exception:
                        pass
                    stuck = 0
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

@auth_required
async def cmd_start(update, ctx):
    await update.message.reply_text(
        "🎵 *TikTok FYP Scraper*\n\n"
        "*Setup:*\n"
        "/setcookies — Paste your TikTok cookies JSON\n"
        "/cookies — Check current session status\n"
        "/cookiehelp — How to export cookies from browser\n\n"
        "*Scraping:*\n"
        "/scrape `<amount>` — e.g. /scrape 20\n"
        "/stop — Stop current scrape\n"
        "/status — Check progress\n\n"
        "*Output:*\n"
        "/format — Switch CSV or TXT\n"
        "/download — Download results\n\n"
        "*Debug:*\n"
        "/debug — Screenshot what the bot sees",
        parse_mode="Markdown"
    )

@auth_required
async def cmd_help(update, ctx):
    await cmd_start(update, ctx)

@auth_required
async def cmd_setcookies(update, ctx):
    args_text = update.message.text.partition(" ")[2].strip()
    if not args_text:
        await update.message.reply_text(
            "📋 *How to use /setcookies:*\n\n"
            "*Option 1 — Full JSON (recommended):*\n"
            "1. Install *EditThisCookie* or *Cookie-Editor* in Chrome\n"
            "2. Go to tiktok.com and log in\n"
            "3. Click extension → Export as JSON\n"
            "4. Copy the full JSON array\n"
            "5. Send: `/setcookies [paste json here]`\n\n"
            "*Option 2 — Raw sessionid only:*\n"
            "1. tiktok.com → F12 → Application → Cookies\n"
            "2. Copy value of `sessionid`\n"
            "3. Send: `/setcookies abc123yourvalue`\n\n"
            "Use /cookiehelp for full guide.",
            parse_mode="Markdown"
        )
        return
    cookies, err = parse_cookies(args_text)
    if err and not cookies:
        await update.message.reply_text(
            f"❌ *Failed:*\n`{err}`\n\nUse /setcookies (no args) for instructions.",
            parse_mode="Markdown"
        )
        return
    with _runtime_cookies_lock:
        _runtime_cookies.clear()
        _runtime_cookies.extend(cookies)
    names = [c["name"] for c in cookies]
    has_session = "sessionid" in names
    session_preview = next((c["value"][:10] + "..." for c in cookies if c["name"] == "sessionid"), "not found")
    icon = "✅" if has_session else "⚠️"
    warning = "\n\n⚠️ No `sessionid` found — may not be logged in." if not has_session else ""
    await update.message.reply_text(
        f"{icon} *Cookies loaded!*\n\n"
        f"Total: `{len(cookies)}` cookies\n"
        f"Names: `{', '.join(names[:12])}{'...' if len(names)>12 else ''}`\n"
        f"sessionid: `{session_preview}`{warning}\n\n"
        "Run /debug to verify TikTok loads correctly.",
        parse_mode="Markdown"
    )

@auth_required
async def cmd_cookiehelp(update, ctx):
    await update.message.reply_text(
        "🍪 *Cookie Export Guide*\n\n"
        "*EditThisCookie (Chrome):*\n"
        "1. Chrome Web Store → install 'EditThisCookie'\n"
        "2. Go to tiktok.com and log in\n"
        "3. Click the extension icon\n"
        "4. Click Export (box with arrow icon)\n"
        "5. JSON is copied to clipboard\n"
        "6. Send: `/setcookies [paste here]`\n\n"
        "*Cookie-Editor (Chrome/Firefox):*\n"
        "1. Install Cookie-Editor extension\n"
        "2. Go to tiktok.com while logged in\n"
        "3. Extension icon → Export → Export as JSON\n"
        "4. Send: `/setcookies [paste here]`\n\n"
        "*Tips:*\n"
        "• Must be logged in when exporting\n"
        "• Cookies expire every ~60 days\n"
        "• Re-export if scraping stops working",
        parse_mode="Markdown"
    )

@auth_required
async def cmd_cookies(update, ctx):
    cookies = get_active_cookies()
    if not cookies:
        await update.message.reply_text(
            "❌ *No cookies loaded.*\n\nUse /setcookies to add cookies.\nUse /cookiehelp for help.",
            parse_mode="Markdown"
        )
        return
    names = [c["name"] for c in cookies]
    session_preview = next((c["value"][:12]+"..." for c in cookies if c["name"]=="sessionid"), "missing")
    source = "runtime (/setcookies)" if _runtime_cookies else "env var (TIKTOK_SESSION)"
    await update.message.reply_text(
        "🍪 *Cookie Status*\n\n"
        f"Source: `{source}`\n"
        f"Total: `{len(cookies)}` cookies\n\n"
        f"sessionid:     {'✅' if 'sessionid' in names else '❌'} `{session_preview}`\n"
        f"sid_tt:        {'✅' if 'sid_tt' in names else '❌ missing'}\n"
        f"tt_csrf_token: {'✅' if 'tt_csrf_token' in names else '⚠️ optional'}\n\n"
        f"All names:\n`{', '.join(names)}`\n\n"
        "Run /debug to verify.",
        parse_mode="Markdown"
    )

@auth_required
async def cmd_scrape(update, ctx):
    uid = update.effective_user.id
    state = user_state[uid]
    if state["running"]:
        await update.message.reply_text("Already running! Use /stop first.")
        return
    if not get_active_cookies():
        await update.message.reply_text("⚠️ No cookies! Use /setcookies first.")
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
    status_msg = await update.message.reply_text("🔄 Scraping 0/" + str(target) + " videos...")
    state["status_msg_id"] = status_msg.message_id
    loop = asyncio.get_event_loop()
    def on_progress(count, total):
        asyncio.run_coroutine_threadsafe(ctx.bot.edit_message_text(chat_id=uid, message_id=state["status_msg_id"], text="🔄 Scraping " + str(count) + "/" + str(total) + " videos..."), loop)
    def on_done(results):
        state["running"] = False
        asyncio.run_coroutine_threadsafe(ctx.bot.edit_message_text(chat_id=uid, message_id=state["status_msg_id"], text="✅ Done! Collected " + str(len(results)) + "/" + str(target) + " videos.\nUse /download to get the file."), loop)
    def on_error(err):
        state["running"] = False
        asyncio.run_coroutine_threadsafe(ctx.bot.edit_message_text(chat_id=uid, message_id=state["status_msg_id"], text="❌ Error: " + str(err) + "\n\nTry /debug."), loop)
    threading.Thread(target=run_scraper, args=(uid, target, stop_event, on_progress, on_done, on_error), daemon=True).start()

@auth_required
async def cmd_stop(update, ctx):
    uid = update.effective_user.id
    state = user_state[uid]
    if not state["running"]:
        await update.message.reply_text("Nothing is running.")
        return
    state["stop_event"].set()
    state["running"] = False
    count = len(state["results"])
    if state.get("status_msg_id"):
        try:
            await ctx.bot.edit_message_text(chat_id=uid, message_id=state["status_msg_id"], text="⏹ Stopped at " + str(count) + " videos. Use /download to get them.")
            return
        except Exception:
            pass
    await update.message.reply_text("Stopped. Collected " + str(count) + " videos.")

@auth_required
async def cmd_status(update, ctx):
    uid = update.effective_user.id
    state = user_state[uid]
    if state["running"]:
        await update.message.reply_text("Running — " + str(len(state["results"])) + " videos so far.")
    else:
        await update.message.reply_text("Idle — last run: " + str(len(state["results"])) + " videos.")

@auth_required
async def cmd_format(update, ctx):
    uid = update.effective_user.id
    keyboard = [[InlineKeyboardButton("CSV", callback_data="fmt_csv"), InlineKeyboardButton("TXT", callback_data="fmt_txt")]]
    await update.message.reply_text("Current: " + user_state[uid]["fmt"].upper() + "\nChoose output:", reply_markup=InlineKeyboardMarkup(keyboard))

@auth_required
async def cmd_download(update, ctx):
    uid = update.effective_user.id
    results = user_state[uid]["results"]
    if not results:
        await update.message.reply_text("No results yet. Run /scrape first.")
        return
    fmt = user_state[uid]["fmt"]
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    if fmt == "csv":
        buf = StringIO()
        writer = csv.DictWriter(buf, fieldnames=["index","url","scraped_at"])
        writer.writeheader()
        writer.writerows(results)
        await update.message.reply_document(document=buf.getvalue().encode(), filename="tiktok_"+ts+".csv", caption=str(len(results))+" TikTok links")
    else:
        content = "\n".join(r["url"] for r in results)
        await update.message.reply_document(document=content.encode(), filename="tiktok_"+ts+".txt", caption=str(len(results))+" TikTok links")

@auth_required
async def cmd_debug(update, ctx):
    uid = update.effective_user.id
    cookies = get_active_cookies()
    await update.message.reply_text("📸 Taking screenshot (~15s)...\nCookies: " + str(len(cookies)))
    loop = asyncio.get_event_loop()
    def _run():
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu"])
                context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36", viewport={"width":1280,"height":900})
                context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
                if cookies:
                    context.add_cookies(cookies)
                page = context.new_page()
                page.goto("https://www.tiktok.com/foryou", wait_until="domcontentloaded", timeout=30000)
                time.sleep(6)
                page.screenshot(path="/tmp/debug.png")
                final_url = page.url
                browser.close()
            caption = "✅ FYP loaded — cookies working!" if "foryou" in final_url else "⚠️ Login wall detected. Re-export cookies with /cookiehelp."
            with open("/tmp/debug.png","rb") as f:
                asyncio.run_coroutine_threadsafe(ctx.bot.send_photo(chat_id=uid, photo=f, caption=caption), loop)
        except Exception as e:
            asyncio.run_coroutine_threadsafe(ctx.bot.send_message(chat_id=uid, text="Screenshot failed: "+str(e)), loop)
    threading.Thread(target=_run, daemon=True).start()

async def button_handler(update, ctx):
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

async def unknown(update, ctx):
    await update.message.reply_text("Unknown command. Use /help.")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("setcookies", cmd_setcookies))
    app.add_handler(CommandHandler("cookiehelp", cmd_cookiehelp))
    app.add_handler(CommandHandler("cookies",    cmd_cookies))
    app.add_handler(CommandHandler("scrape",     cmd_scrape))
    app.add_handler(CommandHandler("stop",       cmd_stop))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("format",     cmd_format))
    app.add_handler(CommandHandler("download",   cmd_download))
    app.add_handler(CommandHandler("debug",      cmd_debug))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
