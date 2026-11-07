#!/usr/bin/env python3
"""Collect a single requested market window for analysis by ChatGPT."""
import json
import os
import re
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

from market_brief import CN, collect, edition, timestamp

MAX_ITEMS = 800
MAX_BODY = 200


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
    records = [{"source": item.source, "published_at": item.published.isoformat(),
                "title": item.title, "excerpt": item.body[:MAX_BODY],
                "url": item.url or None, "importance": item.rank} for item in selected]
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
            "news": records, "quotes": quote_records,
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
