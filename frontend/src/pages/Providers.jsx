import { useState } from "react";
import { api } from "../api.js";
import { T, t } from "../i18n.jsx";
import { Card, ErrorBox, Stat, Table, Time, useLoad } from "../ui.jsx";

const RANGES = [[1, T("Last hour")], [24, T("Last 24 hours")], [168, T("Last 7 days")]];
const pct = (v) => (v == null ? "-" : `${(v * 100).toFixed(1)}%`);
const secs = (s) => (s == null ? "-" : s < 90 ? `${s}s` : s < 5400 ? t("{n} min", { n: Math.round(s / 60) }) : t("{n} h", { n: Math.round(s / 3600) }));

export default function Providers() {
  const [hours, setHours] = useState(24);
  const health = useLoad(() => api.get("/v1/admin/providers", { hours }), [hours], 10000);
  const unresolved = useLoad(() => api.get("/v1/admin/messages/unresolved"), [], 15000);
  const list = health.data?.providers || [];
  return (
    <>
      <div className="help"><b>{t("How to read this")}</b> {t("The delivery rate counts only finished messages (delivered or failed). If a provider's rate drops or messages stay in flight for long, pause dispatch under Admin → Kill switches or move the route to another provider under Rates & routes.")}</div>
      <ErrorBox error={health.error} retry={health.reload} />
      <Card title={t("Providers")} subtitle={t("Refreshes every 10 seconds")}
        actions={<select aria-label={t("Period")} value={hours} onChange={(e) => setHours(Number(e.target.value))}>{RANGES.map(([n, l]) => <option key={n} value={n}>{t(l)}</option>)}</select>}>
        {!health.loading && list.length === 0 && <div className="empty">{t("No messages in this period.")}</div>}
        {list.map((p) => {
          const low = p.delivery_rate != null && p.delivered + p.failed >= 20 && p.delivery_rate < 0.8;
          return (
            <div key={p.provider} className="tpl">
              <div className="row wrap"><b>{p.provider}</b>{low && <span className="alert bad">{t("Delivery rate is low")}</span>}{p.unknown_outcome > 0 && <span className="alert warn">{t("{n} with unknown outcome", { n: p.unknown_outcome })}</span>}</div>
              <div className="grid stats">
                <Stat label={t("Messages")} value={p.total} />
                <Stat label={t("Delivery rate")} value={pct(p.delivery_rate)} tone={p.delivery_rate == null ? "" : low ? "bad" : "good"} sub={t("{n} delivered · {m} failed", { n: p.delivered, m: p.failed })} />
                <Stat label={t("In flight")} value={p.in_flight} sub={p.oldest_in_flight_seconds == null ? undefined : t("oldest {age}", { age: secs(p.oldest_in_flight_seconds) })} tone={p.oldest_in_flight_seconds > 600 ? "warn" : ""} />
                <Stat label={t("Last delivered")} value={p.last_delivered_at ? <Time value={p.last_delivered_at} /> : "-"} />
              </div>
              {p.top_errors.length > 0 && <Table rows={p.top_errors.map((e) => ({ ...e, id: e.code }))} cols={[{ label: t("Top errors"), render: (e) => <code>{e.code}</code> }, { label: t("Count"), num: true, key: "count" }]} />}
            </div>
          );
        })}
      </Card>
      <Card title={t("Messages with unknown outcome")} subtitle={t("The connection dropped after the request may have reached the provider, so we did not retry (to avoid sending twice). The customer was not charged. Compare with the provider's dashboard.")}>
        <ErrorBox error={unresolved.error} retry={unresolved.reload} />
        <Table rows={(unresolved.data || []).map((r) => ({ ...r }))} loading={unresolved.loading} emptyTitle={t("Nothing to reconcile")} empty={t("No messages with unknown outcome.")}
          cols={[{ label: t("Customer"), render: (r) => <b>{r.owner_ref}</b> }, { label: t("Provider"), key: "provider" }, { label: t("To"), key: "to" }, { label: t("From"), key: "sender" }, { label: t("Cost"), render: (r) => `${r.total_price} ${r.currency}` }, { label: t("Created"), render: (r) => <Time value={r.created_at} /> }, { label: t("Message ID"), render: (r) => <code>{r.id}</code> }]} />
      </Card>
    </>
  );
}
