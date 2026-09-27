import {
  SOURCE_URL,
  fetchSourceText,
  htmlToText,
  looksLikeCode,
  normalizeCode,
} from "./monitor.js";

export const BAHAMUT_LATEST_URL = `${SOURCE_URL}&last=1`;
const HOST = "forum.gamer.com.tw";

function attribute(tag, name) {
  return tag.match(new RegExp(`\\b${name}\\s*=\\s*["']([^"']*)["']`, "i"))?.[1];
}

// Balance nested tags instead of ending a post at the first </div>.
function elementRange(html, start, tagName) {
  const tags = new RegExp(`<\\/?${tagName}\\b[^>]*>`, "gi");
  tags.lastIndex = start;
  let depth = 0;
  let contentStart;
  for (let tag; (tag = tags.exec(html));) {
    if (contentStart === undefined) contentStart = tags.lastIndex;
    depth += tag[0].startsWith("</") ? -1 : 1;
    if (depth === 0) return { contentStart, contentEnd: tag.index, end: tags.lastIndex };
  }
  throw new Error(`Bahamut markup has an unclosed ${tagName}`);
}

function removeElements(html, pattern, tagName) {
  for (let match; (match = pattern.exec(html));) {
    const range = elementRange(html, match.index, tagName);
    html = html.slice(0, match.index) + " " + html.slice(range.end);
    pattern.lastIndex = 0;
  }
  return html;
}

export function extractReplyCodes(content) {
  let clean = removeElements(content, /<blockquote\b[^>]*>/gi, "blockquote");
  for (const name of ["strike", "del", "s", "a"]) {
    clean = removeElements(clean, new RegExp(`<${name}\\b[^>]*>`, "gi"), name);
  }
  // A crossed-out code is not a new candidate. It does not expire other sources.
  for (const name of ["span", "div", "font"]) {
    clean = removeElements(clean,
      new RegExp(`<${name}\\b(?=[^>]*\\bstyle=["'][^"']*line-through)[^>]*>`, "gi"), name);
  }
  const text = htmlToText(clean)
    .replace(/(?:https?:\/\/|www\.)\S+/gi, " ");
  const codes = new Set();
  for (const line of text.split("\n")) {
    if (/失效|過期|过期|無效|无效|不能用|無法兌換|无法兑换|不能兌換|不能兑换|\bexpired\b|\binvalid\b|not\s+working/i.test(line)) continue;
    const labelled = /兌換碼|兑换码|序號|序号|禮包碼|礼包码|\bcodes?\b|\bcoupons?\b/i.test(line);
    for (const match of line.matchAll(/(?<![A-Za-z0-9_/-])([A-Za-z0-9][A-Za-z0-9_-]{5,31})(?![A-Za-z0-9_-])/g)) {
      const token = match[1];
      if (!looksLikeCode(token)) continue;
      const randomCode = /^[A-Za-z0-9]{10}$/.test(token) && /\d/.test(token);
      const standalone = line.replace(/[\s`'"「」『』【】()（）:：,，。.!！]/g, "") === token;
      // Plain English chat is not a code. Letter-only codes require a label.
      if (!(labelled || randomCode || (standalone && /\d/.test(token)) || /^WWM/i.test(token))) continue;
      codes.add(normalizeCode(token));
    }
  }
  return [...codes];
}

function threadUrl(value, base) {
  try {
    const url = new URL(value.replace(/&amp;/gi, "&"), base);
    if (url.protocol !== "https:" || url.hostname !== HOST ||
        url.pathname !== "/C.php" || url.searchParams.get("bsn") !== "75703" ||
        url.searchParams.get("snA") !== "388") return null;
    return url;
  } catch { return null; }
}

export function parseBahamutPage(html, sourceUrl = BAHAMUT_LATEST_URL) {
  const clean = html.replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1>/gi, " ");
  const markers = [...clean.matchAll(/<[a-z][^>]*\bdata-floor\s*=\s*["'](\d+)["'][^>]*>/gi)];
  if (!markers.length) throw new Error("Bahamut reply floors were not found (blocked page or changed markup)");

  const pages = [];
  let currentPage;
  for (const match of clean.matchAll(/<a\b[^>]*>/gi)) {
    const href = attribute(match[0], "href");
    if (!href) continue;
    const url = threadUrl(href, sourceUrl);
    const page = Number(url?.searchParams.get("page"));
    if (!Number.isSafeInteger(page) || page < 1 || page > 10000) continue;
    pages.push(page);
    if (/\bpagenow\b/i.test(attribute(match[0], "class") ?? "")) currentPage = page;
  }
  const requestedPage = Number(new URL(sourceUrl).searchParams.get("page"));
  const pageNumber = currentPage ?? (requestedPage || Math.max(1, ...pages));
  const pageUrl = `${SOURCE_URL}&page=${pageNumber}`;
  const posts = [];
  for (let index = 0; index < markers.length; index += 1) {
    const marker = markers[index];
    const floor = Number(marker[1]);
    if (!Number.isSafeInteger(floor) || floor < 1) throw new Error("Bahamut reply floor is invalid");
    const block = clean.slice(marker.index, markers[index + 1]?.index ?? clean.length);
    const content = block.match(/<div\b(?=[^>]*\bclass=["'][^"']*\bc-article__content\b[^"']*["'])[^>]*>/i);
    if (!content) {
      if (/本文已被刪除|文章已被刪除|此文章已由原作者刪除/.test(block)) {
        posts.push({ floor, url: pageUrl, codes: [] });
        continue;
      }
      throw new Error(`Bahamut floor ${floor} content was not found`);
    }
    const range = elementRange(block, content.index, "div");
    const sectionStart = clean.lastIndexOf("<section", marker.index);
    const section = sectionStart >= 0 ? clean.slice(sectionStart, marker.index) : "";
    const opening = section.match(/^<section\b[^>]*>/i)?.[0] ?? "";
    const postId = attribute(opening, "data-sn") ?? attribute(opening, "id")?.match(/^post_(\d+)$/)?.[1];
    const url = /^\d+$/.test(postId ?? "")
      ? `https://${HOST}/Co.php?bsn=75703&sn=${postId}` : pageUrl;
    posts.push({ floor, url, codes: floor === 1 ? [] : extractReplyCodes(block.slice(range.contentStart, range.contentEnd)) });
  }
  // An unexpected ordering must not move the saved cursor past missing replies.
  if (posts.some((post, i) => i > 0 && post.floor <= posts[i - 1].floor)) {
    throw new Error("Bahamut reply floors are not in ascending order");
  }
  return { posts, pageNumber };
}

export async function fetchBahamutReplies(previous, { fetchImpl = fetch, maxPages = 8 } = {}) {
  if (!Number.isInteger(maxPages) || maxPages < 1 || maxPages > 12) {
    throw new Error("BAHAMUT_MAX_PAGES must be an integer from 1 to 12");
  }
  const cursor = previous?.initialized ? previous.lastFloor : null;
  if (cursor !== null && (!Number.isSafeInteger(cursor) || cursor < 1)) {
    throw new Error("Saved Bahamut cursor is invalid; state was not changed");
  }
  const posts = new Map();
  let url = BAHAMUT_LATEST_URL;
  for (let count = 1; count <= maxPages; count += 1) {
    const html = await fetchSourceText({
      name: "Bahamut", url, host: HOST, language: "zh-TW,zh;q=0.9",
    }, fetchImpl);
    const page = parseBahamutPage(html, url);
    for (const post of page.posts) posts.set(post.floor, post);
    const oldest = page.posts[0].floor;
    if (cursor === null || oldest <= cursor) {
      const ordered = [...posts.values()].sort((a, b) => a.floor - b.floor);
      return {
        posts: ordered,
        lastFloor: Math.max(cursor ?? 0, ...ordered.map((post) => post.floor)),
        pagesRead: count,
      };
    }
    if (page.pageNumber <= 1) throw new Error("Bahamut pagination could not reach the saved reply; cursor unchanged");
    url = `${SOURCE_URL}&page=${page.pageNumber - 1}`;
  }
  throw new Error(`Bahamut needs more than ${maxPages} pages to catch up; cursor unchanged`);
}
