// Kontrata e frontend-it për sender-at (M10). VETËM adaptues: forma e serverit → forma e UI-së.
// Frontend-i NUK vendos miratim, politikë ose autoritet: shfaq `effective_status` dhe flamujt `can_*` që llogarit serveri
// (app/services/sender_view.py) dhe dërgon veprime; serveri i rikontrollon gjithmonë.

/** Rregullat e vetme të UI-së: çfarë do të thotë çdo vlerë e serverit. Mos shtoni këtu logjikë vendimi. */
export const AUTHORITY_MODES = ["local", "shadow", "central"];
export const SYNC_STATES = ["not_applicable", "synchronized", "stale", "not_synchronized"];
// Kategoritë e driftit siç i jep serveri (sender_authority.classify_comparison); vetëm shfaqje, vlerat e panjohura kalojnë si tekst.
export const DRIFT_CATEGORIES = ["match_allowed", "match_denied", "projection_stale", "local_deny_central_allow", "local_allow_central_deny",
  "central_missing", "central_pending", "central_rejected", "central_revoked", "policy_mismatch", "sender_identity_mismatch"];
export const isDriftMismatch = (category) => !!category && !category.startsWith("match_");

/** Kodet e domain-it që faqet e sender-it duhet t'i trajtojnë veçmas (përkthimi i tyre është te api.js FRIENDLY). */
export const SENDER_DOMAIN_CODES = Object.freeze({
  frozen: "sender_authority_frozen", // shqyrtimi lokal i ngrirë: autoriteti është Central
  policyDenied: "sender_policy_denied",
  revoked: "sender_revoked",
  rejected: "sender_rejected",
  pending: "sender_pending",
  notSynchronized: "not_synchronized",
});

const str = (v, d = null) => (typeof v === "string" ? v : d);
const bool = (v) => v === true;

/**
 * Sender Enterprise (GET /v1/sender-ids, POST /v1/sender-ids, GET /v1/sender-ids/{id}).
 * Fushat M10 mungojnë tek serveri i vjetër ⇒ ranë mbrapsht në gjendje lokale të sigurt (pa të drejta të shpikura).
 */
export function adaptSender(raw) {
  const local = str(raw.status, "pending");
  const mode = str(raw.authority_mode, "local");
  return {
    id: raw.id,
    ownerRef: str(raw.owner_ref),
    country: str(raw.country),
    value: str(raw.value, ""),
    kind: str(raw.kind),
    localStatus: local,
    effectiveStatus: str(raw.effective_status, local), // ajo që shfaqet te klienti
    centralStatus: str(raw.central_status), // vetëm kur serveri e jep (admin ose autoritet central)
    syncStatus: str(raw.sync_status, "not_applicable"),
    authorityMode: mode,
    canResubmit: raw.can_resubmit === undefined ? ["rejected", "revoked"].includes(local) : bool(raw.can_resubmit),
    canReviewLocally: raw.can_review_locally === undefined ? mode !== "central" : bool(raw.can_review_locally),
    reason: str(raw.reason),
    createdAt: str(raw.created_at),
    // vetëm admin (sender:review) — mungojnë për klientin:
    drift: str(raw.drift),
    cpRevision: Number.isInteger(raw.cp_revision) ? raw.cp_revision : null,
    policyRevision: Number.isInteger(raw.policy_revision) ? raw.policy_revision : null,
    bootstrapIssues: Array.isArray(raw.bootstrap_issues) ? raw.bootstrap_issues.filter((x) => typeof x === "string") : [],
  };
}

/** Nuk ka sinkronizim të freskët: UI tregon "po verifikohet", kurrë "miratuar". */
export const isAwaitingSync = (s) => s.effectiveStatus === "not_synchronized" || s.syncStatus === "stale";

/** Sender i regjistrit Central (GET /admin/senders*). */
export function adaptRegistrySender(raw) {
  return {
    id: raw.id, enterpriseId: raw.enterprise_id, externalRef: str(raw.external_ref), country: str(raw.country), kind: str(raw.sender_kind),
    displayValue: str(raw.display_value, ""), status: str(raw.status, "pending"), source: str(raw.source),
    createdAt: str(raw.created_at), updatedAt: str(raw.updated_at),
  };
}

/** Vendim i historikut (GET /admin/senders/{id}/history) → hyrje e AuditTimeline. */
export function adaptDecision(raw) {
  return {
    id: raw.id, seq: raw.seq, at: str(raw.decided_at), status: str(raw.to_status), fromStatus: str(raw.from_status),
    title: str(raw.decision, ""), actor: str(raw.actor_label), detail: str(raw.reason), evidenceRef: str(raw.evidence_ref),
    policyRevision: Number.isInteger(raw.policy_revision) ? raw.policy_revision : null, source: str(raw.source),
  };
}

/** Politikë shteti (GET /admin/sender-policies*). `requires_approval` / `allowed` janë faktet e serverit. */
export function adaptCountryPolicy(raw) {
  return {
    id: raw.id ?? null, country: str(raw.country), kind: str(raw.sender_kind), allowed: bool(raw.allowed), requiresApproval: bool(raw.requires_approval),
    revision: Number.isInteger(raw.revision) ? raw.revision : null, source: str(raw.source), reason: str(raw.reason), effectiveFrom: str(raw.effective_from),
  };
}

/** Faqe e Central (`{items, limit, offset, has_more}`) me një adaptues për çdo element. */
export const adaptPage = (page, fn) => ({ ...page, items: page.items.map(fn) });
