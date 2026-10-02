import path from "node:path";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
// From vitest/config, not vite: it is the one that types the `test` key.
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    // shadcn/ui generates imports as "@/components/ui/...", so the alias has to
    // exist in both vite and tsconfig or one of them resolves and the other
    // does not.
    alias: { "@": path.resolve(import.meta.dirname, "./src") },
  },
  server: {
    port: 5173,
    proxy: {
      // The backend runs on 8000 in development, so the app can call /api
      // relative paths and needs no base-URL configuration.
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        rewrite: (requestPath) => requestPath.replace(/^\/api/, ""),
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: true,
  },
});
