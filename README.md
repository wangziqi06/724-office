# 7/24 Office -- Self-Evolving AI Agent System

A production-running AI agent built in **~3,500 lines of pure Python** with **zero framework dependency**. No LangChain, no LlamaIndex, no CrewAI -- just the standard library + 3 small packages (`croniter`, `lancedb`, `websocket-client`).

**26 tools. 8 files. Runs 24/7.**

Built solo with AI co-development tools in under 3 months. Production 24/7.

## Features

- **Tool Use Loop** -- OpenAI-compatible function calling with automatic retry, up to 20 iterations per conversation
- **Three-Layer Memory** -- Session history + LLM-compressed long-term memory + LanceDB vector retrieval
- **MCP/Plugin System** -- Connect external MCP servers via JSON-RPC (stdio or HTTP), hot-reload without restart
- **Runtime Tool Creation** -- The agent can write, save, and load new Python tools at runtime (`create_tool`)
- **Self-Repair** -- Daily self-check, session health diagnostics, error log analysis, auto-notification on failure
- **Cron Scheduling** -- One-shot and recurring tasks, persistent across restarts, timezone-aware
- **Multi-Tenant Router** -- Docker-based auto-provisioning, one container per user, health-checked
- **Multimodal** -- Image/video/file/voice/link handling, ASR (speech-to-text), vision via base64
- **Web Search** -- Multi-engine (Tavily, web search, GitHub, HuggingFace) with auto-routing
- **Video Processing** -- Trim, add BGM, AI video generation -- all via ffmpeg + API, exposed as tools
- **Messaging Integration** -- WeChat Work (Enterprise WeChat) with debounce, message splitting, media upload/download

## Architecture

```
                    +-----------------+
                    |  Messaging      |
                    |  Platform       |
                    +--------+--------+
                             |
                    +--------v--------+
                    |   router.py     |  Multi-tenant routing
                    |  (per-user      |  Auto-provision containers
                    |   containers)   |
                    +--------+--------+
                             |
                    +--------v--------+
                    | xiaowang.py     |  Entry point
                    |  HTTP server    |  Callback handling
                    |  Debounce       |  Media download
                    |  ASR pipeline   |  File persistence
                    +--------+--------+
                             |
                    +--------v--------+
                    |    llm.py       |  Tool Use Loop (core)
                    |  LLM API call   |  Session management
                    |  System prompt  |  Cross-session context
                    |  Multimodal     |  Memory injection
                    +--------+--------+
                             |
              +--------------+--------------+
              |              |              |
     +--------v---+  +------v------+  +----v--------+
     |  tools.py   |  | memory.py  |  |scheduler.py |
     | 26 built-in |  | 3-stage    |  | cron + once |
     | tools +     |  | pipeline:  |  | jobs.json   |
     | plugin sys  |  | compress   |  | persistent  |
     | + MCP bridge|  | deduplicate|  | tz-aware    |
     +------+------+  | retrieve   |  +-------------+
            |          +------------+
     +------v------+
     |mcp_client.py|  JSON-RPC over stdio/HTTP
     | MCP protocol|  Namespace: server__tool
     | Auto-reconnect  Hot-reload support
     +-------------+
```

## Memory System

```
Layer 1: Session (short-term)
  - Last 40 messages per session, JSON files
  - Overflow triggers compression

Layer 2: Compressed (long-term)
  - LLM extracts structured facts from evicted messages
  - Deduplication via cosine similarity (threshold: 0.92)
  - Stored as vectors in LanceDB

Layer 3: Retrieval (active recall)
  - User message -> embedding -> vector search
  - Top-K relevant memories injected into system prompt
  - Zero-latency cache for hardware/voice channels
```

## Tool List (26 built-in)

| Category | Tools |
|----------|-------|
| Core | `exec`, `message` |
| Files | `read_file`, `write_file`, `edit_file`, `list_files` |
| Scheduling | `schedule`, `list_schedules`, `remove_schedule` |
| Media Send | `send_image`, `send_file`, `send_video`, `send_link` |
| Video | `trim_video`, `add_bgm`, `generate_video` |
| Search | `web_search` (multi-engine: Tavily, web, GitHub, HuggingFace) |
| Memory | `search_memory`, `recall` (vector semantic search) |
| Diagnostics | `self_check`, `diagnose` |
| Plugins | `create_tool`, `list_custom_tools`, `remove_tool` |
| MCP | `reload_mcp` |

## Quick Start
0. **Prerequisites:**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
uv venv
source .venv/bin/activate
```
   
1. **Clone and configure:**

```bash
git clone https://github.com/your-username/724-office.git
cd 724-office
cp config.example.json config.json
# Edit config.json with your API keys
```

2. **Install dependencies:**

```bash
uv pip install croniter lancedb websocket-client
# Optional: pilk (for WeChat silk audio decoding)
uv pip install discord.py
```

3. **Set up workspace:**

```bash
mkdir -p workspace/memory workspace/files
```

4. **Create personality files** (optional but recommended):

```bash
# workspace/SOUL.md  -- Agent personality and behavior rules
# workspace/AGENT.md -- Operational procedures and troubleshooting guide
# workspace/USER.md  -- User preferences and context
```

5. **Run:**

```bash
python3 xiaowang.py
```

The agent starts an HTTP server on port 8080 (configurable). Point your messaging platform webhook to `http://YOUR_SERVER_IP:8080/`.

## Configuration

See `config.example.json` for the full configuration structure. Key sections:

- **models** -- LLM providers (any OpenAI-compatible API)
- **messaging** -- Messaging platform credentials
- **memory** -- Three-layer memory system settings
- **asr** -- Speech-to-text API credentials
- **mcp_servers** -- MCP server connections

### Supported LLM Providers

Any OpenAI-compatible API works out of the box. Tested providers include:

| Provider | api_base | Models |
|----------|----------|--------|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o`, `gpt-4o-mini` |
| [MiniMax](https://www.minimaxi.com/) | `https://api.minimax.io/v1` | `MiniMax-M2.7`, `MiniMax-M2.5` |

#### MiniMax Setup

[MiniMax](https://www.minimaxi.com/) provides high-performance LLM and embedding APIs. To use MiniMax:

1. Get an API key from the [MiniMax platform](https://platform.minimaxi.com/)
2. Set the default provider to `minimax` in `config.json`:

```json
{
  "models": {
    "default": "minimax",
    "providers": {
      "minimax": {
        "api_base": "https://api.minimax.io/v1",
        "api_key": "YOUR_MINIMAX_API_KEY",
        "model": "MiniMax-M2.7",
        "max_tokens": 8192
      }
    }
  }
}
```

For MiniMax embeddings (embo-01, 1536 dimensions), use the native embedding API:

```json
{
  "memory": {
    "enabled": true,
    "embedding_api": {
      "api_base": "https://api.minimax.io/v1",
      "api_key": "YOUR_MINIMAX_API_KEY",
      "model": "embo-01",
      "dimension": 1536
    }
  }
}
```

**Notes:**
- Temperature is automatically clamped to `[0, 1.0]` for MiniMax (max 1.0 vs OpenAI's 2.0)
- M2.5/M2.7 reasoning tags (`<think>...</think>`) are automatically stripped from responses
- MiniMax embo-01 uses a native API format (handled automatically when `api_base` contains `minimax`)

## Design Principles

1. **Zero framework dependency** -- Every line is visible and debuggable. No magic. No hidden abstractions.
2. **Single-file tools** -- Adding a capability = adding one function with `@tool` decorator in `tools.py`.
3. **Edge-deployable** -- Designed to run on Jetson Orin Nano (8GB RAM, ARM64 + GPU). RAM budget under 2GB.
4. **Self-evolving** -- The agent can create new tools at runtime, diagnose its own issues, and notify the owner.
5. **Offline-capable** -- Core functionality works without cloud APIs (except the LLM itself). Local embeddings supported.

## License

MIT
