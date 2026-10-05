import { expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import i18n from "../i18n";
import { makeLiveNodeClient } from "./liveNodeClient";

it("translates a space HTTP rejection while retaining the status code", async () => {
  server.use(http.post("*/api/local/space/join", () => HttpResponse.json({
    detail: "This device already belongs to a space or has a pending request.",
  }, { status: 400 })));
  await i18n.changeLanguage("zh-CN");
  await expect(makeLiveNodeClient().spaceAction("join", {})).rejects.toThrow(
    "本地 Ryn 节点返回 400: 此设备已加入空间，或已有请求正在等待处理，请勿重复提交。",
  );
});
