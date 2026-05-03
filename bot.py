"""
TikTok FYP Scraper Bot — Complete Rebuild
"""

import os, csv, re, time, asyncio, logging, threading, json
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
TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USERS_RAW  = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USERS      = set(int(x.strip()) for x in ALLOWED_USERS_RAW.split(",") if x.strip())
TIKTOK_COOKIES_RAW = os.environ.get("TIKTOK_COOKIES", "").strip()
# ──────────────────────────────────────────────────────────────────────────────

TIKTOK_VIDEO_RE = re.compile(r'https://www\.tiktok\.com/@[\w.]+/video/\d+')
SAMESITE_MAP    = {"no_restriction": "None", "lax": "Lax", "strict": "Strict", "none": "None"}


def parse_cookies(raw: str) -> list:
    if not raw:
        return []
    try:
        cookies = json.loads(raw)
        cleaned, seen = [], set()
        for c in cookies:
            name = c.get("name", "")
            if not name or name in seen:
                continue
            seen.add(name)
            out = {
                "name": name,
                "value": c.get("value", ""),
                "domain": ".tiktok.com",
                "path": "/",
                "secure": bool(c.get("secure", False)),
                "httpOnly": bool(c.get("httpOnly", False)),
                "sameSite": SAMESITE_MAP.get((c.get("sameSite") or "lax").lower(), "Lax"),
            }
            exp = c.get("expirationDate") or c.get("expires")
            if exp:
                try:
                    out["expires"] = int(float(exp))
                except Exception:
                    pass
            cleaned.append(out)
        return cleaned
    except Exception as e:
        logger.warning("Cookie parse failed: %s", e)
        return []


COOKIES = parse_cookies(TIKTOK_COOKIES_RAW)

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

# ─── STATE ────────────────────────────────────────────────────────────────────
user_state = defaultdict(lambda: {
    "running": False,
    "results": [],
    "stop_event": None,
    "fmt": "csv",
    "status_msg_id": None,
    "target": 0,
})

# ─── COOKIE PROCESSOR ─────────────────────────────────────────────────────────
def process_raw_cookies(raw_text: str):
    try:
        data = json.loads(raw_text.strip())
    except Exception:
        return "", "Could not parse JSON. Make sure you export from Cookie-Editor as JSON format."

    if not isinstance(data, list):
        return "", "Expected a JSON array starting with ["

    cleaned, seen = [], set()
    for c in data:
        name = c.get("name", "") or c.get("Name raw", "")
        value = c.get("value", "") or c.get("Content raw", "")
        if not name or name in seen:
            continue
        seen.add(name)
        raw_ss = c.get("sameSite") or c.get("SameSite raw") or "lax"
        out = {
            "name": name,
            "value": value,
            "domain": ".tiktok.com",
            "path": "/",
            "secure": bool(c.get("secure", False)),
            "httpOnly": bool(c.get("httpOnly", c.get("HTTP only raw", "false")) in [True, "true"]),
            "sameSite": SAMESITE_MAP.get(raw_ss.lower(), "Lax"),
        }
        exp = c.get("expirationDate") or c.get("expires") or c.get("Expires raw")
        if exp:
            try:
                out["expires"] = int(float(exp))
            except Exception:
                pass
        cleaned.append(out)

    names       = {c["name"] for c in cleaned}
    key_cookies = ["sessionid", "sid_tt", "uid_tt", "odin_tt", "msToken"]
    found       = [k for k in key_cookies if k in names]
    missing     = [k for k in key_cookies if k not in names]

    has_session = "sessionid" in names
    sid_val     = next((c["value"][:16] + "..." for c in cleaned if c["name"] == "sessionid"), "not found")

    summary = (
        "Cookie Processing Result\n\n"
        "Total cookies: " + str(len(cleaned)) + "\n"
        "Found: " + ", ".join(found) + "\n"
        + ("Missing: " + ", ".join(missing) + "\n" if missing else "")
        + "\nsessionid: " + sid_val
        + ("\n\nReady to use!" if has_session else "\n\nWarning: no sessionid found - may not work!")
    )

    railway_value = json.dumps(cleaned, separators=(",", ":"))
    return railway_value, summary

# ─── SCRAPER ──────────────────────────────────────────────────────────────────
def run_scraper(user_id, target, stop_event, on_update, on_done, on_error):
    try:
        from playwright.sync_api import sync_playwright

        collected, seen, api_buffer = [], set(), []

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
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
                            body = response.json()
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
                            "index": len(collected) + 1,
                            "url": clean,
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
                                    "index": len(collected) + 1,
                                    "url": clean,
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

                # Track stuck state
                if len(collected) > last_count:
                    stuck_count = 0
                    last_count  = len(collected)
                else:
                    stuck_count += 1

                # Navigate to next video
                try:
                    page.keyboard.press("ArrowDown")
                except Exception:
                    pass
                time.sleep(3)
                scroll_attempts += 1

                # Recovery if stuck
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

                # Every 20 scrolls: hard scroll to trigger lazy loading
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


# ─── BOT COMMANDS ─────────────────────────────────────────────────────────────

@auth_required
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 *TikTok FYP Scraper Bot*\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "▶️ /scrape `<amount>` — Start scraping\n"
        "   _e.g. /scrape 50_\n\n"
        "⏹ /stop — Stop current scrape\n"
        "📊 /status — Check progress\n"
        "📥 /download — Download results\n"
        "✅ /check — Check if session is working\n"
        "🍪 /setcookies — How to add your cookies\n"
        "🖥 /debug — Screenshot what bot sees\n"
        "❓ /help — Show this menu",
        parse_mode="Markdown"
    )

@auth_required
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)

@auth_required
async def cmd_setcookies(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🍪 *How to set your TikTok cookies:*\n\n"
        "1️⃣ Open TikTok in *Kiwi Browser* on Android\n"
        "2️⃣ Install *Cookie-Editor* extension\n"
        "3️⃣ Log into TikTok\n"
        "4️⃣ Open Cookie-Editor\n"
        "5️⃣ Tap *Export* then *JSON*\n"
        "6️⃣ *Paste the JSON directly here* in this chat\n\n"
        "I'll process it and send you the exact value to put in Railway! 🚀",
        parse_mode="Markdown"
    )

@auth_required
async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    msg = await update.message.reply_text("🔍 Checking session...")
    loop = asyncio.get_event_loop()

    def _run():
        import requests as req

        if not COOKIES:
            text = (
                "❌ *No cookies loaded*\n\n"
                "Use /setcookies to learn how to add your cookies,\n"
                "then set `TIKTOK_COOKIES` in Railway."
            )
            asyncio.run_coroutine_threadsafe(
                ctx.bot.edit_message_text(
                    chat_id=uid, message_id=msg.message_id,
                    text=text, parse_mode="Markdown"
                ), loop,
            )
            return

        sid = next((c["value"] for c in COOKIES if c["name"] == "sessionid"), None)
        if not sid:
            asyncio.run_coroutine_threadsafe(
                ctx.bot.edit_message_text(
                    chat_id=uid, message_id=msg.message_id,
                    text="❌ *No sessionid found in cookies*\n\nGet fresh cookies via /setcookies",
                    parse_mode="Markdown"
                ), loop,
            )
            return

        try:
            cookie_dict = {c["name"]: c["value"] for c in COOKIES}
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                "Referer": "https://www.tiktok.com/",
            }
            resp = req.get(
                "https://www.tiktok.com/api/recommend/item_list/?count=1&aid=1988",
                headers=headers,
                cookies=cookie_dict,
                timeout=15,
            )
            if resp.status_code == 200:
                data   = resp.json()
                items  = data.get("itemList") or data.get("aweme_list") or []
                scode  = data.get("statusCode", data.get("status_code", -1))
                if items or scode == 0:
                    text = (
                        "✅ *Session is valid!*\n\n"
                        "🍪 Cookies loaded: " + str(len(COOKIES)) + "\n"
                        "🔑 sessionid: `" + sid[:16] + "...`\n"
                        "📡 API: OK\n\n"
                        "Ready! Use /scrape `<amount>`"
                    )
                else:
                    text = (
                        "⚠️ *Session may be expired*\n"
                        "API status: " + str(scode) + "\n\n"
                        "Try refreshing via /setcookies"
                    )
            elif resp.status_code == 401:
                text = "❌ *Session expired* (401)\n\nGet fresh cookies via /setcookies"
            else:
                text = "⚠️ HTTP " + str(resp.status_code) + " — try /debug to see what TikTok shows"
        except Exception as e:
            text = "❌ Check failed: " + str(e)

        asyncio.run_coroutine_threadsafe(
            ctx.bot.edit_message_text(
                chat_id=uid, message_id=msg.message_id,
                text=text, parse_mode="Markdown"
            ), loop,
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
        await update.message.reply_text("❌ No cookies loaded! Use /setcookies first.")
        return

    args = ctx.args
    try:
        target = max(1, min(500, int(args[0]))) if args else 20
    except (ValueError, IndexError):
        await update.message.reply_text("Usage: /scrape `<amount>`\nExample: /scrape 50", parse_mode="Markdown")
        return

    state["running"]    = True
    state["results"]    = []
    state["target"]     = target
    stop_event          = threading.Event()
    state["stop_event"] = stop_event

    status_msg = await update.message.reply_text(
        "🔄 *Scraping 0 / " + str(target) + " videos...*",
        parse_mode="Markdown"
    )
    state["status_msg_id"] = status_msg.message_id

    loop = asyncio.get_event_loop()

    def on_update(count, total):
        asyncio.run_coroutine_threadsafe(
            ctx.bot.edit_message_text(
                chat_id=uid,
                message_id=state["status_msg_id"],
                text="🔄 *Scraping " + str(count) + " / " + str(total) + " videos...*",
                parse_mode="Markdown"
            ), loop,
        )

    def on_done(results):
        state["running"] = False
        asyncio.run_coroutine_threadsafe(
            ctx.bot.edit_message_text(
                chat_id=uid,
                message_id=state["status_msg_id"],
                text=(
                    "✅ *Done!*\n\n"
                    "🎵 Collected *" + str(len(results)) + " / " + str(target) + "* videos\n"
                    "📥 Use /download to get your file"
                ),
                parse_mode="Markdown"
            ), loop,
        )

    def on_error(err):
        state["running"] = False
        asyncio.run_coroutine_threadsafe(
            ctx.bot.edit_message_text(
                chat_id=uid,
                message_id=state["status_msg_id"],
                text="❌ *Error:* `" + str(err) + "`",
                parse_mode="Markdown"
            ), loop,
        )

    threading.Thread(
        target=run_scraper,
        args=(uid, target, stop_event, on_update, on_done, on_error),
        daemon=True
    ).start()


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
            await ctx.bot.edit_message_text(
                chat_id=uid, message_id=state["status_msg_id"],
                text=text, parse_mode="Markdown"
            )
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
        await update.message.reply_text(
            "🔄 *Running*\n\nCollected: *" + str(count) + " / " + str(state["target"]) + "* videos",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "💤 *Idle*\n\nLast run: *" + str(count) + "* videos\nUse /scrape `<amount>` to start",
            parse_mode="Markdown"
        )


@auth_required
async def cmd_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    results = user_state[uid]["results"]
    if not results:
        await update.message.reply_text("No results yet. Run /scrape `<amount>` first.", parse_mode="Markdown")
        return
    fmt = user_state[uid]["fmt"]
    ts  = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    if fmt == "csv":
        buf    = StringIO()
        writer = csv.DictWriter(buf, fieldnames=["index", "url", "scraped_at"])
        writer.writeheader()
        writer.writerows(results)
        await update.message.reply_document(
            document=buf.getvalue().encode(),
            filename="tiktok_" + ts + ".csv",
            caption="🎵 " + str(len(results)) + " TikTok video links"
        )
    else:
        content = "\n".join(r["url"] for r in results)
        await update.message.reply_document(
            document=content.encode(),
            filename="tiktok_" + ts + ".txt",
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
                    args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu"]
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
                        caption="🖥 What TikTok shows the bot.\n✅ Feed visible = working\n❌ Login wall = cookies expired"
                    ), loop,
                )
        except Exception as e:
            asyncio.run_coroutine_threadsafe(
                ctx.bot.send_message(chat_id=uid, text="❌ Screenshot failed: " + str(e)), loop,
            )

    threading.Thread(target=_run, daemon=True).start()


# ─── COOKIE PASTE HANDLER ─────────────────────────────────────────────────────
@auth_required
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = (update.message.text or "").strip()

    if not (text.startswith("[") and text.endswith("]")):
        await update.message.reply_text("Use /help to see commands.\nTo set cookies, use /setcookies first.")
        return

    await update.message.reply_text("⚙️ Processing your cookies...")
    railway_value, summary = process_raw_cookies(text)

    if not railway_value:
        await update.message.reply_text("❌ " + summary)
        return

    await update.message.reply_text(summary)
    await update.message.reply_document(
        document=railway_value.encode(),
        filename="TIKTOK_COOKIES.txt",
        caption=(
            "📋 Copy the contents of this file.\n\n"
            "In Railway:\n"
            "1. Go to your service Variables\n"
            "2. Add variable named: TIKTOK_COOKIES\n"
            "3. Paste file contents as the value\n"
            "4. Redeploy\n\n"
            "Then use /check to verify it works!"
        )
    )


async def unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Unknown command. Use /help.")


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("scrape",     cmd_scrape))
    app.add_handler(CommandHandler("stop",       cmd_stop))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("download",   cmd_download))
    app.add_handler(CommandHandler("check",      cmd_check))
    app.add_handler(CommandHandler("setcookies", cmd_setcookies))
    app.add_handler(CommandHandler("debug",      cmd_debug))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)


async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()


if __name__ == "__main__":
    main()
