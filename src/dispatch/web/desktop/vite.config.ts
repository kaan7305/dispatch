import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// During dev, Vite serves the SPA on 5173 and proxies /api + /ws to the
// daemon running on 8001. Build output goes to dist/, which the daemon's
// local_app.py mounts via StaticFiles when present.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8001",
      "/ws": { target: "ws://127.0.0.1:8001", ws: true },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2020",
  },
});
