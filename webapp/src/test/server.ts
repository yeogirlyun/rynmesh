import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";

// Optional company-sharing and plugin-service-update reads: absent unless a test installs them.
export const server = setupServer(
  http.get("*/api/local/peer-providers", () => HttpResponse.json({ providers: [] })),
  http.get("*/api/local/llm/models/usage/company", () => HttpResponse.json({ day: "", sharing: false, published_service_ids: [], rows: [] })),
  http.get("*/api/local/service-updates", () => HttpResponse.json({ services: [] })),
);
