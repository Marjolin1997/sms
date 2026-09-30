import { api } from "../api.js";
import { T, t } from "../i18n.jsx";
import { Badge, Button, Card, ErrorBox, Skeleton, Stat, Time, money, useLoad } from "../ui.jsx";

const sum = (o) => Object.values(o || {}).reduce((a, b) => a + b, 0);

// Tekste që vijnë nga serveri (hapat e nisjes); shënohen këtu që t'i gjejë kontrolli i përkthimeve.
const SERVER_TEXTS = [
  T("Add funds to your wallet"), T("SMS is prepaid. Top up online or ask us for a bank transfer."),
  T("Get a sender ID approved"), T("The name recipients see, e.g. your brand. We review it before you can use it."),
  T("Import your contacts"), T("Upload a list, or add people one by one."),
  T("Send your first message"), T("Try a single SMS to yourself first."),
  T("Verify an email domain"), T("Only needed if you send email."),
  T("Connect a webhook"), T("Get delivery updates pushed to your own system."),
  T("Add your billing details"), T("Needed for invoices."),
];
void SERVER_TEXTS;

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
      <span><b>{t(s.title)}</b>{s.optional && <span className="muted"> {t("(optional)")}</span>}<small>{t(s.hint)}</small></span>
      {!s.done && <span className="btn small">{t("Do it")}</span>}
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
      <Card title={t("Nice work, the essentials are done")} subtitle={t("These optional steps can make the platform work harder for you.")}>
        <div className="checklist">{pending.map((s) => <Step key={s.id} s={s} />)}</div>
      </Card>
    );
  const pct = (data.required_done / data.required_total) * 100;
  return (
    <Card title={t("Get started")} subtitle={t("{done} of {total} required steps done", { done: data.required_done, total: data.required_total })}>
      <div className="progress" aria-label={t("Setup progress")}><div style={{ width: `${pct}%` }} /></div>
      <div className="checklist">{data.steps.map((s) => <Step key={s.id} s={s} />)}</div>
    </Card>
  );
}

function StaffHealth({ stats }) {
  return (
    <Card title={t("Platform health")} subtitle={t("Refreshes every 10 seconds")}>
      <div className="grid stats">
        <Stat label={t("Oldest waiting message")} value={stats.oldest_queued_age_seconds == null ? t("none waiting") : `${stats.oldest_queued_age_seconds}s`} tone={stats.oldest_queued_age_seconds > 60 ? "bad" : "good"} sub={t("Should stay near zero")} />
        <Stat label={t("Possibly stuck")} value={stats.stuck_sending} tone={stats.stuck_sending ? "bad" : "good"} sub={t("Sending for over 10 min")} />
        <Stat label={t("Delivery-report problems (24h)")} value={sum(stats.dlr_problems_24h)} tone={sum(stats.dlr_problems_24h) ? "warn" : "good"} />
        {stats.switches.map((s) => (
          <Stat key={s.name} label={t("Switch: {name}", { name: s.name })} value={s.enabled ? t("ON") : t("PAUSED")} tone={s.enabled ? "good" : "bad"} sub={s.reason} />
        ))}
      </div>
    </Card>
  );
}

export default function Dashboard({ me, owner }) {
  const isStaff = !me.owner_ref;
  const canPortal = me.permissions.includes("portal:read") || me.permissions.includes("*");
  const ov = useLoad(() => (owner ? api.get("/v1/portal/overview") : Promise.resolve(null)), [owner], 10000);
  const onb = useLoad(() => (owner && canPortal ? api.get("/v1/portal/onboarding") : Promise.resolve(null)), [owner], 15000);
  const recent = useLoad(() => (owner ? api.get("/v1/messages", { limit: 6 }).catch(() => null) : Promise.resolve(null)), [owner], 10000);
  const stats = useLoad(() => (isStaff && (me.permissions.includes("monitor:read") || me.permissions.includes("*")) ? api.get("/v1/admin/stats") : Promise.resolve(null)), [], 10000);
  const o = ov.data;
  const lowBalance = o && o.wallets.some((w) => Number(w.available) < 5);

  return (
    <>
      <ErrorBox error={ov.error || stats.error} retry={ov.reload} />
      {stats.data && <StaffHealth stats={stats.data} />}
      {isStaff && !owner && <div className="help">{t("Pick an account in the top-right to see its overview. The Staff section in the menu works without one.")}</div>}
      {owner && ov.loading && !o && <Skeleton rows={4} />}
      {lowBalance && (
        <div className="alert warn"><span>{t("Your wallet balance is running low. SMS stops sending when it reaches zero.")}</span><a className="btn small primary" href="#wallet">{t("Top up")}</a></div>
      )}
      {onb.data && !(onb.data.required_done === onb.data.required_total && isStaff) && <Checklist data={onb.data} />}
      {o && (
        <>
          <div className="grid stats">
            {o.wallets.length === 0 && <Stat label={t("Wallet")} value={t("none yet")} tone="warn" sub={t("Ask us to create one")} />}
            {o.wallets.map((w) => (
              <Stat key={w.id} label={t("Balance {currency}", { currency: w.currency })} value={money(w.available)} sub={Number(w.held) ? t("{amount} reserved for messages in flight", { amount: money(w.held) }) : t("Nothing in flight")} tone={Number(w.available) < 5 ? "warn" : "good"} />
            ))}
            <Stat label={t("SMS, last 30 days")} value={sum(o.sms_last_30d)} />
            <Stat label={t("Emails, last 30 days")} value={sum(o.email_last_30d)} />
            <Stat label={t("Campaigns")} value={sum(o.campaigns)} sub={t("{n} running now", { n: o.campaigns.running || 0 })} />
            <Stat label={t("Webhooks")} value={o.webhooks.active_endpoints} sub={o.webhooks.failed_deliveries_24h ? t("{n} failed today", { n: o.webhooks.failed_deliveries_24h }) : t("All healthy")} tone={o.webhooks.failed_deliveries_24h ? "warn" : ""} />
          </div>
          <div className="grid two">
            <Card title={t("SMS by outcome")} subtitle={t("Last 30 days")}><StatusBars counts={o.sms_last_30d} empty={t("No SMS yet. Send your first message from the Send page.")} /></Card>
            <Card title={t("Email by outcome")} subtitle={t("Last 30 days")}><StatusBars counts={o.email_last_30d} empty={t("No emails yet.")} /></Card>
          </div>
        </>
      )}
      {recent.data && recent.data.items.length > 0 && (
        <Card title={t("Latest messages")} actions={<a className="btn small" href="#messages">{t("See all")}</a>}>
          <div className="table-wrap"><table><tbody>
            {recent.data.items.map((m) => (
              <tr key={m.id}><td><b>+{m.to}</b></td><td><Badge>{m.status}</Badge></td><td className="muted">{m.error_code || ""}</td><td className="num"><Time value={m.created_at} /></td></tr>
            ))}
          </tbody></table></div>
        </Card>
      )}
      {!isStaff && !o && !ov.loading && <Button onClick={ov.reload}>{t("Reload")}</Button>}
    </>
  );
}
