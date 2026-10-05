import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import ChatMarkdown from "./ChatMarkdown";

it("formats model prose, lists and code without executing HTML or loading images", () => {
  const { container } = render(<ChatMarkdown>{"今天 **晴天**，约 `27°C`。\n\n- 带水\n- 防晒\n\n```js\nconst n = 1;\n```\n\n<script>alert(1)</script>\n\n![tracking](https://example.test/pixel)\n\n[unsafe](javascript:alert)"}</ChatMarkdown>);
  expect(screen.getByText("晴天").tagName).toBe("STRONG");
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  expect(container.querySelector("pre code")).toHaveTextContent("const n = 1;");
  expect(container.querySelector("script, img")).toBeNull();
  expect(screen.getByText("unsafe")).not.toHaveAttribute("href", "javascript:alert");
});
