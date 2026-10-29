import { expect, it } from "vitest";
import { decodeAgentFileText } from "./agentFilePreview";

it("decodes UTF-8, Windows UTF-16 and GB18030 text exports without corrupting Chinese filenames or content", async () => {
  expect(await decodeAgentFileText(new Blob(["销售报告"]))).toBe("销售报告");
  expect(await decodeAgentFileText(new Blob([new Uint8Array([0xff, 0xfe, 0x2d, 0x4e, 0x87, 0x65])]))).toBe("中文");
  expect(await decodeAgentFileText(new Blob([new Uint8Array([0xfe, 0xff, 0x4e, 0x2d, 0x65, 0x87])]))).toBe("中文");
  expect(await decodeAgentFileText(new Blob([new Uint8Array([0xd6, 0xd0, 0xce, 0xc4])]))).toBe("中文");
});
