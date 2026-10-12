import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import NodeApps from "./NodeApps";
import { nodeApps } from "../domain/nodeApps";
import { friendsApi } from "../domain/friendsClient";
vi.mock("../appContext", () => ({
  useAppContext: () => ({ client: { mode: "live" } }),
}));
const app = {
  id: "desk",
  name: "Desk",
  description: "Service requests",
  operations: {
    create_ticket: {
      effect: "write" as const,
      schema: { properties: { name: { type: "string" } }, required: ["name"] },
    },
  },
};
const workspace = {
  app: "desk",
  provider_peer: "owner-node",
  consumer_peer: "owner-node",
  revision: 0,
  access_generation: "owner",
  state_hash: "sha256:reviewed-workspace",
  records: {},
};
const preview = {
  app: "desk",
  action: "create_ticket",
  arguments: { name: "Review a contract" },
  provider_peer: "owner-node",
  consumer_peer: "owner-node",
  revision: 0,
  effect: "write",
  preview_hash: "sha256:original-reviewed-state",
  price_ryncoin: "0",
};
beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(friendsApi, "list").mockResolvedValue({ friends: [] });
  vi.spyOn(nodeApps, "catalog").mockResolvedValue({
    provider_peer: "owner-node",
    apps: [app],
  });
  vi.spyOn(nodeApps, "workspace").mockResolvedValue(workspace);
  vi.spyOn(nodeApps, "pending").mockResolvedValue([]);
  vi.spyOn(nodeApps, "preview").mockResolvedValue(preview);
  vi.spyOn(nodeApps, "invoke").mockResolvedValue({
    operation_id: "saved",
    revision: 1,
    result: { record: { name: "Review a contract" } },
  });
  vi.spyOn(nodeApps, "resume").mockResolvedValue({
    result: { record: { name: "Original only" } },
  });
});
it("reviews exact app data before any mutation", async () => {
  const user = userEvent.setup();
  render(
    <MemoryRouter>
      <NodeApps />
    </MemoryRouter>,
  );
  await user.type(await screen.findByLabelText("name"), "Review a contract");
  await user.click(screen.getByRole("button", { name: "Review operation" }));
  expect(
    await screen.findByRole("dialog", { name: "Review app operation" }),
  ).toHaveTextContent("Review a contract");
  expect(nodeApps.invoke).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Confirm operation" }));
  await waitFor(() =>
    expect(nodeApps.invoke).toHaveBeenCalledWith(
      expect.objectContaining({
        provider_peer: "owner-node",
        app: "desk",
        action: "create_ticket",
        arguments: { name: "Review a contract" },
        preview_hash: preview.preview_hash,
      }),
    ),
  );
});
it("reload exposes a durable pending operation and resumes its original ID", async () => {
  vi.mocked(nodeApps.pending).mockResolvedValue([
    {
      operation_id: "original-id",
      app: "desk",
      action: "create_ticket",
      provider_peer: "owner-node",
    },
  ]);
  const user = userEvent.setup();
  render(
    <MemoryRouter>
      <NodeApps />
    </MemoryRouter>,
  );
  const retry = await screen.findByRole("button", {
    name: "Retry original operation",
  });
  expect(
    screen.getByRole("button", { name: "Review operation" }),
  ).toBeDisabled();
  await user.click(retry);
  await waitFor(() =>
    expect(nodeApps.resume).toHaveBeenCalledWith("original-id"),
  );
  expect(nodeApps.invoke).not.toHaveBeenCalled();
});
it("a failed confirmed nonmutation unlocks a fresh review without replaying it", async () => {
  vi.mocked(nodeApps.invoke).mockRejectedValue(new Error("Workspace changed"));
  const user = userEvent.setup();
  render(
    <MemoryRouter>
      <NodeApps />
    </MemoryRouter>,
  );
  await user.type(await screen.findByLabelText("name"), "Review a contract");
  await user.click(screen.getByRole("button", { name: "Review operation" }));
  await user.click(
    await screen.findByRole("button", { name: "Confirm operation" }),
  );
  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent("Workspace changed"),
  );
  expect(
    screen.queryByRole("button", { name: "Retry original operation" }),
  ).not.toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "Review operation" }),
  ).toBeEnabled();
});
it("late provider data cannot populate another provider workspace", async () => {
  vi.mocked(friendsApi.list).mockResolvedValue({
    friends: [
      { peer_id: "friend", node_name: "MS-2 Apps", status: "active" } as never,
    ],
  });
  let finish: (value: typeof workspace) => void = () => {};
  vi.mocked(nodeApps.workspace).mockImplementation((_app, peer) =>
    peer
      ? Promise.resolve({
          ...workspace,
          provider_peer: "friend",
          records: { tickets: [{ id: "remote", name: "Remote ticket" }] },
        })
      : new Promise((resolve) => {
          finish = resolve;
        }),
  );
  const user = userEvent.setup();
  render(
    <MemoryRouter>
      <NodeApps />
    </MemoryRouter>,
  );
  await screen.findAllByRole("option", { name: "MS-2 Apps" });
  await waitFor(() => expect(nodeApps.workspace).toHaveBeenCalled());
  await user.selectOptions(screen.getByLabelText("Provider"), "friend");
  expect(await screen.findByText("Remote ticket")).toBeInTheDocument();
  await act(async () =>
    finish({
      ...workspace,
      records: { tickets: [{ id: "old", name: "Late old data" }] },
    }),
  );
  expect(screen.queryByText("Late old data")).not.toBeInTheDocument();
});
