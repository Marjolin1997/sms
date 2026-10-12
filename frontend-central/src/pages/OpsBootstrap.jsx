import { t } from "../i18n.jsx";
import { DetailList, DetailRow } from "../ui.jsx";
import { CheckList, OpsPage, SectionCard, Stamp, Status } from "../ops/components.jsx";
import { section } from "../ops/model.js";

export default function OpsBootstrap() {
  return (
    <OpsPage>
      {(o) => {
        const boot = section(o, "bootstrap"), ent = section(o, "enterprise_evidence");
        const b = o.metrics.bootstrap, run = b.latest;
        const runs = Object.entries(b.runs || {}).map(([k, v]) => `${t(k)}: ${v}`).join(" · ") || "-";
        return (
          <>
            <SectionCard title={t("Latest bootstrap run")} status={boot.status}>
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
              <CheckList checks={boot.checks} />
            </SectionCard>
            <SectionCard title={t("Shadow, drift and cutover")} status={ent.status}>
              <p className="muted">{t("These come from the Enterprise runtime and aren't connected to Central. No change can be made from here.")}</p>
              <CheckList checks={ent.checks} />
            </SectionCard>
          </>
        );
      }}
    </OpsPage>
  );
}
