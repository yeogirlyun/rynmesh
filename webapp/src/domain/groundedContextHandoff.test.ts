import { describe, expect, it } from "vitest";
import { consumeGroundedContextHandoff, createGroundedContextHandoff } from "./groundedContextHandoff";
import type { GroundedArticleContext } from "./groundedContext";

const context: GroundedArticleContext = {
  kind: "reader-article", itemId: "secret-item", title: "Unique secret title",
  sourceTitle: "Source", sourceUrl: "https://example.test/secret", blocks: [{ tag: "p", text: "secret body" }],
  wordCount: 2, extractedAt: "2026-09-02T00:00:00Z",
};

describe("grounded context handoff", () => {
  it("creates opaque one-time cloned handoffs", () => {
    const id = createGroundedContextHandoff(context, { now: 1000 });
    expect(id).toMatch(/^[0-9a-f]{48}$/);
    expect(id).not.toContain("secret");
    context.blocks[0].text = "mutated after handoff";
    expect(consumeGroundedContextHandoff(id, 1001)?.blocks[0].text).toBe("secret body");
    expect(consumeGroundedContextHandoff(id, 1002)).toBeNull();
  });

  it("expires without exposing an enumeration API", () => {
    const id = createGroundedContextHandoff(context, { now: 2000, ttlMs: 5 });
    expect(consumeGroundedContextHandoff(id, 2005)).toBeNull();
  });
});
