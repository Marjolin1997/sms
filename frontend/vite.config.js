import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: proxy drejt API-së, pa CORS. Prodhim: shërbe dist/ nga i njëjti origin me API-n.
const api = process.env.SMS_API_URL || "http://127.0.0.1:8000";
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/v1": api, "/healthz": api, "/readyz": api } },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.js"],
    include: ["src/**/*.test.{js,jsx}"],
  },
});
