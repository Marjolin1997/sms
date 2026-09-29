import { api } from "../api.js";
import { Badge, Card, ErrorBox, Stat, Table, money, useLoad } from "../ui.jsx";

const sum = (o) => Object.values(o || {}).reduce((a, b) => a + b, 0);

function StatusBars({ counts }) {
  const total = sum(counts) || 1;
  const entries = Object.entries(counts || {});
  if (!entries.length) return <div className="empty">No traffic in the last 30 days.</div>;
  return (
    <div className="bars">
      {entries.sort((a, b) => b[1] - a[1]).map(([k, v]) => (
        <div key={k} className="bar-row">
          <Badge>{k}</Badge>
          <div className="bar"><div style={{ width: `${(v / total) * 100}%` }} className={`fill ${k}`} /></div>
          <b>{v}</b>
        </div>
      ))}
    </div>
  );
}

export default function Dashboard({ me, owner }) {
  const ov = useLoad(() => (owner ? api.get("/v1/portal/overview") : Promise.resolve(null)), [owner], 10000);
  const stats = useLoad(() => (me.permissions.includes("monitor:read") || me.permissions.includes("*") ? api.get("/v1/admin/stats") : Promise.resolve(null)), [], 10000);
  const o = ov.data;
  return (
    <>
      <ErrorBox error={ov.error || stats.error} />
      {stats.data && (
        <Card title="Platform health">
          <div className="grid stats">
            <Stat label="Queue age" value={stats.data.oldest_queued_age_seconds == null ? "empty" : `${stats.data.oldest_queued_age_seconds}s`} tone={stats.data.oldest_queued_age_seconds > 60 ? "bad" : "good"} />
            <Stat label="Stuck sending" value={stats.data.stuck_sending} tone={stats.data.stuck_sending ? "bad" : "good"} />
            <Stat label="DLR problems (24h)" value={sum(stats.data.dlr_problems_24h)} tone={sum(stats.data.dlr_problems_24h) ? "warn" : "good"} />
            {stats.data.switches.map((s) => (
              <Stat key={s.name} label={`Switch: ${s.name}`} value={s.enabled ? "ON" : "PAUSED"} tone={s.enabled ? "good" : "bad"} sub={s.reason} />
            ))}
          </div>
        </Card>
      )}
      {!owner && !stats.data && <div className="alert warn">Enter an account above to see its overview.</div>}
      {o && (
        <>
          <div className="grid stats">
            {o.wallets.length === 0 && <Stat label="Wallet" value="none" tone="warn" />}
            {o.wallets.map((w) => (
              <Stat key={w.id} label={`Balance ${w.currency}`} value={money(w.available)} sub={`${money(w.held)} reserved in flight`} tone="good" />
            ))}
            <Stat label="SMS (30d)" value={sum(o.sms_last_30d)} />
            <Stat label="Email (30d)" value={sum(o.email_last_30d)} />
            <Stat label="Campaigns" value={sum(o.campaigns)} sub={`${o.campaigns.running || 0} running`} />
            <Stat label="Webhooks" value={o.webhooks.active_endpoints} sub={`${o.webhooks.failed_deliveries_24h} failed (24h)`} tone={o.webhooks.failed_deliveries_24h ? "warn" : ""} />
          </div>
          <div className="grid two">
            <Card title="SMS by status (30 days)"><StatusBars counts={o.sms_last_30d} /></Card>
            <Card title="Email by status (30 days)"><StatusBars counts={o.email_last_30d} /></Card>
          </div>
          <Card title="Campaigns by status"><StatusBars counts={o.campaigns} /></Card>
        </>
      )}
    </>
  );
}
