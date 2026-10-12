import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { I18nProvider } from "../i18n.jsx";
import Billing from "../pages/Billing.jsx";
import Wallet from "../pages/Wallet.jsx";
import { amt, accountingDate, qty } from "../money.js";
import { UiProvider } from "../ui.jsx";
import { CLIENT, errorResponse, mockFetch } from "../test/helpers.js";

const wrap = (ui) => render(<I18nProvider><UiProvider>{ui}</UiProvider></I18nProvider>);
const INV = { id: 7, number: "INV-000007", status: "open", currency: "EUR", subtotal: "1000.000000", vat_rate: "0.2000", tax: "200.000000", total: "1200.000000", issued_at: "2026-03-01T10:00:00Z", due_at: "2026-03-15T00:00:00Z", paid_at: null, paid_via: null, period_start: "2026-02-01T00:00:00Z", period_end: "2026-03-01T00:00:00Z" };
const DETAIL = { ...INV, lines: [{ description: "Growth - monthly fee", quantity: "1", unit_price: "1000.000000", amount: "1000.000000" }, { description: "Extra emails", quantity: "1500", unit_price: "0.100000", amount: "150.000000" }] };
const SUB = { status: "active", auto_pay: false, cancel_at_period_end: false, pending_plan: null, period_start: "2026-03-01T00:00:00Z", period_end: "2026-04-01T00:00:00Z",
  plan: { id: 1, code: "growth", name: "Growth", currency: "EUR", monthly_fee: "1000.000000", included_emails: 1000, email_overage_price: "0.100000", status: "active" }, usage: { emails: 1500, included: 1000, overage: 500, overage_amount: "50.000000" } };
const PAY = { id: 3, purpose: "invoice", invoice_id: 7, wallet_id: 1, amount: "1200.000000", currency: "EUR", status: "pending", checkout_url: null, failure_reason: null, created_at: "2026-03-02T10:00:00Z" };
const routes = (extra = {}) => ({ "GET /v1/billing/subscription": SUB, "GET /v1/billing/profile": null, "GET /v1/billing/payments": [PAY], "GET /v1/billing/invoices": [INV], "GET /v1/billing/invoices/7": DETAIL, ...extra });
const me = { ...CLIENT };

describe("exact money formatting (Enterprise)", () => {
  const digits = (s) => s.replace(/\D/g, "");
  it("keeps every digit; never goes through Number", () => {
    expect(digits(amt("9007199254740993.010000"))).toBe("900719925474099301");
    expect(digits(amt("5"))).toBe("500");
    expect(amt(12.5)).toBe("-");
    expect(amt("abc")).toBe("-");
    expect(digits(qty("1500"))).toBe("1500");
    expect(accountingDate("2026-03-01T23:59:59Z")).toBe(accountingDate("2026-03-01T00:00:00Z"));
  });
});

describe("customer billing (read side)", () => {
  it("shows the plan, the usage summary and the payment state exactly as the server sent them", async () => {
    mockFetch(routes());
    wrap(<Billing me={me} />);
    expect(await screen.findByText("Growth")).toBeInTheDocument();
    expect(screen.getByText(/50[.,]00 EUR/)).toBeInTheDocument();
    expect(await screen.findByText("pending")).toBeInTheDocument();
  });
  it("lists only what the tenant-scoped endpoint returns and opens the invoice detail with its lines", async () => {
    const f = mockFetch(routes());
    wrap(<Billing me={me} />);
    await userEvent.click(await screen.findByRole("button", { name: "Details of invoice INV-000007" }));
    const card = (await screen.findByText("Invoice details")).closest("section");
    expect(await within(card).findByText("Extra emails")).toBeInTheDocument();
    expect(card.textContent).toMatch(/1[.,\s]?500/);
    expect(card.textContent).toMatch(/1[.,\s]?200[.,]00 EUR/);
    expect(f.calls.filter((c) => c.path === "/v1/billing/invoices/7")).toHaveLength(1);
    expect(f.calls.every((c) => c.method === "GET")).toBe(true);
    await userEvent.click(within(card).getByRole("button", { name: "Close" }));
    expect(screen.queryByText("Invoice details")).toBeNull();
  });
  it("another tenant's invoice (404 from the server) is an error, never data", async () => {
    mockFetch(routes({ "GET /v1/billing/invoices/7": errorResponse(404, "not_found", "invoice not found") }));
    wrap(<Billing me={me} />);
    await userEvent.click(await screen.findByRole("button", { name: "Details of invoice INV-000007" }));
    expect(await screen.findByText("We couldn't find that")).toBeInTheDocument();
    expect(screen.queryByText("Extra emails")).toBeNull();
  });
  it("renders no commercial internals even if the payload carried extra fields", async () => {
    mockFetch(routes({ "GET /v1/billing/invoices/7": { ...DETAIL, central_authority: "central", ledger_entry_id: "L-9", service_client: "svc-ent", reconciliation_required: true } }));
    wrap(<Billing me={me} />);
    await userEvent.click(await screen.findByRole("button", { name: "Details of invoice INV-000007" }));
    await screen.findByText("Extra emails");
    expect(document.body.textContent).not.toMatch(/L-9|svc-ent|central_authority|reconciliation/i);
  });
  it("a failing invoice list shows a sanitized error with retry", async () => {
    mockFetch(routes({ "GET /v1/billing/invoices": errorResponse(500, "internal", "Traceback: boom") }));
    wrap(<Billing me={me} />);
    expect(await screen.findByText(/Something went wrong on our side/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/Traceback/);
  });
  it("tables carry data-label cells for small screens", async () => {
    mockFetch(routes());
    wrap(<Billing me={me} />);
    const t = (await screen.findByText("INV-000007")).closest("table");
    expect(t.querySelectorAll("td[data-label]").length).toBeGreaterThan(0);
  });
});

describe("wallet balance (Enterprise)", () => {
  const W = { id: 1, currency: "EUR", available: "1234567.890000", held: "0.500000", low_balance_threshold: null, low_balance: false };
  const wr = (extra = {}) => ({ "GET /v1/wallets": [W], "GET /v1/wallets/1/topups": [], "GET /v1/wallets/1/ledger": [], "GET /v1/billing/payments": [], ...extra });
  it("shows the backend balance exactly, with all digits", async () => {
    mockFetch(wr());
    wrap(<Wallet />);
    const stat = (await screen.findByText("Available to spend")).closest(".stat");
    expect(stat.textContent.replace(/\D/g, "")).toContain("123456789");
    expect(stat.textContent).toContain("EUR");
  });
});

// ---------------- M11-E2 ----------------
import { vi } from "vitest";

const pdfResponse = (name = "invoice-INV-000007.pdf") =>
  new Response("%PDF-1.4 test", { status: 200, headers: { "Content-Type": "application/pdf", "Content-Disposition": `attachment; filename="${name}"` } });

function spyDownload() {
  const saved = [];
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => "blob:x"), revokeObjectURL: vi.fn() }));
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function () { saved.push({ href: this.href, name: this.download }); });
  return { saved, click };
}

describe("Enterprise official PDF (M11-E2)", () => {
  it("downloads the server-generated PDF with the server's safe filename", async () => {
    const { saved } = spyDownload();
    const f = mockFetch(routes({ "GET /v1/billing/invoices/7/pdf": () => pdfResponse() }));
    wrap(<Billing me={me} />);
    await userEvent.click(await screen.findByRole("button", { name: "Download official PDF of invoice INV-000007" }));
    await waitFor(() => expect(saved).toHaveLength(1));
    expect(saved[0].name).toBe("invoice-INV-000007.pdf");
    expect(f.calls.find((c) => c.path === "/v1/billing/invoices/7/pdf").method).toBe("GET");
    expect(f.calls.every((c) => c.method === "GET")).toBe(true);
  });
  it("shows a loading state and ignores a second click while the PDF is being fetched", async () => {
    spyDownload();
    let release;
    const gate = new Promise((r) => { release = r; });
    const f = mockFetch(routes({ "GET /v1/billing/invoices/7/pdf": async () => { await gate; return pdfResponse(); } }));
    wrap(<Billing me={me} />);
    const btn = await screen.findByRole("button", { name: "Download official PDF of invoice INV-000007" });
    await userEvent.click(btn);
    await waitFor(() => expect(btn).toBeDisabled());
    expect(btn).toHaveTextContent("Preparing…");
    await userEvent.click(btn);
    release();
    await waitFor(() => expect(btn).not.toBeDisabled());
    expect(f.calls.filter((c) => c.path === "/v1/billing/invoices/7/pdf")).toHaveLength(1);
  });
  it("another tenant's invoice (404) shows a clean error and saves nothing", async () => {
    const { saved } = spyDownload();
    mockFetch(routes({ "GET /v1/billing/invoices/7/pdf": errorResponse(404, "not_found", "invoice not found") }));
    wrap(<Billing me={me} />);
    await userEvent.click(await screen.findByRole("button", { name: "Download official PDF of invoice INV-000007" }));
    expect(await screen.findByText(/Couldn't download the document/)).toBeInTheDocument();
    expect(saved).toHaveLength(0);
  });
  it("a 500 is sanitized; a non-PDF 200 is rejected", async () => {
    const { saved } = spyDownload();
    mockFetch(routes({ "GET /v1/billing/invoices/7/pdf": errorResponse(500, "pdf_render_failed", "Traceback boom /srv") }));
    wrap(<Billing me={me} />);
    await userEvent.click(await screen.findByRole("button", { name: "Download official PDF of invoice INV-000007" }));
    await waitFor(() => expect(document.body.textContent).toMatch(/Something went wrong on our side/));
    expect(document.body.textContent).not.toMatch(/Traceback|\/srv/);
    expect(saved).toHaveLength(0);
  });
  it("rejects an unexpected content type", async () => {
    const { saved } = spyDownload();
    mockFetch(routes({ "GET /v1/billing/invoices/7/pdf": () => new Response("<html>", { status: 200, headers: { "Content-Type": "text/html" } }) }));
    wrap(<Billing me={me} />);
    await userEvent.click(await screen.findByRole("button", { name: "Download official PDF of invoice INV-000007" }));
    await waitFor(() => expect(document.body.textContent).toMatch(/Something went wrong on our side/));
    expect(saved).toHaveLength(0);
  });
  it("the detail panel offers the same PDF and 'View / print' is labelled as a printable page", async () => {
    mockFetch(routes());
    wrap(<Billing me={me} />);
    expect(await screen.findByRole("button", { name: "View / print" })).toHaveAttribute("title", expect.stringMatching(/not|official/i));
    await userEvent.click(screen.getByRole("button", { name: "Details of invoice INV-000007" }));
    const card = (await screen.findByText("Invoice details")).closest("section");
    expect(await within(card).findByRole("button", { name: /Download official PDF/ })).toBeInTheDocument();
  });
  it("EN/SQ: the PDF controls and the pending banner are translated", async () => {
    const { t } = await import("../i18n.jsx");
    const sq = (await import("../locales/sq/index.js")).default;
    for (const k of ["Download PDF", "Preparing…", "Pending recalculation", "Download official PDF of invoice {n}", "The server returned an unexpected document."]) expect(sq[k], k).toBeTruthy();
    expect(t("Download PDF")).toBeTruthy();
  });
});

describe("pending recalculation on the wallet (M11-E2)", () => {
  const W = (o = {}) => ({ id: 1, currency: "EUR", available: "12.340000", held: "0.000000", low_balance_threshold: null, low_balance: false, has_pending_recalculation: false, ...o });
  const wr = (w) => ({ "GET /v1/wallets": [w], "GET /v1/wallets/1/topups": [], "GET /v1/wallets/1/ledger": [], "GET /v1/billing/payments": [] });
  it("normal balance: no pending banner and the amount is exactly the server's", async () => {
    mockFetch(wr(W()));
    wrap(<Wallet />);
    const stat = (await screen.findByText("Available to spend")).closest(".stat");
    expect(stat.textContent).toMatch(/12[.,]34 EUR/);
    expect(screen.queryByText("Pending recalculation")).toBeNull();
    expect(screen.queryByTestId("pending-recalculation")).toBeNull();
  });
  it("pending: labelled 'Pending recalculation', amount still shown but not presented as final", async () => {
    mockFetch(wr(W({ has_pending_recalculation: true })));
    wrap(<Wallet />);
    const stat = (await screen.findByText("Available to spend")).closest(".stat");
    expect(stat).toHaveTextContent("Pending recalculation");
    expect(stat.textContent).toMatch(/12[.,]34 EUR/);
    expect(stat.className).toMatch(/warn/);
    expect(stat.className).not.toMatch(/good/);
    const banner = await screen.findByTestId("pending-recalculation");
    expect(banner).toHaveTextContent(/isn't final yet/);
  });
  it("the client never infers pending from anything but the server flag", async () => {
    mockFetch(wr(W({ available: "0.000000", held: "99.000000", low_balance: true })));
    wrap(<Wallet />);
    await screen.findByText("Available to spend");
    expect(screen.queryByTestId("pending-recalculation")).toBeNull();
  });
});
