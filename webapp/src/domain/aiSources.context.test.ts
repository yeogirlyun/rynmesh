import { expect, it } from "vitest";
import { newSource } from "./aiSources";

it("starts every new source in explicit automatic discovery mode rather than asserting a generic window", () => {
  for (const provider of ["custom", "openai", "anthropic", "zai", "ollama"]) {
    const value = newSource(provider);
    expect(value.context_window_mode).toBe("auto");
    expect(value.context_window).toBe(32768);
    expect(value.max_output_tokens).toBeLessThan(value.context_window);
    expect(value.model_metadata).toBeUndefined();
  }
});

it("uses explicit protocol defaults without changing existing Chat presets", () => {
  expect(newSource("anthropic")).toMatchObject({ protocol: "anthropic_messages", base_url: "https://api.anthropic.com/v1" });
  for (const provider of ["openai", "zai", "custom", "ollama"]) expect(newSource(provider).protocol).toBe("openai_chat");
});
