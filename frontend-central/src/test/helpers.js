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

// ---- Operations (M11-D) ----
export const NOW = "2026-03-01T10:00:00+00:00";
export const chk = (code, status = "pass", summary = "ok", details = {}) => ({ code, status, summary, details });
export const entChk = (code, tool) => chk(code, "unknown", "Not available to Central: this evidence lives on the Enterprise side.", { source: "enterprise", tool });
export const opsBody = (o = {}) => ({
  status: "unknown", generated_at: NOW, readiness_computed_at: NOW, readiness_cached: false, readiness_ttl_seconds: 30,
  sections: [
    { code: "central_readiness", status: "pass", checks: [chk("registry_states_valid", "pass", "12 sender(s)"), chk("approved_key_unique", "pass", "unique")] },
    { code: "sync", status: "unknown", checks: [chk("sync_sequence_consistent", "pass", "Feed tail present in the outbox.", { last_seq: 7, floor_seq: 0 }), entChk("enterprise_sync_runtime", "scripts.sender_sync_readiness")] },
    { code: "requests", status: "unknown", checks: [chk("request_ingest", "pass", "Sender requests from Enterprise are being received.", { total: 3 }), entChk("enterprise_request_outbox", "scripts.sender_request_readiness")] },
    { code: "bootstrap", status: "unknown", checks: [chk("bootstrap_run", "pass", "The latest bootstrap run completed."), chk("bootstrap_unresolved", "pass", "The latest run reported no unresolved items."), entChk("bootstrap_issue_categories", "scripts.sender_bootstrap")] },
    { code: "enterprise_evidence", status: "unknown", checks: [entChk("shadow_evidence", "scripts.sender_authority_readiness"), entChk("cutover_evidence", "scripts.sender_policy_readiness"), entChk("enterprise_alerts", "scripts.sender_alerts")] },
  ],
  metrics: {
    registry: { senders_by_status: { approved: 8, pending: 4 }, senders_total: 12, policy_scopes: 2 },
    sync: { epoch: "ab12cd34", last_seq: 7, floor_seq: 0, retained_events: 7, last_event_at: NOW, oldest_retained_at: "2026-02-28T10:00:00+00:00" },
    requests: { total: 3, by_outcome: { created: 3 }, by_auto: { not_applicable: 3 }, last_received_at: NOW, received_24h: 2 },
    bootstrap: { runs: { completed: 1 }, latest: { status: "completed", started_at: NOW, completed_at: NOW, tenant_count: 2, sender_count: 10, imported_count: 7, unresolved_count: 0, artifact_hash: "aaaaaaaaaaaa", report_hash: "bbbbbbbbbbbb", source_revision: "8c820ff" } },
  },
  alerts: [],
  ...o,
});
export const withSection = (body, code, patch) => ({ ...body, sections: body.sections.map((s) => (s.code === code ? { ...s, ...patch } : s)) });
export const allPass = () => {
  const b = opsBody();
  return { ...b, status: "pass", sections: b.sections.map((s) => ({ ...s, status: "pass", checks: s.checks.map((c) => ({ ...c, status: "pass", details: {} })) })) };
};
