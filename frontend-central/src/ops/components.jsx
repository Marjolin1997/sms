import { api } from "../api.js";
import { T, locale, t } from "../i18n.jsx";
import { Button, Card, DetailList, DetailRow, ErrorState, Skeleton, StatusBadge, ago, useLoad } from "../ui.jsx";
import { adaptOps } from "./model.js";

// Frontend-i shfaq SA vijnë nga serveri: statuset, përmbledhjet, detajet e kufizuara. Etiketat dhe këshillat janë tekst statik sipas kodit të kontrollit.
export const POLL_MS = 45000; // polling i kufizuar; useLoad e ndalon kur skeda është e fshehur dhe anulon kërkesat e vjetra

export const CHECKS = {
  policy_revisions_coherent: { label: T("Policy revisions are coherent"), action: T("Inspect the policy revisions of the listed country and kind; do not edit rows by hand.") },
  registry_states_valid: { label: T("Registry states are valid"), action: T("Investigate the sender rows with an inconsistent approved key before any cutover.") },
  approved_key_unique: { label: T("Approved keys are unique"), action: T("Two senders hold the same approved key; resolve the conflict before continuing.") },
  registry_has_decision_history: { label: T("Every sender has a decision history"), action: T("Senders without decisions indicate a failed import; inspect the registry.") },
  current_decision_matches_state: { label: T("Current decision matches state"), action: T("Registry and decision history disagree; stop and investigate.") },
  decision_chain_valid: { label: T("Decision chains follow the state machine"), action: T("A decision chain is invalid; inspect the history of the affected senders.") },
  approved_has_policy_provenance: { label: T("Approved senders have policy provenance"), action: T("Review how the affected senders were approved.") },
  no_approved_under_disallowing_policy: { label: T("No approved sender under a disallowing policy"), action: T("A policy now denies senders that are still approved; they need system revocation.") },
  senders_have_audit: { label: T("Senders are audited"), action: T("Audit rows are missing for some senders; investigate before relying on the history.") },
  sync_sequence_consistent: { label: T("Sync feed is consistent"), action: T("Sequence gap detected: inspect the sender-sync worker and run snapshot recovery on the Enterprise side.") },
  enterprise_sync_runtime: { label: T("Enterprise sync runtime"), action: null },
  request_ingest: { label: T("Sender requests are received from Enterprise"), action: T("Check the Enterprise sender-report worker and its credentials.") },
  enterprise_request_outbox: { label: T("Enterprise request outbox"), action: null },
  bootstrap_run: { label: T("Bootstrap run"), action: T("Inspect the bootstrap report and re-run the dry-run before applying again.") },
  bootstrap_unresolved: { label: T("Bootstrap unresolved items"), action: T("Resolve the listed categories on the Enterprise side; cutover stays blocked until none remain.") },
  bootstrap_issue_categories: { label: T("Bootstrap issue categories"), action: null },
  shadow_evidence: { label: T("Shadow comparison and drift"), action: null },
  cutover_evidence: { label: T("Cutover evidence"), action: null },
  enterprise_alerts: { label: T("Enterprise sender alerts"), action: null },
};
export const ALERTS = { readiness_fail: T("Central readiness check failed"), sync_gap: T("Sync feed gap"), bootstrap_unresolved: T("Bootstrap items unresolved"), bootstrap_failed: T("Bootstrap run failed") };
const SECTIONS = { central_readiness: T("Central readiness"), sync: T("Sync"), requests: T("Requests"), bootstrap: T("Bootstrap"), enterprise_evidence: T("Enterprise evidence") };
export const sectionLabel = (code) => (SECTIONS[code] ? t(SECTIONS[code]) : code);
export const checkLabel = (code) => (CHECKS[code] ? t(CHECKS[code].label) : code);
export const alertLabel = (code) => (ALERTS[code] ? t(ALERTS[code]) : code);

/** Ora e saktë në UTC + e përafërt relative; i njëjti format kudo. */
export function exact(v) {
  if (!v) return "-";
  return `${new Date(v).toLocaleString(locale(), { timeZone: "UTC", hour12: false, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" })} UTC`;
}
export const Stamp = ({ value }) => (value ? <time dateTime={value} title={exact(value)}>{ago(value)} · {exact(value)}</time> : <span>-</span>);

/** Statusi me tekst + simbol + ngjyrë (pass/warn/fail/unknown). UNKNOWN nuk shfaqet kurrë si PASS. */
export const Status = ({ status }) => <StatusBadge>{status}</StatusBadge>;

export function useSenderOps() {
  const r = useLoad(async (signal) => adaptOps(await api.get("/admin/operations/sender", {}, { signal })), [], POLL_MS);
  return r;
}

/** Guaska e faqeve: ngarkim, gabim (me të dhëna të vjetra kur ka), rifreskim manual dhe vula e kohës. */
export function OpsPage({ children }) {
  const r = useSenderOps();
  if (r.error && !r.data) return <ErrorState error={r.error} retry={r.reload} />;
  if (!r.data) return <Skeleton rows={5} />;
  const o = r.data;
  return (
    <>
      <div className="row wrap ops-bar">
        <span className="muted">{t("Generated")}: <Stamp value={o.generatedAt} /></span>
        <Button className="small" onClick={r.reload} aria-label={t("Refresh")}>{t("Refresh")}</Button>
        <small className="muted">{t("Updates automatically about every {n} s while this tab is open.", { n: Math.round(POLL_MS / 1000) })}</small>
      </div>
      {r.stale && <ErrorState stale error={r.error} retry={r.reload} />}
      {children(o)}
    </>
  );
}

const unavailable = (c) => c.status === "unknown" && c.details?.source === "enterprise";

/** Një kontroll: etiketë, status, përmbledhja e serverit, detaje të sigurta dhe veprimi i rekomanduar për WARN/FAIL. */
export function CheckRow({ check }) {
  const info = CHECKS[check.code];
  const details = Object.entries(check.details).filter(([k]) => k !== "source" && k !== "tool");
  return (
    <li className="check" data-status={check.status}>
      <div className="row wrap"><Status status={check.status} /><b>{checkLabel(check.code)}</b></div>
      <div className="muted">{unavailable(check) ? t("Not connected to Central: evidence unavailable here.") : check.summary}</div>
      {unavailable(check) && check.details.tool && <small className="muted">{t("Read it on the Enterprise host with")} <code>python -m {check.details.tool}</code></small>}
      {details.length > 0 && <small className="muted">{details.map(([k, v]) => `${k}: ${String(v)}`).join(" · ")}</small>}
      {(check.status === "warn" || check.status === "fail") && info?.action && <div className="help"><b>{t("Recommended action")}:</b> {t(info.action)}</div>}
    </li>
  );
}
export const CheckList = ({ checks }) => <ul className="checks-list">{checks.map((c) => <CheckRow key={c.code} check={c} />)}</ul>;

export function SectionCard({ title, status, children }) {
  return <Card title={<span className="row wrap">{title} <Status status={status} /></span>}>{children}</Card>;
}

export { Card, DetailList, DetailRow };
