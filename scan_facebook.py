#!/usr/bin/env python3
"""Facebook RSS → 燕雲快報；須先提供實際可用的 FACEBOOK_RSS_URL。

來源由 RSS 服務取得。程式每 30 分鐘檢查，不會加速服務端的來源刷新。
共用 scan_youtube.py 的連線、Webhook 與時間工具；去重 Issue 各自獨立。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from scan_youtube import (
    Destination, GitHubState, MonitorError, TAIWAN, clip,
    http_request, parse_date, parse_webhooks, post_discord, utc_now,
)

PAGE_URL = "https://www.facebook.com/WhereWindsMeetHMT"
PAGE_HANDLE = "WhereWindsMeetHMT"
STATE_TITLE = "[WWM Facebook Monitor] State - do not edit"
STATE_START = "<!-- wwm-facebook-state:start -->"
STATE_END = "<!-- wwm-facebook-state:end -->"
MAX_STATE_BYTES = 58_000
POST_PATTERN = re.compile(r"(?:post|video|photo):[A-Za-z0-9_-]{6,160}\Z")
TARGET_PATTERN = re.compile(r"[0-9a-f]{24}\Z")
FB_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "mbasic.facebook.com"}
ATOM = "http://www.w3.org/2005/Atom"
MEDIA = "http://search.yahoo.com/mrss/"
CONTENT = "http://purl.org/rss/1.0/modules/content/"


class HTMLContent(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.images: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.split(":")[-1]
        values = dict(attrs)
        if tag in ("script", "style"):
            self.hidden += 1
        if self.hidden:
            return
        if tag in ("br", "p", "div", "li"):
            self.parts.append("\n")
        if tag == "img" and values.get("src"):
            self.images.append(values["src"])

    def handle_endtag(self, tag):
        tag = tag.split(":")[-1]
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in ("p", "div", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def text(self) -> str:
        lines = [re.sub(r"[ \t\r]+", " ", line).strip() for line in "".join(self.parts).split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def plain_content(value: str) -> tuple[str, list[str]]:
    parser = HTMLContent()
    parser.feed(value)
    parser.close()
    return parser.text(), parser.images


def is_official_page(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value.strip())
    except ValueError:
        return False
    return (
        parsed.scheme in ("http", "https")
        and parsed.netloc.lower() in FB_HOSTS
        and parsed.path.rstrip("/").lower() in (f"/{PAGE_HANDLE.lower()}", f"/{PAGE_HANDLE.lower()}/posts")
    )


def canonical_post_url(value: str, *, official_feed: bool) -> tuple[str, str]:
    """以 FB 文章／影片 ID 去重；去除追蹤、留言及來源服務的 GUID 變更。"""
    try:
        parsed = urllib.parse.urlsplit(value.strip())
    except ValueError:
        raise MonitorError("Facebook 貼文網址格式錯誤。") from None
    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() not in FB_HOSTS:
        raise MonitorError("RSS 含有非 Facebook 貼文連結；請關閉『共享貼文連至原始網址』選項。")
    path = parsed.path.rstrip("/")
    parts = [x for x in path.split("/") if x]
    query = urllib.parse.parse_qs(parsed.query)
    own_path = bool(parts and parts[0].lower() == PAGE_HANDLE.lower())
    if not official_feed and not own_path:
        raise MonitorError("RSS 沒有可核對的官方粉專來源；不建立基準。")
    match = re.fullmatch(r"/(?:[A-Za-z0-9_.-]+/)?(posts|videos)/(pfbid[A-Za-z0-9]+|\d{6,})", path)
    if match:
        owner = parts[0] if len(parts) == 3 else None
        if owner and owner.lower() != PAGE_HANDLE.lower() and not owner.isdigit():
            raise MonitorError("RSS 含有其他粉專的貼文；不修改紀錄。")
        kind = "post" if match.group(1) == "posts" else "video"
        post_id = kind + ":" + match.group(2)
        canonical = f"https://www.facebook.com/{PAGE_HANDLE}/{match.group(1)}/{match.group(2)}" if own_path else "https://www.facebook.com" + path
    elif path in ("/permalink.php", "/story.php") and query.get("story_fbid"):
        identifier = query["story_fbid"][0]
        owner = query.get("id", [""])[0]
        if not re.fullmatch(r"(?:pfbid[A-Za-z0-9]+|\d{6,})", identifier) or not owner.isdigit():
            raise MonitorError("Facebook permalink 缺少文章或粉專 ID。")
        post_id = "post:" + identifier
        canonical = "https://www.facebook.com/permalink.php?" + urllib.parse.urlencode({"story_fbid": identifier, "id": owner})
    elif path in ("/watch", "/watch/") and query.get("v") and query["v"][0].isdigit():
        post_id = "video:" + query["v"][0]
        canonical = "https://www.facebook.com/watch/?v=" + query["v"][0]
    elif path in ("/photo", "/photo.php") and query.get("fbid") and query["fbid"][0].isdigit():
        post_id = "photo:" + query["fbid"][0]
        canonical = "https://www.facebook.com/photo.php?fbid=" + query["fbid"][0]
    elif len(parts) == 2 and parts[0] == "reel" and parts[1].isdigit():
        post_id = "video:" + parts[1]
        canonical = "https://www.facebook.com/reel/" + parts[1]
    else:
        raise MonitorError("RSS 貼文缺少可去重的 FB 文章 ID；目前不支援此連結格式。")
    if not POST_PATTERN.fullmatch(post_id):
        raise MonitorError("Facebook 貼文 ID 格式錯誤。")
    return post_id, canonical


def feed_date(value: str) -> str | None:
    if not value.strip():
        return None  # 不把偵測時間冒充成 FB 發布時間。
    try:
        result = parsedate_to_datetime(value)
    except (ValueError, TypeError, IndexError):
        result = parse_date(value)
    if result.tzinfo is None:
        raise MonitorError("Facebook RSS 發布時間缺少時區。")
    return result.astimezone(timezone.utc).isoformat(timespec="seconds")


def image_url(value: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    return value if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password else None


@dataclass(frozen=True)
class FacebookPost:
    post_id: str
    url: str
    title: str
    text: str
    published: str | None = None
    image: str | None = None

    def record(self, detected_at: str) -> dict[str, Any]:
        return {
            "post_id": self.post_id, "url": self.url, "title": self.title,
            "text": self.text, "published": self.published, "image": self.image,
            "detected_at": detected_at, "delivered_to": [],
        }


def parse_facebook_feed(data: bytes) -> list[FacebookPost]:
    if len(data) > 2_000_000 or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise MonitorError("Facebook RSS 大小或 XML 格式異常。")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise MonitorError("Facebook 來源不是有效 RSS／Atom；可能是登入頁或服務方案提示。") from None
    atom = root.tag == f"{{{ATOM}}}feed"
    if atom:
        feed_links = [element.get("href", "") for element in root.findall(f"{{{ATOM}}}link") if element.get("rel", "alternate") == "alternate"]
        entries = root.findall(f"{{{ATOM}}}entry")
    elif root.tag == "rss" and root.find("channel") is not None:
        channel = root.find("channel")
        feed_links = [channel.findtext("link") or ""]
        entries = channel.findall("item")
    else:
        raise MonitorError("Facebook 來源不是 RSS 2.0 或 Atom feed。")
    official_feed = any(is_official_page(url) for url in feed_links)
    posts: dict[str, FacebookPost] = {}
    for entry in entries:
        if atom:
            links = [element.get("href", "") for element in entry.findall(f"{{{ATOM}}}link") if element.get("rel", "alternate") == "alternate"]
            raw_url = links[0] if links else ""
            title = entry.findtext(f"{{{ATOM}}}title") or ""
            content_element = entry.find(f"{{{ATOM}}}content")
            if content_element is None:
                content_element = entry.find(f"{{{ATOM}}}summary")
            content = "" if content_element is None else "".join(content_element.itertext()) if len(content_element) == 0 else ET.tostring(content_element, encoding="unicode")
            date = entry.findtext(f"{{{ATOM}}}published") or ""
        else:
            raw_url = entry.findtext("link") or ""
            title = entry.findtext("title") or ""
            content = entry.findtext(f"{{{CONTENT}}}encoded") or entry.findtext("description") or ""
            date = entry.findtext("pubDate") or ""
        # 來源廣告也必須可辨識；不能把 FB 粉專首頁當作每篇貼文。
        post_id, url = canonical_post_url(raw_url, official_feed=official_feed)
        text, html_images = plain_content(content)
        title, _ = plain_content(title)
        title = title or (text.splitlines()[0] if text else "官方 Facebook 新貼文")
        images = []
        for enclosure in entry.findall("enclosure"):
            if enclosure.get("type", "").startswith("image/"):
                images.append(enclosure.get("url", ""))
        for element in entry.iter():
            if element.tag in (f"{{{MEDIA}}}thumbnail", f"{{{MEDIA}}}content") and (element.tag.endswith("thumbnail") or element.get("medium") == "image" or element.get("type", "").startswith("image/")):
                images.append(element.get("url", ""))
        images.extend(html_images)
        image = next((candidate for value in images if (candidate := image_url(value))), None)
        posts[post_id] = FacebookPost(post_id, url, clip(title, 180), clip(text, 350), feed_date(date), image)
    if not posts:
        raise MonitorError("Facebook RSS 沒有可用貼文；不建立空基準。")
    # 保留來源順序供 test 使用；一般 RSS 將最新項目放在最前。
    return list(posts.values())


def fetch_posts(rss_url: str) -> list[FacebookPost]:
    try:
        parsed = urllib.parse.urlsplit(rss_url)
    except ValueError:
        raise MonitorError("FACEBOOK_RSS_URL 網址格式錯誤。") from None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise MonitorError("FACEBOOK_RSS_URL 必須是 HTTPS 的 RSS 網址，不能含帳密或片段。")
    if parsed.hostname.lower() in FB_HOSTS:
        raise MonitorError("FACEBOOK_RSS_URL 要填轉換後的 RSS 網址，不是 Facebook 粉專網址。")
    return parse_facebook_feed(http_request(rss_url, label="Facebook RSS", headers={"Accept": "application/rss+xml,application/atom+xml,application/xml"}))


def validate_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict) or state.get("version") != 1 or state.get("page_url") != PAGE_URL:
        raise MonitorError("Facebook 狀態版本或粉專網址不符；不會自動清空紀錄。")
    parse_date(state.get("initialized_at"))
    seen, pending = state.get("seen_post_ids"), state.get("pending")
    if not isinstance(seen, list) or any(not isinstance(x, str) or not POST_PATTERN.fullmatch(x) for x in seen) or len(seen) != len(set(seen)) or not isinstance(pending, list):
        raise MonitorError("Facebook 去重紀錄損壞。")
    pending_ids: set[str] = set()
    for item in pending:
        if not isinstance(item, dict) or item.get("post_id") not in seen or item.get("post_id") in pending_ids:
            raise MonitorError("Facebook 待送紀錄損壞。")
        pending_ids.add(item["post_id"])
        if any(not isinstance(item.get(key), str) for key in ("url", "title", "text", "detected_at")):
            raise MonitorError("Facebook 待送內容損壞。")
        identifier, _ = canonical_post_url(item["url"], official_feed=True)
        if identifier != item["post_id"]:
            raise MonitorError("Facebook 待送網址與貼文 ID 不符。")
        parse_date(item["detected_at"])
        if item.get("published") is not None:
            parse_date(item["published"])
        if item.get("image") is not None and (not isinstance(item["image"], str) or image_url(item["image"]) is None):
            raise MonitorError("Facebook 待送圖片網址格式錯誤。")
        delivered = item.get("delivered_to")
        if not isinstance(delivered, list) or any(not isinstance(x, str) or not TARGET_PATTERN.fullmatch(x) for x in delivered) or len(delivered) != len(set(delivered)):
            raise MonitorError("Facebook 目的地成功紀錄損壞。")
    return state


def encode_state(state: dict[str, Any]) -> str:
    body = STATE_START + "\n```json\n" + json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n```\n" + STATE_END
    if len(body.encode("utf-8")) > MAX_STATE_BYTES:
        raise MonitorError("Facebook 狀態接近容量上限；停止新增推播，請先擴充儲存。")
    return body


def decode_state(body: str) -> dict[str, Any]:
    start, end = body.find(STATE_START), body.find(STATE_END)
    if start < 0 or end <= start:
        raise MonitorError("Facebook 狀態 Issue 缺少標記；不會自動重設。")
    match = re.fullmatch(r"\s*```json\s*([\s\S]*?)\s*```\s*", body[start + len(STATE_START):end])
    if not match:
        raise MonitorError("Facebook 狀態 Issue 的 JSON 區塊格式錯誤。")
    try:
        return validate_state(json.loads(match.group(1)))
    except json.JSONDecodeError:
        raise MonitorError("Facebook 狀態 JSON 損壞；不會自動重設。") from None


class FacebookState(GitHubState):
    def __init__(self, repository: str, token: str, issue_number: str = ""):
        super().__init__(repository, token, PAGE_URL, issue_number)

    def load(self) -> dict[str, Any] | None:
        if self.issue_number is not None:
            candidates = [self.request(f"/issues/{self.issue_number}")]
        else:
            candidates = []
            for page in range(1, 101):
                issues = self.request(f"/issues?state=all&per_page=100&page={page}")
                if not isinstance(issues, list):
                    raise MonitorError("GitHub Issue 清單格式異常。")
                candidates.extend(item for item in issues if item.get("title") == STATE_TITLE and "pull_request" not in item)
                if len(issues) < 100:
                    break
            else:
                raise MonitorError("Issue 太多；請設定 FACEBOOK_STATE_ISSUE_NUMBER。")
        if not candidates:
            return None
        if len(candidates) != 1 or candidates[0].get("title") != STATE_TITLE or "pull_request" in candidates[0]:
            raise MonitorError("Facebook 狀態 Issue 重複或指定錯誤；請保留唯一紀錄。")
        self.issue_number = candidates[0]["number"]
        return decode_state(candidates[0].get("body") or "")

    def save(self, state: dict[str, Any]) -> None:
        body = encode_state(validate_state(state))
        path, method = ("/issues", "POST") if self.issue_number is None else (f"/issues/{self.issue_number}", "PATCH")
        issue = self.request(path, method, {"title": STATE_TITLE, "body": body})
        if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
            raise MonitorError("GitHub 未確認保存 Facebook 狀態成功；停止後續推播。")
        self.issue_number = issue["number"]


def build_payload(post: FacebookPost, *, test: bool = False, detected_at: str | None = None) -> dict[str, Any]:
    description = "🧪 **測試推播：這是 RSS 既有貼文，不視為本次新發文；不修改去重紀錄。**\n\n" if test else ""
    if post.text:
        description += clip(post.text, 350) + "\n\n"
    description += f"📘 [查看 Facebook 完整原文]({post.url})"
    detected = detected_at or utc_now()
    fields = [
        {"name": "📅 發布時間（臺灣）", "value": parse_date(post.published).astimezone(TAIWAN).strftime("%Y/%m/%d %H:%M") if post.published else "RSS 未提供", "inline": True},
        {"name": "📡 首次偵測（臺灣）", "value": parse_date(detected).astimezone(TAIWAN).strftime("%Y/%m/%d %H:%M"), "inline": True},
    ]
    embed = {
        "author": {"name": "📘 燕雲十六聲｜官方 Facebook 新貼文" if not test else "🧪 燕雲十六聲｜Facebook 推播測試", "url": PAGE_URL},
        "title": clip(post.title, 180), "url": post.url, "description": description,
        "color": 0x1877F2, "fields": fields,
        "footer": {"text": "燕雲十六聲官方 Facebook｜港澳台；文字與圖片依 RSS 提供"},
    }
    if post.published:
        embed["timestamp"] = post.published
    if post.image:
        embed["image"] = {"url": post.image}
    return {"username": "燕雲快報", "allowed_mentions": {"parse": []}, "embeds": [embed]}


def write_summary(lines: list[str]) -> None:
    text = "\n".join(["## Facebook｜燕雲快報監控", "", *lines, ""])
    print(text)
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def scan(storage: FacebookState, rss_url: str, destinations: list[Destination], *, dry_run: bool = False) -> None:
    state = storage.load()
    posts = fetch_posts(rss_url)
    now = utc_now()
    lines = [f"- RSS 讀取成功：{len(posts)} 則貼文。", "- 每 30 分鐘檢查來源；實際刷新頻率由 RSS 服務決定。"]
    if state is None:
        state = {"version": 1, "page_url": PAGE_URL, "initialized_at": now, "seen_post_ids": [post.post_id for post in posts], "pending": []}
        encode_state(state)
        if not dry_run:
            storage.save(state)
        write_summary(lines + ["- dry-run：正式 scan 將建立基準，本次不保存、不發送。" if dry_run else "- 已建立 Facebook 基準，既有貼文不推送。"])
        return
    seen = set(state["seen_post_ids"])
    new_posts = [post for post in posts if post.post_id not in seen]
    new_posts.sort(key=lambda post: (post.published or "", post.post_id))
    for post in new_posts:
        state["seen_post_ids"].append(post.post_id)
        state["pending"].append(post.record(now))
    encode_state(state)
    lines.append(f"- 新發現：{len(new_posts)} 則；待送／重試：{len(state['pending'])} 則。")
    if dry_run:
        write_summary(lines + ["- dry-run：不發送 Discord、不保存狀態。"])
        return
    if new_posts:
        storage.save(state)
    active_keys = {destination.key for destination in destinations}
    failed: set[str] = set()
    failures: list[str] = []
    sent = 0
    for item in list(state["pending"]):
        for index, destination in enumerate(destinations, 1):
            if destination.key in item["delivered_to"] or destination.key in failed:
                continue
            post = FacebookPost(item["post_id"], item["url"], item["title"], item["text"], item.get("published"), item.get("image"))
            try:
                post_discord(destination, build_payload(post, detected_at=item["detected_at"]))
            except MonitorError as error:
                failed.add(destination.key)
                failures.append(f"目的地 {index}：{error}")
                continue
            item["delivered_to"].append(destination.key)
            if active_keys.issubset(set(item["delivered_to"])):
                state["pending"].remove(item)
            storage.save(state)
            sent += 1
        if item in state["pending"] and active_keys.issubset(set(item["delivered_to"])):
            state["pending"].remove(item)
            storage.save(state)
    write_summary(lines + [f"- 成功推播：{sent} 則；尚待完成：{len(state['pending'])} 則。", *[f"- {failure}" for failure in failures]])
    if failures:
        raise MonitorError("部分 Facebook 推播失敗；成功紀錄已保存，下次只補送待完成項目。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("scan", "dry-run", "test"), default=os.getenv("FACEBOOK_MONITOR_MODE") or "scan")
    args = parser.parse_args(argv)
    try:
        if args.mode not in ("scan", "dry-run", "test"):
            raise MonitorError("FACEBOOK_MONITOR_MODE 必須是 scan、dry-run 或 test。")
        rss_url = os.getenv("FACEBOOK_RSS_URL", "").strip()
        if not rss_url:
            raise MonitorError("尚未設定 FACEBOOK_RSS_URL。請先免費建立並驗證官方粉專 RSS，再啟用 Facebook 排程。")
        destinations = [] if args.mode == "dry-run" else parse_webhooks(os.getenv("OFFICIAL_DISCORD_WEBHOOK_URLS", ""))
        if args.mode == "test":
            sample = fetch_posts(rss_url)[0]
            failures = []
            for index, destination in enumerate(destinations, 1):
                try:
                    post_discord(destination, build_payload(sample, test=True))
                except MonitorError as error:
                    failures.append(f"目的地 {index}：{error}")
            write_summary([f"- test：{len(destinations) - len(failures)} 個目的地收到 RSS 貼文測試卡片。", "- 不讀寫去重紀錄；重跑 test 會再送測試訊息。", *[f"- {failure}" for failure in failures]])
            if failures:
                raise MonitorError("部分 Facebook 測試推播失敗。")
            return 0
        storage = FacebookState(
            os.getenv("GH_REPOSITORY", "").strip() or os.getenv("GITHUB_REPOSITORY", "").strip(),
            os.getenv("GH_STATE_TOKEN", "").strip() or os.getenv("GITHUB_TOKEN", "").strip(),
            os.getenv("FACEBOOK_STATE_ISSUE_NUMBER", "").strip(),
        )
        scan(storage, rss_url, destinations, dry_run=args.mode == "dry-run")
        return 0
    except MonitorError as error:
        write_summary([f"- 執行失敗：{error}"])
        return 1


if __name__ == "__main__":
    sys.exit(main())
