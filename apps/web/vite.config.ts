import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * The API and the UI are separate dev servers but one origin in production: the
 * build lands in dist/ and FastAPI serves it, so there is a single process and no
 * CORS in the deployed path. The dev proxy mirrors that shape.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
