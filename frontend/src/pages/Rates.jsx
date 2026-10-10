import { useState } from "react";
import { api } from "../api.js";
import { t } from "../i18n.jsx";
import { Badge, Button, Card, ErrorBox, Field, Table, Tabs, Time, money, useAction, useLoad, useUi, when } from "../ui.jsx";

function Prices({ card, reload, back }) {
  const { confirm } = useUi();
  const [vid, setVid] = useState(card.versions[0]?.id || null);
  const ver = card.versions.find((v) => v.id === vid);
  const rates = useLoad(() => (vid ? api.get(`/v1/rate-card-versions/${vid}/rates`) : Promise.resolve([])), [vid]);
  const [r, setR] = useState({ prefix: "", operator: "", price: "" });
  const [when_, setWhen] = useState("");
  const [q, setQ] = useState({ to: "", text: "Hello" });
  const [quote, setQuote] = useState(null);
  const a = useAction(), b = useAction(), c = useAction(), d = useAction();
  const draft = ver?.status === "draft";
  const hasDraft = card.versions.some((v) => v.status === "draft");
  return (
    <>
      <Card title={`${card.name} (${card.currency})`} actions={<><Button onClick={back}>{t("← All price lists")}</Button><Button busy={b.busy} disabled={hasDraft} onClick={async () => { const x = await b.run(() => api.post(`/v1/rate-cards/${card.id}/versions`), t("Draft created from the latest prices")); if (x && x.id) { await reload(); setVid(x.id); } }}>{t("New draft version")}</Button></>}>
        <ErrorBox error={b.error} />
        <Table rows={card.versions.map((v) => ({ ...v, _onClick: () => setVid(v.id) }))} empty={t("No versions.")} cols={[{ label: t("Version"), render: (v) => <b style={{ textDecoration: v.id === vid ? "underline" : "none" }}>v{v.version}</b> }, { label: t("Status"), render: (v) => <Badge>{v.status}</Badge> }, { label: t("Takes effect"), render: (v) => (v.effective_from ? when(v.effective_from) : "-") }, { label: t("Prices"), key: "rates", num: true }]} />
        <small className="muted">{t("A published version never changes, so past messages keep the price they were sent at. Click a version to see its prices.")}</small>
      </Card>
      {ver && (
        <Card title={t("Prices in v{n}", { n: ver.version })} subtitle={draft ? t("Draft: you can edit and publish") : t("Published: read only")}>
          <ErrorBox error={rates.error} />
          <Table rows={(rates.data || []).map((x) => ({ ...x, _onClick: draft ? () => setR({ prefix: x.prefix, operator: x.operator, price: x.price_per_segment.replace(/0+$/, "").replace(/\.$/, "") }) : undefined }))} loading={rates.loading} empty={t("No prices yet.")} cols={[{ label: t("Destination prefix"), render: (x) => <code>+{x.prefix}</code> }, { label: t("Operator"), render: (x) => x.operator || t("any") }, { label: t("Price per SMS part"), num: true, render: (x) => money(x.price_per_segment) }]} />
          {draft && (
            <>
              <h4>{t("Add or change a price")}</h4>
              <div className="row wrap">
                <Field label={t("Prefix")} hint={t("Digits, no +. 355 = Albania")}><input value={r.prefix} onChange={(e) => setR({ ...r, prefix: e.target.value.replace(/\D/g, "") })} /></Field>
                <Field label={t("Operator (optional)")} hint={t("MCCMNC, e.g. 27601")}><input value={r.operator} onChange={(e) => setR({ ...r, operator: e.target.value })} /></Field>
                <Field label={t("Price per part ({currency})", { currency: card.currency })}><input inputMode="decimal" value={r.price} onChange={(e) => setR({ ...r, price: e.target.value })} /></Field>
                <Button variant="primary" busy={a.busy} disabled={!r.prefix || r.price === ""} onClick={async () => { await a.run(() => api.put(`/v1/rate-card-versions/${ver.id}/rates`, { prefix: r.prefix, operator: r.operator, price_per_segment: r.price }), t("Price saved")); setR({ prefix: "", operator: "", price: "" }); rates.reload(); reload(); }}>{t("Save price")}</Button>
              </div>
              <ErrorBox error={a.error} />
              <h4>{t("Publish")}</h4>
              <div className="row wrap">
                <Field label={t("Takes effect at")} hint={t("Must be in the future and after the previous version")}><input type="datetime-local" value={when_} onChange={(e) => setWhen(e.target.value)} /></Field>
                <Button variant="primary" busy={c.busy} disabled={!when_ || !ver.rates} onClick={async () => { if (await confirm({ title: t("Publish these prices?"), body: t("They take effect on {date} and can't be edited afterwards.", { date: when(new Date(when_).toISOString()) }), confirmLabel: t("Publish") })) { await c.run(() => api.post(`/v1/rate-card-versions/${ver.id}/publish`, { effective_from: new Date(when_).toISOString() }), t("Published")); reload(); } }}>{t("Publish")}</Button>
              </div>
              <ErrorBox error={c.error} />
            </>
          )}
        </Card>
      )}
      <Card title={t("Try a price")} subtitle={t("Check what a message to a number would cost with this list right now")}>
        <div className="row wrap"><Field label={t("Number")}><input placeholder="+355691234567" value={q.to} onChange={(e) => setQ({ ...q, to: e.target.value })} /></Field><Field label={t("Text")}><input value={q.text} onChange={(e) => setQ({ ...q, text: e.target.value })} /></Field>
          <Button busy={d.busy} disabled={!q.to} onClick={async () => { const x = await d.run(() => api.post(`/v1/rate-cards/${card.id}/quote`, { number: q.to, text: q.text })); if (x && x !== true) setQuote(x); }}>{t("Price it")}</Button></div>
        <ErrorBox error={d.error} />
        {quote && <div className="quote"><span>{t("{n} part(s)", { n: quote.segments })} × {money(quote.unit_price)}</span><span>{t("Total")} <b>{money(quote.total)} {quote.currency}</b></span><span className="muted">{quote.encoding}</span></div>}
      </Card>
    </>
  );
}

function Cards() {
  const cards = useLoad(() => api.get("/v1/rate-cards"), []);
  const [sel, setSel] = useState(null);
  const [f, setF] = useState({ name: "", currency: "EUR" });
  const a = useAction();
  const card = (cards.data || []).find((c) => c.id === sel);
  if (card) return <Prices card={card} reload={cards.reload} back={() => setSel(null)} />;
  return (
    <>
      <Card title={t("Price lists")} subtitle={t("Each customer account uses one price list.")}>
        <ErrorBox error={cards.error} retry={cards.reload} />
        <Table rows={(cards.data || []).map((c) => ({ ...c, _onClick: () => setSel(c.id) }))} loading={cards.loading} emptyTitle={t("No price lists yet")} empty={t("Create one below.")} cols={[{ label: t("Name"), render: (c) => <b>{c.name}</b> }, { label: t("Currency"), key: "currency" }, { label: t("Latest"), render: (c) => (c.versions[0] ? <>v{c.versions[0].version} <Badge>{c.versions[0].status}</Badge></> : "-") }]} />
      </Card>
      <Card title={t("New price list")}>
        <div className="row wrap"><Field label={t("Name")}><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="standard" /></Field><Field label={t("Currency")}><input maxLength={3} value={f.currency} onChange={(e) => setF({ ...f, currency: e.target.value.toUpperCase() })} /></Field>
          <Button variant="primary" busy={a.busy} disabled={!f.name || f.currency.length !== 3} onClick={async () => { const x = await a.run(() => api.post("/v1/rate-cards", f), t("Price list created")); if (x && x.id) { await cards.reload(); setSel(x.id); } }}>{t("Create")}</Button></div>
        <ErrorBox error={a.error} />
      </Card>
    </>
  );
}

function Routes() {
  const routes = useLoad(() => api.get("/v1/admin/routes"), []);
  const [f, setF] = useState({ prefix: "", country: "", provider: "", priority: 100 });
  const a = useAction();
  return (
    <>
      <div className="help">{t("A route says which provider carries messages for a number prefix. The longest matching prefix wins; among equals, the highest priority.")}</div>
      <Card title={t("Routes")}>
        <ErrorBox error={routes.error || a.error} retry={routes.reload} />
        <Table rows={routes.data || []} loading={routes.loading} empty={t("No routes yet. Nothing can be sent.")} cols={[{ label: t("Prefix"), render: (r) => <code>+{r.prefix}</code> }, { label: t("Country"), key: "country" }, { label: t("Provider"), key: "provider" }, { label: t("Priority"), key: "priority", num: true }, { label: t("Status"), render: (r) => <Badge>{r.enabled ? "active" : "disabled"}</Badge> },
          { label: "", render: (r) => <Button className="small" busy={a.busy} onClick={async () => { await a.run(() => api.put("/v1/admin/routes", { prefix: r.prefix, country: r.country, provider: r.provider, priority: r.priority, enabled: !r.enabled }), r.enabled ? t("Route disabled") : t("Route enabled")); routes.reload(); }}>{r.enabled ? t("Disable") : t("Enable")}</Button> }]} />
      </Card>
      <Card title={t("Add or change a route")}>
        <div className="row wrap">
          <Field label={t("Prefix")} hint={t("Digits only, e.g. 355")}><input value={f.prefix} onChange={(e) => setF({ ...f, prefix: e.target.value.replace(/\D/g, "") })} /></Field>
          <Field label={t("Country")} hint={t("2 letters")}><input maxLength={2} value={f.country} onChange={(e) => setF({ ...f, country: e.target.value.toUpperCase() })} /></Field>
          <Field label={t("Provider")}><input value={f.provider} onChange={(e) => setF({ ...f, provider: e.target.value })} placeholder={t("fake or http")} /></Field>
          <Field label={t("Priority")} hint={t("Higher wins")}><input type="number" value={f.priority} onChange={(e) => setF({ ...f, priority: Number(e.target.value) })} /></Field>
          <Button variant="primary" busy={a.busy} disabled={!f.prefix || f.country.length !== 2 || !f.provider} onClick={async () => { await a.run(() => api.put("/v1/admin/routes", { ...f, enabled: true }), t("Route saved")); routes.reload(); }}>{t("Save route")}</Button>
        </div>
      </Card>
    </>
  );
}

export default function Rates() {
  const [tab, setTab] = useState("cards");
  return (<><Tabs value={tab} onChange={setTab} tabs={[{ id: "cards", label: t("Price lists") }, { id: "routes", label: t("Routes") }]} />{tab === "cards" ? <Cards /> : <Routes />}</>);
}
