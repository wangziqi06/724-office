# 7/24 Office -- Self-Evolving AI Agent System

A production-running AI agent built in **~10,000 lines of pure Python** with **zero framework dependency**. No LangChain, no LlamaIndex, no CrewAI -- just the standard library + a few small packages.

**36 tools. 20 files. Modular architecture. Runs 24/7.**

Built solo with AI co-development tools. Production 24/7 across multiple users.

## Successor: xiaowang-v2 (Node rewrite, 2026)

The next generation lives in [`xiaowang-v2/`](./xiaowang-v2/): a single Node process + a single WAL SQLite + a hand-written agentic loop, zero framework dependency, running 24/7 over WeCom as a personal agent. Where 7/24 Office grew by adding tools, v2 converges: durable cross-day execution, layered memory with honest forgetting, turn assembly for chat-native input (burst messages / photo-then-caption / voice), and structural instruction-following — the model does fuzzy routing, the harness does the deterministic five (execution feedback, schema locks, state exposure, approval gates, idempotency). Design blueprints and 330+ offline selftest assertions included. Docs are in Chinese.

## What's New in v2.0

- **Modular tool architecture** -- Split from monolithic `tools.py` into 7 domain modules
- **Group chat support** -- Independent container for group conversations with @-mention gating
- **AI Mirror** -- Behavioral profile reports (`soul_report`) + future-self dialogue mode (`future_self`)
- **Nudge system** -- Structural behavior correction: auto-detects when LLM has tools but doesn't use them
- **Dynamic tool filtering** -- 5 context profiles (voice/scheduler/group/diagnostic/default) to reduce token waste
- **Budget-aware system prompt** -- Token budget tracking during system prompt assembly
- **Inactivity guard** -- Auto-skip cron tasks for dormant users (3-day threshold)
- **Circuit breaker** -- Disable tools after 3 consecutive failures per session
- **Interactive visualization** -- ECharts-based HTML pages via `render_page` (line/bar/pie/radar/table/report)
- **Container reconciliation** -- Router auto-rebuilds missing containers from routing table on startup
- **Exponential backoff retry** -- Messaging API calls retry 3x with 2/4/8s delays
- **Session auto-archiving** -- Daily black box recording of all conversations

## Features

- **Tool Use Loop** -- OpenAI-compatible function calling with automatic retry, up to 20 iterations per conversation
- **Three-Layer Memory** -- Session history + LLM-compressed long-term memory + LanceDB vector retrieval
- **MCP/Plugin System** -- Connect external MCP servers via JSON-RPC (stdio or HTTP), hot-reload without restart
- **Runtime Tool Creation** -- The agent can write, save, and load new Python tools at runtime (`create_tool`)
- **Self-Repair** -- Daily self-check, session health diagnostics, error log analysis, auto-notification on failure
- **Cron Scheduling** -- One-shot and recurring tasks, persistent across restarts, timezone-aware, inactivity guard
- **Multi-Tenant Router** -- Docker-based auto-provisioning, one container per user, health-checked, reconciliation
- **Multimodal** -- Image/video/file/voice/link handling, ASR (speech-to-text), vision via base64
- **Web Search** -- Multi-engine (Tavily, Bocha, GitHub, HuggingFace) with auto-routing and dual-engine default
- **Video Processing** -- Trim (with intelligent silence detection), add BGM, AI video generation via API
- **Messaging Integration** -- Pluggable messaging platform with debounce, message splitting, streaming media upload
- **Group Chat** -- Independent container, @-mention gating, context buffer (last 20 messages), speaker identification

## Architecture

```
                    +-----------------+
                    |  Messaging      |
                    |  Platform       |
                    +--------+--------+
                             |
                    +--------v--------+
                    |   router.py     |  Multi-tenant routing
                    |  Auto-provision |  Container reconciliation
                    |  Group routing  |  Health checking
                    +--------+--------+
                             |
                    +--------v--------+
                    | xiaowang.py     |  Entry point
                    |  HTTP server    |  Callback handling
                    |  Debounce       |  Media download/ASR
                    |  Group support  |  Inactivity tracking
                    +--------+--------+
                             |
                    +--------v--------+
                    |    llm.py       |  Tool Use Loop (core)
                    |  Budget-aware   |  Session management
                    |  system prompt  |  Nudge integration
                    |  Multimodal     |  Memory injection
                    +--------+--------+
                             |
     +----------+----+------+------+----+-----------+
     |          |    |             |    |           |
+----v-----+ +-v----v--+ +-------v-+ +-v--------+ |
| tools_    | |tools_   | |tools_   | |tools_    | |
| messaging | |admin    | |search   | |video     | |
| send/file | |exec/diag| |web/mem  | |trim/bgm  | |
| schedule  | |plugin   | |recall   | |generate  | |
+-----------+ |MCP      | +---------+ +----------+ |
              +----+----+                           |
              +----v----+  +----------+  +----------v--+
              |tools_   |  |tools_    |  |  nudge.py   |
              |page     |  |mirror    |  |  5 rules    |
              |ECharts  |  |soul rpt  |  |  auto-hint  |
              |6 types  |  |future    |  +-------------+
              +---------+  |self      |
                           +----------+
              +--------------+--------------+
              |              |              |
       +------v------+  +---v--------+  +--v-----------+
       | memory.py   |  |scheduler.py|  | archive.py   |
       | 3-layer     |  | cron+once  |  | daily black  |
       | compress    |  | inactivity |  | box recorder |
       | deduplicate |  | guard      |  +--------------+
       | retrieve    |  +------------+
       +------+------+
              |
       +------v------+
       |mcp_client.py|  JSON-RPC over stdio/HTTP
       | Auto-reconnect + Hot-reload
       +-------------+
```

## Memory System

```
Layer 1: Session (short-term)
  - Last 40 messages per session, JSON files
  - Overflow triggers compression
  - Auto-archive sessions >100KB

Layer 2: Compressed (long-term)
  - LLM extracts structured facts from evicted messages
  - Deduplication via cosine similarity (threshold: 0.92)
  - Stored as vectors in LanceDB

Layer 3: Retrieval (active recall)
  - User message -> embedding -> vector search
  - Top-K relevant memories injected into system prompt
  - Budget-aware injection (tracks token usage)
```

## Tool List (36 built-in)

| Category | Module | Tools |
|----------|--------|-------|
| Core | `tools_admin` | `exec`, `message` |
| Files | `tools_admin` | `read_file`, `write_file`, `edit_file`, `list_files` |
| Scheduling | `tools_messaging` | `schedule`, `list_schedules`, `remove_schedule` |
| Media Send | `tools_messaging` | `send_image`, `send_file`, `send_video`, `send_link`, `send_location`, `send_namecard` |
| Video | `tools_video` | `trim_video` (auto-cut silence), `add_bgm`, `generate_video` |
| Search | `tools_search` | `web_search` (Tavily+Bocha dual-engine), `search_nearby` (geocoding+POI), `search_memory`, `recall` |
| Visualization | `tools_page` | `render_page` (line/bar/pie/radar/table/report via ECharts) |
| AI Mirror | `tools_mirror` | `soul_report` (behavioral profile HTML), `future_self` (dialogue mode) |
| Diagnostics | `tools_admin` | `self_check`, `diagnose`, `task_history`, `code_audit`, `asr_check`, `daily_digest` |
| Memory | `tools_admin` | `compact_memory`, `compact_guides` |
| Plugins | `tools_admin` | `create_tool`, `list_custom_tools`, `remove_tool` |
| MCP | `tools_admin` | `reload_mcp` |

## Module Structure

| File | Lines | Responsibility |
|------|-------|---------------|
| `xiaowang.py` | ~1040 | Entry: config, HTTP server, callbacks, debounce, ASR, group support |
| `llm.py` | ~1260 | LLM API + tool use loop + budget-aware system prompt + nudge integration |
| `tools.py` | ~37 | Orchestration layer (imports domain modules) |
| `tools_base.py` | ~314 | Registry + @tool decorator + dynamic filtering + circuit breaker |
| `tools_messaging.py` | ~550 | Message/file/schedule/location/namecard tools |
| `tools_admin.py` | ~860 | Exec/file ops/diagnostics/plugins/MCP management |
| `tools_mirror.py` | ~716 | AI Mirror: soul_report + future_self |
| `tools_page.py` | ~470 | Interactive HTML page generation (ECharts) |
| `tools_search.py` | ~293 | Multi-engine web search + memory search |
| `tools_video.py` | ~394 | Video trim/BGM/generation |
| `messaging.py` | ~447 | Messaging platform API wrapper + CDN upload/download |
| `memory.py` | ~1100 | Three-layer memory (session + compressed + vector) |
| `scheduler.py` | ~652 | Cron + one-shot scheduling + inactivity guard |
| `router.py` | ~500+ | Multi-tenant Docker router + auto-provisioning + reconciliation |
| `mcp_client.py` | ~342 | MCP protocol client (JSON-RPC, zero new deps) |
| `nudge.py` | ~190 | Nudge system: detect tool misuse, auto-inject hints |
| `archive.py` | ~204 | Daily session archiving (black box recorder) |
| `audit.py` | ~448 | Automated 11-check code audit |

## Quick Start

### Option 1: Direct Run

```bash
git clone https://github.com/wangziqi06/724-office.git
cd 724-office
cp config.example.json config.json
# Edit config.json with your API keys

pip install croniter lancedb websocket-client pilk numpy httpx beautifulsoup4 pydub jieba fpdf2

mkdir -p workspace/memory workspace/files
python3 xiaowang.py
```

### Option 2: Docker Deployment (Recommended)

```bash
# Copy Dockerfile.example -> Dockerfile
# Copy docker-compose.example.yml -> docker-compose.yml
# Edit .env with your credentials

docker compose build
docker compose up -d
```

The agent starts an HTTP server on port 8080. Point your messaging platform webhook to `http://YOUR_SERVER:8080/`.

## Configuration

See `config.example.json` for the full configuration structure. Key sections:

- **models** -- LLM providers (any OpenAI-compatible API) with fallback chain
- **messaging** -- Messaging platform credentials and endpoints
- **memory** -- Three-layer memory system settings (embedding API, similarity threshold)
- **asr** -- Speech-to-text API credentials
- **video_api** -- AI video generation API
- **mcp_servers** -- MCP server connections (stdio or HTTP transport)
- **page_base_url** -- Base URL for generated visualization pages

## Design Principles

1. **Zero framework dependency** -- Every line is visible and debuggable. No magic. No hidden abstractions.
2. **Modular tools** -- Adding a capability = adding one `@tool`-decorated function in the appropriate domain module.
3. **Edge-deployable** -- Designed to run on Jetson Orin Nano (8GB RAM, ARM64). RAM budget under 2GB.
4. **Self-evolving** -- The agent can create new tools at runtime, diagnose its own issues, and notify the owner.
5. **Structural behavior correction** -- Don't fix agent mistakes with prompts. Add nudges, hooks, and validation.
6. **Build for deletion** -- Every module should be cleanly removable when the model gets smarter.
7. **Context is the scarcest resource** -- Token budget is the core design constraint, not compute.

## License

MIT
