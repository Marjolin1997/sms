import { vi } from "vitest";

/** fetch i rremë: routes = { "GET /v1/me": data | (req) => data | Response }. Regjistron thirrjet. */
export function mockFetch(routes) {
  const calls = [];
  const fn = vi.fn(async (url, init = {}) => {
    const method = (init.method || "GET").toUpperCase();
    const path = String(url).split("?")[0];
    const key = `${method} ${path}`;
    calls.push({ method, path, url: String(url), headers: init.headers || {}, body: init.body ? JSON.parse(init.body) : undefined });
    const h = routes[key];
    if (h === undefined) return new Response(JSON.stringify({ detail: { code: "not_found", message: `no mock for ${key}` } }), { status: 404 });
    const out = typeof h === "function" ? await h(calls[calls.length - 1]) : h;
    if (out instanceof Response) return out;
    return new Response(JSON.stringify(out), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  fn.calls = calls;
  return fn;
}

export const errorResponse = (status, code, message) =>
  new Response(JSON.stringify({ detail: { code, message } }), { status, headers: { "Content-Type": "application/json" } });

export const ADMIN = { id: "u-admin", email: "admin@example.com", role: "admin", status: "active" };
export const OPERATOR = { id: "u-op", email: "op@example.com", role: "operator", status: "active" };

const E1 = "11111111-1111-4111-8111-111111111111";
let seq = 0;
const uid = () => `00000000-0000-4000-8000-${String(++seq).padStart(12, "0")}`;
export const SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
export const PID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
export const sender = (o = {}) => ({ id: uid(), enterprise_id: E1, external_ref: "sms-sender-1", country: "AL", sender_kind: "alphanumeric", display_value: "ACME", norm_value: "acme", status: "pending",
  approved_key: null, current_decision_id: uid(), source: "request", created_at: "2026-01-01T00:00:00+00:00", updated_at: "2026-01-02T00:00:00+00:00", ...o });
export const decision = (o = {}) => ({ id: uid(), seq: 1, decision: "requested", from_status: null, to_status: "pending", category: "request", decided_at: "2026-01-01T00:00:00+00:00",
  decided_by_id: null, actor_label: "system:enterprise", reason: null, evidence_ref: null, policy_source: "default", policy_id: null, policy_revision: null, source: "request", ...o });
export const policy = (o = {}) => ({ id: uid(), country: "AL", sender_kind: "alphanumeric", revision: 1, allowed: true, requires_approval: true, effective_from: "2026-01-01T00:00:00+00:00",
  reason: "initial", content_hash: "abc123", created_by_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc", created_at: "2026-01-01T00:00:00+00:00", ...o });
export const effective = (o = {}) => ({ country: "AL", sender_kind: "alphanumeric", source: "default", allowed: true, requires_approval: true, policy_id: null, revision: null, effective_from: null, effective_to: null, ...o });
export const pageOf = (items, o = {}) => ({ items, limit: 50, offset: 0, has_more: false, ...o });
