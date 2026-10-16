import test from "node:test";
import assert from "node:assert/strict";
import { buildCodeEmbeds, scanSources, main } from "../src/sync.js";
import { YAR_URL } from "../src/monitor.js";
import { parseBahamutPage } from "../src/bahamut.js";
import { reconcileAutomaticState } from "../src/combined-state.js";

test("codes and their source remain readable with embeds hidden; keeps the existing card", () => {
  const entries = [{ code: "SK6HFW6T3T", status: "unverified" }];
  const source = { source: "bahamut", floor: 364, url: "https://forum.gamer.com.tw/Co.php?bsn=75703&sn=10364" };
  const payload = buildCodeEmbeds("發現新兌換碼", entries, source)[0];
  const embed = payload.embeds[0];
  assert.equal(payload.content,
    `**發現新兌換碼**\n\n\`SK6HFW6T3T\`\n\n巴哈姆特第 364 樓\n<${source.url}>`);
  assert.doesNotMatch(payload.content, /玩家分享|尚未驗證/);
  assert.equal(embed.title, "發現新兌換碼");
  assert.equal(embed.description, "`SK6HFW6T3T`");
  assert.equal(embed.color, 0x2f9e44);
  assert.equal(embed.url, source.url);
  assert.equal(embed.footer.text, "巴哈姆特第 364 樓");
  assert.equal(buildCodeEmbeds("發現新兌換碼", entries)[0].embeds[0].footer, undefined);
});

test("one source failure keeps the other; both failures produce a visible error", async () => {
  const yar = [{ code: "KNOWN2026", status: "active" }];
  const output = await scanSources(null, {
    fetchYar: async () => yar,
    fetchBahamut: async () => { throw new Error("HTTP 403"); },
  });
  assert.deepEqual(output.yarEntries, yar);
  assert.equal(output.bahamut, null);
  assert.match(output.failures[0], /403/);
  await assert.rejects(scanSources(null, {
    fetchYar: async () => { throw new Error("offline"); },
    fetchBahamut: async () => { throw new Error("HTTP 403"); },
  }), /All configured sources failed/);
});

test("one reply produces one card with all unseen codes, omitting duplicates and expired codes", () => {
  const html = `<section data-sn="10364"><a data-floor="364"></a><article><div class="c-article__content">
    SK6HFW6T3T<br>JN6NNWJN33<br>EEQJMDMJHX<br>sk6hfw6t3t<br>HM0820
  </div></article></section>`;
  const page = parseBahamutPage(html);
  const old = {
    initialized: true, scannedSourceUrl: YAR_URL,
    codes: [{ code: "JN6NNWJN33", status: "active" }],
    expiredCodes: ["HM0820"],
    bahamut: { initialized: true, lastFloor: 363 },
  };
  const result = reconcileAutomaticState(old, {
    yarEntries: null, bahamut: { posts: page.posts, lastFloor: 364, pagesRead: 1 },
  }, "2026-09-28T04:10:00Z");
  assert.equal(result.announcements.length, 1);
  const source = result.announcements[0];
  const cards = buildCodeEmbeds("發現新兌換碼", source.entries, source);
  assert.equal(cards.length, 1);
  assert.equal(cards[0].embeds[0].description, "`SK6HFW6T3T`\n`EEQJMDMJHX`");
  assert.deepEqual([...cards[0].content.matchAll(/`([^`]+)`/g)].map((match) => match[1]),
    ["SK6HFW6T3T", "EEQJMDMJHX"]);
  assert.equal(cards[0].embeds[0].footer.text, "巴哈姆特第 364 樓");
});

test("long multi-code replies respect the 2000-character content limit without losing or truncating codes", () => {
  const entries = Array.from({ length: 400 }, (_, i) => ({ code: `TEST${String(i).padStart(28, "0")}` }));
  const source = { source: "bahamut", floor: 364,
    url: "https://forum.gamer.com.tw/Co.php?bsn=75703&sn=10364" };
  const cards = buildCodeEmbeds("🎁 發現新兌換碼", entries, source);
  assert.ok(cards.length > 1);
  const visibleCodes = [];
  for (const card of cards) {
    assert.ok(card.content.length <= 2000);
    assert.ok(card.embeds[0].description.length <= 3900);
    assert.ok(card.content.endsWith(`<${source.url}>`));
    const codes = [...card.content.matchAll(/`([^`]+)`/g)].map((match) => match[1]);
    visibleCodes.push(...codes);
    assert.equal(card.embeds[0].description, codes.map((code) => `\`${code}\``).join("\n"));
  }
  assert.deepEqual(visibleCodes, entries.map((entry) => entry.code));
});

function installEnvironment(t, overrides) {
  const values = {
    GH_REPOSITORY: "example/repo", GH_STATE_TOKEN: "test-only-token",
    DISCORD_WEBHOOK_URL: "https://discord.com/api/webhooks/123/test_only_webhook_value_0000",
    DISCORD_WEBHOOK_URLS: "",
    MANUAL_CODES: "", DRY_RUN: "false", SOURCE_SCAN_ENABLED: "true",
    BAHAMUT_SCAN_ENABLED: "true", BAHAMUT_MAX_PAGES: "8", GITHUB_STEP_SUMMARY: "",
    ...overrides,
  };
  const old = Object.fromEntries(Object.keys(values).map((key) => [key, process.env[key]]));
  Object.assign(process.env, values);
  t.after(() => { for (const [key, value] of Object.entries(old)) {
    if (value === undefined) delete process.env[key]; else process.env[key] = value;
  } });
}

const previous = {
  initialized: true, scannedSourceUrl: YAR_URL,
  codes: [{ code: "KNOWN2026", status: "active" }],
  bahamut: { initialized: true, lastFloor: 363 },
};
const issue = { number: 1, title: "[WWM Monitor] State - do not edit",
  body: `<!-- wwm-code-state:start -->\n\`\`\`json\n${JSON.stringify(previous)}\n\`\`\`\n<!-- wwm-code-state:end -->` };

test("preview reads source content but never writes state or calls Discord", async (t) => {
  installEnvironment(t, { DRY_RUN: "true", DISCORD_WEBHOOK_URL: "" });
  const mutations = [];
  t.mock.method(globalThis, "fetch", async (url, options = {}) => {
    if (options.method && options.method !== "GET") mutations.push(String(url));
    const host = new URL(url).hostname;
    if (host === "api.github.com") return Response.json([issue]);
    if (host === "codes.yar.gg") return Response.json({ active: [{ code: "SK6HFW6T3T" }], expired: [] });
    if (host === "forum.gamer.com.tw") return new Response("blocked", { status: 403 });
    throw new Error("Unexpected request");
  });
  await main();
  assert.deepEqual(mutations, []);
});

test("Discord failure leaves state and cursor untouched so the notification can retry", async (t) => {
  installEnvironment(t, { MANUAL_CODES: "SK6HFW6T3T" });
  const mutations = [];
  t.mock.method(globalThis, "fetch", async (url, options = {}) => {
    const host = new URL(url).hostname;
    if (host === "api.github.com" && !options.method) return Response.json([issue]);
    if (host === "discord.com") return new Response("unavailable", { status: 503 });
    mutations.push(String(url));
    throw new Error("Unexpected write");
  });
  await assert.rejects(main(), /All Discord webhooks failed/);
  assert.deepEqual(mutations, []);
});

test("a normal automatic run posts a Bahamut card and only then saves its cursor", async (t) => {
  installEnvironment(t, {});
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, options = {}) => {
    const host = new URL(url).hostname;
    if (host === "api.github.com" && !options.method) return Response.json([issue]);
    if (host === "codes.yar.gg") return Response.json({ active: [{ code: "KNOWN2026" }], expired: [] });
    if (host === "forum.gamer.com.tw") return new Response(`
      <section data-sn="10363"><a data-floor="363"></a><article><div class="c-article__content">謝謝</div></article></section>
      <section data-sn="10364"><a data-floor="364"></a><article><div class="c-article__content">sk6hfw6t3t</div></article></section>`);
    if (host === "discord.com") {
      calls.push("discord");
      const payload = JSON.parse(options.body);
      assert.deepEqual(payload.allowed_mentions, { parse: [] });
      assert.equal(payload.embeds[0].footer.text, "巴哈姆特第 364 樓");
      assert.match(payload.content, /`SK6HFW6T3T`/);
      assert.match(payload.content, /巴哈姆特第 364 樓/);
      return new Response(null, { status: 204 });
    }
    if (host === "api.github.com" && options.method === "PATCH") {
      calls.push("state");
      const body = JSON.parse(options.body).body;
      assert.match(body, /"lastFloor": 364/);
      assert.match(body, /SK6HFW6T3T/);
      return Response.json({ number: 1 });
    }
    throw new Error("Unexpected request");
  });
  await main();
  assert.deepEqual(calls, ["discord", "state"]);
});

test("YAR automatic announcements carry the code in normal message content", async (t) => {
  installEnvironment(t, { BAHAMUT_SCAN_ENABLED: "false" });
  const messages = [];
  t.mock.method(globalThis, "fetch", async (url, options = {}) => {
    const host = new URL(url).hostname;
    if (host === "api.github.com" && !options.method) return Response.json([issue]);
    if (host === "codes.yar.gg") return Response.json({
      active: [{ code: "KNOWN2026" }, { code: "SK6HFW6T3T" }], expired: [],
    });
    if (host === "discord.com") {
      messages.push(JSON.parse(options.body));
      return new Response(null, { status: 204 });
    }
    if (host === "api.github.com" && options.method === "PATCH") return Response.json({ number: 1 });
    throw new Error("Unexpected request");
  });
  await main();
  assert.equal(messages.length, 1);
  assert.match(messages[0].content, /`SK6HFW6T3T`/);
  assert.doesNotMatch(messages[0].content, /KNOWN2026/);
  assert.ok(messages[0].content.endsWith(`<${YAR_URL}>`));
  assert.equal(messages[0].embeds[0].description, "`SK6HFW6T3T`");
});

test("manual reports send the same complete text and card to every configured webhook", async (t) => {
  const urls = [
    "https://discord.com/api/webhooks/123/test_only_webhook_value_0000",
    "https://discord.com/api/webhooks/456/test_only_webhook_value_0001",
    "https://discord.com/api/webhooks/789/test_only_webhook_value_0002",
  ];
  // The user's existing singular secret also accepts multiple lines.
  installEnvironment(t, { DISCORD_WEBHOOK_URL: [...urls, urls[0]].join("\n"),
    MANUAL_CODES: "SK6HFW6T3T,EEQJMDMJHX,sk6hfw6t3t,KNOWN2026" });
  const deliveries = [];
  let stateWrites = 0;
  t.mock.method(globalThis, "fetch", async (url, options = {}) => {
    const host = new URL(url).hostname;
    if (host === "api.github.com" && !options.method) return Response.json([issue]);
    if (host === "discord.com") {
      deliveries.push({ url: String(url), payload: JSON.parse(options.body) });
      return new Response(null, { status: 204 });
    }
    if (host === "api.github.com" && options.method === "PATCH") {
      stateWrites += 1;
      return Response.json({ number: 1 });
    }
    throw new Error("Unexpected request");
  });
  await main();
  assert.deepEqual(deliveries.map((item) => item.url), urls);
  assert.equal(stateWrites, 1);
  for (const { payload } of deliveries) {
    assert.deepEqual(payload, deliveries[0].payload);
    assert.deepEqual(payload.allowed_mentions, { parse: [] });
    assert.match(payload.content, /玩家回報新兌換碼/);
    assert.deepEqual([...payload.content.matchAll(/`([^`]+)`/g)].map((match) => match[1]),
      ["sk6hfw6t3t", "EEQJMDMJHX"]);
  }
});

test("initialization remains visible without embeds and does not announce existing codes", async (t) => {
  installEnvironment(t, { BAHAMUT_SCAN_ENABLED: "false" });
  const messages = [];
  t.mock.method(globalThis, "fetch", async (url, options = {}) => {
    const host = new URL(url).hostname;
    if (host === "api.github.com" && !options.method) return Response.json([]);
    if (host === "codes.yar.gg") return Response.json({ active: [{ code: "KNOWN2026" }], expired: [] });
    if (host === "discord.com") {
      messages.push(JSON.parse(options.body));
      return new Response(null, { status: 204 });
    }
    if (host === "api.github.com" && options.method === "POST") return Response.json({ number: 1 });
    throw new Error("Unexpected request");
  });
  await main();
  assert.equal(messages.length, 1);
  assert.match(messages[0].content, /兌換碼監控已建立/);
  assert.match(messages[0].content, /已建立 1 組兌換碼基準資料/);
  assert.doesNotMatch(messages[0].content, /KNOWN2026/);
  assert.equal(messages[0].embeds[0].color, 0x228be6);
});
