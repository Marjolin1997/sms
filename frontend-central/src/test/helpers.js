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

// ---- Operations + Enterprise snapshot (M11-D2): formë e njëjtë me përgjigjen e backend-it pas bashkimit ----
export const eChk = (code, status = "pass", summary = "ok", details = {}) => ({ code, status, summary, details, source: "enterprise" });
export const ENT = () => ({
  sync: { enabled: true, initialized: true, cursor_seq: 40, latest_central_seq: 45, lag: 5, last_success_age_seconds: 30, snapshot_age_seconds: 600, failure_count: 2, gap_recoveries: 1, drift_repairs: 0, policy_count: 2, authorization_count: 5, withdrawn_count: 1, projection_stale: false, last_error_age_seconds: null },
  requests: { enabled: true, pending: 3, retry: 2, sending: 1, failed: 4, sent: 90, stuck_sending: 0, oldest_open_age_seconds: 600, last_sent_age_seconds: 45, attempts_max: 3, failed_by_category: { http_422: 3, transport: 1 } },
  shadow: { mode: "shadow", window_hours: null, min_samples: 100, comparisons_total: 250, match_total: 240, match_rate: 0.96, critical_total: 7, local_allow_central_deny: 4, local_deny_central_allow: 2, central_missing: 3, policy_mismatch: 1, identity_mismatch: 2, projection_stale: 0, by_category: { match_allowed: 240, central_missing: 3 } },
  bootstrap: { state: { completed: true, version: 1, completed_at: NOW, tenant_count: 2, sender_count: 10, unresolved_count: 2, report_hash: "abcdef012345" }, issues: { open: 3, resolved: 1, open_by_category: { identity_conflict: 2, brand_new_category: 1 } } },
  cutover: { authority_mode: "shadow", dispatch_recheck: true, ack_configured: true, ack_valid: false, evidence_count: 2, latest_evidence: { kind: "pre_cutover", hash: "0123456789ab", readiness_status: "WARN", created_at: NOW, authority_version: 1 } },
  alerts: { items: [], evaluated_age_seconds: 3 },
});
export const META = (o = {}) => ({ present: true, supported: true, schema_version: "sender.ops.v1", generated_at: NOW, received_at: NOW, state: "fresh", age_seconds: 12, stale_after_seconds: 300, deployments: 1, reporter: "ent-prod", source_revision: "8c820ff", ...o });
export const replaceChecks = (body, code, checks) => ({ ...body, sections: body.sections.map((s) => (s.code === code ? { ...s, checks: [...s.checks.filter((c) => !checks.some((n) => n.code === c.code)), ...checks] } : s)) });
export const withEnterprise = (base, { state = "fresh", ent = ENT(), meta = {}, status } = {}) => {
  let b = { ...base, metrics: { ...base.metrics, enterprise: ent, enterprise_snapshot: META({ state, age_seconds: state === "fresh" ? 12 : 900, ...meta }) } };
  const snapStatus = state === "fresh" ? "pass" : state === "stale" ? "warn" : "fail";
  const keep = (s) => (state === "fresh" ? s : s === "pass" ? "unknown" : s);
  b = replaceChecks(b, "enterprise_evidence", [
    eChk("enterprise_snapshot", snapStatus, state === "fresh" ? "The Enterprise snapshot is fresh." : "The Enterprise snapshot is stale.", { age_seconds: b.metrics.enterprise_snapshot.age_seconds }),
    eChk("shadow_evidence", keep("fail"), "7 critical drift comparison(s).", { critical_total: 7 }),
    eChk("cutover_evidence", keep("warn"), "The configured cutover acknowledgement is not valid.", { ack_valid: false }),
    eChk("enterprise_alerts", keep("pass"), "0 critical, 0 warning, 11 ok."),
  ]);
  b = replaceChecks(b, "sync", [eChk("enterprise_sync_runtime", keep("pass"), "Enterprise cursor 40 of Central 45 (lag 5).", { cursor_seq: 40, lag: 5 }), eChk("enterprise_sync_lag", keep("pass"), "sender_sync_lag: value=5, threshold=1000", { value: 5, threshold: 1000 })]);
  b = replaceChecks(b, "requests", [eChk("enterprise_request_outbox", keep("fail"), "pending 3, retry 2, sending 1, failed 4.", { pending: 3, failed: 4 })]);
  b = replaceChecks(b, "bootstrap", [eChk("bootstrap_issue_categories", keep("warn"), "3 open and 1 resolved bootstrap issue(s).", { open: 3, open_identity_conflict: 2, open_other: 1 }), eChk("bootstrap_evidence_agreement", keep("warn"), "Central and Enterprise bootstrap evidence disagree.", { central_senders: 10, enterprise_senders: 12 })]);
  const sectionStatus = (s) => ["fail", "warn", "unknown", "pass"].find((x) => s.checks.some((c) => c.status === x)) || "unknown";
  b = { ...b, sections: b.sections.map((s) => ({ ...s, status: sectionStatus(s) })) };
  b.status = status || ["fail", "warn", "unknown", "pass"].find((x) => b.sections.some((s) => s.status === x));
  b.alerts = [
    ...(b.alerts || []),
    { code: "sender_shadow_critical_drift", severity: "critical", check: "shadow_evidence", section: "enterprise_evidence", source: "enterprise", summary: "7 critical drift comparison(s).", observed_at: NOW },
    { code: "bootstrap_evidence_agreement", severity: "warning", check: "bootstrap_evidence_agreement", section: "bootstrap", source: "enterprise", summary: "Central and Enterprise bootstrap evidence disagree.", observed_at: NOW },
  ];
  return b;
};
