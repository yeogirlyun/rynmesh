import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { askHistory, type AskSource } from "../domain/askHistory";
import AskMaterials, { AskAnswerSources } from "./AskMaterials";

afterEach(() => vi.restoreAllMocks());

const source: AskSource = { library_id: "import:imp_" + "a".repeat(64), title: "Article title",
  source_url: "https://example.test/article", sha256: "b".repeat(64), extraction_truncated: false,
  text_bytes: 5000, text: "Full article body" };

it("loads and shows each material's source, then removes it on request", async () => {
  vi.spyOn(askHistory, "context").mockResolvedValue(source);
  const onRemove = vi.fn().mockResolvedValue(undefined);
  render(<AskMaterials ids={[source.library_id]} onRemove={onRemove} />);
  expect(screen.getByText("Reading local article copy…")).toBeInTheDocument();
  expect(await screen.findByText("Article title", { exact: false })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: source.source_url })).toHaveAttribute("href", source.source_url);
  expect(screen.getByText("The model receives only the material shown in the send review.")).toBeInTheDocument();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Remove article from context" }));
  expect(onRemove).toHaveBeenCalledWith(source.library_id);
});

it("shows the supplied excerpt budget when a byte limit truncates the source", async () => {
  vi.spyOn(askHistory, "context").mockResolvedValue(source);
  render(<AskMaterials ids={[source.library_id]} byteLimits={[1000]} />);
  expect(await screen.findByText(/Supplied excerpt: 1000 bytes of 5000\. Truncated for the context budget\./)).toBeInTheDocument();
});

it("shows an honest error and allows retrying the source lookup on failure", async () => {
  const context = vi.spyOn(askHistory, "context")
    .mockRejectedValueOnce(new Error("This offline version changed. Reopen it."))
    .mockResolvedValue(source);
  render(<AskMaterials ids={[source.library_id]} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("This offline version changed. Reopen it.");
  expect(context).toHaveBeenCalledWith(source.library_id);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Retry source" }));
  expect(context).toHaveBeenCalledTimes(2);
  expect(await screen.findByText("Article title", { exact: false })).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("reveals sources for an answer only after the disclosure is opened", async () => {
  vi.spyOn(askHistory, "context").mockResolvedValue(source);
  render(<AskAnswerSources ids={[source.library_id]} />);
  expect(screen.queryByText("Reading local article copy…")).not.toBeInTheDocument();
  const user = userEvent.setup();
  await user.click(screen.getByText("Sources supplied for this answer"));
  expect(await screen.findByText("Article title", { exact: false })).toBeInTheDocument();
});
