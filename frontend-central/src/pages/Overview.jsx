import { api } from "../api.js";
import { t } from "../i18n.jsx";
import { Card, ErrorState, MetricCard, Skeleton, useLoad } from "../ui.jsx";
import { buildHash } from "../routes.js";

const CAP = 200; // fereastra e kufizuar që jep endpoint-i; mbi të shfaqim "200+"
const count = (page) => (page.hasMore ? `${CAP}+` : String(page.items.length));

// Vetëm të dhëna që jep API-ja Central sot. Readiness i plotë i sender-ave (sync sequence, bootstrap, drift) është vetëm CLI (`sender_central_readiness`).
export default function Overview() {
  const d = useLoad(async (signal) => {
    const [pending, policies] = await Promise.all([
      api.page("/admin/senders", { status: "pending", limit: CAP }, { signal }),
      api.page("/admin/sender-policies", { latest_only: "true", limit: CAP }, { signal }),
    ]);
    return { pending, policies, denied: policies.items.filter((p) => p.allowed === false), auto: policies.items.filter((p) => p.requires_approval === false && p.allowed === true) };
  }, [], 30000);
  if (d.error && !d.data) return <ErrorState error={d.error} retry={d.reload} />;
  if (!d.data) return <Skeleton rows={3} />;
  const { pending, policies, denied, auto } = d.data;
  return (
    <>
      {d.stale && <ErrorState stale error={d.error} retry={d.reload} />}
      <div className="grid three">
        <a className="stat-link" href={buildHash("senders", [], { status: "pending" })}><MetricCard label={t("Senders waiting for review")} value={count(pending)} sub={t("Open the queue")} /></a>
        <a className="stat-link" href={buildHash("policies")}><MetricCard label={t("Explicit country policies")} value={count(policies)} sub={t("Current revisions")} /></a>
        <MetricCard label={t("Countries that deny a sender kind")} value={String(denied.length)} sub={t("Among the current policies")} tone={denied.length ? "warn" : ""} />
        <MetricCard label={t("Policies without approval")} value={String(auto.length)} sub={t("New requests are approved automatically")} />
      </div>
      <Card title={t("Readiness and drift")}>
        <p className="muted">{t("Full sender readiness (sync sequence, bootstrap issues, drift) isn't available over HTTP yet. It is produced by the Central readiness command-line tool; an HTTP view is planned.")}</p>
      </Card>
    </>
  );
}
