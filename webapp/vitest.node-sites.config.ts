import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";
export default defineConfig({ plugins: [react()], define: { "import.meta.env.VITE_RYN_NODE_BASE_URL": JSON.stringify("http://localhost/api/local") }, test: { include: ["src/screens/NodeSite*.test.tsx"], environment: "jsdom", setupFiles: ["./src/test/node-sites-setup.ts"], clearMocks: true, restoreMocks: true, maxWorkers: 1 } });
