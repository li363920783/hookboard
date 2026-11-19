import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { viteSingleFile } from "vite-plugin-singlefile";

// The hub serves exactly one file: web/index.html. Everything is inlined into
// it at build time so there is still nothing else to serve, and it works offline.
export default defineConfig({
  plugins: [react(), tailwindcss(), viteSingleFile()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8899",
      "/a": "http://127.0.0.1:8899",
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});