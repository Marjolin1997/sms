import { useState } from "react";
import { api, uuid } from "../api.js";
import { Badge, Button, Card, Field, Notice, Table, useAction, useLoad } from "../ui.jsx";

function Timeline({ path }) {
  const ev = useLoad(() => api.get(path), [path], 3000);
  return <Table rows={ev.data || []} cols={[{ label: "From", render: (r) => r.from || "-" }, { label: "To", render: (r) => <Badge>{r.to}</Badge> }, { label: "Detail", key: "detail" }]} empty="No events yet." />;
}

function SmsForm() {
  const [f, setF] = useState({ to: "", sender: "", text: "", category: "transactional" });
  const [sent, setSent] = useState(null);
  const a = useAction();
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const submit = async (e) => {
    e.preventDefault();
    const r = await a.run(() => api.post("/v1/messages", { to: f.to, sender: f.sender, text: f.text, category: f.category }, { headers: { "Idempotency-Key": uuid() } }), "Accepted for delivery");
    if (r) setSent(r);
  };
  return (
    <>
      <Card title="Send an SMS">
        <form onSubmit={submit} className="form">
          <div className="grid two">
            <Field label="To (E.164)"><input required placeholder="+355691234567" value={f.to} onChange={set("to")} /></Field>
            <Field label="Sender ID" hint="Must be approved for your account"><input required placeholder="ACME" value={f.sender} onChange={set("sender")} /></Field>
          </div>
          <Field label="Message" hint={`${f.text.length} characters`}><textarea required rows={3} value={f.text} onChange={set("text")} /></Field>
          <Field label="Category" hint="Marketing needs a recorded opt-in for the recipient">
            <select value={f.category} onChange={set("category")}><option>transactional</option><option>marketing</option></select>
          </Field>
          <Notice error={a.error} ok={a.ok} />
          <Button variant="primary" busy={a.busy}>Send SMS</Button>
        </form>
      </Card>
      {sent && (
        <Card title={`Message ${sent.id.slice(0, 8)}…`} actions={<Badge>{sent.status}</Badge>}>
          <div className="kv"><span>Segments</span><b>{sent.segments}</b><span>Price</span><b>{sent.total_price} {sent.currency}</b></div>
          <Timeline path={`/v1/messages/${sent.id}/events`} />
        </Card>
      )}
    </>
  );
}

function EmailForm() {
  const [f, setF] = useState({ from_email: "", to: "", subject: "", text: "", category: "transactional" });
  const [sent, setSent] = useState(null);
  const a = useAction();
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const submit = async (e) => {
    e.preventDefault();
    const r = await a.run(() => api.post("/v1/email/messages", f, { headers: { "Idempotency-Key": uuid() } }), "Queued for delivery");
    if (r) setSent(r);
  };
  return (
    <>
      <Card title="Send an email">
        <form onSubmit={submit} className="form">
          <div className="grid two">
            <Field label="From" hint="Must be on a verified domain"><input required type="email" value={f.from_email} onChange={set("from_email")} /></Field>
            <Field label="To"><input required type="email" value={f.to} onChange={set("to")} /></Field>
          </div>
          <Field label="Subject"><input required value={f.subject} onChange={set("subject")} /></Field>
          <Field label="Body (plain text)"><textarea required rows={4} value={f.text} onChange={set("text")} /></Field>
          <Field label="Category"><select value={f.category} onChange={set("category")}><option>transactional</option><option>marketing</option></select></Field>
          <Notice error={a.error} ok={a.ok} />
          <Button variant="primary" busy={a.busy}>Send email</Button>
        </form>
      </Card>
      {sent && (
        <Card title={`Email ${sent.id.slice(0, 8)}…`} actions={<Badge>{sent.status}</Badge>}>
          <Timeline path={`/v1/email/messages/${sent.id}/events`} />
        </Card>
      )}
    </>
  );
}

export default function Send() {
  const [tab, setTab] = useState("sms");
  return (
    <>
      <div className="tabs">
        <button className={tab === "sms" ? "on" : ""} onClick={() => setTab("sms")}>SMS</button>
        <button className={tab === "email" ? "on" : ""} onClick={() => setTab("email")}>Email</button>
      </div>
      {tab === "sms" ? <SmsForm /> : <EmailForm />}
    </>
  );
}
