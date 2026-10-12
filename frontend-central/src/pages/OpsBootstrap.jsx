import { T, t } from "../i18n.jsx";
import { DetailList, DetailRow } from "../ui.jsx";
import { CheckList, OpsPage, SectionCard, Stamp, Status, val } from "../ops/components.jsx";
import { section } from "../ops/model.js";

// Kategoritë e njohura (të kufizuara); çdo e panjohur shfaqet si "Other / unknown".
const CATEGORIES = {
  identity_conflict: T("Identity conflict"), global_key_conflict: T("Global key conflict"), policy_denied: T("Policy denied"), invalid_legacy_identity: T("Invalid legacy identity"),
  missing_enterprise_mapping: T("Missing enterprise mapping"), local_approved_central_pending: T("Local approved, Central pending"), local_approved_central_rejected: T("Local approved, Central rejected"),
  local_approved_central_revoked: T("Local approved, Central revoked"), missing_in_central: T("Missing in Central"),
};
export function categoryRows(by) {
  const out = {};
  for (const [k, n] of Object.entries(by || {})) {
    const key = CATEGORIES[k] ? k : "other";
    out[key] = (out[key] || 0) + n;
  }
  return Object.entries(out).map(([k, n]) => [k === "other" ? t("Other / unknown") : t(CATEGORIES[k]), n]);
}
const counts = (o) => Object.entries(o || {}).map(([k, v]) => `${k}: ${v}`).join(" · ") || "-";
const pct = (r) => (r == null ? "-" : `${(r * 100).toFixed(2)} %`);

export default function OpsBootstrap() {
  return (
    <OpsPage>
      {(o) => {
        const boot = section(o, "bootstrap"), ent = section(o, "enterprise_evidence");
        const b = o.metrics.bootstrap, run = b.latest, e = o.metrics.enterprise;
        const runs = Object.entries(b.runs || {}).map(([k, v]) => `${t(k)}: ${v}`).join(" · ") || "-";
        const eb = e?.bootstrap, sh = e?.shadow, cu = e?.cutover;
        return (
          <>
            <SectionCard title={t("Latest bootstrap run (Central)")} status={boot.status}>
              {!run ? <p className="muted">{t("No bootstrap run is recorded in Central.")}</p> : (
                <DetailList>
                  <DetailRow label={t("Status")}><Status status={run.status} /></DetailRow>
                  <DetailRow label={t("Started")}><Stamp value={run.started_at} /></DetailRow>
                  <DetailRow label={t("Completed")}>{run.completed_at ? <Stamp value={run.completed_at} /> : null}</DetailRow>
                  <DetailRow label={t("Accounts scanned")}>{run.tenant_count}</DetailRow>
                  <DetailRow label={t("Senders scanned")}>{run.sender_count}</DetailRow>
                  <DetailRow label={t("Imported")}>{run.imported_count}</DetailRow>
                  <DetailRow label={t("Unresolved")}>{run.unresolved_count}</DetailRow>
                  <DetailRow label={t("Artifact hash")}><code>{run.artifact_hash}</code></DetailRow>
                  <DetailRow label={t("Report hash")}>{run.report_hash ? <code>{run.report_hash}</code> : null}</DetailRow>
                  <DetailRow label={t("Source revision")}>{run.source_revision}</DetailRow>
                </DetailList>
              )}
              <p className="muted">{t("Runs by status")}: {runs}</p>
              <h4>{t("Enterprise bootstrap (reported snapshot)")}</h4>
              {!eb ? <p className="muted">{t("Not connected to Central: evidence unavailable here.")}</p> : !eb.state ? <p className="muted">{t("No bootstrap is recorded on Enterprise.")}</p> : (
                <DetailList>
                  <DetailRow label={t("Completed")}>{val(eb.state.completed)}</DetailRow>
                  <DetailRow label={t("Completed at")}>{eb.state.completed_at ? <Stamp value={eb.state.completed_at} /> : null}</DetailRow>
                  <DetailRow label={t("Accounts scanned")}>{eb.state.tenant_count}</DetailRow>
                  <DetailRow label={t("Senders scanned")}>{eb.state.sender_count}</DetailRow>
                  <DetailRow label={t("Unresolved")}>{eb.state.unresolved_count}</DetailRow>
                  <DetailRow label={t("Report hash")}>{eb.state.report_hash ? <code>{eb.state.report_hash}</code> : null}</DetailRow>
                  <DetailRow label={t("Open issues")}>{eb.issues.open}</DetailRow>
                  <DetailRow label={t("Resolved issues")}>{eb.issues.resolved}</DetailRow>
                </DetailList>
              )}
              {eb && eb.issues.open > 0 && (
                <>
                  <h4>{t("Open issues by category")}</h4>
                  <ul className="checks-list">{categoryRows(eb.issues.open_by_category).map(([label, n]) => <li key={label}><b>{label}</b>: {n}</li>)}</ul>
                </>
              )}
              <CheckList checks={boot.checks} />
            </SectionCard>
            <SectionCard title={t("Shadow, drift and cutover")} status={ent.status}>
              <h4>{t("Shadow comparison (reported snapshot)")}</h4>
              {!sh ? <p className="muted">{t("Not connected to Central: evidence unavailable here.")}</p> : (
                <DetailList>
                  <DetailRow label={t("Authority mode")}>{sh.mode}</DetailRow>
                  <DetailRow label={t("Sample progress")}>{t("{n} of {min} required", { n: sh.comparisons_total, min: sh.min_samples })}</DetailRow>
                  <DetailRow label={t("Match rate")}>{pct(sh.match_rate)}</DetailRow>
                  <DetailRow label={t("Critical drift")}>{sh.critical_total}</DetailRow>
                  <DetailRow label={t("Central missing")}>{sh.central_missing}</DetailRow>
                  <DetailRow label={t("Local allow, Central deny")}>{sh.local_allow_central_deny}</DetailRow>
                  <DetailRow label={t("Local deny, Central allow")}>{sh.local_deny_central_allow}</DetailRow>
                  <DetailRow label={t("Policy mismatch")}>{sh.policy_mismatch}</DetailRow>
                  <DetailRow label={t("Identity mismatch")}>{sh.identity_mismatch}</DetailRow>
                  <DetailRow label={t("Projection stale")}>{sh.projection_stale}</DetailRow>
                  <DetailRow label={t("By category")}>{counts(sh.by_category)}</DetailRow>
                </DetailList>
              )}
              <h4>{t("Cutover (reported snapshot)")}</h4>
              {!cu ? <p className="muted">{t("Not connected to Central: evidence unavailable here.")}</p> : (
                <DetailList>
                  <DetailRow label={t("Current authority")}>{cu.authority_mode}</DetailRow>
                  <DetailRow label={t("Dispatch recheck")}>{val(cu.dispatch_recheck)}</DetailRow>
                  <DetailRow label={t("Acknowledgement configured")}>{val(cu.ack_configured)}</DetailRow>
                  <DetailRow label={t("Acknowledgement valid")}>{val(cu.ack_valid)}</DetailRow>
                  <DetailRow label={t("Evidence records")}>{cu.evidence_count}</DetailRow>
                  <DetailRow label={t("Latest evidence")}>{cu.latest_evidence ? <><code>{cu.latest_evidence.hash}</code> · {cu.latest_evidence.kind} · <Status status={cu.latest_evidence.readiness_status.toLowerCase()} /></> : null}</DetailRow>
                </DetailList>
              )}
              <p className="muted">{t("These come from the Enterprise snapshot. No change can be made from here.")}</p>
              <CheckList checks={ent.checks} />
            </SectionCard>
          </>
        );
      }}
    </OpsPage>
  );
}
