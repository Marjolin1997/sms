import { useEffect, useState } from "react";
import { api, uuid } from "../api.js";
import { t, tn } from "../i18n.jsx";
import { Badge, Button, Card, Empty, ErrorBox, Field, Tabs, Table, money, smsInfo, useAction, useDebounced, useLoad } from "../ui.jsx";

function Timeline({ path }) {
  const ev = useLoad(() => api.get(path), [path], 3000);
  return <Table rows={ev.data || []} loading={ev.loading} empty={t("Waiting for the first update…")} cols={[{ label: t("Was"), render: (r) => r.from ? <Badge>{r.from}</Badge> : "-" }, { label: t("Became"), render: (r) => <Badge>{r.to}</Badge> }, { label: t("Detail"), render: (r) => r.detail || "" }]} />;
}

function Sms() {
  const senders = useLoad(() => api.get("/v1/sender-ids", { status: "approved" }), []);
  const templates = useLoad(() => api.get("/v1/templates", { status: "approved" }).catch(() => []), []);
  const wallets = useLoad(() => api.get("/v1/wallets"), []);
  const [f, setF] = useState(() => {
    let to = "";
    try { to = sessionStorage.getItem("sms_prefill_to") || ""; sessionStorage.removeItem("sms_prefill_to"); } catch { /* pa sessionStorage */ }
    return { to, sender: "", text: "", category: "transactional", templateId: "" };
  });
  const [values, setValues] = useState({});
  const [quote, setQuote] = useState(null);
  const [sent, setSent] = useState(null);
  const a = useAction();
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });

  const approved = [...new Set((senders.data || []).map((s) => s.value))];
  useEffect(() => { if (!f.sender && approved.length) setF((x) => ({ ...x, sender: approved[0] })); }, [approved.length]); // eslint-disable-line
  const tpl = (templates.data || []).find((x) => String(x.id) === f.templateId);
  const tplVersion = tpl?.versions?.[0];
  const text = tplVersion ? tplVersion.body.replace(/\{\{([a-z_][a-z0-9_]*)\}\}/g, (_, k) => values[k] || `{{${k}}}`) : f.text;
  const info = smsInfo(text);
  const numberOk = /^\+[1-9]\d{6,14}$/.test(f.to);
  const dText = useDebounced(text), dTo = useDebounced(f.to);
  useEffect(() => {
    if (!/^\+[1-9]\d{6,14}$/.test(dTo) || !dText.trim()) { setQuote(null); return; }
    let live = true;
    api.post("/v1/messages/quote", { to: dTo, text: dText }).then((q) => live && setQuote({ ok: q })).catch((e) => live && setQuote({ error: e }));
    return () => { live = false; };
  }, [dTo, dText]);

  const balance = wallets.data?.find((w) => quote?.ok && w.currency === quote.ok.currency);
  const short = quote?.ok && balance && Number(balance.available) < Number(quote.ok.total);
  const missing = tplVersion ? tplVersion.variables.filter((v) => !values[v]) : [];
  const ready = numberOk && f.sender && (tplVersion ? missing.length === 0 : f.text.trim()) && !quote?.error;

  const submit = async (e) => {
    e.preventDefault();
    const body = { to: f.to, sender: f.sender, category: f.category };
    if (tplVersion) { body.template_id = tpl.id; body.values = values; } else body.text = f.text;
    const r = await a.run(() => api.post("/v1/messages", body, { headers: { "Idempotency-Key": uuid() } }), t("Message accepted for delivery"));
    if (r && r.id) setSent(r);
  };

  if (senders.data && approved.length === 0)
    return (
      <Card title={t("One step before you can send SMS")}>
        <Empty icon="✦" title={t("You need an approved sender ID")} action={<a className="btn primary" href="#senders">{t("Request a sender ID")}</a>}>
          {t("A sender ID is the name recipients see, like your brand. We review each one, usually within a business day.")}
        </Empty>
      </Card>
    );

  return (
    <>
      <Card title={t("New SMS")}>
        <form onSubmit={submit} className="form" noValidate>
          <div className="grid two">
            <Field label={t("To")} hint={t("International format, for example +355691234567")} error={f.to && !numberOk ? t("Start with + and the country code, digits only.") : null}>
              <input required inputMode="tel" autoComplete="off" placeholder={"+355691234567"} value={f.to} onChange={set("to")} />
            </Field>
            <Field label={t("From")} hint={t("Only approved sender IDs are listed")}>
              <select value={f.sender} onChange={set("sender")}>{approved.map((s) => <option key={s}>{s}</option>)}</select>
            </Field>
          </div>
          {(templates.data || []).length > 0 && (
            <Field label={t("Message source")}>
              <select value={f.templateId} onChange={(e) => { setF({ ...f, templateId: e.target.value }); setValues({}); }}>
                <option value="">{t("Write my own text")}</option>
                {templates.data.map((x) => <option key={x.id} value={x.id}>{t("Template: {name}", { name: x.name })}</option>)}
              </select>
            </Field>
          )}
          {tplVersion ? (
            <div className="grid two">
              {tplVersion.variables.map((v) => <Field key={v} label={v}><input value={values[v] || ""} onChange={(e) => setValues({ ...values, [v]: e.target.value })} /></Field>)}
            </div>
          ) : (
            <Field label={t("Message")}>
              <textarea required rows={4} value={f.text} onChange={set("text")} placeholder={t("Type your message")} />
            </Field>
          )}
          <div className={`meter ${info.segments > 3 || info.ucs ? "warn" : ""}`}>
            <span>{t("{n} characters", { n: info.length })} · {info.encoding}{info.ucs && ` ${t("(letters like ë, ç or emoji make each part shorter)")}`}</span>
            <span>{tn(info.segments, "{n} part", "{n} parts")} · {t("{n} per part", { n: info.perSegment })}</span>
          </div>
          {tplVersion && <div className="help"><b>{t("Preview:")}</b> {text}</div>}
          <Field label={t("Type")} hint={f.category === "marketing" ? t("Promotions. Only people who agreed to receive them will get it.") : t("Codes, receipts, alerts. Sent even if someone declined promotions.")}>
            <select value={f.category} onChange={set("category")}><option value="transactional">{t("Transactional (one-to-one info)")}</option><option value="marketing">{t("Marketing (promotions)")}</option></select>
          </Field>
          {quote?.ok && (
            <div className="quote" aria-live="polite">
              <span>{t("To")} <b>{quote.ok.country}</b></span><span>{t("{n} part(s) × {price}", { n: quote.ok.segments, price: money(quote.ok.unit_price) })}</span><span>{t("Cost")} <b>{money(quote.ok.total)} {quote.ok.currency}</b></span>
              {balance && <span className="muted">{t("Balance {amount}", { amount: money(balance.available) })}</span>}
            </div>
          )}
          {quote?.error && <div className="quote bad" role="alert">{quote.error.message}</div>}
          {short && <div className="alert warn"><span>{t("Your balance is lower than the cost of this message.")}</span><a className="btn small primary" href="#wallet">{t("Top up")}</a></div>}
          <ErrorBox error={a.error} />
          <div><Button variant="primary" busy={a.busy} disabled={!ready}>{t("Send SMS")}</Button></div>
        </form>
      </Card>
      {sent && (
        <Card title={t("Sent")} subtitle={t("Reference {id}", { id: sent.id })} actions={<><Badge>{sent.status}</Badge><a className="btn small" href="#messages">{t("Message history")}</a></>}>
          <div className="kvlist"><dt>{t("Parts")}</dt><dd>{sent.segments}</dd><dt>{t("Price")}</dt><dd>{money(sent.total_price)} {sent.currency}</dd></div>
          <h4>{t("Progress")}</h4>
          <Timeline path={`/v1/messages/${sent.id}/events`} />
        </Card>
      )}
    </>
  );
}

function EmailTab() {
  const domains = useLoad(() => api.get("/v1/email/domains"), []);
  const verified = (domains.data || []).filter((d) => d.status === "verified");
  const [f, setF] = useState({ from_email: "", to: "", subject: "", text: "", category: "transactional" });
  const [sent, setSent] = useState(null);
  const a = useAction();
  useEffect(() => { if (!f.from_email && verified.length) setF((x) => ({ ...x, from_email: `hello@${verified[0].domain}` })); }, [verified.length]); // eslint-disable-line
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const submit = async (e) => {
    e.preventDefault();
    const r = await a.run(() => api.post("/v1/email/messages", f, { headers: { "Idempotency-Key": uuid() } }), t("Email queued"));
    if (r && r.id) setSent(r);
  };
  if (domains.data && verified.length === 0)
    return (
      <Card title={t("Verify a domain first")}>
        <Empty icon="@" title={t("No verified sending domain")} action={<a className="btn primary" href="#email">{t("Set up a domain")}</a>}>{t("Email can only be sent from a domain you've proven you own. It takes a few DNS records and a few minutes.")}</Empty>
      </Card>
    );
  return (
    <>
      <Card title={t("New email")}>
        <form onSubmit={submit} className="form">
          <div className="grid two">
            <Field label={t("From")} hint={t("Verified domains: {list}", { list: verified.map((d) => d.domain).join(", ") })}><input required type="email" value={f.from_email} onChange={set("from_email")} /></Field>
            <Field label={t("To")}><input required type="email" value={f.to} onChange={set("to")} placeholder={"name@example.com"} /></Field>
          </div>
          <Field label={t("Subject")}><input required maxLength={200} value={f.subject} onChange={set("subject")} /></Field>
          <Field label={t("Message")} hint={t("Plain text. Marketing emails get an unsubscribe link automatically.")}><textarea required rows={6} value={f.text} onChange={set("text")} /></Field>
          <Field label={t("Type")}><select value={f.category} onChange={set("category")}><option value="transactional">{t("Transactional")}</option><option value="marketing">{t("Marketing (needs recipient consent)")}</option></select></Field>
          <ErrorBox error={a.error} />
          <div><Button variant="primary" busy={a.busy}>{t("Send email")}</Button></div>
        </form>
      </Card>
      {sent && <Card title={t("Queued")} actions={<><Badge>{sent.status}</Badge><a className="btn small" href="#messages">{t("Message history")}</a></>}><Timeline path={`/v1/email/messages/${sent.id}/events`} /></Card>}
    </>
  );
}

export default function Send() {
  const [tab, setTab] = useState("sms");
  return (
    <>
      <Tabs tabs={[{ id: "sms", label: "SMS" }, { id: "email", label: "Email" }]} value={tab} onChange={setTab} />
      {tab === "sms" ? <Sms /> : <EmailTab />}
    </>
  );
}
