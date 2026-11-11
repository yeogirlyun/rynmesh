import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";

// Optional company-sharing reads of the model pool: absent unless a test installs a company plugin.
export const server = setupServer(
  http.get("*/api/local/peer-providers", () => HttpResponse.json({ providers: [] })),
  http.get("*/api/local/llm/models/usage/company", () => HttpResponse.json({ day: "", sharing: false, published_service_ids: [], rows: [] })),
);
