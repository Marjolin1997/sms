import { api } from "../api.js";
import { Badge, Button, Card, ErrorBox, Skeleton, Stat, Time, money, useLoad } from "../ui.jsx";

const sum = (o) => Object.values(o || {}).reduce((a, b) => a + b, 0);

function StatusBars({ counts, empty }) {
  const total = sum(counts) || 1;
  const entries = Object.entries(counts || {});
  if (!entries.length) return <div className="empty">{empty}</div>;
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

function Step({ s }) {
  return (
    <a href={`#${s.link}`} className={`step ${s.done ? "done" : ""}`}>
      <span className="dot" aria-hidden>{s.done ? "✓" : ""}</span>
      <span><b>{s.title}</b>{s.optional && <span className="muted"> (optional)</span>}<small>{s.hint}</small></span>
      {!s.done && <span className="btn small">Do it</span>}
    </a>
  );
}

function Checklist({ data }) {
  if (!data) return null;
  const allDone = data.required_done === data.required_total;
  const pending = data.steps.filter((s) => !s.done);
  if (allDone && pending.length === 0) return null;
  if (allDone)
    return (
      <Card title="Nice work, the essentials are done" subtitle="These optional steps can make the platform work harder for you.">
        <div className="checklist">{pending.map((s) => <Step key={s.id} s={s} />)}</div>
      </Card>
    );
  const pct = (data.required_done / data.required_total) * 100;
  return (
    <Card title="Get started" subtitle={`${data.required_done} of ${data.required_total} required steps done`}>
      <div className="progress" aria-label="Setup progress"><div style={{ width: `${pct}%` }} /></div>
      <div className="checklist">{data.steps.map((s) => <Step key={s.id} s={s} />)}</div>
    </Card>
  );
}

function StaffHealth({ stats }) {
  return (
    <Card title="Platform health" subtitle="Refreshes every 10 seconds">
      <div className="grid stats">
        <Stat label="Oldest waiting message" value={stats.oldest_queued_age_seconds == null ? "none waiting" : `${stats.oldest_queued_age_seconds}s`} tone={stats.oldest_queued_age_seconds > 60 ? "bad" : "good"} sub="Should stay near zero" />
        <Stat label="Possibly stuck" value={stats.stuck_sending} tone={stats.stuck_sending ? "bad" : "good"} sub="Sending for over 10 min" />
        <Stat label="Delivery-report problems (24h)" value={sum(stats.dlr_problems_24h)} tone={sum(stats.dlr_problems_24h) ? "warn" : "good"} />
        {stats.switches.map((s) => (
          <Stat key={s.name} label={`Switch: ${s.name}`} value={s.enabled ? "ON" : "PAUSED"} tone={s.enabled ? "good" : "bad"} sub={s.reason} />
        ))}
      </div>
    </Card>
  );
}

export default function Dashboard({ me, owner }) {
  const isStaff = !me.owner_ref;
  const ov = useLoad(() => (owner ? api.get("/v1/portal/overview") : Promise.resolve(null)), [owner], 10000);
  const onb = useLoad(() => (owner && me.permissions.includes("portal:read") || owner && me.permissions.includes("*") ? api.get("/v1/portal/onboarding") : Promise.resolve(null)), [owner], 15000);
  const recent = useLoad(() => (owner ? api.get("/v1/messages", { limit: 6 }).catch(() => null) : Promise.resolve(null)), [owner], 10000);
  const stats = useLoad(() => (isStaff && (me.permissions.includes("monitor:read") || me.permissions.includes("*")) ? api.get("/v1/admin/stats") : Promise.resolve(null)), [], 10000);
  const o = ov.data;
  const lowBalance = o && o.wallets.some((w) => Number(w.available) < 5);

  return (
    <>
      <ErrorBox error={ov.error || stats.error} retry={ov.reload} />
      {stats.data && <StaffHealth stats={stats.data} />}
      {isStaff && !owner && <div className="help">Pick an account in the top-right to see its overview. The <b>Staff</b> section in the menu works without one.</div>}
      {owner && ov.loading && !o && <Skeleton rows={4} />}
      {lowBalance && (
        <div className="alert warn"><span>Your wallet balance is running low. SMS stops sending when it reaches zero.</span><a className="btn small primary" href="#wallet">Top up</a></div>
      )}
      {onb.data && !(onb.data.required_done === onb.data.required_total && isStaff) && <Checklist data={onb.data} />}
      {o && (
        <>
          <div className="grid stats">
            {o.wallets.length === 0 && <Stat label="Wallet" value="none yet" tone="warn" sub="Ask us to create one" />}
            {o.wallets.map((w) => (
              <Stat key={w.id} label={`Balance ${w.currency}`} value={money(w.available)} sub={Number(w.held) ? `${money(w.held)} reserved for messages in flight` : "Nothing in flight"} tone={Number(w.available) < 5 ? "warn" : "good"} />
            ))}
            <Stat label="SMS, last 30 days" value={sum(o.sms_last_30d)} />
            <Stat label="Emails, last 30 days" value={sum(o.email_last_30d)} />
            <Stat label="Campaigns" value={sum(o.campaigns)} sub={`${o.campaigns.running || 0} running now`} />
            <Stat label="Webhooks" value={o.webhooks.active_endpoints} sub={o.webhooks.failed_deliveries_24h ? `${o.webhooks.failed_deliveries_24h} failed today` : "All healthy"} tone={o.webhooks.failed_deliveries_24h ? "warn" : ""} />
          </div>
          <div className="grid two">
            <Card title="SMS by outcome" subtitle="Last 30 days"><StatusBars counts={o.sms_last_30d} empty="No SMS yet. Send your first message from the Send page." /></Card>
            <Card title="Email by outcome" subtitle="Last 30 days"><StatusBars counts={o.email_last_30d} empty="No emails yet." /></Card>
          </div>
        </>
      )}
      {recent.data && recent.data.items.length > 0 && (
        <Card title="Latest messages" actions={<a className="btn small" href="#messages">See all</a>}>
          <div className="table-wrap"><table><tbody>
            {recent.data.items.map((m) => (
              <tr key={m.id}><td><b>+{m.to}</b></td><td><Badge>{m.status}</Badge></td><td className="muted">{m.error_code || ""}</td><td className="num"><Time value={m.created_at} /></td></tr>
            ))}
          </tbody></table></div>
        </Card>
      )}
      {!isStaff && !o && !ov.loading && <Button onClick={ov.reload}>Reload</Button>}
    </>
  );
}
