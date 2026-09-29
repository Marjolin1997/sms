import { useState } from "react";
import { api } from "../api.js";
import { Badge, Button, Card, Field, Notice, Table, useAction, useLoad, when } from "../ui.jsx";

function parseCsv(text) {
  return text.split("\n").map((l) => l.trim()).filter(Boolean).map((line) => {
    const [a, b, first, last] = line.split(",").map((x) => x.trim());
    const isEmail = (v) => v && v.includes("@");
    const row = {};
    if (isEmail(a)) row.email = a; else if (a) row.phone = a;
    if (isEmail(b)) row.email = b; else if (b) row.phone = row.phone || b;
    if (first) row.first_name = first;
    if (last) row.last_name = last;
    return row;
  }).filter((r) => r.phone || r.email);
}

export default function Contacts() {
  const contacts = useLoad(() => api.get("/v1/contacts", { limit: 200 }), []);
  const lists = useLoad(() => api.get("/v1/lists"), []);
  const [selected, setSelected] = useState(new Set());
  const [csv, setCsv] = useState("");
  const [listName, setListName] = useState("");
  const [target, setTarget] = useState("");
  const [consent, setConsent] = useState({ channel: "sms", address: "", action: "opt_in", evidence: "" });
  const a = useAction(), b = useAction(), c = useAction(), d = useAction();
  const toggle = (id) => setSelected((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const rows = (contacts.data || []).map((x) => ({ ...x, _sel: selected.has(x.id) }));

  return (
    <>
      <div className="grid two">
        <Card title="Import contacts">
          <Field label="One per line: phone or email, optional second address, first name, last name" hint="e.g. +355691234567,ana@example.com,Ana,Hoxha">
            <textarea rows={4} value={csv} onChange={(e) => setCsv(e.target.value)} placeholder="+355691234567,,Ana" />
          </Field>
          <Notice error={a.error} ok={a.ok} />
          <Button variant="primary" busy={a.busy} disabled={!csv.trim()} onClick={async () => {
            const r = await a.run(() => api.post("/v1/contacts/import", { contacts: parseCsv(csv) }), "Import finished");
            if (r) { setCsv(""); contacts.reload(); if (r.errors.length) alert(`${r.created} created, ${r.updated} updated.\nRejected rows:\n` + r.errors.map((e) => `row ${e.row + 1}: ${e.message}`).join("\n")); }
          }}>Import</Button>
        </Card>
        <Card title="Record consent">
          <div className="grid two">
            <Field label="Channel"><select value={consent.channel} onChange={(e) => setConsent({ ...consent, channel: e.target.value })}><option>sms</option><option>email</option></select></Field>
            <Field label="Action"><select value={consent.action} onChange={(e) => setConsent({ ...consent, action: e.target.value })}><option value="opt_in">Opt in</option><option value="opt_out">Opt out</option></select></Field>
          </div>
          <Field label="Address"><input value={consent.address} onChange={(e) => setConsent({ ...consent, address: e.target.value })} placeholder="+355… or name@example.com" /></Field>
          <Field label="Evidence" hint="Required for opt-in: how and when the person agreed"><input value={consent.evidence} onChange={(e) => setConsent({ ...consent, evidence: e.target.value })} placeholder="Signup form v3, 2026-09-29" /></Field>
          <Notice error={b.error} ok={b.ok} />
          <Button variant="primary" busy={b.busy} disabled={!consent.address} onClick={() => b.run(() => api.post("/v1/consent", { ...consent, source: "console", reason: "unsubscribe" }), "Consent recorded")}>Save</Button>
        </Card>
      </div>
      <Card title={`Contacts (${rows.length})`} actions={
        <>
          <select value={target} onChange={(e) => setTarget(e.target.value)}><option value="">Add selected to list…</option>{(lists.data || []).map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}</select>
          <Button busy={c.busy} disabled={!target || !selected.size} onClick={async () => { await c.run(() => api.post(`/v1/lists/${target}/members`, { contact_ids: [...selected] }), "Added to list"); setSelected(new Set()); }}>Add ({selected.size})</Button>
        </>}>
        <Notice error={c.error || contacts.error} ok={c.ok} />
        <Table rows={rows} empty="No contacts yet. Import some above."
          cols={[{ label: "", render: (r) => <input type="checkbox" checked={r._sel} onChange={() => toggle(r.id)} /> },
                 { label: "Name", render: (r) => [r.first_name, r.last_name].filter(Boolean).join(" ") || "-" },
                 { label: "Phone", render: (r) => r.phone || "-" }, { label: "Email", render: (r) => r.email || "-" },
                 { label: "Added", render: (r) => when(r.created_at) }]} />
      </Card>
      <Card title="Lists">
        <div className="row">
          <input placeholder="New list name" value={listName} onChange={(e) => setListName(e.target.value)} />
          <Button busy={d.busy} disabled={!listName} onClick={async () => { await d.run(() => api.post("/v1/lists", { name: listName }), "List created"); setListName(""); lists.reload(); }}>Create list</Button>
        </div>
        <Notice error={d.error} />
        <Table rows={lists.data || []} empty="No lists yet." cols={[{ label: "Name", key: "name" }, { label: "Id", key: "id", num: true }]} />
      </Card>
    </>
  );
}
