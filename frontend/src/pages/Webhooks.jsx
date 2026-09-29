import { useState } from "react";
import { api } from "../api.js";
import { Badge, Button, Card, Field, Notice, Table, useAction, useLoad, when } from "../ui.jsx";

const TYPES = ["*", "message.*", "email.*", "campaign.*", "consent.*"];

export default function Webhooks() {
  const eps = useLoad(() => api.get("/v1/webhooks/endpoints"), []);
  const deliveries = useLoad(() => api.get("/v1/webhooks/deliveries", { limit: 500 }), [], 4000);
  const events = useLoad(() => api.get("/v1/events", { limit: 200 }), [], 4000);
  const [url, setUrl] = useState("");
  const [types, setTypes] = useState(["*"]);
  const [secret, setSecret] = useState(null);
  const a = useAction(), b = useAction();
  const create = async () => {
    const r = await a.run(() => api.post("/v1/webhooks/endpoints", { url, event_types: types }), "Endpoint created");
    if (r) { setSecret(r.secret); setUrl(""); eps.reload(); }
  };
  const evs = [...(events.data || [])].reverse().slice(0, 15);
  return (
    <>
      {secret && (
        <div className="alert good"><b>Signing secret (shown once):</b> <code>{secret}</code><br /><small>Store it now. Verify each delivery with header <code>X-SMS-Signature</code> (HMAC-SHA256 of <code>timestamp.body</code>).</small> <Button onClick={() => setSecret(null)}>Hide</Button></div>
      )}
      <Card title="Add endpoint">
        <div className="form">
          <Field label="HTTPS URL" hint="Must be public https. Private addresses are blocked."><input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://example.com/hooks/sms" /></Field>
          <Field label="Events">
            <div className="row wrap">{TYPES.map((t) => <label key={t} className="chip"><input type="checkbox" checked={types.includes(t)} onChange={(e) => setTypes(e.target.checked ? [...types, t] : types.filter((x) => x !== t))} />{t}</label>)}</div>
          </Field>
          <Notice error={a.error} ok={a.ok} />
          <Button variant="primary" busy={a.busy} disabled={!url || !types.length} onClick={create}>Create endpoint</Button>
        </div>
      </Card>
      <Card title="Endpoints">
        <Notice error={b.error || eps.error} ok={b.ok} />
        <Table rows={eps.data || []} empty="No endpoints yet." cols={[
          { label: "URL", render: (r) => <code>{r.url}</code> }, { label: "Events", render: (r) => r.event_types.join(", ") },
          { label: "Status", render: (r) => <><Badge>{r.status}</Badge>{r.disabled_reason && <small className="muted"> {r.disabled_reason}</small>}</> },
          { label: "", render: (r) => (
            <div className="row">
              <Button busy={b.busy} onClick={() => b.run(() => api.post(`/v1/webhooks/endpoints/${r.id}/test`), "Test event queued")}>Send test</Button>
              <Button busy={b.busy} onClick={async () => { const x = await b.run(() => api.post(`/v1/webhooks/endpoints/${r.id}/rotate-secret`), "Secret rotated"); if (x) setSecret(x.secret); }}>Rotate secret</Button>
              {r.status === "disabled" && <Button busy={b.busy} onClick={async () => { await b.run(() => api.patch(`/v1/webhooks/endpoints/${r.id}`, { enabled: true }), "Re-enabled"); eps.reload(); }}>Enable</Button>}
              <Button variant="danger" busy={b.busy} onClick={async () => { if (confirm("Delete this endpoint?")) { await b.run(() => api.del(`/v1/webhooks/endpoints/${r.id}`), "Deleted"); eps.reload(); } }}>Delete</Button>
            </div>) }]} />
      </Card>
      <div className="grid two">
        <Card title="Recent deliveries">
          <Table rows={[...(deliveries.data || [])].reverse().slice(0, 12)} empty="No deliveries yet." cols={[{ label: "Event", key: "type" }, { label: "Status", render: (r) => <Badge>{r.status}</Badge> }, { label: "HTTP", render: (r) => r.last_status_code || r.last_error || "-" }, { label: "", render: (r) => r.status !== "pending" && <Button onClick={async () => { await b.run(() => api.post(`/v1/webhooks/deliveries/${r.id}/redeliver`), "Redelivery queued"); deliveries.reload(); }}>Retry</Button> }]} />
        </Card>
        <Card title="Event log (latest)">
          <Table rows={evs} empty="No events yet." cols={[{ label: "Type", key: "type" }, { label: "Resource", render: (r) => <code>{String(r.data.resource_id).slice(0, 14)}</code> }, { label: "When", render: (r) => when(r.created_at) }]} />
        </Card>
      </div>
    </>
  );
}
