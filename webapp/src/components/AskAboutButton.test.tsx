import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { askHistory } from "../domain/askHistory";
import AskAboutButton from "./AskAboutButton";

afterEach(() => vi.restoreAllMocks());

function mount(itemId = "import:article", offlineJobId?: string) {
  let location = "";
  render(
    <MemoryRouter initialEntries={["/reading/article"]}>
      <Routes>
        <Route path="/reading/article" element={<AskAboutButton itemId={itemId} offlineJobId={offlineJobId} />} />
        <Route path="/ask" element={<LocationProbe onRender={(value) => { location = value; }} />} />
      </Routes>
    </MemoryRouter>,
  );
  return { user: userEvent.setup(), getLocation: () => location };
}
function LocationProbe({ onRender }: { onRender: (value: string) => void }) {
  onRender(window.location.pathname + window.location.search);
  return <p>Ask screen</p>;
}

it("prepares the article's context and navigates to Ask Ryn with the prepared source", async () => {
  const prepare = vi.spyOn(askHistory, "prepareContext").mockResolvedValue({
    library_id: "import:imp_" + "a".repeat(64), title: "Article", source_url: "", sha256: "b".repeat(64),
    extraction_truncated: false, text_bytes: 100,
  });
  const { user } = mount("import:article", "job-1");
  expect(screen.getByRole("button", { name: "Ask about this content" })).toBeEnabled();
  await user.click(screen.getByRole("button", { name: "Ask about this content" }));
  expect(prepare).toHaveBeenCalledWith("import:article", "job-1");
  expect(await screen.findByText("Ask screen")).toBeInTheDocument();
});

it("shows Preparing article while busy and an honest error on failure, without navigating", async () => {
  let resolveReject!: () => void;
  vi.spyOn(askHistory, "prepareContext").mockImplementation(() => new Promise((_resolve, reject) => {
    resolveReject = () => reject(new Error("This document is not available locally. Open it and retry sharing."));
  }));
  const { user } = mount();
  await user.click(screen.getByRole("button", { name: "Ask about this content" }));
  expect(screen.getByRole("button", { name: "Preparing article…" })).toBeDisabled();
  resolveReject();
  expect(await screen.findByRole("alert")).toHaveTextContent("This document is not available locally. Open it and retry sharing.");
  expect(screen.getByRole("button", { name: "Ask about this content" })).toBeEnabled();
  expect(screen.queryByText("Ask screen")).not.toBeInTheDocument();
});
