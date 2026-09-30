// Testet ekzekutohen në anglisht; gjuha lexohet nga localStorage kur ngarkohet i18n.
try { localStorage.setItem("sms_lang", "en"); } catch { /* jsdom */ }
import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  try { localStorage.clear(); sessionStorage.clear(); } catch { /* jsdom */ }
  location.hash = "";
});
