import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { expect, it } from "vitest";
import { LegacyAgentRedirect } from "./App";

function Destination() {
  const location = useLocation();
  return <output aria-label="Destination">{location.pathname}{location.search}</output>;
}
function route(entry: string) {
  render(<MemoryRouter initialEntries={[entry]}><Routes>
    <Route path="/ask" element={<LegacyAgentRedirect />} />
    <Route path="/agent" element={<Destination />} />
    <Route path="/services/private-ai/chat" element={<Destination />} />
    <Route path="/ask/history" element={<Destination />} />
  </Routes></MemoryRouter>);
}

it("opens the independent Agent from the plain legacy entry", () => {
  route("/ask");
  expect(screen.getByLabelText("Destination")).toHaveTextContent("/agent");
});

it.each(["material=document-1", "conversation=chat-1", "peer=peer-1", "service=model-1", "network=main", "new=1"])(
  "preserves the manual chat context in legacy link %s", query => {
    route(`/ask?${query}`);
    expect(screen.getByLabelText("Destination")).toHaveTextContent(`/services/private-ai/chat?${query}`);
  },
);

it("keeps old history links on their existing route", () => {
  route("/ask/history?conversation=chat-1");
  expect(screen.getByLabelText("Destination")).toHaveTextContent("/ask/history?conversation=chat-1");
});
