import { useState } from "react";
import { api } from "../api.js";
import { T, t } from "../i18n.jsx";
import { Badge, Button, Card, ErrorBox, Field, SecretBanner, Table, Tabs, Time, useAction, useLoad, useUi } from "../ui.jsx";

const TYPES = [["*", T("Everything")], ["message.*", T("SMS updates")], ["email.*", T("Email updates")], ["campaign.*", T("Campaign progress")], ["consent.*", T("Opt-ins and opt-outs")], ["invoice.*", T("Invoices")], ["payment.*", T("Payments")]];
const DISABLED = { too_many_failures: T("turned off after repeated failures"), gone: T("your server said it's gone (410)"), unsafe_url: T("address isn't allowed"), manual: T("turned off by you") };

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
      {secret && <SecretBanner title={t("Your signing secret")} note={t("Use it to check that each request really came from us. It's shown only once. If you lose it, rotate it.")} value={secret} onClose={() => setSecret(null)} />}
      <div className="help"><b>{t("What's a webhook?")}</b> {t("When something happens (an SMS is delivered, an email bounces) we send an HTTPS request to your server so you don't have to keep asking. Give us a public HTTPS address; we sign each request so you can trust it.")}</div>
      <Card title={t("Add an endpoint")}>
        <div className="form">
          <Field label={t("Address to call")} hint={t("Must be public and start with https://")} error={httpBad ? t("Use an https:// address") : null}><input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://example.com/hooks/sms" /></Field>
          <Field label={t("Send me…")}><div className="row wrap">{TYPES.map(([ty, l]) => <label key={ty} className="chip"><input type="checkbox" checked={types.includes(ty)} onChange={(e) => setTypes(e.target.checked ? [...types.filter((x) => ty === "*" ? false : x !== "*"), ty] : types.filter((x) => x !== ty))} />{t(l)}</label>)}</div></Field>
          <ErrorBox error={a.error} />
          <div><Button variant="primary" busy={a.busy} disabled={!url || httpBad || !types.length} onClick={async () => { const r = await a.run(() => api.post("/v1/webhooks/endpoints", { url, event_types: types }), t("Endpoint created")); if (r && r.secret) { setSecret(r.secret); setUrl(""); eps.reload(); } }}>{t("Create endpoint")}</Button></div>
        </div>
      </Card>
      <Card title={t("Your endpoints")}>
        <ErrorBox error={b.error || eps.error} retry={eps.reload} />
        <Table rows={eps.data || []} loading={eps.loading} emptyTitle={t("No endpoints yet")} empty={t("Add one above to start receiving updates.")} cols={[
          { label: t("Address"), render: (r) => <code>{r.url}</code> }, { label: t("Events"), render: (r) => r.event_types.join(", ") },
          { label: t("Status"), render: (r) => <><Badge>{r.status}</Badge>{r.disabled_reason && <small className="muted"> {DISABLED[r.disabled_reason] ? t(DISABLED[r.disabled_reason]) : r.disabled_reason}</small>}</> },
          { label: "", render: (r) => (
            <div className="row wrap">
              <Button className="small" busy={b.busy} onClick={() => b.run(() => api.post(`/v1/webhooks/endpoints/${r.id}/test`), t("Test event sent. See Recent deliveries."))}>{t("Send test")}</Button>
              {r.status === "disabled" && <Button className="small" variant="primary" busy={b.busy} onClick={async () => { await b.run(() => api.patch(`/v1/webhooks/endpoints/${r.id}`, { enabled: true }), t("Turned back on")); eps.reload(); }}>{t("Turn on")}</Button>}
              <Button className="small" busy={b.busy} onClick={async () => { if (await confirm({ title: t("Rotate the secret?"), body: t("The old secret stops working straight away. Update your server with the new one."), confirmLabel: t("Rotate") })) { const x = await b.run(() => api.post(`/v1/webhooks/endpoints/${r.id}/rotate-secret`), t("Secret rotated")); if (x && x.secret) setSecret(x.secret); } }}>{t("Rotate secret")}</Button>
              <Button className="small" variant="danger" busy={b.busy} onClick={async () => { if (await confirm({ title: t("Delete this endpoint?"), body: t("You'll stop receiving updates at this address."), danger: true, confirmLabel: t("Delete") })) { await b.run(() => api.del(`/v1/webhooks/endpoints/${r.id}`), t("Endpoint deleted")); eps.reload(); } }}>{t("Delete")}</Button>
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
      <Card title={t("Recent deliveries")} subtitle={t("Calls we made to your endpoints")}>
        <ErrorBox error={a.error} />
        <Table rows={del} loading={deliveries.loading} empty={t("Nothing sent yet. Use “Send test” on an endpoint.")} cols={[{ label: t("Event"), key: "type" }, { label: t("Result"), render: (r) => <Badge>{r.status}</Badge> }, { label: t("Response"), render: (r) => r.last_status_code || r.last_error || "-" }, { label: t("Tries"), key: "attempts", num: true },
          { label: "", render: (r) => r.status !== "pending" && <Button className="small" onClick={async () => { await a.run(() => api.post(`/v1/webhooks/deliveries/${r.id}/redeliver`), t("Queued to send again")); deliveries.reload(); }}>{t("Send again")}</Button> }]} />
      </Card>
      <Card title={t("Event log")} subtitle={t("Everything that happened, even without an endpoint. Kept for 30 days.")}>
        <Table rows={evs} loading={events.loading} empty={t("No events yet.")} cols={[{ label: t("Type"), key: "type" }, { label: t("About"), render: (r) => <code>{String(r.data.resource_id).slice(0, 14)}</code> }, { label: t("When"), render: (r) => <Time value={r.created_at} /> }]} />
      </Card>
    </div>
  );
}

export default function Webhooks() {
  const [tab, setTab] = useState("endpoints");
  return (<><Tabs value={tab} onChange={setTab} tabs={[{ id: "endpoints", label: t("Endpoints") }, { id: "activity", label: t("Activity") }]} />{tab === "endpoints" ? <Endpoints /> : <Activity />}</>);
}
