import { useState } from "react";
import { api } from "../api.js";
import { T, t } from "../i18n.jsx";
import { Badge, Button, Card, ErrorBox, Field, Table, Tabs, Time, smsInfo, useAction, useLoad, useUi } from "../ui.jsx";

const COUNTRIES = [["AL", T("Albania")], ["XK", T("Kosovo")], ["MK", T("North Macedonia")], ["ME", T("Montenegro")], ["RS", T("Serbia")], ["GR", T("Greece")], ["IT", T("Italy")], ["DE", T("Germany")], ["AT", T("Austria")], ["CH", T("Switzerland")], ["FR", T("France")], ["GB", T("United Kingdom")], ["US", T("United States")], ["TR", T("Türkiye")]];
const STATUS_HELP = { pending: T("Waiting for our review, usually within one business day."), approved: T("Ready to use."), rejected: T("Not approved. See the reason, fix it and submit again."), revoked: T("No longer allowed. See the reason.") };

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
      <div className="help"><b>{t("What is a sender ID?")}</b> {t("It's the name people see instead of a phone number, like ACME. Use 3–11 letters or digits (at least one letter), or a phone number. Each country needs its own approval, and only your brand or business names are approved.")}</div>
      <Card title={t("Request a sender ID")}>
        <form className="form" onSubmit={async (e) => { e.preventDefault(); await a.run(() => api.post("/v1/sender-ids", { country: f.country, value: f.value.trim() }), t("Request sent for review")); setF({ ...f, value: "" }); ids.reload(); }}>
          <div className="grid two">
            <Field label={t("Country it will send to")}><select value={f.country} onChange={(e) => setF({ ...f, country: e.target.value })}>{COUNTRIES.map(([c, n]) => <option key={c} value={c}>{t(n)} ({c})</option>)}</select></Field>
            <Field label={t("Sender ID")} hint={t("For example your brand name")} error={bad ? t("3–11 letters/digits with at least one letter, or a phone number") : null}><input required maxLength={16} value={f.value} onChange={(e) => setF({ ...f, value: e.target.value })} placeholder="ACME" /></Field>
          </div>
          <ErrorBox error={a.error} />
          <div><Button variant="primary" busy={a.busy} disabled={!f.value || bad}>{t("Submit for approval")}</Button></div>
        </form>
      </Card>
      <Card title={t("Your sender IDs")}>
        <ErrorBox error={ids.error} retry={ids.reload} />
        <Table rows={ids.data || []} loading={ids.loading} emptyTitle={t("No sender IDs yet")} empty={t("Request one above. You can't send SMS until one is approved.")}
          cols={[{ label: t("Sender"), render: (r) => <b>{r.value}</b> }, { label: t("Country"), key: "country" }, { label: t("Status"), render: (r) => <><Badge>{r.status}</Badge> <small className="muted">{STATUS_HELP[r.status] ? t(STATUS_HELP[r.status]) : ""}</small></> }, { label: t("Reason"), render: (r) => r.reason || "" }, { label: t("Requested"), render: (r) => <Time value={r.created_at} /> },
            { label: "", render: (r) => ["rejected", "revoked"].includes(r.status) && <Button className="small" onClick={async () => { if (await confirm({ title: t("Submit again?"), body: t("It will go back into the review queue."), confirmLabel: t("Submit again") })) { await a.run(() => api.post("/v1/sender-ids", { country: r.country, value: r.value }), t("Submitted again")); ids.reload(); } }}>{t("Submit again")}</Button> }]} />
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
      <div className="help"><b>{t("Templates")}</b> {t("are messages with blanks, like")} <code>{t("Your code is {{code}}")}</code>. {t("We approve the wording once; after that you send it as often as you like, filling in the blanks. Changing an approved template creates a new version that needs approval again, and the old version keeps working meanwhile.")}</div>
      <Card title={editing ? t("New version of “{name}”", { name: editing.name }) : t("Create a template")} actions={editing && <Button onClick={() => { setEditing(null); setF({ name: "", body: "" }); }}>{t("Cancel")}</Button>}>
        <form className="form" onSubmit={async (e) => {
          e.preventDefault();
          await a.run(() => (editing ? api.post(`/v1/templates/${editing.id}/versions`, { body: f.body }) : api.post("/v1/templates", { name: f.name, body: f.body })), t("Sent for review"));
          setF({ name: "", body: "" }); setEditing(null); list.reload();
        }}>
          {!editing && <Field label={t("Name")} hint={t("Only you see this")}><input required maxLength={64} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder={t("Login code")} /></Field>}
          <Field label={t("Message")} error={malformed ? t("A blank is written {{name}} using lowercase letters, digits or _") : null}><textarea required rows={3} value={f.body} onChange={(e) => setF({ ...f, body: e.target.value })} placeholder={t("Your code is {{code}}. It expires in 10 minutes.")} /></Field>
          <div className="row wrap"><small className="muted">{t("Insert a blank:")}</small>{["first_name", "last_name", "code", "order_id", "amount"].map((v) => <Button key={v} type="button" className="small" onClick={() => insert(v)}>{`{{${v}}}`}</Button>)}</div>
          <div className="meter"><span>{vars.length ? t("Blanks: {list}", { list: vars.join(", ") }) : t("No blanks yet")}</span><span>≈ {t("{n} part(s) with 8-character values", { n: info.segments })}</span></div>
          <ErrorBox error={a.error} />
          <div><Button variant="primary" busy={a.busy} disabled={!f.body.trim() || (!editing && !f.name.trim()) || malformed}>{t("Submit for approval")}</Button></div>
        </form>
      </Card>
      <Card title={t("Your templates")}>
        <ErrorBox error={list.error} retry={list.reload} />
        {list.loading && !list.data ? <Table loading rows={[]} cols={[]} /> : (list.data || []).length === 0 ? <div className="empty">{t("No templates yet.")}</div> : (list.data || []).map((tp) => (
          <div key={tp.id} className="tpl">
            <div className="row wrap"><b>{tp.name}</b><span className="muted small">{t("{n} version(s)", { n: tp.versions.length })}</span><span style={{ flex: 1 }} /><Button className="small" onClick={() => { setEditing(tp); setF({ name: tp.name, body: tp.versions[0]?.body || "" }); scrollTo({ top: 0, behavior: "smooth" }); }}>{t("New version")}</Button></div>
            <Table rows={tp.versions} cols={[{ label: t("Version"), render: (v) => `v${v.version}` }, { label: t("Status"), render: (v) => <Badge>{v.status}</Badge> }, { label: t("Wording"), render: (v) => <code>{v.body}</code> }, { label: t("Blanks"), render: (v) => v.variables.join(", ") || "-" }, { label: t("Note"), render: (v) => v.reason || "" }]} />
          </div>
        ))}
      </Card>
    </>
  );
}

export default function Senders() {
  const [tab, setTab] = useState("ids");
  return (<><Tabs value={tab} onChange={setTab} tabs={[{ id: "ids", label: t("Sender IDs") }, { id: "templates", label: t("Templates") }]} />{tab === "ids" ? <SenderIds /> : <Templates />}</>);
}
