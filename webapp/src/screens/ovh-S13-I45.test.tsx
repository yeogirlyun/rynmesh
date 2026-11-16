import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import Settings from "./Settings";

afterEach(() => vi.restoreAllMocks());

it("S13-I45 T3: Network settings renders proxy scope notice and saves manual outbound proxy", async () => {
  const client = makeFixtureNodeClient();
  const updateSpy = vi.spyOn(client, "updateSettings");
  const context = { client, confirm: vi.fn(), notify: vi.fn(), refreshShell: vi.fn() } as unknown as AppOutletContext;

  render(
    <MemoryRouter initialEntries={["/settings"]}>
      <Routes>
        <Route element={<Outlet context={context} />}>
          <Route path="/settings" element={<Settings />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );

  const user = userEvent.setup();
  const networkNav = await screen.findByRole("button", { name: /Network|网络/ });
  await user.click(networkNav);

  expect(
    await screen.findByText(/代理只用于节点之间和注册中心的连接，不用于模型请求/),
  ).toBeInTheDocument();

  const modeSelect = screen.getByLabelText(/Outbound proxy|出站代理/);
  await user.selectOptions(modeSelect, "manual");
  expect(updateSpy).toHaveBeenCalledWith({
    network_proxy: { mode: "manual", url: "" },
  });

  const urlInput = await screen.findByLabelText(/Proxy URL|代理地址/);
  await user.clear(urlInput);
  await user.type(urlInput, "http://127.0.0.1:8888");
  const saveBtn = screen.getByRole("button", { name: /Save proxy|保存代理/ });
  await user.click(saveBtn);

  expect(updateSpy).toHaveBeenLastCalledWith({
    network_proxy: { mode: "manual", url: "http://127.0.0.1:8888" },
  });
});
