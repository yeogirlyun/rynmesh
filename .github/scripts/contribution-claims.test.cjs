"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  activeClaim,
  expiresAt,
  marker,
  markerData,
  needsApproval,
  parseCommand,
} = require("./contribution-claims.cjs");

test("commands must occupy the first trimmed line exactly", () => {
  assert.equal(parseCommand(" /claim\nI would like this"), "claim");
  assert.equal(parseCommand("/release"), "release");
  assert.equal(parseCommand("please /claim"), null);
  assert.equal(parseCommand("/claim now"), null);
});
test("claim markers round trip without exposing visible state parsing", () => {
  const value = { user: "engineer-one", state: "reserved", expiresAt: "2026-08-24T00:00:00.000Z" };
  assert.deepEqual(markerData(`${marker("claim", value)}\nReserved.`, "claim"), value);
});

test("the latest claim and release marker define the active lock", () => {
  const first = { user: "one", state: "reserved" };
  const second = { user: "two", state: "reserved" };
  const comments = [
    { created_at: "2026-08-01T00:00:00Z", body: marker("claim", first) },
    { created_at: "2026-08-02T00:00:00Z", body: marker("claim-release", { user: "one" }) },
    { created_at: "2026-08-03T00:00:00Z", body: marker("claim", second) },
  ];
  assert.deepEqual(activeClaim(comments), second);
  comments.push({ created_at: "2026-08-04T00:00:00Z", body: marker("claim-release", { user: "two" }) });
  assert.equal(activeClaim(comments), null);
});

test("design, privacy, and large work require approval", () => {
  assert.equal(needsApproval({ labels: [{ name: "needs design" }] }), true);
  assert.equal(needsApproval({ labels: [{ name: "privacy" }] }), true);
  assert.equal(needsApproval({ labels: [{ name: "size:large" }] }), true);
  assert.equal(needsApproval({ labels: [{ name: "size:medium" }, { name: "webapp" }] }), false);
});

test("a reservation expires exactly seven days after claim time", () => {
  assert.equal(expiresAt(new Date("2026-08-17T12:00:00.000Z")), "2026-08-24T12:00:00.000Z");
});
