import { useState } from "react";
import { api } from "../api.js";
import { T, t } from "../i18n.jsx";
import { Button, Card, ErrorBox, Skeleton, Stat, Table, dateOnly, money, useAction, useLoad } from "../ui.jsx";

const RANGES = [[7, T("Last 7 days")], [30, T("Last 30 days")], [90, T("Last 90 days")]];
const iso = (d) => d.toISOString().slice(0, 10);

export default function Reports() {
  const [days, setDays] = useState(30);
  const to = new Date();
  const from = new Date(Date.now() - (days - 1) * 86400000);
  const usage = useLoad(() => api.get("/v1/reports/usage", { from: iso(from), to: iso(to) }), [days]);
  const a = useAction();
  const d = usage.data;
  const max = d ? Math.max(1, ...d.days.map((x) => x.sms.count + x.email.count)) : 1;
  const rows = d ? [...d.days].reverse().filter((x) => x.sms.count || x.email.count).map((x) => ({ ...x, id: x.date })) : [];
  const exportCsv = (kind) => a.run(() => api.download(`/v1/reports/${kind}.csv`, { from: iso(from), to: iso(to) }, `${kind}-${iso(from)}-${iso(to)}.csv`), t("Download started"));
  return (
    <>
      <Card title={t("Usage")} subtitle={t("What you sent and what it cost. Days are in UTC.")}
        actions={<>
          <select aria-label={t("Period")} value={days} onChange={(e) => setDays(Number(e.target.value))}>{RANGES.map(([n, l]) => <option key={n} value={n}>{t(l)}</option>)}</select>
          <Button busy={a.busy} onClick={() => exportCsv("messages")}>{t("Export SMS (CSV)")}</Button>
          <Button busy={a.busy} onClick={() => exportCsv("emails")}>{t("Export emails (CSV)")}</Button>
        </>}>
        <ErrorBox error={usage.error || a.error} retry={usage.reload} />
        {!d ? <Skeleton rows={3} /> : (
          <div className="grid stats">
            <Stat label={t("SMS sent")} value={d.totals.sms.count} sub={t("{n} delivered · {m} failed", { n: d.totals.sms.delivered, m: d.totals.sms.failed })} />
            <Stat label={t("SMS cost (delivered)")} value={money(d.totals.sms.cost)} sub={t("{n} parts", { n: d.totals.sms.segments })} />
            <Stat label={t("Emails sent")} value={d.totals.email.count} sub={t("{n} delivered · {m} bounced", { n: d.totals.email.delivered, m: d.totals.email.bounced })} />
          </div>
        )}
      </Card>
      <Card title={t("By day")}>
        <Table rows={rows} loading={usage.loading} emptyTitle={t("No activity in this period")} empty={t("Messages you send will show up here.")}
          cols={[{ label: t("Day"), render: (r) => dateOnly(r.date) },
            { label: t("Volume"), render: (r) => <div className="bar" title={`${r.sms.count} SMS · ${r.email.count} email`}><div className="fill" style={{ width: `${((r.sms.count + r.email.count) / max) * 100}%` }} /></div> },
            { label: "SMS", num: true, render: (r) => r.sms.count }, { label: t("Delivered"), num: true, render: (r) => r.sms.delivered },
            { label: t("Failed"), num: true, render: (r) => r.sms.failed }, { label: t("Cost"), num: true, render: (r) => money(r.sms.cost) },
            { label: "Email", num: true, render: (r) => r.email.count }]} />
      </Card>
    </>
  );
}
