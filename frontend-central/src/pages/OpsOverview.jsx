import { t } from "../i18n.jsx";
import { MetricCard } from "../ui.jsx";
import { CheckList, OpsPage, SectionCard, SnapshotFreshness, Stamp, Status, sectionLabel } from "../ops/components.jsx";
import { checkOf, section } from "../ops/model.js";
import { buildHash } from "../routes.js";

const Summary = ({ href, label, status, children }) => (
  <a className="summary-card" href={href} aria-label={`${label}: ${status}`}>
    <MetricCard label={label} value={<Status status={status} />} sub={children} />
  </a>
);

export default function OpsOverview() {
  return (
    <OpsPage>
      {(o) => {
        const sync = section(o, "sync"), req = section(o, "requests"), boot = section(o, "bootstrap"), central = section(o, "central_readiness"), ent = section(o, "enterprise_evidence");
        const shadow = checkOf(o, "shadow_evidence"), cutover = checkOf(o, "cutover_evidence"), snap = checkOf(o, "enterprise_snapshot");
        const lagCheck = checkOf(o, "enterprise_sync_lag");
        const m = o.metrics, e = m.enterprise;
        return (
          <>
            <div className="overall" role="status" aria-label={t("Overall sender governance health")}>
              <b>{t("Overall sender governance health")}</b><Status status={o.status} />
            </div>
            {o.status === "unknown" && <div className="help">{t("Some evidence is unavailable or unconfirmed (for example the Enterprise snapshot or a bootstrap run), so the overall health can't be confirmed as healthy.")}</div>}
            <div className="grid three">
              <Summary href={buildHash("ops")} label={t("Central readiness")} status={central.status}>{t("{n} senders", { n: m.registry.senders_total ?? 0 })}</Summary>
              <Summary href={buildHash("ops")} label={t("Enterprise snapshot")} status={snap?.status ?? "unknown"}>{m.snapshot.present ? (m.snapshot.state ? t(m.snapshot.state) : t("unsupported")) : t("Not received")}</Summary>
              <Summary href={buildHash("ops-sync")} label={t("Sync feed")} status={sync.status}>{e?.sync?.lag != null ? t("Lag: {n}", { n: e.sync.lag }) : `${t("Latest sequence")}: ${m.sync.last_seq ?? "-"}`}{lagCheck && lagCheck.status !== "pass" ? "" : ""}</Summary>
              <Summary href={buildHash("ops-sync")} label={t("Request intake")} status={req.status}>{e?.requests ? t("Pending: {n}", { n: e.requests.pending }) : `${t("Received in 24 h")}: ${m.requests.received_24h ?? 0}`}</Summary>
              <Summary href={buildHash("ops-bootstrap")} label={t("Bootstrap")} status={boot.status}>{m.bootstrap.latest ? t("Unresolved: {n}", { n: m.bootstrap.latest.unresolved_count }) : t("No run recorded")}</Summary>
              <Summary href={buildHash("ops-bootstrap")} label={t("Critical drift")} status={shadow?.status ?? "unknown"}>{e?.shadow ? t("Critical: {n}", { n: e.shadow.critical_total }) : t("Shadow evidence")}</Summary>
              <Summary href={buildHash("ops-bootstrap")} label={t("Cutover evidence")} status={cutover?.status ?? "unknown"}>{e?.cutover ? t("Authority: {mode}", { mode: e.cutover.authority_mode }) : t("Enterprise side")}</Summary>
            </div>
            <SectionCard title={t("Enterprise snapshot")} status={snap?.status ?? "unknown"}>
              <SnapshotFreshness meta={m.snapshot} status={snap?.status ?? "unknown"} />
              {snap && <CheckList checks={[snap]} />}
            </SectionCard>
            <SectionCard title={sectionLabel("central_readiness")} status={central.status}>
              <p className="muted">{t("Computed")}: <Stamp value={o.readinessComputedAt} /> {o.readinessCached && `(${t("cached up to {n} s", { n: o.ttl ?? 0 })})`}</p>
              <CheckList checks={central.checks} />
            </SectionCard>
            <small className="muted">{t("Enterprise evidence checks")}: {ent.checks.length}</small>
          </>
        );
      }}
    </OpsPage>
  );
}
