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
  const tokensIn = (line) => [...line.matchAll(/(?<![A-Za-z0-9_-])([A-Za-z0-9][A-Za-z0-9_-]{5,31})(?![A-Za-z0-9_-])/g)];
  const isExpiredLine = (line) => /失效|過期|过期|無效|无效|不能用|無法兌換|无法兑换|不能兌換|不能兑换|\bexpired\b|\binvalid\b|not\s+working/i.test(line);
  const lines = text.split("\n").filter((line) => !isExpiredLine(line));
  const codeLabel = /兌換碼|兑换码|序號|序号|禮包碼|礼包码|\bcodes?\b|\bcoupons?\b/i;
  const isCodeListLine = (line) => {
    const tokens = tokensIn(line);
    if (!tokens.length) return false;
    let remainder = line;
    for (const token of [...tokens].reverse()) {
      remainder = remainder.slice(0, token.index) + remainder.slice(token.index + token[0].length);
    }
    return !remainder.replace(/[\s`'"「」『』【】()（）:：,，。.!！;；、/|｜•·*\[\]-]/g, "");
  };
  // Multi-code lists can include ten-letter random codes with no digits.
  // Requiring a list keeps standalone English chat out of this exception.
  const listCodes = new Set(lines.filter(isCodeListLine).flatMap((line) =>
    tokensIn(line).map((match) => normalizeCode(match[1])).filter((token) =>
      looksLikeCode(token) && (/\d/.test(token) || /^[A-Z]{10}$/.test(token) || /^WWM/.test(token)),
    ),
  ));
  const codes = new Set();
  let followsCodeHeading = false;
  for (const line of lines) {
    const tokens = tokensIn(line);
    const codeListLine = isCodeListLine(line);
    const hasLabel = codeLabel.test(line);
    const labelled = hasLabel || (followsCodeHeading && codeListLine);
    if (hasLabel && !tokens.some((match) => looksLikeCode(match[1]))) {
      followsCodeHeading = true;
    } else if (!codeListLine && !hasLabel && line.trim()) {
      followsCodeHeading = false;
    }
    for (const match of tokens) {
      const token = match[1];
      const letterListCode = listCodes.size >= 2 && codeListLine &&
        /^[A-Za-z]{10}$/.test(token) && looksLikeCode(normalizeCode(token));
      if (!looksLikeCode(token) && !letterListCode) continue;
      const randomCode = /^[A-Za-z0-9]{10}$/.test(token) && /\d/.test(token);
      const standalone = line.replace(/[\s`'"「」『』【】()（）:：,，。.!！]/g, "") === token;
      if (!(labelled || letterListCode || randomCode || (standalone && /\d/.test(token)) || /^WWM/i.test(token))) continue;
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

function floorMarkers(html) {
  return [...html.matchAll(/<[a-z][^>]*\bdata-floor\s*=\s*["'](\d+)["'][^>]*>/gi)];
}

function hasClass(tag, name) {
  return (attribute(tag, "class") ?? "").split(/\s+/).includes(name);
}

function contentOpening(html) {
  return html.match(/<([a-z][\w:-]*)\b(?=[^>]*\bclass=["'][^"']*\bc-article__content\b[^"']*["'])[^>]*>/i);
}

function isDeletedPost(html) {
  const text = htmlToText(html).replace(/\s+/g, "");
  // The site's deletion notice can include the author's linked account name.
  // A disabled/collapsed wrapper alone is NOT proof that a post was deleted.
  return /(?:此)?文章已由(?:原作者|作者|板主|版主|管理員|站方)(?:[（(][^()（）]{0,100}[)）])?刪除|(?:此文章|本篇文章|本文|文章)已(?:被|遭)?刪除|[【〖]刪除[】〗]/.test(text);
}

function replyBlocks(html) {
  const blocks = [];
  const sections = /<section\b[^>]*>/gi;
  for (let match; (match = sections.exec(html));) {
    const opening = match[0];
    const id = attribute(opening, "id") ?? "";
    const postId = id.match(/^(?:post|disable)_(\d+)$/)?.[1]
      ?? attribute(opening, "data-sn");
    if (!postId && !hasClass(opening, "c-section")) continue;
    const range = elementRange(html, match.index, "section");
    const block = html.slice(match.index, range.end);
    // Only inspect the header for a floor number; quotes and comments can also
    // contain data-floor attributes, but they are not independent reply floors.
    const bodyStart = block.search(/<article\b|<[a-z][^>]*\bclass=["'][^"']*\b(?:c-post__body|c-article__content|c-reply)\b/i);
    const header = bodyStart < 0 ? block : block.slice(0, bodyStart);
    const markers = floorMarkers(header);
    const labelled = markers.filter((marker) => hasClass(marker[0], "floor"));
    const floors = [...new Set((labelled.length ? labelled : markers).map((marker) => Number(marker[1])))];
    if (!floors.length) {
      if (postId) throw new Error(`Bahamut post ${postId} floor was not found; cursor unchanged`);
      continue;
    }
    if (floors.length !== 1) throw new Error("Bahamut reply header has conflicting floors; cursor unchanged");
    blocks.push({ floor: floors[0], postId, html: block });
    sections.lastIndex = range.end;
  }
  if (blocks.length) return blocks;

  // Compatibility with minimal pages that omit section wrappers. Consecutive
  // markers for the same floor belong to one block, not several partial posts.
  const markers = floorMarkers(html).filter((marker, index, all) =>
    index === 0 || marker[1] !== all[index - 1][1]);
  return markers.map((marker, index) => ({
    floor: Number(marker[1]), postId: undefined,
    html: html.slice(marker.index, markers[index + 1]?.index ?? html.length),
  }));
}

export function parseBahamutPage(html, sourceUrl = BAHAMUT_LATEST_URL, { afterFloor = 0 } = {}) {
  const clean = html.replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1>/gi, " ");
  const blocks = replyBlocks(clean);
  if (!blocks.length) throw new Error("Bahamut reply floors were not found (blocked page or changed markup)");

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
  const groups = new Map();
  for (const block of blocks) {
    const group = groups.get(block.floor) ?? [];
    group.push(block);
    groups.set(block.floor, group);
  }
  for (const [floor, variants] of groups) {
    if (!Number.isSafeInteger(floor) || floor < 1) throw new Error("Bahamut reply floor is invalid");
    const postIds = [...new Set(variants.map((block) => block.postId).filter(Boolean))];
    if (postIds.length > 1) throw new Error(`Bahamut floor ${floor} has conflicting post IDs; cursor unchanged`);
    const postId = postIds[0];
    const url = /^\d+$/.test(postId ?? "")
      ? `https://${HOST}/Co.php?bsn=75703&sn=${postId}` : pageUrl;
    // These floors have already been processed (or are the excluded first-floor
    // summary). Their body layout must not block later, unseen replies.
    if (floor === 1 || floor <= afterFloor) {
      posts.push({ floor, url, codes: [] });
      continue;
    }
    // Folded posts can appear twice: a disable_ placeholder, then a post_ body
    // with display:none. Read the real body; do not treat the placeholder as a post.
    const bodies = variants.map((block) => ({ block, content: contentOpening(block.html) }))
      .filter((item) => item.content);
    if (!bodies.length) {
      if (!variants.some((block) => isDeletedPost(block.html))) {
        throw new Error(`Bahamut floor ${floor} content was not found (page ${pageNumber}, post ${postId ?? "unknown"}); cursor unchanged`);
      }
      posts.push({ floor, url, codes: [] });
      continue;
    }
    const codes = new Set();
    for (const { block, content } of bodies) {
      const range = elementRange(block.html, content.index, content[1]);
      for (const code of extractReplyCodes(block.html.slice(range.contentStart, range.contentEnd))) codes.add(code);
    }
    posts.push({ floor, url, codes: [...codes] });
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
    const page = parseBahamutPage(html, url, { afterFloor: cursor ?? 0 });
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
