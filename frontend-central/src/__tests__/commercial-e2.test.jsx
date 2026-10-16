import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App.jsx";
import { setToken } from "../api.js";
import { I18nProvider } from "../i18n.jsx";
import { ADMIN, OPERATOR, errorResponse, mockFetch, pageOf } from "../test/helpers.js";

const E1 = "11111111-1111-4111-8111-111111111111";
const INV = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const TS = "2026-03-01T10:00:00+00:00";
const inv = (o = {}) => ({ id: INV, number: "INV-2026-000007", enterprise_id: E1, subscription_id: "14141414-1414-4141-8141-141414141414", period_index: 3, period_start: "2026-02-01T00:00:00+00:00", period_end: "2026-03-01T00:00:00+00:00",
  plan_version_id: "15151515-1515-4151-8151-151515151515", currency: "EUR", subtotal: "1000.000000", vat_rate: "0.2000", tax: "200.000000", total: "1200.000000", status: "paid", bill_to: { legal_name: "Acme" }, issuer: { name: "Platform" },
  issued_at: TS, due_at: "2026-03-15T00:00:00+00:00", paid_at: TS, voided_at: null, voided_reason: null, credit_note_count: 0, credited_total: "0.000000", ...o });
const detail = (o = {}) => ({ ...inv(o), lines: [{ line_no: 1, line_type: "plan_fee", description: "Monthly plan", quantity: "1", unit_price: "1000.000000", amount: "1000.000000", currency: "EUR", period_start: TS, period_end: TS, pricing_source: "plan" }],
  settlement: { allocation: null, payments: [], credit_notes: [], credited_total: "0.000000", net_amount: "1200.000000" } });
const pdf = (name = "invoice-INV-2026-000007.pdf") => new Response("%PDF-1.4 x", { status: 200, headers: { "Content-Type": "application/pdf", "Content-Disposition": `attachment; filename="${name}"` } });
const routes = (extra = {}) => ({ "GET /auth/me": ADMIN, "GET /admin/billing/invoices": pageOf([inv(), inv({ id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", number: "INV-2026-000008", credit_note_count: 2, credited_total: "50.250000" })]),
  [`GET /admin/billing/invoices/${INV}`]: detail(), [`GET /admin/billing/invoices/${INV}/pdf`]: () => pdf(), "GET /admin/money/payments": pageOf([]), ...extra });
const boot = async (me, hash, extra = {}) => {
  setToken("t"); location.hash = hash;
  const f = mockFetch({ ...routes(extra), "GET /auth/me": me });
  render(<I18nProvider><App /></I18nProvider>);
  await screen.findByRole("navigation", { name: "Main navigation" });
  return f;
};
const calls = (f, path) => f.calls.filter((c) => c.path === path);
function spyDownload() {
  const saved = [];
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => "blob:x"), revokeObjectURL: vi.fn() }));
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function () { saved.push(this.download); });
  return saved;
}
afterEach(() => { location.hash = ""; });

describe("invoice filters (server-side) — Central", () => {
  it("1 invoice number filter is sent to the server, upper-cased, and kept in the URL", async () => {
    const f = await boot(ADMIN, "#com-invoices");
    await screen.findByText("INV-2026-000007");
    await userEvent.type(screen.getByLabelText("Invoice number"), "inv-2026");
    await userEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => expect(location.hash).toContain("number=INV-2026"));
    await waitFor(() => expect(calls(f, "/admin/billing/invoices").some((c) => c.url.includes("number=INV-2026"))).toBe(true));
  });
  it("2/3/4 date from, date to and the combined range are sent as ISO dates", async () => {
    const f = await boot(ADMIN, "#com-invoices");
    await screen.findByText("INV-2026-000007");
    await userEvent.type(screen.getByLabelText("Issued from"), "2026-02-01");
    await userEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => expect(calls(f, "/admin/billing/invoices").some((c) => c.url.includes("issued_from=2026-02-01") && !c.url.includes("issued_to"))).toBe(true));
    await userEvent.type(screen.getByLabelText("Issued to"), "2026-02-28");
    await userEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => expect(calls(f, "/admin/billing/invoices").some((c) => c.url.includes("issued_from=2026-02-01") && c.url.includes("issued_to=2026-02-28"))).toBe(true));
    expect(location.hash).toContain("issued_from=2026-02-01");
    expect(location.hash).toContain("issued_to=2026-02-28");
  });
  it("5 an inverted date range shows a field error and is never sent or silently swapped", async () => {
    const f = await boot(ADMIN, "#com-invoices");
    await screen.findByText("INV-2026-000007");
    const before = calls(f, "/admin/billing/invoices").length;
    await userEvent.type(screen.getByLabelText("Issued from"), "2026-03-10");
    await userEvent.type(screen.getByLabelText("Issued to"), "2026-03-01");
    await userEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    expect(await screen.findByText(/The end date must not be before the start date/)).toBeInTheDocument();
    expect(screen.getByLabelText("Issued to")).toHaveAttribute("aria-invalid", "true");
    expect(calls(f, "/admin/billing/invoices")).toHaveLength(before);
    expect(location.hash).not.toContain("issued_from");
    expect(screen.getByLabelText("Issued from")).toHaveValue("2026-03-10"); // sync-swap: s'ndryshohet
  });
  it("5b a server 422 for an inverted range (hand-edited URL) is shown against the field", async () => {
    await boot(ADMIN, "#com-invoices?issued_from=2026-03-10&issued_to=2026-03-01", { "GET /admin/billing/invoices": errorResponse(422, "invalid", "issued_from must be on or before issued_to") });
    expect((await screen.findAllByText(/must be on or before issued_to/)).length).toBeGreaterThan(0);
    expect(await screen.findByText(/Issued from: .*must be on or before/)).toBeInTheDocument();
  });
  it("6 filters survive a reload of the page from the URL", async () => {
    const f = await boot(ADMIN, "#com-invoices?number=INV-2026&issued_from=2026-02-01&status=paid");
    await waitFor(() => expect(calls(f, "/admin/billing/invoices").some((c) => c.url.includes("number=INV-2026") && c.url.includes("issued_from=2026-02-01") && c.url.includes("status=paid"))).toBe(true));
    expect(screen.getByLabelText("Invoice number")).toHaveValue("INV-2026");
    expect(screen.getByLabelText("Issued from")).toHaveValue("2026-02-01");
  });
  it("7 pagination keeps every filter", async () => {
    const f = await boot(ADMIN, "#com-invoices?number=INV&issued_to=2026-03-31", { "GET /admin/billing/invoices": () => pageOf([inv()], { has_more: true }) });
    await userEvent.click(await screen.findByRole("button", { name: "Next" }));
    await waitFor(() => expect(location.hash).toMatch(/offset=50/));
    expect(location.hash).toContain("number=INV");
    expect(location.hash).toContain("issued_to=2026-03-31");
    await waitFor(() => expect(calls(f, "/admin/billing/invoices").some((c) => c.url.includes("offset=50") && c.url.includes("number=INV") && c.url.includes("issued_to=2026-03-31"))).toBe(true));
  });
  it("8 an empty filtered result offers to clear the filters", async () => {
    await boot(ADMIN, "#com-invoices?number=ZZZ", { "GET /admin/billing/invoices": pageOf([]) });
    expect(await screen.findByText("Nothing matches your filters")).toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: "Clear filters" })[0]);
    await waitFor(() => expect(location.hash).toBe("#com-invoices"));
  });
  it("9 the credit-note indicator comes from the list payload (count and exact total)", async () => {
    await boot(ADMIN, "#com-invoices");
    const row = (await screen.findByText("INV-2026-000008")).closest("tr");
    expect(row.textContent).toMatch(/credited/);
    expect(row.textContent).toMatch(/2 ·/);
    expect(row.textContent).toMatch(/50[.,]25 EUR/);
    const plain = screen.getByText("INV-2026-000007").closest("tr");
    expect(within(plain).getByLabelText("No credit notes")).toBeInTheDocument();
  });
  it("9b credit payments can be filtered by enterprise (sent to the server)", async () => {
    const f = await boot(ADMIN, "#com-payments");
    await screen.findByText("No credit payments yet");
    await userEvent.type(screen.getByLabelText("Enterprise ID"), E1);
    await userEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => expect(calls(f, "/admin/money/payments").some((c) => c.url.includes(`enterprise_id=${E1}`))).toBe(true));
    expect(location.hash).toContain(`enterprise_id=${E1}`);
  });
});

describe("official invoice PDF — Central", () => {
  const open = async (me = ADMIN, extra = {}) => { const f = await boot(me, `#com-invoices/${INV}`, extra); return [f, await screen.findByRole("button", { name: "Download official PDF of invoice INV-2026-000007" })]; };
  it("10 downloads the server-generated PDF using the server's filename", async () => {
    const saved = spyDownload();
    const [f, btn] = await open();
    await userEvent.click(btn);
    await waitFor(() => expect(saved).toEqual(["invoice-INV-2026-000007.pdf"]));
    expect(calls(f, `/admin/billing/invoices/${INV}/pdf`)).toHaveLength(1);
    expect(f.calls.every((c) => c.method === "GET")).toBe(true);
  });
  it("11 shows a loading state and ignores a second click while fetching", async () => {
    spyDownload();
    let release;
    const gate = new Promise((r) => { release = r; });
    const [f, btn] = await open(ADMIN, { [`GET /admin/billing/invoices/${INV}/pdf`]: async () => { await gate; return pdf(); } });
    await userEvent.click(btn);
    await waitFor(() => expect(btn).toBeDisabled());
    expect(btn).toHaveTextContent("Preparing…");
    await userEvent.click(btn);
    release();
    await waitFor(() => expect(btn).not.toBeDisabled());
    expect(calls(f, `/admin/billing/invoices/${INV}/pdf`)).toHaveLength(1);
  });
  it("12 a 404 is a clean message and saves nothing", async () => {
    const saved = spyDownload();
    const [, btn] = await open(ADMIN, { [`GET /admin/billing/invoices/${INV}/pdf`]: errorResponse(404, "not_found", "invoice not found") });
    await userEvent.click(btn);
    expect((await screen.findAllByText(/We couldn't find that/)).length).toBeGreaterThan(0);
    expect(saved).toHaveLength(0);
  });
  it("13 a 5xx is sanitized (no stack trace, no internal text)", async () => {
    const saved = spyDownload();
    const [, btn] = await open(ADMIN, { [`GET /admin/billing/invoices/${INV}/pdf`]: errorResponse(500, "pdf_render_failed", "Traceback boom /srv/x") });
    await userEvent.click(btn);
    await waitFor(() => expect(document.body.textContent).toMatch(/Something went wrong on our side/));
    expect(document.body.textContent).not.toMatch(/Traceback|\/srv/);
    expect(saved).toHaveLength(0);
  });
  it("13b a non-PDF 200 is rejected", async () => {
    const saved = spyDownload();
    const [, btn] = await open(ADMIN, { [`GET /admin/billing/invoices/${INV}/pdf`]: () => new Response("<html>", { status: 200, headers: { "Content-Type": "text/html" } }) });
    await userEvent.click(btn);
    await waitFor(() => expect(document.body.textContent).toMatch(/Something went wrong on our side/));
    expect(saved).toHaveLength(0);
  });
  it("14/15 admin and operator both read; the operator gets no write control", async () => {
    const saved = spyDownload();
    const [, btn] = await open(OPERATOR);
    expect(screen.getByText("Read-only access")).toBeInTheDocument();
    await userEvent.click(btn);
    await waitFor(() => expect(saved).toHaveLength(1));
  });
  it("16 the invoice pages have no financial write control (only GETs)", async () => {
    const f = await boot(ADMIN, "#com-invoices");
    await screen.findByText("INV-2026-000007");
    const names = screen.getAllByRole("button").map((b) => b.textContent);
    expect(names.filter((n) => /approve|reject|void|issue|create|publish|assign|resume|save|allocate|reverse|adjust/i.test(n))).toEqual([]);
    expect(f.calls.every((c) => c.method === "GET")).toBe(true);
  });
  it("17 responsive: filter bar and table keep their small-screen hooks", async () => {
    await boot(ADMIN, "#com-invoices");
    const t = (await screen.findByText("INV-2026-000007")).closest("table");
    expect(t.querySelectorAll("td[data-label]").length).toBeGreaterThan(0);
    expect(t.querySelectorAll(".hide-sm").length).toBeGreaterThan(0);
    expect(screen.getByRole("search")).toBeInTheDocument();
  });
  it("18 EN/SQ: the new texts are translated", async () => {
    const sq = (await import("../locales/sq/index.js")).default;
    for (const k of ["Invoice number", "Issued from", "Issued to", "Download PDF", "Preparing…", "The end date must not be before the start date.", "{n} credit note(s)", "No credit notes"]) expect(sq[k], k).toBeTruthy();
  });
  it("19 keyboard: the PDF button is reachable and Enter downloads", async () => {
    const saved = spyDownload();
    const [, btn] = await open();
    btn.focus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(saved).toHaveLength(1));
  });
});
