import { useRef, useState } from "react";
import { api } from "./../api.js";
import { T, t } from "../i18n.jsx";
import { Button, DetailList, DetailRow, ErrorBox, Field, Modal, StatusBadge, Time, useUi } from "../ui.jsx";
import { adaptEffective, adaptPolicyChange } from "./model.js";
import { buildHash } from "../routes.js";

// Rregull: ky skedar shfaq përgjigje të serverit dhe dërgon qëllime. Nuk llogarit politikë efektive, përshtatshmëri miratimi,
// pronësi unike ose a revokon një politikë sendera; serveri është autoritet dhe kthen 409/422 për çdo shkelje.

export const DECISION_LABEL = {
  requested: T("Requested"), approved: T("Approved"), rejected: T("Rejected"), revoked: T("Revoked"), resubmitted: T("Resubmitted"),
};
export const decisionLabel = (d) => (DECISION_LABEL[d] ? t(DECISION_LABEL[d]) : d);

export const yesNo = (v) => (v ? t("Yes") : t("No"));
export const shortId = (id) => (id ? `${id.slice(0, 8)}…` : "-");

/** Aktori i një vendimi: njeri (etiketa/ID) ose sistem (politika). Nuk e bën të duket si vendim njerëzor. */
export function actorText(d) {
  if (d.system) return d.actor === "system:sender-policy" || !d.actor ? t("System (sender policy)") : t("System ({name})", { name: d.actor });
  return d.actor ? t("Admin {id}", { id: shortId(d.actor) }) : t("Admin");
}

const CATEGORY_NOTE = {
  policy_auto_approved: T("Approved automatically because the policy doesn't require approval."),
  policy_denied: T("Rejected automatically because the policy doesn't allow this sender kind in this country."),
  policy_revoked: T("Revoked automatically by a policy change."),
};

/** Shënim i shkurtër për vendimin AKTUAL kur është automatik (kategoria vjen nga serveri). */
export function AutoNote({ decision }) {
  const key = decision && CATEGORY_NOTE[decision.category];
  return key ? <div className="help" role="status">{t(key)}</div> : null;
}

export function EnterpriseRef({ id }) {
  if (!id) return "-";
  return <a className="mono" href={buildHash("senders", [], { enterprise_id: id })} title={id} aria-label={t("Senders of enterprise {id}", { id })}>{shortId(id)}</a>;
}

/** Përmbledhje e politikës efektive NGA SERVERI. `default` = politikë virtuale (nuk ruhet në DB). */
export function EffectiveSummary({ eff }) {
  return (
    <>
      {eff.source === "default" && <div className="help" role="status"><b>{t("Default policy")}</b> {t("No explicit policy exists for this country and kind. The platform default applies and nothing is stored.")}</div>}
      <DetailList>
        <DetailRow label={t("Source")}><StatusBadge>{eff.source}</StatusBadge></DetailRow>
        <DetailRow label={t("Allowed")}><StatusBadge>{eff.allowed ? "allowed" : "denied"}</StatusBadge></DetailRow>
        <DetailRow label={t("Requires approval")}>{yesNo(eff.requiresApproval)}</DetailRow>
        <DetailRow label={t("Revision")}>{eff.revision}</DetailRow>
        <DetailRow label={t("Effective from")}>{eff.effectiveFrom ? <Time value={eff.effectiveFrom} /> : null}</DetailRow>
        <DetailRow label={t("Effective to")}>{eff.effectiveTo ? <Time value={eff.effectiveTo} /> : null}</DetailRow>
      </DetailList>
    </>
  );
}

export async function fetchEffective(country, kind, signal) {
  return adaptEffective(await api.get("/admin/sender-policies/effective", { country, sender_kind: kind }, { signal }));
}

export const ACTION_LABEL = { approve: T("Approve"), reject: T("Reject"), revoke: T("Revoke"), resubmit: T("Resubmit") };
export const ACTION_ARIA = { approve: T("Approve {sender}"), reject: T("Reject {sender}"), revoke: T("Revoke {sender}"), resubmit: T("Resubmit {sender}") };

const ACTION_COPY = {
  approve: { title: T("Approve sender ID"), confirm: T("Approve"), body: T("Approving makes this sender ID usable once the change reaches the account's system."), reason: false },
  reject: { title: T("Reject sender ID"), confirm: T("Reject"), body: T("The account is told why. Be specific so they can fix it."), reason: true },
  revoke: { title: T("Revoke sender ID"), confirm: T("Revoke"), body: T("Future sends with this sender ID stop being authorized once the change reaches the account's system. Messages already handed to a provider can't be recalled."), reason: true },
  resubmit: { title: T("Resubmit sender ID"), confirm: T("Resubmit"), body: T("This puts the sender ID back in the review queue. The current policy is applied again and may approve or reject it automatically."), reason: false },
};

/** Dialog për approve/reject/revoke/resubmit. Reason i detyrueshëm vetëm për reject/revoke (si skema e serverit). Pa ndryshim optimist. */
export function DecisionDialog({ action, sender, onClose, onDone, onConflict }) {
  const copy = ACTION_COPY[action];
  const { toast } = useUi();
  const [reason, setReason] = useState("");
  const [evidence, setEvidence] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const reasonRef = useRef(null);
  const evidenceRef = useRef(null);
  const fe = (name) => error?.fields?.[name] || null;
  const global = error && !Object.keys(error.fields || {}).length ? error : null;
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setError(null);
    const body = {};
    if (copy.reason) body.reason = reason.trim();
    if (evidence.trim()) body.evidence_ref = evidence.trim();
    try {
      await api.post(`/admin/senders/${sender.id}/${action}`, body);
      toast(t("Decision saved"));
      onClose();
      await onDone();
    } catch (err) {
      setError(err);
      if (err.kind === "conflict" || err.kind === "not_found") onConflict && onConflict(); // rifresko gjendjen; dialogu mbetet me mesazhin
      else requestAnimationFrame(() => (err.fields?.reason ? reasonRef : err.fields?.evidence_ref ? evidenceRef : null)?.current?.focus());
    } finally { setBusy(false); }
  };
  return (
    <Modal title={`${t(copy.title)} — ${sender.displayValue} (${sender.country})`} onClose={onClose} busy={busy}>
      <form className="form" onSubmit={submit} noValidate>
        <p className="muted">{t(copy.body)}</p>
        {copy.reason && (
          <Field label={t("Reason")} error={fe("reason")}>
            <textarea data-autofocus ref={reasonRef} rows={3} maxLength={500} value={reason} aria-invalid={!!fe("reason")} onChange={(e) => setReason(e.target.value)} />
          </Field>
        )}
        <Field label={t("Evidence reference (optional)")} hint={t("A reference, not a document.")} error={fe("evidence_ref")}>
          <input ref={evidenceRef} maxLength={128} value={evidence} aria-invalid={!!fe("evidence_ref")} onChange={(e) => setEvidence(e.target.value)} />
        </Field>
        <ErrorBox error={global} />
        <div className="row end">
          <Button type="button" onClick={onClose} disabled={busy}>{t("Cancel")}</Button>
          <Button variant={action === "approve" || action === "resubmit" ? "primary" : "danger-solid"} busy={busy} disabled={copy.reason && !reason.trim()}>{t(copy.confirm)}</Button>
        </div>
      </form>
    </Modal>
  );
}

/** Revizion i ri politike. allowed=false kërkon konfirmim të qartë; vlerat dërgohen siç janë (pa detyrim nga frontend-i). */
export function PolicyDialog({ initial, onClose, onSaved }) {
  const [f, setF] = useState({ country: initial?.country || "", kind: initial?.kind || "alphanumeric", allowed: initial ? initial.allowed : true, requiresApproval: initial ? initial.requiresApproval : true, reason: "" });
  const [step, setStep] = useState("edit");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const refs = { country: useRef(null), reason: useRef(null), requires_approval: useRef(null) };
  const fe = (name) => error?.fields?.[name] || null;
  const global = error && !Object.keys(error.fields || {}).length ? error : null;
  const edit = (patch) => { setF((s) => ({ ...s, ...patch })); setError(null); setStep("edit"); };
  const send = async () => {
    setBusy(true); setError(null);
    try {
      const r = adaptPolicyChange(await api.post("/admin/sender-policies", { country: f.country.trim(), sender_kind: f.kind, allowed: f.allowed, requires_approval: f.requiresApproval, reason: f.reason.trim() }));
      onClose();
      await onSaved(r);
    } catch (err) {
      setError(err); setStep("edit");
      const first = ["country", "requires_approval", "reason"].find((k) => err.fields?.[k]);
      if (first) requestAnimationFrame(() => refs[first]?.current?.focus());
    } finally { setBusy(false); }
  };
  const submit = (e) => {
    e.preventDefault();
    if (!f.allowed && step !== "confirm") { setStep("confirm"); return; } // allowed=false: konfirmim i detyrueshëm para dërgimit
    send();
  };
  return (
    <Modal title={t("New policy revision")} onClose={onClose} busy={busy}>
      <form className="form" onSubmit={submit} noValidate>
        <div className="grid two">
          <Field label={t("Country (ISO code)")} error={fe("country")}>
            <input data-autofocus ref={refs.country} maxLength={2} value={f.country} aria-invalid={!!fe("country")} onChange={(e) => edit({ country: e.target.value })} placeholder="AL" />
          </Field>
          <Field label={t("Sender kind")}>
            <select value={f.kind} onChange={(e) => edit({ kind: e.target.value })}><option value="alphanumeric">{t("alphanumeric")}</option><option value="numeric">{t("numeric")}</option></select>
          </Field>
        </div>
        <div className="checks" role="group" aria-label={t("Rules")}>
          <label><input type="checkbox" checked={f.allowed} onChange={(e) => edit({ allowed: e.target.checked })} />{t("Allowed")}</label>
          <label><input ref={refs.requires_approval} type="checkbox" checked={f.requiresApproval} aria-invalid={!!fe("requires_approval")} onChange={(e) => edit({ requiresApproval: e.target.checked })} />{t("Requires approval")}</label>
          {fe("requires_approval") && <small className="err" role="alert">{fe("requires_approval")}</small>}
        </div>
        <Field label={t("Reason")} hint={t("Recorded in the audit log.")} error={fe("reason")}>
          <textarea ref={refs.reason} rows={3} maxLength={500} value={f.reason} aria-invalid={!!fe("reason")} onChange={(e) => edit({ reason: e.target.value })} />
        </Field>
        {step === "confirm" && (
          <div className="warnbox" role="alert">
            <b>{t("This policy change may revoke currently approved sender IDs.")}</b>
            <div>{t("Approved sender IDs for this country and kind can be revoked at once. Messages already handed to a provider can't be recalled.")}</div>
          </div>
        )}
        <ErrorBox error={global} />
        <div className="row end">
          <Button type="button" onClick={onClose} disabled={busy}>{t("Cancel")}</Button>
          <Button variant={step === "confirm" ? "danger-solid" : "primary"} busy={busy} disabled={!f.country.trim() || !f.reason.trim()}>{step === "confirm" ? t("Yes, apply this change") : t("Save revision")}</Button>
        </div>
      </form>
    </Modal>
  );
}
