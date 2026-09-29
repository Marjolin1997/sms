import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { Badge, Button, Card, ErrorBox, Field, Table, Tabs, Time, useAction, useDebounced, useLoad, useUi } from "../ui.jsx";

// CSV/TXT: phone,email,first_name,last_name (kokë opsionale; kolona të njohura sipas emrit)
export function parseContacts(text) {
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  if (!lines.length) return { rows: [], skipped: 0 };
  const split = (l) => l.split(/[,;\t]/).map((x) => x.trim().replace(/^"|"$/g, ""));
  const head = split(lines[0]).map((h) => h.toLowerCase().replace(/[\s-]+/g, "_"));
  const known = ["phone", "mobile", "email", "first_name", "last_name", "name"];
  const hasHeader = head.some((h) => known.includes(h));
  const cols = hasHeader ? head : ["phone", "email", "first_name", "last_name"];
  const rows = [];
  for (const line of hasHeader ? lines.slice(1) : lines) {
    const cells = split(line);
    const r = {};
    cols.forEach((c, i) => {
      const v = cells[i];
      if (!v) return;
      if (c === "phone" || c === "mobile") r.phone = v.startsWith("+") ? v : v.replace(/[^\d]/g, "").replace(/^/, "+");
      else if (c === "email") r.email = v;
      else if (c === "first_name" || c === "name") r.first_name = v;
      else if (c === "last_name") r.last_name = v;
    });
    if (!hasHeader) { // pa kokë: një qelizë me @ është email, përndryshe telefon
      for (const v of cells) if (v.includes("@")) r.email = v;
    }
    if (r.phone || r.email) rows.push(r);
  }
  return { rows, skipped: lines.length - (hasHeader ? 1 : 0) - rows.length };
}

function Import({ onDone }) {
  const [text, setText] = useState("");
  const file = useRef(null);
  const a = useAction();
  const parsed = parseContacts(text);
  const [result, setResult] = useState(null);
  const run = async () => {
    const r = await a.run(() => api.post("/v1/contacts/import", { contacts: parsed.rows.slice(0, 1000) }), null);
    if (r && r !== true) { setResult(r); onDone(); }
  };
  return (
    <Card title="Import contacts" subtitle="Paste rows or choose a CSV file. Columns: phone, email, first name, last name.">
      <div className="help">Importing people does <b>not</b> mean they agreed to marketing. Record consent under the Consent tab, or they'll be left out of promotions.</div>
      <div className="form">
        <div className="row wrap">
          <input ref={file} type="file" accept=".csv,.txt,text/csv,text/plain" hidden onChange={async (e) => { const f = e.target.files[0]; if (f) setText(await f.text()); e.target.value = ""; }} />
          <Button onClick={() => file.current.click()}>Choose CSV file</Button>
          <small className="muted">Up to 1000 rows at a time. Phone numbers need the country code.</small>
        </div>
        <Field label="Or paste here"><textarea rows={5} value={text} onChange={(e) => setText(e.target.value)} placeholder={"phone,email,first_name,last_name\n+355691234567,ana@example.com,Ana,Hoxha"} /></Field>
        {text.trim() && <div className="quote"><span><b>{parsed.rows.length}</b> people found</span>{parsed.skipped > 0 && <span className="muted">{parsed.skipped} line(s) without a phone or email will be ignored</span>}{parsed.rows.length > 1000 && <span>Only the first 1000 will be imported this time</span>}</div>}
        <ErrorBox error={a.error} />
        <div><Button variant="primary" busy={a.busy} disabled={!parsed.rows.length} onClick={run}>Import {parsed.rows.length || ""} people</Button></div>
        {result && (
          <div className="alert good"><span><b>{result.created}</b> added, <b>{result.updated}</b> updated.{result.errors.length > 0 && <> <b>{result.errors.length}</b> rows were rejected.</>}</span></div>
        )}
        {result?.errors?.length > 0 && <details><summary>See rejected rows</summary><ul>{result.errors.slice(0, 50).map((e) => <li key={e.row}>Row {e.row + 1}: {e.message}</li>)}</ul></details>}
      </div>
    </Card>
  );
}

function People({ lists, reloadLists }) {
  const { confirm, toast } = useUi();
  const [q, setQ] = useState("");
  const dq = useDebounced(q);
  const [listId, setListId] = useState("");
  const [st, setSt] = useState({ items: [], loading: true, error: null, more: false });
  const [sel, setSel] = useState(new Set());
  const [target, setTarget] = useState("");
  const a = useAction();
  const load = async (after = 0) => {
    setSt((s) => ({ ...s, loading: true, error: null }));
    try {
      const r = await api.get("/v1/contacts", { q: dq, list_id: listId, after_id: after || undefined, limit: 50 });
      setSt((s) => ({ items: after ? [...s.items, ...r] : r, loading: false, error: null, more: r.length === 50 }));
    } catch (e) { setSt((s) => ({ ...s, loading: false, error: e })); }
  };
  useEffect(() => { setSel(new Set()); load(); }, [dq, listId]); // eslint-disable-line
  const toggle = (id) => setSel((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const rows = st.items.map((x) => ({ ...x }));
  const all = rows.length > 0 && rows.every((r) => sel.has(r.id));
  return (
    <Card title="People" actions={
      <>
        <select aria-label="Add selected to list" value={target} onChange={(e) => setTarget(e.target.value)}><option value="">Add {sel.size || "selected"} to list…</option>{lists.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}</select>
        <Button busy={a.busy} disabled={!target || !sel.size} onClick={async () => { await a.run(() => api.post(`/v1/lists/${target}/members`, { contact_ids: [...sel] }), `Added ${sel.size} to the list`); setSel(new Set()); setTarget(""); reloadLists(); }}>Add</Button>
      </>}>
      <div className="toolbar">
        <input aria-label="Search people" placeholder="Search name, phone or email" value={q} onChange={(e) => setQ(e.target.value)} />
        <select aria-label="Filter by list" value={listId} onChange={(e) => setListId(e.target.value)}><option value="">All people</option>{lists.map((l) => <option key={l.id} value={l.id}>In list: {l.name}</option>)}</select>
      </div>
      <ErrorBox error={st.error || a.error} retry={() => load()} />
      <Table rows={rows} loading={st.loading} emptyTitle={q || listId ? "No matches" : "No contacts yet"} empty={q || listId ? "Try a different search." : "Import a file or add someone from the Add tab."}
        footer={st.more && <div className="row end" style={{ padding: 12 }}><Button busy={st.loading} onClick={() => load(rows[rows.length - 1].id)}>Load more</Button></div>}
        cols={[{ label: <input type="checkbox" aria-label="Select all" checked={all} onChange={() => setSel(all ? new Set() : new Set(rows.map((r) => r.id)))} />, render: (r) => <input type="checkbox" aria-label={`Select ${r.first_name || r.phone || r.email}`} checked={sel.has(r.id)} onChange={() => toggle(r.id)} /> },
          { label: "Name", render: (r) => <b>{[r.first_name, r.last_name].filter(Boolean).join(" ") || "-"}</b> }, { label: "Phone", render: (r) => (r.phone ? `+${r.phone}` : "-") }, { label: "Email", render: (r) => r.email || "-" }, { label: "Added", render: (r) => <Time value={r.created_at} /> },
          { label: "", render: (r) => <Button variant="danger" className="small" onClick={async () => { if (await confirm({ title: "Erase this person?", body: "Their name, phone, email and attributes are permanently removed (GDPR). We keep only an anonymous block so they can't be messaged again by mistake.", danger: true, confirmLabel: "Erase permanently" })) { await a.run(() => api.del(`/v1/contacts/${r.id}`), "Person erased"); load(); } }}>Erase</Button> }]} />
    </Card>
  );
}

function AddOne({ onDone }) {
  const [f, setF] = useState({ first_name: "", last_name: "", phone: "", email: "" });
  const a = useAction();
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const phoneBad = f.phone && !/^\+[1-9]\d{6,14}$/.test(f.phone.replace(/[\s-]/g, ""));
  return (
    <Card title="Add one person">
      <form className="form" onSubmit={async (e) => { e.preventDefault(); const body = { ...f, phone: f.phone ? f.phone.replace(/[\s-]/g, "") : null, email: f.email || null }; const r = await a.run(() => api.post("/v1/contacts", body), "Saved"); if (r) { setF({ first_name: "", last_name: "", phone: "", email: "" }); onDone(); } }}>
        <div className="grid two">
          <Field label="First name"><input value={f.first_name} onChange={set("first_name")} /></Field>
          <Field label="Last name"><input value={f.last_name} onChange={set("last_name")} /></Field>
          <Field label="Phone" hint="With country code" error={phoneBad ? "Use international format, e.g. +355691234567" : null}><input inputMode="tel" value={f.phone} onChange={set("phone")} placeholder="+355691234567" /></Field>
          <Field label="Email"><input type="email" value={f.email} onChange={set("email")} /></Field>
        </div>
        <small className="muted">A phone number or an email is required. Adding someone who already exists updates them.</small>
        <ErrorBox error={a.error} />
        <div><Button variant="primary" busy={a.busy} disabled={(!f.phone && !f.email) || phoneBad}>Save person</Button></div>
      </form>
    </Card>
  );
}

function Lists({ lists, reload }) {
  const [name, setName] = useState("");
  const a = useAction();
  return (
    <Card title="Lists" subtitle="Group people to send a campaign to them. Add people from the People tab.">
      <div className="row wrap"><input aria-label="New list name" style={{ maxWidth: 320 }} placeholder="e.g. Newsletter" value={name} onChange={(e) => setName(e.target.value)} /><Button variant="primary" busy={a.busy} disabled={!name.trim()} onClick={async () => { await a.run(() => api.post("/v1/lists", { name: name.trim() }), "List created"); setName(""); reload(); }}>Create list</Button></div>
      <ErrorBox error={a.error} />
      <Table rows={lists} emptyTitle="No lists yet" empty="Create your first list above." cols={[{ label: "Name", render: (r) => <b>{r.name}</b> }]} />
    </Card>
  );
}

function Consent() {
  const [f, setF] = useState({ channel: "sms", address: "", action: "opt_in", evidence: "" });
  const [check, setCheck] = useState(null);
  const a = useAction(), b = useAction();
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  return (
    <>
      <div className="help"><b>Why this matters.</b> Promotions may only go to people who agreed to receive them. Write down how and when they agreed. If you can't, don't message them. Codes and receipts (transactional) don't need this, but anyone who replied STOP is always blocked.</div>
      <div className="grid two">
        <Card title="Record a decision">
          <form className="form" onSubmit={async (e) => { e.preventDefault(); await a.run(() => api.post("/v1/consent", { ...f, source: "console", reason: "unsubscribe" }), f.action === "opt_in" ? "Consent recorded" : "Opt-out recorded"); setF({ ...f, address: "", evidence: "" }); }}>
            <div className="grid two">
              <Field label="Channel"><select value={f.channel} onChange={set("channel")}><option value="sms">SMS</option><option value="email">Email</option></select></Field>
              <Field label="They…"><select value={f.action} onChange={set("action")}><option value="opt_in">agreed to promotions</option><option value="opt_out">asked to stop promotions</option></select></Field>
            </div>
            <Field label={f.channel === "sms" ? "Phone number" : "Email address"}><input required value={f.address} onChange={set("address")} placeholder={f.channel === "sms" ? "+355691234567" : "name@example.com"} /></Field>
            {f.action === "opt_in" && <Field label="How and when did they agree?" hint="Kept as proof. For example: ticked the box on the website signup form, 14 Aug 2026"><input required minLength={3} value={f.evidence} onChange={set("evidence")} /></Field>}
            <ErrorBox error={a.error} />
            <div><Button variant="primary" busy={a.busy}>Save</Button></div>
          </form>
        </Card>
        <Card title="Check someone">
          <form className="form" onSubmit={async (e) => { e.preventDefault(); const r = await b.run(() => api.get("/v1/consent/check", { channel: f.channel, address: f.address, category: "marketing" })); if (r && r !== true) setCheck(r); }}>
            <small className="muted">Uses the channel and address from the form on the left.</small>
            <ErrorBox error={b.error} />
            <div><Button busy={b.busy} disabled={!f.address}>Can I send them promotions?</Button></div>
            {check && <div className={`alert ${check.allowed ? "good" : "warn"}`}><span>{check.allowed ? "Yes, they agreed." : { no_consent: "No. There's no recorded consent.", opted_out: "No. They opted out of promotions." }[check.reason] || `No. ${check.reason.replace("blocked:", "Blocked: ").replaceAll("_", " ")}.`}</span></div>}
          </form>
        </Card>
      </div>
    </>
  );
}

export default function Contacts() {
  const [tab, setTab] = useState("people");
  const lists = useLoad(() => api.get("/v1/lists"), []);
  const [refresh, setRefresh] = useState(0);
  return (
    <>
      <Tabs value={tab} onChange={setTab} tabs={[{ id: "people", label: "People" }, { id: "add", label: "Add" }, { id: "import", label: "Import" }, { id: "lists", label: "Lists", count: lists.data?.length }, { id: "consent", label: "Consent" }]} />
      {tab === "people" && <People key={refresh} lists={lists.data || []} reloadLists={lists.reload} />}
      {tab === "add" && <AddOne onDone={() => setRefresh((n) => n + 1)} />}
      {tab === "import" && <Import onDone={() => setRefresh((n) => n + 1)} />}
      {tab === "lists" && <Lists lists={lists.data || []} reload={lists.reload} />}
      {tab === "consent" && <Consent />}
    </>
  );
}
