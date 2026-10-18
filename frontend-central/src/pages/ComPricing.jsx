import { useState } from "react";
import { api } from "../api.js";
import { t } from "../i18n.jsx";
import { UUID, buildHash } from "../routes.js";
import { Button, Card, Field, Skeleton, StatusBadge, Table, useLoad } from "../ui.jsx";
import { ErrorState, DateOnly, EntLink, KV, Money, Note, Panel, ServerList, TabBar, Ts, humanize, shortId } from "../commercial/components.jsx";

function VersionRules({ id }) {
  const v = useLoad((signal) => api.get(`/admin/pricing/versions/${id}`, {}, { signal }), [id]);
  return (
    <Panel title={t("Price rules")} state={v} subtitle={v.data ? <>{t("Version {n}", { n: v.data.version })} · <StatusBadge>{v.data.status}</StatusBadge> · {t("Effective from")} <Ts value={v.data.effective_from} /></> : null}>
      {(d) => (
        <>
          <Table rows={(d.rules || []).map((r) => ({ ...r, id: r.rule_id }))} empty={t("This version has no rules.")} cols={[
            { label: t("Channel"), render: (r) => humanize(r.channel) },
            { label: t("Prefix"), render: (r) => <span className="mono">{r.prefix || "*"}</span> },
            { label: t("Operator"), render: (r) => r.operator || "*" },
            { label: t("Unit price"), num: true, render: (r) => <span className="mono-num">{r.unit_price}</span> },
          ]} />
          <Note>{t("The price that applies to a destination is decided by the server. Use the Preview tab to ask it.")}</Note>
        </>
      )}
    </Panel>
  );
}

function BookDetail({ id }) {
  const book = useLoad((signal) => api.get(`/admin/pricing/books/${id}`, {}, { signal }), [id]);
  const state = useLoad((signal) => api.get("/admin/pricing/state", {}, { signal }), []);
  const [ver, setVer] = useState(null);
  if (!UUID.test(id)) return null;
  const eff = state.data?.books.find((b) => b.book_id === id);
  return (
    <>
      <p><a href={buildHash("com-pricing")}>← {t("All price books")}</a></p>
      <Panel title={t("Price book")} state={book}>
        {(b) => (
          <>
            <KV rows={[[t("Code"), b.code], [t("Name"), b.name], [t("Currency"), b.currency], [t("Created"), <Ts value={b.created_at} />],
              eff ? [t("Effective version"), eff.effective_version_id ? shortId(eff.effective_version_id) : t("None")] : null, eff ? [t("Selection reason"), humanize(eff.reason)] : null]} />
            <Table rows={b.versions} empty={t("No versions.")} cols={[
              { label: t("Version"), key: "version", num: true },
              { label: t("Status"), render: (v) => <StatusBadge>{v.status}</StatusBadge> },
              { label: t("Effective from"), render: (v) => <Ts value={v.effective_from} /> },
              { label: t("Rules"), num: true, key: "rule_count" },
              { label: t("Imported"), hideSm: true, render: (v) => (v.imported ? t("Yes") : t("No")) },
              { label: "", render: (v) => <Button className="small" aria-label={t("Rules of version {n}", { n: v.version })} onClick={() => setVer(v.id)}>{t("Rules")}</Button> },
            ]} />
          </>
        )}
      </Panel>
      {ver && <VersionRules id={ver} />}
    </>
  );
}

function Books({ query }) {
  const state = useLoad((signal) => api.get("/admin/pricing/state", {}, { signal }), []);
  const eff = (id) => state.data?.books.find((b) => b.book_id === id);
  return (
    <ServerList route="com-pricing" query={query} path="/admin/pricing/books" title={t("Price books")} emptyTitle={t("No price books yet")} emptyText={t("Price books appear here once pricing is configured.")}
      cols={[
        { label: t("Code"), render: (b) => <a href={buildHash("com-pricing", [b.id])}><b>{b.code}</b></a> },
        { label: t("Name"), key: "name" },
        { label: t("Currency"), key: "currency" },
        { label: t("Effective version"), hideSm: true, render: (b) => (state.error ? t("unknown") : !state.data ? "…" : eff(b.id)?.effective_version_id ? shortId(eff(b.id).effective_version_id) : t("None")) },
        { label: t("Active versions"), hideSm: true, num: true, render: (b) => eff(b.id)?.active_versions ?? "-" },
        { label: "", render: (b) => <a className="btn small" href={buildHash("com-pricing", [b.id])} aria-label={t("Details for price book {code}", { code: b.code })}>{t("Details")}</a> },
      ]} />
  );
}

function Preview() {
  const [f, setF] = useState({ enterprise_id: "", product_id: "", channel: "sms", number: "", operator: "" });
  const [run, setRun] = useState(null);
  const ok = UUID.test(f.enterprise_id.trim()) && UUID.test(f.product_id.trim());
  const res = useLoad(async (signal) => (run ? api.get("/admin/pricing/preview", run, { signal }) : null), [JSON.stringify(run)]);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  return (
    <Card title={t("Price preview")} subtitle={t("Asks the server which price would apply. Nothing is saved.")}>
      <form className="form" onSubmit={(e) => { e.preventDefault(); setRun({ ...f, enterprise_id: f.enterprise_id.trim(), product_id: f.product_id.trim() }); }}>
        <div className="grid two">
          <Field label={t("Enterprise ID")}><input value={f.enterprise_id} onChange={set("enterprise_id")} /></Field>
          <Field label={t("Product ID")}><input value={f.product_id} onChange={set("product_id")} /></Field>
          <Field label={t("Channel")}><select value={f.channel} onChange={set("channel")}>{["sms", "email"].map((c) => <option key={c} value={c}>{c}</option>)}</select></Field>
          <Field label={t("Number")}><input value={f.number} maxLength={20} onChange={set("number")} /></Field>
          <Field label={t("Operator")}><input value={f.operator} maxLength={8} onChange={set("operator")} /></Field>
        </div>
        <div><Button variant="primary" disabled={!ok}>{t("Ask the server")}</Button></div>
      </form>
      {run && (res.error ? <ErrorState error={res.error} retry={res.reload} /> : !res.data ? <Skeleton rows={2} /> : (
        <KV rows={[[t("Unit price"), <b className="mono-num">{res.data.unit_price} {res.data.currency}</b>], [t("Price book"), res.data.book_id], [t("Version"), res.data.version_id], [t("Rule"), res.data.rule_id]]} />
      ))}
    </Card>
  );
}

export default function ComPricing({ rest, query }) {
  const { active, bar } = TabBar({ route: "com-pricing", query, tabs: [{ id: "books", label: t("Price books") }, { id: "assignments", label: t("Assignments") }, { id: "preview", label: t("Preview") }] });
  if (rest.length) return <BookDetail id={rest[0]} />;
  return (
    <>
      {bar}
      {active === "assignments" ? (
        <ServerList route="com-pricing" tab="assignments" query={query} path="/admin/pricing/assignments" title={t("Price assignments")} emptyTitle={t("No assignments yet")} emptyText={t("An enterprise needs an effective assignment to be priced.")}
          filters={[{ name: "enterprise_id", label: t("Enterprise ID") }, { name: "product_id", label: t("Product ID") }]}
          cols={[
            { label: t("Enterprise"), render: (a) => <EntLink id={a.enterprise_id} route="com-pricing" extra={{ tab: "assignments" }} /> },
            { label: t("Product"), render: (a) => <span className="mono" title={a.product_id}>{shortId(a.product_id)}</span> },
            { label: t("Price book"), render: (a) => <a className="mono" href={buildHash("com-pricing", [a.book_id])}>{shortId(a.book_id)}</a> },
            { label: t("Effective from"), render: (a) => <DateOnly value={a.effective_from} /> },
            { label: t("Created"), hideSm: true, render: (a) => <Ts value={a.created_at} /> },
          ]} />
      ) : active === "preview" ? <Preview /> : <Books query={query} />}
    </>
  );
}
