import { expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import i18n from "../../i18n";
import { MessageBubble, type ChatRecord } from "./ChatParts";

it.each(["text/html", "image/svg+xml"])("T1 offers %s as a file and confirms the download action", async (mime) => {
  await i18n.changeLanguage("zh-CN");
  const record = { dir: "in", kind: "image", attachment: { filename: "remote-file", mime } } as ChatRecord;
  render(<MessageBubble record={record} url="/api/local/messages/attachment/message?peer_id=peer" peerName="Peer" />);
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  const download = screen.getByRole("link", { name: "remote-file" });
  expect(download).toHaveAttribute("download", "remote-file");
  expect(screen.queryByText("已作为文件下载")).not.toBeInTheDocument();
  fireEvent.click(download);
  expect(screen.getByRole("status")).toHaveTextContent("已作为文件下载");
});

it("T2 retains passive raster preview", () => {
  const record = { dir: "in", kind: "image", attachment: { filename: "photo.png", mime: "image/png" } } as ChatRecord;
  render(<MessageBubble record={record} url="/attachment" peerName="Peer" />);
  expect(screen.getByRole("img", { name: "photo.png" })).toHaveAttribute("src", "/attachment");
});
