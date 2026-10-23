import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

// Dev: the SPA runs on :5173 and proxies API/auth calls to the FastAPI server
// on :3000 so cookies stay first-party. Production: FastAPI serves dist/
// itself, so relative URLs just work with no proxy at all.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: 5173,
    host: "127.0.0.1",  // match the API's host
    proxy: {
      "/api": {
        target: "http://127.0.0.1:3000",
        changeOrigin: false,  // same host, don't change origin
      },
      "/auth": {
        target: "http://127.0.0.1:3000",
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
