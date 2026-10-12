import { useState } from "react";
import { api } from "../api.js";
import { t } from "../i18n.jsx";
import { UUID, buildHash } from "../routes.js";
import { Button, Card, Empty, StatusBadge, Table, useLoad } from "../ui.jsx";
import { formatQuantity } from "../commercial/format.js";
import { Actor, DateOnly, DetailModal, EntLink, KV, Money, Note, Panel, ServerList, TabBar, Ts, humanize, shortId } from "../commercial/components.jsx";

const INVOICE_STATUS = ["open", "paid", "void"];

const Party = ({ title, party }) => (
  <Card title={title}>
    {party && typeof party === "object" && Object.keys(party).length ? (
      <KV rows={Object.entries(party).filter(([, v]) => v === null || ["string", "number", "boolean"].includes(typeof v)).map(([k, v]) => [humanize(k), v === null || v === "" ? "-" : String(v)])} />
    ) : <Note>{t("Not recorded on this invoice.")}</Note>}
  </Card>
);

function Settlement({ s, currency }) {
  return (
    <Card title={t("Settlement")} subtitle={t("Computed by the server from the allocation and credit notes.")}>
      <KV rows={[
        [t("Credited total"), <Money value={s.credited_total} currency={currency} />], [t("Net amount"), <Money value={s.net_amount} currency={currency} />],
        [t("Allocation"), s.allocation ? <><Money value={s.allocation.amount} currency={s.allocation.currency} /> · <Ts value={s.allocation.allocated_at} /></> : t("No payment allocated")],
        s.allocation ? [t("Allocated by"), <Actor a={s.allocation.allocated_by} />] : null,
      ]} />
      <h4>{t("Payments")}</h4>
      <Table rows={s.payments} empty={t("No payments recorded for this invoice.")} cols={[
        { label: t("Payment"), render: (p) => <span className="mono">{shortId(p.id)}</span> },
        { label: t("Amount"), num: true, render: (p) => <Money value={p.amount} currency={p.currency} /> },
        { label: t("Status"), render: (p) => <StatusBadge>{p.status}</StatusBadge> },
        { label: t("Reference"), hideSm: true, render: (p) => p.external_reference || "-" },
        { label: t("Created"), render: (p) => <Ts value={p.created_at} /> },
      ]} />
      <h4>{t("Credit notes")}</h4>
      <Table rows={s.credit_notes} empty={t("No credit notes for this invoice.")} cols={[
        { label: t("Number"), key: "number" }, { label: t("Amount"), num: true, render: (n) => <Money value={n.amount} currency={n.currency} /> },
        { label: t("Reason"), key: "reason" }, { label: t("Issued"), render: (n) => <Ts value={n.issued_at} /> },
      ]} />
    </Card>
  );
}

function InvoiceDetail({ id }) {
  const inv = useLoad((signal) => api.get(`/admin/billing/invoices/${id}`, {}, { signal }), [id]);
  if (!UUID.test(id)) return null;
  return (
    <>
      <p><a href={buildHash("com-invoices")}>← {t("All invoices")}</a></p>
      <Panel title={t("Invoice")} state={inv}>
        {(i) => (
          <>
            <KV rows={[
              [t("Number"), <b>{i.number}</b>], [t("Status"), <StatusBadge>{i.status}</StatusBadge>], [t("Enterprise"), <EntLink id={i.enterprise_id} />],
              [t("Subscription"), <a className="mono" href={buildHash("com-subs", [i.enterprise_id])}>{shortId(i.subscription_id)}</a>],
              [t("Billing period"), <>#{i.period_index} · <DateOnly value={i.period_start} /> – <DateOnly value={i.period_end} /></>],
              [t("Plan version"), <span className="mono">{shortId(i.plan_version_id)}</span>],
              [t("Issued"), <Ts value={i.issued_at} />], [t("Due"), <DateOnly value={i.due_at} />], [t("Paid"), <Ts value={i.paid_at} />],
              i.voided_at ? [t("Voided"), <Ts value={i.voided_at} />] : null, i.voided_reason ? [t("Void reason"), i.voided_reason] : null,
              [t("Currency"), i.currency], [t("Subtotal"), <Money value={i.subtotal} currency={i.currency} />],
              [t("VAT rate"), i.vat_rate], [t("Tax"), <Money value={i.tax} currency={i.currency} />], [t("Total"), <b><Money value={i.total} currency={i.currency} /></b>],
            ]} />
            <Note>{t("All amounts are shown exactly as issued by the server; the console never recomputes them.")}</Note>
          </>
        )}
      </Panel>
      {inv.data && (
        <>
          <Card title={t("Invoice lines")}>
            <Table rows={inv.data.lines.map((l) => ({ ...l, id: l.line_no }))} empty={t("This invoice has no lines.")} cols={[
              { label: "#", key: "line_no" }, { label: t("Description"), key: "description" }, { label: t("Category"), hideSm: true, render: (l) => humanize(l.line_type) },
              { label: t("Quantity"), num: true, render: (l) => formatQuantity(l.quantity) },
              { label: t("Unit price"), num: true, render: (l) => <Money value={l.unit_price} currency={l.currency} /> },
              { label: t("Amount"), num: true, render: (l) => <Money value={l.amount} currency={l.currency} /> },
              { label: t("Usage period"), hideSm: true, render: (l) => (l.period_start ? <><DateOnly value={l.period_start} /> – <DateOnly value={l.period_end} /></> : "-") },
              { label: t("Pricing"), hideSm: true, render: (l) => (l.pricing_source ? humanize(l.pricing_source) : "-") },
            ]} />
          </Card>
          <div className="grid two"><Party title={t("Issuer")} party={inv.data.issuer} /><Party title={t("Billed to")} party={inv.data.bill_to} /></div>
          {inv.data.settlement ? <Settlement s={inv.data.settlement} currency={inv.data.currency} /> : <Empty title={t("No settlement information")}>{t("The server returned no settlement data for this invoice.")}</Empty>}
        </>
      )}
    </>
  );
}

function CreditNotes({ query }) {
  const [open, setOpen] = useState(null);
  return (
    <>
      <ServerList route="com-invoices" tab="credit-notes" query={query} path="/admin/billing/credit-notes" title={t("Credit notes")} emptyTitle={t("No credit notes yet")} emptyText={t("Credit notes issued against invoices appear here.")}
        filters={[{ name: "enterprise_id", label: t("Enterprise ID") }, { name: "invoice_id", label: t("Invoice ID") }]}
        cols={[
          { label: t("Number"), render: (n) => <b>{n.number}</b> }, { label: t("Enterprise"), render: (n) => <EntLink id={n.enterprise_id} /> },
          { label: t("Invoice"), render: (n) => <a className="mono" href={buildHash("com-invoices", [n.invoice_id])}>{shortId(n.invoice_id)}</a> },
          { label: t("Amount"), num: true, render: (n) => <Money value={n.amount} currency={n.currency} /> },
          { label: t("Reason"), hideSm: true, key: "reason" }, { label: t("Issued"), hideSm: true, render: (n) => <Ts value={n.issued_at} /> },
          { label: "", render: (n) => <Button className="small" aria-label={t("Details for credit note {n}", { n: n.number })} onClick={() => setOpen(n)}>{t("Details")}</Button> },
        ]} />
      {open && <DetailModal title={t("Credit note")} onClose={() => setOpen(null)} rows={[[t("Number"), open.number], [t("Invoice"), open.invoice_id], [t("Enterprise"), open.enterprise_id], [t("Amount"), <Money value={open.amount} currency={open.currency} />], [t("Reason"), open.reason], [t("Issued"), <Ts value={open.issued_at} />], [t("Created by"), <Actor a={open.created_by} />]]} />}
    </>
  );
}

export default function ComInvoices({ rest, query }) {
  const { active, bar } = TabBar({ route: "com-invoices", query, tabs: [{ id: "invoices", label: t("Invoices") }, { id: "credit-notes", label: t("Credit notes") }] });
  if (rest.length) return <InvoiceDetail id={rest[0]} />;
  return (
    <>
      {bar}
      {active === "credit-notes" ? <CreditNotes query={query} /> : (
        <ServerList route="com-invoices" query={query} path="/admin/billing/invoices" title={t("Invoices")} emptyTitle={t("No invoices yet")} emptyText={t("Invoices appear after the first billing period is processed.")}
          filters={[{ name: "enterprise_id", label: t("Enterprise ID") }, { name: "status", label: t("Status"), options: INVOICE_STATUS }]}
          cols={[
            { label: t("Invoice"), render: (i) => <a href={buildHash("com-invoices", [i.id])}><b>{i.number}</b></a> },
            { label: t("Enterprise"), render: (i) => <EntLink id={i.enterprise_id} /> },
            { label: t("Period"), hideSm: true, render: (i) => <><DateOnly value={i.period_start} /> – <DateOnly value={i.period_end} /></> },
            { label: t("Issued"), hideSm: true, render: (i) => <DateOnly value={i.issued_at} /> },
            { label: t("Due"), hideSm: true, render: (i) => <DateOnly value={i.due_at} /> },
            { label: t("Total"), num: true, render: (i) => <Money value={i.total} currency={i.currency} /> },
            { label: t("Status"), render: (i) => <StatusBadge>{i.status}</StatusBadge> },
            { label: "", render: (i) => <a className="btn small" href={buildHash("com-invoices", [i.id])} aria-label={t("Details for invoice {n}", { n: i.number })}>{t("View")}</a> },
          ]} />
      )}
    </>
  );
}
