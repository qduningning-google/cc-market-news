#!/usr/bin/env python3
"""Render a market-news JSON record as Feishu message cards and send it."""
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

LIMIT = 20 * 1024


def escape_md(text):
    return re.sub(r"([\\\\`*_\[\]])", r"\\\1", html.escape(text, quote=False))


def payload(title, elements):
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": "blue", "title": {"tag": "plain_text", "content": title}},
            "elements": elements,
        },
    }


def markdown(text):
    return {"tag": "div", "text": {"tag": "lark_md", "content": text}}


def encode(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def render(record):
    status = record.get("status")
    articles = record.get("articles")
    if status not in ("news", "no_news") or not isinstance(articles, list):
        raise ValueError("Invalid status or articles")
    if status == "no_news":
        if articles:
            raise ValueError("no_news must have no articles")
        return []
    if not articles or len(articles) > 30:
        raise ValueError("news must contain 1 to 30 articles")
    date = record.get("date")
    if not isinstance(date, str) or len(date) != 10:
        raise ValueError("Invalid date")
    title = "中国资本市场简报  |  " + date
    groups = []
    for index, article in enumerate(articles, 1):
        if not isinstance(article, dict):
            raise ValueError("Article must be an object")
        keys = ("importance", "category", "title", "summary", "significance", "source", "published_at", "url")
        if any(not isinstance(article.get(k), str) or not article[k].strip() for k in keys):
            raise ValueError("Article has missing fields")
        url = urlparse(article["url"])
        if url.scheme != "https" or not url.netloc:
            raise ValueError("Article link must be HTTPS")
        group = [
            markdown("**%02d  %s**\n%s  ·  %s" % (index, escape_md(article["title"]), escape_md(article["importance"]), escape_md(article["category"]))),
            markdown("**要闻**  " + escape_md(article["summary"]) + "\n\n**关注**  " + escape_md(article["significance"])),
            {"tag": "note", "elements": [{"tag": "plain_text", "content": article["source"] + "  ·  " + article["published_at"]}]},
            {"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "阅读原文 ↗"}, "url": article["url"], "type": "default"}]},
        ]
        if len(encode(payload(title, group))) > LIMIT:
            raise ValueError("One article exceeds Feishu's request limit")
        groups.append(group)
    messages, current, count = [], [], 0
    for group in groups:
        candidate = current + ([{"tag": "hr"}] if current else []) + group
        if count == 5 or len(encode(payload(title, candidate))) > LIMIT:
            messages.append(payload(title, current))
            current = []
            count = 0
            candidate = group
        current = candidate
        count += 1
    if current:
        messages.append(payload(title, current))
    return messages


def main():
    record = json.loads(Path("briefs/latest.json").read_text(encoding="utf-8"))
    messages = render(record)
    if not messages:
        print("No relevant news; no message sent.")
        return
    if os.environ.get("DRY_RUN") == "1":
        print("Validated %d message(s); dry run." % len(messages))
        return
    webhook = os.environ.get("FEISHU_WEBHOOK", "")
    parsed = urlparse(webhook)
    if parsed.scheme != "https" or parsed.hostname != "open.feishu.cn" or not parsed.path.startswith("/open-apis/bot/v2/hook/"):
        raise ValueError("FEISHU_WEBHOOK missing or invalid")
    for index, message in enumerate(messages, 1):
        request = urllib.request.Request(webhook, data=encode(message), headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
            if result.get("code", result.get("StatusCode")) != 0:
                raise RuntimeError("Feishu returned a failure code: %s" % result.get("code", result.get("StatusCode")))
            print("Sent message %d of %d." % (index, len(messages)))
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError("Sending failed or timed out; delivery may be uncertain. Check the Feishu group before rerunning.") from exc


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Error: %s" % exc, file=sys.stderr)
        sys.exit(1)
