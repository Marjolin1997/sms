import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { I18nProvider, LangSwitch } from "../i18n.jsx";
import Activity from "../pages/Activity.jsx";
import Reports from "../pages/Reports.jsx";
import { visibleGroups } from "../routes.js";
import { rangeFrom, rangeProblem, shiftDay } from "../reports/range.js";
import { UiProvider } from "../ui.jsx";
import { CLIENT, errorResponse, mockFetch } from "../test/helpers.js";

const wrap = (ui) => render(<I18nProvider><UiProvider>{ui}</UiProvider></I18nProvider>);
const at = (hash, ui, routes) => { location.hash = hash; const f = mockFetch(routes); wrap(ui); return f; };
const calls = (f, path) => f.calls.filter((c) => c.path === path);
const qs = (c) => new URLSearchParams(c.url.split("?")[1] || "");
afterEach(() => { location.hash = ""; vi.restoreAllMocks(); });

const row = (o = {}) => ({ id: 12, name: "Autumn sale", channel: "sms", status: "completed", created_at: "2026-10-01T10:00:00Z", scheduled_at: null, started_at: null, completed_at: null,
  recipients_total: 100, message_counts: { delivered: 90, failed: 8, sending: 2 }, delivery_rate: 91.8, ...o });
const overview = (o = {}) => ({ from: "2026-09-14", to: "2026-10-13", timezone: "UTC", messages: { total: 100, by_status: { delivered: 90, failed: 8, sending: 2 }, delivery_rate: 91.8 },
  emails: { by_status: {} }, campaigns: { by_status: { completed: 3, draft: 1 } }, top_failures: [{ status: "failed", error_code: "no_route", count: 5 }, { status: "failed", error_code: "weird_x9", count: 3 }],
  failures_truncated: false, recent_campaigns: [row()], ...o });
const failures = (o = {}) => ({ from: "2026-09-14", to: "2026-10-13", timezone: "UTC", campaign_id: null, total_messages: 100, by_status: { delivered: 90, failed: 8, sending: 2 },
  rows: [{ status: "failed", error_code: "no_route", count: 5 }, { status: "failed", error_code: "weird_x9", count: 3 }], truncated: false, ...o });
const usage = { from: "2026-09-14", to: "2026-10-13", days: [{ date: "2026-10-01", sms: { count: 10, delivered: 8, failed: 1, segments: 10, cost: "0.4" }, email: { count: 2, delivered: 2, bounced: 0, failed: 0 } }],
  totals: { sms: { count: 10, delivered: 8, failed: 1, segments: 10, cost: "0.4" }, email: { count: 2, delivered: 2, bounced: 0, failed: 0 } } };
const audit = (o = {}) => ({ id: 5, at: "2026-10-02T10:00:00Z", by: "you", action: "campaign.pause", target_type: "campaign", target_id: "12", detail: { at: "x" }, ...o });
const base = (extra = {}) => ({ "GET /v1/reports/overview": overview(), "GET /v1/reports/campaigns": [row(), row({ id: 11, name: "Draft", status: "draft", message_counts: {}, delivery_rate: null, recipients_total: 0 })],
  "GET /v1/reports/failures": failures(), "GET /v1/reports/usage": usage, "GET /v1/reports/audit": [audit(), audit({ id: 4, by: "staff", action: "wallet.adjust", target_type: "wallet", target_id: "1", detail: {} })],
  "GET /v1/reports/audit/facets": { actions: ["campaign.pause", "wallet.adjust"], target_types: ["campaign", "wallet"] }, ...extra });

describe("reports — navigation and range", () => {
  it("1 reports and activity are in the customer menu and need reports:read", () => {
    const ids = (me) => visibleGroups(me).flatMap((g) => g.items.map((i) => i.id));
    expect(ids(CLIENT)).toEqual(expect.arrayContaining(["reports", "activity"]));
    expect(ids({ ...CLIENT, permissions: ["messages:read"] })).not.toContain("activity");
  });
  it("3/4 date range defaults to 30 UTC days and validates order and length before any request", async () => {
    expect(rangeFrom({}, new Date("2026-10-13T12:00:00Z"))).toEqual({ from: "2026-09-14", to: "2026-10-13" });
    expect(shiftDay("2026-03-01", -1)).toBe("2026-02-28");
    expect(rangeProblem("2026-10-02", "2026-10-01")).toBe("order");
    expect(rangeProblem("2024-01-01", "2026-01-01")).toBe("long");
    const f = at("#reports", <Reports />, base());
    await screen.findByText("SMS submitted");
    const n = calls(f, "/v1/reports/overview").length;
    const to = screen.getByLabelText("To");
    await userEvent.clear(to);
    await userEvent.type(to, "2020-01-01");
    expect(await screen.findByText("The end date must not be before the start date.")).toBeInTheDocument();
    expect(calls(f, "/v1/reports/overview").length).toBe(n);
  });
  it("3b a valid range goes to the URL and to the server as from/to", async () => {
    const f = at("#reports?from=2026-10-01&to=2026-10-05", <Reports />, base());
    await screen.findByText("SMS submitted");
    const q = qs(calls(f, "/v1/reports/overview")[0]);
    expect([q.get("from"), q.get("to")]).toEqual(["2026-10-01", "2026-10-05"]);
  });
});

describe("reports — overview", () => {
  it("2 shows server metrics only (nothing computed from rows) and UNKNOWN never appears", async () => {
    at("#reports", <Reports />, base());
    expect((await screen.findAllByText("91.8%")).length).toBeGreaterThan(0);
    expect(screen.queryByText(/unknown/i)).toBeNull();
    expect(screen.getByText("Autumn sale")).toBeInTheDocument();
  });
  it("2b no delivery rate yet shows a dash, not 0%", async () => {
    at("#reports", <Reports />, base({ "GET /v1/reports/overview": overview({ messages: { total: 2, by_status: { sending: 2 }, delivery_rate: null } }) }));
    await screen.findByText("SMS submitted");
    expect(screen.queryByText("0%")).toBeNull();
  });
  it("2c empty period", async () => {
    at("#reports", <Reports />, base({ "GET /v1/reports/overview": overview({ messages: { total: 0, by_status: {}, delivery_rate: null }, campaigns: { by_status: {} }, top_failures: [], recent_campaigns: [] }) }));
    expect(await screen.findByText("No SMS in this period.")).toBeInTheDocument();
    expect(screen.getByText("No failures")).toBeInTheDocument();
  });
});

describe("reports — campaigns", () => {
  it("5/6/7 campaign report is server filtered, searched (debounced) and status-filtered", async () => {
    const f = at("#reports?tab=campaigns", <Reports />, base());
    expect(await screen.findByText("Autumn sale")).toBeInTheDocument();
    expect(screen.getByText("91.8%")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Search"), "aut");
    await waitFor(() => expect(calls(f, "/v1/reports/campaigns").some((c) => qs(c).get("q") === "aut")).toBe(true), { timeout: 3000 });
    await userEvent.selectOptions(screen.getByLabelText("Filter by status"), "completed");
    await waitFor(() => expect(calls(f, "/v1/reports/campaigns").some((c) => qs(c).get("status") === "completed")).toBe(true));
    expect(location.hash).toContain("status=completed");
  });
  it("8 pagination is keyset: Older sends before_id of the last row", async () => {
    const page = Array.from({ length: 26 }, (_, i) => row({ id: 100 - i, name: `C${100 - i}` }));
    const f = at("#reports?tab=campaigns", <Reports />, base({ "GET /v1/reports/campaigns": page }));
    await screen.findByText("C100");
    expect(screen.queryByText("C75")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Older" }));
    await waitFor(() => expect(calls(f, "/v1/reports/campaigns").some((c) => qs(c).get("before_id") === "76")).toBe(true));
  });
  it("9 export sends the same filters to the server CSV endpoint", async () => {
    const saved = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function () { saved.push(this.download); });
    global.URL.createObjectURL = vi.fn(() => "blob:x"); global.URL.revokeObjectURL = vi.fn();
    const f = at("#reports?tab=campaigns&status=draft", <Reports />, { ...base(), "GET /v1/reports/campaigns.csv": new Response("﻿id\r\n", { status: 200 }) });
    await screen.findByText("Autumn sale");
    await userEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    await waitFor(() => expect(calls(f, "/v1/reports/campaigns.csv").length).toBe(1));
    expect(qs(calls(f, "/v1/reports/campaigns.csv")[0]).get("status")).toBe("draft");
    await waitFor(() => expect(saved[0]).toMatch(/^campaigns-\d{4}/));
  });
  it("22 a too-large export shows a clear message and no file", async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    at("#reports?tab=campaigns", <Reports />, { ...base(), "GET /v1/reports/campaigns.csv": errorResponse(422, "export_too_large", "the export has more than 20000 rows; narrow the filters or the dates") });
    await screen.findByText("Autumn sale");
    await userEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    expect(await screen.findByText(/more than 20000 rows\. Narrow the dates/)).toBeInTheDocument();
    expect(click).not.toHaveBeenCalled();
  });
});

describe("reports — failures and usage", () => {
  it("10/11/12 failure rows keep status and code; unknown codes stay visible as Failed", async () => {
    const f = at("#reports?tab=failures", <Reports />, base());
    expect(await screen.findAllByText("no_route")).not.toHaveLength(0);
    expect(screen.getAllByText("weird_x9").length).toBeGreaterThan(0);
    expect(screen.getAllByText("No route or price").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Failed").length).toBeGreaterThan(0);
    expect(screen.queryByText(/unknown/i)).toBeNull();
    expect(calls(f, "/v1/reports/failures")).toHaveLength(1);
  });
  it("10b campaign filter is numeric and goes to the server; foreign campaign → safe 404", async () => {
    const f = at("#reports?tab=failures&campaign=12", <Reports />, base());
    await screen.findAllByText("no_route");
    expect(qs(calls(f, "/v1/reports/failures")[0]).get("campaign_id")).toBe("12");
  });
  it("10c a 404 for a campaign that is not yours is a plain not-found message", async () => {
    at("#reports?tab=failures&campaign=99", <Reports />, base({ "GET /v1/reports/failures": errorResponse(404, "not_found", "campaign not found") }));
    expect((await screen.findAllByText(/couldn't find that/i)).length).toBeGreaterThan(0);
  });
  it("10d data of the previous filter is never shown under a new filter that failed", async () => {
    at("#reports?tab=failures", <Reports />, base({ "GET /v1/reports/failures": (c) => (qs(c).get("campaign_id") === "99" ? errorResponse(404, "not_found", "campaign not found") : failures()) }));
    await screen.findAllByText("no_route");
    await userEvent.type(screen.getByLabelText("Campaign number"), "99{Enter}");
    expect((await screen.findAllByText(/couldn't find that/i)).length).toBeGreaterThan(0);
    expect(screen.queryByText("no_route")).toBeNull();
    expect(screen.queryByText("By error code")).toBeNull();
  });
  it("13 usage report shows server totals and a text table with the chart", async () => {
    at("#reports?tab=usage", <Reports />, base());
    expect(await screen.findByText("SMS sent")).toBeInTheDocument();
    const img = screen.getByRole("img", { name: /8 delivered, 1 failed, 1 other/ });
    expect(img).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
  });
  it("18 usage export goes through the server", async () => {
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    global.URL.createObjectURL = vi.fn(() => "blob:x"); global.URL.revokeObjectURL = vi.fn();
    const f = at("#reports?tab=usage", <Reports />, { ...base(), "GET /v1/reports/usage.csv": new Response("x", { status: 200 }) });
    await screen.findByText("SMS sent");
    await userEvent.click(screen.getByRole("button", { name: "Export usage (CSV)" }));
    await waitFor(() => expect(calls(f, "/v1/reports/usage.csv")).toHaveLength(1));
  });
});

describe("activity log", () => {
  it("14/16 lists the tenant audit with safe actor labels", async () => {
    at("#activity", <Activity />, base());
    expect(await screen.findByText("Campaign paused")).toBeInTheDocument();
    expect(screen.getByText(/^you$/i)).toBeInTheDocument();
    expect(screen.getByText(/^our staff$/i)).toBeInTheDocument();
    expect(screen.getAllByText("wallet.adjust").length).toBeGreaterThan(0); // veprim i panjohur: kodi mbetet
  });
  it("15 action and item filters go to the server and the URL; clear works", async () => {
    const f = at("#activity", <Activity />, base());
    await screen.findByText("Campaign paused");
    await userEvent.selectOptions(screen.getByLabelText("Filter by action"), "campaign.pause");
    await waitFor(() => expect(calls(f, "/v1/reports/audit").some((c) => qs(c).get("action") === "campaign.pause")).toBe(true));
    expect(location.hash).toContain("action=campaign.pause");
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => expect(location.hash).not.toContain("action=campaign.pause"));
  });
  it("15b keyset pagination", async () => {
    const page = Array.from({ length: 31 }, (_, i) => audit({ id: 200 - i, target_id: String(200 - i) }));
    const f = at("#activity", <Activity />, base({ "GET /v1/reports/audit": page }));
    await screen.findAllByText("Campaign paused");
    await userEvent.click(screen.getByRole("button", { name: "Older" }));
    await waitFor(() => expect(calls(f, "/v1/reports/audit").some((c) => qs(c).get("before_id") === "171")).toBe(true));
  });
  it("19 audit export carries the filters", async () => {
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    global.URL.createObjectURL = vi.fn(() => "blob:x"); global.URL.revokeObjectURL = vi.fn();
    const f = at("#activity?action=campaign.pause", <Activity />, { ...base(), "GET /v1/reports/audit.csv": new Response("x", { status: 200 }) });
    await screen.findByText("Campaign paused");
    await userEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    await waitFor(() => expect(qs(calls(f, "/v1/reports/audit.csv")[0]).get("action")).toBe("campaign.pause"));
  });
  it("empty and no write controls", async () => {
    at("#activity", <Activity />, base({ "GET /v1/reports/audit": [] }));
    expect(await screen.findByText("Nothing recorded in this period")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /delete|edit|remove|retry|resend/i })).toBeNull();
  });
});

describe("errors, i18n and read-only", () => {
  it.each([[401, "unauthenticated"], [403, "forbidden"], [429, "rate_limited"], [500, "server"]])("25-29 %s on the overview is a state, not a blank page", async (status, code) => {
    at("#reports", <Reports />, base({ "GET /v1/reports/overview": errorResponse(status, code, "x") }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("SMS submitted")).toBeNull();
  });
  it("30 network failure offers a retry", async () => {
    location.hash = "#reports";
    const f = mockFetch({});
    f.mockRejectedValue(new TypeError("fail"));
    wrap(<Reports />);
    expect(await screen.findByRole("button", { name: /try again|retry/i })).toBeInTheDocument();
  });
  it("34 Albanian", async () => {
    location.hash = "#reports";
    mockFetch(base());
    wrap(<><LangSwitch /><Reports /></>);
    await screen.findByText("SMS submitted");
    await userEvent.click(screen.getByRole("button", { name: "Shqip" }));
    expect(await screen.findByText("SMS të pranuara")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "English" }));
  });
  it("35 reports and activity never send anything but GET", async () => {
    const f = at("#reports?tab=failures", <Reports />, base());
    await screen.findAllByText("no_route");
    for (const tab of ["Overview", "Campaigns", "Usage"]) await userEvent.click(within(screen.getByRole("tablist")).getByRole("tab", { name: tab }));
    expect(f.calls.every((c) => c.method === "GET")).toBe(true);
  });
  it("32 keyboard: tabs and filters are reachable and labelled", async () => {
    at("#reports?tab=campaigns", <Reports />, base());
    await screen.findByText("Autumn sale");
    await userEvent.tab();
    expect(document.activeElement).toBeTruthy();
    for (const l of ["Search", "Filter by status", "From", "To"]) expect(screen.getByLabelText(l)).toBeInTheDocument();
  });
});
