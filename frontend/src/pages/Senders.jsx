import { useState } from "react";
import { api } from "../api.js";
import { t } from "../i18n.jsx";
import { Badge, Button, Card, ErrorBox, Field, Table, Tabs, smsInfo, useAction, useLoad } from "../ui.jsx";
import { SenderDetail, SenderList } from "../senders/components.jsx";

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

export default function Senders({ me, rest = [] }) {
  const [tab, setTab] = useState("ids");
  if (/^\d+$/.test(rest[0] || "")) return <SenderDetail me={me} id={rest[0]} />; // #senders/<id>
  return (<><Tabs value={tab} onChange={setTab} tabs={[{ id: "ids", label: t("Sender IDs") }, { id: "templates", label: t("Templates") }]} />{tab === "ids" ? <SenderList me={me} /> : <Templates />}</>);
}
