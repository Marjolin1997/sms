import { useState } from "react";
import { api } from "../api.js";
import { Badge, Button, Card, Field, Notice, Stat, Table, money, useAction, useLoad, when } from "../ui.jsx";

const ACTIVE = ["scheduled", "preparing", "running"];

function Detail({ id, onBack }) {
  const c = useLoad(() => api.get(`/v1/campaigns/${id}`), [id], 3000);
  const est = useLoad(() => api.get(`/v1/campaigns/${id}/estimate`).catch(() => null), [id]);
  const preRun = c.data && ["draft", "scheduled"].includes(c.data.status);
  const a = useAction();
  const d = c.data;
  if (!d) return <Card title="Campaign">{c.error ? c.error.message : "Loading…"}</Card>;
  const act = (verb, body) => a.run(async () => { await (body ? api.post(`/v1/campaigns/${id}/${verb}`, body) : api.post(`/v1/campaigns/${id}/${verb}`)); c.reload(); }, `Campaign ${verb} OK`);
  const s = d.stats;
  return (
    <>
      <Card title={`${d.name}`} actions={<><Badge>{d.status}</Badge><Button onClick={onBack}>← All campaigns</Button></>}>
        <div className="kv">
          <span>Channel</span><b>{d.channel}</b><span>Category</span><b>{d.category}</b>
          <span>Rate</span><b>{d.rate_per_minute}/min</b><span>Budget</span><b>{d.max_cost ? money(d.max_cost) : "none"}</b>
          <span>Started</span><b>{when(d.started_at)}</b><span>Completed</span><b>{when(d.completed_at)}</b>
        </div>
        {d.pause_reason && <div className="alert warn">Paused: {d.pause_reason.replaceAll("_", " ")}</div>}
        <Notice error={a.error} ok={a.ok} />
        <div className="row">
          {d.status === "draft" && <Button variant="primary" busy={a.busy} onClick={() => act("schedule", {})}>Send now</Button>}
          {d.status === "running" && <Button busy={a.busy} onClick={() => act("pause")}>Pause</Button>}
          {d.status === "paused" && <Button variant="primary" busy={a.busy} onClick={() => act("resume")}>Resume</Button>}
          {!["completed", "cancelled"].includes(d.status) && <Button variant="danger" busy={a.busy} onClick={() => confirm("Cancel this campaign? Unsent messages are stopped.") && act("cancel")}>Cancel</Button>}
        </div>
      </Card>
      <div className="grid stats">
        {preRun && est.data && <Stat label="Estimated audience" value={est.data.recipients} sub={`${est.data.excluded} excluded`} />}
        {preRun && est.data && d.channel === "sms" && <Stat label="Estimated cost" value={`${money(est.data.total)} ${est.data.currency || ""}`} />}
        {Object.entries(s.recipients).map(([k, v]) => <Stat key={k} label={`Recipients ${k}`} value={v} />)}
        <Stat label="Delivery rate" value={s.delivery_rate == null ? "-" : `${(s.delivery_rate * 100).toFixed(1)}%`} tone={s.delivery_rate > 0.9 ? "good" : ""} />
        {d.channel === "sms" && <Stat label="Cost delivered" value={money(s.cost.delivered)} sub={`${money(s.cost.in_flight)} in flight · ${money(s.cost.refunded)} refunded`} />}
      </div>
      <div className="grid two">
        <Card title="Message status">{Object.keys(s.messages).length ? Object.entries(s.messages).map(([k, v]) => <div key={k} className="kv"><span><Badge>{k}</Badge></span><b>{v}</b></div>) : <div className="empty">No messages yet.</div>}</Card>
        <Card title="Skipped recipients">{Object.keys(s.skipped_reasons).length ? Object.entries(s.skipped_reasons).map(([k, v]) => <div key={k} className="kv"><span>{k}</span><b>{v}</b></div>) : <div className="empty">None skipped.</div>}</Card>
      </div>
    </>
  );
}

function Create({ onDone }) {
  const lists = useLoad(() => api.get("/v1/lists"), []);
  const [f, setF] = useState({ name: "", channel: "sms", list_id: "", sender: "", text: "", subject: "", from_email: "", category: "marketing", max_cost: "", rate_per_minute: 300 });
  const a = useAction();
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const submit = async (e) => {
    e.preventDefault();
    const body = { name: f.name, channel: f.channel, list_id: Number(f.list_id), category: f.category, text: f.text, rate_per_minute: Number(f.rate_per_minute) };
    if (f.channel === "sms") { body.sender = f.sender; if (f.max_cost) body.max_cost = f.max_cost; }
    else { body.subject = f.subject; body.from_email = f.from_email; }
    const r = await a.run(() => api.post("/v1/campaigns", body), "Draft created");
    if (r) onDone(r.id);
  };
  return (
    <Card title="New campaign">
      <form onSubmit={submit} className="form">
        <div className="grid two">
          <Field label="Name"><input required value={f.name} onChange={set("name")} /></Field>
          <Field label="Channel"><select value={f.channel} onChange={set("channel")}><option value="sms">SMS</option><option value="email">Email</option></select></Field>
          <Field label="Audience list"><select required value={f.list_id} onChange={set("list_id")}><option value="">Choose…</option>{(lists.data || []).map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}</select></Field>
          <Field label="Category" hint="Marketing only reaches contacts with opt-in"><select value={f.category} onChange={set("category")}><option>marketing</option><option>transactional</option></select></Field>
          {f.channel === "sms" ? <Field label="Sender ID"><input required value={f.sender} onChange={set("sender")} /></Field> : <Field label="From address" hint="On a verified domain"><input required type="email" value={f.from_email} onChange={set("from_email")} /></Field>}
          <Field label="Rate (per minute)"><input type="number" min="1" max="10000" value={f.rate_per_minute} onChange={set("rate_per_minute")} /></Field>
          {f.channel === "sms" && <Field label="Budget cap" hint="Campaign pauses before exceeding it"><input placeholder="optional, e.g. 25.00" value={f.max_cost} onChange={set("max_cost")} /></Field>}
          {f.channel === "email" && <Field label="Subject"><input required value={f.subject} onChange={set("subject")} /></Field>}
        </div>
        <Field label="Message" hint={f.channel === "sms" ? "Use {{first_name}} (or any contact field) to personalize. 160 characters = 1 segment." : "Use {{first_name}} to personalize; an unsubscribe link is added automatically."}><textarea required rows={4} value={f.text} onChange={set("text")} /></Field>
        <Notice error={a.error} ok={a.ok} />
        <div className="row"><Button variant="primary" busy={a.busy}>Create draft</Button><Button type="button" onClick={() => onDone()}>Cancel</Button></div>
      </form>
    </Card>
  );
}

export default function Campaigns() {
  const [view, setView] = useState({ mode: "list" });
  const list = useLoad(() => api.get("/v1/campaigns"), [view.mode], 5000);
  if (view.mode === "new") return <Create onDone={(id) => setView(id ? { mode: "detail", id } : { mode: "list" })} />;
  if (view.mode === "detail") return <Detail id={view.id} onBack={() => setView({ mode: "list" })} />;
  const rows = (list.data || []).map((c) => ({ ...c, _onClick: () => setView({ mode: "detail", id: c.id }) }));
  return (
    <Card title="Campaigns" actions={<Button variant="primary" onClick={() => setView({ mode: "new" })}>New campaign</Button>}>
      {list.error && <Notice error={list.error} />}
      <Table rows={rows} empty="No campaigns yet. Create a contact list first, then a campaign."
        cols={[{ label: "Name", key: "name" }, { label: "Channel", render: (r) => r.channel.toUpperCase() }, { label: "Status", render: (r) => <Badge>{r.status}</Badge> },
               { label: "Category", key: "category" }, { label: "Started", render: (r) => when(r.started_at) }]} />
    </Card>
  );
}
