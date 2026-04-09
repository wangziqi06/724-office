"""
Tool Registry — Orchestration Layer

All LLM-callable tool definitions + implementations, split across domain modules:
  tools_base.py      — registry, decorator, helpers
  tools_messaging.py — exec/message/files/schedule/media
  tools_video.py     — trim/bgm/generate_video
  tools_search.py    — web_search/memory/recall
  tools_admin.py     — diagnose/audit/plugins/mcp/archive
  tools_page.py      — render_page (interactive HTML page generation)
  tools_mirror.py    — soul_report/future_self (AI mirror)

## Adding a new tool:
1. Write a function in the corresponding domain module
2. Decorate with @tool

No other files need to be modified.
"""

# Import sub-modules (triggers @tool registration)
from tools_base import get_definitions, execute, _strip_markdown, _split_message  # noqa: F401
import tools_messaging  # noqa: F401
import tools_video  # noqa: F401
import tools_search  # noqa: F401
import tools_admin  # noqa: F401
import tools_page  # noqa: F401
import tools_mirror  # noqa: F401


def init_extra(config):
    """Called by main entry point to pass extra configuration"""
    # Search engine config
    tools_search.init_search_config(config)
    # Video API config
    tools_video.set_video_config(config.get("video_api", {}))
    # Plugin loading
    tools_admin._load_plugins()
    # MCP servers
    tools_admin._load_mcp_servers(config)
