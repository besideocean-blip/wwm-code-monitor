import test from "node:test";
import assert from "node:assert/strict";
import { reconcileAutomaticState, reconcileManualState } from "../src/combined-state.js";
import { YAR_URL } from "../src/monitor.js";

const now = "2026-09-27T19:00:00Z";
const active = (code) => ({ code, status: "active" });
const reply = (floor, ...codes) => ({ floor, codes, url: `https://forum.gamer.com.tw/Co.php?bsn=75703&sn=${10000 + floor}` });
const snapshot = (...posts) => ({ posts, lastFloor: Math.max(...posts.map((post) => post.floor)), pagesRead: 1 });
const previous = () => ({
  initialized: true, scannedSourceUrl: YAR_URL,
  codes: [active("KNOWN2026")],
  bahamut: { initialized: true, lastFloor: 363 },
});

test("adding Bahamut quietly baselines its history without suppressing new YAR announcements", () => {
  const old = previous();
  delete old.bahamut;
  const result = reconcileAutomaticState(old, {
    yarEntries: [active("KNOWN2026"), active("YARNEW2026")],
    bahamut: snapshot(reply(364, "SK6HFW6T3T")),
  }, now);
  assert.equal(result.bahamutFirstRun, true);
  assert.deepEqual(result.announcements.map((item) => item.entries[0].code), ["YARNEW2026"]);
  assert.ok(result.state.seenCodes.includes("SK6HFW6T3T"));
});

test("one code is announced once across Bahamut, later YAR and manual reports", () => {
  const first = reconcileAutomaticState(previous(), {
    yarEntries: [active("KNOWN2026")], bahamut: snapshot(reply(364, "sk6hfw6t3t")),
  }, now);
  assert.equal(first.announcements[0].source, "bahamut");
  assert.equal(first.announcements[0].entries[0].status, "unverified");
  const second = reconcileAutomaticState(first.state, {
    yarEntries: [active("KNOWN2026"), active("SK6HFW6T3T")], bahamut: snapshot(reply(365, "SK6HFW6T3T")),
  }, now);
  assert.deepEqual(second.announcements, []);
  assert.equal(second.state.codes.find((entry) => entry.code === "SK6HFW6T3T").status, "active");
  assert.deepEqual(reconcileManualState(second.state, [active("sk6hfw6t3t")], now).newActive, []);
});

test("YAR wins a simultaneous discovery and confirmed expired codes never become Bahamut alerts", () => {
  const result = reconcileAutomaticState(previous(), {
    yarEntries: [active("SK6HFW6T3T"), { code: "EXPIRED2026", status: "expired" }],
    bahamut: snapshot(reply(364, "SK6HFW6T3T", "EXPIRED2026")),
  }, now);
  assert.equal(result.announcements.length, 1);
  assert.equal(result.announcements[0].source, "yar");
  assert.ok(!result.state.codes.some((entry) => entry.code === "EXPIRED2026"));
  const later = reconcileAutomaticState(result.state, {
    yarEntries: null, bahamut: snapshot(reply(365, "EXPIRED2026")),
  }, now);
  assert.deepEqual(later.announcements, []);
});

test("YAR can continue while a failed Bahamut fetch leaves its cursor unchanged", () => {
  const old = previous();
  const result = reconcileAutomaticState(old, { yarEntries: [active("NEWCODE2026")], bahamut: null }, now);
  assert.deepEqual(result.state.bahamut, old.bahamut);
  assert.equal(result.announcements.length, 1);
  assert.equal(old.codes.length, 1);
});

test("a failed YAR source does not suppress new Bahamut candidates or reset the YAR baseline", () => {
  const result = reconcileAutomaticState(previous(), { yarEntries: null, bahamut: snapshot(reply(364, "SK6HFW6T3T")) }, now);
  assert.equal(result.announcements.length, 1);
  assert.equal(result.state.scannedSourceUrl, YAR_URL);
});

test("manual submissions preserve the Bahamut cursor and expiry history", () => {
  const old = { ...previous(), expiredCodes: ["EXPIRED2026"], seenCodes: ["EXPIRED2026"] };
  const result = reconcileManualState(old, [active("MANUAL2026")], now);
  assert.deepEqual(result.state.bahamut, old.bahamut);
  assert.deepEqual(result.state.expiredCodes, old.expiredCodes);
  assert.ok(result.state.seenCodes.includes("EXPIRED2026"));
});

test("starting with only Bahamut success still quietly baselines YAR when it first recovers", () => {
  const first = reconcileAutomaticState(null, { yarEntries: null, bahamut: snapshot(reply(364, "SK6HFW6T3T")) }, now);
  assert.deepEqual(first.announcements, []);
  const second = reconcileAutomaticState(first.state, { yarEntries: [active("YAROLD2026")], bahamut: null }, now);
  assert.equal(second.yarFirstRun, true);
  assert.deepEqual(second.announcements, []);
});
