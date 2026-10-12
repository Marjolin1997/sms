import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Dev: proxy drejt API-së Central (CENTRAL_API_URL), pa CORS. Prodhim: shërbe dist/ nga i njëjti origin me Central API.
// Ky aplikacion NUK flet me API-n Enterprise (/v1/*) dhe nuk ndan auth me të.
const api = process.env.CENTRAL_API_URL || "http://127.0.0.1:8001";
export default defineConfig({
  plugins: [react()],
  server: { port: 5174, proxy: { "/auth": api, "/admin": api, "/healthz": api } },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.js"],
    include: ["src/**/*.test.{js,jsx}"],
  },
});
