import path from "node:path";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
// From vitest/config, not vite: it is the one that types the `test` key.
import { defineConfig } from "vitest/config";

/**
 * The /api prefix, stripped and forwarded to the backend.
 *
 * The app calls relative `/api/...` paths and so needs no base-URL setting
 * anywhere; this is the one place that knows where the backend is.
 */
function apiProxy(port: number) {
  return {
    "/api": {
      target: `http://localhost:${port}`,
      changeOrigin: true,
      rewrite: (requestPath: string) => requestPath.replace(/^\/api/, ""),
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    // shadcn/ui generates imports as "@/components/ui/...", so the alias has to
    // exist in both vite and tsconfig or one of them resolves and the other
    // does not.
    //
    // "@shared" points at the repo-level `shared/` directory, which holds the
    // SSE event contract. Without it the contract was shared only in name: the
    // test compared its shape to the backend's, but no application code could
    // import the types, so the browser and the server agreed by coincidence
    // rather than by compilation.
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
      "@shared": path.resolve(import.meta.dirname, "../shared"),
    },
  },
  server: { port: 5173, proxy: apiProxy(8000) },
  // `preview` does **not** inherit `server.proxy`, so until this existed
  // `pnpm preview` served the built app next to a backend it could not reach
  // and every request 404'd against Vite itself. Nobody noticed because
  // nobody ran it; the browser test runs it, and needs the backend the e2e
  // launcher starts rather than a developer's own.
  preview: { port: 4173, proxy: apiProxy(Number(process.env.E2E_API_PORT) || 8000) },
  test: {
    // Vitest's default include matches `*.spec.ts` too, so without this it
    // collects the Playwright spec, fails to resolve `@playwright/test`, and
    // reports a failing file beside 160 passing tests. The browser tests have
    // their own runner: `pnpm test:e2e`.
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: true,
  },
});
