import { execFileSync } from "node:child_process";
import { describe, expect, it } from "vitest";

describe("i18n coverage script", () => {
  it("passes on the current sources (no missing/unused/mismatched translations)", () => {
    const out = execFileSync("node", ["scripts/i18n-check.mjs"], { encoding: "utf8" });
    expect(out).toMatch(/i18n: në rregull/);
  });
});
