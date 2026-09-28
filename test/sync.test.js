import test from "node:test";
import assert from "node:assert/strict";
import { buildCodeEmbeds, scanSources, main } from "../src/sync.js";
import { YAR_URL } from "../src/monitor.js";

test("keeps the existing title, green color and code lines; shows the Bahamut floor", () => {
  const entries = [{ code: "SK6HFW6T3T", status: "unverified" }];
  const source = { source: "bahamut", floor: 364, url: "https://forum.gamer.com.tw/Co.php?bsn=75703&sn=10364" };
  const embed = buildCodeEmbeds("發現新兌換碼", entries, source)[0].embeds[0];
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

function installEnvironment(t, overrides) {
  const values = {
    GH_REPOSITORY: "example/repo", GH_STATE_TOKEN: "test-only-token",
    DISCORD_WEBHOOK_URL: "https://discord.com/api/webhooks/123/test_only_webhook_value_0000",
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
  await assert.rejects(main(), /Discord Webhook returned HTTP 503/);
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
