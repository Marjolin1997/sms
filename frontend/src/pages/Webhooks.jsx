import { useState } from "react";
import { api } from "../api.js";
import { Badge, Button, Card, ErrorBox, Field, SecretBanner, Table, Tabs, Time, useAction, useLoad, useUi } from "../ui.jsx";

const TYPES = [["*", "Everything"], ["message.*", "SMS updates"], ["email.*", "Email updates"], ["campaign.*", "Campaign progress"], ["consent.*", "Opt-ins and opt-outs"], ["invoice.*", "Invoices"], ["payment.*", "Payments"]];

function Endpoints() {
  const { confirm } = useUi();
  const eps = useLoad(() => api.get("/v1/webhooks/endpoints"), []);
  const [url, setUrl] = useState("");
  const [types, setTypes] = useState(["*"]);
  const [secret, setSecret] = useState(null);
  const a = useAction(), b = useAction();
  const httpBad = url && !/^https:\/\//i.test(url);
  return (
    <>
      {secret && <SecretBanner title="Your signing secret" note="Use it to check that each request really came from us. It's shown only once. If you lose it, rotate it." value={secret} onClose={() => setSecret(null)} />}
      <div className="help"><b>What's a webhook?</b> When something happens (an SMS is delivered, an email bounces) we send an HTTPS request to your server so you don't have to keep asking. Give us a public HTTPS address; we sign each request so you can trust it.</div>
      <Card title="Add an endpoint">
        <div className="form">
          <Field label="Address to call" hint="Must be public and start with https://" error={httpBad ? "Use an https:// address" : null}><input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://example.com/hooks/sms" /></Field>
          <Field label="Send me…"><div className="row wrap">{TYPES.map(([t, l]) => <label key={t} className="chip"><input type="checkbox" checked={types.includes(t)} onChange={(e) => setTypes(e.target.checked ? [...types.filter((x) => t === "*" ? false : x !== "*"), t] : types.filter((x) => x !== t))} />{l}</label>)}</div></Field>
          <ErrorBox error={a.error} />
          <div><Button variant="primary" busy={a.busy} disabled={!url || httpBad || !types.length} onClick={async () => { const r = await a.run(() => api.post("/v1/webhooks/endpoints", { url, event_types: types }), "Endpoint created"); if (r && r.secret) { setSecret(r.secret); setUrl(""); eps.reload(); } }}>Create endpoint</Button></div>
        </div>
      </Card>
      <Card title="Your endpoints">
        <ErrorBox error={b.error || eps.error} retry={eps.reload} />
        <Table rows={eps.data || []} loading={eps.loading} emptyTitle="No endpoints yet" empty="Add one above to start receiving updates." cols={[
          { label: "Address", render: (r) => <code>{r.url}</code> }, { label: "Events", render: (r) => r.event_types.join(", ") },
          { label: "Status", render: (r) => <><Badge>{r.status}</Badge>{r.disabled_reason && <small className="muted"> {{ too_many_failures: "turned off after repeated failures", gone: "your server said it's gone (410)", unsafe_url: "address isn't allowed", manual: "turned off by you" }[r.disabled_reason] || r.disabled_reason}</small>}</> },
          { label: "", render: (r) => (
            <div className="row wrap">
              <Button className="small" busy={b.busy} onClick={() => b.run(() => api.post(`/v1/webhooks/endpoints/${r.id}/test`), "Test event sent. See Recent deliveries.")}>Send test</Button>
              {r.status === "disabled" && <Button className="small" variant="primary" busy={b.busy} onClick={async () => { await b.run(() => api.patch(`/v1/webhooks/endpoints/${r.id}`, { enabled: true }), "Turned back on"); eps.reload(); }}>Turn on</Button>}
              <Button className="small" busy={b.busy} onClick={async () => { if (await confirm({ title: "Rotate the secret?", body: "The old secret stops working straight away. Update your server with the new one.", confirmLabel: "Rotate" })) { const x = await b.run(() => api.post(`/v1/webhooks/endpoints/${r.id}/rotate-secret`), "Secret rotated"); if (x && x.secret) setSecret(x.secret); } }}>Rotate secret</Button>
              <Button className="small" variant="danger" busy={b.busy} onClick={async () => { if (await confirm({ title: "Delete this endpoint?", body: "You'll stop receiving updates at this address.", danger: true, confirmLabel: "Delete" })) { await b.run(() => api.del(`/v1/webhooks/endpoints/${r.id}`), "Endpoint deleted"); eps.reload(); } }}>Delete</Button>
            </div>) }]} />
      </Card>
    </>
  );
}

function Activity() {
  const deliveries = useLoad(() => api.get("/v1/webhooks/deliveries", { limit: 500 }), [], 4000);
  const events = useLoad(() => api.get("/v1/events", { limit: 500 }), [], 4000);
  const a = useAction();
  const del = [...(deliveries.data || [])].reverse().slice(0, 20);
  const evs = [...(events.data || [])].reverse().slice(0, 20);
  return (
    <div className="grid two">
      <Card title="Recent deliveries" subtitle="Calls we made to your endpoints">
        <ErrorBox error={a.error} />
        <Table rows={del} loading={deliveries.loading} empty="Nothing sent yet. Use “Send test” on an endpoint." cols={[{ label: "Event", key: "type" }, { label: "Result", render: (r) => <Badge>{r.status}</Badge> }, { label: "Response", render: (r) => r.last_status_code || r.last_error || "-" }, { label: "Tries", key: "attempts", num: true },
          { label: "", render: (r) => r.status !== "pending" && <Button className="small" onClick={async () => { await a.run(() => api.post(`/v1/webhooks/deliveries/${r.id}/redeliver`), "Queued to send again"); deliveries.reload(); }}>Send again</Button> }]} />
      </Card>
      <Card title="Event log" subtitle="Everything that happened, even without an endpoint. Kept for 30 days.">
        <Table rows={evs} loading={events.loading} empty="No events yet." cols={[{ label: "Type", key: "type" }, { label: "About", render: (r) => <code>{String(r.data.resource_id).slice(0, 14)}</code> }, { label: "When", render: (r) => <Time value={r.created_at} /> }]} />
      </Card>
    </div>
  );
}

export default function Webhooks() {
  const [tab, setTab] = useState("endpoints");
  return (<><Tabs value={tab} onChange={setTab} tabs={[{ id: "endpoints", label: "Endpoints" }, { id: "activity", label: "Activity" }]} />{tab === "endpoints" ? <Endpoints /> : <Activity />}</>);
}
