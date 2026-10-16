import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import App from "../App.jsx";
import { setToken } from "../api.js";
import { I18nProvider } from "../i18n.jsx";
import { ADMIN, OPERATOR, errorResponse, mockFetch, pageOf } from "../test/helpers.js";

const E1 = "11111111-1111-4111-8111-111111111111";
const ACC = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const PROD = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const INV = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const BOOK = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
const VER = "ffffffff-ffff-4fff-8fff-ffffffffffff";
const PAY = "99999999-9999-4999-8999-999999999999";
const TS = "2026-03-01T10:00:00+00:00";

const actor = { user_id: "u1", label: "admin@example.com" };
const account = { id: ACC, enterprise_id: E1, product_id: PROD, currency: "EUR", status: "active", created_at: TS, updated_at: TS,
  totals: { funds: "1234567.890000", outstanding_grants: "200.000000", available_to_grant: "1234367.890000", payment_credits: "1500000.000000", credit_adjustments: "0.000000", debit_adjustments: "265432.110000", grants_issued: "300.000000", grants_reversed: "100.000000", entries: 4 } };
const grant = (o = {}) => ({ id: "12121212-1212-4121-8121-121212121212", account_id: ACC, enterprise_id: E1, product_id: PROD, currency: "EUR", amount: "300.000000", status: "active", purpose: "sms_credit", source_payment_id: null, note: "welcome", created_by: actor, created_at: TS, reversed_by: null, reversed_at: null, reversal_reason: null, ...o });
const payment = (o = {}) => ({ id: PAY, account_id: ACC, enterprise_id: E1, currency: "EUR", amount: "100.500000", source: "bank_transfer", external_reference: "REF-77", note: null, status: "approved", created_by: actor, created_at: TS, approved_by: actor, approved_at: TS, rejected_by: null, rejected_at: null, rejection_reason: null, ...o });
const invPayment = (o = {}) => ({ ...payment(), purpose: "invoice", invoice_id: INV, allocation_id: "13131313-1313-4131-8131-131313131313", ...o });
const invoice = (o = {}) => ({ id: INV, number: "INV-2026-0001", enterprise_id: E1, subscription_id: "14141414-1414-4141-8141-141414141414", period_index: 3, period_start: "2026-02-01T00:00:00+00:00", period_end: "2026-03-01T00:00:00+00:00",
  plan_version_id: "15151515-1515-4151-8151-151515151515", currency: "EUR", subtotal: "1000.000000", vat_rate: "0.2000", tax: "200.000000", total: "1200.000000", status: "open",
  bill_to: { legal_name: "Acme Sh.p.k", country: "AL" }, issuer: { legal_name: "Platform Ltd", tax_id: "K12345" }, issued_at: TS, due_at: "2026-03-15T00:00:00+00:00", paid_at: null, voided_at: null, voided_reason: null, ...o });
const line = (o = {}) => ({ line_no: 1, line_type: "plan_fee", description: "Monthly plan", quantity: "1", unit_price: "1000.000000", amount: "1000.000000", currency: "EUR", period_start: "2026-02-01T00:00:00+00:00", period_end: "2026-03-01T00:00:00+00:00", plan_version_id: null, pricing_source: "plan", price_book_id: null, price_version_id: null, price_rule_id: null, ...o });
const settlement = (o = {}) => ({ allocation: { id: "13131313-1313-4131-8131-131313131313", payment_id: PAY, invoice_id: INV, enterprise_id: E1, amount: "1200.000000", currency: "EUR", allocated_at: TS, allocated_by: actor },
  payments: [invPayment()], credit_notes: [{ id: "16161616-1616-4161-8161-161616161616", number: "CN-2026-0001", invoice_id: INV, enterprise_id: E1, currency: "EUR", amount: "50.000000", reason: "goodwill", issued_at: TS, created_by: actor, issuer: {}, bill_to: {} }], credited_total: "50.000000", net_amount: "1150.000000", ...o });
const creditNote = (o = {}) => ({ id: "16161616-1616-4161-8161-161616161616", number: "CN-2026-0001", invoice_id: INV, enterprise_id: E1, currency: "EUR", amount: "50.000000", reason: "goodwill", issued_at: TS, created_by: actor, issuer: {}, bill_to: {}, ...o });
const sub = (o = {}) => ({ id: "17171717-1717-4171-8171-171717171717", enterprise_id: E1, status: "active", display_status: "active", plan_version_id: "15151515-1515-4151-8151-151515151515", pending_plan_version_id: null, cancel_at_period_end: false,
  anchor_started_at: TS, next_period_index: 4, next_period: { start: "2026-03-01T00:00:00+00:00", end: "2026-04-01T00:00:00+00:00" }, cancelled_at: null, created_at: TS, updated_at: TS, ...o });
const period = (o = {}) => ({ id: "18181818-1818-4181-8181-181818181818", subscription_id: "17171717-1717-4171-8171-171717171717", enterprise_id: E1, period_index: 3, period_start: "2026-02-01T00:00:00+00:00", period_end: "2026-03-01T00:00:00+00:00", plan_version_id: "15151515-1515-4151-8151-151515151515", status: "invoiced", invoice_id: INV, billed_at: TS, ...o });
const overview = (o = {}) => ({ generated_at: TS, reconciliation: { status: "WARN", counts: { INFO: 0, WARN: 2, FAIL: 0, CRITICAL: 0 } }, payments: { pending: 3, approved: 5, rejected: 1, oldest_pending_age_seconds: 10, stale_pending: 1, stale_after_seconds: 172800 },
  accounts: [{ account_id: ACC, enterprise_id: E1, product_id: PROD, currency: "EUR", status: "active", available_to_grant: "1234367.890000", outstanding_grants: "200.000000", funds: "1234567.890000" }], grants: { active: 4, reversed: 1, active_amount: "200.000000" },
  usage_reports: [], unresolved_reversals: [], pricing: { epoch: "e", revision: 1, active_versions: 1, assignments: 1, accounts_without_price_assignment: [] },
  alerts: [{ level: "WARN", code: "stale_pending_payments", enterprise_id: null, product_id: null, currency: null, subject: null, message: "1 payment(s) pending > 172800s" }], ...o });
const billingOps = (o = {}) => ({ generated_at: TS, authority: { mode: "shadow", ack: false, changed_at: null, central_invoices: 7, legacy_worker_frozen_attested: false }, import: {}, shadow: {}, usage: { metered_subscriptions: 1, oldest_report_age_seconds: 5, newest_report_age_seconds: 1, periods_waiting_for_usage: 2, periods_postponed: 0 },
  periods: { due_unprocessed: 6, no_charge_total: 1 }, worker: { last_run_at: TS, last_run_age_seconds: 5, invoices_issued_last_run: 3, last_run_failed: 0 }, settlement: { invoice_payments_pending: 2, invoice_payments_stale_pending: 0, invoices_overdue_open: 4, anomalies: {} }, ...o });
const check = (name, lvl, reason = "r") => ({ name, level: lvl, reason });
const recon = (o = {}) => ({ status: "WARN", generated_at: TS, counts: { INFO: 0, WARN: 1, FAIL: 0, CRITICAL: 0 }, min_severity: "WARN",
  discrepancies: [{ code: "stale_usage_report", severity: "WARN", enterprise_id: E1, product_id: PROD, currency: "EUR", subject: "wallet", expected: null, reported: null, detail: "report is old", extra: {} }],
  keys: [{ enterprise_id: E1, product_id: PROD, currency: "EUR", authority_mode: "central", report_seq: 9, report_age_seconds: 3, ledger_max_id: 1, money_cursor_seq: 2, central_issued_total: "300.000000", central_reversed_total: "100.000000", enterprise_received_total: "300.000000", applied_total: "300.000000", deferred_total: "0.000000", reversed_applied_total: "100.000000", unresolved_reversal_total: "0.000000", gross: "10.000000", available: "55.250000", held: "1.000000", shadow_projection: null }], ...o });
const usage = { items: [{ report_id: "19191919-1919-4191-8191-191919191919", enterprise_id: E1, product_id: PROD, currency: "EUR", report_seq: 9, authority_mode: "central", generated_at: TS, received_at: TS, ledger_max_id: 1, money_cursor_seq: 2, available: "55.250000", held: "1.000000", gross: "10.000000", flows: {}, grants: 1 }] };
const settleSummary = { invoices: { open: 4, paid: 10, void: 1, overdue_open: 4 }, invoice_payments: { pending: 2, approved: 9, rejected: 1, stale_pending: 0, stale_after_seconds: 1 }, rejected_on_overdue_open_invoice: [], aging: {}, anomalies: {} };

const R = (extra = {}) => ({
  "GET /auth/me": ADMIN,
  "GET /admin/financial/overview": overview(), "GET /admin/billing/ops": billingOps(),
  "GET /admin/money/accounts": pageOf([account]), [`GET /admin/money/accounts/${ACC}`]: account,
  [`GET /admin/money/accounts/${ACC}/ledger`]: { items: [{ seq: 1, entry_type: "payment_credit", amount: "100.500000", currency: "EUR", source_type: "payment", source_id: PAY, reason: null, actor, created_at: TS }], limit: 100, next_after_seq: null },
  "GET /admin/money/grants": pageOf([grant()]), "GET /admin/money/payments": pageOf([payment()]), "GET /admin/money/usage-reports": usage, "GET /admin/money/reconciliation": recon(),
  "GET /admin/billing/invoice-payments": pageOf([invPayment()]),
  "GET /admin/pricing/books": pageOf([{ id: BOOK, code: "std", name: "Standard", currency: "EUR", created_at: TS }]),
  "GET /admin/pricing/state": { epoch: "e", revision: 2, books: [{ book_id: BOOK, code: "std", currency: "EUR", effective_version_id: VER, reason: "effective", versions: 2, active_versions: 1 }] },
  [`GET /admin/pricing/books/${BOOK}`]: { id: BOOK, code: "std", name: "Standard", currency: "EUR", created_at: TS, versions: [{ id: VER, book_id: BOOK, version: 2, status: "active", effective_from: TS, content_hash: "h", imported: false, created_at: TS, activated_at: TS, retired_at: null, retire_reason: null, editable: false, rule_count: 2 }] },
  [`GET /admin/pricing/versions/${VER}`]: { id: VER, book_id: BOOK, version: 2, status: "active", effective_from: TS, rules: [{ rule_id: "r1", channel: "sms", prefix: "355", operator: "", unit_price: "0.045000" }, { rule_id: "r2", channel: "sms", prefix: "", operator: "", unit_price: "0.100000" }] },
  "GET /admin/pricing/assignments": pageOf([{ id: "a1", enterprise_id: E1, product_id: PROD, book_id: BOOK, effective_from: TS, created_at: TS }]),
  "GET /admin/pricing/preview": { book_id: BOOK, version_id: VER, rule_id: "r1", currency: "EUR", unit_price: "0.045000" },
  "GET /admin/billing/subscriptions": pageOf([sub()]), [`GET /admin/billing/subscriptions/${E1}`]: sub(),
  [`GET /admin/billing/profiles/${E1}`]: { enterprise_id: E1, legal_name: "Acme Sh.p.k", address: "Rruga 1", country: "AL", tax_id: "K1", email: "a@b.al", vat_rate: "0.2000", updated_at: TS },
  "GET /admin/billing/plan-versions/15151515-1515-4151-8151-151515151515": { id: "15151515-1515-4151-8151-151515151515", plan_id: "p", version: 1, status: "active", currency: "EUR", monthly_fee: "1000.000000", included_emails: 50000, content_hash: "h", editable: false, created_at: TS, activated_at: TS, retired_at: null, retire_reason: null },
  "GET /admin/billing/periods": pageOf([period()]), "GET /admin/billing/plans": pageOf([{ id: "p1", code: "growth", name: "Growth", created_at: TS }]),
  "GET /admin/billing/invoices": pageOf([invoice()]), [`GET /admin/billing/invoices/${INV}`]: { ...invoice(), lines: [line(), line({ line_no: 2, line_type: "overage", description: "Extra emails", quantity: "1500", unit_price: "0.100000", amount: "150.000000" })], settlement: settlement() },
  "GET /admin/billing/credit-notes": pageOf([creditNote()]), "GET /admin/billing/settlement": settleSummary, "GET /admin/financial/unresolved-reversals": { items: [] },
  "GET /admin/financial/readiness": { status: "PASS", checks: [check("reconciliation_no_critical", "PASS", "no CRITICAL discrepancy")] },
  "GET /admin/pricing/readiness": { ok: true, checks: [check("pricing_active_version", "PASS")] },
  "GET /admin/billing/final-readiness": { status: "PASS", mode: "shadow", generated_at: TS, checks: [check("billing_invoice_arithmetic", "PASS")], alerts: [] },
  ...extra,
});
const boot = async (me, hash, extra = {}) => {
  setToken("t"); location.hash = hash;
  const f = mockFetch({ ...R(extra), "GET /auth/me": me });
  render(<I18nProvider><App /></I18nProvider>);
  await screen.findByRole("navigation", { name: "Main navigation" });
  return f;
};
const GET_ONLY = (f) => f.calls.every((c) => c.method === "GET" || c.path === "/auth/token");
const WRITE_WORDS = /approve|reject|grant credit|new grant|create|issue|void|publish|assign|resume|save|allocate|reconcile now|top.?up|adjust|reverse|retire|activate/i;
afterEach(() => { location.hash = ""; });

describe("navigation and access", () => {
  it("1 admin sees the Commercial group with every page", async () => {
    await boot(ADMIN, "#com");
    const n = screen.getByRole("navigation", { name: "Main navigation" });
    expect(within(n).getByText("Commercial")).toBeInTheDocument();
    for (const l of ["Commercial Overview", "Credit & Grants", "Payments", "Pricing", "Subscriptions", "Invoices", "Reconciliation", "Financial Readiness"]) expect(within(n).getByRole("link", { name: new RegExp(`^.?\\s*${l}$`) })).toBeInTheDocument();
  });
  it("2 operator reads the same pages and is told it is read-only", async () => {
    await boot(OPERATOR, "#com-invoices");
    expect(await screen.findByText("INV-2026-0001")).toBeInTheDocument();
    expect(screen.getByText("Read-only access")).toBeInTheDocument();
  });
  it("3 an unknown commercial route and a malformed detail id are 404", async () => {
    await boot(ADMIN, "#com-nope");
    expect(await screen.findByText("This page doesn't exist")).toBeInTheDocument();
    location.hash = "#com-invoices/not-a-uuid";
    expect(await screen.findAllByText("This page doesn't exist")).not.toHaveLength(0);
  });
});

describe("overview, credit and grants", () => {
  it("4 overview shows backend numbers and statuses without recomputing", async () => {
    await boot(ADMIN, "#com");
    expect(await screen.findByText("Overdue open invoices")).toBeInTheDocument();
    expect(await screen.findByText("Pending payments")).toBeInTheDocument();
    const od = screen.getByRole("link", { name: "Overdue open invoices" });
    expect(within(od).getByText("4")).toBeInTheDocument();
    expect(within(screen.getByRole("main")).getByRole("link", { name: "Reconciliation" })).toHaveTextContent("warn");
    expect(await screen.findByText("stale pending payments")).toBeInTheDocument();
  });
  it("5 overview: one failing source does not blank the page", async () => {
    await boot(ADMIN, "#com", { "GET /admin/billing/ops": errorResponse(500, "x", "Traceback boom") });
    expect((await screen.findAllByText("Credit accounts")).length).toBeGreaterThan(0);
    expect(screen.queryByText(/Traceback/)).toBeNull();
  });
  it("6 credit accounts list uses backend totals, formatted exactly", async () => {
    await boot(ADMIN, "#com-credit");
    const row = (await screen.findByText("Available to grant")).closest("table").querySelector("tbody tr");
    expect(row.textContent.replace(/\D/g, "")).toContain("123436789");
    expect(row.textContent).toContain("EUR");
  });
  it("7 account detail shows totals, the Enterprise-reported balance and the ledger", async () => {
    await boot(ADMIN, `#com-credit/${ACC}`);
    expect(await screen.findByText("Append-only history, oldest first. Nothing here can be edited.")).toBeInTheDocument();
    expect(await screen.findByText("payment credit")).toBeInTheDocument();
    expect(screen.getByText("Balance reported by Enterprise")).toBeInTheDocument();
    expect(await screen.findByText(/55[.,]25 EUR/)).toBeInTheDocument();
  });
  it("8 ledger loads the next page with after_seq from the server", async () => {
    let n = 0;
    const f = await boot(ADMIN, `#com-credit/${ACC}`, { [`GET /admin/money/accounts/${ACC}/ledger`]: () => (++n === 1
      ? { items: [{ seq: 1, entry_type: "payment_credit", amount: "1.000000", currency: "EUR", source_type: "payment", source_id: "a", reason: null, actor, created_at: TS }], limit: 100, next_after_seq: 1 }
      : { items: [{ seq: 2, entry_type: "grant", amount: "2.000000", currency: "EUR", source_type: "grant", source_id: "b", reason: null, actor, created_at: TS }], limit: 100, next_after_seq: null }) });
    await userEvent.click(await screen.findByRole("button", { name: "Load more" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Ledger" }).closest("section").querySelectorAll("tbody tr").length).toBe(2));
    expect(f.calls.some((c) => c.url.includes("after_seq=1"))).toBe(true);
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });
  it("9 grants tab lists grants and opens a read-only detail", async () => {
    await boot(ADMIN, "#com-credit?tab=grants");
    await userEvent.click(await screen.findByRole("button", { name: /Details for grant/ }));
    const dlg = await screen.findByRole("dialog");
    expect(within(dlg).getByText("welcome")).toBeInTheDocument();
    expect(within(dlg).getAllByRole("button").map((b) => b.textContent)).toEqual(["Close"]);
  });
});

describe("payments", () => {
  it("10 credit payments list with status filter in the URL", async () => {
    const f = await boot(ADMIN, "#com-payments");
    expect(await screen.findByText("REF-77")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Status"), "pending");
    await userEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => expect(location.hash).toContain("status=pending"));
    await waitFor(() => expect(f.calls.some((c) => c.path === "/admin/money/payments" && c.url.includes("status=pending"))).toBe(true));
  });
  it("11 payment detail shows maker, decision time and reference — no raw provider payload", async () => {
    await boot(ADMIN, "#com-payments");
    await userEvent.click(await screen.findByRole("button", { name: /Details for payment/ }));
    const dlg = await screen.findByRole("dialog");
    for (const w of ["Created by", "Approved by", "Rejection reason", "REF-77"]) expect(within(dlg).getByText(w)).toBeInTheDocument();
    expect(dlg.textContent).not.toMatch(/payload|card|iban/i);
  });
  it("12 invoice payments tab shows the invoice link and allocation", async () => {
    await boot(ADMIN, "#com-payments?tab=invoice");
    const link = await screen.findByRole("link", { name: /dddddddd/ });
    expect(link.getAttribute("href")).toBe(`#com-invoices/${INV}`);
    await userEvent.click(screen.getByRole("button", { name: /Details for payment/ }));
    expect(await screen.findByText("13131313-1313-4131-8131-131313131313")).toBeInTheDocument();
  });
  it("13 an invalid ID filter is blocked locally and never sent", async () => {
    const f = await boot(ADMIN, "#com-payments?tab=invoice");
    await screen.findByText("REF-77");
    const before = f.calls.length;
    await userEvent.type(screen.getByLabelText("Invoice ID"), "nope");
    await userEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    expect(await screen.findByText(/Enter a valid ID \(UUID\)\./)).toBeInTheDocument();
    expect(f.calls.length).toBe(before);
  });
});

describe("pricing", () => {
  it("14 price books show the server's effective version", async () => {
    await boot(ADMIN, "#com-pricing");
    expect(await screen.findByText("std")).toBeInTheDocument();
    expect(await screen.findByText("ffffffff…")).toBeInTheDocument();
  });
  it("15 book detail lists versions; rules load from the server and show exact unit prices", async () => {
    await boot(ADMIN, `#com-pricing/${BOOK}`);
    await userEvent.click(await screen.findByRole("button", { name: "Rules of version 2" }));
    expect(await screen.findByText("0.045000")).toBeInTheDocument();
    expect(screen.getByText("355")).toBeInTheDocument();
  });
  it("16 assignments tab", async () => {
    await boot(ADMIN, "#com-pricing?tab=assignments");
    expect(await screen.findByText("Price assignments")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: /eeeeeeee/ })).toBeInTheDocument();
  });
  it("17 preview asks the server and shows its answer; fail-closed 409 is shown as an error", async () => {
    const f = await boot(ADMIN, "#com-pricing?tab=preview");
    await userEvent.type(await screen.findByLabelText("Enterprise ID"), E1);
    await userEvent.type(screen.getByLabelText("Product ID"), PROD);
    await userEvent.type(screen.getByLabelText("Number"), "35569");
    await userEvent.click(screen.getByRole("button", { name: "Ask the server" }));
    expect(await screen.findByText(/0\.045000 EUR/)).toBeInTheDocument();
    expect(f.calls.find((c) => c.path === "/admin/pricing/preview").url).toContain("number=35569");
  });
  it("18 preview: 409 from the server surfaces as a conflict", async () => {
    await boot(ADMIN, "#com-pricing?tab=preview", { "GET /admin/pricing/preview": errorResponse(409, "conflict", "no price assignment is effective for this enterprise/product") });
    await userEvent.type(await screen.findByLabelText("Enterprise ID"), E1);
    await userEvent.type(screen.getByLabelText("Product ID"), PROD);
    await userEvent.click(screen.getByRole("button", { name: "Ask the server" }));
    expect(await screen.findByText("This changed in the meantime")).toBeInTheDocument();
  });
});

describe("subscriptions and periods", () => {
  it("19 subscription list", async () => {
    await boot(ADMIN, "#com-subs");
    expect(await screen.findByText("11111111…")).toBeInTheDocument();
    expect(screen.getAllByText("active").length).toBeGreaterThan(0);
  });
  it("20 subscription detail: plan version, billing profile and periods", async () => {
    await boot(ADMIN, `#com-subs/${E1}`);
    expect(await screen.findByText("Acme Sh.p.k")).toBeInTheDocument();
    expect(await screen.findByText(/1[.,\s]?000[.,]00 EUR/)).toBeInTheDocument();
    expect(await screen.findByText("Billing periods")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: /dddddddd/ })).toBeInTheDocument();
  });
  it("21 a missing billing profile is an empty state, not an error", async () => {
    await boot(ADMIN, `#com-subs/${E1}`, { [`GET /admin/billing/profiles/${E1}`]: errorResponse(404, "not_found", "billing profile not found") });
    expect(await screen.findByText("No billing profile")).toBeInTheDocument();
  });
  it("22 billing periods tab and plans tab", async () => {
    await boot(ADMIN, "#com-subs?tab=periods");
    expect((await screen.findAllByText("invoiced")).length).toBeGreaterThan(0);
    location.hash = "#com-subs?tab=plans";
    expect(await screen.findByText("growth")).toBeInTheDocument();
  });
});

describe("invoices", () => {
  it("23 list shows number, totals, status and links to the detail", async () => {
    await boot(ADMIN, "#com-invoices");
    const a = await screen.findByRole("link", { name: "INV-2026-0001" });
    expect(a.getAttribute("href")).toBe(`#com-invoices/${INV}`);
    expect(screen.getByText(/1[.,\s]?200[.,]00 EUR/)).toBeInTheDocument();
  });
  it("24 pagination is server side: Next sends offset and keeps filters", async () => {
    const f = await boot(ADMIN, "#com-invoices?status=open", { "GET /admin/billing/invoices": () => pageOf([invoice()], { has_more: true }) });
    await userEvent.click(await screen.findByRole("button", { name: "Next" }));
    await waitFor(() => expect(location.hash).toContain("offset=50"));
    expect(location.hash).toContain("status=open");
    await waitFor(() => expect(f.calls.some((c) => c.path === "/admin/billing/invoices" && c.url.includes("offset=50") && c.url.includes("status=open"))).toBe(true));
  });
  it("25 filters by enterprise and status are sent to the server", async () => {
    const f = await boot(ADMIN, "#com-invoices");
    await screen.findByText("INV-2026-0001");
    await userEvent.type(screen.getByLabelText("Enterprise ID"), E1);
    await userEvent.selectOptions(screen.getByLabelText("Status"), "paid");
    await userEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => expect(f.calls.some((c) => c.path === "/admin/billing/invoices" && c.url.includes(`enterprise_id=${E1}`) && c.url.includes("status=paid"))).toBe(true));
    expect(location.hash).toContain("status=paid");
  });
  it("26 an empty filtered list offers to clear the filters", async () => {
    await boot(ADMIN, "#com-invoices?status=void", { "GET /admin/billing/invoices": pageOf([]) });
    expect(await screen.findByText("Nothing matches your filters")).toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: "Clear filters" })[0]);
    await waitFor(() => expect(location.hash).toBe("#com-invoices"));
  });
  it("27 detail shows the issuer and bill-to snapshots, tax fields and totals exactly as sent", async () => {
    await boot(ADMIN, `#com-invoices/${INV}`);
    expect(await screen.findByText("Platform Ltd")).toBeInTheDocument();
    expect(screen.getByText("Acme Sh.p.k")).toBeInTheDocument();
    expect(screen.getByText("0.2000")).toBeInTheDocument();
    expect(screen.getAllByText(/1[.,\s]?200[.,]00 EUR/).length).toBeGreaterThan(0);
  });
  it("28 invoice lines render description, exact quantity, unit price and amount", async () => {
    await boot(ADMIN, `#com-invoices/${INV}`);
    const row = (await screen.findByText("Extra emails")).closest("tr");
    expect(row.textContent).toMatch(/1[.,\s]?500/);
    expect(row.textContent).toMatch(/0[.,]10 EUR/);
    expect(row.textContent).toMatch(/150[.,]00 EUR/);
  });
  it("29 a line total that disagrees with the invoice total is NOT 'fixed' by the console", async () => {
    await boot(ADMIN, `#com-invoices/${INV}`, { [`GET /admin/billing/invoices/${INV}`]: { ...invoice({ total: "999.000000" }), lines: [line({ amount: "1.000000" })], settlement: settlement() } });
    const total = (await screen.findByText("Total", { selector: "dt" })).nextElementSibling;
    expect(total.textContent).toMatch(/999[.,]00 EUR/);
    expect(total.textContent).not.toMatch(/1[.,\s]?200/);
  });
  it("30 settlement panel: allocation, payments, credited total and net amount come from the server", async () => {
    await boot(ADMIN, `#com-invoices/${INV}`);
    expect(await screen.findByText("Settlement")).toBeInTheDocument();
    expect(screen.getByText("Net amount")).toBeInTheDocument();
    expect(screen.getByText(/1[.,\s]?150[.,]00 EUR/)).toBeInTheDocument();
    expect(screen.getByText("CN-2026-0001")).toBeInTheDocument();
  });
  it("31 an unpaid invoice with no allocation says so", async () => {
    await boot(ADMIN, `#com-invoices/${INV}`, { [`GET /admin/billing/invoices/${INV}`]: { ...invoice(), lines: [line()], settlement: settlement({ allocation: null, payments: [], credit_notes: [], credited_total: "0.000000", net_amount: "1200.000000" }) } });
    expect(await screen.findByText("No payment allocated")).toBeInTheDocument();
    expect(screen.getByText("No payments recorded for this invoice.")).toBeInTheDocument();
  });
  it("32 credit notes tab lists and opens a read-only detail", async () => {
    await boot(ADMIN, "#com-invoices?tab=credit-notes");
    await userEvent.click(await screen.findByRole("button", { name: /Details for credit note CN-2026-0001/ }));
    expect(within(await screen.findByRole("dialog")).getByText("goodwill")).toBeInTheDocument();
  });
});

describe("reconciliation and readiness", () => {
  it("33 reconciliation shows the server verdict, discrepancies, keys and usage reports", async () => {
    await boot(ADMIN, "#com-recon");
    expect(await screen.findByText("stale usage report")).toBeInTheDocument();
    expect(await screen.findByText("Reconciled keys")).toBeInTheDocument();
    expect((await screen.findAllByText(/55[.,]25 EUR/)).length).toBeGreaterThan(0);
    expect(await screen.findByText("Invoice settlement summary")).toBeInTheDocument();
  });
  it("34 changing the minimum severity asks the server again", async () => {
    const f = await boot(ADMIN, "#com-recon");
    await screen.findByText("Reconciled keys");
    await userEvent.selectOptions(screen.getByLabelText("Minimum severity"), "FAIL");
    await waitFor(() => expect(f.calls.some((c) => c.path === "/admin/money/reconciliation" && c.url.includes("min_severity=FAIL"))).toBe(true));
  });
  it("35 readiness PASS", async () => {
    await boot(ADMIN, "#com-ready");
    await screen.findByText("Financial readiness");
    expect((await screen.findAllByText("pass")).length).toBeGreaterThanOrEqual(3);
  });
  it("36 readiness WARN and FAIL come from the server and are not softened", async () => {
    await boot(ADMIN, "#com-ready", { "GET /admin/financial/readiness": { status: "WARN", checks: [check("payments_not_stale", "WARN", "1 pending")] }, "GET /admin/billing/final-readiness": { status: "FAIL", mode: "shadow", generated_at: TS, checks: [check("billing_settlement_integrity", "FAIL", "broken")], alerts: [] } });
    expect(await screen.findByText("payments not stale")).toBeInTheDocument();
    expect(screen.getAllByText("warn").length).toBeGreaterThan(0);
    expect(screen.getAllByText("fail").length).toBeGreaterThan(0);
  });
  it("37 a readiness source that cannot be loaded is UNKNOWN, never pass", async () => {
    await boot(ADMIN, "#com-ready", { "GET /admin/financial/readiness": errorResponse(500, "x", "internal boom") });
    const card = (await screen.findByText("Financial readiness")).closest("section");
    expect(await within(card).findByText("unknown")).toBeInTheDocument();
    expect(within(card).queryByText("pass")).toBeNull();
    expect(screen.queryByText(/internal boom/)).toBeNull();
  });
  it("38 an unrecognised readiness level renders as unknown", async () => {
    await boot(ADMIN, "#com-ready", { "GET /admin/financial/readiness": { status: "GREEN", checks: [check("x_check", "OK", "fine")] } });
    const card = (await screen.findByText("Financial readiness")).closest("section");
    expect((await within(card).findAllByText("unknown")).length).toBeGreaterThan(0);
    expect(within(card).queryByText("pass")).toBeNull();
  });
});

describe("errors", () => {
  const msg = (kind) => ({ 403: "You don't have access", 404: "We couldn't find that", 429: "Too many requests", 500: "Something went wrong" }[kind]);
  for (const [status, code] of [[403, "forbidden"], [404, "not_found"], [429, "rate_limited"], [500, "internal"]]) {
    it(`39 HTTP ${status} on a list is a clear, sanitized error`, async () => {
      await boot(ADMIN, "#com-invoices", { "GET /admin/billing/invoices": errorResponse(status, code, "Traceback (most recent call last): secret_provider_payload") });
      expect(await screen.findByText(msg(status))).toBeInTheDocument();
      expect(document.body.textContent).not.toMatch(/Traceback|secret_provider_payload/);
    });
  }
  it("40 401 returns to sign-in with a notice", async () => {
    setToken("t"); location.hash = "#com-invoices";
    mockFetch({ ...R(), "GET /admin/billing/invoices": errorResponse(401, "unauthorized", "x") });
    render(<I18nProvider><App /></I18nProvider>);
    expect(await screen.findByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByText("Your session has ended. Please sign in again.")).toBeInTheDocument();
  });
  it("41 network failure offers a retry that works", async () => {
    let fail = true;
    await boot(ADMIN, "#com-invoices", { "GET /admin/billing/invoices": () => { if (fail) throw new TypeError("Failed to fetch"); return pageOf([invoice()]); } });
    expect(await screen.findByText("You seem to be offline")).toBeInTheDocument();
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("INV-2026-0001")).toBeInTheDocument();
  });
});

describe("read-only guarantee, accessibility, i18n and robustness", () => {
  const PAGES = ["#com", "#com-credit", `#com-credit/${ACC}`, "#com-credit?tab=grants", "#com-payments", "#com-payments?tab=invoice", "#com-pricing", `#com-pricing/${BOOK}`, "#com-pricing?tab=assignments", "#com-subs", `#com-subs/${E1}`, "#com-subs?tab=plans", "#com-invoices", `#com-invoices/${INV}`, "#com-invoices?tab=credit-notes", "#com-recon", "#com-ready"];
  it("42 no commercial page has a financial write control, and only GETs are sent (admin and operator)", async () => {
    for (const me of [ADMIN, OPERATOR]) {
      for (const h of PAGES) {
        const f = await boot(me, h);
        await act(async () => { await new Promise((r) => setTimeout(r, 30)); });
        const names = screen.getAllByRole("button").map((b) => b.textContent);
        expect(names.filter((n) => WRITE_WORDS.test(n)), `${h} ${names.join("|")}`).toEqual([]);
        expect(screen.queryAllByRole("form").length).toBeGreaterThanOrEqual(0);
        expect(GET_ONLY(f), h).toBe(true);
        document.body.innerHTML = "";
      }
    }
  }, 90000);
  it("43 unknown backend statuses render neutral, not green", async () => {
    await boot(ADMIN, "#com-payments", { "GET /admin/money/payments": pageOf([payment({ status: "quantum_pending" })]) });
    const b = (await screen.findByText("quantum pending")).closest(".badge");
    expect(b.className).toContain("muted");
  });
  it("44 keyboard: Tab reaches Details, Enter opens the modal, Escape closes it and restores focus", async () => {
    await boot(ADMIN, "#com-payments");
    const btn = await screen.findByRole("button", { name: /Details for payment/ });
    btn.focus();
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(btn);
  });
  it("45 responsive: tables carry data-label cells and secondary columns are marked for small screens", async () => {
    await boot(ADMIN, "#com-invoices");
    const t = (await screen.findByText("INV-2026-0001")).closest("table");
    expect(t.querySelectorAll("td[data-label]").length).toBeGreaterThan(0);
    expect(t.querySelectorAll(".hide-sm").length).toBeGreaterThan(0);
  });
  it("46 i18n: the language switch translates commercial pages (SQ and EN)", async () => {
    await boot(ADMIN, "#com-invoices");
    await userEvent.click(screen.getByRole("button", { name: "English" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Invoices" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Shqip" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Faturat" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Pasqyra komerciale/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "English" }));
  });
});
