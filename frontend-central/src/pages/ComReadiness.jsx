import { api } from "../api.js";
import { t } from "../i18n.jsx";
import { Skeleton, Table, useLoad } from "../ui.jsx";
import { ErrorState, Level, Note, POLL_MS, Panel, Ts, humanize } from "../commercial/components.jsx";
import { level } from "../commercial/format.js";

// Gatishmëria llogaritet VETËM në server. Këtu shfaqet çdo burim me statusin e vet; asnjë status i përgjithshëm nuk shpiket në klient.
// Një burim që nuk ngarkohet shfaqet UNKNOWN (kurrë PASS).
function Source({ title, state, status, checks, extra }) {
  const unknown = !state.data;
  return (
    <Panel title={title} state={state} actions={<Level value={unknown ? "unknown" : status(state.data)} />}>
      {(d) => (
        <>
          {extra && extra(d)}
          <Table rows={checks(d).map((c, i) => ({ ...c, id: `${c.name}-${i}` }))} empty={t("The server returned no checks.")} cols={[
            { label: t("Status"), render: (c) => <Level value={c.level} /> },
            { label: t("Check"), render: (c) => <b>{humanize(c.name)}</b> },
            { label: t("Reason"), key: "reason" },
          ]} />
        </>
      )}
    </Panel>
  );
}

export default function ComReadiness() {
  const fin = useLoad((signal) => api.get("/admin/financial/readiness", {}, { signal }), [], POLL_MS);
  const pri = useLoad((signal) => api.get("/admin/pricing/readiness", {}, { signal }), [], POLL_MS);
  const bil = useLoad((signal) => api.get("/admin/billing/final-readiness", {}, { signal }), [], POLL_MS);
  const all = [fin, pri, bil];
  if (all.every((s) => s.error && !s.data)) return <ErrorState error={fin.error} retry={() => all.forEach((s) => s.reload())} />;
  if (all.every((s) => !s.data && !s.error)) return <Skeleton rows={5} />;
  return (
    <>
      <Note>{t("Readiness is computed by the server. A section that can't be loaded is shown as unknown, never as ready.")}</Note>
      <Source title={t("Financial readiness")} state={fin} status={(d) => d.status} checks={(d) => d.checks} />
      <Source title={t("Pricing readiness")} state={pri} status={(d) => (d.ok === false || d.checks.some((c) => level(c.level) === "fail") ? "fail" : d.checks.some((c) => level(c.level) === "warn") ? "warn" : d.ok === true ? "pass" : "unknown")} checks={(d) => d.checks} />
      <Source title={t("Billing readiness")} state={bil} status={(d) => d.status} checks={(d) => d.checks}
        extra={(d) => <p className="muted small">{t("Authority mode")}: <b>{humanize(d.mode)}</b> · {t("Generated")}: <Ts value={d.generated_at} /></p>} />
    </>
  );
}
