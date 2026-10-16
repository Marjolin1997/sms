import { useEffect, useRef, useState } from "react";
import { buildHash, useHashState } from "../messaging/hashstate.js";
import { api } from "../api.js";
import { T, t } from "../i18n.jsx";
import { Badge, Button, Card, ErrorBox, ErrorState, Field, Skeleton, Table, Tabs, Time, useAction, useDebounced, useLoad, useUi } from "../ui.jsx";

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
      if (c === "phone" || c === "mobile") r.phone = v.replace(/[\s().-]/g, ""); // vetëm formatim i padëmshëm; vlefshmërinë e vendos serveri
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

const BATCH = 1000; // kufiri i serverit për një kërkesë importi
const MAX_ROWS = 10000; // kufi i klientit: importet më të mëdha ndahen në skedarë
const SHOWN_ERRORS = 50;

// Importi: kërkesat shkojnë në grupe; gjendjet janë të dukshme (zgjedhur → duke u importuar → përfunduar / pjesërisht / dështoi).
function Import({ onDone }) {
  const [text, setText] = useState("");
  const file = useRef(null);
  const [fileName, setFileName] = useState("");
  const parsed = parseContacts(text);
  const rows = parsed.rows.slice(0, MAX_ROWS);
  const [run, setRun] = useState({ phase: "idle" }); // idle | importing | done | failed
  const batches = Math.max(1, Math.ceil(rows.length / BATCH));
  const start = async () => {
    const acc = { created: 0, updated: 0, rejected: 0, errors: [] };
    setRun({ phase: "importing", batch: 0, ...acc });
    try {
      for (let i = 0; i < batches; i++) {
        setRun((r) => ({ ...r, batch: i + 1 }));
        const r = await api.post("/v1/contacts/import", { contacts: rows.slice(i * BATCH, (i + 1) * BATCH) });
        acc.created += r.created; acc.updated += r.updated; acc.rejected += r.errors.length;
        for (const e of r.errors) if (acc.errors.length < SHOWN_ERRORS) acc.errors.push({ row: e.row + i * BATCH, message: e.message });
        setRun({ phase: "importing", batch: i + 1, ...acc, errors: [...acc.errors] });
      }
      setRun({ phase: "done", ...acc });
    } catch (e) { setRun({ phase: "failed", error: e, ...acc }); }
    onDone();
  };
  const busy = run.phase === "importing";
  const imported = (run.created || 0) + (run.updated || 0);
  return (
    <Card title={t("Import contacts")} subtitle={t("Paste rows or choose a CSV file. Columns: phone, email, first name, last name.")}>
      <div className="help">{t("Importing people does not mean they agreed to marketing.")} {t("Record consent under the Consent tab, or they'll be left out of promotions.")}</div>
      <div className="form">
        <div className="row wrap">
          <input ref={file} type="file" accept=".csv,.txt,text/csv,text/plain" hidden aria-label={t("Choose CSV file")} onChange={async (e) => { const f = e.target.files[0]; if (f) { setText(await f.text()); setFileName(f.name); setRun({ phase: "idle" }); } e.target.value = ""; }} />
          <Button disabled={busy} onClick={() => file.current.click()}>{t("Choose CSV file")}</Button>
          {fileName && <small className="muted">{t("Selected: {name}", { name: fileName })}</small>}
          <small className="muted">{t("Phone numbers need the country code. The server checks every row.")}</small>
        </div>
        <Field label={t("Or paste here")}><textarea rows={5} value={text} disabled={busy} onChange={(e) => { setText(e.target.value); setFileName(""); setRun({ phase: "idle" }); }} placeholder={"phone,email,first_name,last_name\n+355691234567,ana@example.com,Ana,Hoxha"} /></Field>
        {text.trim() && run.phase === "idle" && <div className="quote"><span><b>{rows.length}</b> {t("people found")}</span>{parsed.skipped > 0 && <span className="muted">{t("{n} line(s) without a phone or email will be ignored", { n: parsed.skipped })}</span>}{parsed.rows.length > MAX_ROWS && <span>{t("Only the first {n} will be imported this time", { n: MAX_ROWS })}</span>}{batches > 1 && <span className="muted">{t("Sent in {n} batches", { n: batches })}</span>}</div>}
        <div aria-live="polite" role="status">
          {busy && <div className="alert info"><span>{t("Importing… batch {a} of {b}. Don't close this page.", { a: run.batch, b: batches })}</span></div>}
          {run.phase === "done" && (
            <div className={`alert ${run.rejected && !imported ? "bad" : run.rejected ? "warn" : "good"}`}>
              <span><b>{run.created}</b> {t("added")}, <b>{run.updated}</b> {t("updated")}{run.rejected > 0 && <>, <b>{run.rejected}</b> {t("rows were rejected.")}</>}{" "}
                {run.rejected && !imported ? t("Nothing was imported.") : run.rejected ? t("Partly imported: fix the rejected rows and import them again.") : t("All rows were imported.")}</span>
            </div>
          )}
          {run.phase === "failed" && <div className="alert bad"><span>{t("The import stopped.")} {imported > 0 ? t("{n} people were imported before it stopped.", { n: imported }) : t("Nothing was imported.")}</span></div>}
        </div>
        {run.phase === "failed" && <ErrorBox error={run.error} />}
        {run.errors?.length > 0 && <details open={run.phase !== "idle"}><summary>{t("See rejected rows")} {run.rejected > run.errors.length ? t("(first {n} shown)", { n: run.errors.length }) : ""}</summary><ul>{run.errors.map((e) => <li key={e.row}>{t("Row {n}", { n: e.row + 1 })}: {e.message}</li>)}</ul></details>}
        <div><Button variant="primary" busy={busy} disabled={!rows.length} onClick={start}>{t("Import {n} people", { n: rows.length || "" })}</Button></div>
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

function ContactTable({ lists, reloadLists, listId, setListId, q, setQ, extraActions, onRemoveFromList }) {
  const { confirm } = useUi();
  const dq = useDebounced(q);
  const [st, setSt] = useState({ items: [], loading: true, error: null, more: false });
  const [sel, setSel] = useState(new Set());
  const [target, setTarget] = useState("");
  const seq = useRef(0);
  const a = useAction();
  const load = async (after = 0) => {
    const me = ++seq.current; // përgjigjet e vjetra mbishkruhen kurrë
    setSt((s) => ({ ...s, loading: true, error: null }));
    try {
      const r = await api.get("/v1/contacts", { q: dq, list_id: listId, after_id: after || undefined, limit: 51 });
      if (me !== seq.current) return;
      setSt((s) => ({ items: after ? [...s.items, ...r.slice(0, 50)] : r.slice(0, 50), loading: false, error: null, more: r.length > 50 }));
    } catch (e) { if (me === seq.current) setSt((s) => ({ ...s, loading: false, error: e })); }
  };
  useEffect(() => { setSel(new Set()); load(); }, [dq, listId]); // eslint-disable-line
  const toggle = (id) => setSel((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const rows = st.items;
  const all = rows.length > 0 && rows.every((r) => sel.has(r.id));
  const filtering = q || listId;
  return (
    <>
      <div className="toolbar" role="search" aria-label={t("Filter people")}>
        <input aria-label={t("Search people")} placeholder={t("Search name, phone or email")} value={q} onChange={(e) => setQ(e.target.value)} />
        <select aria-label={t("Filter by list")} value={listId} onChange={(e) => setListId(e.target.value)}><option value="">{t("All people")}</option>{lists.map((l) => <option key={l.id} value={l.id}>{t("In list: {name}", { name: l.name })}</option>)}</select>
        {filtering && <Button onClick={() => { setQ(""); setListId(""); }}>{t("Clear filters")}</Button>}
        <select aria-label={t("Add selected to list")} value={target} onChange={(e) => setTarget(e.target.value)}><option value="">{sel.size ? t("Add {n} to list…", { n: sel.size }) : t("Add selected to list…")}</option>{lists.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}</select>
        <Button busy={a.busy} disabled={!target || !sel.size} onClick={async () => { await a.run(() => api.post(`/v1/lists/${target}/members`, { contact_ids: [...sel] }), t("Added {n} to the list", { n: sel.size })); setSel(new Set()); setTarget(""); reloadLists(); }}>{t("Add")}</Button>
        {extraActions}
      </div>
      <ErrorBox error={st.error || a.error} retry={() => load()} />
      <Table rows={rows} loading={st.loading} emptyTitle={filtering ? t("No matches") : t("No contacts yet")} empty={filtering ? t("Try a different search.") : t("Import a file or add someone from the Add tab.")}
        footer={st.more && <div className="row end" style={{ padding: 12 }}><Button busy={st.loading} onClick={() => load(rows[rows.length - 1].id)}>{t("Load more")}</Button></div>}
        cols={[{ label: <input type="checkbox" aria-label={t("Select all")} checked={all} onChange={() => setSel(all ? new Set() : new Set(rows.map((r) => r.id)))} />, render: (r) => <input type="checkbox" aria-label={t("Select {who}", { who: r.first_name || r.phone || r.email })} checked={sel.has(r.id)} onChange={() => toggle(r.id)} /> },
          { label: t("Name"), render: (r) => <b>{[r.first_name, r.last_name].filter(Boolean).join(" ") || "-"}</b> }, { label: t("Phone"), render: (r) => (r.phone ? `+${r.phone}` : "-") }, { label: t("Email"), hideSm: true, render: (r) => r.email || "-" }, { label: t("Added"), hideSm: true, render: (r) => <Time value={r.created_at} /> },
          { label: "", render: (r) => <div className="row wrap">
            {onRemoveFromList && <Button className="small" onClick={async () => { if (await a.run(() => api.del(`/v1/lists/${listId}/members/${r.id}`), t("Removed from the list"))) { load(); reloadLists(); } }}>{t("Remove from list")}</Button>}
            <Button className="small" onClick={async () => { const d = await a.run(() => api.get(`/v1/contacts/${r.id}/export`)); if (d && d !== true) download(`contact-${r.id}.json`, d); }}>{t("Export data")}</Button>
            <Button variant="danger" className="small" onClick={async () => { if (await confirm({ title: t("Erase this person?"), body: t("Their name, phone, email and attributes are permanently removed (GDPR). We keep only an anonymous block so they can't be messaged again by mistake."), danger: true, confirmLabel: t("Erase permanently") })) { await a.run(() => api.del(`/v1/contacts/${r.id}`), t("Person erased")); load(); reloadLists(); } }}>{t("Erase")}</Button></div> }]} />
    </>
  );
}

function People({ lists, reloadLists, hs }) {
  const q = hs.query.q || "", listId = hs.query.list || "";
  const [draft, setDraft] = useState(q);
  useEffect(() => { setDraft(q); }, [q]);
  const dq = useDebounced(draft);
  useEffect(() => { if (dq.trim() !== q) hs.go([], { tab: "people", q: dq.trim(), list: listId }); }, [dq]); // eslint-disable-line
  const keep = { tab: "people" };
  return (
    <Card title={t("People")}>
      <ContactTable lists={lists} reloadLists={reloadLists} listId={listId} q={draft} setQ={(v) => { setDraft(v); }} setListId={(v) => hs.go([], { ...keep, q: draft, list: v })} />
    </Card>
  );
}

function AddOne({ onDone }) {
  const [f, setF] = useState({ first_name: "", last_name: "", phone: "", email: "" });
  const a = useAction();
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  return (
    <Card title={t("Add one person")}>
      <form className="form" onSubmit={async (e) => { e.preventDefault(); const body = { ...f, phone: f.phone ? f.phone.replace(/[\s().-]/g, "") : null, email: f.email || null }; const r = await a.run(() => api.post("/v1/contacts", body), t("Saved")); if (r) { setF({ first_name: "", last_name: "", phone: "", email: "" }); onDone(); } }}>
        <div className="grid two">
          <Field label={t("First name")}><input value={f.first_name} onChange={set("first_name")} /></Field>
          <Field label={t("Last name")}><input value={f.last_name} onChange={set("last_name")} /></Field>
          <Field label={t("Phone")} hint={t("With country code")} error={a.error?.fields?.phone}><input inputMode="tel" value={f.phone} onChange={set("phone")} placeholder="+355691234567" /></Field>
          <Field label={t("Email")} error={a.error?.fields?.email}><input type="email" value={f.email} onChange={set("email")} /></Field>
        </div>
        <small className="muted">{t("A phone number or an email is required. Adding someone who already exists updates them.")}</small>
        <ErrorBox error={a.error} />
        <div><Button variant="primary" busy={a.busy} disabled={!f.phone && !f.email}>{t("Save person")}</Button></div>
      </form>
    </Card>
  );
}

function Lists({ lists, listsState, reload, hs }) {
  const [name, setName] = useState("");
  const a = useAction();
  const openId = hs.query.list_detail || "";
  const open = lists.find((l) => String(l.id) === openId);
  return (
    <>
      <Card title={t("Lists")} subtitle={t("Group people to send a campaign to them. Add people from the People tab.")}>
        <form className="row wrap" onSubmit={async (e) => { e.preventDefault(); await a.run(() => api.post("/v1/lists", { name: name.trim() }), t("List created")); setName(""); reload(); }}>
          <input aria-label={t("New list name")} style={{ maxWidth: 320 }} placeholder={t("e.g. Newsletter")} value={name} onChange={(e) => setName(e.target.value)} maxLength={64} />
          <Button variant="primary" busy={a.busy} disabled={!name.trim()}>{t("Create list")}</Button>
        </form>
        <ErrorBox error={a.error} />
        {listsState.error && !listsState.data ? <ErrorState error={listsState.error} retry={reload} /> : (
          <Table rows={lists.map((l) => ({ ...l, _onClick: () => hs.go([], { tab: "lists", list_detail: String(l.id) }) }))} loading={listsState.loading} emptyTitle={t("No lists yet")} empty={t("Create your first list above.")}
            cols={[{ label: t("Name"), render: (r) => <b>{r.name}</b> }, { label: t("People"), num: true, render: (r) => r.member_count ?? "-" }, { label: t("Created"), hideSm: true, render: (r) => (r.created_at ? <Time value={r.created_at} /> : "-") },
              { label: "", render: (r) => <Button className="small" aria-label={t("Open list {name}", { name: r.name })} onClick={() => hs.go([], { tab: "lists", list_detail: String(r.id) })}>{t("Open")}</Button> }]} />
        )}
      </Card>
      {open && <ListDetail key={open.id} list={open} lists={lists} reload={reload} hs={hs} />}
    </>
  );
}

function Audience({ listId }) {
  const sms = useLoad((signal) => api.get(`/v1/lists/${listId}/audience`, { channel: "sms", category: "marketing" }, { signal }).catch(() => null), [listId]);
  if (!sms.data) return null;
  return <div className="quote" aria-live="polite"><span><b>{sms.data.eligible}</b> {t("can receive SMS promotions")}</span>{Object.entries(sms.data.excluded || {}).map(([k, v]) => <span key={k} className="muted">{v} · {k.replace("blocked:", "").replaceAll("_", " ")}</span>)}</div>;
}

function ListDetail({ list, lists, reload, hs }) {
  const [q, setQ] = useState("");
  return (
    <Card title={list.name} subtitle={t("{n} people in this list", { n: list.member_count ?? 0 })} actions={<Button onClick={() => hs.go([], { tab: "lists" })}>{t("Close")}</Button>}>
      <Audience listId={list.id} />
      <ContactTable lists={lists} reloadLists={reload} listId={String(list.id)} setListId={() => {}} q={q} setQ={setQ} onRemoveFromList />
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
  const hs = useHashState("contacts");
  const tabs = ["people", "add", "import", "lists", "consent"];
  const tab = tabs.includes(hs.query.tab) ? hs.query.tab : "people";
  const lists = useLoad((signal) => api.get("/v1/lists", {}, { signal }), []);
  const [refresh, setRefresh] = useState(0);
  return (
    <>
      <Tabs value={tab} onChange={(v) => hs.go([], v === "people" ? {} : { tab: v })} tabs={[{ id: "people", label: t("People") }, { id: "add", label: t("Add") }, { id: "import", label: t("Import") }, { id: "lists", label: t("Lists"), count: lists.data?.length }, { id: "consent", label: t("Consent") }]} />
      {tab === "people" && <People key={refresh} lists={lists.data || []} reloadLists={lists.reload} hs={hs} />}
      {tab === "add" && <AddOne onDone={() => { setRefresh((n) => n + 1); lists.reload(); }} />}
      {tab === "import" && <Import onDone={() => { setRefresh((n) => n + 1); lists.reload(); }} />}
      {tab === "lists" && <Lists lists={lists.data || []} listsState={lists} reload={lists.reload} hs={hs} />}
      {tab === "consent" && <Consent />}
    </>
  );
}
