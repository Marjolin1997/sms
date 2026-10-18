import { describe, expect, it } from "vitest";
import { accountingDate, exactUtc, formatAmount, formatMoney, formatQuantity, level } from "../commercial/format.js";

const digits = (s) => s.replace(/\D/g, "");

describe("money formatting is string based and exact", () => {
  it("keeps every digit of values beyond Number precision", () => {
    expect(digits(formatAmount("9007199254740993.010000"))).toBe("900719925474099301");
    expect(digits(formatAmount("12345678901234.123456"))).toBe("12345678901234123456");
  });
  it("shows at least 2 and at most 6 decimals, never rounds", () => {
    expect(digits(formatAmount("5"))).toBe("500");
    expect(digits(formatAmount("5.5"))).toBe("550");
    expect(digits(formatAmount("0.000001"))).toBe("0000001");
    expect(digits(formatAmount("10.100000"))).toBe("1010");
    expect(digits(formatAmount("0.29"))).toBe("029");
  });
  it("keeps the sign and the currency; invalid or non-string input is a dash", () => {
    expect(formatAmount("-12.5").startsWith("-")).toBe(true);
    expect(formatMoney("1.00", "EUR").endsWith(" EUR")).toBe(true);
    for (const bad of [null, undefined, 12.5, "", "abc", "1e5", "1,5", "--1"]) expect(formatAmount(bad)).toBe("-");
    expect(formatMoney("x", "EUR")).toBe("-");
  });
  it("quantities keep exactly what the server sent", () => {
    expect(digits(formatQuantity("1500"))).toBe("1500");
    expect(formatQuantity("2.50")).toMatch(/2.5$/);
    expect(formatQuantity(3)).toBe("-");
  });
  it("accounting dates are UTC and do not shift with the browser timezone", () => {
    expect(digits(accountingDate("2026-03-01T00:00:00+00:00"))).toMatch(/01032026|03012026|20260301/);
    expect(digits(accountingDate("2026-03-01T23:59:59+00:00"))).toBe(digits(accountingDate("2026-03-01T00:00:00+00:00")));
    expect(exactUtc("2026-03-01T10:00:00+00:00")).toMatch(/UTC$/);
    expect(accountingDate(null)).toBe("-");
  });
  it("server status levels: anything unrecognised is unknown, never pass", () => {
    expect(level("PASS")).toBe("pass");
    expect(level("CRITICAL")).toBe("critical");
    for (const v of [undefined, null, "", "OK", "healthy", 1]) expect(level(v)).toBe("unknown");
  });
});
