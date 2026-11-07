#!/usr/bin/env python3
"""独立生成资本市场早晚报，收集、复盘与推送均使用本任务的状态。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from email.utils import parsedate_to_datetime
import html
import json
import math
import os
from pathlib import Path
import re
import urllib.error
from urllib.parse import quote, urlencode, urlsplit
import urllib.request
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from send_brief import LIMIT, encode, escape_md, markdown, payload

CN = ZoneInfo("Asia/Shanghai")
US = ZoneInfo("America/New_York")
ASHARE = "https://asharehub.com"
SOURCES = {"cls": "财联社", "jin10": "金十", "sina": "新浪"}
FEED = "https://www.federalreserve.gov/feeds/press_all.xml"
CLOCKS = {"am": 8, "pm": 20}
SCHEDULES = {"0 0 * * *": "am", "0 12 * * *": "pm"}
STATE = Path(".market-brief-state")
QUOTES = {
    "恒生指数": "^HSI",
    "标普500": "^GSPC",
    "纳斯达克": "^IXIC",
    "标普500期货": "ES=F",
    "美元指数": "DX-Y.NYB",
    "美国10年期国债收益率": "^TNX",
    "COMEX黄金期货": "GC=F",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def read_url(url, *, data=None, headers=None):
    """凭据只发送给已指定的 HTTPS 服务，不跟随重定向。"""
    request = urllib.request.Request(url, data=data, headers={
        "User-Agent": "Mozilla/5.0 (compatible; MarketBrief/1.0)", **(headers or {})
    })
    opener = urllib.request.build_opener(NoRedirect()) if data or headers else urllib.request.build_opener()
    with opener.open(request, timeout=18) as response:
        return response.read()


def ashare(key, endpoint, **params):
    return json.loads(read_url(f"{ASHARE}{endpoint}?{urlencode(params)}", headers={"X-API-Key": key}))


def timestamp(text):
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return value.astimezone(CN) if value.tzinfo else value.replace(tzinfo=CN)


def edition_end(at=None, schedule=None):
    """计算计划截止时刻。即使运行晚到，也按原来的半日窗口取数。"""
    now = datetime.now(CN)
    if at:
        end = timestamp(at)
        if end.hour not in CLOCKS.values() or end.minute or end.second or end.microsecond:
            raise ValueError("窗口结束时间须为北京时间 08:00 或 20:00")
    elif schedule:
        hour = CLOCKS[SCHEDULES[schedule]]
        end = datetime.combine(now.date(), time(hour), CN)
        if end > now:
            end -= timedelta(days=1)
    else:
        end = max(edition_end(schedule=rule) for rule in SCHEDULES)
    if end > now:
        raise ValueError("窗口结束时间不能晚于当前时间")
    return end


def edition(end):
    return f"{end:%Y-%m-%d}-{'am' if end.hour == 8 else 'pm'}"


def dates_in_window(start, end):
    return sorted({start.date(), (end - timedelta(microseconds=1)).date()})


def clean(text):
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", text)).split())


def valid_link(url):
    try:
        parts = urlsplit(url or "")
        return parts.scheme == "https" and bool(parts.hostname) and not parts.username
    except (ValueError, TypeError):
        return False


def pretty_time(value):
    return f"{value:%m-%d %H:%M}"


@dataclass
class Flash:
    title: str
    body: str
    published: datetime
    source: str
    url: str
    rank: int = 0


def parse_flashes(rows, source, start, end):
    """按发布时间做左闭右开的窗口过滤，防止早晚报重复收录边界消息。"""
    if not isinstance(rows, list):
        raise ValueError("快讯格式异常")
    result = []
    for row in rows:
        try:
            published = timestamp(row["publish_time"])
            body = clean(row["content_cn"])
        except (KeyError, TypeError, ValueError):
            continue
        if not start <= published < end or not body:
            continue
        heading = re.match(r"^【([^】]{2,110})】\s*(.*)", body)
        title, body = heading.groups() if heading else ("", body)
        body = re.sub(r"^(财联社|金十数据)\d{1,2}月\d{1,2}日(电|讯)[，,:：]\s*", "", body)
        title = title or re.split(r"[。；]", body, maxsplit=1)[0][:65]
        url = row.get("url")
        rank = row.get("importance")
        result.append(Flash(title, body or title, published, SOURCES[source],
                            url if valid_link(url) else "", rank if isinstance(rank, int) else 0))
    return result


@dataclass
class Quote:
    value: float
    at: datetime
    url: str
    base: float = None

    @property
    def move(self):
        return (self.value / self.base - 1) * 100 if self.base else None


def chart(symbol, start, end):
    """选截止时刻前已结束的半小时线，行情延迟时保留实际时间。"""
    encoded = quote(symbol, safe="")
    params = urlencode({"period1": int((start - timedelta(days=7)).timestamp()),
                        "period2": int(end.timestamp()), "interval": "30m"})
    for host in ("query1", "query2"):
        try:
            data = json.loads(read_url(f"https://{host}.finance.yahoo.com/v8/finance/chart/{encoded}?{params}"))
            break
        except urllib.error.HTTPError as error:
            if host == "query2" or error.code not in (403, 429):
                raise
    chart_data = data["chart"]["result"][0]
    meta = chart_data["meta"]
    closes = chart_data["indicators"]["quote"][0]["close"]
    points = [
        (datetime.fromtimestamp(seconds if seconds == meta.get("regularMarketTime") else seconds + 1800, CN), value)
        for seconds, value in zip(chart_data.get("timestamp", []), closes)
        if isinstance(value, (int, float)) and math.isfinite(value)
    ]
    points = [(at, value) for at, value in points if at <= end]
    if not points:
        raise ValueError("窗口前无可用行情")
    at, value = points[-1]
    # 只有窗口内有报价更新才计算区间变化。休市沿用最近价。
    base = next((p for ts, p in reversed(points) if ts <= start), None) if at > start else None
    return Quote(value, at, f"https://finance.yahoo.com/quote/{encoded}/", base)


def spot_gold(end):
    url = "https://api.gold-api.com/price/XAU"
    data = json.loads(read_url(url))
    at = timestamp(data["updatedAt"])
    value = float(data["price"])
    if not end - timedelta(hours=12) < at <= end + timedelta(minutes=5) or not math.isfinite(value) or value <= 0:
        raise ValueError("没有接近本期截止时刻的现货报价")
    return Quote(value, at, "https://gold-api.com/")


def fed_releases(start, end):
    entries = ElementTree.fromstring(read_url(FEED)).findall("./channel/item")
    releases = []
    for entry in entries:
        at = parsedate_to_datetime(entry.findtext("pubDate", "")).astimezone(CN)
        title = entry.findtext("title", "")
        url = entry.findtext("link", "")
        if start <= at < end and valid_link(url) and re.search(r"FOMC|monetary policy|interest rate", title, re.I):
            releases.append(Flash("美联储发布政策文件", title, at, "美联储", url, 3))
    return releases


def collect(key, end):
    """分别拉取三源和必要行情。单源失败保留已取得的数据并标记缺口。"""
    start = end - timedelta(hours=12)
    tasks = {}
    warnings = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for day in dates_in_window(start, end):
            for source in SOURCES:
                tasks[pool.submit(ashare, key, "/v2/news/flash", source=source,
                                  start_date=day.strftime("%Y%m%d"), end_date=day.strftime("%Y%m%d"))] = ("news", source)
        tasks[pool.submit(ashare, key, "/v2/reference/trade-calendar",
                          start_date=(end.date() - timedelta(days=10)).strftime("%Y%m%d"),
                          end_date=(end.date() + timedelta(days=10)).strftime("%Y%m%d"))] = ("calendar", "交易日历")
        for name, symbol in QUOTES.items():
            tasks[pool.submit(chart, symbol, start, end)] = ("quote", name)
        tasks[pool.submit(spot_gold, end)] = ("spot", "现货黄金")
        tasks[pool.submit(fed_releases, start, end)] = ("official", "美联储公告")
        flashes, market = [], {}
        calendar, failed_news, successful = [], set(), 0
        for future, (kind, name) in tasks.items():
            try:
                data = future.result()
                if kind == "news":
                    flashes.extend(parse_flashes(data, name, start, end))
                    successful += 1
                    if len(data) >= 6000:
                        warnings.append(f"{SOURCES[name]} 单日返回疑似达到上限")
                elif kind == "calendar":
                    if not isinstance(data, list) or not data:
                        raise ValueError("交易日历为空")
                    calendar = [datetime.strptime(row["cal_date"], "%Y%m%d").date()
                                for row in data if row["is_open"] == 1]
                elif kind == "official":
                    flashes.extend(data)
                else:
                    market[name] = data
            except (OSError, KeyError, TypeError, ValueError, ElementTree.ParseError) as error:
                if kind == "news":
                    failed_news.add(SOURCES[name])
                elif kind != "spot":
                    warnings.append(f"{name}暂缺")
                # 日志只记录错误类型，避免打印含密钥的请求地址或响应正文。
                print(f"{name} 获取异常（{type(error).__name__}）", flush=True)
        if failed_news:
            warnings.insert(0, "快讯来源部分缺失：" + "、".join(sorted(failed_news)))
        if not successful:
            warnings.insert(0, "三路快讯均不可用，新闻覆盖不足")
        if "现货黄金" not in market and "COMEX黄金期货" in market:
            warnings.append("现货黄金缺少有效报价，以 COMEX 黄金期货作参考")
        if calendar:
            try:
                trade = max(day for day in calendar if day < end.date() or (day == end.date() and end.hour >= 15))
                rows = ashare(key, "/v2/indices/daily", symbol="000300.SH", trade_date=trade.strftime("%Y%m%d"))
                row = next(row for row in rows if row["trade_date"] == trade.strftime("%Y%m%d"))
                market["沪深300"] = Quote(float(row["close"]), datetime.combine(trade, time(15), CN),
                                         "https://asharehub.com/zh/docs/indices", float(row["pre_close"]))
            except (OSError, KeyError, TypeError, ValueError, StopIteration):
                warnings.append("沪深300收盘价暂缺")
    return flashes, market, calendar, warnings, successful


# 同类事件只保留一条，优先具体数据、实际行动和可回溯的来源。
TOPICS = [
    ("美国就业", 10, r"(美国|美联储).{0,18}(非农就业|失业率)|^(季调后非农|非农新增就业)",
     "就业意外会同时影响增长和利率预期，结合美元与美债确认。"),
    ("美国通胀", 10, r"美国.{0,20}(消费者物价指数|CPI|核心PCE|核心通胀)",
     "通胀变化主要影响美联储利率路径，以及美元和黄金。"),
    ("美联储", 10, r"美联储.{0,20}(宣布.{0,8}(利率|加息|降息)|利率决议|发布政策文件)|^FOMC",
     "区分正式决议、官员表态和市场预期。"),
    ("中国稳市", 10, r"资本市场稳市机制|中央汇金.{0,18}(增持|减持|ETF)|股市.{0,12}平准基金",
     "政策方向影响 A 股风险偏好，持续性取决于细则和资金实际行动。"),
    ("资金入市", 9, r"互换便利.{0,55}(股票买入|买入股票|首批股票投资)|首批股票买入|股票回购增持再贷款.{0,18}(下调|上调|投放)",
     "区分工具额度、融资到位和股票买入，观察后续资金是否持续。"),
    ("中国货币政策", 10, r"(中国人民银行|中国央行|人民银行|^央行).{0,22}(降准|降息|调整.{0,12}(利率|准备金率))",
     "影响国内流动性和融资成本，结合人民币及政策执行观察。"),
    ("中美经贸", 8, r"(中美|何立峰).{0,40}(经贸磋商|贸易磋商|视频通话|关税|贸易协议)",
     "沟通可影响情绪，企业盈利仍看关税细则和订单变化。"),
    ("中国增长", 8, r"中国.{0,15}(制造业PMI|GDP|进出口总值|出口同比|工业增加值)|国家统计局.{0,25}(PMI|工业增加值|零售总额)",
     "观察内需或外需是否改变，避免用单月数据确认趋势。"),
    ("交易规则", 7, r"(证监会|交易所|深交所|上交所).{0,22}(融券|量化交易|指数编制方案|印花税|交易规则)",
     "留意适用范围和生效时间，区分市场影响与单一标的调整。"),
    ("美股盘面", 6, r"美股收盘|纳指收盘|标普500指数.{0,20}(收涨|收跌)",
     "观察涨跌是否扩散到更多行业，判断反弹的广度。"),
    ("海外重大风险", 8, r"(原油|航运|关税|出口管制).{0,12}(禁运|封锁|正式生效|宣布加征)",
     "观察实际覆盖范围及油价、美元和股指期货的反应。"),
    ("能源和黄金", 6, r"现货黄金.{0,18}(跌逾|涨逾|下跌|上涨|新高|新低)|(?:WTI|布伦特原油).{0,12}(涨超|跌超)",
     "价格异动需要和美元、利率或供给事件一起确认。"),
]


def select_events(flashes):
    """挑选影响预期的事件，同主题保留证据最完整的一条。"""
    selected = {}
    for item in flashes:
        if re.search(r"早间新闻精选|晚间新闻精选|新闻汇总|今日重点关注|今日待公布|盘前要闻|数据与事件|^国内新闻|^国际新闻|财报日历", item.title):
            continue
        text = item.title + " " + item.body[:180]
        for label, weight, pattern, impact in TOPICS:
            if not re.search(pattern, item.title, re.I):
                continue
            if label in ("美国就业", "美国通胀") and (
                    re.search(r"将公布|公布前|前瞻|料将", item.title)
                    or not re.search(r"增加|新增|减少|下降|上涨|下跌|录得|为\s*[\d.]|实际|持平|超预期", text)):
                continue
            value = weight + min(item.rank, 3) / 4 + bool(item.url) / 2
            if label == "美国就业" and "非农" in item.title and re.search(r"增加|新增|减少|实际", text):
                value += 2
            if re.search(r"预计|将公布|前瞻|策略师|押注", item.title):
                value -= 3
            if label not in selected or value > selected[label][0]:
                selected[label] = (value, item, label, impact)
            break
    return sorted(selected.values(), key=lambda item: (item[0], item[1].published), reverse=True)[:5]


def observed(quote, end):
    return quote.move if quote and quote.at > end - timedelta(hours=12) else None


def percent_move(move):
    if abs(move) < 0.005:
        return "→ 0.00%"
    return f"{'↑' if move > 0 else '↓'} {abs(move):.2f}%"


def direction(move, threshold=0.25):
    if move is None:
        return "待确认"
    if move > threshold:
        return "偏强"
    if move < -threshold:
        return "偏弱"
    return "震荡"


def assessment(market, events, calendar, end):
    """用已观察到的价格和已披露事件生成条件展望。"""
    start = end - timedelta(hours=12)
    labels = {event[2] for event in events}
    us = observed(market.get("标普500"), end)
    china = observed(market.get("沪深300"), end)
    dollar = observed(market.get("美元指数"), end)
    rates = market.get("美国10年期国债收益率")
    rate_change = rates.value - rates.base if rates and rates.base and rates.at > start else None
    votes = [direction(move, 0.2) for move in (us, china, observed(market.get("恒生指数"), end))
             if move is not None]
    if ("偏强" in votes and "偏弱" in votes) or (
            us is not None and us > 0.2 and dollar is not None and dollar > 0.15
            and rate_change is not None and rate_change > 0.015):
        risk = "分化"
    elif votes.count("偏强") >= 2:
        risk = "改善"
    elif votes.count("偏弱") >= 2:
        risk = "降温"
    else:
        risk = "待确认"
    evidence = []
    for name, move in (("沪深300", china), ("标普500", us), ("美元指数", dollar)):
        if move is not None:
            evidence.append(f"{name} {percent_move(move)}")
    if rate_change is not None:
        evidence.append(f"美债10年 {rate_change * 100:+.1f} 基点")
    domestic = ("国内稳市或流动性政策有新增信息，关注后续执行。" if labels & {"中国稳市", "资金入市", "中国货币政策"}
                else "中国宏观主线等待新增数据。")
    jobs = next((item for _, item, label, _ in events if label == "美国就业"), None)
    if jobs and re.search(r"远超预期|大幅超预期|高于预期", jobs.title):
        overseas = "美国就业强于预期，增长有韧性，利率维持高位的风险上升。"
    else:
        overseas = ("美国宏观预期有新线索，关注利率定价。" if labels & {"美国就业", "美国通胀", "美联储"}
                    else "美国宏观预期等待新数据。")
    if not calendar or end.date() not in calendar:
        china_view = "休市／待下一交易日"
    elif labels & {"中国稳市", "资金入市"} and (us or 0) >= 0:
        china_view = "偏强，等待盘面确认"
    elif us is not None and us < -0.7 and not labels & {"中国稳市", "资金入市", "中国货币政策"}:
        china_view = "偏弱，等待盘面确认"
    else:
        china_view = "震荡观察"
    gold_view = "待确认"
    if dollar is not None and rate_change is not None:
        if dollar > 0.15 and rate_change > 0.015:
            gold_view = "偏弱"
        elif dollar < -0.15 and rate_change < -0.015:
            gold_view = "偏强"
        else:
            gold_view = "震荡"
    futures = market.get("标普500期货")
    futures_view = direction(observed(futures, end))
    if end.weekday() >= 5:
        futures_view = gold_view = "休市／待下一交易时段"
    hong_kong = "休市／待下一交易时段" if end.weekday() >= 5 else "观察期货及港股开盘"
    return risk, "；".join(evidence), domestic + overseas, {
        "沪深300": china_view, "恒生指数": hong_kong, "标普500期货": futures_view, "黄金": gold_view,
    }


def quote_line(name, item, end):
    if not item:
        return f"{name}  暂缺"
    if name == "美国10年期国债收益率":
        value = f"{item.value:.2f}%"
        movement = f" · {(item.value - item.base) * 100:+.1f}bp" if item.base else ""
    else:
        value = f"{item.value:,.2f}"
        movement = f" · {percent_move(item.move)}" if item.move is not None else ""
    past = " · 最近交易报价" if item.at <= end - timedelta(hours=12) else ""
    if item.at > end:
        past = " · 截止后参考报价"
    return f"{name}  **{value}{movement}**  · {pretty_time(item.at)}{past}"


def replay(previous, market, end):
    if not previous:
        return "本日早报记录暂缺。"
    parts = []
    for label, stance in previous["views"].items():
        if label == "黄金":
            spot = market.get("现货黄金")
            if spot and previous.get("spot") and timestamp(previous["spot"]["at"]) < spot.at <= end:
                move = (spot.value / previous["spot"]["value"] - 1) * 100
            else:
                move = observed(market.get("COMEX黄金期货"), end)
                label = "黄金期货"
        else:
            move = observed(market.get(label), end)
        if move is not None:
            parts.append(f"{label} {stance} → {percent_move(move)}")
    return "；".join(parts) or "已读取早报，当前窗口无可比报价。"


def make_brief(end, flashes, market, calendar, warnings, successful, previous=None):
    """复用仓库已有的飞书卡片组件，早报蓝色、晚报紫色。"""
    start = end - timedelta(hours=12)
    events = select_events(flashes)
    risk, evidence, macro, views = assessment(market, events, calendar, end)
    warning = bool(warnings)
    risk_color = {"分化": "orange", "改善": "green", "降温": "red", "待确认": "orange"}[risk]
    top = f"{pretty_time(start)} – {pretty_time(end)} · 北京时间\n"
    top += f"🧭 **风险偏好**　<font color='{risk_color}'>{risk}</font>\n"
    top += escape_md(evidence or "当期价格信号有限，等下一交易时段确认。")
    quote_names = ["沪深300", "恒生指数", "标普500", "纳斯达克", "标普500期货",
                   "现货黄金" if "现货黄金" in market else "COMEX黄金期货",
                   "美元指数", "美国10年期国债收益率"]
    elements = [markdown(top), {"tag": "hr"},
                markdown("📊 **市场快照**\n" + "\n".join(quote_line(name, market.get(name), end)
                                                          for name in quote_names)
                         + "\n[沪深300来源](https://asharehub.com/zh/docs/indices)"
                           " · [国际行情](https://finance.yahoo.com/markets/)"),
                {"tag": "hr"}, markdown("📰 **关键资讯**")]
    if not events:
        empty = "本窗口暂无达到筛选阈值的新事件。" if successful else "三路快讯不可用，资讯判断暂缺。"
        elements.append(markdown(empty))
    for index, (_, item, label, impact) in enumerate(events, 1):
        heading = escape_md(item.title[:85])
        summary = "" if item.title == item.body else escape_md(item.body[:145] + ("…" if len(item.body) > 145 else ""))
        source = f"{item.source} · {pretty_time(item.published)}"
        text = f"**{index:02d}｜{heading}**\n"
        text += (summary + "\n") if summary else ""
        text += f"**影响** {escape_md(impact)}\n{escape_md(source)}"
        elements.append(markdown(text))
        if item.url:
            elements.append({"tag": "action", "actions": [{"tag": "button", "type": "default",
                             "text": {"tag": "plain_text", "content": "阅读原文 ↗"}, "url": item.url}]})
    outlook = "🌐 **宏观**　" + escape_md(macro)
    if end.hour == 8:
        outlook += "\n🔭 **08:00–20:00 展望**　" + escape_md("；".join(f"{k} {v}" for k, v in views.items()))
        outlook += "\n**改判信号**　A 股看上涨家数与权重同步性，黄金看美元和美债收益率是否反向。"
    else:
        outlook += "\n🔁 **早报复盘**　" + escape_md(replay(previous, market, end))
        outlook += "\n🔭 **隔夜观察**　" + escape_md(
            f"美股期货 {views['标普500期货']}，黄金 {views['黄金']}。观察美股开盘后涨跌广度及美元、利率方向。")
    elements.extend([{"tag": "hr"}, markdown(outlook)])
    if warning:
        elements.append(markdown("⚠️ **数据说明**　" + escape_md("；".join(dict.fromkeys(warnings)))))
    if calendar and end.date() not in calendar:
        following = next((day for day in sorted(calendar) if day > end.date()), None)
        elements.append(markdown(f"📅 A 股休市，下一交易日 {following:%m-%d}。" if following else "📅 A 股休市。"))
    label = "早报" if end.hour == 8 else "晚报"
    message = payload(f"资本市场{label}｜{end:%m-%d}", elements)
    message["card"]["header"]["template"] = "blue" if end.hour == 8 else "purple"
    if len(encode(message)) > LIMIT:
        raise ValueError("卡片超过飞书大小限制")
    spot = market.get("现货黄金")
    state = {"views": views, "spot": {"value": spot.value, "at": spot.at.isoformat()} if spot and spot.at <= end else None}
    return message, state


def deliver(message, webhook):
    """只发送一张卡片。结果不明确时不盲目重发。"""
    parts = urlsplit(webhook)
    if (parts.scheme != "https" or parts.netloc != "open.feishu.cn"
            or not re.fullmatch(r"/open-apis/bot/v2/hook/[0-9a-fA-F-]+", parts.path)
            or parts.query or parts.fragment):
        raise ValueError("NEWS_FEISHU 缺失或 Webhook 格式不符")
    try:
        result = json.loads(read_url(webhook, data=encode(message),
                                     headers={"Content-Type": "application/json; charset=utf-8"}))
    except (OSError, ValueError):
        raise RuntimeError("飞书请求失败或结果不确定，重跑前先检查群消息") from None
    if result.get("code", result.get("StatusCode")) != 0:
        raise RuntimeError("飞书拒绝了消息，请检查机器人配置")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--at", help="指定北京时间窗口结束点，用于复跑")
    parser.add_argument("--schedule", choices=SCHEDULES)
    parser.add_argument("--resolve", action="store_true")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--output", type=Path, help="将预览卡片写入指定文件")
    args = parser.parse_args()
    end = edition_end(args.at, args.schedule)
    if args.resolve:
        print(f"at={end.isoformat()}")
        print(f"edition={edition(end)}")
        print(f"morning={end:%Y-%m-%d}-am")
        return
    key = os.environ.get("X_API_KEY")
    if not key:
        raise ValueError("缺少 GitHub Actions Secret X_API_KEY")
    current_file = STATE / f"{edition(end)}.json"
    if args.send and current_file.exists():
        print("该时段已发送，跳过重复推送")
        return
    previous_file = STATE / f"{end:%Y-%m-%d}-am.json"
    previous = json.loads(previous_file.read_text()) if end.hour == 20 and previous_file.is_file() else None
    data = collect(key, end)
    message, state = make_brief(end, *data, previous=previous)
    if args.output:
        args.output.write_bytes(encode(message))
    print(f"{edition(end)} 生成卡片，筛选窗口内资讯 {len(data[0])} 条；{len(encode(message))} 字节")
    if args.send:
        deliver(message, os.environ.get("NEWS_FEISHU", ""))
        STATE.mkdir(exist_ok=True)
        current_file.write_text(json.dumps(state, ensure_ascii=False))
        print("飞书返回发送成功")
    if not data[-1]:
        raise SystemExit("所有快讯请求失败")



# One-off, AI-reviewed 24-hour test; this branch is not used by the scheduled job.
def main():
    import sys
    if "--resolve" in sys.argv:
        print("at=" + datetime.now(CN).isoformat())
        print("edition=ai-test-send")
        print("morning=ai-test-send")
        return
    if "--send" not in sys.argv:
        raise SystemExit("Test send must be explicitly selected")
    text = "🧭 **风险偏好：分化。** 美股科技股反弹，但强就业推动美元、美债收益率及加息预期上行；中国增长方向仍需更多数据确认。\n\n📊 **已发生的行情**（北京时间）\n美股11月7日05:00收盘：标普500 **+0.31%**，纳指 **+1.09%**，道指 **−0.46%**；费城半导体指数 **+4.2%**。美元指数约 **102.26**；美债10年期收益率约 **4.86%**（均为11月6日美盘参考）。现货黄金 **4,074.36美元/盎司**（11月7日02:54）。[美股收盘](https://www.cls.cn/detail/2527950) · [美元与美债](https://www.reuters.com/world/asia-pacific/dollar-holds-yen-gains-equity-rout-saps-risk-appetite-ahead-payrolls-2026-11-06/) · [黄金](https://www.reuters.com/world/india/gold-eases-inflation-worries-higher-oil-us-payrolls-data-focus-2026-11-06/)\n\n📰 **重点事实与影响**\n1. **美国非农明显超预期**（11月6日21:30）：10月新增 **16.5万人**，预期6.5万人；失业率 **4.1%**，平均时薪同比 **3.1%**。就业韧性缓解增长担忧，但工资压力强化利率高位预期。[美国劳工统计局](https://www.bls.gov/news.release/archives/empsit_11062026.htm)\n2. **美国消费者情绪回升、通胀预期下降**（11月6日23:00）：密歇根大学11月信心指数初值 **51.0**，前值48.0；一年期通胀预期 **4.3%**，前值4.7%。与强就业形成分化信号，仍需CPI确认。[密歇根大学](https://www.sca.isr.umich.edu/)\n3. **中美经贸负责人通话**（11月7日08:02报道）：双方同意继续发挥磋商机制、管控分歧；中方对近期限制措施表达关切。沟通有助缓和预期，但尚未公布具体政策调整。[新华社](https://www.xinhuanet.com/politics/leaders/20261107/ebbfb492652949189aee6a26a748bf1a/c.html)\n4. **中国外储小幅回升**（11月7日10:01报道）：10月末外储 **3.4052万亿美元**，环比增加49亿美元；官方称受汇率折算及资产价格变化影响，不能单凭此判断资金流向。[国家外汇局](https://www.safe.gov.cn/safe/2026/1106/28067.html)\n5. **霍尔木兹海峡油轮事件**（11月7日18:57报道）：据伊朗海军声明，一艘油轮触雷起火停航。地缘风险可能影响下周油价与通胀预期；影响程度待独立核实及开盘价格确认。[新华社](https://www.xinhuanet.com/world/20261107/4f456cae06264450a189ac9f96ed26ce/c.html)\n\n🔭 **未来判断**：周末A股、港股及美股休市，展望顺延至下一交易时段。关注油价、美元及美债收益率是否同时上行；若科技反弹扩散且收益率回落，风险偏好可改善。美股期货最新有效报价暂缺。A股和港股11月6日收盘分别为上证 **−1.10%**、恒指 **−1.27%**（窗口外参考）。[A股](https://stock.cngold.org/c/2026-11-06/c10864459.html) · [港股](https://finance.eastmoney.com/a/202611063924832341.html)\n\n数据：本次云端从AShareHub三源获取，按事件去重并以官方及原始报道交叉核对；分析由ChatGPT完成。"
    card = payload("资本市场24小时测试简报｜11月7日", [markdown(text)])
    if len(encode(card)) > LIMIT:
        raise ValueError("Card too large")
    deliver(card, os.environ.get("NEWS_FEISHU", ""))
    print("飞书返回发送成功")

if __name__ == "__main__":
    main()

