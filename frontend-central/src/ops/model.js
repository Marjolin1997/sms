// Adaptues i GET /admin/operations/sender. VETËM forma: statuset dhe pragjet vijnë nga serveri; asnjë gatishmëri nuk rindërtohet këtu.
// Çdo status i panjohur ose i munguar bëhet "unknown" (kurrë "pass").
export const STATUSES = ["pass", "warn", "fail", "unknown"];
export const normStatus = (s) => (STATUSES.includes(s) ? s : "unknown");

const primitive = (v) => v === null || ["string", "number", "boolean"].includes(typeof v);
const safeDetails = (d) => Object.fromEntries(Object.entries(d && typeof d === "object" ? d : {}).filter(([, v]) => primitive(v)));
const str = (v) => (typeof v === "string" ? v : null);
const num = (v) => (Number.isFinite(v) ? v : null);

// Freskia e snapshot-it Enterprise: gjendja (fresh|stale|expired) dhe mosha vijnë nga serveri; këtu vetëm normalizohen.
function adaptSnapshotMeta(m) {
  const present = !!(m && m.present === true);
  return {
    present, supported: present && m.supported !== false, state: ["fresh", "stale", "expired"].includes(m?.state) ? m.state : null, ageSeconds: num(m?.age_seconds), staleAfter: num(m?.stale_after_seconds),
    generatedAt: str(m?.generated_at), receivedAt: str(m?.received_at), reporter: str(m?.reporter), sourceRevision: str(m?.source_revision), deployments: num(m?.deployments) ?? 0, schemaVersion: str(m?.schema_version),
  };
}

export function adaptOps(raw) {
  const sections = {};
  for (const s of Array.isArray(raw?.sections) ? raw.sections : []) {
    sections[s.code] = {
      code: s.code, status: normStatus(s.status),
      checks: (Array.isArray(s.checks) ? s.checks : []).map((c) => ({ code: String(c.code), status: normStatus(c.status), summary: str(c.summary) || "", details: safeDetails(c.details), source: c.source === "enterprise" ? "enterprise" : "central" })),
    };
  }
  const m = raw?.metrics || {};
  return {
    status: normStatus(raw?.status), generatedAt: str(raw?.generated_at), readinessComputedAt: str(raw?.readiness_computed_at), readinessCached: raw?.readiness_cached === true,
    ttl: num(raw?.readiness_ttl_seconds), sections,
    metrics: { registry: m.registry || {}, sync: m.sync || {}, requests: m.requests || {}, bootstrap: m.bootstrap || {}, snapshot: adaptSnapshotMeta(m.enterprise_snapshot), enterprise: m.enterprise && typeof m.enterprise === "object" ? m.enterprise : null },
    alerts: (Array.isArray(raw?.alerts) ? raw.alerts : []).map((a) => ({ code: String(a.code), severity: str(a.severity) || "warning", check: str(a.check), section: str(a.section), source: a.source === "enterprise" ? "enterprise" : "central", summary: str(a.summary) || "", observedAt: str(a.observed_at) })),
  };
}

export const section = (ops, code) => ops.sections[code] || { code, status: "unknown", checks: [] };
export const checkOf = (ops, code) => Object.values(ops.sections).flatMap((s) => s.checks).find((c) => c.code === code) || null;
