// Tabela e vetme e rrugëve të konsolës CENTRAL. Hash routing me query për gjendje filtrash: #senders?status=pending&offset=50
// Të gjitha rrugët janë të lexueshme nga admin dhe operator; shkrimi kontrollohet nga `canWrite` (vetëm UI — serveri vendos).
import { T } from "./i18n.jsx";

export const DEFAULT_ROUTE = "dashboard";
export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export const NAV = [
  { group: "", items: [{ id: "dashboard", label: T("Overview"), icon: "◧", load: () => import("./pages/Overview.jsx"), desc: T("Sender governance at a glance.") }] },
  { group: T("Sender Governance"), items: [
    { id: "senders", label: T("Sender Registry"), icon: "✦", detail: true, load: () => import("./pages/Senders.jsx"), desc: T("Every sender ID across all accounts, with its decision history.") },
    { id: "policies", label: T("Country Policies"), icon: "§", detail: true, load: () => import("./pages/Policies.jsx"), desc: T("Which sender kinds are allowed per country, and whether they need approval.") },
  ] },
  { group: T("Operations"), items: [
    { id: "ops", label: T("Sender Readiness"), icon: "✔", load: () => import("./pages/OpsOverview.jsx"), desc: T("Overall health and Central consistency checks, straight from the server.") },
    { id: "ops-sync", label: T("Sync & Requests"), icon: "⇄", load: () => import("./pages/OpsSync.jsx"), desc: T("The sender feed to Enterprise and the requests coming back.") },
    { id: "ops-bootstrap", label: T("Bootstrap & Cutover"), icon: "⏻", load: () => import("./pages/OpsBootstrap.jsx"), desc: T("Bootstrap runs, shadow evidence and cutover state. Read-only.") },
    { id: "ops-alerts", label: T("Alerts"), icon: "!", load: () => import("./pages/OpsAlerts.jsx"), desc: T("Sender alerts that Central can evaluate.") },
  ] },
  { group: T("Commercial"), items: [
    { id: "com", label: T("Commercial Overview"), icon: "€", load: () => import("./pages/ComOverview.jsx"), desc: T("Credit, payments, invoices and reconciliation at a glance. Read-only, straight from the server.") },
    { id: "com-credit", label: T("Credit & Grants"), icon: "◎", detail: true, load: () => import("./pages/ComCredit.jsx"), desc: T("Credit accounts, grants and the append-only ledger.") },
    { id: "com-payments", label: T("Payments"), icon: "⇆", load: () => import("./pages/ComPayments.jsx"), desc: T("Credit payments and invoice payments, with who recorded and decided them.") },
    { id: "com-pricing", label: T("Pricing"), icon: "%", detail: true, load: () => import("./pages/ComPricing.jsx"), desc: T("Price books, versions, rules and which enterprise is assigned to which book.") },
    { id: "com-subs", label: T("Subscriptions"), icon: "↻", detail: true, load: () => import("./pages/ComSubscriptions.jsx"), desc: T("Plans, subscriptions, billing profiles and billing periods.") },
    { id: "com-invoices", label: T("Invoices"), icon: "▤", detail: true, load: () => import("./pages/ComInvoices.jsx"), desc: T("Invoices, their lines, settlement and credit notes.") },
    { id: "com-recon", label: T("Reconciliation"), icon: "≈", load: () => import("./pages/ComRecon.jsx"), desc: T("Usage reports and the server's reconciliation of credit and settlement.") },
    { id: "com-ready", label: T("Financial Readiness"), icon: "✔", load: () => import("./pages/ComReadiness.jsx"), desc: T("Financial, pricing and billing readiness, computed by the server.") },
  ] },
  { group: T("Audit"), items: [
    { id: "audit-senders", label: T("Sender decisions"), icon: "✦", load: () => import("./pages/AuditSenders.jsx"), desc: T("Every sender approval, rejection, revocation and automatic policy decision.") },
    { id: "audit-commercial", label: T("Commercial audit"), icon: "€", load: () => import("./pages/AuditCommercial.jsx"), desc: T("Staff actions on plans, prices, invoices, payments and credit.") },
    { id: "audit-ops", label: T("Operations state"), icon: "♥", load: () => import("./pages/AuditOps.jsx"), desc: T("The latest Enterprise operations report. Central keeps no history.") },
    { id: "audit-staff", label: T("Staff activity"), icon: "☰", load: () => import("./pages/AuditStaff.jsx"), desc: T("Every audited action by staff and system processes.") },
  ] },
];

export const canWrite = (me) => !!me && me.role === "admin";

const ALL = NAV.flatMap((g) => g.items.map((i) => ({ ...i, group: g.group })));
export const findRoute = (id) => ALL.find((r) => r.id === id) || null;

/** "#senders/<uuid>?status=x" → { id, rest, query }. id e pavlefshme ⇒ null (404). */
export function parseHash(hash) {
  const raw = String(hash || "").replace(/^#/, "");
  const [path, qs = ""] = raw.split("?");
  const [id, ...rest] = path.split("/");
  const query = Object.fromEntries(new URLSearchParams(qs));
  if (!id) return { id: DEFAULT_ROUTE, rest: [], query };
  return /^[a-z0-9-]{1,40}$/.test(id) ? { id, rest: rest.filter(Boolean), query } : { id: null, rest: [], query };
}

export function buildHash(id, rest = [], query = {}) {
  const qs = new URLSearchParams(Object.entries(query).filter(([, v]) => v !== undefined && v !== null && v !== "")).toString();
  return `#${[id, ...rest].join("/")}${qs ? `?${qs}` : ""}`;
}

export function resolveRoute(hash) {
  const { id, rest, query } = parseHash(hash);
  const route = id && findRoute(id);
  if (!route) return { status: "not_found", id, crumbs: [] };
  if (rest.length && (!route.detail || rest.length > 1 || !UUID.test(rest[0]))) return { status: "not_found", id, crumbs: [] };
  const crumbs = [...(route.group ? [{ label: route.group }] : []), { label: route.label, href: buildHash(route.id) }];
  return { status: "ok", id, route, rest, query, crumbs };
}
