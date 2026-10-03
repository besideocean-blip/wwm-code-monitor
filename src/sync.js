import {
  YAR_URL,
  fetchYarEntries,
  normalizeCode,
} from "./monitor.js";
import { fetchBahamutReplies } from "./bahamut.js";
import { reconcileAutomaticState, reconcileManualState } from "./combined-state.js";
import { appendFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const STATE_TITLE = "[WWM Monitor] State - do not edit";
const STATE_START = "<!-- wwm-code-state:start -->";
const STATE_END = "<!-- wwm-code-state:end -->";
const MAX_STATE_BYTES = 60_000;
const MAX_EMBED_DESCRIPTION = 3900;
const ANNOUNCEMENT_SOURCE_URL = YAR_URL;

function requireEnvironment(name) {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`Missing environment variable: ${name}`);
  return value;
}

function validateRepository(value) {
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(value)) {
    throw new Error("GitHub repository format is invalid");
  }
  return value.split("/").map(encodeURIComponent).join("/");
}

function validateWebhookUrl(value) {
  const url = new URL(value);
  const parts = url.pathname.split("/").filter(Boolean);
  if (
    url.protocol !== "https:" ||
    url.hostname !== "discord.com" ||
    url.search ||
    url.hash ||
    parts.length !== 4 ||
    parts[0] !== "api" ||
    parts[1] !== "webhooks" ||
    !/^\d+$/.test(parts[2]) ||
    !/^[A-Za-z0-9._-]{20,}$/.test(parts[3])
  ) {
    throw new Error("DISCORD_WEBHOOK_URL format is invalid");
  }
  return url.toString();
}

function parseWebhookUrls() {
  const raw =
    process.env.DISCORD_WEBHOOK_URLS?.trim() ||
    process.env.DISCORD_WEBHOOK_URL?.trim();

  if (!raw) {
    throw new Error(
      "Missing environment variable: DISCORD_WEBHOOK_URLS or DISCORD_WEBHOOK_URL",
    );
  }

  const urls = raw
    .split(/\r?\n/)
    .map((value) => value.trim())
    .filter(Boolean)
    .map(validateWebhookUrl);

  return [...new Set(urls)];
}

function parseManualEntries(value) {
  const tokens = value?.match(/[A-Za-z0-9][A-Za-z0-9_-]{5,31}/g) ?? [];
  const entries = new Map();

  for (const token of tokens) {
    entries.set(normalizeCode(token), {
      code: token.trim(),
      status: "active",
    });
  }

  return [...entries.values()];
}

async function githubRequest(repository, token, path, options = {}) {
  const response = await fetch(
    `https://api.github.com/repos/${repository}${path}`,
    {
      ...options,
      headers: {
        accept: "application/vnd.github+json",
        authorization: `Bearer ${token}`,
        "content-type": "application/json",
        "user-agent": "wwm-code-monitor",
        "x-github-api-version": "2022-11-28",
        ...options.headers,
      },
      signal: AbortSignal.timeout(15_000),
    },
  );

  if (!response.ok) {
    throw new Error(`GitHub API returned HTTP ${response.status}`);
  }
  if (response.status === 204) return null;
  return response.json();
}

function encodeState(state) {
  const json = JSON.stringify(state, null, 2);
  const body = `${STATE_START}\n\`\`\`json\n${json}\n\`\`\`\n${STATE_END}`;
  if (Buffer.byteLength(body, "utf8") > MAX_STATE_BYTES) {
    throw new Error("State data is too large for the GitHub Issue");
  }
  return body;
}

function decodeState(body) {
  const start = body.indexOf(STATE_START);
  const end = body.indexOf(STATE_END);
  if (start < 0 || end <= start) {
    throw new Error("GitHub state issue format is invalid");
  }

  const section = body.slice(start + STATE_START.length, end);
  const match = section.match(/```json\s*([\s\S]*?)\s*```/);
  if (!match) throw new Error("GitHub state JSON block was not found");
  return JSON.parse(match[1]);
}

async function loadStateIssue(repository, token) {
  const issues = await githubRequest(
    repository,
    token,
    "/issues?state=open&per_page=100",
  );
  const issue = issues.find(
    (item) => !item.pull_request && item.title === STATE_TITLE,
  );
  if (!issue) return { issue: null, state: null };
  return { issue, state: decodeState(issue.body ?? "") };
}

async function saveState(repository, token, issue, state) {
  const body = JSON.stringify({ title: STATE_TITLE, body: encodeState(state) });
  if (issue) {
    return githubRequest(repository, token, `/issues/${issue.number}`, {
      method: "PATCH",
      body,
    });
  }
  return githubRequest(repository, token, "/issues", {
    method: "POST",
    body,
  });
}

async function postDiscord(webhookUrl, payload) {
  const response = await fetch(webhookUrl, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      username: "燕雲十六聲兌換碼",
      allowed_mentions: { parse: [] },
      ...payload,
    }),
    signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok) {
    throw new Error(`Discord Webhook returned HTTP ${response.status}`);
  }
}

async function postDiscordAll(webhookUrls, payload) {
  const results = await Promise.allSettled(
    webhookUrls.map((webhookUrl) =>
      postDiscord(webhookUrl, payload),
    ),
  );

  let successCount = 0;
  let failureCount = 0;

  results.forEach((result, index) => {
    if (result.status === "fulfilled") {
      successCount += 1;
    } else {
      failureCount += 1;
      console.warn(
        `Discord webhook ${index + 1} failed: ${result.reason?.message ?? result.reason}`,
      );
    }
  });

  console.log(
    `Discord broadcast complete: ${successCount} succeeded, ${failureCount} failed.`,
  );

  if (successCount === 0) {
    throw new Error("All Discord webhooks failed.");
  }
}

function chunkCodeLines(entries) {
  const chunks = [];
  let current = [];
  let currentLength = 0;

  for (const entry of entries) {
    const line = `\`${entry.code}\``;
    const nextLength = currentLength + line.length + 1;
    if (current.length > 0 && nextLength > MAX_EMBED_DESCRIPTION) {
      chunks.push(current);
      current = [];
      currentLength = 0;
    }
    current.push(line);
    currentLength += line.length + 1;
  }

  if (current.length > 0) chunks.push(current);
  return chunks;
}

export function buildCodeEmbeds(title, entries, source = {}) {
  return chunkCodeLines(entries).map((lines) => ({
    embeds: [
      {
        title,
        description: lines.join("\n"),
        color: 0x2f9e44,
        url: source.url ?? ANNOUNCEMENT_SOURCE_URL,
        timestamp: new Date().toISOString(),
        ...(source.source === "bahamut" ? {
          footer: { text: `巴哈姆特第 ${source.floor} 樓` },
        } : {}),
      },
    ],
  }));
}

async function postCodeEmbeds(webhookUrls, title, entries, source) {
  for (const payload of buildCodeEmbeds(title, entries, source)) {
    await postDiscordAll(webhookUrls, payload);
  }
}

function isSourceScanEnabled() {
  return process.env.SOURCE_SCAN_ENABLED !== "false";
}

export async function scanSources(previousState, {
  fetchYar = fetchYarEntries,
  fetchBahamut = fetchBahamutReplies,
  bahamutEnabled = process.env.BAHAMUT_SCAN_ENABLED !== "false",
  maxPages = Number(process.env.BAHAMUT_MAX_PAGES || 8),
} = {}) {
  const sources = [
    { name: "YAR", key: "yarEntries", run: () => fetchYar() },
    ...(bahamutEnabled ? [{
      name: "Bahamut", key: "bahamut",
      run: () => fetchBahamut(previousState?.bahamut, { maxPages }),
    }] : []),
  ];
  const output = { yarEntries: null, bahamut: null, failures: [] };
  const results = await Promise.allSettled(sources.map((source) => source.run()));
  for (const [index, result] of results.entries()) {
    const source = sources[index];
    if (result.status === "fulfilled") {
      output[source.key] = result.value;
      console.log(`${source.name} source loaded.`);
    } else {
      const message = `${source.name}: ${result.reason?.message ?? "source read failed"}`;
      output.failures.push(message);
      console.warn(`Source failed: ${message}`);
    }
  }
  if (!output.yarEntries && !output.bahamut) {
    throw new Error(`All configured sources failed; state unchanged. ${output.failures.join(" | ")}`);
  }
  return output;
}

async function writeScanSummary(snapshot, result, dryRun) {
  const count = result.announcements.reduce((total, item) => total + item.entries.length, 0);
  const newBaseline = `${dryRun ? "預計" : "本次"}建立，既有內容不公告`;
  const lines = [
    `## 兌換碼掃描${dryRun ? "（預覽，不發送、不寫入）" : ""}`,
    "",
    `- YAR：${snapshot.yarEntries ? `讀取成功，${snapshot.yarEntries.filter((item) => item.status === "active").length} 組可用碼` : "讀取失敗，保留原紀錄"}`,
    `- 巴哈：${snapshot.bahamut ? `讀取成功，${snapshot.bahamut.pagesRead} 頁，最新第 ${snapshot.bahamut.lastFloor} 樓` : "未讀取成功或已停用，保留原進度"}`,
    `- 本次${dryRun ? "預計" : "待"}通知：${count} 組`,
    `- YAR 基準：${result.yarFirstRun ? newBaseline : "未變更"}`,
    `- 巴哈基準：${result.bahamutFirstRun ? newBaseline : "未變更"}`,
    ...snapshot.failures.map((message) => `- 注意：${message.replace(/[\r\n]/g, " ")}`),
    "",
  ];
  console.log(lines.join("\n"));
  if (process.env.GITHUB_STEP_SUMMARY) await appendFile(process.env.GITHUB_STEP_SUMMARY, lines.join("\n"));
}

export async function main() {
  const repository = validateRepository(requireEnvironment("GH_REPOSITORY"));
  const githubToken = requireEnvironment("GH_STATE_TOKEN");
  const dryRun = process.env.DRY_RUN === "true";
  const webhookUrls = dryRun ? null : parseWebhookUrls();
  const now = new Date().toISOString();
  const manualEntries = parseManualEntries(process.env.MANUAL_CODES);

  if (manualEntries.length > 0) {
    const stored = await loadStateIssue(repository, githubToken);
    const result = reconcileManualState(
      stored.state ?? { initialized: false, codes: [] },
      manualEntries,
      now,
    );

    encodeState(result.state); // Validate size before any external announcement.
    if (!dryRun && result.newActive.length > 0) {
      await postCodeEmbeds(webhookUrls, "玩家回報新兌換碼", result.newActive);
    }
    if (!dryRun) await saveState(repository, githubToken, stored.issue, result.state);

    console.log(
      `${dryRun ? "Preview: " : ""}Manual report checked ${manualEntries.length} code(s), found ${result.newActive.length} new.`,
    );
    return;
  }

  if (!isSourceScanEnabled()) {
    console.log(
      "Source scanning is disabled. Manual /report submissions still work.",
    );
    return;
  }

  const stored = await loadStateIssue(repository, githubToken);
  const snapshot = await scanSources(stored.state);
  const result = reconcileAutomaticState(stored.state, snapshot, now);
  encodeState(result.state);
  await writeScanSummary(snapshot, result, dryRun);

  if (dryRun) return;

  if (!stored.state?.initialized) {
    await postDiscordAll(webhookUrls, {
      embeds: [
        {
          title: "兌換碼監控已建立",
          description: `已建立 ${result.state.codes.length} 組兌換碼基準資料。之後只會通知新出現的兌換碼。`,
          color: 0x228be6,
          url: ANNOUNCEMENT_SOURCE_URL,
          timestamp: new Date().toISOString(),
        },
      ],
    });
  }
  for (const announcement of result.announcements) {
    await postCodeEmbeds(webhookUrls, "發現新兌換碼", announcement.entries, announcement);
  }
  // Keep the previous cursor/seen history if Discord fails, so a later scan retries.
  await saveState(repository, githubToken, stored.issue, result.state);
  console.log("Source sync complete; state saved.");
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
