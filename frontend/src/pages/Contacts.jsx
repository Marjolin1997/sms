import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { T, t } from "../i18n.jsx";
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
    <Card title={t("Import contacts")} subtitle={t("Paste rows or choose a CSV file. Columns: phone, email, first name, last name.")}>
      <div className="help">{t("Importing people does not mean they agreed to marketing.")} {t("Record consent under the Consent tab, or they'll be left out of promotions.")}</div>
      <div className="form">
        <div className="row wrap">
          <input ref={file} type="file" accept=".csv,.txt,text/csv,text/plain" hidden onChange={async (e) => { const f = e.target.files[0]; if (f) setText(await f.text()); e.target.value = ""; }} />
          <Button onClick={() => file.current.click()}>{t("Choose CSV file")}</Button>
          <small className="muted">{t("Up to 1000 rows at a time. Phone numbers need the country code.")}</small>
        </div>
        <Field label={t("Or paste here")}><textarea rows={5} value={text} onChange={(e) => setText(e.target.value)} placeholder={"phone,email,first_name,last_name\n+355691234567,ana@example.com,Ana,Hoxha"} /></Field>
        {text.trim() && <div className="quote"><span><b>{parsed.rows.length}</b> {t("people found")}</span>{parsed.skipped > 0 && <span className="muted">{t("{n} line(s) without a phone or email will be ignored", { n: parsed.skipped })}</span>}{parsed.rows.length > 1000 && <span>{t("Only the first 1000 will be imported this time")}</span>}</div>}
        <ErrorBox error={a.error} />
        <div><Button variant="primary" busy={a.busy} disabled={!parsed.rows.length} onClick={run}>{t("Import {n} people", { n: parsed.rows.length || "" })}</Button></div>
        {result && (
          <div className="alert good"><span><b>{result.created}</b> {t("added")}, <b>{result.updated}</b> {t("updated")}.{result.errors.length > 0 && <> <b>{result.errors.length}</b> {t("rows were rejected.")}</>}</span></div>
        )}
        {result?.errors?.length > 0 && <details><summary>{t("See rejected rows")}</summary><ul>{result.errors.slice(0, 50).map((e) => <li key={e.row}>{t("Row {n}", { n: e.row + 1 })}: {e.message}</li>)}</ul></details>}
      </div>
    </Card>
  );
}

// Shkarkon një objekt JSON si skedar (eksport GDPR)
function download(name, data) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
  const el = document.createElement("a");
  el.href = url; el.download = name; el.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
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
    <Card title={t("People")} actions={
      <>
        <select aria-label={t("Add selected to list")} value={target} onChange={(e) => setTarget(e.target.value)}><option value="">{sel.size ? t("Add {n} to list…", { n: sel.size }) : t("Add selected to list…")}</option>{lists.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}</select>
        <Button busy={a.busy} disabled={!target || !sel.size} onClick={async () => { await a.run(() => api.post(`/v1/lists/${target}/members`, { contact_ids: [...sel] }), t("Added {n} to the list", { n: sel.size })); setSel(new Set()); setTarget(""); reloadLists(); }}>{t("Add")}</Button>
      </>}>
      <div className="toolbar">
        <input aria-label={t("Search people")} placeholder={t("Search name, phone or email")} value={q} onChange={(e) => setQ(e.target.value)} />
        <select aria-label={t("Filter by list")} value={listId} onChange={(e) => setListId(e.target.value)}><option value="">{t("All people")}</option>{lists.map((l) => <option key={l.id} value={l.id}>{t("In list: {name}", { name: l.name })}</option>)}</select>
      </div>
      <ErrorBox error={st.error || a.error} retry={() => load()} />
      <Table rows={rows} loading={st.loading} emptyTitle={q || listId ? t("No matches") : t("No contacts yet")} empty={q || listId ? t("Try a different search.") : t("Import a file or add someone from the Add tab.")}
        footer={st.more && <div className="row end" style={{ padding: 12 }}><Button busy={st.loading} onClick={() => load(rows[rows.length - 1].id)}>{t("Load more")}</Button></div>}
        cols={[{ label: <input type="checkbox" aria-label={t("Select all")} checked={all} onChange={() => setSel(all ? new Set() : new Set(rows.map((r) => r.id)))} />, render: (r) => <input type="checkbox" aria-label={t("Select {who}", { who: r.first_name || r.phone || r.email })} checked={sel.has(r.id)} onChange={() => toggle(r.id)} /> },
          { label: t("Name"), render: (r) => <b>{[r.first_name, r.last_name].filter(Boolean).join(" ") || "-"}</b> }, { label: t("Phone"), render: (r) => (r.phone ? `+${r.phone}` : "-") }, { label: t("Email"), render: (r) => r.email || "-" }, { label: t("Added"), render: (r) => <Time value={r.created_at} /> },
          { label: "", render: (r) => <div className="row wrap"><Button className="small" onClick={async () => { const d = await a.run(() => api.get(`/v1/contacts/${r.id}/export`)); if (d && d !== true) download(`contact-${r.id}.json`, d); }}>{t("Export data")}</Button><Button variant="danger" className="small" onClick={async () => { if (await confirm({ title: t("Erase this person?"), body: t("Their name, phone, email and attributes are permanently removed (GDPR). We keep only an anonymous block so they can't be messaged again by mistake."), danger: true, confirmLabel: t("Erase permanently") })) { await a.run(() => api.del(`/v1/contacts/${r.id}`), t("Person erased")); load(); } }}>{t("Erase")}</Button></div> }]} />
    </Card>
  );
}

function AddOne({ onDone }) {
  const [f, setF] = useState({ first_name: "", last_name: "", phone: "", email: "" });
  const a = useAction();
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const phoneBad = f.phone && !/^\+[1-9]\d{6,14}$/.test(f.phone.replace(/[\s-]/g, ""));
  return (
    <Card title={t("Add one person")}>
      <form className="form" onSubmit={async (e) => { e.preventDefault(); const body = { ...f, phone: f.phone ? f.phone.replace(/[\s-]/g, "") : null, email: f.email || null }; const r = await a.run(() => api.post("/v1/contacts", body), t("Saved")); if (r) { setF({ first_name: "", last_name: "", phone: "", email: "" }); onDone(); } }}>
        <div className="grid two">
          <Field label={t("First name")}><input value={f.first_name} onChange={set("first_name")} /></Field>
          <Field label={t("Last name")}><input value={f.last_name} onChange={set("last_name")} /></Field>
          <Field label={t("Phone")} hint={t("With country code")} error={phoneBad ? t("Use international format, e.g. +355691234567") : null}><input inputMode="tel" value={f.phone} onChange={set("phone")} placeholder="+355691234567" /></Field>
          <Field label={t("Email")}><input type="email" value={f.email} onChange={set("email")} /></Field>
        </div>
        <small className="muted">{t("A phone number or an email is required. Adding someone who already exists updates them.")}</small>
        <ErrorBox error={a.error} />
        <div><Button variant="primary" busy={a.busy} disabled={(!f.phone && !f.email) || phoneBad}>{t("Save person")}</Button></div>
      </form>
    </Card>
  );
}

function Lists({ lists, reload }) {
  const [name, setName] = useState("");
  const a = useAction();
  return (
    <Card title={t("Lists")} subtitle={t("Group people to send a campaign to them. Add people from the People tab.")}>
      <div className="row wrap"><input aria-label={t("New list name")} style={{ maxWidth: 320 }} placeholder={t("e.g. Newsletter")} value={name} onChange={(e) => setName(e.target.value)} /><Button variant="primary" busy={a.busy} disabled={!name.trim()} onClick={async () => { await a.run(() => api.post("/v1/lists", { name: name.trim() }), t("List created")); setName(""); reload(); }}>{t("Create list")}</Button></div>
      <ErrorBox error={a.error} />
      <Table rows={lists} emptyTitle={t("No lists yet")} empty={t("Create your first list above.")} cols={[{ label: t("Name"), render: (r) => <b>{r.name}</b> }]} />
    </Card>
  );
}

const CHECK = { no_consent: T("No. There's no recorded consent."), opted_out: T("No. They opted out of promotions.") };

function Consent() {
  const [f, setF] = useState({ channel: "sms", address: "", action: "opt_in", evidence: "" });
  const [check, setCheck] = useState(null);
  const a = useAction(), b = useAction();
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  return (
    <>
      <div className="help"><b>{t("Why this matters.")}</b> {t("Promotions may only go to people who agreed to receive them. Write down how and when they agreed. If you can't, don't message them. Codes and receipts (transactional) don't need this, but anyone who replied STOP is always blocked.")}</div>
      <div className="grid two">
        <Card title={t("Record a decision")}>
          <form className="form" onSubmit={async (e) => { e.preventDefault(); await a.run(() => api.post("/v1/consent", { ...f, source: "console", reason: "unsubscribe" }), f.action === "opt_in" ? t("Consent recorded") : t("Opt-out recorded")); setF({ ...f, address: "", evidence: "" }); }}>
            <div className="grid two">
              <Field label={t("Channel")}><select value={f.channel} onChange={set("channel")}><option value="sms">SMS</option><option value="email">{t("Email")}</option></select></Field>
              <Field label={t("They…")}><select value={f.action} onChange={set("action")}><option value="opt_in">{t("agreed to promotions")}</option><option value="opt_out">{t("asked to stop promotions")}</option></select></Field>
            </div>
            <Field label={f.channel === "sms" ? t("Phone number") : t("Email address")}><input required value={f.address} onChange={set("address")} placeholder={f.channel === "sms" ? "+355691234567" : "name@example.com"} /></Field>
            {f.action === "opt_in" && <Field label={t("How and when did they agree?")} hint={t("Kept as proof. For example: ticked the box on the website signup form, 14 Aug 2026")}><input required minLength={3} value={f.evidence} onChange={set("evidence")} /></Field>}
            <ErrorBox error={a.error} />
            <div><Button variant="primary" busy={a.busy}>{t("Save")}</Button></div>
          </form>
        </Card>
        <Card title={t("Check someone")}>
          <form className="form" onSubmit={async (e) => { e.preventDefault(); const r = await b.run(() => api.get("/v1/consent/check", { channel: f.channel, address: f.address, category: "marketing" })); if (r && r !== true) setCheck(r); }}>
            <small className="muted">{t("Uses the channel and address from the form on the left.")}</small>
            <ErrorBox error={b.error} />
            <div><Button busy={b.busy} disabled={!f.address}>{t("Can I send them promotions?")}</Button></div>
            {check && <div className={`alert ${check.allowed ? "good" : "warn"}`}><span>{check.allowed ? t("Yes, they agreed.") : CHECK[check.reason] ? t(CHECK[check.reason]) : `${t("No.")} ${check.reason.replace("blocked:", `${t("Blocked")}: `).replaceAll("_", " ")}.`}</span></div>}
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
      <Tabs value={tab} onChange={setTab} tabs={[{ id: "people", label: t("People") }, { id: "add", label: t("Add") }, { id: "import", label: t("Import") }, { id: "lists", label: t("Lists"), count: lists.data?.length }, { id: "consent", label: t("Consent") }]} />
      {tab === "people" && <People key={refresh} lists={lists.data || []} reloadLists={lists.reload} />}
      {tab === "add" && <AddOne onDone={() => setRefresh((n) => n + 1)} />}
      {tab === "import" && <Import onDone={() => setRefresh((n) => n + 1)} />}
      {tab === "lists" && <Lists lists={lists.data || []} reload={lists.reload} />}
      {tab === "consent" && <Consent />}
    </>
  );
}
