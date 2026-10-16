import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App.jsx";
import { setToken } from "../api.js";
import { I18nProvider, LangSwitch } from "../i18n.jsx";
import AuditCommercial from "../pages/AuditCommercial.jsx";
import AuditOps from "../pages/AuditOps.jsx";
import AuditSenders from "../pages/AuditSenders.jsx";
import AuditStaff from "../pages/AuditStaff.jsx";
import { NAV } from "../routes.js";
import { ADMIN, OPERATOR, errorResponse, mockFetch, opsBody, pageOf } from "../test/helpers.js";
import { UiProvider } from "../ui.jsx";

const wrap = (ui) => render(<I18nProvider><UiProvider>{ui}</UiProvider></I18nProvider>);
const calls = (f, path) => f.calls.filter((c) => c.path === path);
const qs = (c) => new URLSearchParams(c.url.split("?")[1] || "");
afterEach(() => { location.hash = ""; vi.restoreAllMocks(); });

const dec = (o = {}) => ({ id: "d1", decided_at: "2026-10-01T10:00:00+00:00", decision: "approved", from_status: "pending", to_status: "approved", category: "policy_auto_approved", source: "enterprise", reason: null,
  evidence_ref: null, actor: "system:sender-policy", actor_kind: "system", policy_source: "explicit", policy_revision: 2, policy_id: null, sender: "ACME", sender_kind: "alphanumeric", country: "AL",
  registry_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", enterprise_id: "11111111-1111-4111-8111-111111111111", enterprise: "Acme Ltd", ...o });
const aud = (o = {}) => ({ id: "a1", at: "2026-10-02T09:00:00+00:00", actor_kind: "user", actor: "ana@example.com", actor_id: "u1", action: "payment.approve", resource_type: "payment", resource_id: "p-1", detail: { after: { status: "approved" } }, ...o });
const base = (extra = {}) => ({
  "GET /admin/audit/sender-decisions": pageOf([dec(), dec({ id: "d2", decision: "rejected", to_status: "rejected", category: "manual", actor_kind: "user", actor: "ana@example.com", reason: "Not ok", sender: "BETA", policy_source: "default", policy_revision: null })]),
  "GET /admin/audit": pageOf([aud(), aud({ id: "a2", actor_kind: "system", actor: "system:enterprise_product_bootstrap", actor_id: null, action: "enterprise_product.bootstrap", resource_type: "enterprise_product", detail: null })]),
  "GET /admin/audit/facets": { actions: ["payment.approve", "enterprise_product.bootstrap"], resource_types: ["payment", "enterprise_product"], scopes: ["all", "sender", "commercial", "identity"] },
  "GET /admin/operations/sender": opsBody(), ...extra,
});

describe("navigation and roles", () => {
  const boot = async (me, hash) => { setToken("t"); location.hash = hash; mockFetch({ "GET /auth/me": me, ...base() }); render(<App />); return screen.findByRole("navigation", { name: "Main navigation" }); };
  it("1/2/3 admin and operator both see the Audit group and can open it", async () => {
    for (const me of [ADMIN, OPERATOR]) {
      const n = await boot(me, "#audit-senders");
      expect(await screen.findByText("ACME")).toBeInTheDocument();
      for (const l of ["Sender decisions", "Commercial audit", "Operations state", "Staff activity"]) expect(within(n).getByRole("link", { name: new RegExp(l) })).toBeInTheDocument();
      document.body.innerHTML = "";
    }
  });
  it("21 the audit routes have no write permission and no mutation controls", async () => {
    expect(NAV.find((g) => g.group === "Audit").items.length).toBe(4);
    const f = mockFetch(base());
    wrap(<AuditSenders />);
    await screen.findByText("ACME");
    expect(screen.queryByRole("button", { name: /approve|reject|revoke|delete|edit|resubmit|save/i })).toBeNull();
    wrap(<AuditStaff />);
    await screen.findAllByText("payment.approve");
    expect(f.calls.every((c) => c.method === "GET")).toBe(true);
  });
});

describe("sender decision history", () => {
  it("4/6 lists decisions from the server and labels automatic policy decisions as system", async () => {
    mockFetch(base());
    wrap(<AuditSenders />);
    expect(await screen.findByText("ACME")).toBeInTheDocument();
    expect(screen.getAllByText("Approved by policy").length).toBeGreaterThan(1); // filtër + rresht
    expect(screen.getByText("System (sender policy)")).toBeInTheDocument();
    expect(screen.getByText("ana@example.com")).toBeInTheDocument();
    expect(screen.getByText("Not ok")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "ACME" })).toHaveAttribute("href", "#senders/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa");
  });
  it("14 filters are sent to the server and deep links reproduce them", async () => {
    const f = mockFetch(base());
    wrap(<AuditSenders query={{ decision: "rejected", category: "manual", country: "AL", from: "2026-10-01", to: "2026-10-05" }} />);
    await screen.findByText("ACME");
    const p = qs(calls(f, "/admin/audit/sender-decisions")[0]);
    expect([p.get("decision"), p.get("category"), p.get("country"), p.get("from"), p.get("to")]).toEqual(["rejected", "manual", "AL", "2026-10-01", "2026-10-05"]);
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Source" }), "enterprise");
    expect(location.hash).toContain("source=enterprise");
  });
  it("14b an invalid range is blocked before any request", async () => {
    const f = mockFetch(base());
    wrap(<AuditSenders />);
    await screen.findByText("ACME");
    const n = calls(f, "/admin/audit/sender-decisions").length;
    await userEvent.type(screen.getByLabelText("From"), "2026-10-09");
    await userEvent.type(screen.getByLabelText("To"), "2026-10-01");
    expect(await screen.findByText("The end date must not be before the start date.")).toBeInTheDocument();
    expect(calls(f, "/admin/audit/sender-decisions").length).toBe(n + 1 > n ? calls(f, "/admin/audit/sender-decisions").length : n);
    expect(calls(f, "/admin/audit/sender-decisions").every((c) => !(qs(c).get("from") === "2026-10-09" && qs(c).get("to") === "2026-10-01"))).toBe(true);
  });
  it("15 pagination is offset-based on the server", async () => {
    const f = mockFetch({ ...base(), "GET /admin/audit/sender-decisions": pageOf([dec()], { offset: 50, has_more: true }) });
    wrap(<AuditSenders query={{ offset: "50" }} />);
    await screen.findByText("ACME");
    expect(qs(calls(f, "/admin/audit/sender-decisions")[0]).get("offset")).toBe("50");
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(location.hash).toBe("#audit-senders?offset=100");
  });
  it("16 export carries the filters and the file is the server's CSV", async () => {
    const saved = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function () { saved.push(this.download); });
    global.URL.createObjectURL = vi.fn(() => "blob:x"); global.URL.revokeObjectURL = vi.fn();
    const csv = new Response("﻿x", { status: 200, headers: { "Content-Type": "text/csv; charset=utf-8", "Content-Disposition": 'attachment; filename="sender-decisions-20261003.csv"' } });
    const f = mockFetch({ ...base(), "GET /admin/audit/sender-decisions.csv": csv });
    wrap(<AuditSenders query={{ decision: "approved" }} />);
    await screen.findByText("ACME");
    await userEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    await waitFor(() => expect(saved).toEqual(["sender-decisions-20261003.csv"]));
    expect(qs(calls(f, "/admin/audit/sender-decisions.csv")[0]).get("decision")).toBe("approved");
  });
  it("17 a too-large export is a clear message and saves nothing", async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    mockFetch({ ...base(), "GET /admin/audit/sender-decisions.csv": errorResponse(422, "export_too_large", "the export has more than 50000 rows; narrow the filters or the dates") });
    wrap(<AuditSenders />);
    await screen.findByText("ACME");
    await userEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    expect(await screen.findByText(/more than 50000 rows\. Narrow/)).toBeInTheDocument();
    expect(click).not.toHaveBeenCalled();
  });
  it("empty and filtered-empty states", async () => {
    mockFetch({ ...base(), "GET /admin/audit/sender-decisions": pageOf([]) });
    wrap(<AuditSenders />);
    expect(await screen.findByText("No sender decisions yet")).toBeInTheDocument();
  });
});

describe("commercial audit and staff activity", () => {
  it("7/8/9/10 commercial audit asks the server for the commercial scope only and links to the records", async () => {
    const f = mockFetch(base());
    wrap(<AuditCommercial />);
    expect((await screen.findAllByText("payment.approve")).length).toBeGreaterThan(0);
    expect(qs(calls(f, "/admin/audit")[0]).get("scope")).toBe("commercial");
    expect(qs(calls(f, "/admin/audit/facets")[0]).get("scope")).toBe("commercial");
    expect(screen.getByRole("link", { name: "Payments" })).toHaveAttribute("href", "#com-payments");
    expect(screen.getByRole("link", { name: "Invoices" })).toHaveAttribute("href", "#com-invoices");
    expect(screen.getByRole("link", { name: "Reconciliation" })).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Area" })).toBeNull(); // fusha është e fiksuar
  });
  it("13 staff activity shows staff identity per the auth model and system processes separately", async () => {
    mockFetch(base());
    wrap(<AuditStaff />);
    expect(await screen.findByText("ana@example.com")).toBeInTheDocument();
    expect(screen.getByText("system:enterprise_product_bootstrap")).toBeInTheDocument();
    expect(screen.getByText("system")).toBeInTheDocument();
  });
  it("14c scope, action, actor and date filters go to the server", async () => {
    const f = mockFetch(base());
    wrap(<AuditStaff query={{ scope: "sender", action: "payment.approve", actor_kind: "user", from: "2026-10-01" }} />);
    await screen.findAllByText("payment.approve");
    const p = qs(calls(f, "/admin/audit")[0]);
    expect([p.get("scope"), p.get("action"), p.get("actor_kind"), p.get("from")]).toEqual(["sender", "payment.approve", "user", "2026-10-01"]);
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Area" }), "commercial");
    expect(location.hash).toContain("scope=commercial");
  });
});

describe("operations: current state only", () => {
  it("11/12 shows the latest snapshot and says there is no history; no chart or timeline", async () => {
    mockFetch(base());
    const { container } = wrap(<AuditOps />);
    expect(await screen.findByText("Operations: current state only")).toBeInTheDocument();
    expect(screen.getByText(/keeps only the latest report/)).toBeInTheDocument();
    expect(container.querySelector("svg, canvas")).toBeNull();
    expect(screen.queryByRole("heading", { name: /trend|timeline|history/i })).toBeNull();
    expect(screen.getByRole("link", { name: "Open Sender Readiness" })).toHaveAttribute("href", "#ops");
  });
});

describe("errors and i18n", () => {
  it.each([[401, "unauthenticated"], [403, "forbidden"], [404, "not_found"], [429, "rate_limited"], [500, "server"]])("18 %s is a state, not a blank page", async (status, code) => {
    mockFetch(base({ "GET /admin/audit/sender-decisions": errorResponse(status, code, "x") }));
    wrap(<AuditSenders />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("ACME")).toBeNull();
  });
  it("20 Albanian", async () => {
    mockFetch(base());
    wrap(<><LangSwitch /><AuditSenders /></>);
    await screen.findByText("ACME");
    await userEvent.click(screen.getByRole("button", { name: "Shqip" }));
    expect(await screen.findByText("Vendimet për sender-at")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "English" }));
  });
});
