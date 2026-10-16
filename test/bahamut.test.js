import test from "node:test";
import assert from "node:assert/strict";
import { extractReplyCodes, parseBahamutPage, fetchBahamutReplies } from "../src/bahamut.js";
import { fetchYarEntries, SOURCE_URL } from "../src/monitor.js";
import { reconcileAutomaticState } from "../src/combined-state.js";

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

test("deleted floor 354 with an author account or moderator notice does not block floors 388 and 389", () => {
  for (const notice of [
    '此文章已由原作者(<a href="/user">TestPlayer</a>)刪除',
    "此文章已由原作者（TestPlayer）刪除",
    "【刪除】管理員：文章內容重複（不倒扣刪文）",
    "本文已被刪除",
  ]) {
    const deleted = `<section class="c-section" id="disable_10354">
      <a class="floor" data-floor="354">354 樓</a><div>${notice}</div></section>`;
    const parsed = parseBahamutPage(deleted + page([[388, "QEQWC6RAP8"], [389, "sdk8xhrwh4"]], 20));
    assert.deepEqual(parsed.posts.map(({ floor, codes }) => ({ floor, codes })), [
      { floor: 354, codes: [] },
      { floor: 388, codes: ["QEQWC6RAP8"] },
      { floor: 389, codes: ["SDK8XHRWH4"] },
    ]);
  }
});

test("a folded placeholder uses its hidden real body and is announced only once", () => {
  const placeholder = `<section class="c-section" id="disable_10354">
    <a class="floor" data-floor="354">354 樓</a><div>此文章已摺疊</div></section>`;
  const body = `<section class="c-section" id="post_10354" style="display:none">
    <a class="floor" data-floor="354">354 樓</a>
    <article><div class="c-article__content">QEQWC6RAP8</div></article></section>`;
  const parsed = parseBahamutPage(placeholder + body + page([[355, "sdk8xhrwh4"]], 18));
  assert.deepEqual(parsed.posts.map((post) => post.floor), [354, 355]);
  assert.deepEqual(parsed.posts[0].codes, ["QEQWC6RAP8"]);
  assert.equal(parsed.posts[0].url, "https://forum.gamer.com.tw/Co.php?bsn=75703&sn=10354");
  assert.throws(() => parseBahamutPage(placeholder + page([[355, "sdk8xhrwh4"]], 18)),
    /floor 354 content was not found/);
});

test("repeated floor attributes and controls do not cut the article into incomplete fragments", () => {
  const html = `<a data-floor="999" class="jump">Jump</a>
    <section class="c-section" id="post_10354" data-floor="354">
      <a class="floor" data-floor="354">354 樓</a>
      <button data-floor="354">回覆</button>
      <article><div class="c-article__content">QEQWC6RAP8
        <blockquote><a data-floor="1">1 樓</a>QUOTED2026</blockquote>
      </div></article>
      <div class="c-reply"><a data-floor="354">小留言</a>COMMENT777</div>
    </section>` + page([[355, "sdk8xhrwh4"]], 18);
  const parsed = parseBahamutPage(html);
  assert.deepEqual(parsed.posts.map(({ floor, codes }) => ({ floor, codes })), [
    { floor: 354, codes: ["QEQWC6RAP8"] }, { floor: 355, codes: ["SDK8XHRWH4"] },
  ]);
});

test("the content class can be on an article element, with nested content preserved", () => {
  const html = `<section class="c-section" id="post_10388"><a class="floor" data-floor="388">388 樓</a>
    <article class="c-article__content"><div>QEQWC6RAP8</div><div>sdk8xhrwh4</div></article></section>`;
  assert.deepEqual(parseBahamutPage(html).posts[0].codes, ["QEQWC6RAP8", "SDK8XHRWH4"]);
});

test("already processed floor 354 need not have a body, but an unread new floor must still fail safely", async () => {
  const old = { initialized: true, lastFloor: 387 };
  const malformed = (floor) => `<section class="c-section" id="post_${10000 + floor}">
    <a class="floor" data-floor="${floor}">${floor} 樓</a><div>Unexpected layout</div></section>`;
  const snapshot = await fetchBahamutReplies(old, {
    fetchImpl: async () => new Response(malformed(354) + page([[388, "QEQWC6RAP8"], [389, "sdk8xhrwh4"]], 20)),
  });
  const result = reconcileAutomaticState({ initialized: true, codes: [], bahamut: old },
    { yarEntries: null, bahamut: snapshot }, "2026-10-16T13:30:00Z");
  assert.deepEqual(result.announcements.flatMap((item) => item.entries.map((entry) => entry.code)),
    ["QEQWC6RAP8", "SDK8XHRWH4"]);
  assert.equal(result.state.bahamut.lastFloor, 389);
  const second = reconcileAutomaticState(result.state, { yarEntries: null, bahamut: snapshot }, "2026-10-16T14:00:00Z");
  assert.deepEqual(second.announcements, []);
  await assert.rejects(fetchBahamutReplies(old, {
    fetchImpl: async () => new Response(malformed(388) + page([[389, "sdk8xhrwh4"]], 20)),
  }), /floor 388 content was not found.*cursor unchanged/);
  assert.equal(old.lastFloor, 387);
});

test("conflicting reply identities cannot be merged under the same floor", () => {
  const html = page([[354, "QEQWC6RAP8"]], 18)
    + page([[354, "sdk8xhrwh4"]], 18).replace('data-sn="10354"', 'data-sn="99999"');
  assert.throws(() => parseBahamutPage(html), /conflicting post IDs/);
});

test("extracts mixed alphanumeric and ten-letter codes from the same unlabelled reply", () => {
  const expected = ["SK6HFW6T3T", "JN6NNWJN33", "EEQJMDMJHX"];
  for (const separator of ["<br>", " ", "，", "/", "、"]) {
    assert.deepEqual(extractReplyCodes(expected.join(separator)), expected);
  }
  assert.deepEqual(extractReplyCodes("sk6hfw6t3t<br>eeqjmdmjhx<br>SK6HFW6T3T"),
    ["SK6HFW6T3T", "EEQJMDMJHX"]);
  assert.deepEqual(extractReplyCodes("EEQJMDMJHX<br>YRYQHTNEDA"),
    ["EEQJMDMJHX", "YRYQHTNEDA"]);
  assert.deepEqual(extractReplyCodes("SK6HFW6T3T<br>THANKYOU"), ["SK6HFW6T3T"]);
});

test("a code-list heading also applies to codes on following lines", () => {
  assert.deepEqual(extractReplyCodes("<p>兌換碼：</p><div>NEVERLOOKBACK</div><div>SK6HFW6T3T</div>"),
    ["NEVERLOOKBACK", "SK6HFW6T3T"]);
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
