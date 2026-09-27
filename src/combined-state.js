import { YAR_URL, normalizeCode, reconcileSourceState } from "./monitor.js";

function history(previous) {
  return new Set([
    ...(previous?.seenCodes ?? []),
    ...(previous?.codes ?? []).map((entry) => entry.code),
  ].map(normalizeCode));
}

export function reconcileAutomaticState(previous, { yarEntries, bahamut }, now) {
  let state = { ...previous, codes: previous?.codes ?? [] };
  const seen = history(previous);
  const expired = new Set((previous?.expiredCodes ?? []).map(normalizeCode));
  const announcements = [];
  let yarFirstRun = false;
  let bahamutFirstRun = false;

  if (yarEntries) {
    const result = reconcileSourceState(previous, yarEntries, now, YAR_URL);
    state = { ...state, ...result.state };
    yarFirstRun = result.firstRun;
    const entries = result.newActive.filter((entry) => !seen.has(normalizeCode(entry.code)));
    if (entries.length) announcements.push({ source: "yar", entries, url: YAR_URL });
    for (const entry of yarEntries) {
      const code = normalizeCode(entry.code);
      seen.add(code);
      if (entry.status === "expired") expired.add(code);
      else expired.delete(code);
    }
  }

  if (bahamut) {
    bahamutFirstRun = previous?.bahamut?.initialized !== true;
    const cursor = previous?.bahamut?.lastFloor ?? 0;
    const known = new Map(state.codes.map((entry) => [normalizeCode(entry.code), entry]));
    for (const post of bahamut.posts) {
      if (!bahamutFirstRun && post.floor <= cursor) continue;
      const entries = [];
      for (const value of post.codes) {
        const code = normalizeCode(value);
        if (seen.has(code) || expired.has(code)) continue;
        seen.add(code);
        const entry = { code, status: "unverified" };
        known.set(code, { ...entry, firstSeenAt: now, lastSeenAt: now });
        if (!bahamutFirstRun) entries.push(entry);
      }
      if (entries.length) announcements.push({ source: "bahamut", entries, url: post.url, floor: post.floor });
    }
    state.codes = [...known.values()].sort((a, b) => a.code.localeCompare(b.code, "en"));
    state.bahamut = { initialized: true, lastFloor: bahamut.lastFloor, lastCheckedAt: now };
  }

  state.initialized = true;
  state.sourceUrl = YAR_URL;
  state.updatedAt = now;
  state.seenCodes = [...seen].sort();
  state.expiredCodes = [...expired].sort();
  return { state, announcements, yarFirstRun, bahamutFirstRun };
}

export function reconcileManualState(previousState, manualEntries, now) {
  const seen = history(previousState);
  const known = new Map((previousState?.codes ?? []).map((entry) => [normalizeCode(entry.code), entry]));
  const newActive = [];
  for (const entry of manualEntries) {
    const code = normalizeCode(entry.code);
    // Includes baseline, expired and Bahamut codes, not only currently active codes.
    if (seen.has(code)) continue;
    seen.add(code);
    newActive.push(entry);
    known.set(code, { code: entry.code, status: "active", firstSeenAt: now, lastSeenAt: now });
  }
  return {
    newActive,
    state: {
      ...previousState,
      initialized: true,
      sourceUrl: previousState?.sourceUrl ?? YAR_URL,
      scannedSourceUrl: previousState?.scannedSourceUrl ?? null,
      updatedAt: now,
      codes: [...known.values()].sort((a, b) => a.code.localeCompare(b.code, "en", { sensitivity: "base" })),
      seenCodes: [...seen].sort(),
    },
  };
}
