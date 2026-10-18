import { useEffect, useState } from "react";
import { api } from "../api.js";
import { T, t } from "../i18n.jsx";
import { AuditTimeline, Badge, Button, Card, ErrorBox, ErrorState, Field, Skeleton, Stat, Table, Tabs, Time, money, smsInfo, useAction, useDebounced, useLoad, useUi, when } from "../ui.jsx";
import { CAMPAIGN_STATUSES, browserZone, failureInfo, instantFrom, localText, utcOf, utcText } from "../messaging/format.js";
import { buildHash, useHashState } from "../messaging/hashstate.js";

const PAGE = 25;
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
const ACTIVITY = {
  "campaign.create": T("Campaign created"), "campaign.schedule": T("Scheduled / started"), "campaign.pause": T("Paused"),
  "campaign.resume": T("Resumed"), "campaign.cancel": T("Cancelled"),
};
const RECIPIENT_STATUSES = ["pending", "queued", "skipped", "cancelled"];
const skippedText = (r) => (SKIPPED[r] ? t(SKIPPED[r]) : r ? r.replace("blocked:", `${t("Blocked")}: `).replaceAll("_", " ") : "");

// ---- Orari: zonë e shprehur; kurrë zhvendosje e heshtur ----
export const emptySchedule = () => ({ mode: "now", date: "", time: "09:00", zone: "local" });
export function scheduleResult(s, now = Date.now()) {
  if (s.mode === "now") return { ok: true, iso: null, error: null };
  const d = instantFrom(s.date, s.time, s.zone);
  if (!d) return { ok: false, iso: null, error: t("Choose a date and a time.") };
  if (d.getTime() <= now) return { ok: false, iso: null, error: t("That time has already passed. Choose a later time.") };
  return { ok: true, iso: d.toISOString(), error: null, date: d };
}

export function ScheduleField({ value, onChange }) {
  const r = scheduleResult(value);
  const set = (k) => (e) => onChange({ ...value, [k]: e.target.value });
  return (
    <fieldset className="schedule">
      <legend>{t("When to send")}</legend>
      <div className="row wrap">
        <label className="check"><input type="radio" name="when" checked={value.mode === "now"} onChange={() => onChange({ ...value, mode: "now" })} /> {t("Send now")}</label>
        <label className="check"><input type="radio" name="when" checked={value.mode === "later"} onChange={() => onChange({ ...value, mode: "later" })} /> {t("Schedule for later")}</label>
      </div>
      {value.mode === "later" && (
        <>
          <div className="grid three">
            <Field label={t("Date")}><input type="date" value={value.date} onChange={set("date")} /></Field>
            <Field label={t("Time")}><input type="time" value={value.time} onChange={set("time")} /></Field>
            <Field label={t("Time zone")}>
              <select value={value.zone} onChange={set("zone")}><option value="local">{t("My time zone ({zone})", { zone: browserZone() })}</option><option value="utc">UTC</option></select>
            </Field>
          </div>
          {r.error && value.date ? <div className="alert warn" role="alert"><span>{r.error}</span></div> : null}
          {r.ok && <div className="help" aria-live="polite">{t("It will be sent at {local}, which is {utc}.", { local: localText(r.date), utc: utcText(r.date) })}</div>}
        </>
      )}
    </fieldset>
  );
}

// ---------------------------------------------------------------- Lista
function List({ hs }) {
  const { query } = hs;
  const status = query.status || "", name = query.q || "", before = query.before || "";
  const [draft, setDraft] = useState(name);
  const dq = useDebounced(draft);
  useEffect(() => { if (dq.trim() !== name) hs.go([], { ...query, q: dq.trim(), before: "" }); }, [dq]); // eslint-disable-line
  const key = JSON.stringify({ status, name, before });
  const list = useLoad(async (signal) => ({ key, rows: await api.get("/v1/campaigns", { status, q: name, before_id: before || undefined, limit: PAGE + 1 }, { signal }) }), [key], 8000);
  const rows = list.data ? list.data.rows : null;
  const shown = rows ? rows.slice(0, PAGE) : [];
  const more = rows ? rows.length > PAGE : false;
  const filtering = !!(status || name);
  const pending = list.data && list.data.key !== key;
  return (
    <Card title={t("Your campaigns")} actions={<a className="btn primary" href={buildHash("campaigns", ["new"])}>{t("New campaign")}</a>}>
      <div className="toolbar" role="search" aria-label={t("Filter campaigns")}>
        <input aria-label={t("Search campaigns")} placeholder={t("Search by name")} value={draft} onChange={(e) => setDraft(e.target.value)} />
        <select aria-label={t("Filter by status")} value={status} onChange={(e) => hs.go([], { ...query, status: e.target.value, before: "" })}>
          <option value="">{t("All statuses")}</option>{CAMPAIGN_STATUSES.map((s) => <option key={s} value={s}>{t("Status: {status}", { status: t(s) })}</option>)}
        </select>
        {filtering && <Button onClick={() => { setDraft(""); hs.go([], {}); }}>{t("Clear filters")}</Button>}
      </div>
      {list.error && !rows ? <ErrorState error={list.error} retry={list.reload} /> : list.loading && !rows ? <Skeleton rows={5} /> : (
        <div aria-busy={pending || undefined} style={{ opacity: pending ? 0.6 : 1 }}>
          {list.stale && <ErrorBox error={list.error} retry={list.reload} />}
          <Table rows={shown.map((c) => ({ ...c, _onClick: () => hs.go([String(c.id)]) }))} emptyTitle={filtering ? t("No campaigns match") : t("No campaigns yet")}
            empty={filtering ? t("Try different filters.") : t("A campaign sends one message to a whole list. Create a contact list, then come back here.")}
            emptyAction={filtering ? <Button onClick={() => { setDraft(""); hs.go([], {}); }}>{t("Clear filters")}</Button> : <a className="btn primary" href={buildHash("campaigns", ["new"])}>{t("Create your first campaign")}</a>}
            cols={[
              { label: t("Campaign"), render: (r) => <a href={buildHash("campaigns", [String(r.id)])} aria-label={t("Open campaign {name}", { name: r.name })}><b>{r.name}</b></a> },
              { label: t("Sender"), hideSm: true, render: (r) => (r.channel === "sms" ? r.sender : t("Email")) },
              { label: t("Status"), render: (r) => <Badge>{r.status}</Badge> },
              { label: t("Scheduled / started"), hideSm: true, render: (r) => (r.started_at ? <Time value={r.started_at} /> : r.scheduled_at ? <Time value={r.scheduled_at} /> : "-") },
              { label: t("Recipients"), num: true, render: (r) => r.recipients_total ?? "-" },
              { label: t("Delivered"), num: true, hideSm: true, render: (r) => r.message_counts?.delivered ?? 0 },
              { label: t("Failed"), num: true, hideSm: true, render: (r) => (r.message_counts?.failed ?? 0) + (r.message_counts?.bounced ?? 0) },
              { label: t("Waiting"), num: true, hideSm: true, render: (r) => r.recipient_counts?.pending ?? 0 },
            ]} />
          {(before || more) && (
            <nav className="pager" aria-label={t("Pagination")}>
              <Button disabled={!before} onClick={() => hs.go([], { ...query, before: "" })}>{t("Newest")}</Button>
              <Button disabled={!more} onClick={() => hs.go([], { ...query, before: String(shown[shown.length - 1].id) })}>{t("Older")}</Button>
            </nav>
          )}
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------- Krijimi (hapa)
const STEPS = [T("Details"), T("Recipients"), T("Message"), T("Schedule"), T("Review")];

function Create({ hs }) {
  const { confirm } = useUi();
  const lists = useLoad((signal) => api.get("/v1/lists", {}, { signal }), []);
  const senders = useLoad((signal) => api.get("/v1/sender-ids", { status: "approved" }, { signal }), []);
  const templates = useLoad((signal) => api.get("/v1/templates", { status: "approved" }, { signal }).catch(() => []), []);
  const domains = useLoad((signal) => api.get("/v1/email/domains", {}, { signal }).catch(() => []), []);
  const [step, setStep] = useState(0);
  const [f, setF] = useState({ name: "", channel: "sms", category: "marketing", sender: "", from_email: "", list_id: "", mode: "text", text: "", template_id: "", subject: "", max_cost: "", rate_per_minute: 300 });
  const [sch, setSch] = useState(emptySchedule());
  const [draft, setDraft] = useState(null); // draft i ruajtur nga serveri (nëse planifikimi dështoi pas krijimit)
  const a = useAction();
  const approved = [...new Set((senders.data || []).map((s) => s.value))];
  const verified = (domains.data || []).filter((d) => d.status === "verified");
  useEffect(() => { if (!f.sender && approved.length) setF((x) => ({ ...x, sender: approved[0] })); }, [approved.length]); // eslint-disable-line
  useEffect(() => { if (!f.from_email && verified.length) setF((x) => ({ ...x, from_email: `hello@${verified[0].domain}` })); }, [verified.length]); // eslint-disable-line
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const sms = f.channel === "sms";
  const info = smsInfo(f.text);
  const audience = useLoad((signal) => (f.list_id ? api.get(`/v1/lists/${f.list_id}/audience`, { channel: f.channel, category: f.category }, { signal }).catch(() => null) : Promise.resolve(null)), [f.list_id, f.channel, f.category]);
  const wallets = useLoad((signal) => api.get("/v1/wallets", {}, { signal }).catch(() => []), []);
  const list = (lists.data || []).find((l) => String(l.id) === String(f.list_id));
  const tpl = (templates.data || []).find((x) => String(x.id) === String(f.template_id));
  const sched = scheduleResult(sch);
  const valid = [
    f.name.trim() && (sms ? f.sender : f.from_email),
    f.list_id,
    f.mode === "template" && sms ? f.template_id : f.text.trim() && (sms || f.subject.trim()),
    sched.ok && (sch.mode === "now" || sch.date),
  ];
  const submit = async (action) => {
    if (a.busy) return;
    const body = { name: f.name.trim(), channel: f.channel, list_id: Number(f.list_id), category: f.category, rate_per_minute: Number(f.rate_per_minute) };
    if (f.mode === "template" && sms) body.template_id = Number(f.template_id); else body.text = f.text;
    if (sms) { body.sender = f.sender; if (f.max_cost) body.max_cost = f.max_cost; } else { body.subject = f.subject; body.from_email = f.from_email; }
    if (action !== "draft" && sch.mode === "now" && !(await confirm({ title: t("Send this campaign now?"), body: t("Messages start going out right away. You can pause or cancel while it runs; messages already sent can't be recalled."), confirmLabel: t("Send now") }))) return;
    const r = await a.run(async () => {
      const c = draft || (await api.post("/v1/campaigns", body));
      setDraft(c);
      if (action !== "draft") await api.post(`/v1/campaigns/${c.id}/schedule`, sch.mode === "later" ? { scheduled_at: sched.iso } : {});
      return c;
    });
    if (r && r.id) hs.go([String(r.id)]);
  };
  const noLists = lists.data && lists.data.length === 0;
  const last = step === STEPS.length - 1;
  return (
    <Card title={t("New campaign")} subtitle={t("Saved as a draft first. Nothing is sent until you choose to send or schedule it.")} actions={<a className="btn" href={buildHash("campaigns")}>{t("← All campaigns")}</a>}>
      {noLists && <div className="help">{t("You need a contact list first.")} <a href="#contacts">{t("Create one on the Contacts page")}</a>.</div>}
      <ol className="steps" aria-label={t("Steps")}>{STEPS.map((s, i) => <li key={s} className={i === step ? "on" : i < step ? "done" : ""} aria-current={i === step ? "step" : undefined}>{t(s)}</li>)}</ol>
      <form className="form" onSubmit={(e) => { e.preventDefault(); if (last) submit(sched.ok && (sch.mode === "now" || sch.date) ? "send" : "draft"); else setStep(step + 1); }}>
        {step === 0 && (
          <div className="grid two">
            <Field label={t("Name")} hint={t("Only you see this")}><input required value={f.name} onChange={set("name")} maxLength={80} placeholder={t("Autumn sale")} /></Field>
            <Field label={t("Channel")}><select value={f.channel} onChange={set("channel")}><option value="sms">SMS</option><option value="email">{t("Email")}</option></select></Field>
            {sms ? (
              <Field label={t("From")} hint={approved.length ? t("Approved sender IDs") : t("You have no approved sender ID yet")}>
                <select required value={f.sender} onChange={set("sender")}>{approved.length === 0 && <option value="">{t("No approved sender ID")}</option>}{approved.map((s) => <option key={s}>{s}</option>)}</select>
              </Field>
            ) : <Field label={t("From address")} hint={verified.length ? t("On your verified domain") : t("Verify a domain first")}><input required type="email" value={f.from_email} onChange={set("from_email")} /></Field>}
            <Field label={t("Type")} hint={t("Marketing only reaches people who agreed to promotions")}><select value={f.category} onChange={set("category")}><option value="marketing">{t("marketing")}</option><option value="transactional">{t("transactional")}</option></select></Field>
          </div>
        )}
        {step === 1 && (
          <>
            <Field label={t("Who receives it")} hint={t("A contact list. The server decides who is eligible when it starts.")}>
              <select required value={f.list_id} onChange={set("list_id")}><option value="">{t("Choose a list…")}</option>{(lists.data || []).map((l) => <option key={l.id} value={l.id}>{t("{name} ({n} people)", { name: l.name, n: l.member_count ?? 0 })}</option>)}</select>
            </Field>
            {f.list_id && (
              <div className="quote" aria-live="polite">
                {audience.data ? (
                  <>
                    <span><b>{audience.data.eligible}</b> {t("can be contacted")}</span>
                    {Object.entries(audience.data.excluded || {}).map(([k, v]) => <span key={k} className="muted">{v} · {skippedText(k)}</span>)}
                  </>
                ) : audience.loading ? <span className="muted">{t("Checking the list…")}</span> : <span className="muted">{t("The audience couldn't be checked now.")}</span>}
              </div>
            )}
          </>
        )}
        {step === 2 && (
          <>
            {sms && (templates.data || []).length > 0 && (
              <Field label={t("Message source")}><select value={f.mode} onChange={set("mode")}><option value="text">{t("Write a message")}</option><option value="template">{t("Use an approved template")}</option></select></Field>
            )}
            {!sms && <Field label={t("Subject")}><input required value={f.subject} onChange={set("subject")} placeholder={t("{{first_name}}, your offer inside")} /></Field>}
            {f.mode === "template" && sms ? (
              <Field label={t("Template")}><select required value={f.template_id} onChange={set("template_id")}><option value="">{t("Choose a template…")}</option>{templates.data.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}</select></Field>
            ) : (
              <>
                <Field label={t("Message")} hint={t("Put {{first_name}} (or any contact field) where the name should go")}><textarea required rows={4} value={f.text} onChange={set("text")} /></Field>
                {sms && <div className="meter" aria-live="polite"><span>{t("{n} characters", { n: info.length })} · {info.encoding}</span><span>{t("About {n} part(s) per message (estimate; the server counts the final number)", { n: info.segments })}</span></div>}
              </>
            )}
            <details className="adv"><summary>{t("Speed and budget")}</summary>
              <div className="grid two" style={{ marginTop: 12 }}>
                <Field label={t("Speed limit (messages per minute)")}><input type="number" min="1" max="10000" value={f.rate_per_minute} onChange={set("rate_per_minute")} /></Field>
                {sms && <Field label={t("Budget cap")} hint={t("The campaign pauses before going over")}><input inputMode="decimal" placeholder={t("optional, e.g. 25.00")} value={f.max_cost} onChange={set("max_cost")} /></Field>}
              </div>
            </details>
          </>
        )}
        {step === 3 && <ScheduleField value={sch} onChange={setSch} />}
        {step === 4 && (
          <>
            <dl className="kvlist" aria-label={t("Review")}>
              <dt>{t("Name")}</dt><dd>{f.name}</dd>
              <dt>{t("From")}</dt><dd>{sms ? f.sender : f.from_email}</dd>
              <dt>{t("Recipients")}</dt><dd>{list ? list.name : "-"}{audience.data ? ` — ${t("{n} can be contacted", { n: audience.data.eligible })}` : ""}</dd>
              <dt>{t("Message")}</dt><dd className="preview">{f.mode === "template" && sms ? (tpl ? t("Template: {name}", { name: tpl.name }) : "-") : f.text}</dd>
              <dt>{t("When")}</dt><dd>{sch.mode === "now" ? t("As soon as you confirm") : sched.ok ? `${localText(sched.date)} · ${utcText(sched.date)}` : "-"}</dd>
              {sms && (wallets.data || [])[0] && <><dt>{t("Available balance")}</dt><dd>{money(wallets.data[0].available)} {wallets.data[0].currency}</dd></>}
            </dl>
            <small className="muted">{t("The cost estimate appears on the campaign page once the draft is saved; the server decides whether the balance is enough.")}</small>
          </>
        )}
        {draft && a.error && <div className="alert info" role="status"><span>{t("Your draft was saved, but it was not started.")} <a href={buildHash("campaigns", [String(draft.id)])}>{t("Open the draft")}</a></span></div>}
        <ErrorBox error={a.error} />
        {a.error?.code === "insufficient_funds" && <a className="btn small primary" href="#wallet">{t("Top up")}</a>}
        <div className="row wrap">
          <Button type="button" disabled={step === 0 || a.busy} onClick={() => setStep(step - 1)}>{t("Back")}</Button>
          {!last && <Button variant="primary" disabled={!valid[step === 4 ? 3 : step] || noLists} type="submit">{t("Next")}</Button>}
          {last && <>
            <Button type="button" busy={a.busy} disabled={!valid.slice(0, 3).every(Boolean)} onClick={() => submit("draft")}>{t("Save draft")}</Button>
            <Button variant="primary" busy={a.busy} disabled={!valid.every(Boolean)} type="submit">{sch.mode === "now" ? t("Send now") : t("Schedule")}</Button>
          </>}
        </div>
      </form>
    </Card>
  );
}

// ---------------------------------------------------------------- Detaji
function Overview({ id, c, est, wallet, reload }) {
  const { confirm } = useUi();
  const a = useAction();
  const [sch, setSch] = useState(emptySchedule());
  const d = c.data;
  const s = d.stats;
  const pre = ["draft", "scheduled"].includes(d.status);
  const sms = d.channel === "sms";
  const sched = scheduleResult(sch);
  const act = (verb, msg, body) => a.run(async () => { await api.post(`/v1/campaigns/${id}/${verb}`, body); reload(); }, msg);
  return (
    <>
      <Card title={t("Campaign")}>
        <dl className="kvlist">
          <dt>{t("Channel")}</dt><dd>{d.channel.toUpperCase()}</dd><dt>{t("From")}</dt><dd>{sms ? d.sender : d.from_email}</dd>
          <dt>{t("Recipients")}</dt><dd>{d.list_name || "-"}</dd><dt>{t("Type")}</dt><dd>{t(d.category)}</dd>
          <dt>{t("Message")}</dt><dd className="preview">{d.text || (d.template_id ? t("Template #{id}", { id: d.template_id }) : "-")}</dd>
          <dt>{t("Created")}</dt><dd>{when(d.created_at)}</dd><dt>{t("Scheduled for")}</dt><dd>{d.scheduled_at ? `${utcOf(d.scheduled_at)} · ${when(d.scheduled_at)}` : "-"}</dd>
          <dt>{t("Started")}</dt><dd>{when(d.started_at)}</dd><dt>{t("Finished")}</dt><dd>{when(d.completed_at)}</dd>
          <dt>{t("Speed limit")}</dt><dd>{t("{n} per minute", { n: d.rate_per_minute })}</dd>{sms && <><dt>{t("Budget cap")}</dt><dd>{d.max_cost ? money(d.max_cost) : t("none")}</dd></>}
        </dl>
        {d.pause_reason && <div className="alert warn"><span>{PAUSE[d.pause_reason] ? t(PAUSE[d.pause_reason]) : t("Paused: {reason}", { reason: d.pause_reason })}</span>{d.pause_reason === "insufficient_funds" && <a className="btn small primary" href="#wallet">{t("Top up")}</a>}</div>}
      </Card>
      {d.status === "draft" && (
        <Card title={t("Send or schedule")} subtitle={t("Nothing has been sent yet.")}>
          <div className="quote">
            {est.data && <span><b>{est.data.recipients}</b> {t("will be reached")} · {t("{n} left out (no consent, opted out or no address)", { n: est.data.excluded })}</span>}
            {est.data && sms && <span>{t("Estimated cost")}: <b>{money(est.data.total)} {est.data.currency || ""}</b></span>}
            {sms && wallet.data?.[0] && <span>{t("Available balance")}: <b>{money(wallet.data[0].available)} {wallet.data[0].currency}</b></span>}
          </div>
          <ScheduleField value={sch} onChange={setSch} />
          <ErrorBox error={a.error} />
          {a.error?.code === "insufficient_funds" && <a className="btn small primary" href="#wallet">{t("Top up")}</a>}
          <div className="row"><Button variant="primary" busy={a.busy} disabled={!sched.ok || (sch.mode === "later" && !sch.date)} onClick={async () => {
            if (sch.mode === "now" && !(await confirm({ title: t("Send this campaign now?"), body: t("You can pause or cancel while it runs; messages already sent can't be recalled."), confirmLabel: t("Send now") }))) return;
            act("schedule", sch.mode === "now" ? t("Campaign started") : t("Campaign scheduled"), sch.mode === "later" ? { scheduled_at: sched.iso } : {});
          }}>{sch.mode === "now" ? t("Send now") : t("Schedule")}</Button></div>
        </Card>
      )}
      <ErrorBox error={d.status === "draft" ? null : a.error} />
      <div className="row wrap">
        {d.status === "running" && <Button busy={a.busy} onClick={() => act("pause", t("Campaign paused"))}>{t("Pause")}</Button>}
        {d.status === "paused" && <Button variant="primary" busy={a.busy} onClick={() => act("resume", t("Campaign resumed"))}>{t("Resume")}</Button>}
        {!["completed", "cancelled"].includes(d.status) && <Button variant="danger" busy={a.busy} onClick={async () => { if (await confirm({ title: t("Cancel this campaign?"), body: t("Messages not yet sent are stopped and their cost returned. Messages already sent can't be recalled."), danger: true, confirmLabel: t("Cancel campaign") })) act("cancel", t("Campaign cancelled")); }}>{t("Cancel")}</Button>}
      </div>
      <div className="grid stats">
        {pre && est.data && <Stat label={t("Will reach")} value={est.data.recipients} sub={t("{n} left out (no consent, opted out or no address)", { n: est.data.excluded })} />}
        {Object.entries(s.recipients).map(([k, v]) => <Stat key={k} label={t("People {status}", { status: t(k) })} value={v} />)}
        <Stat label={t("Delivery rate")} value={s.delivery_rate == null ? "-" : `${(s.delivery_rate * 100).toFixed(1)}%`} tone={s.delivery_rate > 0.9 ? "good" : ""} />
        {sms && <Stat label={t("Charged for delivered")} value={money(s.cost.delivered)} sub={t("{a} in flight · {b} refunded", { a: money(s.cost.in_flight), b: money(s.cost.refunded) })} />}
      </div>
      <div className="grid two">
        <Card title={t("Delivery summary")} subtitle={t("From the server, per message")}>{Object.keys(s.messages).length ? Object.entries(s.messages).map(([k, v]) => <div key={k} className="kv"><span><Badge>{k}</Badge></span><b>{v}</b></div>) : <div className="empty">{t("Nothing sent yet.")}</div>}</Card>
        <Card title={t("Left out")} subtitle={t("People who were skipped, and why")}>{Object.keys(s.skipped_reasons).length ? Object.entries(s.skipped_reasons).map(([k, v]) => <div key={k} className="kv"><span>{skippedText(k)}</span><b>{v}</b></div>) : <div className="empty">{t("Nobody was skipped.")}</div>}</Card>
      </div>
    </>
  );
}

const MsgFailure = ({ code }) => {
  const f = failureInfo(code);
  return f ? <span title={f.code}>{f.label}</span> : <span>-</span>;
};

function Recipients({ id, hs }) {
  const { query } = hs;
  const status = query.status || "", after = query.after || "";
  const key = JSON.stringify({ status, after });
  const r = useLoad(async (signal) => ({ key, rows: await api.get(`/v1/campaigns/${id}/recipients`, { status, after_id: after || undefined, limit: PAGE + 1 }, { signal }) }), [id, key]);
  const rows = r.data ? r.data.rows.slice(0, PAGE) : [];
  const more = r.data ? r.data.rows.length > PAGE : false;
  const go = (q) => hs.go([String(id)], { tab: "recipients", ...q });
  return (
    <Card title={t("Recipients")}>
      <div className="toolbar" role="search" aria-label={t("Filter recipients")}>
        <select aria-label={t("Filter by state")} value={status} onChange={(e) => go({ status: e.target.value })}><option value="">{t("All states")}</option>{RECIPIENT_STATUSES.map((s) => <option key={s} value={s}>{t(s)}</option>)}</select>
      </div>
      {r.error && !r.data ? <ErrorState error={r.error} retry={r.reload} /> : r.loading && !r.data ? <Skeleton rows={4} /> : (
        <>
          <Table rows={rows} emptyTitle={status ? t("No recipients in this state") : t("No recipients yet")} empty={t("Recipients appear once the campaign starts.")}
            cols={[
              { label: t("Recipient"), render: (x) => <b>{x.address ? (/^\d+$/.test(x.address) ? `+${x.address}` : x.address) : t("Erased")}</b> },
              { label: t("Name"), hideSm: true, render: (x) => x.name || "-" },
              { label: t("State"), render: (x) => <Badge>{x.status}</Badge> },
              { label: t("Delivery"), render: (x) => (x.message_status ? <Badge>{x.message_status}</Badge> : "-") },
              { label: t("Reason"), hideSm: true, render: (x) => (x.error_code ? <MsgFailure code={x.error_code} /> : x.reason ? skippedText(x.reason) : "-") },
            ]} />
          {(after || more) && (
            <nav className="pager" aria-label={t("Pagination")}>
              <Button disabled={!after} onClick={() => go({ status })}>{t("First page")}</Button>
              <Button disabled={!more} onClick={() => go({ status, after: String(rows[rows.length - 1].id) })}>{t("Next")}</Button>
            </nav>
          )}
        </>
      )}
    </Card>
  );
}

export function MessageTimeline({ publicId }) {
  const ev = useLoad((signal) => api.get(`/v1/messages/${publicId}/events`, {}, { signal }), [publicId]);
  if (ev.error && !ev.data) return <ErrorState error={ev.error} retry={ev.reload} />;
  if (!ev.data) return <Skeleton rows={2} />;
  return <Table rows={ev.data.map((e, i) => ({ ...e, id: i }))} empty={t("No history.")} cols={[{ label: t("Status"), render: (r) => <Badge>{r.to}</Badge> }, { label: t("Detail"), render: (r) => r.detail || "" }]} />;
}

function Traffic({ id, hs, sms }) {
  const { query } = hs;
  const status = query.status || "", before = query.before || "", open = query.msg || "";
  const key = JSON.stringify({ status, before });
  const r = useLoad(async (signal) => (sms ? { key, ...(await api.get("/v1/messages", { campaign_id: id, status, before_id: before || undefined, limit: PAGE }, { signal })) } : null), [id, key, sms]);
  const go = (q) => hs.go([String(id)], { tab: "traffic", ...q });
  if (!sms) return <Card title={t("SMS traffic")}><div className="empty">{t("Email campaigns are listed under Message history → Email.")}</div></Card>;
  return (
    <>
      <Card title={t("SMS traffic")}>
        <div className="toolbar" role="search" aria-label={t("Filter messages")}>
          <select aria-label={t("Filter by status")} value={status} onChange={(e) => go({ status: e.target.value })}>
            {["", "queued", "sending", "sent", "delivered", "failed"].map((s) => <option key={s} value={s}>{s ? t("Status: {status}", { status: t(s) }) : t("All statuses")}</option>)}
          </select>
        </div>
        {r.error && !r.data ? <ErrorState error={r.error} retry={r.reload} /> : r.loading && !r.data ? <Skeleton rows={4} /> : (
          <>
            <Table rows={r.data.items.map((m) => ({ ...m, _onClick: () => go({ status, before, msg: open === m.id ? "" : m.id }) }))} emptyTitle={status ? t("No matches") : t("No messages yet")} empty={t("Messages sent by this campaign appear here.")}
              cols={[
                { label: t("To"), render: (m) => <b>+{m.to}</b> },
                { label: t("Status"), render: (m) => <Badge>{m.status}</Badge> },
                { label: t("Why not delivered"), hideSm: true, render: (m) => <MsgFailure code={m.error_code} /> },
                { label: t("Parts"), num: true, hideSm: true, key: "segments" },
                { label: t("Cost"), num: true, hideSm: true, render: (m) => `${money(m.total_price)} ${m.currency}` },
                { label: t("Sent"), render: (m) => <Time value={m.created_at} /> },
              ]} />
            {(before || r.data.next_before_id) && (
              <nav className="pager" aria-label={t("Pagination")}>
                <Button disabled={!before} onClick={() => go({ status })}>{t("Newest")}</Button>
                <Button disabled={!r.data.next_before_id} onClick={() => go({ status, before: String(r.data.next_before_id) })}>{t("Older")}</Button>
              </nav>
            )}
          </>
        )}
      </Card>
      {open && <Card title={t("What happened")} subtitle={t("Reference {id}", { id: open })} actions={<Button onClick={() => go({ status, before })}>{t("Close")}</Button>}><MessageTimeline publicId={open} /></Card>}
    </>
  );
}

function Activity({ id }) {
  const ev = useLoad((signal) => api.get(`/v1/campaigns/${id}/activity`, {}, { signal }), [id]);
  return (
    <Card title={t("Activity")} subtitle={t("What happened to this campaign, newest first")}>
      {ev.error && !ev.data ? <ErrorState error={ev.error} retry={ev.reload} /> : !ev.data ? <Skeleton rows={3} /> : (
        <AuditTimeline empty={t("No recorded activity.")} events={ev.data.map((e, i) => ({ id: i, at: e.at, title: ACTIVITY[e.action] ? t(ACTIVITY[e.action]) : e.action, actor: e.by === "you" ? t("you") : t("our team"), detail: e.detail?.at ? t("Planned for {when}", { when: utcOf(e.detail.at) }) : null }))} />
      )}
    </Card>
  );
}

function Detail({ id, hs }) {
  const c = useLoad((signal) => api.get(`/v1/campaigns/${id}`, {}, { signal }), [id], 4000);
  const est = useLoad((signal) => api.get(`/v1/campaigns/${id}/estimate`, {}, { signal }).catch(() => null), [id]);
  const wallet = useLoad((signal) => api.get("/v1/wallets", {}, { signal }).catch(() => []), []);
  const d = c.data;
  const tab = ["overview", "recipients", "traffic", "activity"].includes(hs.query.tab) ? hs.query.tab : "overview";
  if (!d) return <Card title={t("Campaign")}>{c.error ? <ErrorState error={c.error} retry={c.reload} /> : <Skeleton />}</Card>;
  return (
    <>
      <Card title={d.name} actions={<><Badge>{d.status}</Badge><a className="btn" href={buildHash("campaigns")}>{t("← All campaigns")}</a></>}>
        {c.stale && <ErrorBox error={c.error} retry={c.reload} />}
        <Tabs value={tab} onChange={(v) => hs.go([String(id)], v === "overview" ? {} : { tab: v })} tabs={[{ id: "overview", label: t("Overview") }, { id: "recipients", label: t("Recipients") }, { id: "traffic", label: t("SMS traffic") }, { id: "activity", label: t("Activity") }]} />
      </Card>
      {tab === "overview" && <Overview id={id} c={c} est={est} wallet={wallet} reload={() => { c.reload(); est.reload(); }} />}
      {tab === "recipients" && <Recipients id={id} hs={hs} />}
      {tab === "traffic" && <Traffic id={id} hs={hs} sms={d.channel === "sms"} />}
      {tab === "activity" && <Activity id={id} />}
    </>
  );
}

export default function Campaigns() {
  const hs = useHashState("campaigns");
  const [first] = hs.rest;
  if (first === "new") return <Create hs={hs} />;
  if (first && /^\d+$/.test(first)) return <Detail key={first} id={first} hs={hs} />;
  return <List hs={hs} />;
}
