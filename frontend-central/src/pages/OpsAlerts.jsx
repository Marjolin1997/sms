import { t } from "../i18n.jsx";
import { Empty, Table } from "../ui.jsx";
import { CheckList, OpsPage, SectionCard, Stamp, Status, alertLabel, checkLabel, sectionLabel } from "../ops/components.jsx";
import { section } from "../ops/model.js";

export default function OpsAlerts() {
  return (
    <OpsPage>
      {(o) => {
        const ent = section(o, "enterprise_evidence");
        const enterpriseAlerts = ent.checks.filter((c) => c.code === "enterprise_alerts");
        return (
          <>
            <SectionCard title={t("Central-side alerts")} status={o.alerts.length ? (o.alerts.some((a) => a.severity === "critical") ? "fail" : "warn") : "pass"}>
              {o.alerts.length === 0 ? <Empty title={t("No active alerts")}>{t("Every Central-side check that Central can evaluate is currently healthy.")}</Empty> : (
                <Table rows={o.alerts.map((a, i) => ({ ...a, id: `${a.code}-${i}` }))} cols={[
                  { label: t("Severity"), render: (a) => <Status status={a.severity} /> },
                  { label: t("Alert"), render: (a) => <b>{alertLabel(a.code)}</b> },
                  { label: t("Check"), hideSm: true, render: (a) => checkLabel(a.check) },
                  { label: t("Section"), hideSm: true, render: (a) => sectionLabel(a.section) },
                  { label: t("Summary"), key: "summary" },
                  { label: t("Observed"), render: (a) => <Stamp value={a.observedAt} /> },
                ]} />
              )}
            </SectionCard>
            <SectionCard title={t("Enterprise sender alerts")} status="unknown"><CheckList checks={enterpriseAlerts} /></SectionCard>
          </>
        );
      }}
    </OpsPage>
  );
}
