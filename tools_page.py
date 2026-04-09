"""
Visualization page tools — generate interactive HTML pages, sent via link cards

Each page is a self-contained HTML file (ECharts CDN + inline data), auto-cleaned after 24h.
"""

import json
import logging
import os
import time
import uuid

import messaging
from tools_base import tool

log = logging.getLogger("agent")

PAGES_DIR = "/pages"
PAGE_EXPIRY = 24 * 3600  # 24h


def _get_base_url():
    """Read page_base_url from config.json, fallback to default"""
    config_path = os.environ.get("AGENT_CONFIG", "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("page_base_url", "http://your-server-ip/p")
    except Exception:
        return "http://your-server-ip/p"


def cleanup_expired_pages():
    """Clean up expired pages (>24h)"""
    if not os.path.isdir(PAGES_DIR):
        return 0
    now = time.time()
    removed = 0
    try:
        for f in os.listdir(PAGES_DIR):
            if not f.endswith(".html"):
                continue
            fp = os.path.join(PAGES_DIR, f)
            try:
                if now - os.path.getmtime(fp) > PAGE_EXPIRY:
                    os.remove(fp)
                    removed += 1
            except OSError:
                pass
    except Exception as e:
        log.warning(f"[pages] cleanup error: {e}")
    if removed:
        log.info(f"[pages] cleaned up {removed} expired pages")
    return removed


# -- HTML template framework ------------------------------------------------

_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    background: #f5f3ef;
    color: #374151;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans SC", sans-serif;
    font-size: 15px;
    line-height: 1.7;
    padding: 20px 16px;
    min-height: 100vh;
}
h1 {
    font-size: 20px;
    font-weight: 600;
    color: #0f2b5b;
    margin-bottom: 20px;
    text-align: center;
    letter-spacing: 0.5px;
    padding-bottom: 12px;
    border-bottom: 2px solid #c8952e;
}
h2 {
    font-size: 16px;
    font-weight: 600;
    color: #0f2b5b;
    margin: 24px 0 12px;
    padding-bottom: 8px;
    border-bottom: 2px solid #c8952e;
}
.chart-box {
    width: 100%;
    height: 320px;
    margin: 16px 0;
    background: #ffffff;
    border-radius: 12px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
    border-top: 3px solid #c8952e;
}
.loading {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: #9ca3af;
    font-size: 14px;
}
table {
    width: 100%;
    border-collapse: collapse;
    margin: 16px 0;
    font-size: 14px;
    background: #ffffff;
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
}
th {
    background: #0f2b5b;
    color: #ffffff;
    padding: 12px 10px;
    text-align: left;
    font-weight: 500;
    font-size: 13px;
    letter-spacing: 0.3px;
}
td {
    padding: 11px 10px;
    border-bottom: 1px solid #f0f0f0;
    color: #374151;
}
tr:last-child td { border-bottom: none; }
tr:hover td { background: #f8fafc; }
.section {
    margin: 16px 0;
    padding: 16px;
    background: #ffffff;
    border-radius: 12px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
    border-left: 3px solid #c8952e;
}
.section p {
    margin: 8px 0;
    color: #4b5563;
}
.footer {
    text-align: center;
    color: #9ca3af;
    font-size: 12px;
    margin-top: 32px;
    padding-top: 16px;
    border-top: 1px solid #c8952e;
    letter-spacing: 0.3px;
}
"""

_COLORS = "['#1a56db','#c8952e','#6b9bd2','#e8c87a','#94a3b8','#d4a853','#3b82f6']"

_ECHARTS_CDN = "https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"


def _html_wrap(title, body_content, has_chart=True):
    """Generate complete HTML page wrapper"""
    from datetime import datetime
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    echarts_tag = f'<script src="{_ECHARTS_CDN}"></script>' if has_chart else ""
    # Escape title for HTML
    safe_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">\n'
        f'<meta property="og:title" content="{safe_title}">\n'
        '<meta name="robots" content="noindex, nofollow">\n'
        f'<title>{safe_title}</title>\n'
        f'{echarts_tag}\n'
        f'<style>{_CSS}</style>\n'
        '</head>\n<body>\n'
        f'<h1>{safe_title}</h1>\n'
        f'{body_content}\n'
        f'<div class="footer">AI Agent &middot; Valid for 24h</div>\n'
        '</body>\n</html>'
    )


def _chart_init_js(chart_id, data, setup_code):
    """Generate ECharts initialization JS block (waits for CDN to load)"""
    data_json = json.dumps(data, ensure_ascii=False)
    return (
        "<script>\n"
        "document.addEventListener('DOMContentLoaded', function() {\n"
        f"  var el = document.getElementById('{chart_id}');\n"
        "  var intv = setInterval(function() {\n"
        "    if (typeof echarts !== 'undefined') {\n"
        "      clearInterval(intv);\n"
        f"      var d = {data_json};\n"
        f"      var chart = echarts.init(el);\n"
        f"      {setup_code}\n"
        "      window.addEventListener('resize', function() { chart.resize(); });\n"
        "    }\n"
        "  }, 100);\n"
        "});\n"
        "</script>\n"
    )


# -- 6 templates -------------------------------------------------------------

def _render_line(title, data):
    """Line chart: {x: [...], series: [{name, values}]}"""
    setup = (
        "chart.setOption({\n"
        "  backgroundColor: 'transparent',\n"
        "  tooltip: { trigger: 'axis' },\n"
        "  legend: { data: d.series.map(function(s){ return s.name; }), textStyle: { color: '#374151' } },\n"
        "  grid: { left: 40, right: 20, top: 40, bottom: 30 },\n"
        "  xAxis: { type: 'category', data: d.x, axisLabel: { color: '#6b7280' } },\n"
        "  yAxis: { type: 'value', axisLabel: { color: '#6b7280' }, splitLine: { lineStyle: { color: '#e5e7eb' } } },\n"
        "  series: d.series.map(function(s) {\n"
        "    return { name: s.name, type: 'line', data: s.values, smooth: true, symbolSize: 6 };\n"
        "  }),\n"
        f"  color: {_COLORS}\n"
        "});\n"
    )
    body = (
        '<div id="chart" class="chart-box"><div class="loading">Loading...</div></div>\n'
        + _chart_init_js("chart", data, setup)
    )
    return _html_wrap(title, body)


def _render_bar(title, data):
    """Bar chart: {x: [...], series: [{name, values}]}"""
    setup = (
        "chart.setOption({\n"
        "  backgroundColor: 'transparent',\n"
        "  tooltip: { trigger: 'axis' },\n"
        "  legend: { data: d.series.map(function(s){ return s.name; }), textStyle: { color: '#374151' } },\n"
        "  grid: { left: 40, right: 20, top: 40, bottom: 30 },\n"
        "  xAxis: { type: 'category', data: d.x, axisLabel: { color: '#888', rotate: d.x.length > 6 ? 30 : 0 } },\n"
        "  yAxis: { type: 'value', axisLabel: { color: '#6b7280' }, splitLine: { lineStyle: { color: '#e5e7eb' } } },\n"
        "  series: d.series.map(function(s) {\n"
        "    return { name: s.name, type: 'bar', data: s.values, barMaxWidth: 40 };\n"
        "  }),\n"
        f"  color: {_COLORS}\n"
        "});\n"
    )
    body = (
        '<div id="chart" class="chart-box"><div class="loading">Loading...</div></div>\n'
        + _chart_init_js("chart", data, setup)
    )
    return _html_wrap(title, body)


def _render_pie(title, data):
    """Pie chart: {items: [{name, value}]}"""
    setup = (
        "chart.setOption({\n"
        "  backgroundColor: 'transparent',\n"
        "  tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },\n"
        "  series: [{\n"
        "    type: 'pie', radius: ['35%', '65%'],\n"
        "    label: { color: '#374151', fontSize: 13 },\n"
        "    data: d.items,\n"
        "    emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(26,86,219,0.3)' } }\n"
        "  }],\n"
        f"  color: {_COLORS}\n"
        "});\n"
    )
    body = (
        '<div id="chart" class="chart-box"><div class="loading">Loading...</div></div>\n'
        + _chart_init_js("chart", data, setup)
    )
    return _html_wrap(title, body)


def _render_radar(title, data):
    """Radar chart: {indicators: [{name, max?}] or [str], series: [{name, values}]}"""
    setup = (
        "var indicators = d.indicators.map(function(ind) {\n"
        "  if (typeof ind === 'string') return { name: ind, max: 100 };\n"
        "  return { name: ind.name, max: ind.max || 100 };\n"
        "});\n"
        "chart.setOption({\n"
        "  backgroundColor: 'transparent',\n"
        "  tooltip: {},\n"
        "  legend: { data: d.series.map(function(s){ return s.name; }), textStyle: { color: '#374151' } },\n"
        "  radar: {\n"
        "    indicator: indicators,\n"
        "    axisName: { color: '#374151' },\n"
        "    splitArea: { areaStyle: { color: ['#f8fafc', '#ffffff'] } },\n"
        "    splitLine: { lineStyle: { color: '#e5e7eb' } }\n"
        "  },\n"
        "  series: [{\n"
        "    type: 'radar',\n"
        "    data: d.series.map(function(s) {\n"
        "      return { name: s.name, value: s.values, areaStyle: { opacity: 0.15 } };\n"
        "    })\n"
        "  }],\n"
        f"  color: {_COLORS}\n"
        "});\n"
    )
    body = (
        '<div id="chart" class="chart-box"><div class="loading">Loading...</div></div>\n'
        + _chart_init_js("chart", data, setup)
    )
    return _html_wrap(title, body)


def _render_table(title, data):
    """Table: {columns: [...], rows: [[...]]}"""
    cols = data.get("columns", [])
    rows = data.get("rows", [])
    header = "".join(f"<th>{c}</th>" for c in cols)
    body_rows = ""
    for row in rows:
        cells = "".join(f"<td>{cell}</td>" for cell in row)
        body_rows += f"<tr>{cells}</tr>\n"

    body = (
        '<div style="overflow-x:auto;">\n'
        f'<table>\n<thead><tr>{header}</tr></thead>\n'
        f'<tbody>{body_rows}</tbody>\n</table>\n</div>'
    )
    return _html_wrap(title, body, has_chart=False)


def _render_report(title, data):
    """Composite report: {sections: [{heading, content?, chart?}]}
    chart format: {type: "line"|"bar"|"pie", data: {...}}
    """
    sections_html = ""
    chart_count = 0

    for i, sec in enumerate(data.get("sections", [])):
        heading = sec.get("heading", "")
        content = sec.get("content", "")
        chart = sec.get("chart")

        sections_html += f'<div class="section">\n<h2>{heading}</h2>\n'
        if content:
            for p in content.split("\n"):
                p = p.strip()
                if p:
                    sections_html += f"<p>{p}</p>\n"

        if chart and isinstance(chart, dict):
            chart_id = f"chart_{i}"
            chart_type = chart.get("type", "bar")
            chart_data = chart.get("data", {})

            sections_html += f'<div id="{chart_id}" class="chart-box"><div class="loading">Loading...</div></div>\n'

            if chart_type in ("line", "bar"):
                setup = (
                    "chart.setOption({\n"
                    "  backgroundColor: 'transparent',\n"
                    "  tooltip: { trigger: 'axis' },\n"
                    "  legend: { data: d.series.map(function(s){ return s.name; }), textStyle: { color: '#374151' } },\n"
                    "  grid: { left: 40, right: 20, top: 40, bottom: 30 },\n"
                    "  xAxis: { type: 'category', data: d.x, axisLabel: { color: '#6b7280' } },\n"
                    "  yAxis: { type: 'value', axisLabel: { color: '#6b7280' }, splitLine: { lineStyle: { color: '#e5e7eb' } } },\n"
                    f"  series: d.series.map(function(s) {{ return {{ name: s.name, type: '{chart_type}', data: s.values, smooth: true }}; }}),\n"
                    f"  color: {_COLORS}\n"
                    "});\n"
                )
            elif chart_type == "pie":
                setup = (
                    "chart.setOption({\n"
                    "  backgroundColor: 'transparent',\n"
                    "  tooltip: { trigger: 'item' },\n"
                    "  series: [{ type: 'pie', radius: ['35%','65%'], label: { color: '#374151' }, data: d.items }],\n"
                    f"  color: {_COLORS}\n"
                    "});\n"
                )
            else:
                setup = ""

            if setup:
                sections_html += _chart_init_js(chart_id, chart_data, setup)
                chart_count += 1

        sections_html += "</div>\n"

    return _html_wrap(title, sections_html, has_chart=(chart_count > 0))


# -- Template routing --------------------------------------------------------

_TEMPLATES = {
    "line": _render_line,
    "bar": _render_bar,
    "pie": _render_pie,
    "radar": _render_radar,
    "table": _render_table,
    "report": _render_report,
}


# -- Tool definition ---------------------------------------------------------

@tool("render_page",
      "Generate an interactive visualization page and send it to the user via a link card. "
      "Important: When your reply contains comparison data with 3+ options, trends with 5+ data points, "
      "or distribution with 3+ categories, you MUST call this tool to generate a visualization instead of plain text lists. "
      "Templates: line (trend) / bar (comparison) / pie (proportion) / radar (multi-dimension) / table / report (composite).",
      {"title": {"type": "string", "description": "Page title (also used as the link card title)"},
       "template": {"type": "string", "enum": ["line", "bar", "pie", "radar", "table", "report"],
                     "description": "Template type"},
       "data": {"type": "object", "description": "Data. line/bar: {x:[...], series:[{name,values}]}; "
                "pie: {items:[{name,value}]}; radar: {indicators:[...], series:[{name,values}]}; "
                "table: {columns:[...], rows:[[...]]}; "
                "report: {sections:[{heading, content?, chart?}]}"},
       "desc": {"type": "string", "description": "Card description text (optional, auto-generated if omitted)"}},
      ["title", "template", "data"])
def tool_render_page(args, ctx):
    title = args["title"]
    template = args["template"]
    data = args["data"]
    desc = args.get("desc", "")
    owner_id = ctx.get("owner_id")

    # Piggyback cleanup of expired pages
    cleanup_expired_pages()

    # Validate template
    render_fn = _TEMPLATES.get(template)
    if not render_fn:
        return f"[error] Unknown template: {template}, available: {', '.join(_TEMPLATES.keys())}"

    # Generate HTML
    try:
        html = render_fn(title, data)
    except Exception as e:
        log.error(f"[pages] render error: {e}", exc_info=True)
        return f"[error] Page render failed: {e}"

    # Write file
    page_id = uuid.uuid4().hex[:8]
    filename = f"{page_id}.html"
    filepath = os.path.join(PAGES_DIR, filename)

    if not os.path.isdir(PAGES_DIR):
        os.makedirs(PAGES_DIR, exist_ok=True)

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        log.error(f"[pages] write error: {e}")
        return f"[error] File write failed: {e}"

    # Build URL
    base_url = _get_base_url()
    page_url = f"{base_url}/{filename}"

    # Send link card
    if not desc:
        desc = f"Click to view: {title}"

    if owner_id:
        try:
            result = messaging.send_link(owner_id, title, desc, page_url)
            if result.get("code") != 0:
                log.warning(f"[pages] send_link failed: {result}")
                return f"Page generated: {page_url}\n(Link card send failed: {result.get('msg', '?')})"
        except Exception as e:
            log.warning(f"[pages] send_link error: {e}")
            return f"Page generated: {page_url}\n(Link card send failed: {e})"

    log.info(f"[pages] rendered {template} page: {page_url}")
    return f"Visualization page generated and sent: {page_url}"
