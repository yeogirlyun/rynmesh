import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import InviteDeepLinks, { useInviteDeepLink } from "./InviteDeepLinks";

const source = vi.hoisted(() => ({ getCurrent: vi.fn(), onOpenUrl: vi.fn(), isTauri: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ isTauri: source.isTauri }));
vi.mock("@tauri-apps/plugin-deep-link", () => source);
function View() {
  const { pending, clear } = useInviteDeepLink();
  const location = useLocation();
  return <><span data-testid="route">{location.pathname}{location.search}{location.hash}</span><span data-testid="history-state">{JSON.stringify(location.state)}</span><span data-testid="pending">{pending}</span><button onClick={clear}>Clear</button></>;
}
let event: (urls: string[]) => void;
beforeEach(() => {
  source.isTauri.mockReturnValue(true);
  source.getCurrent.mockResolvedValue(["ryn://join/PRIVATE_MARKER"]);
  source.onOpenUrl.mockImplementation(async callback => { event = callback; return vi.fn(); });
});
afterEach(() => vi.clearAllMocks());
it("routes a cold and warm link using only in-memory state and does not replace another pending invite", async () => {
  const fetchSpy = vi.spyOn(globalThis, "fetch");
  render(<MemoryRouter initialEntries={["/"]}><InviteDeepLinks><View /></InviteDeepLinks></MemoryRouter>);
  await waitFor(() => expect(screen.getByTestId("pending")).toHaveTextContent("rynmesh://join/PRIVATE_MARKER"));
  expect(screen.getByTestId("route")).toHaveTextContent(/^\/friends$/);
  expect(screen.getByTestId("history-state")).toHaveTextContent("null");
  act(() => event(["ryn://join/SECOND_MARKER"]));
  expect(screen.getByRole("status")).toHaveTextContent("Another invitation arrived");
  expect(screen.getByRole("status")).not.toHaveTextContent("MARKER");
  expect(screen.getByTestId("pending")).toHaveTextContent("PRIVATE_MARKER");
  fireEvent.click(screen.getByText("Clear"));
  act(() => event(["ryn://join/SECOND_MARKER"]));
  expect(screen.getByTestId("pending")).toHaveTextContent("SECOND_MARKER");
  expect(source.getCurrent).toHaveBeenCalledTimes(1);
  expect(fetchSpy).not.toHaveBeenCalled();
  fetchSpy.mockRestore();
});
it("does not call native APIs in an ordinary browser", () => {
  source.isTauri.mockReturnValue(false);
  render(<MemoryRouter><InviteDeepLinks><View /></InviteDeepLinks></MemoryRouter>);
  expect(source.onOpenUrl).not.toHaveBeenCalled();
  expect(screen.getByTestId("pending")).toBeEmptyDOMElement();
});
