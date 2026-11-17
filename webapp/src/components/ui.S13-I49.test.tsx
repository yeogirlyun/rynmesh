import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { ContentRow, RecommendationCard } from "./ui";

afterEach(cleanup);

it.each([true, false])("T3: recommendation starter=%s shows truthful badges", async (starter) => {
  const client = makeFixtureNodeClient();
  const rec = (await client.requestRecommendations())[0];
  const item = await client.getContentItem(rec.contentId);
  render(<RecommendationCard rec={rec} item={{ ...item, starter, safety_outcome: "passed", provenance_status: "signed" }}
    publisher={undefined} onInspect={vi.fn()} onFetchPreview={vi.fn()} onFetchFull={vi.fn()} />);
  if (starter) {
    expect(screen.getByText("Guide example")).toBeInTheDocument();
    expect(screen.queryByText("Passed")).not.toBeInTheDocument();
    expect(screen.queryByText("Signed")).not.toBeInTheDocument();
  } else {
    expect(screen.getByText("Passed")).toBeInTheDocument();
    expect(screen.getByText("Signed")).toBeInTheDocument();
  }
});

it("T3: content table also hides legacy starter verification badges", async () => {
  const client = makeFixtureNodeClient();
  const rec = (await client.requestRecommendations())[0];
  const item = await client.getContentItem(rec.contentId);
  render(<table><tbody><ContentRow item={{ ...item, starter: true }} publisher={undefined} provider={undefined}
    selected={false} onSelect={vi.fn()} onOpen={vi.fn()} /></tbody></table>);
  expect(screen.getByText("Guide example")).toBeInTheDocument();
  expect(screen.queryByText("Passed")).not.toBeInTheDocument();
  expect(screen.queryByText("Signed")).not.toBeInTheDocument();
});

