# TensorEdge Publisher

AI-powered Telegram bot that monitors RSS feeds, writes and publishes articles to WordPress using local LLMs via Ollama. Built for [TensorEdge.net](https://tensoredge.net) — a tech blog covering mini PCs, local AI, and hardware.

## Features

- **`/newswatch`** — Scan curated RSS/news sources, score items by relevance, and send each one to Telegram with "Publish" / "Skip" buttons
- **`/publish <url>`** — Fetch any article URL, generate a full blog post with Mistral, create a WordPress draft, and send for approval
- **`/fix`** — Find all published posts missing a featured image, attempt to fetch one from the source article, or offer to delete
- **`/approve <id>`** — Publish a WordPress draft by ID
- **`/drafts`** — List current drafts
- **`/published`** — List recently published posts
- **`/edit <id> <instruction>`** — AI-edit a post using a natural-language instruction
- **`/seo <id>`** — Run an SEO analysis on a post
- **`/image <id>`** — Upload a photo from Telegram and set it as the featured image
- **`/delete <id>`** — Delete a post
- **`/unpublish <id>`** — Revert a published post to draft
- **`/replace <id> <old> | <new>`** — Direct text replacement in a post
- **`/linkedin <id>`** — Reformat a post for LinkedIn
- **`/digest`** — Get a curated news digest immediately
- **`/trending`** — AI-picked trending stories
- **`/plugin`** / **`/theme`** / **`/wpcli`** — Full WordPress admin control via WP-CLI
- **`/health`** — Site health report
- **`/status`** — Quick site stats
- **`/chat`** — Toggle free-form conversation with the local LLM

Content-aware generation: short articles (< 1 500 chars) get a concise 150–250 word news post; full articles get a detailed 400–600 word write-up with insight and analysis.

## Requirements

- Python 3.11+
- [Ollama](https://ollama.ai) with `mistral-small` (or any compatible model) running locally
- WordPress with the REST API enabled and an Application Password configured
- Docker + Docker Compose (for the full stack)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- (Optional) A Cloudflare Tunnel token for public HTTPS access to WordPress

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/MoHelal/tensoredge-publisher.git
cd tensoredge-publisher
```

### 2. Configure environment variables

```bash
cp .env.example .env
# Edit .env with your actual values
```

### 3. Start the stack

```bash
docker compose up -d
```

WordPress will be available at `http://localhost:80`. Configure your Cloudflare Tunnel to expose it publicly, then set up the WordPress REST API Application Password under **Users → Profile → Application Passwords**.

### 4. Run the bot standalone (without Docker)

```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN=...
export ALLOWED_TELEGRAM_USER_ID=...
# (set all required env vars)
python hermes_agent.py
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | Yes | Bot token from @BotFather |
| `ALLOWED_TELEGRAM_USER_ID` | Yes | Your Telegram user ID (bot is single-user) |
| `WP_USER` | Yes | WordPress username |
| `WP_APP_PASSWORD` | Yes | WordPress Application Password |
| `WP_INTERNAL_URL` | No | Internal URL of WordPress (default: `http://wordpress`) |
| `WP_CONTAINER` | No | Docker container name for WP-CLI (default: `tensoredge-publisher-wordpress-1`) |
| `OLLAMA_URL` | No | Ollama API endpoint (default: `http://host.docker.internal:11434`) |
| `OLLAMA_MODEL` | No | Model for reading/summarising (default: `mistral-small`) |
| `OLLAMA_WRITE_MODEL` | No | Model for writing posts (default: `mistral-small`) |
| `MYSQL_ROOT_PASSWORD` | Yes (compose) | MariaDB root password |
| `CLOUDFLARE_TUNNEL_TOKEN` | No | Cloudflare Tunnel token for public HTTPS |

## Architecture

```
Telegram ──► hermes_agent.py ──► Ollama (local LLM)
                    │
                    └──► WordPress REST API
                    └──► WP-CLI (via Docker exec)
                    └──► RSS / news sources
```

The bot is intentionally single-user: every command is gated behind `ALLOWED_TELEGRAM_USER_ID`.

## License

MIT
