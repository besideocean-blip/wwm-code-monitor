#!/usr/bin/env python3
"""Monitor the WWM HMT official news archive using only Python's standard library.

Modes: scan (baseline on first run), test (one existing article, no state writes),
preview (read-only, no credentials required). State lives in a dedicated Issue.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

SOURCE_URL = "https://www.wherewindsmeetgame.com/hmt/news/"
SOURCE_HOST = "www.wherewindsmeetgame.com"
STATE_TITLE = "[WWM HMT Official News] Monitor state"
STATE_MARKER = "<!-- wwm-hmt-official-news-monitor-state:v1 -->"
USER_AGENT = "Mozilla/5.0 (compatible; WWM-HMT-Official-NewsMonitor/1.0)"
MAX_PAGES = 50
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}
FIELD_CLASSES = {"news_tit": "title", "news_text": "summary",
                 "date_day": "day", "date_year": "year"}
ID_PATTERN = re.compile(r"[0-9a-f]{32}")


class MonitorError(Exception):
    """An actionable error that can be printed without leaking a token."""


def clean_text(value: str) -> str:
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    return " ".join(value.split())


def official_url(href: str, base: str, *, article: bool) -> str:
    parts = urllib.parse.urlsplit(urllib.parse.urljoin(base, href))
    pattern = (r"/hmt/news/(?:[A-Za-z0-9_-]+/)+[^/]+\.html" if article else
               r"/hmt/news/(?:index(?:_\d+)?\.html)?")
    is_campaign = article and bool(re.fullmatch(
        r"/jm/activity/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/?", parts.path))
    if (parts.scheme != "https" or parts.netloc != SOURCE_HOST or
            not (re.fullmatch(pattern, parts.path) or is_campaign)):
        raise MonitorError("官網列表出現未支援的公告連結，已停止並保留紀錄。")
    query = ""
    if is_campaign:
        # HMT news cards also link to official campaigns. Preserve the language
        # parameter, but exclude marketing tracking from the deduplication key.
        values = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        if values.get("lang") != ["zh-TW"]:
            raise MonitorError("官網活動連結缺少台港澳語系，已停止並保留紀錄。")
        query = "lang=zh-TW"
    return urllib.parse.urlunsplit(("https", SOURCE_HOST, parts.path, query, ""))


@dataclass(frozen=True)
class Article:
    url: str
    title: str
    summary: str
    published: str  # The official list's date; not the activity start date.

    @property
    def key(self) -> str:
        return hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:32]


class NewsListParser(HTMLParser):
    """Extract only news cards within ul.news_list, never the global menu."""

    def __init__(self, page_url: str):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.stack: list[tuple[str, set[str]]] = []
        self.current: dict[str, Any] | None = None
        self.anchor_depth = 0
        self.articles: list[Article] = []
        self.next_url: str | None = None
        self.total_pages: int | None = None
        self.saw_list = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "ul" and "news_list" in classes:
            self.saw_list = True
        if tag == "a" and "next-btn" in classes:
            self.next_url = official_url(values.get("href") or "", self.page_url,
                                         article=False)
        if tag == "a" and "jump-btn" in classes:
            try:
                self.total_pages = int(values.get("data-all") or "")
            except ValueError:
                raise MonitorError("官網分頁數字無法解析；請檢查網站版型。") from None
        inside_list = any(t == "ul" and "news_list" in c for t, c in self.stack)
        if tag == "a" and "news" in classes and inside_list:
            if self.current is not None:
                raise MonitorError("官網公告卡片結構異常，未修改去重紀錄。")
            self.current = {
                "url": official_url(values.get("href") or "", self.page_url,
                                    article=True),
                "fallback_title": values.get("title") or "",
                "title": [], "summary": [], "day": [], "year": [],
            }
            self.anchor_depth = len(self.stack) + 1
        if tag not in VOID_TAGS:
            self.stack.append((tag, classes))
        elif tag == "br":
            self.handle_data(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.current is None:
            return
        for tag, classes in reversed(self.stack):
            if tag in {"script", "style"}:
                return
            for cls, field in FIELD_CLASSES.items():
                if cls in classes:
                    self.current[field].append(data)
                    return

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_TAGS:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] != tag:
                continue
            if self.current is not None and i + 1 == self.anchor_depth:
                self.finish_article()
            del self.stack[i:]
            return

    def finish_article(self) -> None:
        assert self.current is not None
        item = self.current
        title = clean_text("".join(item["title"])) or clean_text(item["fallback_title"])
        day = clean_text("".join(item["day"]))
        year = clean_text("".join(item["year"]))
        if not title or not re.fullmatch(r"\d{1,2}[./-]\d{1,2}", day):
            raise MonitorError("官網公告標題或日期無法解析，未修改去重紀錄。")
        try:
            month, dom = re.split(r"[./-]", day)
            published = date(int(year), int(month), int(dom)).isoformat()
        except ValueError:
            raise MonitorError("官網公告日期無法解析，未修改去重紀錄。") from None
        self.articles.append(Article(item["url"], title,
                                     clean_text("".join(item["summary"])), published))
        self.current = None

    def validate(self) -> None:
        if not self.saw_list or not self.articles or self.current is not None:
            raise MonitorError("官網未解析出完整公告；可能暫時異常或版型改變。紀錄保留。")
        if self.total_pages is None or not 1 <= self.total_pages <= MAX_PAGES:
            raise MonitorError("官網分頁結構異常或超出安全上限，未修改去重紀錄。")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # Do not forward authentication or webhook tokens to a redirect destination.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPClient:
    def __init__(self):
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, url: str, service: str, *, method: str = "GET",
                payload: Any = None, headers: dict[str, str] | None = None) -> bytes:
        request_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        request_headers.update(headers or {})
        body = None
        if payload is not None:
            request_headers["Content-Type"] = "application/json; charset=utf-8"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        # A GET or identical Issue PATCH is safe to retry. An ambiguous POST
        # (Discord message or Issue creation) must not be blindly repeated.
        idempotent = method in {"GET", "PATCH"}
        for attempt in range(3):
            req = urllib.request.Request(url, data=body, method=method,
                                         headers=request_headers)
            try:
                with self.opener.open(req, timeout=20) as response:
                    result = response.read(MAX_RESPONSE_BYTES + 1)
                if len(result) > MAX_RESPONSE_BYTES:
                    raise MonitorError(f"{service} 回應過大，已停止。")
                return result
            except urllib.error.HTTPError as exc:
                status = exc.code
                error_body = exc.read(4096)
                retry_after = exc.headers.get("Retry-After", "")
                exc.close()
                # A 429 explicitly rejects the operation and can be retried.
                retryable = status == 429 or (idempotent and status in {500, 502, 503, 504})
                delay = float(2 ** attempt)
                if status == 429:
                    try:
                        data = json.loads(error_body)
                        delay = float(data.get("retry_after", retry_after or delay))
                    except (ValueError, TypeError, AttributeError):
                        try:
                            delay = float(retry_after)
                        except ValueError:
                            pass
                if retryable and attempt < 2 and 0 <= delay <= 30:
                    time.sleep(max(1, delay))
                    continue
                hints = {
                    ("GitHub", 403): "請確認工作流程具有 issues: write 權限。",
                    ("GitHub", 404): "請確認專案名稱正確且已開啟 Issues。",
                    ("GitHub", 410): "請確認專案已開啟 Issues。",
                    ("Discord", 401): "請確認 Webhook 網址有效。",
                    ("Discord", 403): "請確認 Webhook 所在頻道及權限。",
                    ("Discord", 404): "Webhook 可能已刪除，請更新 Secret。",
                    ("Discord", 429): "已達發送速率限制，保留狀態待下次重試。",
                }
                hint = hints.get((service, status), "已停止並保留已儲存的紀錄。")
                raise MonitorError(f"{service} HTTP {status}。{hint}") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if idempotent and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                extra = "POST 的送出結果不確定；請查看紀錄。" if not idempotent else "下次排程會再嘗試。"
                raise MonitorError(f"{service} 連線失敗或逾時。{extra}") from None
        raise MonitorError(f"{service} 連線失敗。")

    def json(self, url: str, service: str, **kwargs) -> Any:
        raw = self.request(url, service, **kwargs)
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise MonitorError(f"{service} 未回傳有效 JSON，已停止。") from None


def fetch_news(http: HTTPClient, *, all_pages: bool = True) -> list[Article]:
    url: str | None = SOURCE_URL
    visited: set[str] = set()
    items: dict[str, Article] = {}
    expected_total = None
    page_number = 0
    while url is not None:
        if url in visited or len(visited) >= MAX_PAGES:
            raise MonitorError("官網分頁循環或超出上限，未修改去重紀錄。")
        visited.add(url)
        page_number += 1
        raw = http.request(url, "官網")
        try:
            html = raw.decode("utf-8-sig")
        except UnicodeError:
            raise MonitorError("官網頁面編碼無法解析，未修改去重紀錄。") from None
        parser = NewsListParser(url)
        try:
            parser.feed(html)
            parser.close()
            parser.validate()
        except MonitorError as exc:
            raise MonitorError(f"官網第 {page_number} 頁：{exc}") from None
        if expected_total is None:
            expected_total = parser.total_pages
        if parser.total_pages != expected_total:
            raise MonitorError("官網分頁數在掃描途中變動，留待下次完整重試。")
        for item in parser.articles:
            items.setdefault(item.url, item)
        if not all_pages:
            break
        if page_number < expected_total:
            expected_next = f"https://{SOURCE_HOST}/hmt/news/index_{page_number + 1}.html"
            if parser.next_url != expected_next:
                raise MonitorError("官網下一頁連結缺失或改變，未修改去重紀錄。")
        elif parser.next_url is not None:
            raise MonitorError("官網末頁仍有下一頁，未修改去重紀錄。")
        url = parser.next_url
        if url is not None:
            time.sleep(0.2)
    return sorted(items.values(), key=lambda item: (item.published, item.url))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_state(articles: list[Article]) -> dict[str, Any]:
    return {"schema": 1, "source": SOURCE_URL, "initialized_at": utc_now(),
            "seen": sorted({item.key for item in articles}), "pending": {}}


def validate_state(state: Any) -> None:
    if (not isinstance(state, dict) or state.get("schema") != 1 or
            state.get("source") != SOURCE_URL or
            not isinstance(state.get("initialized_at"), str) or
            not state["initialized_at"] or not isinstance(state.get("seen"), list) or
            not isinstance(state.get("pending"), dict)):
        raise MonitorError("官網去重 Issue 格式不符；已停止，避免把舊文重新推播。")
    if any(not isinstance(key, str) or not ID_PATTERN.fullmatch(key) for key in state["seen"]):
        raise MonitorError("官網去重紀錄損壞，已停止；請保留 Issue 並檢查內容。")
    if len(state["seen"]) != len(set(state["seen"])):
        raise MonitorError("官網去重紀錄有重複識別碼，已停止。")
    for key, entry in state["pending"].items():
        try:
            if not ID_PATTERN.fullmatch(key) or key in state["seen"] or not isinstance(entry, dict):
                raise ValueError
            item = Article(**entry["article"])
            if any(not isinstance(value, str) for value in asdict(item).values()):
                raise ValueError
            if not item.title or item.key != key or official_url(item.url, SOURCE_URL, article=True) != item.url:
                raise ValueError
            date.fromisoformat(item.published)
            receipts = entry["delivered_to"]
            if (not isinstance(receipts, list) or any(not isinstance(receipt, str) or
                    not ID_PATTERN.fullmatch(receipt) for receipt in receipts)):
                raise ValueError
        except (TypeError, ValueError, KeyError, MonitorError):
            raise MonitorError("官網待發紀錄損壞，已停止；未重建或清空原有紀錄。") from None


def encode_state(state: dict[str, Any]) -> str:
    validate_state(state)
    body = (STATE_MARKER + "\n"
            "這是台港澳官網公告監控的獨立去重紀錄，請勿刪除或手動修改內文。\n"
            "公告以網址的雜湊識別；不儲存 Discord Webhook 或權杖。\n\n"
            "```json\n" + json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n```\n")
    if len(body) > 60000:
        raise MonitorError("官網去重 Issue 接近容量上限，已停止；需升級紀錄格式。")
    return body


def decode_state(body: str) -> dict[str, Any]:
    if not body.startswith(STATE_MARKER):
        raise MonitorError("官網去重 Issue 標記缺失，已停止；請勿刪除後重跑。")
    match = re.search(r"```json\s*\n(.*?)\n```", body, re.DOTALL)
    if match is None:
        raise MonitorError("官網去重 Issue 的 JSON 區塊缺失，已停止。")
    try:
        state = json.loads(match.group(1))
    except ValueError:
        raise MonitorError("官網去重 Issue 的 JSON 損壞，已停止。") from None
    validate_state(state)
    return state


def merge_states(states: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep all known articles and recipient receipts from valid duplicates.

    A seen key includes the first-run baseline as well as delivered articles.
    It therefore wins over an older pending copy. Keep the oldest Issue's
    initialization date, and never mutate any input or discard unreadable data.
    """
    if not states:
        raise MonitorError("沒有可合併的官網去重紀錄，已停止。")
    for state in states:
        validate_state(state)
    seen = {key for state in states for key in state["seen"]}
    pending: dict[str, Any] = {}
    for state in states:
        for key, entry in state["pending"].items():
            if key in seen:
                continue
            if key not in pending:
                pending[key] = {"article": dict(entry["article"]),
                                "delivered_to": sorted(set(entry["delivered_to"]))}
            else:
                if pending[key]["article"]["url"] != entry["article"]["url"]:
                    raise MonitorError("官網待發紀錄的網址識別衝突，已停止並保留原紀錄。")
                pending[key]["delivered_to"] = sorted(
                    set(pending[key]["delivered_to"]) | set(entry["delivered_to"]))
    merged = {"schema": 1, "source": SOURCE_URL,
              "initialized_at": states[0]["initialized_at"],
              "seen": sorted(seen), "pending": pending}
    encode_state(merged)  # Check format and capacity before any Issue is changed.
    return merged


class GitHubIssueStore:
    def __init__(self, http: HTTPClient, token: str, repository: str):
        if not token or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise MonitorError("缺少 GITHUB_TOKEN 或 GITHUB_REPOSITORY；請透過套件的工作流程執行。")
        self.http = http
        self.api = f"https://api.github.com/repos/{repository}/issues"
        self.headers = {"Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28"}
        self.number: int | None = None

    def load(self) -> dict[str, Any] | None:
        self.number = None
        matches: dict[int, dict[str, Any]] = {}
        for page in range(1, 51):
            query = urllib.parse.urlencode({"state": "all", "sort": "created",
                                            "direction": "asc", "per_page": 100, "page": page})
            issues = self.http.json(self.api + "?" + query, "GitHub", headers=self.headers)
            if not isinstance(issues, list):
                raise MonitorError("GitHub Issue 清單格式異常，已停止。")
            for issue in issues:
                if not isinstance(issue, dict) or "pull_request" in issue:
                    continue
                body = issue.get("body") or ""
                if (issue.get("title") == STATE_TITLE or
                        isinstance(body, str) and body.startswith(STATE_MARKER)):
                    number = issue.get("number")
                    if type(number) is not int or number <= 0:
                        raise MonitorError("GitHub 去重 Issue 編號異常，已停止。")
                    matches[number] = issue
            if len(issues) < 100:
                break
        else:
            raise MonitorError("專案 Issue 數量超出查找上限，已停止，未建立新紀錄。")
        if not matches:
            return None
        states = []
        # The oldest number is stable even if another Issue is closed or updated.
        for number in sorted(matches):
            body = matches[number].get("body") or ""
            try:
                if not isinstance(body, str):
                    raise MonitorError("Issue 內文格式異常。")
                states.append(decode_state(body))
            except MonitorError as exc:
                raise MonitorError(f"官網去重 Issue #{number} 無法讀取：{exc}") from None
        state = merge_states(states) if len(states) > 1 else states[0]
        self.number = min(matches)
        if state != states[0]:
            # Persist the full union before the source scan or any Discord send.
            # Other Issues remain intact; subsequent runs converge on this one.
            self.save(state)
        if len(states) > 1:
            print(f"已整合 {len(states)} 份官網去重紀錄，沿用 Issue #{self.number}；"
                  f"已記錄 {len(state['seen'])} 篇，待發 {len(state['pending'])} 篇。")
        return state

    def save(self, state: dict[str, Any]) -> None:
        body = encode_state(state)
        if self.number is None:
            result = self.http.json(self.api, "GitHub", method="POST",
                                    payload={"title": STATE_TITLE, "body": body}, headers=self.headers)
            if not isinstance(result, dict) or not isinstance(result.get("number"), int):
                raise MonitorError("GitHub 未確認去重 Issue 建立結果；請檢查 Issues 再重跑。")
            self.number = result["number"]
        else:
            self.http.json(f"{self.api}/{self.number}", "GitHub", method="PATCH",
                           payload={"body": body}, headers=self.headers)


def parse_webhooks(value: str) -> list[str]:
    result = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = urllib.parse.urlsplit(line)
        valid_hosts = {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}
        if (parts.scheme != "https" or parts.netloc not in valid_hosts or parts.fragment or
                not re.fullmatch(r"/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9._-]+", parts.path)):
            raise MonitorError("OFFICIAL_DISCORD_WEBHOOK_URLS 格式錯誤；請一行只貼一條完整 Webhook。")
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        if (set(query) - {"wait", "thread_id"} or
                ("thread_id" in query and (len(query["thread_id"]) != 1 or
                                           not query["thread_id"][0].isdigit()))):
            raise MonitorError("Webhook 含不支援的參數；請使用 Discord 複製的原始網址。")
        # Normalise wait=true so equivalent URLs share the same delivery receipt.
        query["wait"] = ["true"]
        url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                       urllib.parse.urlencode(sorted(query.items()), doseq=True), ""))
        if url not in result:
            result.append(url)
    if not result:
        raise MonitorError("OFFICIAL_DISCORD_WEBHOOK_URLS 未設定；請在 discord-production 新增此 Secret。")
    return result


def webhook_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def clip(value: str, units: int) -> str:
    # Discord limits count UTF-16 units; emoji can occupy two units.
    raw = value.encode("utf-16-le")
    if len(raw) <= units * 2:
        return value
    return raw[:(units - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


def discord_payload(article: Article, *, test: bool = False) -> dict[str, Any]:
    return {
        "username": "燕雲快報｜官網公告",
        "content": ("🧪 **官網公告測試成功！**以下為既有公告示範。" if test else
                    "📜 **《燕雲十六聲》台港澳官方新公告**"),
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": clip(article.title, 256), "url": article.url,
            "description": clip(article.summary, 600) or "點擊標題閱讀官方完整公告。",
            "color": 0xC59A52,
            "fields": [{"name": "官方發布日期", "value": article.published.replace("-", "/"),
                        "inline": True}],
            "footer": {"text": "台港澳官方網站｜摘要摘自公告列表"},
        }],
    }


def send_discord(http: HTTPClient, webhook: str, article: Article, *, test: bool = False) -> None:
    result = http.json(webhook, "Discord", method="POST",
                       payload=discord_payload(article, test=test))
    if not isinstance(result, dict) or not result.get("id"):
        raise MonitorError("Discord 未確認訊息建立結果，未記錄為推播成功。")


def process_scan(articles: list[Article], store: GitHubIssueStore, state: dict[str, Any] | None,
                 webhooks: list[str], send) -> str:
    """Persist the pending queue first, then each successful recipient receipt."""
    if not articles:
        raise MonitorError("官網公告清單為空，未修改紀錄。")
    if not webhooks:
        raise MonitorError("沒有設定官網推播的 Webhook，未修改紀錄。")
    if state is None:
        store.save(new_state(articles))
        return f"初始化完成：已記錄 {len(articles)} 篇既有公告；沒有推播舊文。"
    validate_state(state)
    known = set(state["seen"])
    added = 0
    for article in articles:
        if article.key not in known and article.key not in state["pending"]:
            state["pending"][article.key] = {"article": asdict(article), "delivered_to": []}
            added += 1
    if added:
        store.save(state)  # Never send if the durable pending queue cannot be saved.
    targets = [(url, webhook_key(url)) for url in webhooks]
    completed = 0
    pending_keys = sorted(state["pending"], key=lambda key: (
        state["pending"][key]["article"]["published"],
        state["pending"][key]["article"]["url"]))
    for key in pending_keys:
        entry = state["pending"][key]
        article = Article(**entry["article"])
        for url, target_key in targets:
            if target_key in entry["delivered_to"]:
                continue
            send(url, article)
            entry["delivered_to"].append(target_key)
            store.save(state)  # A successful channel is skipped on the next retry.
        state["seen"].append(key)
        state["seen"].sort()
        del state["pending"][key]
        store.save(state)
        completed += 1
        print(f"已推播：{article.published}｜{clip(article.title, 120)}")
    return f"掃描完成：官網共 {len(articles)} 篇；新增 {added} 篇；完成推播 {completed} 篇。"


def write_summary(message: str, *, failed: bool = False) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with Path(summary_path).open("a", encoding="utf-8") as stream:
                stream.write("## 台港澳官網公告監控\n\n")
                stream.write(("❌ " if failed else "✅ ") + message + "\n")
        except OSError:
            print("無法寫入 Actions 摘要；執行結果仍可在日誌查看。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("scan", "test", "preview"), default="scan")
    args = parser.parse_args()
    try:
        http = HTTPClient()
        if args.mode == "preview":
            articles = fetch_news(http)
            latest = articles[-1]
            print(json.dumps({"source": SOURCE_URL, "article_count": len(articles),
                              "latest": asdict(latest),
                              "discord_preview": discord_payload(latest, test=True)},
                             ensure_ascii=False, indent=2))
            message = f"預覽完成：解析 {len(articles)} 篇公告；未推播、未修改去重紀錄。"
        else:
            webhooks = parse_webhooks(os.environ.get("OFFICIAL_DISCORD_WEBHOOK_URLS", ""))
            if args.mode == "test":
                article = fetch_news(http, all_pages=False)[-1]
                for webhook in webhooks:
                    send_discord(http, webhook, article, test=True)
                message = f"測試完成：已向 {len(webhooks)} 個設定的 Webhook 發送各 1 則測試；未修改去重紀錄。"
            else:
                store = GitHubIssueStore(http, os.environ.get("GITHUB_TOKEN", ""),
                                         os.environ.get("GITHUB_REPOSITORY", ""))
                state = store.load()
                articles = fetch_news(http)
                message = process_scan(articles, store, state, webhooks,
                                       lambda url, article: send_discord(http, url, article))
        print(message)
        write_summary(message)
        return 0
    except MonitorError as exc:
        message = str(exc)
        print("ERROR: " + message, file=sys.stderr)
        write_summary(message, failed=True)
        return 1
    except Exception as exc:
        # Do not print repr/traceback: urllib exceptions can include a secret URL.
        message = f"未預期的錯誤（{type(exc).__name__}）；未自動清空去重紀錄，請檢查程式。"
        print("ERROR: " + message, file=sys.stderr)
        write_summary(message, failed=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
