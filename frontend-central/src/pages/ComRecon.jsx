import { useState } from "react";
import { api } from "../api.js";
import { t } from "../i18n.jsx";
import { Card, Table, useLoad } from "../ui.jsx";
import { EntLink, KV, Level, Money, Note, POLL_MS, Panel, Ts, humanize } from "../commercial/components.jsx";

const SEVERITIES = ["INFO", "WARN", "FAIL", "CRITICAL"];

function Discrepancies({ min }) {
  const r = useLoad((signal) => api.get("/admin/money/reconciliation", { min_severity: min }, { signal }), [min], POLL_MS);
  return (
    <Panel title={t("Reconciliation result")} state={r} subtitle={r.data ? <>{t("Generated")}: <Ts value={r.data.generated_at} /></> : null}>
      {(d) => (
        <>
          <KV rows={[[t("Status"), <Level value={d.status} />], ...SEVERITIES.map((s) => [t(s.toLowerCase()), d.counts?.[s] ?? 0])]} />
          <h4>{t("Discrepancies")}</h4>
          <Table rows={d.discrepancies.map((x, i) => ({ ...x, id: `${x.code}-${x.subject}-${i}` }))} empty={t("No discrepancy at or above this severity.")} cols={[
            { label: t("Severity"), render: (x) => <Level value={x.severity} /> },
            { label: t("Code"), render: (x) => <b>{humanize(x.code)}</b> },
            { label: t("Enterprise"), hideSm: true, render: (x) => <EntLink id={x.enterprise_id} /> },
            { label: t("Subject"), hideSm: true, render: (x) => x.subject || "-" },
            { label: t("Expected"), hideSm: true, num: true, render: (x) => x.expected ?? "-" },
            { label: t("Reported"), hideSm: true, num: true, render: (x) => x.reported ?? "-" },
            { label: t("Detail"), key: "detail" },
          ]} />
          <h4>{t("Reconciled keys")}</h4>
          <Table rows={d.keys.map((k, i) => ({ ...k, id: `${k.enterprise_id}-${k.product_id}-${k.currency}-${i}` }))} empty={t("No usage keys reconciled yet.")} cols={[
            { label: t("Enterprise"), render: (k) => <EntLink id={k.enterprise_id} /> },
            { label: t("Currency"), key: "currency" },
            { label: t("Authority"), render: (k) => (k.authority_mode ? humanize(k.authority_mode) : "-") },
            { label: t("Report #"), hideSm: true, num: true, render: (k) => k.report_seq ?? "-" },
            { label: t("Issued by Central"), num: true, hideSm: true, render: (k) => <Money value={k.central_issued_total} currency={k.currency} /> },
            { label: t("Received by Enterprise"), num: true, hideSm: true, render: (k) => <Money value={k.enterprise_received_total} currency={k.currency} /> },
            { label: t("Available"), num: true, render: (k) => <Money value={k.available} currency={k.currency} /> },
            { label: t("Held"), num: true, hideSm: true, render: (k) => <Money value={k.held} currency={k.currency} /> },
            { label: t("Unresolved reversals"), num: true, hideSm: true, render: (k) => <Money value={k.unresolved_reversal_total} currency={k.currency} /> },
          ]} />
        </>
      )}
    </Panel>
  );
}

export default function ComRecon() {
  const [min, setMin] = useState("WARN");
  const usage = useLoad((signal) => api.get("/admin/money/usage-reports", {}, { signal }), [], POLL_MS);
  const settle = useLoad((signal) => api.get("/admin/billing/settlement", {}, { signal }), [], POLL_MS);
  const rev = useLoad((signal) => api.get("/admin/financial/unresolved-reversals", {}, { signal }), [], POLL_MS);
  return (
    <>
      <Card title={t("Minimum severity")}>
        <select aria-label={t("Minimum severity")} value={min} onChange={(e) => setMin(e.target.value)}>{SEVERITIES.map((s) => <option key={s} value={s}>{t(s.toLowerCase())}</option>)}</select>
        <Note>{t("Computed by the server over the latest usage reports. Nothing is written.")}</Note>
      </Card>
      <Discrepancies min={min} />
      <Panel title={t("Latest usage reports")} state={usage} subtitle={t("What each Enterprise last reported to Central.")}>
        {(d) => (
          <Table rows={d.items.map((r) => ({ ...r, id: r.report_id }))} empty={t("No usage report has been received yet.")} cols={[
            { label: t("Enterprise"), render: (r) => <EntLink id={r.enterprise_id} /> }, { label: t("Currency"), key: "currency" },
            { label: t("Report #"), num: true, key: "report_seq" }, { label: t("Authority"), hideSm: true, render: (r) => humanize(r.authority_mode) },
            { label: t("Available"), num: true, render: (r) => <Money value={r.available} currency={r.currency} /> },
            { label: t("Held"), num: true, hideSm: true, render: (r) => <Money value={r.held} currency={r.currency} /> },
            { label: t("Generated"), hideSm: true, render: (r) => <Ts value={r.generated_at} /> }, { label: t("Received"), render: (r) => <Ts value={r.received_at} /> },
          ]} />
        )}
      </Panel>
      <Panel title={t("Unresolved reversals")} state={rev}>
        {(d) => (
          <Table rows={d.items.map((x, i) => ({ ...x, id: `${x.grant_id}-${i}` }))} empty={t("No unresolved reversals.")} cols={[
            { label: t("Severity"), render: (x) => <Level value={x.severity} /> }, { label: t("Grant"), render: (x) => <span className="mono">{x.grant_id}</span> },
            { label: t("Enterprise"), render: (x) => <EntLink id={x.enterprise_id} /> }, { label: t("Amount"), num: true, render: (x) => <Money value={x.amount} currency={x.currency} /> },
            { label: t("Age (s)"), hideSm: true, num: true, render: (x) => x.age_seconds ?? "-" }, { label: t("Reason"), hideSm: true, render: (x) => x.reason || "-" },
          ]} />
        )}
      </Panel>
      <Panel title={t("Invoice settlement summary")} state={settle}>
        {(d) => (
          <>
            <KV rows={[
              [t("Open invoices"), d.invoices.open], [t("Paid invoices"), d.invoices.paid], [t("Void invoices"), d.invoices.void], [t("Overdue open invoices"), d.invoices.overdue_open],
              [t("Pending invoice payments"), d.invoice_payments.pending], [t("Stale pending payments"), d.invoice_payments.stale_pending],
              [t("Approved invoice payments"), d.invoice_payments.approved], [t("Rejected invoice payments"), d.invoice_payments.rejected],
            ]} />
            <h4>{t("Anomalies")}</h4>
            {Object.entries(d.anomalies || {}).filter(([, v]) => Array.isArray(v) && v.length).length === 0 ? <Note>{t("No settlement anomalies.")}</Note> : (
              <Table rows={Object.entries(d.anomalies).filter(([, v]) => v.length).map(([k, v]) => ({ id: k, kind: k, count: v.length }))} cols={[{ label: t("Anomaly"), render: (r) => humanize(r.kind) }, { label: t("Count"), num: true, key: "count" }]} />
            )}
          </>
        )}
      </Panel>
    </>
  );
}
