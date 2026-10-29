import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { OfficePreview } from "./OfficePreview";
import type { FilePreview } from "../domain/agentFilePreview";

const workbook: FilePreview = { kind: "spreadsheet", truncated: false, formulas_uncalculated: true, sheets: [
  { name: "Sales", columns: 3, total_rows: 3, total_columns: 3, truncated: false, rows: [
    { index: 1, cells: [{ column: 1, value: "<script>unsafe()</script>" }, { column: 3, value: "Revenue" }] },
    { index: 3, cells: [{ column: 3, value: "=SUM(C1:C2)", formula: "=SUM(C1:C2)" }] }] },
  { name: "Quarter summary", columns: 1, total_rows: 1, total_columns: 1, truncated: false,
    rows: [{ index: 1, cells: [{ column: 1, value: "Quarter total" }] }] },
] };

it("keeps worksheet coordinates, displays unsaved formulas and switches sheets without running markup", async () => {
  const user = userEvent.setup();
  const { container } = render(<OfficePreview load={vi.fn().mockResolvedValue(workbook)} />);
  const panel = await screen.findByRole("tabpanel");
  const rows = within(panel).getAllByRole("row");
  expect(within(rows[1]).getAllByRole("cell").map(cell => cell.textContent)).toEqual(["<script>unsafe()</script>", "", "Revenue"]);
  expect(within(rows[2]).getAllByRole("cell").map(cell => cell.textContent)).toEqual(["", "", ""]);
  expect(within(rows[3]).getAllByRole("cell")[2]).toHaveAttribute("title", "=SUM(C1:C2)");
  expect(container.querySelector("script,iframe,object")).toBeNull();
  expect(screen.getByRole("note")).toHaveTextContent("Some formulas have no saved result");
  await user.click(screen.getByRole("tab", { name: "Quarter summary" }));
  expect(screen.getByRole("tab", { name: "Quarter summary" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByText("Quarter total")).toBeVisible();
  expect(screen.queryByText("Revenue")).not.toBeInTheDocument();
});

it("renders Word paragraphs and tables with inert formatting", async () => {
  const document: FilePreview = { kind: "document", truncated: false, blocks: [
    { type: "paragraph", style: "Heading1", runs: [{ text: "Sales report", bold: true, italic: false }] },
    { type: "table", rows: [["Store", "Revenue"], ["Shanghai", "1,234"]] },
    { type: "paragraph", style: "", runs: [{ text: "<img src=x onerror=alert(1)>", bold: false, italic: true }] },
  ] };
  const { container } = render(<OfficePreview load={vi.fn().mockResolvedValue(document)} />);
  expect(await screen.findByText("Sales report")).toBeVisible();
  expect(screen.getByRole("table")).toHaveTextContent("Shanghai");
  expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeVisible();
  expect(container.querySelector("img,script")).toBeNull();
});

it("navigates presentation slides and explains partial previews", async () => {
  const user = userEvent.setup();
  const slides: FilePreview = { kind: "presentation", truncated: true, total_slides: 2,
    slides: [{ number: 1, paragraphs: ["Quarter two", "Sales increased"] }, { number: 2, paragraphs: ["Quarter three", "New plan"] }] };
  render(<OfficePreview load={vi.fn().mockResolvedValue(slides)} />);
  expect(await screen.findByRole("heading", { name: "Quarter two" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Previous slide" })).toBeDisabled();
  expect(screen.getByRole("note")).toHaveTextContent("Showing a limited content preview");
  await user.click(screen.getByRole("button", { name: "Next slide" }));
  expect(screen.getByRole("heading", { name: "Quarter three" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Next slide" })).toBeDisabled();
});

it("aborts obsolete reads and does not let old bytes replace a newer file preview", async () => {
  let resolveOld!: (value: FilePreview) => void;
  const old = vi.fn((_: AbortSignal) => new Promise<FilePreview>(resolve => { resolveOld = resolve; }));
  const fresh = vi.fn().mockResolvedValue({ kind: "document", truncated: false,
    blocks: [{ type: "paragraph", style: "", runs: [{ text: "New version", bold: false, italic: false }] }] });
  const view = render(<OfficePreview load={old} />);
  await waitFor(() => expect(old).toHaveBeenCalled());
  view.rerender(<OfficePreview load={fresh} />);
  expect(await screen.findByText("New version")).toBeVisible();
  expect(old.mock.calls[0][0].aborted).toBe(true);
  await act(async () => resolveOld(workbook));
  expect(screen.queryByText("Revenue")).not.toBeInTheDocument();
  expect(screen.getByText("New version")).toBeVisible();
});

it("shows an actionable legacy conversion error and retries loading", async () => {
  const user = userEvent.setup();
  const load = vi.fn().mockRejectedValueOnce(new Error("agent_preview_converter_missing")).mockResolvedValueOnce(workbook);
  render(<OfficePreview load={load} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("needs LibreOffice installed on the node");
  await user.click(screen.getByRole("button", { name: "Retry preview" }));
  expect(await screen.findByText("Revenue")).toBeVisible();
  expect(load).toHaveBeenCalledTimes(2);
});
