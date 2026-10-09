import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const target = process.env.WARDEN_API ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "../backend/static", emptyOutDir: true },
  server: {
    port: 5173,
    proxy: {
      "/api": target,
      "/mcp": target,
      "/demo": target,
      "/webhooks": target,
      "/ws": { target: target.replace(/^http/, "ws"), ws: true },
    },
  },
});
