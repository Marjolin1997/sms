import { useState } from "react";
import { api } from "../api.js";
import { Badge, Button, Card, ErrorBox, Field, Table, Tabs, Time, smsInfo, useAction, useLoad, useUi } from "../ui.jsx";

const COUNTRIES = [["AL", "Albania"], ["XK", "Kosovo"], ["MK", "North Macedonia"], ["ME", "Montenegro"], ["RS", "Serbia"], ["GR", "Greece"], ["IT", "Italy"], ["DE", "Germany"], ["AT", "Austria"], ["CH", "Switzerland"], ["FR", "France"], ["GB", "United Kingdom"], ["US", "United States"], ["TR", "Türkiye"]];
const STATUS_HELP = { pending: "Waiting for our review, usually within one business day.", approved: "Ready to use.", rejected: "Not approved. See the reason, fix it and submit again.", revoked: "No longer allowed. See the reason." };

function SenderIds() {
  const { confirm } = useUi();
  const ids = useLoad(() => api.get("/v1/sender-ids"), [], 15000);
  const [f, setF] = useState({ country: "AL", value: "" });
  const a = useAction();
  const alnum = /^(?=.*[A-Za-z])[A-Za-z0-9 ]{3,11}$/.test(f.value);
  const numeric = /^\+?[1-9]\d{2,14}$/.test(f.value);
  const bad = f.value && !alnum && !numeric;
  return (
    <>
      <div className="help"><b>What is a sender ID?</b> It's the name people see instead of a phone number, like <code>ACME</code>. Use 3–11 letters or digits (at least one letter), or a phone number. Each country needs its own approval, and only your brand or business names are approved.</div>
      <Card title="Request a sender ID">
        <form className="form" onSubmit={async (e) => { e.preventDefault(); await a.run(() => api.post("/v1/sender-ids", { country: f.country, value: f.value.trim() }), "Request sent for review"); setF({ ...f, value: "" }); ids.reload(); }}>
          <div className="grid two">
            <Field label="Country it will send to"><select value={f.country} onChange={(e) => setF({ ...f, country: e.target.value })}>{COUNTRIES.map(([c, n]) => <option key={c} value={c}>{n} ({c})</option>)}</select></Field>
            <Field label="Sender ID" hint="For example your brand name" error={bad ? "3–11 letters/digits with at least one letter, or a phone number" : null}><input required maxLength={16} value={f.value} onChange={(e) => setF({ ...f, value: e.target.value })} placeholder="ACME" /></Field>
          </div>
          <ErrorBox error={a.error} />
          <div><Button variant="primary" busy={a.busy} disabled={!f.value || bad}>Submit for approval</Button></div>
        </form>
      </Card>
      <Card title="Your sender IDs">
        <ErrorBox error={ids.error} retry={ids.reload} />
        <Table rows={ids.data || []} loading={ids.loading} emptyTitle="No sender IDs yet" empty="Request one above. You can't send SMS until one is approved."
          cols={[{ label: "Sender", render: (r) => <b>{r.value}</b> }, { label: "Country", key: "country" }, { label: "Status", render: (r) => <><Badge>{r.status}</Badge> <small className="muted">{STATUS_HELP[r.status]}</small></> }, { label: "Reason", render: (r) => r.reason || "" }, { label: "Requested", render: (r) => <Time value={r.created_at} /> },
            { label: "", render: (r) => ["rejected", "revoked"].includes(r.status) && <Button className="small" onClick={async () => { if (await confirm({ title: "Submit again?", body: "It will go back into the review queue.", confirmLabel: "Submit again" })) { await a.run(() => api.post("/v1/sender-ids", { country: r.country, value: r.value }), "Submitted again"); ids.reload(); } }}>Submit again</Button> }]} />
      </Card>
    </>
  );
}

const VAR = /\{\{([a-z_][a-z0-9_]*)\}\}/g;
function Templates() {
  const list = useLoad(() => api.get("/v1/templates"), [], 15000);
  const [f, setF] = useState({ name: "", body: "" });
  const [editing, setEditing] = useState(null); // template për version të ri
  const a = useAction();
  const vars = [...new Set([...f.body.matchAll(VAR)].map((m) => m[1]))];
  const info = smsInfo(f.body.replace(VAR, "XXXXXXXX"));
  const malformed = /\{\{|\}\}/.test(f.body.replace(VAR, ""));
  const insert = (v) => setF({ ...f, body: `${f.body}{{${v}}}` });
  return (
    <>
      <div className="help"><b>Templates</b> are messages with blanks, like <code>Your code is {"{{code}}"}</code>. We approve the wording once; after that you send it as often as you like, filling in the blanks. Changing an approved template creates a new version that needs approval again, and the old version keeps working meanwhile.</div>
      <Card title={editing ? `New version of “${editing.name}”` : "Create a template"} actions={editing && <Button onClick={() => { setEditing(null); setF({ name: "", body: "" }); }}>Cancel</Button>}>
        <form className="form" onSubmit={async (e) => {
          e.preventDefault();
          await a.run(() => (editing ? api.post(`/v1/templates/${editing.id}/versions`, { body: f.body }) : api.post("/v1/templates", { name: f.name, body: f.body })), "Sent for review");
          setF({ name: "", body: "" }); setEditing(null); list.reload();
        }}>
          {!editing && <Field label="Name" hint="Only you see this"><input required maxLength={64} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="Login code" /></Field>}
          <Field label="Message" error={malformed ? "A blank is written {{name}} using lowercase letters, digits or _" : null}><textarea required rows={3} value={f.body} onChange={(e) => setF({ ...f, body: e.target.value })} placeholder="Your code is {{code}}. It expires in 10 minutes." /></Field>
          <div className="row wrap"><small className="muted">Insert a blank:</small>{["first_name", "last_name", "code", "order_id", "amount"].map((v) => <Button key={v} type="button" className="small" onClick={() => insert(v)}>{`{{${v}}}`}</Button>)}</div>
          <div className="meter"><span>{vars.length ? `Blanks: ${vars.join(", ")}` : "No blanks yet"}</span><span>≈ {info.segments} part(s) with 8-character values</span></div>
          <ErrorBox error={a.error} />
          <div><Button variant="primary" busy={a.busy} disabled={!f.body.trim() || (!editing && !f.name.trim()) || malformed}>Submit for approval</Button></div>
        </form>
      </Card>
      <Card title="Your templates">
        <ErrorBox error={list.error} retry={list.reload} />
        {list.loading && !list.data ? <Table loading rows={[]} cols={[]} /> : (list.data || []).length === 0 ? <div className="empty">No templates yet.</div> : (list.data || []).map((t) => (
          <div key={t.id} className="tpl">
            <div className="row wrap"><b>{t.name}</b><span className="muted small">{t.versions.length} version(s)</span><span style={{ flex: 1 }} /><Button className="small" onClick={() => { setEditing(t); setF({ name: t.name, body: t.versions[0]?.body || "" }); scrollTo({ top: 0, behavior: "smooth" }); }}>New version</Button></div>
            <Table rows={t.versions} cols={[{ label: "Version", render: (v) => `v${v.version}` }, { label: "Status", render: (v) => <Badge>{v.status}</Badge> }, { label: "Wording", render: (v) => <code>{v.body}</code> }, { label: "Blanks", render: (v) => v.variables.join(", ") || "-" }, { label: "Note", render: (v) => v.reason || "" }]} />
          </div>
        ))}
      </Card>
    </>
  );
}

export default function Senders() {
  const [tab, setTab] = useState("ids");
  return (<><Tabs value={tab} onChange={setTab} tabs={[{ id: "ids", label: "Sender IDs" }, { id: "templates", label: "Templates" }]} />{tab === "ids" ? <SenderIds /> : <Templates />}</>);
}
