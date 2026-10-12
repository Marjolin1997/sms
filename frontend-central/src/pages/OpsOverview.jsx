import { t } from "../i18n.jsx";
import { MetricCard } from "../ui.jsx";
import { CheckList, OpsPage, SectionCard, Stamp, Status, sectionLabel } from "../ops/components.jsx";
import { section } from "../ops/model.js";
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
        const shadow = ent.checks.find((c) => c.code === "shadow_evidence");
        const cutover = ent.checks.find((c) => c.code === "cutover_evidence");
        const m = o.metrics;
        return (
          <>
            <div className="overall" role="status" aria-label={t("Overall sender governance health")}>
              <b>{t("Overall sender governance health")}</b><Status status={o.status} />
            </div>
            {o.status === "unknown" && <div className="help">{t("Some evidence lives only on the Enterprise side and isn't connected to Central, so the overall health can't be confirmed as healthy.")}</div>}
            <div className="grid three">
              <Summary href={buildHash("ops")} label={t("Central readiness")} status={central.status}>{t("{n} senders", { n: m.registry.senders_total ?? 0 })}</Summary>
              <Summary href={buildHash("ops-sync")} label={t("Sync feed")} status={sync.status}>{t("Latest sequence")}: {m.sync.last_seq ?? "-"}</Summary>
              <Summary href={buildHash("ops-sync")} label={t("Request intake")} status={req.status}>{t("Received in 24 h")}: {m.requests.received_24h ?? 0}</Summary>
              <Summary href={buildHash("ops-bootstrap")} label={t("Bootstrap")} status={boot.status}>{m.bootstrap.latest ? t("Unresolved: {n}", { n: m.bootstrap.latest.unresolved_count }) : t("No run recorded")}</Summary>
              <Summary href={buildHash("ops-bootstrap")} label={t("Critical drift")} status={shadow?.status ?? "unknown"}>{t("Shadow evidence")}</Summary>
              <Summary href={buildHash("ops-bootstrap")} label={t("Cutover evidence")} status={cutover?.status ?? "unknown"}>{t("Enterprise side")}</Summary>
            </div>
            <SectionCard title={sectionLabel("central_readiness")} status={central.status}>
              <p className="muted">{t("Computed")}: <Stamp value={o.readinessComputedAt} /> {o.readinessCached && `(${t("cached up to {n} s", { n: o.ttl ?? 0 })})`}</p>
              <CheckList checks={central.checks} />
            </SectionCard>
          </>
        );
      }}
    </OpsPage>
  );
}
