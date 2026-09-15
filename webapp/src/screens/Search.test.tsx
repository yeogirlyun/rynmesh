import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { friendsApi } from "../domain/friendsClient";
import { localSearch, type SearchPage, type SearchResult } from "../domain/localSearch";
import Search, { Highlight } from "./Search";

const status = { state: "ready", error_code: "", indexed_count: 2, updated_at: 1 };
const snippet = (text: string) => ({ text, matches: [] as [number, number][], offset_unit: "unicode_codepoints" as const, prefix_omitted: false, suffix_omitted: false });
const result = (title: string): SearchResult => ({ id: title, title, source: "Journal", timestamp: 100, kinds: ["saved", "history"], body_state: "available",
  targets: [{ label: "Read local content", href: "/target" }], snippet: snippet("Local body"), title_match: snippet(title) });
const page = (...titles: string[]): SearchPage => ({ results: titles.map(result), total: titles.length, next_cursor: "", partial: false, index: status });

beforeEach(() => {
  vi.spyOn(localSearch, "status").mockResolvedValue(status);
  vi.spyOn(localSearch, "query").mockResolvedValue(page("Current result"));
  vi.spyOn(localSearch, "rebuild").mockResolvedValue(status);
  vi.spyOn(friendsApi, "list").mockResolvedValue({ friends: [] });
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
});
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

function Target() {
  const navigate = useNavigate();
  return <><h1>Opened result</h1><button onClick={() => navigate(-1)}>Return to search</button></>;
}
// Fake timers here must be advanced in small steps: a single large jump can leave a
// timer that a microtask schedules mid-tick unfired until a later tick, under this
// environment's fake-timer/microtask interleaving.
async function advance(totalMs: number) {
  for (let elapsed = 0; elapsed < totalMs; elapsed += 250) {
    await act(async () => { await vi.advanceTimersByTimeAsync(250); });
  }
}

function mount(initial = "/search") {
  const context = { client: makeFixtureNodeClient(), confirm: vi.fn(), notify: vi.fn() } as unknown as AppOutletContext;
  render(<MemoryRouter initialEntries={[initial]}><Routes><Route element={<main className="app-main"><Outlet context={context} /></main>}>
    <Route path="/search" element={<Search />} /><Route path="/target" element={<Target />} />
  </Route></Routes></MemoryRouter>);
  return userEvent.setup();
}

it("starts empty and explains the local scope without querying all records", async () => {
  mount();
  expect(await screen.findByText(/Enter keywords to search local data/)).toBeInTheDocument();
  expect(localSearch.query).not.toHaveBeenCalled();
});

it("does not invent a 1970 date for synced metadata without a local timestamp", async () => {
  vi.mocked(localSearch.query).mockResolvedValue({ ...page(), results: [{ ...result("Remote article"), timestamp: 0 }], total: 1 });
  const user = mount();
  await user.type(screen.getByLabelText("Search keywords"), "Remote");
  expect(await screen.findByText(/Date unavailable/)).toBeInTheDocument();
  expect(screen.queryByText(/1970/)).not.toBeInTheDocument();
});

it("discards late replies from older keywords", async () => {
  let finish!: (value: SearchPage) => void;
  vi.mocked(localSearch.query).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; })).mockResolvedValue(page("New result"));
  mount();
  fireEvent.change(screen.getByLabelText("Search keywords"), { target: { value: "old" } });
  await waitFor(() => expect(localSearch.query).toHaveBeenCalledTimes(1));
  fireEvent.change(screen.getByLabelText("Search keywords"), { target: { value: "new" } });
  expect(await screen.findByRole("heading", { name: "New result" })).toBeInTheDocument();
  await act(async () => finish(page("Old private result")));
  expect(screen.queryByText("Old private result")).not.toBeInTheDocument();
});

it("restores keywords and filters after opening a result", async () => {
  const user = mount();
  fireEvent.change(screen.getByLabelText("Search keywords"), { target: { value: "春天 Python" } });
  await user.selectOptions(screen.getByLabelText("Search type"), "saved");
  expect(await screen.findByRole("heading", { name: "Current result" })).toBeInTheDocument();
  await user.click(screen.getByRole("link", { name: "Read local content" }));
  expect(await screen.findByText("Opened result")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Return to search" }));
  expect(screen.getByLabelText("Search keywords")).toHaveValue("春天 Python");
  expect(screen.getByLabelText("Search type")).toHaveValue("saved");
  expect(await screen.findByRole("heading", { name: "Current result" })).toBeInTheDocument();
});

it("moves keyboard focus to the first newly loaded result when pagination ends", async () => {
  const first = { ...page("First result"), total: 3, next_cursor: "second" };
  const second = { ...page("New result", "Last result"), total: 3 };
  vi.mocked(localSearch.query).mockImplementation(async (request) => request.cursor === "second" ? second : first);
  const user = mount();
  fireEvent.change(screen.getByLabelText("Search keywords"), { target: { value: "result" } });
  const more = await screen.findByRole("button", { name: "Load more results" });
  more.focus();
  await user.keyboard("{Enter}");
  await screen.findByRole("heading", { name: "Last result" });
  expect(screen.queryByRole("button", { name: "Load more results" })).not.toBeInTheDocument();
  expect(screen.getAllByRole("link", { name: "Read local content" })[1]).toHaveFocus();
});

it("does not move focus back to results if the user left pagination while waiting", async () => {
  let finish!: (value: SearchPage) => void;
  vi.mocked(localSearch.query).mockImplementation(async (request) => request.cursor === "second"
    ? new Promise((resolve) => { finish = resolve; })
    : { ...page("First result"), total: 2, next_cursor: "second" });
  const user = mount();
  fireEvent.change(screen.getByLabelText("Search keywords"), { target: { value: "result" } });
  await user.click(await screen.findByRole("button", { name: "Load more results" }));
  screen.getByLabelText("Search source").focus();
  await act(async () => finish({ ...page("New result"), total: 2 }));
  expect(screen.getByLabelText("Search source")).toHaveFocus();
});

it("reloads every previously loaded page before restoring the app content scroller", async () => {
  const first = { ...page(...Array.from({ length: 20 }, (_, i) => `Row ${i}`)), total: 40, next_cursor: "second" };
  const second = { ...page(...Array.from({ length: 20 }, (_, i) => `Row ${i + 20}`)), total: 40 };
  vi.mocked(localSearch.query).mockImplementation(async (request) => request.cursor === "second" ? second : first);
  const user = mount();
  fireEvent.change(screen.getByLabelText("Search keywords"), { target: { value: "rows" } });
  await user.click(await screen.findByRole("button", { name: "Load more results" }));
  expect(await screen.findByRole("heading", { name: "Row 39" })).toBeInTheDocument();
  const scroller = screen.getByRole("main");
  scroller.scrollTop = 1600;
  await user.click(screen.getAllByRole("link", { name: "Read local content" })[39]);
  expect(await screen.findByText("Opened result")).toBeInTheDocument();
  scroller.scrollTop = 0;
  await user.click(screen.getByRole("button", { name: "Return to search" }));
  expect(await screen.findByRole("heading", { name: "Row 39" })).toBeInTheDocument();
  await waitFor(() => expect(scroller.scrollTop).toBe(1600));
  expect(screen.getAllByRole("link", { name: "Read local content" })).toHaveLength(40);
  expect(window.scrollTo).not.toHaveBeenCalled();
});

it("keeps the previously loaded page and shows an inline error when pagination reports a changed result set", async () => {
  vi.mocked(localSearch.query).mockResolvedValueOnce({ ...page("Old result"), next_cursor: "next" })
    .mockRejectedValueOnce(new Error("Results changed. Refresh to start from the first page."))
    .mockResolvedValue(page("Updated result"));
  const user = mount();
  fireEvent.change(screen.getByLabelText("Search keywords"), { target: { value: "record" } });
  const loadMore = await screen.findByRole("button", { name: "Load more results" });
  await user.click(loadMore);
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Results changed");
  expect(loadMore.nextElementSibling).toBe(alert);
  expect(screen.getByRole("heading", { name: "Old result" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Refresh results" }));
  expect(await screen.findByRole("heading", { name: "Updated result" })).toBeInTheDocument();
});

it("keeps loaded results and an adjacent inline error when loading more results fails outright", async () => {
  const first = { ...page("First result"), total: 2, next_cursor: "second" };
  vi.mocked(localSearch.query).mockImplementation(async (request) =>
    request.cursor === "second" ? Promise.reject(new Error("Load more failed")) : first);
  const user = mount();
  fireEvent.change(screen.getByLabelText("Search keywords"), { target: { value: "result" } });
  const loadMore = await screen.findByRole("button", { name: "Load more results" });
  await user.click(loadMore);
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Load more failed");
  expect(loadMore.nextElementSibling).toBe(alert);
  expect(screen.getByRole("heading", { name: "First result" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Load more results" })).toBeInTheDocument();
});

it("stops automatic re-queries after 10 rounds against a stuck index and lets Refresh try again", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
  try {
    vi.mocked(localSearch.query).mockResolvedValue({ ...page("Pending result"), partial: true, indexing_pending: true });
    mount();
    fireEvent.change(screen.getByLabelText("Search keywords"), { target: { value: "pending" } });
    await advance(250);
    expect(localSearch.query).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/The index is still being built/)).not.toBeInTheDocument();
    for (let round = 0; round < 10; round++) {
      await advance(3250);
    }
    expect(localSearch.query).toHaveBeenCalledTimes(11);
    expect(screen.getByText("The index is still being built. Refresh to check again.")).toBeInTheDocument();
    await advance(10000);
    expect(localSearch.query).toHaveBeenCalledTimes(11);
    fireEvent.click(screen.getByRole("button", { name: "Refresh results" }));
    await advance(250);
    expect(localSearch.query).toHaveBeenCalledTimes(12);
    expect(screen.queryByText(/The index is still being built/)).not.toBeInTheDocument();
  } finally {
    vi.useRealTimers();
  }
});

it("resets the automatic re-query counter when the query changes", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
  try {
    vi.mocked(localSearch.query).mockResolvedValue({ ...page("Pending result"), partial: true, indexing_pending: true });
    mount();
    fireEvent.change(screen.getByLabelText("Search keywords"), { target: { value: "pending" } });
    await advance(250);
    for (let round = 0; round < 10; round++) {
      await advance(3250);
    }
    expect(screen.getByText(/The index is still being built/)).toBeInTheDocument();
    expect(localSearch.query).toHaveBeenCalledTimes(11);
    fireEvent.change(screen.getByLabelText("Search keywords"), { target: { value: "pending again" } });
    await advance(250);
    expect(screen.queryByText(/The index is still being built/)).not.toBeInTheDocument();
    expect(localSearch.query).toHaveBeenCalledTimes(12);
    for (let round = 0; round < 10; round++) {
      await advance(3250);
    }
    expect(localSearch.query).toHaveBeenCalledTimes(22);
    expect(screen.getByText(/The index is still being built/)).toBeInTheDocument();
  } finally {
    vi.useRealTimers();
  }
});

it("gives a filter change (not the keyword) its own fresh re-query budget", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
  try {
    vi.mocked(localSearch.query).mockResolvedValue({ ...page("Pending result"), partial: true, indexing_pending: true });
    mount();
    fireEvent.change(screen.getByLabelText("Search keywords"), { target: { value: "pending" } });
    await advance(250);
    for (let round = 0; round < 10; round++) {
      await advance(3250);
    }
    expect(screen.getByText(/The index is still being built/)).toBeInTheDocument();
    expect(localSearch.query).toHaveBeenCalledTimes(11);
    fireEvent.change(screen.getByLabelText("Search type"), { target: { value: "saved" } });
    await advance(250);
    expect(screen.queryByText(/The index is still being built/)).not.toBeInTheDocument();
    expect(localSearch.query).toHaveBeenCalledTimes(12);
    for (let round = 0; round < 10; round++) {
      await advance(3250);
    }
    expect(localSearch.query).toHaveBeenCalledTimes(22);
    expect(screen.getByText(/The index is still being built/)).toBeInTheDocument();
  } finally {
    vi.useRealTimers();
  }
});

it("distinguishes partial indexing from no matches and recovers from a failed rebuild", async () => {
  vi.mocked(localSearch.query).mockResolvedValue({ ...page(), partial: true });
  vi.mocked(localSearch.rebuild).mockRejectedValueOnce(new Error("Rebuild failed safely")).mockResolvedValue(status);
  const user = mount();
  fireEvent.change(screen.getByLabelText("Search keywords"), { target: { value: "missing" } });
  expect(await screen.findByText(/Showing partial results/)).toBeInTheDocument();
  expect(screen.queryByText(/No matches/)).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Rebuild search index" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Rebuild failed safely");
  await waitFor(() => expect(screen.getByRole("button", { name: "Rebuild search index" })).toBeEnabled());
  vi.mocked(localSearch.query).mockResolvedValue(page());
  await user.click(screen.getByRole("button", { name: "Rebuild search index" }));
  expect(await screen.findByText(/No matches/)).toBeInTheDocument();
});

it("keeps healthy results visible with a document recovery link and no false indexing message", async () => {
  vi.mocked(localSearch.query).mockResolvedValue({ ...page('Healthy result'), partial: true,
    indexing_pending: false, unavailable_sources: ['saved_documents'] });
  const user = mount();
  fireEvent.change(screen.getByLabelText('Search keywords'), { target: { value: 'healthy' } });
  expect(await screen.findByRole('heading', { name: 'Healthy result' })).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Review document copies in Settings' })).toHaveAttribute('href', '/settings');
  expect(screen.queryByText(/while the index catches up/)).not.toBeInTheDocument();
  expect(screen.queryByText(/No matches/)).not.toBeInTheDocument();
  vi.mocked(localSearch.query).mockResolvedValue(page('Healthy result', 'Repaired document'));
  await user.click(screen.getByRole('button', { name: 'Refresh results' }));
  expect(await screen.findByRole('heading', { name: 'Repaired document' })).toBeInTheDocument();
  expect(screen.queryByRole('link', { name: 'Review document copies in Settings' })).not.toBeInTheDocument();
});

it("explains unavailable downloaded content without claiming a complete empty search", async () => {
  vi.mocked(localSearch.query).mockResolvedValue({ ...page(), partial: true,
    indexing_pending: false, unavailable_sources: ['offline_downloads'] });
  mount();
  fireEvent.change(screen.getByLabelText('Search keywords'), { target: { value: 'download' } });
  expect(await screen.findByRole('link', { name: 'Check offline reading' })).toHaveAttribute('href', '/offline');
  expect(screen.queryByText(/No matches/)).not.toBeInTheDocument();
  expect(screen.queryByText(/while the index catches up/)).not.toBeInTheDocument();
});

it("warns when the index has skipped rows and stays silent when there are none", async () => {
  vi.mocked(localSearch.status).mockResolvedValue({ ...status, skipped_rows: 1 });
  mount();
  expect(await screen.findByText("1 item could not be indexed and will not appear in results.")).toBeInTheDocument();
});

it("does not warn about skipped rows when the count is zero or absent", async () => {
  mount();
  await screen.findByText(/Index: ready/);
  expect(screen.queryByText(/could not be indexed/)).not.toBeInTheDocument();
});

it("rechecks an opened result and shows revoked or deleted content as unavailable", async () => {
  vi.spyOn(localSearch, "open").mockRejectedValue(new Error("This result was removed or access changed."));
  mount("/search?open=card%3Aone");
  expect(await screen.findByRole("alert")).toHaveTextContent("removed or access changed");
  expect(localSearch.open).toHaveBeenCalledWith("card:one");
  expect(localSearch.query).not.toHaveBeenCalled();
});

it("highlights Unicode codepoints safely instead of interpreting content as HTML", () => {
  const value = { ...snippet("😀道路<script>"), matches: [[1, 3]] as [number, number][] };
  const { container } = render(<Highlight value={value} />);
  expect(Array.from(container.querySelectorAll("mark")).map((node) => node.textContent).join("")).toBe("道路");
  expect(container.querySelector("script")).toBeNull();
  expect(container.textContent).toBe("😀道路<script>");
});
