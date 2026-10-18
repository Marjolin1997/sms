// Adaptues të përgjigjeve Central (/admin/senders*, /admin/sender-policies*): vetëm forma. Asnjë logjikë politike këtu.
const str = (v, d = null) => (typeof v === "string" ? v : d);
const int = (v) => (Number.isInteger(v) ? v : null);

export const SENDER_STATUSES = ["pending", "approved", "rejected", "revoked"];
export const SENDER_KINDS = ["alphanumeric", "numeric"];

export function adaptSender(raw) {
  return {
    id: raw.id, enterpriseId: str(raw.enterprise_id), externalRef: str(raw.external_ref), country: str(raw.country), kind: str(raw.sender_kind),
    displayValue: str(raw.display_value, ""), normValue: str(raw.norm_value), status: str(raw.status, "pending"), approvedKey: str(raw.approved_key),
    currentDecisionId: str(raw.current_decision_id), source: str(raw.source), createdAt: str(raw.created_at), updatedAt: str(raw.updated_at),
  };
}

/** Hyrje e historikut (/admin/senders/{id}/history), rend kronologjik nga serveri. `system` = vendim pa admin njeri (decided_by_id null). */
export function adaptDecision(raw) {
  const system = raw.decided_by_id == null;
  return {
    id: raw.id, seq: int(raw.seq), decision: str(raw.decision, ""), fromStatus: str(raw.from_status), toStatus: str(raw.to_status), category: str(raw.category),
    at: str(raw.decided_at), system, actor: str(raw.actor_label) || (system ? null : str(raw.decided_by_id)), reason: str(raw.reason), evidenceRef: str(raw.evidence_ref),
    policySource: str(raw.policy_source), policyId: str(raw.policy_id), policyRevision: int(raw.policy_revision), source: str(raw.source),
  };
}

export function adaptPolicy(raw) {
  return {
    id: raw.id, country: str(raw.country), kind: str(raw.sender_kind), revision: int(raw.revision), allowed: raw.allowed === true, requiresApproval: raw.requires_approval === true,
    effectiveFrom: str(raw.effective_from), reason: str(raw.reason), contentHash: str(raw.content_hash), createdById: str(raw.created_by_id), createdAt: str(raw.created_at),
  };
}

/** GET /admin/sender-policies/effective: `source` = explicit | default (default = politikë virtuale, nuk ruhet në DB). */
export function adaptEffective(raw) {
  return {
    country: str(raw.country), kind: str(raw.sender_kind), source: str(raw.source), allowed: raw.allowed === true, requiresApproval: raw.requires_approval === true,
    policyId: str(raw.policy_id), revision: int(raw.revision), effectiveFrom: str(raw.effective_from), effectiveTo: str(raw.effective_to),
  };
}

/** POST /admin/sender-policies → politika + created (false = identike me aktuale, s'u krijua revizion) + revoked_senders (numri nga serveri). */
export function adaptPolicyChange(raw) {
  return { policy: adaptPolicy(raw), created: raw.created === true, revokedSenders: int(raw.revoked_senders) ?? 0 };
}

export const adaptPage = (page, fn) => ({ ...page, items: page.items.map(fn) });

/**
 * AFORDANCË e UI-së (jo autorizim): cilat veprime ofrohen për një status, sipas makinës së gjendjeve të dokumentuar
 * (pending→approve|reject, approved→revoke, rejected|revoked→resubmit). Central nuk jep flamuj `can_*`; serveri vendos
 * dhe kthen 409 për çdo tranzicion të pavlefshëm, që UI e trajton duke rifreskuar.
 */
export function actionsFor(status) {
  if (status === "pending") return ["approve", "reject"];
  if (status === "approved") return ["revoke"];
  if (status === "rejected" || status === "revoked") return ["resubmit"];
  return [];
}
