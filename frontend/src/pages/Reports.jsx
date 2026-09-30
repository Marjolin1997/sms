import { useState } from "react";
import { api, downloadFile } from "../api.js";
import { Button, Card, Empty, ErrorBox, Skeleton, Stat, Table, Tabs, money, useAction, useLoad } from "../ui.jsx";

const iso = (d) => d.toISOString().slice(0, 10);
const daysAgo = (n) => { const d = new Date(); d.setUTCDate(d.getUTCDate() - n); return iso(d); };
const PRESETS = [
  { id: "7", label: "Last 7 days", from: () => daysAgo(6) },
  { id: "30", label: "Last 30 days", from: () => daysAgo(29) },
  { id: "90", label: "Last 90 days", from: () => daysAgo(89) },
  { id: "custom", label: "Custom" },
];

// Ndryshimi kundrejt periudhës së mëparshme; `goodWhenUp` vendos ngjyrën (më shumë dështime = keq).
function delta(cur, prev, { pct = false, goodWhenUp = true } = {}) {
  if (cur == null || prev == null) return null;
  const diff = pct ? cur - prev : prev === 0 ? null : ((cur - prev) / prev) * 100;
  if (diff == null) return cur > 0 ? "New activity vs. previous period" : null;
  if (Math.abs(diff) < 0.05) return "Same as previous period";
  const good = diff > 0 === goodWhenUp;
  return <span className={good ? "good" : "bad"}>{diff > 0 ? "▲" : "▼"} {Math.abs(diff).toFixed(1)}{pct ? " pts" : "%"} vs. previous period</span>;
}

function Chart({ series }) {
  const max = Math.max(1, ...series.map((d) => d.total));
  const W = 720, H = 200, pad = 24, bw = (W - pad) / series.length;
  const label = `Messages per day. Total ${series.reduce((a, d) => a + d.total, 0)}.`;
  const ticks = [0, Math.ceil(max / 2), max];
  return (
    <div className="chart-wrap">
      <svg viewBox={`0 0 ${W} ${H + 20}`} role="img" aria-label={label} className="chart">
        {ticks.map((t) => {
          const y = H - (t / max) * H;
          return <g key={t}><line x1={pad} x2={W} y1={y} y2={y} className="grid-line" /><text x={pad - 4} y={y + 4} textAnchor="end" className="axis">{t}</text></g>;
        })}
        {series.map((d, i) => {
          const x = pad + i * bw + bw * 0.15, w = bw * 0.7;
          const other = d.total - d.delivered - d.failed;
          const h = (n) => (n / max) * H;
          let y = H;
          const seg = (n, cls) => { y -= h(n); return n > 0 && <rect x={x} y={y} width={w} height={h(n)} className={`seg ${cls}`} />; };
          return (
            <g key={d.date}>
              <title>{`${d.date}: ${d.total} total, ${d.delivered} delivered, ${d.failed} failed`}</title>
              <rect x={pad + i * bw} y={0} width={bw} height={H} fill="transparent" />
              {seg(d.delivered, "delivered")}{seg(other, "pending")}{seg(d.failed, "failed")}
              {(i === 0 || i === series.length - 1 || i === Math.floor(series.length / 2)) && (
                <text x={x + w / 2} y={H + 15} textAnchor={i === 0 ? "start" : i === series.length - 1 ? "end" : "middle"} className="axis">{d.date.slice(5)}</text>
              )}
            </g>
          );
        })}
      </svg>
      <div className="legend"><span><i className="sw delivered" />Delivered</span><span><i className="sw pending" />In progress</span><span><i className="sw failed" />Failed</span></div>
    </div>
  );
}

const rateText = (r) => (r == null ? "-" : `${r}%`);

function Breakdown({ title, subtitle, rows, empty, keyLabel }) {
  return (
    <Card title={title} subtitle={subtitle}>
      <Table rows={rows} empty={empty} cols={[
        { label: keyLabel, render: (r) => <b>{r.key}</b> },
        { label: "Messages", num: true, key: "total" },
        { label: "Failed", num: true, key: "failed" },
        { label: "Delivered", num: true, render: (r) => rateText(r.delivery_rate) },
      ]} />
    </Card>
  );
}

export default function Reports() {
  const [kind, setKind] = useState("sms");
  const [preset, setPreset] = useState("30");
  const [from, setFrom] = useState(daysAgo(29));
  const [to, setTo] = useState(iso(new Date()));
  const dl = useAction();
  const range = preset === "custom" ? { date_from: from, date_to: to } : { date_from: PRESETS.find((p) => p.id === preset).from(), date_to: iso(new Date()) };
  const invalid = preset === "custom" && (!from || !to || from > to);
  const r = useLoad(() => (invalid ? Promise.resolve(null) : api.get("/v1/analytics/overview", { channel: kind, ...range })), [kind, preset, from, to], 60000);
  const d = r.data;
  const sms = kind === "sms";

  const pick = (id) => {
    setPreset(id);
    if (id === "custom") return;
    const p = PRESETS.find((x) => x.id === id);
    setFrom(p.from()); setTo(iso(new Date()));
  };

  return (
    <>
      <Tabs tabs={[{ id: "sms", label: "SMS" }, { id: "email", label: "Email" }]} value={kind} onChange={setKind} />
      <Card>
        <div className="toolbar">
          <select aria-label="Period" value={preset} onChange={(e) => pick(e.target.value)}>
            {PRESETS.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
          </select>
          {preset === "custom" && (
            <>
              <input type="date" aria-label="From" value={from} max={to} onChange={(e) => setFrom(e.target.value)} />
              <input type="date" aria-label="To" value={to} min={from} max={iso(new Date())} onChange={(e) => setTo(e.target.value)} />
            </>
          )}
          <Button busy={dl.busy} disabled={invalid} onClick={() => dl.run(() => downloadFile("/v1/analytics/export.csv", { channel: kind, ...range }), "Download started")}>Download CSV</Button>
        </div>
        {invalid && <div className="alert warn">Pick a start date that is not after the end date.</div>}
        <ErrorBox error={r.error || dl.error} retry={r.reload} />
        <small className="muted">Dates use UTC. The CSV lists each message (without its text), up to 50,000 rows.</small>
      </Card>

      {r.loading && !d && <Skeleton rows={4} />}
      {d && d.current.total === 0 && (
        <Card><Empty title="No messages in this period" action={<a className="btn primary" href="#send">Send a message</a>}>Try a longer period, or send your first {sms ? "SMS" : "email"} to see results here.</Empty></Card>
      )}
      {d && d.current.total > 0 && (
        <>
          <div className="grid stats">
            <Stat label="Messages" value={d.current.total.toLocaleString()} sub={delta(d.current.total, d.previous.total)} />
            <Stat label="Delivery rate" value={rateText(d.current.delivery_rate)} tone={d.current.delivery_rate == null ? "" : d.current.delivery_rate >= 95 ? "good" : d.current.delivery_rate >= 85 ? "warn" : "bad"}
              sub={d.current.delivery_rate == null ? "Nothing finished yet" : delta(d.current.delivery_rate, d.previous.delivery_rate, { pct: true })} />
            <Stat label="Failed" value={d.current.failed.toLocaleString()} tone={d.current.failed ? "warn" : "good"} sub={delta(d.current.failed, d.previous.failed, { goodWhenUp: false })} />
            <Stat label="In progress" value={d.current.in_flight.toLocaleString()} sub="Waiting for a delivery report" />
            {sms && <Stat label="Spent" value={d.current.spend.length ? d.current.spend.map((s) => `${money(s.amount)} ${s.currency}`).join(" + ") : "0.00"} sub={`${d.current.segments.toLocaleString()} message parts. Only delivered messages are charged.`} />}
          </div>
          <Card title="Messages per day" subtitle={`${d.range.from} to ${d.range.to}`}><Chart series={d.series} /></Card>
          <div className="grid two">
            <Breakdown title="What went wrong" subtitle="Most common failure reasons" keyLabel="Reason" rows={d.top_errors} empty="No failures in this period." />
            <Breakdown title="By type" subtitle="Transactional vs. marketing" keyLabel="Type" rows={d.by_category} empty="No data." />
            {sms && <Breakdown title="By country" keyLabel="Country" rows={d.by_country} empty="No data." />}
            {sms && <Breakdown title="By sender ID" keyLabel="Sender" rows={d.by_sender} empty="No data." />}
          </div>
        </>
      )}
    </>
  );
}
