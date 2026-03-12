"""Research tool CLI dispatcher for Claude Code sub-agent.

Usage:
    .venv/bin/python cli/tool_runner.py <tool> '<json_args>'

Examples:
    .venv/bin/python cli/tool_runner.py naver_volume '["ERP 개발", "앱 개발"]'
    .venv/bin/python cli/tool_runner.py autocomplete '"ERP 외주"'
    .venv/bin/python cli/tool_runner.py naver_search '["ERP 개발", 5]'
    .venv/bin/python cli/tool_runner.py naver_trend '["앱 개발", "ERP"]'
    .venv/bin/python cli/tool_runner.py google_trend '["앱 개발"]'
    .venv/bin/python cli/tool_runner.py geo_chatgpt '"ERP 외주 개발 업체 추천"'
    .venv/bin/python cli/tool_runner.py geo_claude '"ERP 외주 개발 업체 추천"'
    .venv/bin/python cli/tool_runner.py geo_gemini '"ERP 외주 개발 업체 추천"'
"""

import asyncio
import json
import sys
from pathlib import Path

# Add project root to sys.path so core.tools.* can be imported
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv

load_dotenv()

# (module, function, call_mode)
# "single"     → fn(json_value)          — for functions taking one arg (list or str)
# "positional" → fn(*json_array)         — for functions taking multiple positional args
TOOLS: dict[str, tuple[str, str, str]] = {
    "naver_volume": ("core.tools.naver_searchad", "naver_keyword_volume", "single"),
    "naver_trend": ("core.tools.naver_datalab", "naver_keyword_trend", "single"),
    "naver_search": ("core.tools.naver_search", "naver_blog_search", "positional"),
    "google_trend": ("core.tools.google_trends", "google_keyword_trend", "single"),
    "autocomplete": ("core.tools.autocomplete", "search_suggestions", "single"),
    # Web crawling / SERP
    "web_fetch": ("core.tools.web_fetch", "web_fetch", "single"),
    "naver_serp": ("core.tools.naver_serp", "naver_serp_features", "single"),
    # GEO citation tools
    "geo_chatgpt": ("core.tools.ai_search", "ai_search", "single"),
    "geo_claude": ("core.tools.claude_search", "claude_search", "single"),
    "geo_gemini": ("core.tools.gemini_search", "gemini_search", "single"),
}


def _usage() -> str:
    tools = ", ".join(sorted(TOOLS))
    return f"Usage: tool_runner.py <tool> '<json_args>'\nAvailable tools: {tools}"


def main() -> None:
    if len(sys.argv) < 2:
        print(_usage(), file=sys.stderr)
        sys.exit(1)

    name = sys.argv[1]
    if name not in TOOLS:
        print(f"Unknown tool: {name}\n{_usage()}", file=sys.stderr)
        sys.exit(1)

    raw = json.loads(sys.argv[2]) if len(sys.argv) > 2 else []

    module_path, func_name, call_mode = TOOLS[name]
    mod = __import__(module_path, fromlist=[func_name])
    fn = getattr(mod, func_name)

    if call_mode == "positional" and isinstance(raw, list):
        result = asyncio.run(fn(*raw))
    else:
        result = asyncio.run(fn(raw))

    # Functions return JSON strings; print directly
    if isinstance(result, str):
        print(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
