import { useEffect, useState } from "react";
import { api } from "../api.js";
import { T, t } from "../i18n.jsx";
import { Badge, Button, Card, ErrorBox, Field, Skeleton, Stat, Table, Time, money, smsInfo, useAction, useLoad, useUi, when } from "../ui.jsx";

const PAUSE = {
  budget_exhausted: T("Paused: the budget cap was reached. Nothing more was sent."),
  insufficient_funds: T("Paused: your wallet ran out. Top up, then resume."),
  account_disabled: T("Paused: your account can't send right now."),
  manual: T("Paused by you."),
};
const SKIPPED = {
  no_consent: T("No recorded consent"), opted_out: T("Opted out of promotions"),
  no_address: T("No phone/email for this channel"), recipient_suppressed: T("Opted out after the campaign was prepared"),
};

function Detail({ id, onBack }) {
  const { confirm } = useUi();
  const c = useLoad(() => api.get(`/v1/campaigns/${id}`), [id], 3000);
  const est = useLoad(() => api.get(`/v1/campaigns/${id}/estimate`).catch(() => null), [id]);
  const a = useAction();
  const d = c.data;
  if (!d) return <Card title={t("Campaign")}>{c.error ? <ErrorBox error={c.error} retry={c.reload} /> : <Skeleton />}</Card>;
  const act = (verb, msg, body) => a.run(async () => { await api.post(`/v1/campaigns/${id}/${verb}`, body); c.reload(); }, msg);
  const s = d.stats;
  const pre = ["draft", "scheduled"].includes(d.status);
  const sms = d.channel === "sms";
  return (
    <>
      <Card title={d.name} actions={<><Badge>{d.status}</Badge><Button onClick={onBack}>{t("← All campaigns")}</Button></>}>
        <dl className="kvlist">
          <dt>{t("Channel")}</dt><dd>{d.channel.toUpperCase()}</dd><dt>{t("Type")}</dt><dd>{t(d.category)}</dd>
          <dt>{t("Speed limit")}</dt><dd>{t("{n} per minute", { n: d.rate_per_minute })}</dd>{sms && <><dt>{t("Budget cap")}</dt><dd>{d.max_cost ? money(d.max_cost) : t("none")}</dd></>}
          <dt>{t("Started")}</dt><dd>{when(d.started_at)}</dd><dt>{t("Finished")}</dt><dd>{when(d.completed_at)}</dd>
        </dl>
        {d.pause_reason && <div className="alert warn"><span>{PAUSE[d.pause_reason] ? t(PAUSE[d.pause_reason]) : t("Paused: {reason}", { reason: d.pause_reason })}</span>{d.pause_reason === "insufficient_funds" && <a className="btn small primary" href="#wallet">{t("Top up")}</a>}</div>}
        <ErrorBox error={a.error} />
        <div className="row wrap">
          {d.status === "draft" && <Button variant="primary" busy={a.busy} onClick={async () => { if (await confirm({ title: t("Send this campaign now?"), body: est.data ? (sms ? t("It will go to {n} people and cost about {cost}. You can pause or cancel while it runs.", { n: est.data.recipients, cost: `${money(est.data.total)} ${est.data.currency || ""}` }) : t("It will go to {n} people. You can pause or cancel while it runs.", { n: est.data.recipients })) : t("You can pause or cancel while it runs."), confirmLabel: t("Send now") })) act("schedule", t("Campaign started"), {}); }}>{t("Send now")}</Button>}
          {d.status === "running" && <Button busy={a.busy} onClick={() => act("pause", t("Campaign paused"))}>{t("Pause")}</Button>}
          {d.status === "paused" && <Button variant="primary" busy={a.busy} onClick={() => act("resume", t("Campaign resumed"))}>{t("Resume")}</Button>}
          {!["completed", "cancelled"].includes(d.status) && <Button variant="danger" busy={a.busy} onClick={async () => { if (await confirm({ title: t("Cancel this campaign?"), body: t("Messages not yet sent are stopped and their cost returned. Messages already sent can't be recalled."), danger: true, confirmLabel: t("Cancel campaign") })) act("cancel", t("Campaign cancelled")); }}>{t("Cancel")}</Button>}
        </div>
      </Card>
      <div className="grid stats">
        {pre && est.data && <Stat label={t("Will reach")} value={est.data.recipients} sub={t("{n} left out (no consent, opted out or no address)", { n: est.data.excluded })} />}
        {pre && est.data && sms && <Stat label={t("Estimated cost")} value={`${money(est.data.total)} ${est.data.currency || ""}`} />}
        {Object.entries(s.recipients).map(([k, v]) => <Stat key={k} label={t("People {status}", { status: t(k) })} value={v} />)}
        <Stat label={t("Delivery rate")} value={s.delivery_rate == null ? "-" : `${(s.delivery_rate * 100).toFixed(1)}%`} tone={s.delivery_rate > 0.9 ? "good" : ""} />
        {sms && <Stat label={t("Charged for delivered")} value={money(s.cost.delivered)} sub={t("{a} in flight · {b} refunded", { a: money(s.cost.in_flight), b: money(s.cost.refunded) })} />}
      </div>
      <div className="grid two">
        <Card title={t("Message outcomes")}>{Object.keys(s.messages).length ? Object.entries(s.messages).map(([k, v]) => <div key={k} className="kv"><span><Badge>{k}</Badge></span><b>{v}</b></div>) : <div className="empty">{t("Nothing sent yet.")}</div>}</Card>
        <Card title={t("Left out")} subtitle={t("People who were skipped, and why")}>{Object.keys(s.skipped_reasons).length ? Object.entries(s.skipped_reasons).map(([k, v]) => <div key={k} className="kv"><span>{SKIPPED[k] ? t(SKIPPED[k]) : k.replace("blocked:", `${t("Blocked")}: `).replaceAll("_", " ")}</span><b>{v}</b></div>) : <div className="empty">{t("Nobody was skipped.")}</div>}</Card>
      </div>
    </>
  );
}

function Create({ onDone }) {
  const lists = useLoad(() => api.get("/v1/lists"), []);
  const senders = useLoad(() => api.get("/v1/sender-ids", { status: "approved" }), []);
  const domains = useLoad(() => api.get("/v1/email/domains").catch(() => []), []);
  const [f, setF] = useState({ name: "", channel: "sms", list_id: "", sender: "", text: "", subject: "", from_email: "", category: "marketing", max_cost: "", rate_per_minute: 300 });
  const a = useAction();
  const approved = [...new Set((senders.data || []).map((s) => s.value))];
  const verified = (domains.data || []).filter((d) => d.status === "verified");
  useEffect(() => { if (!f.sender && approved.length) setF((x) => ({ ...x, sender: approved[0] })); }, [approved.length]); // eslint-disable-line
  useEffect(() => { if (!f.from_email && verified.length) setF((x) => ({ ...x, from_email: `hello@${verified[0].domain}` })); }, [verified.length]); // eslint-disable-line
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const sms = f.channel === "sms";
  const info = smsInfo(f.text);
  const submit = async (e) => {
    e.preventDefault();
    const body = { name: f.name, channel: f.channel, list_id: Number(f.list_id), category: f.category, text: f.text, rate_per_minute: Number(f.rate_per_minute) };
    if (sms) { body.sender = f.sender; if (f.max_cost) body.max_cost = f.max_cost; } else { body.subject = f.subject; body.from_email = f.from_email; }
    const r = await a.run(() => api.post("/v1/campaigns", body), t("Draft saved. Review it, then send."));
    if (r && r.id) onDone(r.id);
  };
  const noLists = lists.data && lists.data.length === 0;
  return (
    <Card title={t("New campaign")} subtitle={t("Saved as a draft first. Nothing is sent until you press Send.")}>
      {noLists && <div className="help">{t("You need a contact list first.")} <a href="#contacts">{t("Create one on the Contacts page")}</a>.</div>}
      <form onSubmit={submit} className="form">
        <div className="grid two">
          <Field label={t("Name")} hint={t("Only you see this")}><input required value={f.name} onChange={set("name")} placeholder={t("Autumn sale")} /></Field>
          <Field label={t("Channel")}><select value={f.channel} onChange={set("channel")}><option value="sms">SMS</option><option value="email">{t("Email")}</option></select></Field>
          <Field label={t("Who receives it")}><select required value={f.list_id} onChange={set("list_id")}><option value="">{t("Choose a list…")}</option>{(lists.data || []).map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}</select></Field>
          <Field label={t("Type")} hint={t("Marketing only reaches people who agreed to promotions")}><select value={f.category} onChange={set("category")}><option value="marketing">{t("marketing")}</option><option value="transactional">{t("transactional")}</option></select></Field>
          {sms ? <Field label={t("From")} hint={approved.length ? t("Approved sender IDs") : t("You have no approved sender ID yet")}><select required value={f.sender} onChange={set("sender")}>{approved.map((s) => <option key={s}>{s}</option>)}</select></Field>
               : <Field label={t("From address")} hint={verified.length ? t("On your verified domain") : t("Verify a domain first")}><input required type="email" value={f.from_email} onChange={set("from_email")} /></Field>}
          {!sms && <Field label={t("Subject")}><input required value={f.subject} onChange={set("subject")} placeholder={t("{{first_name}}, your offer inside")} /></Field>}
        </div>
        <Field label={t("Message")} hint={t("Put {{first_name}} (or any contact field) where the name should go")}>
          <textarea required rows={4} value={f.text} onChange={set("text")} />
        </Field>
        {sms && <div className="meter"><span>{t("{n} characters", { n: info.length })} · {info.encoding}</span><span>{t("{n} part(s) per message", { n: info.segments })}</span></div>}
        <details className="adv"><summary>{t("Speed and budget")}</summary>
          <div className="grid two" style={{ marginTop: 12 }}>
            <Field label={t("Speed limit (messages per minute)")} hint={t("Lower is gentler on your audience and providers")}><input type="number" min="1" max="10000" value={f.rate_per_minute} onChange={set("rate_per_minute")} /></Field>
            {sms && <Field label={t("Budget cap")} hint={t("The campaign pauses before going over")}><input inputMode="decimal" placeholder={t("optional, e.g. 25.00")} value={f.max_cost} onChange={set("max_cost")} /></Field>}
          </div>
        </details>
        <ErrorBox error={a.error} />
        <div className="row"><Button variant="primary" busy={a.busy} disabled={noLists}>{t("Save draft")}</Button><Button type="button" onClick={() => onDone()}>{t("Cancel")}</Button></div>
      </form>
    </Card>
  );
}

export default function Campaigns() {
  const [view, setView] = useState({ mode: "list" });
  const list = useLoad(() => api.get("/v1/campaigns"), [view.mode], 5000);
  if (view.mode === "new") return <Create onDone={(id) => setView(id ? { mode: "detail", id } : { mode: "list" })} />;
  if (view.mode === "detail") return <Detail id={view.id} onBack={() => setView({ mode: "list" })} />;
  const rows = (list.data || []).map((c) => ({ ...c, _onClick: () => setView({ mode: "detail", id: c.id }) }));
  return (
    <Card title={t("Your campaigns")} actions={<Button variant="primary" onClick={() => setView({ mode: "new" })}>{t("New campaign")}</Button>}>
      <ErrorBox error={list.error} retry={list.reload} />
      <Table rows={rows} loading={list.loading} emptyTitle={t("No campaigns yet")} empty={t("A campaign sends one message to a whole list. Create a contact list, then come back here.")} emptyAction={<Button variant="primary" onClick={() => setView({ mode: "new" })}>{t("Create your first campaign")}</Button>}
        cols={[{ label: t("Name"), render: (r) => <b>{r.name}</b> }, { label: t("Channel"), render: (r) => r.channel.toUpperCase() }, { label: t("Status"), render: (r) => <Badge>{r.status}</Badge> }, { label: t("Type"), render: (r) => t(r.category) }, { label: t("Started"), render: (r) => (r.started_at ? <Time value={r.started_at} /> : "-") }]} />
    </Card>
  );
}
