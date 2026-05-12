#!/usr/bin/env python3
"""
Hermes Agent v2 - Full WordPress control + self-upgrade + site health
for tensoredge.net
Author: Mohamed (TensorEdge)
"""

import os
import re
import json
import logging
import subprocess
import tempfile
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ── Logging ──────────────────────────────────────────────────────────────────────
os.makedirs("/app/logs", exist_ok=True)
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("/app/logs/hermes.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("hermes")

# ── Config ───────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN       = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USER_ID      = int(os.environ["ALLOWED_TELEGRAM_USER_ID"])
WP_URL               = os.environ.get("WP_INTERNAL_URL", "http://wordpress")
WP_API               = f"{WP_URL}/wp-json/wp/v2"
WP_USER              = os.environ["WP_USER"]
WP_APP_PASSWORD      = os.environ["WP_APP_PASSWORD"]
WP_AUTH              = (WP_USER, WP_APP_PASSWORD)
WP_CONTAINER         = os.environ.get("WP_CONTAINER", "mohelal-wordpress-1")
OLLAMA_URL           = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL         = os.environ.get("OLLAMA_MODEL", "mistral-small")
DOHA_TZ              = ZoneInfo("Asia/Qatar")

# ── State ────────────────────────────────────────────────────────────────────────
chat_mode_users: set = set()
pending_confirmations: dict = {}   # message_id -> {"action": str, "data": dict}

# ── Dynamic cron scheduler ────────────────────────────────────────────────────────
CRON_FILE     = "/app/logs/cron_jobs.json"
_scheduler    = None   # set in main()
_bot_instance = None   # set in main()

CRON_ACTIONS = {
    "digest":   "Daily news digest from all sources including Hacker News",
    "trending": "AI-picked trending stories",
    "health":   "Site health report",
    "status":   "Quick site stats",
}

def load_cron_jobs() -> dict:
    if os.path.exists(CRON_FILE):
        try:
            with open(CRON_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_cron_jobs(jobs: dict):
    with open(CRON_FILE, "w") as f:
        json.dump(jobs, f, indent=2)

def parse_time(time_str: str) -> tuple:
    """Parse HH:MM into (hour, minute). Supports 24h and 12h formats."""
    time_str = time_str.strip().upper().replace(".", ":")
    try:
        if "AM" in time_str or "PM" in time_str:
            t = datetime.strptime(time_str.replace(" ",""), "%I:%M%p")
        else:
            t = datetime.strptime(time_str, "%H:%M")
        return t.hour, t.minute
    except Exception:
        raise ValueError("Invalid time format. Use HH:MM (e.g. 08:00 or 9:30)")

async def run_cron_action(action: str):
    """Execute a scheduled action and send result to Mohamed via Telegram."""
    global _bot_instance
    if not _bot_instance:
        return
    try:
        if action == "digest":
            text = build_digest()
            await _bot_instance.send_message(chat_id=ALLOWED_USER_ID, text=text,
                                             disable_web_page_preview=True)
        elif action == "trending":
            top = pick_top_stories(get_trending())
            if top:
                await _bot_instance.send_message(chat_id=ALLOWED_USER_ID,
                                                 text=build_trending_msg(top),
                                                 disable_web_page_preview=True)
        elif action == "health":
            report = get_site_health()
            await _bot_instance.send_message(chat_id=ALLOWED_USER_ID, text=report)
        elif action == "status":
            stats = wp_stats()
            await _bot_instance.send_message(
                chat_id=ALLOWED_USER_ID,
                text="TensorEdge Status\nPublished: " + str(stats["published"]) +
                     "\nDrafts: " + str(stats["drafts"])
            )
        logger.info(f"Cron action '{action}' executed successfully.")
    except Exception as e:
        logger.error(f"Cron action '{action}' failed: {e}")

def register_cron_job(job_id: str, action: str, hour: int, minute: int):
    """Add a job to APScheduler."""
    global _scheduler
    if not _scheduler:
        return
    # Remove existing job with same id if present
    try:
        _scheduler.remove_job(job_id)
    except Exception:
        pass
    _scheduler.add_job(
        run_cron_action,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=DOHA_TZ),
        id=job_id,
        args=[action],
        replace_existing=True,
    )
    logger.info(f"Cron job '{job_id}' registered: {action} at {hour:02d}:{minute:02d} Doha")

def reload_all_cron_jobs():
    """Load persisted jobs from disk and register them in the scheduler."""
    jobs = load_cron_jobs()
    for job_id, job in jobs.items():
        try:
            register_cron_job(job_id, job["action"], job["hour"], job["minute"])
        except Exception as e:
            logger.error(f"Failed to reload cron job {job_id}: {e}")
    logger.info(f"Reloaded {len(jobs)} cron jobs from disk.")

# ── Digest sources ───────────────────────────────────────────────────────────────
DIGEST_SOURCES = [
    {"name": "Videocardz",           "url": "https://feeds.feedburner.com/VideoCardzcom",          "emoji": "🎮"},
    {"name": "Wccftech Hardware",    "url": "https://wccftech.com/topic/hardware/feed",             "emoji": "💻"},
    {"name": "r/MiniPCs",            "url": "https://www.reddit.com/r/MiniPCs/.rss",               "emoji": "🖥️"},
    {"name": "r/OpenClaw",           "url": "https://www.reddit.com/r/OpenClaw/.rss",              "emoji": "🦾"},
    {"name": "Towards Data Science", "url": "https://towardsdatascience.com/feed",                  "emoji": "🤖"},
]
HN_TOP_COUNT = 5   # Hacker News top stories

# ── News Monitor Sources ──────────────────────────────────────────────────────
NEWS_SOURCES = [
    # ── Hardware / Mini PC Focus ──────────────────────────────────────────────
    {"name": "WCCFTech",        "url": "https://wccftech.com/feed/",                      "emoji": "💻"},
    {"name": "Tom's Hardware",  "url": "https://www.tomshardware.com/feeds/all",           "emoji": "🔧"},
    {"name": "NotebookCheck",   "url": "https://www.notebookcheck.net/News.152.100.html",  "emoji": "📓"},
    {"name": "ExtremeTech",     "url": "https://www.extremetech.com/feed",                "emoji": "⚗️"},
    {"name": "HotHardware",     "url": "https://hothardware.com/rss/news.aspx",           "emoji": "🔥"},
    # ── Broad Tech News ───────────────────────────────────────────────────────
    {"name": "TechCrunch",      "url": "https://techcrunch.com/feed/",                    "emoji": "🚀"},
    {"name": "ZDNet",           "url": "https://www.zdnet.com/news/rss.xml",              "emoji": "📡"},
    {"name": "TechRepublic",    "url": "https://www.techrepublic.com/rssfeeds/articles/", "emoji": "📰"},
    {"name": "CNET",            "url": "https://www.cnet.com/rss/news/",                  "emoji": "📺"},
    {"name": "How-To Geek",     "url": "https://www.howtogeek.com/feed/",                 "emoji": "🛠️"},
    {"name": "gHacks",          "url": "https://www.ghacks.net/feed/",                   "emoji": "🔍"},
    # ── Manufacturer / Official ───────────────────────────────────────────────
    {"name": "NVIDIA Dev Blog", "url": "https://developer.nvidia.com/blog/feed",          "emoji": "🟢"},
]
NEWS_SEEN_FILE = "/app/logs/news_seen.json"
_news_cache: dict = {}  # short_key -> {url, source, title, desc}

NEWS_FILTER_PROMPT = (
    "You are a news curator for TensorEdge.net, a tech blog about mini PCs, "
    "local AI, AMD/Intel/NVIDIA hardware, CPU/GPU benchmarks, and AI tools.\n\n"
    "Score this news item 1-10 for relevance:\n"
    "9-10: mini PCs, AMD APUs, local LLM, AI hardware\n"
    "6-8: CPU/GPU launches, gaming performance, PC hardware\n"
    "4-5: broader tech news, AI tools, Windows\n"
    "1-3: phones, enterprise cloud, politics, sports\n\n"
    "Reply ONLY with JSON: {relevant: bool, score: int, reason: string}"
)

HELP_TEXT = """Hermes v2 - TensorEdge.net Full Control

PUBLISHING
/fetch <reddit_url> - Fetch, reformat and save as draft
/approve <id> - Publish a draft
/drafts - List drafts
/published - List published posts
/edit <id> <instruction> - AI-edit a post
/seo <id> - SEO analysis
/unpublish <id> - Revert to draft
/delete <id> - Delete a post
/image <id> - Upload image and set as featured

CONTENT
/linkedin <id> - Format post for LinkedIn
/digest - Get news digest now
/trending - AI-picked trending stories
/newswatch - Check news sources and get stories to publish
/publish <url> - Fetch any article URL, generate and publish as a post
/fix - Find posts missing images, fetch from source or offer to delete
/replace <id> <old> | <new> - Direct text replacement in post

WORDPRESS CONTROL
/plugin list - Show all plugins
/plugin install <name> - Install a plugin
/plugin activate <name> - Activate a plugin
/plugin deactivate <name> - Deactivate a plugin
/theme list - List themes
/theme activate <name> - Activate a theme
/pages - List all pages
/wpcli <command> - Run raw WP-CLI command

SITE
/health - Full site health report
/status - Quick site stats

HERMES
/install <github_url> - Install Python module from GitHub
/github - Sync agent code to GitHub
/chat - Toggle free conversation mode
/help - Show this menu"""

# ── Auth guard ───────────────────────────────────────────────────────────────────
def is_authorized(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID

async def deny(update: Update):
    await update.message.reply_text("Unauthorized.")

# ── WP-CLI helper ────────────────────────────────────────────────────────────────
def wpcli(command: str, allow_root: bool = True) -> tuple[bool, str]:
    """Run a WP-CLI command inside the WordPress container."""
    root_flag = "--allow-root" if allow_root else ""
    full_cmd  = f"docker exec {WP_CONTAINER} wp {command} {root_flag} --path=/var/www/html 2>&1"
    try:
        result = subprocess.run(
            full_cmd, shell=True, capture_output=True, text=True, timeout=60
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "Command timed out after 60 seconds"
    except Exception as e:
        return False, str(e)

# ── Site health ──────────────────────────────────────────────────────────────────
def get_site_health() -> str:
    lines = ["Site Health Report - tensoredge.net\n"]

    # WordPress + PHP version
    ok, out = wpcli("core version")
    lines.append("WordPress: " + (out.strip() if ok else "unknown"))

    ok, out = wpcli("eval 'echo PHP_VERSION;'")
    lines.append("PHP: " + (out.strip() if ok else "unknown"))

    # Plugin count
    ok, out = wpcli("plugin list --format=count")
    lines.append("Total plugins: " + (out.strip() if ok else "unknown"))

    ok, out = wpcli("plugin list --status=active --format=count")
    lines.append("Active plugins: " + (out.strip() if ok else "unknown"))

    # Active theme
    ok, out = wpcli("theme list --status=active --field=name")
    lines.append("Active theme: " + (out.strip() if ok else "unknown"))

    # Database size
    ok, out = wpcli("db size --size_format=mb")
    lines.append("Database size: " + (out.strip() + " MB" if ok else "unknown"))

    # Disk usage of wp-content
    try:
        result = subprocess.run(
            f"docker exec {WP_CONTAINER} du -sh /var/www/html/wp-content 2>/dev/null",
            shell=True, capture_output=True, text=True, timeout=15
        )
        disk = result.stdout.split()[0] if result.stdout else "unknown"
        lines.append("wp-content size: " + disk)
    except Exception:
        lines.append("wp-content size: unknown")

    # Post counts
    ok, out = wpcli("post list --status=publish --format=count")
    lines.append("Published posts: " + (out.strip() if ok else "unknown"))

    ok, out = wpcli("post list --status=draft --format=count")
    lines.append("Drafts: " + (out.strip() if ok else "unknown"))

    # Check for updates
    ok, out = wpcli("core check-update --format=count")
    update_status = "Up to date" if (ok and out.strip() == "0") else "Updates available"
    lines.append("Core updates: " + update_status)

    ok, out = wpcli("plugin list --update=available --format=count")
    lines.append("Plugin updates available: " + (out.strip() if ok else "unknown"))

    lines.append("\nLast checked: " + datetime.now(DOHA_TZ).strftime("%H:%M Doha time"))
    return "\n".join(lines)

# ── Reddit (public JSON) ─────────────────────────────────────────────────────────
def extract_reddit_images(post: dict) -> list:
    """Extract image URLs in the correct order.
    For gallery posts: uses gallery_data ordering (preserves Reddit gallery order).
    For inline posts: scans selftext for preview.redd.it URLs in reading order.
    Falls back to media_metadata if nothing else found.
    """
    import re
    images = []
    seen   = set()
    selftext = post.get("selftext", "")

    # Build a lookup from media_metadata: item_id -> full URL
    meta_lookup = {}
    if post.get("media_metadata"):
        for item_id, item in post["media_metadata"].items():
            try:
                src = item.get("s", {})
                img_url = src.get("u") or src.get("gif")
                if img_url:
                    clean = img_url.replace("&amp;", "&")
                    base  = clean.split("?")[0]
                    meta_lookup[item_id] = clean
                    meta_lookup[base]    = clean
            except Exception:
                continue

    # Case 0 — True Reddit gallery (is_gallery=True with gallery_data ordering)
    # This handles posts where images are uploaded as a gallery separate from selftext
    if post.get("is_gallery") and post.get("gallery_data"):
        for item in post["gallery_data"].get("items", []):
            media_id = str(item.get("media_id", ""))
            url = meta_lookup.get(media_id, "")
            if url:
                base = url.split("?")[0]
                if base not in seen:
                    seen.add(base)
                    images.append(url)
        # If we got gallery images, return them — selftext has the text content
        if images:
            return images[:20]

    # Extract images IN ORDER from selftext
    # Pattern: preview.redd.it or i.redd.it URLs anywhere in selftext
    combined_pattern = r"https?://(?:preview|i)[.]redd[.]it/[^\s)>]+"
    for m in re.finditer(combined_pattern, selftext):
        raw_url = m.group(0).replace("&amp;", "&")
        base    = raw_url.split("?")[0]
        if base not in seen:
            seen.add(base)
            images.append(raw_url)

    # If no selftext images found, fall back to media_metadata order
    if not images and meta_lookup:
        for item_id, url in meta_lookup.items():
            if not item_id.startswith("http"):  # skip base-URL keys, use ID keys
                base = url.split("?")[0]
                if base not in seen:
                    seen.add(base)
                    images.append(url)

    # Single image post fallback
    if not images and post.get("post_hint") == "image":
        url = post.get("url", "")
        if url and any(ext in url.lower() for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
            images.append(url)

    # Preview image last resort
    if not images and post.get("preview"):
        try:
            preview = post["preview"]["images"][0]["source"]["url"]
            images.append(preview.replace("&amp;", "&"))
        except Exception:
            pass

    return images[:20]

def download_image(url: str) -> tuple[bytes, str]:
    """Download image bytes and detect mime type."""
    r = requests.get(url, headers={"User-Agent": "HermesAgent/2.0 by TensorEdge"}, timeout=20)
    r.raise_for_status()
    content_type = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    # Normalize mime type
    mime_map = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}
    mime = mime_map.get(content_type, content_type)
    if mime not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        mime = "image/jpeg"
    return r.content, mime

def fetch_reddit(url: str) -> dict:
    clean = url.split("?")[0].rstrip("/") + ".json"
    r = requests.get(
        clean,
        headers={"User-Agent": "HermesAgent/2.0 by TensorEdge"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    post = data[0]["data"]["children"][0]["data"]
    comments = []
    try:
        for child in data[1]["data"]["children"][:5]:
            d    = child.get("data", {})
            body = d.get("body", "")
            if body and body not in ("[deleted]", "[removed]"):
                comments.append({"author": d.get("author",""), "score": d.get("score",0), "body": body[:600]})
    except Exception:
        pass
    return {
        "title":     post.get("title", ""),
        "selftext":  post.get("selftext", ""),
        "subreddit": post.get("subreddit", ""),
        "score":     post.get("score", 0),
        "author":    post.get("author", ""),
        "permalink": "https://reddit.com" + post.get("permalink", ""),
        "comments":  comments,
        "images":    extract_reddit_images(post),
        "post_type": "gallery" if post.get("is_gallery") else
                     "image"   if post.get("post_hint") == "image" else
                     "link"    if post.get("post_hint") == "link" else "text",
    }

# ── Ollama (Mistral Small 22B) ────────────────────────────────────────────────────
def ollama_chat(system: str, user: str, timeout: int = 120) -> str:
    r = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model":   OLLAMA_MODEL,
            "stream":  False,
            "options": {"num_predict": 4096, "temperature": 0.3},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["message"]["content"].strip()

# ── LLM tasks ────────────────────────────────────────────────────────────────────
HERMES_IDENTITY = (
    "You are Hermes, the publishing assistant for tensoredge.net owned by Mohamed, "
    "a Lead Cloud & AI Engineer and tech reviewer in Doha, Qatar.\n\n"
    "CRITICAL RULES - NEVER BREAK THESE:\n"
    "1. NEVER fabricate data, stats, dates, or results\n"
    "2. If you cannot check something with a real tool, say: I cannot verify that, use /health or /status\n"
    "3. You do NOT have browser access or internet access directly\n"
    "4. You can only act through WordPress REST API and WP-CLI commands\n"
    "5. Always be honest about your limitations\n\n"
    "Your real capabilities:\n"
    "- Publish, edit, delete WordPress posts and pages via REST API\n"
    "- Run WP-CLI commands inside the WordPress container\n"
    "- Fetch and reformat Reddit posts\n"
    "- Run SEO analysis and LinkedIn formatting via Mistral Small\n"
    "- Show real site health data via WP-CLI\n"
    "- Install Python modules from GitHub into yourself\n\n"
    "Available commands: /fetch /approve /drafts /published /edit /seo /unpublish "
    "/delete /image /linkedin /digest /trending /plugin /theme /pages /wpcli "
    "/health /status /install /chat\n\n"
    "Be concise, direct, honest. Never pretend to do something you have not actually done."
)

def markdown_to_html(text: str, wp_image_map: dict = None, skip_first_image: str = "") -> str:
    """Convert Reddit markdown to HTML.
    wp_image_map: {original_url_base: wordpress_media_url} for inline image replacement.
    skip_first_image: base URL of first image to skip (already used as hero).
    """
    import html as html_mod
    wp_image_map = wp_image_map or {}
    first_image_skipped = [False]  # mutable flag for closure

    # Decode common HTML entities Reddit uses
    import html as _html
    text = _html.unescape(text)

    # Unescape Reddit's backslash escapes: \( -> (, \) -> ), \+ -> +, \~ -> ~
    text = re.sub(r"\\([\(\)\+\~\[\]\*\_\`\#\!\-\.])", r"\1", text)

    def make_figure(url: str, caption: str = "") -> str:
        url    = url.replace("&amp;", "&")
        base   = url.split("?")[0]
        # Skip the first image — it is already injected as the hero
        if skip_first_image and not first_image_skipped[0]:
            if base == skip_first_image or base in skip_first_image or skip_first_image in base:
                first_image_skipped[0] = True
                return ""  # return empty — caption dedup will clean up the text
        wp_url = wp_image_map.get(base, url)
        alt    = caption or "Image"
        fig    = f'<figure class="wp-block-image size-large"><img src="{wp_url}" alt="{alt}"/>'
        if caption:
            fig += f'<figcaption>{caption}</figcaption>'
        fig += "</figure>"
        return fig

    # Reddit images: [caption](url) WITHOUT leading ! — must match before generic links
    # Track captions that get converted so we can remove duplicate paragraph text
    converted_captions = set()
    def replace_reddit_img(m):
        cap = m.group(1).strip()
        url = m.group(2)
        if cap:
            converted_captions.add(cap)
        return "\n" + make_figure(url, cap) + "\n"

    text = re.sub(
        r"(?<!!)\[([^\]]*)\]\((https?://(?:preview|i)\.redd\.it/[^)]+)\)",
        replace_reddit_img,
        text
    )

    # Remove lines that are just the caption text (now duplicated as paragraph)
    if converted_captions:
        lines_temp = text.split("\n")
        cleaned = []
        for ln in lines_temp:
            stripped = ln.strip()
            if stripped in converted_captions:
                continue  # skip duplicate caption line
            cleaned.append(ln)
        text = "\n".join(cleaned)

    # Regular markdown links [text](url) -> <a href>
    # Must run AFTER image replacement so redd.it images are already converted
    def replace_link(m):
        link_text = m.group(1).strip()
        link_url  = m.group(2).strip().replace("&amp;", "&")
        # Skip if already converted to figure
        if "preview.redd.it" in link_url or "i.redd.it" in link_url:
            return m.group(0)
        return f'<a href="{link_url}" target="_blank" rel="noopener">{link_text}</a>'

    text = re.sub(
        r"(?<!!)\[([^\]]+)\]\((https?://[^\)]+)\)",
        replace_link,
        text
    )

    # Standard markdown images: ![caption](url)
    text = re.sub(
        r"!\[([^\]]*)\]\(([^\)]+)\)",
        lambda m: make_figure(m.group(2), m.group(1).strip()),
        text
    )

    # Plain preview.redd.it URLs on their own line
    text = re.sub(
        r"(?m)^(https?://(?:preview|i)\.redd\.it/\S+)$",
        lambda m: make_figure(m.group(1)),
        text
    )

    # Convert markdown tables to HTML
    def fmt_cell(c: str) -> str:
        """Apply inline markdown formatting inside a table cell."""
        c = c.strip()
        c = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", c)
        c = re.sub(r"\*(.+?)\*",       r"<em>\1</em>",         c)
        c = re.sub(r"`(.+?)`",           r"<code>\1</code>",      c)
        c = re.sub(r"\\\+",           r"+",                     c)  # unescape \+
        return c

    def convert_table(block: str) -> str:
        rows = [r.strip() for r in block.strip().split("\n") if r.strip()]
        if len(rows) < 2:
            return block

        # Parse all rows, skip separator rows
        data_rows = []
        for row in rows:
            if re.match(r"^\|[\s:\-|]+\|$", row):
                continue  # skip |:-|:-| alignment rows
            # Split by | and clean each cell
            cells = [fmt_cell(c) for c in row.strip("|").split("|")]
            cells = [c for c in cells if True]  # keep all cells including empty
            data_rows.append(cells)

        if not data_rows:
            return block

        # Normalize all rows to same column count as header
        num_cols = len(data_rows[0])
        normalized = []
        for row in data_rows:
            if len(row) < num_cols:
                row = row + [""] * (num_cols - len(row))
            elif len(row) > num_cols:
                row = row[:num_cols]
            normalized.append(row)

        # Build HTML table
        header = normalized[0]
        body   = normalized[1:]

        html  = '<figure class="wp-block-table">\n<table>\n'
        html += "<thead><tr>" + "".join(f"<th>{c}</th>" for c in header) + "</tr></thead>\n"
        html += "<tbody>\n"
        for row in body:
            html += "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>\n"
        html += "</tbody>\n</table>\n</figure>"
        return html

    # Detect and convert table blocks (consecutive lines starting with |)
    def replace_tables(text: str) -> str:
        lines  = text.split("\n")
        output = []
        table_buf = []
        for line in lines:
            if line.strip().startswith("|"):
                table_buf.append(line)
            else:
                if table_buf:
                    output.append(convert_table("\n".join(table_buf)))
                    table_buf = []
                output.append(line)
        if table_buf:
            output.append(convert_table("\n".join(table_buf)))
        return "\n".join(output)

    text = replace_tables(text)

    # Now process line by line
    lines   = text.split("\n")
    output  = []
    in_list = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_list:
                output.append("</ul>")
                in_list = False
            output.append("")  # preserve blank line for paragraph spacing
            continue
        # Pass through already-converted HTML blocks
        if stripped.startswith(("<figure", "<h", "<ul", "<li", "<table", "<thead",
                                "<tbody", "<tr", "<th", "<td", "</", "<p><strong>")):
            if in_list and not stripped.startswith("<li"):
                output.append("</ul>")
                in_list = False
            output.append(stripped)
            continue
        # Bullet points
        if stripped.startswith(("* ", "- ", "+ ")):
            if not in_list:
                output.append("<ul>")
                in_list = True
            item = stripped[2:].strip()
            item = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", item)
            item = re.sub(r"\*(.+?)\*",       r"<em>\1</em>",         item)
            item = item.replace("&amp;", "&")
            output.append("<li>" + item + "</li>")
            continue
        if in_list:
            output.append("</ul>")
            in_list = False
        # Headers
        if stripped.startswith("### "):
            output.append("<h3>" + html_mod.escape(stripped[4:]) + "</h3>")
        elif stripped.startswith("## "):
            output.append("<h2>" + html_mod.escape(stripped[3:]) + "</h2>")
        elif stripped.startswith("# "):
            output.append("<h2>" + html_mod.escape(stripped[2:]) + "</h2>")
        else:
            line_out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", stripped)
            line_out = re.sub(r"\*(.+?)\*",       r"<em>\1</em>",         line_out)
            line_out = re.sub(r"`(.+?)`",            r"<code>\1</code>",     line_out)
            line_out = line_out.replace("&amp;", "&")
            output.append("<p>" + line_out + "</p>")

    if in_list:
        output.append("</ul>")
    return "\n".join(output)

def reformat_for_wordpress(reddit: dict) -> dict:
    """Convert Reddit post directly to WordPress format — no LLM rewrite."""
    title   = reddit["title"]
    body    = reddit.get("selftext", "") or ""
    content = ""

    # Source attribution block
    permalink  = reddit["permalink"]
    subreddit  = reddit["subreddit"]
    score      = reddit["score"]
    content += (
        f'<p><strong>Source:</strong> <a href="{permalink}" target="_blank">' +
        f'r/{subreddit} — {score} upvotes</a></p>\n'
    )

    # Main body
    if body:
        content += markdown_to_html(body) + "\n"
    else:
        content += "<p><em>No body text — see images below.</em></p>\n"

    # Community reactions removed — only post author content

    # Auto-generate tags from title + subreddit
    stopwords = {"the","a","an","and","or","but","in","on","at","to","for",
                 "of","with","is","was","are","be","has","have","it","this","that"}
    words = re.findall(r"[a-zA-Z]{3,}", title.lower())
    tags  = list(dict.fromkeys([w for w in words if w not in stopwords]))[:5]
    tags.append(reddit["subreddit"])

    # Excerpt — clean first 160 chars, strip any markdown image syntax
    clean_body = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", body)  # strip [text](url)
    clean_body = re.sub(r"https?://\S+", "", clean_body)               # strip raw URLs
    clean_body = re.sub(r"\s+", " ", clean_body).strip()
    excerpt = (clean_body[:157] + "...") if len(clean_body) > 160 else (clean_body or title)

    return {
        "title":   title,
        "content": content,
        "excerpt": excerpt[:160],
        "tags":    tags[:6],
    }

def edit_post_content(content: str, instruction: str) -> str:
    system = "You are a WordPress HTML editor. Apply the instruction. Return ONLY updated HTML, no explanation."
    return ollama_chat(system, "Instruction: " + instruction + "\n\nARTICLE:\n" + content[:8000], timeout=300)

def analyse_seo(title: str, content: str) -> str:
    system = "You are an SEO expert. Analyse the article. Give score /10 and 3 actionable fixes. Be concise."
    return ollama_chat(system, "TITLE: " + title + "\n\nCONTENT:\n" + content[:3000], timeout=60)

def mistral_html_cleanup(html: str, title: str) -> tuple[str, str]:
    """Ask Mistral to fix HTML formatting issues without changing content.
    Returns (cleaned_html, report_of_changes).
    """
    system = (
        "You are an HTML formatting assistant for a WordPress tech blog. "
        "Your ONLY job is to fix formatting issues in the HTML provided. "
        "STRICT RULES:\n"
        "1. NEVER change any words, facts, numbers, or meaning\n"
        "2. NEVER add or remove content\n"
        "3. ONLY fix these specific issues:\n"
        "   - Convert remaining markdown links [text](url) to <a href=\"url\" target=\"_blank\">text</a>\n"
        "   - Convert remaining **bold** to <strong>bold</strong>\n"
        "   - Convert remaining *italic* to <em>italic</em>\n"
        "   - Remove backslash escapes: \\( -> (, \\) -> ), \\+ -> +, \\~ -> ~\n"
        "   - Fix &amp; that should be &\n"
        "   - Fix &#37; that should be %\n"
        "   - Fix \\~ that should be ~\n"
        "   - Ensure paragraphs are wrapped in <p> tags\n"
        "   - Remove any raw markdown that leaked through\n"
        "4. Return ONLY valid JSON with no markdown fences:\n"
        '{"html": "the fixed HTML here", "changes": ["change 1", "change 2"]}\n'
        "If no changes needed, return the original HTML unchanged with changes as empty list.\n"
        "Keep the changes list SHORT and specific, max 5 items."
    )
    user = f"ARTICLE TITLE: {title}\n\nHTML TO FIX:\n{html[:12000]}"

    try:
        raw = ollama_chat(system, user, timeout=180)
        raw = re.sub(r"^```json\s*", "", raw.strip())
        raw = re.sub(r"\s*```$", "", raw)
        raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)
        result   = json.loads(raw)
        fixed    = result.get("html", html)
        changes  = result.get("changes", [])
        report   = ("Mistral formatting fixes:\n" + "\n".join(f"• {c}" for c in changes)) if changes else "No formatting issues found."
        return fixed, report
    except Exception as e:
        logger.warning(f"Mistral cleanup failed: {e}")
        return html, "Mistral cleanup skipped (timeout or parse error) — please review manually."

def generate_seo_meta(title: str, content: str, post_type: str) -> dict:
    """Use Mistral to generate SEO title, meta description and focus keyword."""
    system = (
        "You are an SEO expert for TensorEdge.net, a tech blog about mini PCs, "
        "local LLM inference, AI hardware, and benchmarks by Mohamed Helal in Doha, Qatar.\n\n"
        "Generate SEO metadata for the given article. Rules:\n"
        "1. SEO title: max 60 chars, include main product/topic + brand TensorEdge at end\n"
        "2. Meta description: 140-155 chars, compelling, includes key specs/numbers if available\n"
        "3. Focus keyword: 2-5 words, what someone would search for\n"
        "4. Category: one of: Mini PC Reviews, Local AI, Buying Guides, Benchmarks\n\n"
        "Return ONLY valid JSON, no markdown fences:\n"
        '{"seo_title":"...","meta_desc":"...","focus_keyword":"...","category":"..."}'
    )
    user = (
        "POST TITLE: " + title + "\n"
        "POST TYPE: " + post_type + "\n\n"
        "CONTENT EXCERPT:\n" + content[:2000]
    )
    try:
        raw = ollama_chat(system, user, timeout=60)
        raw = re.sub(r"^```json\s*", "", raw.strip())
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"SEO generation failed: {e}")
        return {}

def set_yoast_meta(post_id: int, seo_title: str, meta_desc: str, focus_keyword: str, category: str):
    """Set Yoast SEO metadata via WordPress REST API."""
    # Set Yoast meta fields
    requests.post(f"{WP_API}/posts/{post_id}", json={
        "meta": {
            "_yoast_wpseo_title":    seo_title,
            "_yoast_wpseo_metadesc": meta_desc,
            "_yoast_wpseo_focuskw":  focus_keyword,
        }
    }, auth=WP_AUTH, timeout=10)

    # Assign category
    cat_response = requests.get(f"{WP_API}/categories",
        params={"search": category, "per_page": 1}, auth=WP_AUTH, timeout=10)
    if cat_response.ok and cat_response.json():
        cat_id = cat_response.json()[0]["id"]
        requests.post(f"{WP_API}/posts/{post_id}",
            json={"categories": [cat_id]}, auth=WP_AUTH, timeout=10)

def format_for_linkedin(title: str, content: str, url: str) -> str:
    system = (
        "Write a LinkedIn post for Mohamed, a senior Cloud & AI Engineer in Doha. "
        "Strong hook, short paragraphs, 3-5 emojis, Key Takeaways section, "
        "engagement question, 5-7 hashtags, under 1300 chars, include URL. "
        "Return ONLY the post text."
    )
    return ollama_chat(system, "Title: " + title + "\nURL: " + url + "\n\nContent:\n" + content[:3000], timeout=120)

# ── WordPress REST API ────────────────────────────────────────────────────────────
def resolve_tags(tags: list) -> list:
    ids = []
    for tag in tags:
        r = requests.get(f"{WP_API}/tags", params={"search": tag}, auth=WP_AUTH, timeout=10)
        hits = r.json()
        if hits:
            ids.append(hits[0]["id"])
        else:
            nr = requests.post(f"{WP_API}/tags", json={"name": tag}, auth=WP_AUTH, timeout=10)
            if nr.ok:
                ids.append(nr.json()["id"])
    return ids

def wp_create_draft(title, content, excerpt, tags) -> dict:
    r = requests.post(f"{WP_API}/posts",
        json={"title": title, "content": content, "excerpt": excerpt,
              "status": "draft", "tags": resolve_tags(tags)},
        auth=WP_AUTH, timeout=15)
    r.raise_for_status()
    return r.json()

def wp_publish(post_id: int) -> dict:
    r = requests.post(f"{WP_API}/posts/{post_id}", json={"status": "publish"}, auth=WP_AUTH, timeout=10)
    r.raise_for_status()
    return r.json()

def wp_unpublish(post_id: int) -> dict:
    r = requests.post(f"{WP_API}/posts/{post_id}", json={"status": "draft"}, auth=WP_AUTH, timeout=10)
    r.raise_for_status()
    return r.json()

def wp_delete(post_id: int) -> bool:
    return requests.delete(f"{WP_API}/posts/{post_id}", auth=WP_AUTH, timeout=10).ok

def wp_list(status: str = "draft", per_page: int = 8) -> list:
    r = requests.get(f"{WP_API}/posts",
        params={"status": status, "per_page": per_page, "_fields": "id,title,date,link,status"},
        auth=WP_AUTH, timeout=10)
    r.raise_for_status()
    return r.json()

def wp_get_post(post_id: int) -> dict:
    r = requests.get(f"{WP_API}/posts/{post_id}", auth=WP_AUTH, timeout=10)
    r.raise_for_status()
    return r.json()

def wp_update(post_id: int, **fields) -> dict:
    r = requests.post(f"{WP_API}/posts/{post_id}", json=fields, auth=WP_AUTH, timeout=15)
    r.raise_for_status()
    return r.json()

def wp_stats() -> dict:
    d = requests.get(f"{WP_API}/posts", params={"status": "draft",   "per_page": 1}, auth=WP_AUTH, timeout=10)
    p = requests.get(f"{WP_API}/posts", params={"status": "publish", "per_page": 1}, auth=WP_AUTH, timeout=10)
    return {"drafts": int(d.headers.get("X-WP-Total", 0)), "published": int(p.headers.get("X-WP-Total", 0))}

def wp_upload_image(image_bytes: bytes, filename: str, mime_type: str) -> dict:
    r = requests.post(
        f"{WP_API}/media",
        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                 "Content-Type": mime_type},
        data=image_bytes,
        auth=WP_AUTH,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

def wp_set_featured_image(post_id: int, media_id: int) -> dict:
    r = requests.post(f"{WP_API}/posts/{post_id}", json={"featured_media": media_id},
                      auth=WP_AUTH, timeout=10)
    r.raise_for_status()
    return r.json()

def wp_list_pages() -> list:
    r = requests.get(f"{WP_API}/pages",
        params={"per_page": 20, "_fields": "id,title,status,link"},
        auth=WP_AUTH, timeout=10)
    r.raise_for_status()
    return r.json()

# ── RSS Feed fetcher ─────────────────────────────────────────────────────────────
def fetch_feed(source: dict, max_items: int = 5) -> list:
    try:
        r    = requests.get(source["url"], headers={"User-Agent": "HermesAgent/2.0"}, timeout=15)
        r.raise_for_status()
        root  = ET.fromstring(r.content)
        items = []
        for item in root.findall(".//item")[:max_items]:
            title = item.findtext("title", "").strip()
            link  = item.findtext("link",  "").strip()
            if title and link:
                items.append({"title": title, "link": link})
        if not items:
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall("atom:entry", ns)[:max_items]:
                title   = entry.findtext("atom:title", "", ns).strip()
                link_el = entry.find("atom:link", ns)
                link    = link_el.get("href", "") if link_el is not None else ""
                if title and link:
                    items.append({"title": title, "link": link})
        return items
    except Exception as e:
        logger.error(f"Feed error {source['name']}: {e}")
        return []

def load_seen_news() -> set:
    """Load set of already-seen news article URLs."""
    if os.path.exists(NEWS_SEEN_FILE):
        try:
            with open(NEWS_SEEN_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()

def save_seen_news(seen: set):
    """Persist seen news URLs."""
    with open(NEWS_SEEN_FILE, "w") as f:
        json.dump(list(seen)[-500:], f)  # keep last 500

def fetch_article_image(url: str) -> str:
    """Try to extract the best image from an article page using og:image."""
    try:
        r = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }, timeout=15, allow_redirects=True)
        r.raise_for_status()
        html = r.text
        # og:image first (most reliable)
        og = re.search(r'property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
        if not og:
            og = re.search(r'content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html)
        if og:
            img = og.group(1).strip().replace("&amp;", "&")
            if img.startswith("http"):
                return img
        # twitter:image fallback
        tw = re.search(r'name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', html)
        if not tw:
            tw = re.search(r'content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']', html)
        if tw:
            img = tw.group(1).strip().replace("&amp;", "&")
            if img.startswith("http"):
                return img
        # Fallback to first large content image
        imgs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>', html)
        for img in imgs:
            if img.startswith("http") and any(e in img.lower() for e in [".jpg",".jpeg",".png",".webp"]):
                if not any(s in img.lower() for s in ["logo","icon","avatar","pixel","tracking","1x1","badge"]):
                    return img.replace("&amp;", "&")
    except Exception as e:
        logger.debug(f"Image fetch failed for {url}: {e}")
    return ""

def is_news_relevant(title: str, description: str) -> dict:
    """Ask Mistral if this news story is relevant for TensorEdge."""
    # Fast keyword pre-filter — skip obvious irrelevants before calling Mistral
    title_lower = title.lower()
    skip_keywords = ["crypto", "bitcoin", "nft", "stock market", "celebrity",
                     "sports", "football", "movie", "tv show", "lawsuit", "politics"]
    if any(kw in title_lower for kw in skip_keywords):
        return {"relevant": False, "score": 1, "reason": "keyword skip"}

    # Fast accept for clearly relevant topics
    accept_keywords = ["mini pc", "minipc", "nuc", "ryzen", "amd", "intel", "nvidia",
                       "gpu", "cpu", "llm", "local ai", "geforce", "radeon", "apu",
                       "benchmark", "gaming pc", "minisforum", "geekom", "aoostar",
                       "arm", "snapdragon", "apple silicon", "ai chip", "inference"]
    if any(kw in title_lower for kw in accept_keywords):
        return {"relevant": True, "score": 7, "reason": "keyword match"}

    # Ask Mistral for borderline cases
    try:
        raw = ollama_chat(
            NEWS_FILTER_PROMPT,
            f"HEADLINE: {title}\nDESCRIPTION: {description[:200]}",
            timeout=30
        )
        raw = raw.replace("```json","").replace("```","").strip()
        # Extract JSON even if wrapped in text
        json_match = re.search(r'\{[^}]+\}', raw)
        if json_match:
            return json.loads(json_match.group(0))
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"Relevance parse error: {e} — raw: {raw[:100] if raw else 'empty'}")
        # Default: include if borderline (let user decide)
        return {"relevant": True, "score": 4, "reason": "parse fallback"}

def fetch_news_items() -> list:
    """Fetch all news sources and return new unseen items."""
    seen    = load_seen_news()
    new_items = []
    for source in NEWS_SOURCES:
        try:
            items = fetch_feed(source, max_items=10)
            for item in items:
                url = item["link"]
                if url not in seen:
                    item["source"]  = source["name"]
                    item["emoji"]   = source["emoji"]
                    new_items.append(item)
        except Exception as e:
            logger.error(f"News fetch error {source['name']}: {e}")
    return new_items

def write_news_post(title: str, url: str, source: str, description: str) -> dict:
    """Use Mistral to write a short news post for WordPress."""
    system = (
        "You are a tech news writer for TensorEdge.net. "
        "Write a SHORT news post (150-250 words) based on the provided headline and source article. "
        "Rules:\n"
        "1. Start with a strong opening sentence summarizing the key news\n"
        "2. Expand with relevant technical details and context\n"
        "3. End with 1-2 sentences on why this matters for mini PC / local AI users\n"
        "4. ALWAYS include attribution: 'Source: <a href=\"{url}\" target=\"_blank\">{source}</a>'\n"
        "5. Use clean HTML only (p, strong, a tags)\n"
        "6. Do NOT copy verbatim — write in your own words\n\n"
        "Return ONLY valid JSON: {{\"title\":\"...\",...}}\n"
    )
    user = f"HEADLINE: {title}\nSOURCE: {source}\nURL: {url}\nDESCRIPTION: {description[:500]}"
    try:
        raw = ollama_chat(system, user, timeout=120)
        raw = re.sub(r"^```json\s*", "", raw.strip())
        raw = re.sub(r"\s*```$", "", raw)
        # Strip ALL control characters except \t \n \r
        raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw)
        # Replace literal newlines inside JSON string values with space
        # Find JSON string values and clean them
        def clean_json_str(m):
            return m.group(0).replace("\n", " ").replace("\r", " ").replace("\t", " ")
        raw = re.sub(r'"[^"]*"', clean_json_str, raw)
        return json.loads(raw)
    except Exception as e:
        logger.error(f"News post write failed: {e}")
        # Return a minimal fallback post so publish doesn't fail silently
        return {
            "title":   news_title[:60] if "news_title" in dir() else "Tech News",
            "content": f"<p>Source: <a href=\"{news_url}\">{news_src}</a></p>" if "news_url" in dir() else "",
            "excerpt": "",
            "tags":    [],
        }

def fetch_hackernews(max_items: int = 5) -> list:
    """Fetch top stories from Hacker News official Firebase API."""
    try:
        r    = requests.get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=10)
        ids  = r.json()[:max_items * 2]   # fetch extra in case some fail
        items = []
        for story_id in ids:
            if len(items) >= max_items:
                break
            try:
                sr    = requests.get(f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json", timeout=5)
                story = sr.json()
                title = story.get("title", "").strip()
                url   = story.get("url", f"https://news.ycombinator.com/item?id={story_id}")
                if title:
                    items.append({"title": title, "link": url})
            except Exception:
                continue
        return items
    except Exception as e:
        logger.error(f"Hacker News fetch error: {e}")
        return []

# ── Daily Digest ─────────────────────────────────────────────────────────────────
def build_digest() -> str:
    now      = datetime.now(DOHA_TZ)
    date_str = now.strftime("%A, %B %d %Y")
    time_str = now.strftime("%I:%M %p")
    parts    = [
        "Good morning Mohamed!",
        "TensorEdge Daily Digest - " + date_str,
        "Doha time: " + time_str,
        "",
    ]
    for source in DIGEST_SOURCES:
        items = fetch_feed(source, max_items=5)
        parts.append(source["emoji"] + " " + source["name"])
        for item in items:
            title = item["title"][:80] + "..." if len(item["title"]) > 80 else item["title"]
            parts.append("- [" + title + "](" + item["link"] + ")")
        if not items:
            parts.append("- Could not fetch feed")
        parts.append("")
    # Hacker News
    hn_items = fetch_hackernews(HN_TOP_COUNT)
    parts.append("⚡ Hacker News Top " + str(HN_TOP_COUNT))
    for item in hn_items:
        title = item["title"][:80] + "..." if len(item["title"]) > 80 else item["title"]
        parts.append("- [" + title + "](" + item["link"] + ")")
    if not hn_items:
        parts.append("- Could not fetch Hacker News")
    parts.append("")
    parts.append("Use /fetch with a Reddit URL to publish any story.")
    return "\n".join(parts)

async def send_daily_digest(ctx) -> None:
    logger.info("Sending daily digest...")
    try:
        await ctx.bot.send_message(chat_id=ALLOWED_USER_ID, text=build_digest(),
                                   disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Daily digest error: {e}")

# ── Trending Stories ─────────────────────────────────────────────────────────────
def get_trending() -> list:
    trending = []
    for source in DIGEST_SOURCES:
        for item in fetch_feed(source, max_items=5):
            trending.append({"source": source["name"], "emoji": source["emoji"],
                             "title": item["title"], "link": item["link"]})
    return trending

def pick_top_stories(trending: list) -> list:
    if not trending:
        return []
    system = (
        "Content curator for TensorEdge.net (mini PCs, AI hardware, local LLM, cloud). "
        "Pick 5 most relevant stories. Return ONLY a JSON array of 0-based indices like [0,3,5,7,11]"
    )
    story_list = "\n".join(str(i) + ". [" + s["source"] + "] " + s["title"] for i, s in enumerate(trending))
    try:
        raw     = ollama_chat(system, story_list, timeout=60).replace("```json","").replace("```","").strip()
        indices = json.loads(raw)
        return [trending[i] for i in indices if i < len(trending)]
    except Exception:
        return trending[:5]

def build_trending_msg(top: list) -> str:
    parts = ["Trending now - worth posting to TensorEdge:\n"]
    for i, s in enumerate(top, 1):
        title = s["title"][:75] + "..." if len(s["title"]) > 75 else s["title"]
        parts.append(str(i) + ". " + s["emoji"] + " " + s["source"])
        parts.append("   [" + title + "](" + s["link"] + ")")
        if "reddit.com" in s["link"]:
            parts.append("   Use: /fetch " + s["link"])
        parts.append("")
    parts.append("Use /fetch url to publish any of these.")
    return "\n".join(parts)

async def send_trending_suggestion(ctx) -> None:
    logger.info("Sending trending suggestions...")
    try:
        top = pick_top_stories(get_trending())
        if top:
            await ctx.bot.send_message(chat_id=ALLOWED_USER_ID, text=build_trending_msg(top),
                                       disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Trending error: {e}")

# ── Confirmation helper ───────────────────────────────────────────────────────────
def confirm_keyboard(action: str, data_json: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes, proceed", callback_data="confirm:" + action + ":" + data_json),
        InlineKeyboardButton("Cancel",        callback_data="cancel"),
    ]])

# ── Telegram command handlers ────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    await update.message.reply_text(HELP_TEXT)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    await update.message.reply_text(HELP_TEXT)

async def cmd_fetch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    if not ctx.args:
        return await update.message.reply_text("Usage: /fetch <reddit_url>")
    url = ctx.args[0]
    if "reddit.com" not in url:
        return await update.message.reply_text("Please provide a valid Reddit URL.")
    msg = await update.message.reply_text("Fetching Reddit post...")
    try:
        reddit = fetch_reddit(url)
    except Exception as e:
        return await msg.edit_text("Failed to fetch: " + str(e))
    # Show post type info
    images_list = reddit.get("images", [])
    image_count = len(images_list)
    raw_type    = reddit.get("post_type", "text")
    # Override type if images were found regardless of Reddit's post_hint
    if image_count > 1:
        post_type = "gallery"
    elif image_count == 1:
        post_type = "image"
    else:
        post_type = raw_type
    type_label  = {"gallery": f"Gallery ({image_count} images)", "image": "Single image",
                   "link": "Link post", "text": "Text post"}.get(post_type, "Text post")
    await msg.edit_text(
        "Fetched: " + reddit["title"][:60] + "\n"
        "Type: " + type_label + "\n\n"
        "Reformatting with Mistral Small (local)..."
    )
    try:
        article = reformat_for_wordpress(reddit)
    except Exception as e:
        return await msg.edit_text("Mistral reformatting failed: " + str(e))
    await msg.edit_text("Saving draft to WordPress...")
    try:
        # Build WordPress image URL map for inline replacement
        wp_image_map = {}

        post    = wp_create_draft(article["title"], article["content"], article["excerpt"], article["tags"])
        post_id = post["id"]

        # Upload images
        featured_set   = False
        uploaded_count = 0
        gallery_ids    = []
        first_media_url = ""
        images         = reddit.get("images", [])

        if images:
            await msg.edit_text(
                "Draft saved! Uploading " + str(len(images)) + " image(s) to WordPress..."
            )
            for i, img_url in enumerate(images):
                try:
                    img_bytes, mime = download_image(img_url)
                    ext      = {"image/jpeg": "jpg", "image/png": "png",
                                "image/gif": "gif", "image/webp": "webp"}.get(mime, "jpg")
                    filename = f"post-{post_id}-img{i+1}.{ext}"
                    media    = wp_upload_image(img_bytes, filename, mime)
                    media_id = media["id"]
                    media_url = media.get("source_url", "")
                    gallery_ids.append(media_id)
                    uploaded_count += 1
                    # Map original URL base -> WordPress URL for inline replacement
                    # Also map media_id -> WordPress URL for gallery lookups
                    if media_url:
                        wp_image_map[img_url.split("?")[0]] = media_url
                        wp_image_map[str(media_id)]         = media_url
                    if not featured_set:
                        wp_set_featured_image(post_id, media_id)
                        featured_set    = True
                        first_media_url = media.get("source_url", "")
                except Exception as e:
                    logger.warning(f"Image {i+1} upload failed: {e}")

            # Rebuild content with inline WordPress image URLs replacing Reddit URLs
            # Pass first image URL so it gets skipped (already injected as hero above)
            first_reddit_url = images[0].split("?")[0] if images else ""
            # Detect pure gallery posts — images in gallery_data, not in selftext
            selftext_has_images = any(
                x in (reddit.get("selftext") or "")
                for x in ["preview.redd.it", "i.redd.it"]
            )
            is_pure_gallery = (reddit.get("post_type") == "gallery" and not selftext_has_images)
            if wp_image_map:
                updated_content = markdown_to_html(
                    reddit["selftext"] or "",
                    wp_image_map=wp_image_map,
                    skip_first_image=first_reddit_url,
                )
                # For pure gallery posts, append all gallery images after the text
                if is_pure_gallery and len(gallery_ids) > 1:
                    gallery_html = "<h2>Gallery</h2>\n"
                    for mid in gallery_ids[1:]:  # skip first — already injected as hero
                        wp_url_for_id = wp_image_map.get(str(mid), "")
                        if wp_url_for_id:
                            gallery_html += (
                                '<figure class="wp-block-image size-large">' +
                                f'<img src="{wp_url_for_id}" alt="Gallery image"/>' +
                                "</figure>\n"
                            )
                    updated_content = updated_content + "\n" + gallery_html
                # Re-add source attribution
                permalink = reddit["permalink"]
                subreddit = reddit["subreddit"]
                score     = reddit["score"]
                source    = (
                    f'<p><strong>Source:</strong> <a href="{permalink}" target="_blank">' +
                    f'r/{subreddit} — {score} upvotes</a></p>\n'
                )
                # Inject hero image at top of content if we have one
                hero_html = ""
                if first_media_url:
                    # Get caption from first image link in selftext
                    first_cap_match = re.search(
                        r"(?<!!)\[([^\]]+)\]\(https?://(?:preview|i)\.redd\.it/",
                        reddit.get("selftext", "") or ""
                    )
                    first_cap = first_cap_match.group(1).strip() if first_cap_match else ""
                    hero_html = (
                        f'<figure class="wp-block-image size-large">' +
                        f'<img src="{first_media_url}" alt="{first_cap}"/>' +
                        (f'<figcaption>{first_cap}</figcaption>' if first_cap else "") +
                        "</figure>\n"
                    )
                wp_update(post_id, content=source + hero_html + updated_content)

        edit_link  = "https://tensoredge.net/wp-admin/post.php?post=" + str(post_id) + "&action=edit"
        img_status = (f"\nImages: {uploaded_count}/{len(images)} uploaded" +
                      (" (featured set ✅)" if featured_set else "")) if images else "\nImages: None (text post)"

        # ── Mistral HTML cleanup pass ─────────────────────────────────────────
        await msg.edit_text("Draft saved! Running Mistral formatting check...")
        try:
            current_post = wp_get_post(post_id)
            content_obj  = current_post.get("content", {})
            raw_html     = content_obj.get("raw") or content_obj.get("rendered", "")
            fixed_html, cleanup_report = mistral_html_cleanup(raw_html, article["title"])
            if fixed_html != raw_html:
                wp_update(post_id, content=fixed_html)
                logger.info(f"Mistral cleanup applied to post {post_id}")
        except Exception as e:
            cleanup_report = f"Formatting check skipped: {e}"
            logger.warning(f"Cleanup error: {e}")

        # ── Auto SEO generation ───────────────────────────────────────────────
        seo_report = ""
        try:
            await msg.edit_text("Almost done! Generating SEO metadata with Mistral...")
            post_content = wp_get_post(post_id)
            content_text = post_content.get("content", {}).get("rendered", "")
            seo = generate_seo_meta(article["title"], content_text, post_type)
            if seo:
                set_yoast_meta(
                    post_id,
                    seo.get("seo_title", ""),
                    seo.get("meta_desc", ""),
                    seo.get("focus_keyword", ""),
                    seo.get("category", ""),
                )
                seo_report = (
                    "\nSEO:\n"
                    "• Title: " + seo.get("seo_title", "")[:55] + "\n"
                    "• Keyword: " + seo.get("focus_keyword", "") + "\n"
                    "• Category: " + seo.get("category", "")
                )
        except Exception as e:
            seo_report = "\nSEO: skipped (" + str(e)[:40] + ")"
            logger.warning(f"SEO error: {e}")

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Approve & Publish", callback_data="approve:" + str(post_id)),
            InlineKeyboardButton("Delete Draft",      callback_data="delete:"  + str(post_id)),
        ]])
        await msg.edit_text(
            "Draft ready for review!\n\n"
            "Title: "   + article["title"] + "\n"
            "Type: "    + type_label + "\n"
            "Tags: "    + ", ".join(article["tags"]) + "\n"
            "Post ID: " + str(post_id) + "\n"
            + img_status + "\n\n"
            "Formatting check:\n" + cleanup_report
            + seo_report + "\n\n"
            "Preview: " + edit_link + "\n\n"
            "Tap Approve to publish or Delete to discard.",
            reply_markup=keyboard,
        )
    except Exception as e:
        await msg.edit_text("WordPress error: " + str(e))

async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    if not ctx.args:
        return await update.message.reply_text("Usage: /approve <post_id>")
    try:
        post = wp_publish(int(ctx.args[0]))
        await update.message.reply_text("Published! " + post.get("link", "https://tensoredge.net"))
    except Exception as e:
        await update.message.reply_text("Publish failed: " + str(e))

async def cmd_drafts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    try:
        posts = wp_list("draft")
        if not posts:
            return await update.message.reply_text("No drafts found.")
        lines = ["Current Drafts:\n"]
        for p in posts:
            lines.append(str(p["id"]) + " - " + p["title"]["rendered"][:50])
        lines.append("\nUse /approve <id> to publish.")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text("Error: " + str(e))

async def cmd_published(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    try:
        posts = wp_list("publish")
        if not posts:
            return await update.message.reply_text("No published posts yet.")
        lines = ["Published Posts:\n"]
        for p in posts:
            lines.append(str(p["id"]) + " - " + p["title"]["rendered"][:45] + "\n   " + p.get("link", ""))
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text("Error: " + str(e))

async def cmd_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    if not ctx.args or len(ctx.args) < 2:
        return await update.message.reply_text("Usage: /edit <post_id> <instruction>")
    try:
        post_id     = int(ctx.args[0])
        instruction = " ".join(ctx.args[1:])
        msg         = await update.message.reply_text("Applying edits with Mistral Small...")
        post        = wp_get_post(post_id)
        wp_update(post_id, content=edit_post_content(post["content"]["rendered"], instruction))
        await msg.edit_text("Post " + str(post_id) + " updated. Applied: " + instruction)
    except Exception as e:
        await update.message.reply_text("Edit failed: " + str(e))

async def cmd_replace(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Direct text replacement in a post — no LLM needed."""
    if not is_authorized(update): return await deny(update)
    if not ctx.args or len(ctx.args) < 3:
        return await update.message.reply_text(
            "Usage: /replace <post_id> <old_text> | <new_text>\n\n"
            "Example: /replace 1656 I will leave a link to the Github repo | "
            "Find it on <a href=\"https://github.com/AlexsJones/llmfit\">GitHub</a>"
        )
    post_id = int(ctx.args[0])
    # Join remaining args and split on | separator
    rest = " ".join(ctx.args[1:])
    if "|" not in rest:
        return await update.message.reply_text(
            "Please separate old and new text with | symbol\n"
            "Example: /replace 1656 old text here | new text here"
        )
    parts   = rest.split("|", 1)
    old_txt = parts[0].strip()
    new_txt = parts[1].strip()
    try:
        post        = wp_get_post(post_id)
        content_obj = post.get("content", {})
        html        = content_obj.get("raw") or content_obj.get("rendered", "")
        if old_txt not in html:
            return await update.message.reply_text(
                "Text not found in post:\n\"" + old_txt[:100] + "\""
            )
        updated = html.replace(old_txt, new_txt, 1)
        wp_update(post_id, content=updated)
        await update.message.reply_text(
            "✅ Replaced in post " + str(post_id) + ":\n\n"
            "Old: \"" + old_txt[:80] + "\"\n"
            "New: \"" + new_txt[:80] + "\""
        )
    except Exception as e:
        await update.message.reply_text("Replace failed: " + str(e))

async def cmd_seo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    if not ctx.args:
        return await update.message.reply_text("Usage: /seo <post_id>")
    try:
        post_id = int(ctx.args[0])
        msg     = await update.message.reply_text("Generating SEO metadata with Mistral...")
        post    = wp_get_post(post_id)
        title   = post["title"]["rendered"]
        content_obj = post.get("content", {})
        html    = content_obj.get("rendered", "")
        post_type = "review" if any(w in title.lower() for w in ["review","minisforum","geekom","aoostar"]) else "article"

        # Generate SEO
        seo = generate_seo_meta(title, html, post_type)
        if not seo:
            return await msg.edit_text("Mistral SEO generation failed — try again.")

        # Apply to WordPress via Yoast
        set_yoast_meta(
            post_id,
            seo.get("seo_title", ""),
            seo.get("meta_desc", ""),
            seo.get("focus_keyword", ""),
            seo.get("category", ""),
        )

        # Also update excerpt
        clean = re.sub(r"<[^>]+>", "", html)
        clean = re.sub(r"\s+", " ", clean).strip()
        excerpt = clean[:155] + "..." if len(clean) > 155 else clean
        wp_update(post_id, excerpt=excerpt)

        await msg.edit_text(
            "SEO Updated for Post " + str(post_id) + "\n\n"
            "Title: " + seo.get("seo_title", "") + "\n"
            "Meta: "  + seo.get("meta_desc", "")[:80] + "...\n"
            "Keyword: " + seo.get("focus_keyword", "") + "\n"
            "Category: " + seo.get("category", "") + "\n\n"
            "All set in Yoast SEO ✅"
        )
    except Exception as e:
        await update.message.reply_text("SEO failed: " + str(e))

async def cmd_unpublish(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    if not ctx.args:
        return await update.message.reply_text("Usage: /unpublish <post_id>")
    try:
        wp_unpublish(int(ctx.args[0]))
        await update.message.reply_text("Post " + ctx.args[0] + " moved to draft.")
    except Exception as e:
        await update.message.reply_text("Failed: " + str(e))

async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    if not ctx.args:
        return await update.message.reply_text("Usage: /delete <post_id>")
    post_id = ctx.args[0]
    await update.message.reply_text(
        "Are you sure you want to permanently delete post " + post_id + "?",
        reply_markup=confirm_keyboard("delete_post", post_id),
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    try:
        stats = wp_stats()
        await update.message.reply_text(
            "TensorEdge.net Status\n\n"
            "Published: " + str(stats["published"]) + "\n"
            "Drafts: "    + str(stats["drafts"])    + "\n\n"
            "https://tensoredge.net"
        )
    except Exception as e:
        await update.message.reply_text("Could not reach WordPress: " + str(e))

async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    msg = await update.message.reply_text("Running site health checks...")
    try:
        report = get_site_health()
        await msg.edit_text(report)
    except Exception as e:
        await msg.edit_text("Health check failed: " + str(e))

async def cmd_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    if not ctx.args:
        return await update.message.reply_text(
            "Usage: /image <post_id>\n\nThen send the image as a reply to this message."
        )
    post_id = int(ctx.args[0])
    ctx.user_data["awaiting_image_for"] = post_id
    await update.message.reply_text(
        "Ready to upload image for post " + str(post_id) + ".\n\nSend the image now."
    )

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    post_id = ctx.user_data.get("awaiting_image_for")
    if not post_id:
        return await update.message.reply_text("Use /image <post_id> first, then send the image.")
    msg = await update.message.reply_text("Uploading image to WordPress...")
    try:
        photo   = update.message.photo[-1]
        file    = await ctx.bot.get_file(photo.file_id)
        img_bytes = await file.download_as_bytearray()
        media   = wp_upload_image(bytes(img_bytes), f"post-{post_id}.jpg", "image/jpeg")
        media_id = media["id"]
        wp_set_featured_image(post_id, media_id)
        ctx.user_data.pop("awaiting_image_for", None)
        await msg.edit_text(
            "Image uploaded and set as featured image for post " + str(post_id) + ".\n"
            "Media ID: " + str(media_id)
        )
    except Exception as e:
        await msg.edit_text("Image upload failed: " + str(e))

async def cmd_linkedin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    if not ctx.args:
        return await update.message.reply_text("Usage: /linkedin <post_id>")
    try:
        post_id      = int(ctx.args[0])
        msg          = await update.message.reply_text("Formatting for LinkedIn with Mistral Small...")
        post         = wp_get_post(post_id)
        linkedin_post = format_for_linkedin(
            post["title"]["rendered"],
            post["content"]["rendered"],
            post.get("link", "https://tensoredge.net")
        )
        await msg.edit_text("LinkedIn Post Ready\n\n" + linkedin_post + "\n\nCopy and paste into LinkedIn.")
    except Exception as e:
        await update.message.reply_text("LinkedIn format failed: " + str(e))

async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    status = {s["name"]: "..." for s in DIGEST_SOURCES}

    def status_text():
        return "Building digest...\n\n" + "\n".join(icon + " " + name for name, icon in status.items())

    msg = await update.message.reply_text(status_text())
    try:
        for source in DIGEST_SOURCES:
            status[source["name"]] = "loading"
            await msg.edit_text(status_text())
            items = fetch_feed(source, max_items=5)
            status[source["name"]] = "done (" + str(len(items)) + " stories)"
        await msg.edit_text(status_text() + "\n\nCompiling...")
        await msg.edit_text(build_digest(), disable_web_page_preview=True)
    except Exception as e:
        await msg.edit_text("Digest failed: " + str(e))

async def cmd_trending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    msg = await update.message.reply_text("Finding trending stories...")
    try:
        top = pick_top_stories(get_trending())
        if not top:
            return await msg.edit_text("Could not fetch trending stories right now.")
        await msg.edit_text(build_trending_msg(top), disable_web_page_preview=True)
    except Exception as e:
        await msg.edit_text("Error: " + str(e))

# ── Plugin management ────────────────────────────────────────────────────────────
async def cmd_plugin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    if not ctx.args:
        return await update.message.reply_text("Usage: /plugin list|install|activate|deactivate <name>")

    action = ctx.args[0].lower()

    if action == "list":
        msg = await update.message.reply_text("Fetching plugin list...")
        ok, out = wpcli("plugin list --fields=name,status,version --format=table")
        await msg.edit_text("Plugins:\n\n" + out[:3000] if ok else "Failed: " + out)

    elif action in ("install", "activate", "deactivate"):
        if len(ctx.args) < 2:
            return await update.message.reply_text("Usage: /plugin " + action + " <plugin-name>")
        name = ctx.args[1]
        await update.message.reply_text(
            "Confirm: " + action + " plugin '" + name + "' on tensoredge.net?",
            reply_markup=confirm_keyboard("plugin_" + action, name),
        )
    else:
        await update.message.reply_text("Unknown action. Use: list, install, activate, deactivate")

# ── Theme management ─────────────────────────────────────────────────────────────
async def cmd_theme(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    if not ctx.args:
        return await update.message.reply_text("Usage: /theme list|activate <name>")

    action = ctx.args[0].lower()

    if action == "list":
        msg = await update.message.reply_text("Fetching themes...")
        ok, out = wpcli("theme list --fields=name,status,version --format=table")
        await msg.edit_text("Themes:\n\n" + out[:3000] if ok else "Failed: " + out)

    elif action == "activate":
        if len(ctx.args) < 2:
            return await update.message.reply_text("Usage: /theme activate <theme-name>")
        name = ctx.args[1]
        await update.message.reply_text(
            "Confirm: activate theme '" + name + "' on tensoredge.net? This will change the live site.",
            reply_markup=confirm_keyboard("theme_activate", name),
        )
    else:
        await update.message.reply_text("Unknown action. Use: list or activate")

# ── Pages management ─────────────────────────────────────────────────────────────
async def cmd_pages(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    try:
        pages = wp_list_pages()
        if not pages:
            return await update.message.reply_text("No pages found.")
        lines = ["Pages:\n"]
        for p in pages:
            lines.append(str(p["id"]) + " - " + p["title"]["rendered"][:45] + " [" + p["status"] + "]")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text("Error: " + str(e))

# ── Raw WP-CLI ───────────────────────────────────────────────────────────────────
async def cmd_wpcli(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    if not ctx.args:
        return await update.message.reply_text("Usage: /wpcli <command>\nExample: /wpcli post list --status=publish")
    command = " ".join(ctx.args)

    # Always confirm raw WP-CLI
    await update.message.reply_text(
        "Confirm running WP-CLI command on tensoredge.net:\n\nwp " + command,
        reply_markup=confirm_keyboard("wpcli", command),
    )

# ── Self-upgrade from GitHub ──────────────────────────────────────────────────────
async def cmd_install(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    if not ctx.args:
        return await update.message.reply_text(
            "Usage: /install <github_url>\n"
            "Example: /install https://github.com/user/repo"
        )
    github_url = ctx.args[0]
    if "github.com" not in github_url:
        return await update.message.reply_text("Please provide a valid GitHub URL.")

    await update.message.reply_text(
        "Confirm: install Python module from\n" + github_url + "\n\nThis will modify Hermes and restart it.",
        reply_markup=confirm_keyboard("install_github", github_url),
    )

async def do_install_github(url: str, bot, chat_id: int):
    try:
        await bot.send_message(chat_id, "Installing from GitHub: " + url)
        # Clone to temp directory
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                ["git", "clone", "--depth=1", url, tmpdir + "/repo"],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                return await bot.send_message(chat_id, "Git clone failed:\n" + result.stderr[:500])

            repo_dir = tmpdir + "/repo"

            # Look for requirements.txt and install
            req_file = repo_dir + "/requirements.txt"
            if os.path.exists(req_file):
                pip_result = subprocess.run(
                    ["pip", "install", "-r", req_file, "--break-system-packages"],
                    capture_output=True, text=True, timeout=120
                )
                if pip_result.returncode != 0:
                    await bot.send_message(chat_id, "pip install warning:\n" + pip_result.stderr[:300])

            # Copy Python files to /app/modules/
            os.makedirs("/app/modules", exist_ok=True)
            py_files = [f for f in os.listdir(repo_dir) if f.endswith(".py") and f != "setup.py"]
            for py_file in py_files:
                import shutil
                shutil.copy2(repo_dir + "/" + py_file, "/app/modules/" + py_file)

            if py_files:
                await bot.send_message(
                    chat_id,
                    "Installed successfully!\n\nFiles added to /app/modules/:\n" +
                    "\n".join(py_files) + "\n\nModules are available for use."
                )
            else:
                await bot.send_message(chat_id, "No Python files found in the repository root.")
    except Exception as e:
        await bot.send_message(chat_id, "Install failed: " + str(e))

# ── Chat mode ────────────────────────────────────────────────────────────────────
async def cmd_cron(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Dynamic cron job manager — add, remove, list scheduled jobs."""
    if not is_authorized(update): return await deny(update)
    args = ctx.args

    if not args or args[0].lower() == "list":
        jobs = load_cron_jobs()
        if not jobs:
            return await update.message.reply_text(
                "No scheduled jobs yet.\n\n"
                "Add one with:\n/cron add <action> <time>\n\n"
                "Available actions:\n" + "\n".join(f"- {k}: {v}" for k, v in CRON_ACTIONS.items())
            )
        lines = ["Scheduled Jobs (Doha time):\n"]
        for job_id, job in jobs.items():
            lines.append(
                f"{job_id}\n"
                f"  Action: {job['action']}\n"
                f"  Time: {job['hour']:02d}:{job['minute']:02d}\n"
            )
        lines.append("Use /cron remove <job_id> to delete a job.")
        return await update.message.reply_text("\n".join(lines))

    elif args[0].lower() == "add":
        if len(args) < 3:
            return await update.message.reply_text(
                "Usage: /cron add <action> <time>\n\n"
                "Example: /cron add digest 08:00\n"
                "Example: /cron add trending 10:30\n\n"
                "Available actions: " + ", ".join(CRON_ACTIONS.keys())
            )
        action   = args[1].lower()
        time_str = args[2]

        if action not in CRON_ACTIONS:
            return await update.message.reply_text(
                "Unknown action: " + action + "\n\nAvailable: " + ", ".join(CRON_ACTIONS.keys())
            )
        try:
            hour, minute = parse_time(time_str)
        except ValueError as e:
            return await update.message.reply_text(str(e))

        # Generate unique job ID
        job_id = action + "_" + f"{hour:02d}{minute:02d}"

        # Save and register
        jobs           = load_cron_jobs()
        jobs[job_id]   = {"action": action, "hour": hour, "minute": minute}
        save_cron_jobs(jobs)
        register_cron_job(job_id, action, hour, minute)

        await update.message.reply_text(
            "Cron job added!\n\n"
            "ID: " + job_id + "\n"
            "Action: " + action + " (" + CRON_ACTIONS[action] + ")\n"
            "Time: " + f"{hour:02d}:{minute:02d}" + " Doha\n\n"
            "This job will survive restarts. Use /cron list to see all jobs."
        )

    elif args[0].lower() == "remove":
        if len(args) < 2:
            return await update.message.reply_text("Usage: /cron remove <job_id>\nUse /cron list to see job IDs.")
        job_id = args[1]
        jobs   = load_cron_jobs()
        if job_id not in jobs:
            return await update.message.reply_text("Job not found: " + job_id + "\nUse /cron list to see all jobs.")
        del jobs[job_id]
        save_cron_jobs(jobs)
        try:
            _scheduler.remove_job(job_id)
        except Exception:
            pass
        await update.message.reply_text("Job removed: " + job_id)

    elif args[0].lower() == "run":
        if len(args) < 2:
            return await update.message.reply_text("Usage: /cron run <action>\nAvailable: " + ", ".join(CRON_ACTIONS.keys()))
        action = args[1].lower()
        if action not in CRON_ACTIONS:
            return await update.message.reply_text("Unknown action: " + action)
        msg = await update.message.reply_text("Running " + action + " now...")
        await run_cron_action(action)
        await msg.edit_text("Done! " + action + " executed.")

    elif args[0].lower() == "ask":
        # Natural language cron creation via Mistral
        if len(args) < 2:
            return await update.message.reply_text("Usage: /cron ask <natural language request>\nExample: /cron ask send me the digest at 7AM every day")
        request = " ".join(args[1:])
        msg = await update.message.reply_text("Understanding your request with Mistral...")
        system = (
            "You are a cron job parser for Hermes agent. "
            "Given a natural language scheduling request, extract the action and time.\n\n"
            "Available actions: " + ", ".join(CRON_ACTIONS.keys()) + "\n\n"
            "Return ONLY valid JSON like: {\"action\": \"digest\", \"time\": \"08:00\", \"explanation\": \"Daily digest at 8AM\"}\n"
            "If you cannot parse it, return: {\"error\": \"reason\"}"
        )
        try:
            raw    = ollama_chat(system, request, timeout=30)
            raw    = raw.replace("```json","").replace("```","").strip()
            parsed = json.loads(raw)
            if "error" in parsed:
                return await msg.edit_text("Could not understand: " + parsed["error"] + "\n\nTry: /cron add digest 08:00")
            action   = parsed["action"]
            time_str = parsed["time"]
            hour, minute = parse_time(time_str)
            job_id   = action + "_" + f"{hour:02d}{minute:02d}"
            jobs     = load_cron_jobs()
            jobs[job_id] = {"action": action, "hour": hour, "minute": minute}
            save_cron_jobs(jobs)
            register_cron_job(job_id, action, hour, minute)
            await msg.edit_text(
                "Cron job created!\n\n"
                "I understood: " + parsed.get("explanation", request) + "\n\n"
                "ID: " + job_id + "\n"
                "Action: " + action + "\n"
                "Time: " + f"{hour:02d}:{minute:02d}" + " Doha"
            )
        except Exception as e:
            await msg.edit_text("Failed to parse request: " + str(e) + "\n\nTry: /cron add digest 08:00")

    else:
        await update.message.reply_text(
            "Usage:\n"
            "/cron list - Show all scheduled jobs\n"
            "/cron add <action> <time> - Add a new job\n"
            "/cron remove <job_id> - Remove a job\n"
            "/cron run <action> - Run an action right now\n"
            "/cron ask <natural language> - Create job using plain English\n\n"
            "Available actions: " + ", ".join(CRON_ACTIONS.keys())
        )

async def process_news_items(bot, chat_id: int, items: list):
    """Filter news items with Mistral and send relevant ones to Telegram."""
    seen       = load_seen_news()
    sent_count = 0
    for item in items:
        url   = item["link"]
        title = item["title"]
        desc  = item.get("description", "")[:300]
        if url in seen:
            continue
        # Ask Mistral if relevant
        result = is_news_relevant(title, desc)
        seen.add(url)
        score = result.get("score", 0)
        relevant = result.get("relevant", False)
        logger.info(f"News score {score}/10 [{relevant}]: {title[:60]}")
        if not relevant or score < 4:
            continue
        # Fetch article image
        img_url = fetch_article_image(url)
        score   = result.get("score", 0)
        reason  = result.get("reason", "")
        # Build Telegram message
        caption = (
            f"{item['emoji']} *{item['source']}* — Score {score}/10\n\n"
            f"*{title}*\n\n"
            f"{reason}\n\n"
            f"[Read original]({url})"
        )
        # Store news data in bot_data keyed by short hash to avoid 64-byte limit
        import hashlib
        news_key = "news_" + hashlib.md5(url.encode()).hexdigest()[:8]
        # We'll pass news_key in callback_data (safe length)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📝 Publish to site", callback_data=f"news_pub:{news_key}"),
            InlineKeyboardButton("⏭ Skip",             callback_data="news_skip"),
        ]])
        # Store full data for callback lookup
        _news_cache[news_key] = {
            "url":    url,
            "source": item["source"],
            "title":  title,
            "desc":   desc,
        }
        try:
            if img_url:
                try:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=img_url,
                        caption=caption,
                        parse_mode="Markdown",
                        reply_markup=keyboard,
                    )
                except Exception:
                    # Image URL invalid — fall back to text message with preview
                    await bot.send_message(
                        chat_id=chat_id,
                        text=caption,
                        parse_mode="Markdown",
                        reply_markup=keyboard,
                        disable_web_page_preview=False,
                    )
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text=caption,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                    disable_web_page_preview=False,
                )
            sent_count += 1
            if sent_count >= 5:  # max 5 stories per check
                break
        except Exception as e:
            logger.error(f"News send error: {e}")
    save_seen_news(seen)
    return sent_count

async def run_news_monitor() -> None:
    """Scheduled news monitor — runs every 6 hours."""
    logger.info("News monitor running...")
    try:
        from telegram import Bot
        bot   = Bot(token=BOT_TOKEN)
        items = fetch_news_items()
        if not items:
            logger.info("News monitor: no new items")
            return
        count = await process_news_items(bot, ALLOWED_USER_ID, items)
        if count == 0:
            logger.info("News monitor: no relevant stories found")
        else:
            logger.info(f"News monitor: sent {count} stories")
    except Exception as e:
        logger.error(f"News monitor error: {e}")

async def cmd_newswatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Manually trigger the news monitor."""
    if not is_authorized(update): return await deny(update)
    msg = await update.message.reply_text("Checking news sources...")
    try:
        items = fetch_news_items()
        if not items:
            return await msg.edit_text("No new stories found.")
        await msg.edit_text(f"Found {len(items)} new items. Filtering with Mistral...")
        count = await process_news_items(ctx.bot, update.effective_user.id, items)
        if count == 0:
            await msg.edit_text("No relevant stories found this time.")
        else:
            await msg.edit_text(f"Sent {count} relevant stories for your review.")
    except Exception as e:
        await msg.edit_text(f"News check failed: {str(e)}")


async def cmd_publish(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Fetch a URL, generate a post with Mistral, and send for approval."""
    if not is_authorized(update): return await deny(update)
    if not ctx.args:
        return await update.message.reply_text(
            "Usage: /publish <url>\nExample: /publish https://techcrunch.com/2026/05/05/some-article"
        )
    url = ctx.args[0].strip()
    if not url.startswith("http"):
        return await update.message.reply_text("Please provide a full URL starting with https://")

    msg = await update.message.reply_text(f"⏳ Fetching article from {url}...")
    try:
        import urllib.request as _ur
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "identity",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        req = _ur.Request(url, headers=headers)
        with _ur.urlopen(req, timeout=20) as resp:
            raw_html = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return await msg.edit_text(f"❌ Failed to fetch URL: {e}\n\nTip: Some sites block bots. Try a different article URL.")

    import re as _re, json as _json
    # Extract plain text
    text = _re.sub(r"<style[^>]*>.*?</style>", " ", raw_html, flags=_re.DOTALL)
    text = _re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=_re.DOTALL)
    text = _re.sub(r"<[^>]+>", " ", text)
    text = _re.sub(r"\s+", " ", text).strip()

    # Grab first suitable image
    img_match = _re.search(r'<img[^>]+src=["\']([^"\']+(jpg|jpeg|png|webp))["\']', raw_html, _re.IGNORECASE)
    image_url = img_match.group(1) if img_match else None

    if not image_url:
        ctx.user_data["pending_publish_url"] = url
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚠️ Publish anyway (no image)", callback_data="noimg_yes"),
            InlineKeyboardButton("❌ Cancel", callback_data="noimg_cancel"),
        ]])
        await msg.edit_text(
            "⚠️ No image found in this article. How would you like to proceed?",
            reply_markup=kb
        )
        return

    article_text = text[:8000]
    await msg.edit_text("🤖 Generating post with Mistral...")

    gen_system = (
        "You are writing for TensorEdge.net, a tech blog about mini PCs, local AI, and hardware. "
        "Write a blog post based on the article provided. "
        "Reply ONLY with valid JSON: {title, content, excerpt, tags} — no markdown, no backticks."
    )
    if len(article_text) < 1500:
        content_instruction = "150-250 words, concise news post in HTML <p> tags, link back to the original source, do NOT copy verbatim"
    else:
        content_instruction = "400-600 words minimum in HTML <p> tags, cover all key points from the article, add your own insight and analysis, include relevant technical details, link back to the original source, do NOT copy verbatim"
    gen_user = (
        "Write a blog post with:\n"
        "- title: SEO-optimised, under 70 chars\n"
        f"- content: {content_instruction}\n"
        "- excerpt: one sentence summary\n"
        "- tags: 3-5 relevant tags as a JSON array\n\n"
        f"Source URL: {url}\n\nArticle content:\n{article_text}"
    )
    try:
        gen_resp = ollama_chat(gen_system, gen_user, timeout=180)
        raw = gen_resp.strip()
        raw = _re.sub(r"^```json|^```|```$", "", raw, flags=_re.MULTILINE).strip()
        data = _json.loads(raw)
    except Exception as e:
        return await msg.edit_text(f"❌ Generation failed: {e}")

    title   = data.get("title", "Untitled")
    body    = data.get("content", "")
    excerpt = data.get("excerpt", "")
    tags    = data.get("tags", [])

    await msg.edit_text("📝 Creating draft on WordPress...")
    try:
        draft = wp_create_draft(title, body, excerpt=excerpt, tags=tags)
        post_id = draft["id"] if isinstance(draft, dict) else int(draft)
        # Assign to News category (ID 42)
        requests.post(f"{WP_API}/posts/{post_id}", json={"categories": [42]}, auth=WP_AUTH, timeout=10)
    except Exception as e:
        return await msg.edit_text(f"❌ WordPress draft failed: {e}")

    # Try to set featured image
    if image_url:
        try:
            img_r = requests.get(image_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            img_r.raise_for_status()
            ext = image_url.split("?")[0].split(".")[-1].lower()
            if ext not in ["jpg","jpeg","png","webp"]: ext = "jpg"
            mime = {"jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png","webp":"image/webp"}.get(ext,"image/jpeg")
            media_r = requests.post(f"{WP_API}/media",
                headers={"Content-Disposition": f'attachment; filename="post-{post_id}-hero.{ext}"', "Content-Type": mime},
                data=img_r.content, auth=WP_AUTH, timeout=30)
            if media_r.ok:
                media_id = media_r.json()["id"]
                requests.post(f"{WP_API}/posts/{post_id}", json={"featured_media": media_id}, auth=WP_AUTH, timeout=10)
        except Exception:
            pass

    preview = excerpt or _re.sub(r"<[^>]+>", "", body)[:200]
    preview = preview[:300]
    title_short = title[:100]
    url_short = url[:100]
    caption = (
        f"📰 New Post Ready\n\n"
        f"{title_short}\n\n"
        f"{preview}\n\n"
        f"🔗 {url_short}\n"
        f"Draft ID: {post_id}"
    )
    caption = caption[:1020]  # Telegram caption limit is 1024
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Publish", callback_data=f"approve:{post_id}"),
        InlineKeyboardButton("🗑 Delete",  callback_data=f"delete:{post_id}"),
    ]])
    await msg.delete()
    await update.message.reply_text(caption, reply_markup=kb)

async def cmd_github(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Sync hermes_agent.py to the tensoredge-publisher GitHub repo via host shell commands."""
    if not is_authorized(update): return await deny(update)
    msg = await update.message.reply_text("⏳ Syncing to GitHub...")

    repo = "/home/mohelal/tensoredge-publisher"
    commit_msg = f"Update: auto-sync {datetime.now(DOHA_TZ).strftime('%Y-%m-%d %H:%M')}"
    cmds = [
        (["cp", "/app/hermes_agent.py", f"{repo}/hermes_agent.py"], None),
        (["git", "add", "hermes_agent.py"],                         repo),
        (["git", "commit", "-m", commit_msg],                       repo),
        (["git", "push"],                                            repo),
    ]

    try:
        for args, cwd in cmds:
            result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0 and "nothing to commit" not in result.stdout + result.stderr:
                raise Exception(result.stderr or result.stdout)
    except subprocess.TimeoutExpired:
        return await msg.edit_text("❌ Sync failed: git command timed out")
    except Exception as e:
        return await msg.edit_text(f"❌ Sync failed: {str(e).strip()[:300]}")

    await msg.edit_text(f"✅ GitHub synced successfully\n\n`{commit_msg}`", parse_mode="Markdown")


async def cmd_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return await deny(update)
    user_id = update.effective_user.id
    if user_id in chat_mode_users:
        chat_mode_users.discard(user_id)
        await update.message.reply_text("Chat mode OFF. Use /help to see commands.")
    else:
        chat_mode_users.add(user_id)
        await update.message.reply_text(
            "Chat mode ON. Powered by Mistral Small 22B on your X1 Pro.\n\n"
            "Send /chat again to switch back to command mode."
        )

# ── Callback handler ─────────────────────────────────────────────────────────────

def wp_title_exists(title: str) -> bool:
    """Return True if a post with a very similar title already exists in WordPress."""
    import difflib
    try:
        r = requests.get(f"{WP_API}/posts",
            params={"search": title[:50], "per_page": 10, "status": "publish,draft"},
            auth=WP_AUTH, timeout=10)
        if not r.ok:
            return False
        posts = r.json()
        title_lower = title.lower().strip()
        for post in posts:
            existing = post.get("title", {}).get("rendered", "").lower().strip()
            ratio = difflib.SequenceMatcher(None, title_lower, existing).ratio()
            if ratio > 0.95:
                return True
        return False
    except Exception:
        return False


async def cmd_publish_url(url: str, message, ctx: ContextTypes.DEFAULT_TYPE):
    """Fetch url, generate a post with Mistral, and create a WP draft. No image (user chose to proceed without one)."""
    import re as _re, json as _json, urllib.request as _ur

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "identity",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        req = _ur.Request(url, headers=headers)
        with _ur.urlopen(req, timeout=20) as resp:
            raw_html = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return await message.edit_text(f"❌ Failed to fetch URL: {e}\n\nTip: Some sites block bots. Try a different article URL.")

    text = _re.sub(r"<style[^>]*>.*?</style>", " ", raw_html, flags=_re.DOTALL)
    text = _re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=_re.DOTALL)
    text = _re.sub(r"<[^>]+>", " ", text)
    text = _re.sub(r"\s+", " ", text).strip()

    image_url = None  # no-image path — user explicitly chose to proceed without an image
    article_text = text[:8000]

    await message.edit_text("🤖 Generating post with Mistral...")

    gen_system = (
        "You are writing for TensorEdge.net, a tech blog about mini PCs, local AI, and hardware. "
        "Write a blog post based on the article provided. "
        "Reply ONLY with valid JSON: {title, content, excerpt, tags} — no markdown, no backticks."
    )
    if len(article_text) < 1500:
        content_instruction = "150-250 words, concise news post in HTML <p> tags, link back to the original source, do NOT copy verbatim"
    else:
        content_instruction = "400-600 words minimum in HTML <p> tags, cover all key points from the article, add your own insight and analysis, include relevant technical details, link back to the original source, do NOT copy verbatim"
    gen_user = (
        "Write a blog post with:\n"
        "- title: SEO-optimised, under 70 chars\n"
        f"- content: {content_instruction}\n"
        "- excerpt: one sentence summary\n"
        "- tags: 3-5 relevant tags as a JSON array\n\n"
        f"Source URL: {url}\n\nArticle content:\n{article_text}"
    )
    try:
        gen_resp = ollama_chat(gen_system, gen_user, timeout=180)
        raw = gen_resp.strip()
        raw = _re.sub(r"^```json|^```|```$", "", raw, flags=_re.MULTILINE).strip()
        data = _json.loads(raw)
    except Exception as e:
        return await message.edit_text(f"❌ Generation failed: {e}")

    title   = data.get("title", "Untitled")
    body    = data.get("content", "")
    excerpt = data.get("excerpt", "")
    tags    = data.get("tags", [])

    await message.edit_text("📝 Creating draft on WordPress...")
    try:
        draft = wp_create_draft(title, body, excerpt=excerpt, tags=tags)
        post_id = draft["id"] if isinstance(draft, dict) else int(draft)
        requests.post(f"{WP_API}/posts/{post_id}", json={"categories": [42]}, auth=WP_AUTH, timeout=10)
    except Exception as e:
        return await message.edit_text(f"❌ WordPress draft failed: {e}")

    preview = excerpt or _re.sub(r"<[^>]+>", "", body)[:200]
    preview = preview[:300]
    title_short = title[:100]
    url_short = url[:100]
    caption = (
        f"📰 New Post Ready (no image)\n\n"
        f"{title_short}\n\n"
        f"{preview}\n\n"
        f"🔗 {url_short}\n"
        f"Draft ID: {post_id}"
    )
    caption = caption[:1020]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Publish", callback_data=f"approve:{post_id}"),
        InlineKeyboardButton("🗑 Delete",  callback_data=f"delete:{post_id}"),
    ]])
    await message.reply_text(caption, reply_markup=kb)


async def button_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass  # Ignore stale/expired callback queries
    if query.from_user.id != ALLOWED_USER_ID:
        return await query.edit_message_text("Unauthorized.")

    data = query.data

    if data == "cancel":
        return await query.edit_message_text("Cancelled.")

    if data.startswith("approve:"):
        try:
            post = wp_publish(int(data.split(":")[1]))
            link = post.get("link", "https://tensoredge.net")
            try:
                await query.edit_message_caption("✅ Published! " + link)
            except Exception:
                await query.edit_message_text("✅ Published! " + link)
        except Exception as e:
            try:
                await query.edit_message_caption("❌ Publish failed: " + str(e))
            except Exception:
                await query.edit_message_text("❌ Publish failed: " + str(e))

    elif data.startswith("delete:"):
        try:
            wp_delete(int(data.split(":")[1]))
            try:
                await query.edit_message_caption("🗑 Post " + data.split(":")[1] + " deleted.")
            except Exception:
                await query.edit_message_text("🗑 Post " + data.split(":")[1] + " deleted.")
        except Exception as e:
            try:
                await query.edit_message_caption("❌ Delete failed: " + str(e))
            except Exception:
                await query.edit_message_text("❌ Delete failed: " + str(e))

    elif data.startswith("confirm:"):
        parts  = data.split(":", 2)
        action = parts[1]
        value  = parts[2] if len(parts) > 2 else ""

        if action == "delete_post":
            try:
                wp_delete(int(value))
                await query.edit_message_text("Post " + value + " deleted.")
            except Exception as e:
                await query.edit_message_text("Delete failed: " + str(e))

        elif action == "plugin_install":
            await query.edit_message_text("Installing plugin: " + value + "...")
            ok, out = wpcli("plugin install " + value + " --activate")
            await query.edit_message_text(("Plugin installed and activated: " if ok else "Failed: ") + value + "\n\n" + out[:500])

        elif action == "plugin_activate":
            ok, out = wpcli("plugin activate " + value)
            await query.edit_message_text(("Activated: " if ok else "Failed: ") + value + "\n" + out[:300])

        elif action == "plugin_deactivate":
            await query.edit_message_text("Deactivating plugin: " + value + "...")
            ok, out = wpcli("plugin deactivate " + value)
            await query.edit_message_text(("Deactivated: " if ok else "Failed: ") + value + "\n" + out[:300])

        elif action == "theme_activate":
            await query.edit_message_text("Activating theme: " + value + "...")
            ok, out = wpcli("theme activate " + value)
            await query.edit_message_text(("Theme activated: " if ok else "Failed: ") + value + "\n" + out[:300])

        elif action == "wpcli":
            await query.edit_message_text("Running: wp " + value + "...")
            ok, out = wpcli(value)
            status  = "Done" if ok else "Error"
            await query.edit_message_text(status + "\n\n" + out[:3000])

        elif action == "install_github":
            await query.edit_message_text("Starting GitHub install...")
            await do_install_github(value, ctx.bot, query.from_user.id)

    elif data == "news_skip":
        try:
            await query.edit_message_caption("⏭ Skipped.")
        except Exception:
            await query.edit_message_text("⏭ Skipped.")

    elif data == "noimg_yes":
        pending_url = ctx.user_data.get("pending_publish_url")
        if not pending_url:
            return await query.edit_message_text("❌ Session expired. Please run /publish again.")
        ctx.user_data.pop("pending_publish_url", None)
        await query.edit_message_text(f"⏳ Fetching and generating post (no image)...")
        await cmd_publish_url(pending_url, query.message, ctx)

    elif data == "noimg_cancel":
        ctx.user_data.pop("pending_publish_url", None)
        await query.edit_message_text("❌ Cancelled. Try a different URL with /publish.")

    elif data.startswith("news_pub:"):
        news_key   = data.split(":", 1)[1]
        cached     = _news_cache.get(news_key, {})
        news_url   = cached.get("url",    "")
        news_src   = cached.get("source", "Unknown")
        news_title = cached.get("title",  "News")
        try:
            await query.edit_message_caption("✍️ Writing & publishing news post with Mistral...")
        except Exception:
            await query.edit_message_text("✍️ Writing & publishing news post with Mistral...")
        try:
            article = write_news_post(news_title, news_url, news_src, "")

            # Use fallback title+content if Mistral failed
            if not article or not article.get("content"):
                article = {
                    "title":   news_title[:80],
                    "content": f'<p><strong>Source: <a href="{news_url}" target="_blank">{news_src}</a></strong></p>',
                    "excerpt": "",
                    "tags":    [],
                }
            # ── Duplicate check ──────────────────────────────────────────────
            if wp_title_exists(article["title"]):
                try:
                    await query.edit_message_caption(f"⚠️ Skipped — similar post already exists:\n{article['title']}")
                except Exception:
                    await query.edit_message_text(f"⚠️ Skipped — similar post already exists:\n{article['title']}")
                return

            # Download and upload image from Telegram message
            media_id = None
            if query.message.photo:
                try:
                    photo     = query.message.photo[-1]
                    file      = await ctx.bot.get_file(photo.file_id)
                    img_bytes = await file.download_as_bytearray()
                    media     = wp_upload_image(bytes(img_bytes), "news-image.jpg", "image/jpeg")
                    media_id  = media["id"]
                except Exception as img_e:
                    logger.warning(f"News image upload failed: {img_e}")

            # Get News category ID
            news_cat_id = None
            cat_r = requests.get(f"{WP_API}/categories",
                params={"search": "News", "per_page": 1}, auth=WP_AUTH, timeout=10)
            if cat_r.ok and cat_r.json():
                news_cat_id = cat_r.json()[0]["id"]

            # Create post data
            post_data = {
                "title":      article["title"],
                "content":    article["content"],
                "excerpt":    article.get("excerpt", ""),
                "status":     "publish",  # Publish directly — user already approved
                "categories": [news_cat_id] if news_cat_id else [],
            }
            if article.get("tags"):
                # Create/get tag IDs
                tag_ids = []
                for tag in article["tags"][:5]:
                    tr = requests.post(f"{WP_API}/tags",
                        json={"name": tag}, auth=WP_AUTH, timeout=5)
                    if tr.ok:
                        tag_ids.append(tr.json()["id"])
                if tag_ids:
                    post_data["tags"] = tag_ids

            # Publish post
            pr = requests.post(f"{WP_API}/posts", json=post_data, auth=WP_AUTH, timeout=15)
            if not pr.ok:
                try:
                    return await query.edit_message_caption(f"Publish failed: {pr.text[:100]}")
                except Exception:
                    return await query.edit_message_text(f"Publish failed: {pr.text[:100]}")

            post_id  = pr.json()["id"]
            post_url = pr.json().get("link", f"https://tensoredge.net/?p={post_id}")

            # Set featured image
            if media_id:
                requests.post(f"{WP_API}/posts/{post_id}",
                    json={"featured_media": media_id}, auth=WP_AUTH, timeout=10)

            await query.edit_message_caption(
                f"✅ Published!\n\n"
                f"Title: {article['title'][:60]}\n"
                f"Source: {news_src}\n"
                f"URL: {post_url}"
            )
        except Exception as e:
            logger.error(f"News publish error: {e}")
            await query.edit_message_caption(f"Failed: {str(e)[:100]}")

# ── Natural language chat ─────────────────────────────────────────────────────────
async def unknown_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update): return
    user_text = update.message.text.strip()
    if not user_text:
        return
    user_id = update.effective_user.id
    if user_id not in chat_mode_users:
        await update.message.reply_text("Use /chat to enable conversation mode, or /help for commands.")
        return
    msg = await update.message.reply_text("Thinking...")
    try:
        response = ollama_chat(HERMES_IDENTITY, user_text, timeout=120)
        await msg.edit_text(response)
    except Exception as e:
        logger.error(f"Chat error: {e}")
        await msg.edit_text("Mistral is warming up, try again in a moment.")

async def cmd_fix(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Find published posts missing a featured image and try to fetch one from the source."""
    if not is_authorized(update): return await deny(update)
    msg = await update.message.reply_text("🔍 Scanning for published posts missing a featured image...")

    # Collect all published posts with no featured image (paginated)
    all_posts = []
    page = 1
    while True:
        r = requests.get(
            f"{WP_API}/posts",
            params={
                "status": "publish",
                "per_page": 100,
                "page": page,
                "_fields": "id,title,content,link,featured_media",
            },
            auth=WP_AUTH,
            timeout=15,
        )
        if not r.ok:
            break
        batch = r.json()
        if not batch:
            break
        all_posts.extend(p for p in batch if p.get("featured_media", 1) == 0)
        if len(batch) < 100:
            break
        page += 1

    total = len(all_posts)
    if total == 0:
        return await msg.edit_text("✅ All published posts already have a featured image.")

    await msg.edit_text(f"Found {total} post(s) without a featured image. Processing...")

    fixed = 0
    unfixed = 0

    for post in all_posts:
        post_id = post["id"]
        title   = post.get("title", {}).get("rendered", f"Post {post_id}")
        content = post.get("content", {}).get("rendered", "")

        # Find first external href that isn't tensoredge.net
        source_url = None
        for href in re.findall(r'href=["\']((https?://)[^"\']+)["\']', content):
            candidate = href[0]
            if "tensoredge.net" not in candidate:
                source_url = candidate
                break

        image_url = None
        if source_url:
            try:
                image_url = fetch_article_image(source_url)
            except Exception:
                pass

        if image_url:
            try:
                img_r = requests.get(image_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
                img_r.raise_for_status()
                ext = image_url.split("?")[0].split(".")[-1].lower()
                if ext not in ["jpg", "jpeg", "png", "webp"]:
                    ext = "jpg"
                mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")
                media = wp_upload_image(img_r.content, f"post-{post_id}-hero.{ext}", mime)
                requests.post(
                    f"{WP_API}/posts/{post_id}",
                    json={"featured_media": media["id"]},
                    auth=WP_AUTH,
                    timeout=10,
                )
                fixed += 1
                await update.message.reply_text(f"✅ Fixed: {title[:80]}")
            except Exception as e:
                unfixed += 1
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{post_id}"),
                    InlineKeyboardButton("⏭ Skip",   callback_data="cancel"),
                ]])
                await update.message.reply_text(
                    f"⚠️ Image upload failed for:\n{title[:80]}\n\nError: {str(e)[:100]}",
                    reply_markup=kb,
                )
        else:
            unfixed += 1
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{post_id}"),
                InlineKeyboardButton("⏭ Skip",   callback_data="cancel"),
            ]])
            await update.message.reply_text(
                f"⚠️ No image found for:\n{title[:80]}",
                reply_markup=kb,
            )

    await update.message.reply_text(
        f"✅ Done. Fixed {fixed} / {total} posts. {unfixed} could not be fixed."
    )

# ── Main ─────────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("fetch",     cmd_fetch))
    app.add_handler(CommandHandler("approve",   cmd_approve))
    app.add_handler(CommandHandler("drafts",    cmd_drafts))
    app.add_handler(CommandHandler("published", cmd_published))
    app.add_handler(CommandHandler("edit",      cmd_edit))
    app.add_handler(CommandHandler("replace",  cmd_replace))
    app.add_handler(CommandHandler("seo",       cmd_seo))
    app.add_handler(CommandHandler("unpublish", cmd_unpublish))
    app.add_handler(CommandHandler("delete",    cmd_delete))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("health",    cmd_health))
    app.add_handler(CommandHandler("image",     cmd_image))
    app.add_handler(CommandHandler("linkedin",  cmd_linkedin))
    app.add_handler(CommandHandler("digest",    cmd_digest))
    app.add_handler(CommandHandler("trending",  cmd_trending))
    app.add_handler(CommandHandler("plugin",    cmd_plugin))
    app.add_handler(CommandHandler("theme",     cmd_theme))
    app.add_handler(CommandHandler("pages",     cmd_pages))
    app.add_handler(CommandHandler("wpcli",     cmd_wpcli))
    app.add_handler(CommandHandler("install",   cmd_install))
    app.add_handler(CommandHandler("github",    cmd_github))
    app.add_handler(CommandHandler("cron",      cmd_cron))
    app.add_handler(CommandHandler("newswatch",  cmd_newswatch))
    app.add_handler(CommandHandler("publish",    cmd_publish))
    app.add_handler(CommandHandler("fix",       cmd_fix))
    app.add_handler(CommandHandler("chat",      cmd_chat))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))

    # ── Dynamic APScheduler (replaces hardcoded job_queue) ──────────────────────
    global _scheduler, _bot_instance
    _scheduler    = AsyncIOScheduler(timezone=DOHA_TZ)
    _bot_instance = app.bot
    _scheduler.start()

    # Seed default jobs if no jobs exist yet
    if not load_cron_jobs():
        default_jobs = {
            "digest_0800":    {"action": "digest",   "hour": 8,  "minute": 0},
            "trending_1000":  {"action": "trending",  "hour": 10, "minute": 0},
            "trending_1600":  {"action": "trending",  "hour": 16, "minute": 0},
        }
        save_cron_jobs(default_jobs)
        logger.info("Seeded default cron jobs.")

    # News monitor — every 6 hours (standalone async job, no ctx needed)
    _scheduler.add_job(
        lambda: asyncio.run(run_news_monitor()),
        trigger=CronTrigger(hour="6,12,18,0", minute=30, timezone=DOHA_TZ),
        id="news_monitor",
        replace_existing=True,
    )
    logger.info("News monitor scheduled: 06:30, 12:30, 18:30, 00:30 Doha time")

    # Load all persisted jobs
    reload_all_cron_jobs()

    logger.info("Hermes v2 started - dynamic cron scheduler active. Use /cron to manage jobs.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
