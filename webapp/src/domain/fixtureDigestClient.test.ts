import { expect, it, vi } from "vitest";
import { fixtureDigestApi } from "./fixtureDigestClient";

it("never contacts the live node while previewing the feed", async () => {
  const fetch = vi.spyOn(globalThis, "fetch");
  await fixtureDigestApi.getDigest();
  await fixtureDigestApi.markDiscoverySeen();
  await fixtureDigestApi.refreshDigest();
  await expect(fixtureDigestApi.addSource("https://example.com/feed")).rejects.toThrow("live device");
  await expect(fixtureDigestApi.clearConsumption()).rejects.toThrow("live device");
  expect(fetch).not.toHaveBeenCalled();
  fetch.mockRestore();
});
