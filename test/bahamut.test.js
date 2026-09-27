import test from "node:test";
import assert from "node:assert/strict";
import { extractReplyCodes, parseBahamutPage, fetchBahamutReplies } from "../src/bahamut.js";
import { fetchYarEntries, SOURCE_URL } from "../src/monitor.js";

// Synthetic fixtures based on the existing project's Bahamut content/floor selectors.
// A successful live read is still required to validate the site's current markup.
function page(floors, number, last = number) {
  return floors.map(([floor, text]) => `
    <section class="c-section" data-sn="${10000 + floor}">
      <a href="#" class="floor" data-floor="${floor}">#${floor}</a>
      <article><div class="c-article__content"><div>${text}</div></div></article>
      <div class="c-reply">COMMENT777</div>
    </section>`).join("") + Array.from({ length: last }, (_, index) =>
      `<a class="${index + 1 === number ? "pagenow" : "page"}" href="${SOURCE_URL.replaceAll("&", "&amp;")}&amp;page=${index + 1}">${index + 1}</a>`,
    ).join("");
}

test("reads reply bodies and source links without first-floor codes or small comments", () => {
  const parsed = parseBahamutPage(page([[1, "FIRST2026"], [2, "sk6hfw6t3t"]], 1));
  assert.deepEqual(parsed.posts.map((post) => post.codes), [[], ["SK6HFW6T3T"]]);
  assert.equal(parsed.posts[1].url, "https://forum.gamer.com.tw/Co.php?bsn=75703&sn=10002");
  assert.equal(parsed.pageNumber, 1);
});

test("ignores nested quotes, deleted codes, links, expired lines and ordinary English", () => {
  const codes = extractReplyCodes(`
    <blockquote>QUOTED2026<blockquote>OLDER2026</blockquote></blockquote>
    <s>STRUCK2026</s><span style="text-decoration: line-through">STYLED2026</span>
    <a href="https://example.com">LINKED2026</a>
    <div>https://example.com/URLCODE123</div>
    <div>THANKYOU</div><div>已過期 EXPIRED2026</div>
    <div>兌換碼：NEVERLOOKBACK</div>
    <div>sk6hfw6t3t</div>
    <div>AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1</div>
  `);
  assert.deepEqual(codes, ["NEVERLOOKBACK", "SK6HFW6T3T"]);
});

test("fails closed for maintenance and missing post content", () => {
  assert.throws(() => parseBahamutPage("<title>系統維修中</title>"), /floors were not found/);
  assert.throws(() => parseBahamutPage('<a data-floor="364"></a><p>unexpected layout</p>'), /content was not found/);
});

test("first scan only reads the latest page for a quiet baseline", async () => {
  let requests = 0;
  const snapshot = await fetchBahamutReplies(null, {
    fetchImpl: async () => { requests += 1; return new Response(page([[363, "OLD2026"], [364, "sk6hfw6t3t"]], 19)); },
  });
  assert.equal(requests, 1);
  assert.equal(snapshot.lastFloor, 364);
});

test("catches all pages back to the saved floor instead of dropping replies at a page boundary", async () => {
  const visited = [];
  const snapshot = await fetchBahamutReplies({ initialized: true, lastFloor: 360 }, {
    fetchImpl: async (url) => {
      visited.push(String(url));
      return new Response(new URL(url).searchParams.has("last")
        ? page([[361, "NEWCODE361"], [362, "NEWCODE362"]], 19)
        : page([[359, "OLDCODE359"], [360, "OLDCODE360"]], 18, 19));
    },
  });
  assert.equal(visited.length, 2);
  assert.match(visited[1], /page=18$/);
  assert.deepEqual(snapshot.posts.map((post) => post.floor), [359, 360, 361, 362]);
  assert.equal(snapshot.lastFloor, 362);
});

test("403, pagination failure and page limit return no advanced cursor", async () => {
  const old = { initialized: true, lastFloor: 2 };
  await assert.rejects(fetchBahamutReplies(old, { fetchImpl: async () => new Response("blocked", { status: 403 }) }), /HTTP 403/);
  await assert.rejects(fetchBahamutReplies(old, {
    maxPages: 1, fetchImpl: async () => new Response(page([[361, "NEWCODE361"]], 19)),
  }), /cursor unchanged/);
  await assert.rejects(fetchBahamutReplies(old, {
    fetchImpl: async () => new Response(page([[361, "NEWCODE361"]], 1)),
  }), /pagination/);
  assert.equal(old.lastFloor, 2);
});

test("does not follow off-host redirects", async () => {
  await assert.rejects(fetchBahamutReplies(null, {
    fetchImpl: async () => new Response(null, { status: 302, headers: { location: "https://example.com/" } }),
  }), /official HTTPS host/);
});

test("YAR falls back to its other public endpoint when the first endpoint is unavailable", async () => {
  const urls = [];
  const entries = await fetchYarEntries(async (url) => {
    urls.push(String(url));
    return urls.length === 1 ? new Response("blocked", { status: 503 })
      : Response.json({ active: [{ code: "SK6HFW6T3T" }], expired: [] });
  });
  assert.equal(urls[0], "https://codes.yar.gg/api/codes");
  assert.equal(urls.length, 2);
  assert.deepEqual(entries, [{ code: "SK6HFW6T3T", status: "active" }]);
});
