#!/usr/bin/env python3
"""燕雲十六聲官方 YouTube → Discord；僅使用 Python 3.12+ 標準函式庫。

去重紀錄存於獨立 GitHub Issue，不依賴 runner 本機、cache 或 Artifact。
一般影片與 Shorts 一律按 YouTube Atom feed 的 video ID 處理。
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_CHANNEL_ID = "UCIg76vZz1n2iITdQARIxxsQ"  # @WhereWindsMeetHMT；2026-10-09 查證
CHANNEL_URL = "https://www.youtube.com/@WhereWindsMeetHMT"
STATE_TITLE = "[WWM YouTube Monitor] State - do not edit"
STATE_START = "<!-- wwm-youtube-state:start -->"
STATE_END = "<!-- wwm-youtube-state:end -->"
MAX_STATE_BYTES = 58_000
MAX_RESPONSE_BYTES = 8_000_000
CHANNEL_PATTERN = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
VIDEO_PATTERN = re.compile(r"[A-Za-z0-9_-]{11}\Z")
TARGET_PATTERN = re.compile(r"[0-9a-f]{24}\Z")
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
TAIWAN = timezone(timedelta(hours=8))


class MonitorError(RuntimeError):
    """只包含可安全寫入 Actions log 的錯誤訊息。"""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 不將 GitHub Authorization 或 Webhook token 轉送至重新導向目的地。
        return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clip(text: str, limit: int) -> str:
    """以 UTF-16 長度截斷，避免 emoji 造成 Discord 欄位超長。"""
    encoded = text.encode("utf-16-le")
    if len(encoded) <= limit * 2:
        return text
    return encoded[: (limit - 1) * 2].decode("utf-16-le", errors="ignore").rstrip() + "…"


def parse_date(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        raise MonitorError("YouTube 發布時間或狀態時間格式錯誤。") from None
    if result.tzinfo is None:
        raise MonitorError("YouTube 發布時間或狀態時間缺少時區。")
    return result


def http_request(
    url: str,
    *,
    label: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: Any = None,
    safe_retry: bool = True,
) -> bytes:
    """GET/PATCH 可重試；POST 僅在明確的 429 拒絕時重試。

    POST timeout / 5xx 結果可能不明，不在同一次呼叫盲目重送。
    不輸出原始 urllib exception，因為它可能包含 Webhook URL。
    """
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "User-Agent": "WWM-YouTube-Monitor/1.0",
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    opener = urllib.request.build_opener(NoRedirect())
    for attempt in range(3):
        delay = 2 ** attempt
        try:
            with opener.open(request, timeout=20) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise MonitorError(f"{label} 回應過大，已停止。")
            return data
        except urllib.error.HTTPError as error:
            status = error.code
            can_retry = safe_retry and status in (500, 502, 503, 504)
            if status == 429:
                can_retry = True  # 此次要求明確被限流，未執行。
                raw = error.read(32_000)
                try:
                    retry_after = json.loads(raw).get("retry_after")
                except (ValueError, AttributeError):
                    retry_after = None
                value = retry_after if retry_after is not None else error.headers.get("Retry-After", 5)
                try:
                    delay = float(value) + 0.25
                except (ValueError, TypeError):
                    delay = 5
                if not math.isfinite(delay) or delay < 0 or delay > 30:
                    raise MonitorError(f"{label} HTTP 429；等待時間較長，保留進度供下次重試。") from None
            if not can_retry or attempt == 2:
                extra = "；確認 issues: write、Issues 功能與權杖權限" if label == "GitHub" and status in (403, 404, 410) else ""
                raise MonitorError(f"{label} HTTP {status}{extra}。") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            if not safe_retry or attempt == 2:
                raise MonitorError(f"{label} 連線失敗或逾時；保留進度供下次重試。") from None
        time.sleep(delay)
    raise MonitorError(f"{label} 連線失敗。")


@dataclass(frozen=True)
class Video:
    video_id: str
    title: str
    published: str
    description: str = ""

    def record(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "published": self.published,
            "description": self.description,
            "delivered_to": [],
        }


def parse_feed(data: bytes, channel_id: str) -> list[Video]:
    if len(data) > 2_000_000 or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise MonitorError("YouTube RSS 大小或 XML 格式異常。")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise MonitorError("YouTube 回應不是有效的 Atom RSS；不修改去重紀錄。") from None
    if root.tag != f"{{{NS['atom']}}}feed":
        raise MonitorError("YouTube 回應不是 Atom feed；不修改去重紀錄。")
    # 實際 feed 根節點常省略 UC，entry 與 author URI 則保留完整 ID。
    root_channel = (root.findtext("yt:channelId", namespaces=NS) or "").strip()
    if root_channel not in (channel_id, channel_id[2:]):
        raise MonitorError("RSS 頻道 ID 與設定不一致；已停止。")
    author_uri = root.findtext("atom:author/atom:uri", namespaces=NS)
    if author_uri and author_uri.rstrip("/") != f"https://www.youtube.com/channel/{channel_id}":
        raise MonitorError("RSS 頻道作者網址與設定不一致；已停止。")
    videos: dict[str, Video] = {}
    for entry in root.findall("atom:entry", NS):
        video_id = (entry.findtext("yt:videoId", namespaces=NS) or "").strip()
        entry_channel = entry.findtext("yt:channelId", namespaces=NS)
        title = (entry.findtext("atom:title", namespaces=NS) or "").strip()
        published = (entry.findtext("atom:published", namespaces=NS) or "").strip()
        if not VIDEO_PATTERN.fullmatch(video_id) or entry_channel != channel_id or not title:
            raise MonitorError("RSS 中有不完整或不同頻道的影片；不修改去重紀錄。")
        published = parse_date(published).astimezone(timezone.utc).isoformat(timespec="seconds")
        description = (entry.findtext("media:group/media:description", namespaces=NS) or "").strip()
        videos[video_id] = Video(video_id, clip(title, 240), published, clip(description, 350))
    if not videos:
        # 已知本頻道有影片；空回應不應建立錯誤基準。
        raise MonitorError("YouTube RSS 沒有影片；保留原紀錄，不建立空基準。")
    return sorted(videos.values(), key=lambda video: (parse_date(video.published), video.video_id))


def fetch_videos(channel_id: str) -> list[Video]:
    url = "https://www.youtube.com/feeds/videos.xml?" + urllib.parse.urlencode({"channel_id": channel_id})
    return parse_feed(http_request(url, label="YouTube", headers={"Accept": "application/atom+xml"}), channel_id)


@dataclass(frozen=True)
class Destination:
    url: str
    key: str


def parse_webhooks(raw: str) -> list[Destination]:
    destinations: dict[str, Destination] = {}
    for line in raw.splitlines():
        value = line.strip()
        if not value:
            continue
        try:
            parsed = urllib.parse.urlsplit(value)
        except ValueError:
            raise MonitorError("OFFICIAL_DISCORD_WEBHOOK_URLS 網址格式錯誤。") from None
        match = re.fullmatch(r"/api/(?:v\d+/)?webhooks/(\d+)/([A-Za-z0-9._-]{20,})", parsed.path)
        if parsed.scheme != "https" or parsed.netloc != "discord.com" or parsed.query or parsed.fragment or not match:
            raise MonitorError("OFFICIAL_DISCORD_WEBHOOK_URLS 格式錯誤；一行放一組 Discord Webhook 原始網址。")
        # 只雜湊 Webhook ID：不把 token 放入 Issue，且重置同一 Webhook token 不會重複推播。
        key = hashlib.sha256(match.group(1).encode("ascii")).hexdigest()[:24]
        destinations[key] = Destination(value, key)
    if not destinations:
        raise MonitorError("缺少 OFFICIAL_DISCORD_WEBHOOK_URLS；請新增燕雲快報頻道的 Secret。")
    return list(destinations.values())


def validate_state(state: Any, channel_id: str) -> dict[str, Any]:
    if not isinstance(state, dict) or state.get("version") != 1 or state.get("channel_id") != channel_id:
        raise MonitorError("YouTube 狀態版本或頻道 ID 不一致；不會自動清空紀錄。")
    parse_date(state.get("initialized_at"))
    seen = state.get("seen_video_ids")
    pending = state.get("pending")
    if not isinstance(seen, list) or any(not isinstance(x, str) or not VIDEO_PATTERN.fullmatch(x) for x in seen) or len(seen) != len(set(seen)):
        raise MonitorError("YouTube seen_video_ids 紀錄損壞；請還原狀態 Issue。")
    if not isinstance(pending, list):
        raise MonitorError("YouTube pending 紀錄損壞；請還原狀態 Issue。")
    pending_ids: set[str] = set()
    for item in pending:
        if not isinstance(item, dict) or item.get("video_id") not in seen or item.get("video_id") in pending_ids:
            raise MonitorError("YouTube 待送影片紀錄損壞。")
        pending_ids.add(item["video_id"])
        if not isinstance(item.get("title"), str) or not item["title"] or not isinstance(item.get("description"), str):
            raise MonitorError("YouTube 待送影片內容損壞。")
        parse_date(item.get("published"))
        delivered = item.get("delivered_to")
        if not isinstance(delivered, list) or any(not isinstance(x, str) or not TARGET_PATTERN.fullmatch(x) for x in delivered) or len(delivered) != len(set(delivered)):
            raise MonitorError("YouTube 目的地成功紀錄損壞。")
    return state


def encode_state(state: dict[str, Any]) -> str:
    # compact JSON 留下更多空間保存完整 video ID 歷史；不自動刪除歷史。
    body = STATE_START + "\n```json\n" + json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n```\n" + STATE_END
    if len(body.encode("utf-8")) > MAX_STATE_BYTES:
        raise MonitorError("YouTube 狀態 Issue 接近容量上限；停止新增推播，請先擴充狀態儲存。")
    return body


def decode_state(body: str, channel_id: str) -> dict[str, Any]:
    start, end = body.find(STATE_START), body.find(STATE_END)
    if start < 0 or end <= start:
        raise MonitorError("YouTube 狀態 Issue 缺少標記；不會自動重設。")
    section = body[start + len(STATE_START) : end]
    match = re.fullmatch(r"\s*```json\s*([\s\S]*?)\s*```\s*", section)
    if not match:
        raise MonitorError("YouTube 狀態 Issue 的 JSON 區塊格式錯誤。")
    try:
        return validate_state(json.loads(match.group(1)), channel_id)
    except json.JSONDecodeError:
        raise MonitorError("YouTube 狀態 JSON 損壞；不會自動重設。") from None


class GitHubState:
    def __init__(self, repository: str, token: str, channel_id: str, issue_number: str = ""):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise MonitorError("缺少或無效的 GH_REPOSITORY / GITHUB_REPOSITORY。")
        if not token:
            raise MonitorError("缺少 GH_STATE_TOKEN / GITHUB_TOKEN。Actions 會自動提供，不必另外建立 PAT。")
        if issue_number and not re.fullmatch(r"[1-9]\d*", issue_number):
            raise MonitorError("YOUTUBE_STATE_ISSUE_NUMBER 必須是正整數。")
        self.base = f"https://api.github.com/repos/{repository}"
        self.token = token
        self.channel_id = channel_id
        self.issue_number = int(issue_number) if issue_number else None

    def request(self, path: str, method: str = "GET", payload: Any = None) -> Any:
        raw = http_request(
            self.base + path, label="GitHub", method=method, payload=payload,
            safe_retry=method in ("GET", "PATCH"),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            return json.loads(raw)
        except ValueError:
            raise MonitorError("GitHub 回應不是有效 JSON。") from None

    def load(self) -> dict[str, Any] | None:
        if self.issue_number is not None:
            candidates = [self.request(f"/issues/{self.issue_number}")]
        else:
            candidates = []
            for page in range(1, 101):
                issues = self.request(f"/issues?state=all&per_page=100&page={page}")
                if not isinstance(issues, list):
                    raise MonitorError("GitHub Issue 清單格式異常。")
                candidates.extend(x for x in issues if x.get("title") == STATE_TITLE and "pull_request" not in x)
                if len(issues) < 100:
                    break
            else:
                raise MonitorError("Issue 太多；請設定 YOUTUBE_STATE_ISSUE_NUMBER，避免誤判為首次啟動。")
        if not candidates:
            return None
        if len(candidates) != 1 or candidates[0].get("title") != STATE_TITLE or "pull_request" in candidates[0]:
            raise MonitorError("YouTube 狀態 Issue 重複或指定錯誤；請保留唯一紀錄。")
        self.issue_number = candidates[0]["number"]
        return decode_state(candidates[0].get("body") or "", self.channel_id)

    def save(self, state: dict[str, Any]) -> None:
        body = encode_state(validate_state(state, self.channel_id))
        payload = {"title": STATE_TITLE, "body": body}
        path, method = ("/issues", "POST") if self.issue_number is None else (f"/issues/{self.issue_number}", "PATCH")
        issue = self.request(path, method, payload)
        if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
            raise MonitorError("GitHub 未確認保存狀態成功；停止後續推播。")
        self.issue_number = issue["number"]


def build_payload(video: Video, *, test: bool = False) -> dict[str, Any]:
    url = f"https://www.youtube.com/watch?v={video.video_id}"
    published = parse_date(video.published).astimezone(TAIWAN).strftime("%Y/%m/%d %H:%M")
    description = ("🧪 **測試推播：以下為 RSS 最新影片，並非本次新發布；不修改去重紀錄。**\n\n" if test else "")
    if video.description:
        description += clip(video.description, 350) + "\n\n"
    description += f"▶ [觀看完整影片]({url})"
    return {
        "username": "燕雲快報",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "author": {"name": "🎬 燕雲十六聲｜官方 YouTube 新影片" if not test else "🧪 燕雲十六聲｜YouTube 推播測試", "url": CHANNEL_URL},
            "title": clip(video.title, 240),
            "url": url,
            "description": description,
            "color": 0xFF0000,
            "image": {"url": f"https://i.ytimg.com/vi/{video.video_id}/hqdefault.jpg"},
            "fields": [{"name": "📅 發布時間（臺灣）", "value": published, "inline": True}],
            "timestamp": video.published,
            "footer": {"text": "燕雲十六聲官方 YouTube｜港澳台"},
        }],
    }


def post_discord(destination: Destination, payload: dict[str, Any]) -> None:
    # wait=true 要求 Discord 回傳已建立訊息；204 不視為確定送達。
    raw = http_request(destination.url + "?wait=true", label="Discord", method="POST", payload=payload, safe_retry=False)
    try:
        message = json.loads(raw)
    except ValueError:
        raise MonitorError("Discord 未回傳訊息確認，發送結果不明。") from None
    if not isinstance(message, dict) or not isinstance(message.get("id"), str) or not message["id"].isdigit():
        raise MonitorError("Discord 未回傳訊息 ID，發送結果不明。")


def write_summary(lines: list[str]) -> None:
    text = "\n".join(["## YouTube 官方情報監控", "", *lines, ""])
    print(text)
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def scan(
    storage: GitHubState,
    channel_id: str,
    destinations: list[Destination],
    *,
    dry_run: bool = False,
) -> None:
    state = storage.load()
    videos = fetch_videos(channel_id)
    lines = [f"- RSS 讀取成功：{len(videos)} 部影片（不區分一般影片與 Shorts）。"]
    if state is None:
        state = {
            "version": 1,
            "channel_id": channel_id,
            "initialized_at": utc_now(),
            "seen_video_ids": [video.video_id for video in videos],
            "pending": [],
        }
        encode_state(state)
        if not dry_run:
            storage.save(state)
        write_summary(lines + [
            "- 預覽：尚未建立基準；正式 scan 將記錄既有影片，不推送舊片。" if dry_run else "- 首次啟動：已建立基準，既有影片不推送。",
            "- 本次 Discord 推播：0 則。",
        ])
        return

    seen = set(state["seen_video_ids"])
    new_videos = [video for video in videos if video.video_id not in seen]
    for video in new_videos:
        state["seen_video_ids"].append(video.video_id)
        state["pending"].append(video.record())
    encode_state(state)  # 容量檢查要在發送前。
    lines.append(f"- 新發現：{len(new_videos)} 部；待送／重試：{len(state['pending'])} 部。")
    if dry_run:
        write_summary(lines + ["- dry-run：不發送 Discord、不保存狀態。"])
        return
    if new_videos:
        # 先保存待送快照；下一次即使影片離開 RSS 清單，仍可重試。
        storage.save(state)

    active_keys = {destination.key for destination in destinations}
    failed: set[str] = set()
    failures: list[str] = []
    sent = 0
    for item in list(state["pending"]):
        for index, destination in enumerate(destinations, 1):
            if destination.key in item["delivered_to"] or destination.key in failed:
                continue
            video = Video(item["video_id"], item["title"], item["published"], item["description"])
            try:
                post_discord(destination, build_payload(video))
            except MonitorError as error:
                failed.add(destination.key)
                failures.append(f"目的地 {index}：{error}")
                continue
            item["delivered_to"].append(destination.key)
            if active_keys.issubset(set(item["delivered_to"])):
                state["pending"].remove(item)
            # 每一個目的地送成功即保存；保存失敗直接中止，不發下一則。
            storage.save(state)
            sent += 1
        if item in state["pending"] and active_keys.issubset(set(item["delivered_to"])):
            # 已移除失敗 Webhook 時，可結清剩餘目的地已送達的影片。
            state["pending"].remove(item)
            storage.save(state)

    write_summary(lines + [f"- 本次成功推播：{sent} 則；尚待完成：{len(state['pending'])} 部。", *[f"- {message}" for message in failures]])
    if failures:
        raise MonitorError("部分 Discord 目的地失敗；成功紀錄已保存，待送項目會在下次 scan 重試。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("scan", "dry-run", "test"), default=os.getenv("YOUTUBE_MONITOR_MODE") or "scan")
    args = parser.parse_args(argv)
    try:
        if args.mode not in ("scan", "dry-run", "test"):
            raise MonitorError("YOUTUBE_MONITOR_MODE 必須是 scan、dry-run 或 test。")
        channel_id = os.getenv("YOUTUBE_CHANNEL_ID", "").strip() or DEFAULT_CHANNEL_ID
        if not CHANNEL_PATTERN.fullmatch(channel_id):
            raise MonitorError("YOUTUBE_CHANNEL_ID 必須是 UC 開頭的 24 字元 ID，不是 @帳號代碼或網址。")
        destinations = [] if args.mode == "dry-run" else parse_webhooks(os.getenv("OFFICIAL_DISCORD_WEBHOOK_URLS", ""))
        if args.mode == "test":
            latest = fetch_videos(channel_id)[-1]
            failures = []
            for index, destination in enumerate(destinations, 1):
                try:
                    post_discord(destination, build_payload(latest, test=True))
                except MonitorError as error:
                    failures.append(f"目的地 {index}：{error}")
            write_summary([f"- test：{len(destinations) - len(failures)} 個目的地收到測試影片卡片。", "- 不讀寫去重紀錄；重跑 test 會再送一則測試訊息。", *[f"- {failure}" for failure in failures]])
            if failures:
                raise MonitorError("部分測試推播失敗。")
            return 0
        storage = GitHubState(
            os.getenv("GH_REPOSITORY", "").strip() or os.getenv("GITHUB_REPOSITORY", "").strip(),
            os.getenv("GH_STATE_TOKEN", "").strip() or os.getenv("GITHUB_TOKEN", "").strip(),
            channel_id,
            os.getenv("YOUTUBE_STATE_ISSUE_NUMBER", "").strip(),
        )
        scan(storage, channel_id, destinations, dry_run=args.mode == "dry-run")
        return 0
    except MonitorError as error:
        write_summary([f"- 執行失敗：{error}"])
        return 1


if __name__ == "__main__":
    sys.exit(main())
