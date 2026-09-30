import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { I18nProvider, LangSwitch, T, getLang, locale, t, tn } from "../i18n.jsx";
import sq from "../locales/sq/index.js";

describe("t()", () => {
  it("returns the English key when the language is English", () => {
    expect(getLang()).toBe("en");
    expect(t("Save")).toBe("Save");
  });
  it("fills {placeholders} and leaves unknown ones visible", () => {
    expect(t("Row {n}", { n: 3 })).toBe("Row 3");
    expect(t("Row {n}")).toBe("Row {n}");
    expect(t("Hi {a} {b}", { a: "x" })).toBe("Hi x {b}");
  });
  it("tn() picks singular for 1 and plural otherwise", () => {
    expect(tn(1, "{n} item", "{n} items")).toBe("1 item");
    expect(tn(4, "{n} item", "{n} items")).toBe("4 items");
    expect(tn(0, "{n} item", "{n} items")).toBe("0 items");
  });
  it("T() is a no-op marker", () => {
    expect(T("Anything")).toBe("Anything");
  });
});

describe("language switch", () => {
  it("switches the whole tree to Albanian and back, and remembers the choice", async () => {
    const user = userEvent.setup();
    function Probe() { return <p data-testid="p">{t("Cancel")}</p>; }
    render(<I18nProvider><Probe /><LangSwitch /></I18nProvider>);
    expect(screen.getByTestId("p")).toHaveTextContent("Cancel");
    await user.click(screen.getByRole("button", { name: "Shqip" }));
    expect(screen.getByTestId("p")).toHaveTextContent(sq["Cancel"]);
    expect(localStorage.getItem("sms_lang")).toBe("sq");
    expect(locale()).toBe("sq-AL");
    expect(document.documentElement.lang).toBe("sq");
    await user.click(screen.getByRole("button", { name: "English" }));
    expect(screen.getByTestId("p")).toHaveTextContent("Cancel");
    expect(locale()).toBe("en-GB");
  });
});

describe("Albanian dictionary", () => {
  it("has no empty translations", () => {
    for (const [k, v] of Object.entries(sq)) expect(String(v).trim(), k).not.toBe("");
  });
  it("keeps the same {variables} as the English key where the key has any", () => {
    const vars = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort().join(",");
    for (const [k, v] of Object.entries(sq)) if (/\{\w+\}/.test(k)) expect(vars(v), k).toBe(vars(k));
  });
  it("translates the API statuses used by badges", () => {
    for (const s of ["delivered", "failed", "queued", "pending", "approved", "opted_out"]) expect(sq[s], s).toBeTruthy();
  });
});
