import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // Tauri: keep Rust/Vite logs visible and pin the dev port the shell expects.
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api/local": {
        target: "http://127.0.0.1:8791",
        changeOrigin: true,
      },
    },
  },
});
