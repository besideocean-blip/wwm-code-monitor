const SOURCE_URL = "https://forum.gamer.com.tw/C.php?bsn=75703&snA=388";
const SOURCE_HOST = "forum.gamer.com.tw";
const ARLEN_URL = "https://www.arlenfuture.com/games/where-winds-meet-codes/";
const ARLEN_HOST = "www.arlenfuture.com";
const PC_GAMER_URL =
  "https://www.pcgamer.com/games/action/where-winds-meet-codes/";
const PC_GAMER_HOST = "www.pcgamer.com";
const YAR_URL = "https://codes.yar.gg/";
const YAR_API_URL = "https://codes.yar.gg/api/codes";
const YAR_API_HOST = "codes.yar.gg";
const YAR_FALLBACK_URL = "https://codes-backend.wwmcodes.workers.dev/v1/codes";
const MAX_SOURCE_BYTES = 2 * 1024 * 1024;
const MAX_REDIRECTS = 3;

const BLOCKED_TOKENS = new Set([
  "ACTIVE",
  "BUTTON",
  "COPY",
  "DISCORD",
  "EXPIRED",
  "FACEBOOK",
  "INSTAGRAM",
  "INVALID",
  "AUTHORITY",
  "MAGAZINE",
  "OFFICIAL",
  "REDEMPTION",
  "TWITTER",
  "VALID",
  "YOUTUBE",
]);

export function normalizeCode(value) {
  return value.trim().toUpperCase();
}

function decodeHtmlEntities(text) {
  const named = {
    amp: "&",
    apos: "'",
    gt: ">",
    lt: "<",
    nbsp: " ",
    quot: '"',
  };

  return text.replace(
    /&(#x[\da-f]+|#\d+|[a-z]+);/gi,
    (entity, value) => {
      if (value.startsWith("#x")) {
        return String.fromCodePoint(Number.parseInt(value.slice(2), 16));
      }
      if (value.startsWith("#")) {
        return String.fromCodePoint(Number.parseInt(value.slice(1), 10));
      }
      return named[value.toLowerCase()] ?? entity;
    },
  );
}

export function htmlToText(html) {
  return decodeHtmlEntities(
    html
      .replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1>/gi, " ")
      .replace(/<(br|\/div|\/p|\/li|\/h[1-6]|\/tr)\b[^>]*>/gi, "\n")
      .replace(/<[^>]+>/g, " "),
  )
    .replace(/\r/g, "")
    .replace(/[ \t]+/g, " ")
    .replace(/\n{2,}/g, "\n")
    .trim();
}

export function looksLikeCode(value) {
  const code = value.trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{5,31}$/.test(code)) return false;

  const normalized = normalizeCode(code);
  if (BLOCKED_TOKENS.has(normalized)) return false;
  if (/\.(COM|NET|ORG|GG|IO)$/i.test(normalized)) return false;
  if (/^WWM[A-Za-z0-9_-]{3,28}$/i.test(code)) return true;

  const hasLetter = /[A-Za-z]/.test(code);
  const hasDigit = /\d/.test(code);
  const isUppercaseLettersOnly = /^[A-Z]{8,24}$/.test(code);
  return hasLetter && (hasDigit || isUppercaseLettersOnly);
}

function extractCodes(text) {
  const tokens = text?.match(/[A-Za-z0-9][A-Za-z0-9_-]{5,31}/g) ?? [];
  const codes = new Map();
  for (const token of tokens) {
    if (looksLikeCode(token)) codes.set(normalizeCode(token), token);
  }
  return [...codes.values()];
}

function setCodeEntry(entries, code, status) {
  const normalized = normalizeCode(code);
  const previous = entries.get(normalized);
  if (previous?.status === "expired") return;

  entries.set(normalized, {
    code: normalized,
    status,
  });
}

function separateExpiredMarkup(html) {
  const expiredChunks = [];
  let activeHtml = html.replace(
    /<(strike|s|del)\b[^>]*>([\s\S]*?)<\/\1>/gi,
    (_match, _tag, content) => {
      expiredChunks.push(content);
      return " ";
    },
  );

  activeHtml = activeHtml.replace(
    /<([a-z][\w:-]*)\b(?=[^>]*\bstyle=["'][^"']*text-decoration(?:-line)?\s*:[^"']*line-through)[^>]*>([\s\S]*?)<\/\1>/gi,
    (_match, _tag, content) => {
      expiredChunks.push(content);
      return " ";
    },
  );

  return { activeHtml, expiredHtml: expiredChunks.join("\n") };
}

export function parseBahamutThread(html) {
  const firstFloorIndex = html.search(/data-floor=["']1["']/i);
  if (firstFloorIndex < 0) {
    throw new Error("Bahamut first floor was not found");
  }

  const firstFloorHtml = html.slice(firstFloorIndex);
  const articleMatch = firstFloorHtml.match(
    /<div\s+class=["'][^"']*\bc-article__content\b[^"']*["'][^>]*>([\s\S]*?)<\/article>/i,
  );
  if (!articleMatch) {
    throw new Error("Bahamut first floor content was not found");
  }

  const { activeHtml, expiredHtml } = separateExpiredMarkup(articleMatch[1]);
  const entries = new Map();

  for (const code of extractCodes(htmlToText(activeHtml))) {
    setCodeEntry(entries, code, "active");
  }
  for (const code of extractCodes(htmlToText(expiredHtml))) {
    setCodeEntry(entries, code, "expired");
  }

  return [...entries.values()];
}

export function parsePcGamerArticle(html) {
  const text = htmlToText(html);
  const activeStart = text.search(
    /Here are all the active codes currently available for Where Winds Meet/i,
  );
  const expiredStart = text.search(/Expired Where Winds Meet Codes/i);

  if (activeStart < 0) {
    throw new Error("PC Gamer active code section was not found");
  }
  if (expiredStart <= activeStart) {
    throw new Error("PC Gamer expired code section was not found");
  }

  const entries = new Map();
  for (const code of extractCodes(text.slice(activeStart, expiredStart))) {
    setCodeEntry(entries, code, "active");
  }
  for (const code of extractCodes(text.slice(expiredStart))) {
    setCodeEntry(entries, code, "expired");
  }

  return [...entries.values()];
}

export function parseArlenPage(html) {
  const text = htmlToText(html);
  const activeStart = text.search(/有效兌換碼|有效碼/i);
  const expiredStart = text.search(/失效兌換碼|失效碼/i);
  const entries = new Map();

  if (activeStart >= 0) {
    const activeSection =
      expiredStart > activeStart
        ? text.slice(activeStart, expiredStart)
        : text.slice(activeStart);
    for (const code of extractCodes(activeSection)) {
      setCodeEntry(entries, code, "active");
    }
  }

  if (expiredStart >= 0) {
    for (const code of extractCodes(text.slice(expiredStart))) {
      setCodeEntry(entries, code, "expired");
    }
  }

  const statusPattern =
    /([A-Za-z0-9][A-Za-z0-9_-]{5,31})\s*(有效|失效|active|valid|expired|invalid)\b/gi;
  for (const match of text.matchAll(statusPattern)) {
    if (!looksLikeCode(match[1])) continue;
    const status = /失效|expired|invalid/i.test(match[2])
      ? "expired"
      : "active";
    setCodeEntry(entries, match[1], status);
  }

  if (entries.size === 0) {
    throw new Error("Arlen code sections were not found");
  }

  return [...entries.values()];
}

export function parseYarCodesPayload(payload) {
  if (
    !payload ||
    !Array.isArray(payload.active) ||
    !Array.isArray(payload.expired) ||
    payload.active.length === 0
  ) {
    throw new Error("Yar code lists are missing or empty");
  }

  const entries = new Map();
  for (const [status, items] of [
    ["active", payload.active],
    ["expired", payload.expired],
  ]) {
    for (const item of items) {
      const code = item?.code;
      if (
        typeof code !== "string" ||
        !/^[A-Za-z0-9][A-Za-z0-9_-]{5,31}$/.test(code)
      ) {
        throw new Error("Yar returned an invalid code entry");
      }
      setCodeEntry(entries, code, status);
    }
  }

  return [...entries.values()];
}

function validateUrl(value, expectedHost, label) {
  const url = new URL(value);
  if (url.protocol !== "https:" || url.hostname !== expectedHost) {
    throw new Error(`${label} source must use the official HTTPS host`);
  }
  return url;
}

async function fetchEntriesFromSource(source, fetchImpl = fetch) {
  let url = validateUrl(source.url, source.host, source.name);

  for (let redirects = 0; redirects <= MAX_REDIRECTS; redirects += 1) {
    const response = await fetchImpl(url, {
      redirect: "manual",
      headers: {
        "accept-language": source.language,
        "user-agent": "WWM-Code-Monitor/2.2",
      },
      signal: AbortSignal.timeout(15_000),
    });

    if (response.status >= 300 && response.status < 400) {
      const location = response.headers.get("location");
      if (!location || redirects === MAX_REDIRECTS) {
        throw new Error(`${source.name} source redirected too many times`);
      }
      url = validateUrl(new URL(location, url), source.host, source.name);
      continue;
    }
    if (!response.ok) {
      throw new Error(`${source.name} source returned HTTP ${response.status}`);
    }

    const declaredSize = Number(response.headers.get("content-length") || 0);
    if (declaredSize > MAX_SOURCE_BYTES) {
      throw new Error(`${source.name} source exceeded 2 MB`);
    }

    const buffer = await response.arrayBuffer();
    if (buffer.byteLength > MAX_SOURCE_BYTES) {
      throw new Error(`${source.name} source exceeded 2 MB`);
    }

    return source.parse(new TextDecoder().decode(buffer));
  }

  throw new Error(`${source.name} source could not be loaded`);
}

export function fetchBahamutEntries() {
  return fetchEntriesFromSource({
    name: "Bahamut",
    url: SOURCE_URL,
    host: SOURCE_HOST,
    language: "zh-TW,zh;q=0.9",
    parse: parseBahamutThread,
  });
}

export function fetchArlenEntries() {
  return fetchEntriesFromSource({
    name: "Arlen",
    url: ARLEN_URL,
    host: ARLEN_HOST,
    language: "zh-TW,zh;q=0.9,en;q=0.8",
    parse: parseArlenPage,
  });
}

export function fetchSourceText(source, fetchImpl = fetch) {
  return fetchEntriesFromSource({ ...source, parse: (body) => body }, fetchImpl);
}

export async function fetchYarEntries(fetchImpl = fetch) {
  const failures = [];
  for (const [url, host] of [
    [YAR_API_URL, YAR_API_HOST],
    [YAR_FALLBACK_URL, new URL(YAR_FALLBACK_URL).hostname],
  ]) {
    try {
      return await fetchEntriesFromSource({
        name: "Yar",
        url,
        host,
        language: "en-US,en;q=0.9",
        parse: (body) => parseYarCodesPayload(JSON.parse(body)),
      }, fetchImpl);
    } catch (error) {
      failures.push(error.message);
    }
  }
  throw new Error(failures.join("; "));
}

export function fetchPcGamerEntries() {
  return fetchEntriesFromSource({
    name: "PC Gamer",
    url: PC_GAMER_URL,
    host: PC_GAMER_HOST,
    language: "en-US,en;q=0.9",
    parse: parsePcGamerArticle,
  });
}

export function reconcileState(
  previousState,
  currentEntries,
  now,
  sourceUrl = SOURCE_URL,
) {
  const initialized = previousState?.initialized === true;
  const known = new Map(
    (previousState?.codes ?? []).map((entry) => [
      normalizeCode(entry.code),
      entry,
    ]),
  );
  const newActive = [];

  for (const entry of currentEntries) {
    const normalized = normalizeCode(entry.code);
    const previous = known.get(normalized);

    if (entry.status === "expired") {
      known.delete(normalized);
      continue;
    }

    if (initialized && !previous && entry.status === "active") {
      newActive.push(entry);
    }

    known.set(normalized, {
      code: entry.code,
      status: entry.status,
      firstSeenAt: previous?.firstSeenAt ?? now,
      lastSeenAt: now,
    });
  }

  return {
    firstRun: !initialized,
    newActive,
    state: {
      initialized: true,
      sourceUrl,
      updatedAt: now,
      codes: [...known.values()].sort((a, b) =>
        a.code.localeCompare(b.code, "en", { sensitivity: "base" }),
      ),
    },
  };
}

export function reconcileSourceState(previousState, currentEntries, now, sourceUrl) {
  const previous = previousState ?? { initialized: false, codes: [] };
  const result = reconcileState(
    {
      ...previous,
      initialized:
        previous.initialized === true &&
        previous.scannedSourceUrl === sourceUrl,
    },
    currentEntries,
    now,
    sourceUrl,
  );

  return {
    ...result,
    state: { ...result.state, scannedSourceUrl: sourceUrl },
  };
}

export { ARLEN_URL, PC_GAMER_URL, SOURCE_URL, YAR_URL };
