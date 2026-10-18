import { describe, expect, it } from "vitest";
import { adaptCountryPolicy, adaptDecision, adaptPage, adaptRegistrySender, adaptSender, isAwaitingSync, isDriftMismatch } from "../senders/model.js";

const base = { id: 7, owner_ref: "acme", country: "AL", value: "ACME", kind: "alphanumeric", status: "approved", reason: null, created_at: "2026-01-01T00:00:00Z" };

describe("adaptSender (Enterprise)", () => {
  it("maps the M10 read model one-to-one and never recomputes it", () => {
    const s = adaptSender({ ...base, status: "approved", authority_mode: "central", effective_status: "policy_denied", central_status: "approved", sync_status: "synchronized", can_resubmit: false, can_review_locally: false });
    expect(s).toMatchObject({ localStatus: "approved", effectiveStatus: "policy_denied", centralStatus: "approved", syncStatus: "synchronized", authorityMode: "central", canResubmit: false, canReviewLocally: false });
  });
  it("legacy payload without M10 fields falls back to the safe local view", () => {
    const s = adaptSender({ ...base, status: "rejected" });
    expect(s).toMatchObject({ effectiveStatus: "rejected", authorityMode: "local", syncStatus: "not_applicable", canResubmit: true, canReviewLocally: true, centralStatus: null });
  });
  it("trusts explicit server flags over local guesses", () => {
    expect(adaptSender({ ...base, status: "rejected", can_resubmit: false }).canResubmit).toBe(false);
  });
  it("customer payload has no admin diagnostics; admin payload does", () => {
    expect(adaptSender(base)).toMatchObject({ drift: null, cpRevision: null, bootstrapIssues: [] });
    const a = adaptSender({ ...base, drift: "central_missing", cp_revision: 12, policy_revision: 3, bootstrap_issues: ["conflict"] });
    expect(a).toMatchObject({ drift: "central_missing", cpRevision: 12, policyRevision: 3, bootstrapIssues: ["conflict"] });
  });
  it("not_synchronized / stale are 'awaiting sync', never approved", () => {
    expect(isAwaitingSync(adaptSender({ ...base, effective_status: "not_synchronized", sync_status: "not_synchronized" }))).toBe(true);
    expect(isAwaitingSync(adaptSender({ ...base, sync_status: "stale" }))).toBe(true);
    expect(isAwaitingSync(adaptSender({ ...base, sync_status: "synchronized" }))).toBe(false);
  });
  it("drift helper: only match_* is non-mismatch", () => {
    expect(isDriftMismatch("match_allowed")).toBe(false);
    expect(isDriftMismatch("central_missing")).toBe(true);
    expect(isDriftMismatch(null)).toBe(false);
  });
});

describe("Central adapters", () => {
  it("registry sender, decision, policy and page", () => {
    expect(adaptRegistrySender({ id: "u", enterprise_id: "e", external_ref: "sms-sender-1", country: "AL", sender_kind: "alphanumeric", display_value: "ACME", status: "approved", source: "request" }))
      .toMatchObject({ externalRef: "sms-sender-1", kind: "alphanumeric", displayValue: "ACME", status: "approved" });
    expect(adaptDecision({ id: "d", seq: 2, decision: "approve", from_status: "pending", to_status: "approved", decided_at: "t", actor_label: "ops", reason: "ok", evidence_ref: "e1", policy_revision: 4 }))
      .toMatchObject({ title: "approve", status: "approved", fromStatus: "pending", actor: "ops", detail: "ok", policyRevision: 4 });
    expect(adaptCountryPolicy({ country: "AL", sender_kind: "alphanumeric", allowed: true, requires_approval: false, revision: 2, source: "explicit" }))
      .toMatchObject({ allowed: true, requiresApproval: false, revision: 2 });
    expect(adaptPage({ items: [{ id: 1 }], hasMore: true, limit: 1, offset: 0 }, (x) => ({ k: x.id })).items).toEqual([{ k: 1 }]);
  });
  it("tolerates missing optional fields without inventing values", () => {
    expect(adaptCountryPolicy({}).allowed).toBe(false);
    expect(adaptCountryPolicy({}).requiresApproval).toBe(false);
  });
});
