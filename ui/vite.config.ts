import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The daemon serves the build from src/rufur/static; `npm run dev` proxies API calls to it.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  build: {
    outDir: "../src/rufur/static",
    emptyOutDir: true,
    rollupOptions: { output: { manualChunks: { charts: ["recharts"], ui: ["radix-ui", "lucide-react"] } } },
  },
  server: { proxy: { "/v1": process.env.RUFUR_URL ?? "http://127.0.0.1:7437" } },
});
