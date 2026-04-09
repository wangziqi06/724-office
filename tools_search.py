"""
Search Tools — web_search + memory/recall
"""

import json
import os
import subprocess
import urllib.request
import urllib.parse

from tools_base import tool, log

_search_keys = {}  # set by init_search_config()

def init_search_config(config):
    global _search_keys
    _search_keys = {
        "tavily": config.get("tavily_api_key", ""),
        "bocha": config.get("bocha_api_key", ""),
        "github": config.get("github_token", ""),
        "huggingface": config.get("huggingface_token", ""),
    }

def _tavily_search(query, count=5):
    """Tavily API search — high quality for English content, returns original excerpts and links"""
    api_key = _search_keys.get("tavily", "")
    if not api_key:
        return "[error] Tavily API key not configured"

    url = "https://api.tavily.com/search"
    body = json.dumps({
        "api_key": api_key,
        "query": query,
        "search_depth": "advanced",
        "include_answer": True,
        "max_results": count,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        return f"[error] Tavily search failed: {e}"

    parts = []
    answer = data.get("answer")
    if answer:
        parts.append("== AI Summary ==\n" + answer)

    results = data.get("results", [])
    if not results:
        return answer or "No relevant results found."

    items = []
    for i, item in enumerate(results[:count], 1):
        title = item.get("title", "")
        content = item.get("content", "")[:300]
        link = item.get("url", "")
        score = item.get("score", 0)
        items.append(f"{i}. {title} (relevance: {score:.2f})\n   {content}\n   Link: {link}")
    parts.append("\n\n".join(items))
    return "\n\n".join(parts)


def _bocha_search(query, count=5):
    """Bocha API general web search"""
    api_key = _search_keys.get("bocha", "")
    if not api_key:
        return "[error] Bocha API key not configured"

    url = "https://api.bochaai.com/v1/web-search"
    body = json.dumps({"query": query, "count": count, "summary": True}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        return f"[error] Bocha search failed: {e}"

    results = []
    web_pages = data.get("data", {}).get("webPages", {}).get("value", [])
    if not web_pages:
        return "No relevant results found."
    for i, item in enumerate(web_pages[:count], 1):
        title = item.get("name", "")
        snippet = item.get("summary", item.get("snippet", ""))
        link = item.get("url", "")
        results.append(f"{i}. {title}\n   {snippet}\n   Link: {link}")
    return "\n\n".join(results)


def _github_search(query, count=5):
    """GitHub public API search: search repos first, then code, merge and dedupe"""
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "ai-agent",
    }
    results = []

    # 1. Search repositories (name + description + README)
    encoded = urllib.parse.quote(query)
    url = "https://api.github.com/search/repositories?q=%s&sort=stars&per_page=%d" % (encoded, count)
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        for item in data.get("items", [])[:count]:
            name = item.get("full_name", "")
            desc = (item.get("description") or "")[:150]
            stars = item.get("stargazers_count", 0)
            link = item.get("html_url", "")
            lang = item.get("language", "")
            updated = (item.get("updated_at") or "")[:10]
            line = "%s ⭐%d" % (name, stars)
            if lang:
                line += " [%s]" % lang
            if updated:
                line += " (updated %s)" % updated
            line += "\n   %s\n   Link: %s" % (desc, link)
            results.append(line)
    except Exception as e:
        results.append("[repo search error: %s]" % e)

    # 2. If repo results < 2, supplement with code search
    if len(results) < 2:
        try:
            code_url = "https://api.github.com/search/code?q=%s&per_page=%d" % (encoded, count)
            req = urllib.request.Request(code_url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                code_data = json.loads(resp.read())
            seen_repos = set()
            for item in code_data.get("items", []):
                repo = item.get("repository", {})
                repo_name = repo.get("full_name", "")
                if repo_name and repo_name not in seen_repos:
                    seen_repos.add(repo_name)
                    desc = (repo.get("description") or "")[:150]
                    link = repo.get("html_url", "")
                    results.append("%s (from code search)\n   %s\n   Link: %s" % (repo_name, desc, link))
                    if len(seen_repos) >= count:
                        break
        except Exception:
            pass  # Code search is supplementary, don't report errors

    if not results:
        return "No relevant projects found on GitHub."
    return "\n\n".join("%d. %s" % (i, r) for i, r in enumerate(results, 1))


def _huggingface_search(query, count=5):
    """HuggingFace API search for models"""
    encoded = urllib.parse.quote(query)
    url = f"https://huggingface.co/api/models?search={encoded}&sort=downloads&direction=-1&limit={count}"
    req = urllib.request.Request(url, headers={"User-Agent": "ai-agent"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        # Fallback to web search
        return _bocha_search(f"huggingface {query}", count)

    if not data:
        return "No relevant models found on HuggingFace."
    results = []
    for i, item in enumerate(data[:count], 1):
        model_id = item.get("modelId", item.get("id", ""))
        downloads = item.get("downloads", 0)
        likes = item.get("likes", 0)
        pipeline = item.get("pipeline_tag", "")
        results.append(f"{i}. {model_id} (downloads: {downloads}, likes: {likes})" +
                       (f" [{pipeline}]" if pipeline else "") +
                       f"\n   Link: https://huggingface.co/{model_id}")
    return "\n\n".join(results)


@tool("web_search", "Search the web. Supports multiple search sources. "
      "source=auto uses dual-engine (Tavily + Bocha) by default, specific keywords route to specialized sources. "
      "source=tavily for Tavily (English-optimized, returns excerpts + AI summary). "
      "source=github for GitHub. source=web for Bocha. source=all for all sources.",
      {"query": {"type": "string", "description": "Search keywords"},
       "source": {"type": "string", "description": "Search source: auto/web/tavily/github/huggingface/all",
                  "enum": ["auto", "web", "tavily", "github", "huggingface", "all"]},
       "count": {"type": "integer", "description": "Number of results (default 5)"}},
      ["query"])
def tool_web_search(args, ctx):
    query = args["query"]
    source = args.get("source", "auto")
    count = args.get("count", 5)

    if source == "auto":
        ql = query.lower()
        if any(kw in ql for kw in ["huggingface", "hugging face", "hf model"]):
            source = "huggingface"
        elif any(kw in ql for kw in ["github.com", "github repo"]):
            source = "github"
        # Verification queries: contains project/tool name + verification intent -> multi-engine
        elif any(kw in ql for kw in ["does it exist", "is it real", "verify", "exist",
                                      "skill", "plugin", "mcp", "tool",
                                      "open source", "repo"]):
            source = "all"
        else:
            source = "web+tavily"

    if source == "github":
        return _github_search(query, count)
    elif source == "tavily":
        return _tavily_search(query, count)
    elif source == "web+tavily":
        # Dual engine: Tavily + Bocha, return both results
        parts = []
        tav = _tavily_search(query, count)
        if tav and "[error]" not in tav:
            parts.append("== Tavily ==\n" + tav)
        bocha = _bocha_search(query, count)
        if bocha and "[error]" not in bocha:
            parts.append("== Bocha ==\n" + bocha)
        return "\n\n".join(parts) if parts else "No search results."
    elif source == "huggingface":
        return _huggingface_search(query, count)
    elif source == "all":
        parts = []
        tav = _tavily_search(query, max(count // 2, 3))
        if tav and "[error]" not in tav:
            parts.append("== Tavily ==\n" + tav)
        gh = _github_search(query, max(count // 2, 3))
        if gh and "[error]" not in gh:
            parts.append("== GitHub ==\n" + gh)
        bocha = _bocha_search(query, max(count // 2, 3))
        if bocha and "[error]" not in bocha:
            parts.append("== Bocha ==\n" + bocha)
        return "\n\n".join(parts) if parts else "No results from any search source."
    else:
        return _bocha_search(query, count)

# --- Memory search tools ---

@tool("search_memory", "Search memory files. Keyword search in workspace/memory/ directory, "
      "returns matching content snippets and filenames. More precise and efficient than reading all of MEMORY.md.",
      {"query": {"type": "string", "description": "Search keywords (space-separated for multiple)"},
       "scope": {"type": "string", "description": "Scope: all (default, all memory files), long (MEMORY.md only), daily (daily logs only)"}},
      ["query"])
def tool_search_memory(args, ctx):
    query = args["query"]
    scope = args.get("scope", "all")
    memory_dir = os.path.join(ctx["workspace"], "memory")

    if not os.path.isdir(memory_dir):
        return "Memory directory does not exist."

    grep_args = ["grep", "-r", "-i", "-n", "--include=*.md"]
    if scope == "long":
        target = os.path.join(memory_dir, "MEMORY.md")
        if not os.path.exists(target):
            return "MEMORY.md does not exist."
        grep_args = ["grep", "-i", "-n", "--", query, target]
    elif scope == "daily":
        grep_args.extend(["--include=2*.md", "--", query, memory_dir])
    else:
        grep_args.extend(["--", query, memory_dir])

    try:
        result = subprocess.run(grep_args, capture_output=True, text=True, timeout=10)
        output = result.stdout.strip()
        if not output:
            return "No memories found containing '%s'." % query

        lines = output.split("\n")
        if len(lines) > 30:
            return "\n".join(lines[:30]) + ("\n... %d matches total, showing first 30" % len(lines))
        return "%d matches:\n%s" % (len(lines), "\n".join(lines))
    except Exception as e:
        return "[error] search failed: %s" % e


# --- Semantic memory retrieval ---

@tool('recall', 'Semantic search through long-term memory. Use when the user asks about previous '
      'conversations or needs to recall historical information. Smarter than search_memory '
      '(vector semantic matching vs keyword matching).',
      {'query': {'type': 'string', 'description': 'Search keywords or question'}},
      ['query'])
def tool_recall(args, ctx):
    import memory as mem_mod
    result = mem_mod.retrieve(args['query'], ctx['session_key'], top_k=10)
    return result or 'No relevant memories found.'
