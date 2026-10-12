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
