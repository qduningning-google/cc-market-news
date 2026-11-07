#!/usr/bin/env python3
"""Collect a single requested market window for analysis by ChatGPT."""
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from market_brief import CN, collect, edition, timestamp

MAX_ITEMS = 800
MAX_BODY = 200
MAX_RECAP_ITEMS = 12
MAX_RECAP_BODY = 450
MARKET_SCOPE = re.compile(r"A股|沪深|两市|三市|全市场|港股|恒生|恒指|美股|标普|纳指|纳斯达克|道指|上证|沪指|深证|深成指|创业板|科创50")
RECAP_PATTERNS = {
    "turnover": re.compile(r"成交(?:量|额|总额|金额)?[^，。；]{0,18}\d"),
    "breadth": re.compile(
        r"(?:上涨|下跌|涨跌)(?:个股|股票|家数)[^，。；]{0,18}\d"
        r"|(?:上涨|下跌|收涨|收跌)\s*(?:超|逾|约|达|为)?\s*\d[\d,.]*\s*(?:家|只)"
        r"|\d[\d,.]*\s*(?:多|余)?\s*(?:家|只|股)(?:个股|股票|成分股)?\s*(?:上涨|下跌|收涨|收跌|飘红)"
    ),
    "indices": re.compile(r"(?:沪深300|上证|沪指|深成指|深证成指|创业板|科创50|恒指|恒生|标普|纳指|纳斯达克|道指|道琼斯|罗素2000)[^，。；]{0,25}(?:涨|跌|高开|低开|持平|收平|收报|报\s*\d)"),
    "stocks": re.compile(r"(?:涨停|跌停|[A-Za-z\u4e00-\u9fff](?:收涨|收跌|上涨|下跌|大涨|大跌|涨|跌)(?:超|逾|近)?\s*\d+(?:\.\d+)?\s*[%％])"),
}
CLOSE_RECAP = re.compile(r"收盘|收评|收市|收涨|收跌")


def request_window(request, now=None):
    if not isinstance(request, dict) or request.get("version") != 1:
        raise ValueError("invalid request version")
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", request_id):
        raise ValueError("invalid request_id")
    value = request.get("window_end")
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T(?:08|20):00:00\+08:00", value):
        raise ValueError("window_end must be 08:00 or 20:00 Beijing time")
    end = timestamp(value)
    if request.get("edition") != edition(end):
        raise ValueError("edition does not match window_end")
    now = now or datetime.now(CN)
    if end > now or now - end > timedelta(hours=24):
        raise ValueError("window is in the future or more than 24 hours old")
    return end


def flash_record(item, max_body=MAX_BODY):
    return {"source": item.source, "published_at": item.published.isoformat(),
            "title": item.title, "excerpt": item.body[:max_body],
            "url": item.url or None, "importance": item.rank}


def market_recaps(items):
    """Reserve candidate flashes for each market statistic before the general cap."""
    ordered = sorted(items, key=lambda item: (bool(CLOSE_RECAP.search(item.title)),
                                              item.rank, item.published), reverse=True)
    recaps = {kind: [] for kind in RECAP_PATTERNS}
    for item in ordered:
        excerpt = f"{item.title} {item.body[:MAX_RECAP_BODY]}"
        for kind, pattern in RECAP_PATTERNS.items():
            if len(recaps[kind]) >= MAX_RECAP_ITEMS or not pattern.search(excerpt):
                continue
            if kind == "turnover" and not MARKET_SCOPE.search(excerpt):
                continue
            recaps[kind].append(flash_record(item, MAX_RECAP_BODY))
    return recaps


def build_record(request, key, now=None):
    end = request_window(request, now)
    if not key:
        raise ValueError("X_API_KEY missing")
    flashes, quotes, calendar, warnings, successful = collect(key, end)
    # Exact duplicate removal only: semantic event selection is left to ChatGPT.
    unique = {}
    for item in flashes:
        fingerprint = (item.body, item.published.isoformat())
        if fingerprint not in unique:
            unique[fingerprint] = item
    all_items = sorted(unique.values(), key=lambda item: (item.rank, item.published), reverse=True)
    selected = all_items[:MAX_ITEMS]
    records = [flash_record(item) for item in selected]
    quote_records = {name: {"value": item.value, "at": item.at.isoformat(),
                             "base": item.base, "url": item.url}
                     for name, item in quotes.items()}
    if len(all_items) > MAX_ITEMS:
        warnings.append(f"快讯按重要性和时间截取 {MAX_ITEMS}/{len(all_items)} 条")
    return {"version": 1, "request_id": request["request_id"], "edition": request["edition"],
            "window_start": (end - timedelta(hours=12)).isoformat(),
            "window_end": end.isoformat(), "collected_at": datetime.now(CN).isoformat(),
            "status": "ok" if successful else "error", "successful_news_queries": successful,
            "warnings": list(dict.fromkeys(warnings)), "news_total": len(all_items),
            "news": records, "market_recaps": market_recaps(all_items), "quotes": quote_records,
            "trade_days": [day.isoformat() for day in calendar]}


def main():
    request = json.loads(Path("requests/latest.json").read_text(encoding="utf-8"))
    end = request_window(request)
    try:
        record = build_record(request, os.environ.get("X_API_KEY"))
    except Exception as exc:
        # Persist a bounded error record; never write credentials or remote responses.
        record = {"version": 1, "request_id": request["request_id"], "edition": request["edition"],
                  "window_start": (end - timedelta(hours=12)).isoformat(),
                  "window_end": end.isoformat(), "collected_at": datetime.now(CN).isoformat(),
                  "status": "error", "warnings": ["取数失败：" + type(exc).__name__],
                  "news": [], "quotes": {}}
    target = Path("data") / (request["edition"] + ".json")
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print("Collected", request["edition"], "status=", record["status"], "news=", len(record["news"]))
    if record["status"] != "ok":
        sys.exit(1)


if __name__ == "__main__":
    main()
