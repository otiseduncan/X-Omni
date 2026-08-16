import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies to Core so the browser talks to one origin and
// session cookies work the same as they do in production.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: "127.0.0.1",
    proxy: {
      "/api": { target: "http://127.0.0.1:8100", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8100", ws: true },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
