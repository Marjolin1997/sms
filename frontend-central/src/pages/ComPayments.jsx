import { useState } from "react";
import { t } from "../i18n.jsx";
import { Button, StatusBadge } from "../ui.jsx";
import { Actor, DateOnly, DetailModal, EntLink, Money, ServerList, TabBar, Ts, humanize, shortId } from "../commercial/components.jsx";
import { buildHash } from "../routes.js";

const STATUSES = ["pending", "approved", "rejected"];

// Vetëm fushat e dokumentuara të backend-it: asnjë payload i papërpunuar i ofruesit nuk vjen nga këto API.
const rows = (p, invoice) => [
  [t("Payment"), p.id], [t("Purpose"), humanize(invoice ? p.purpose || "invoice" : "credit")], [t("Enterprise"), <EntLink id={p.enterprise_id} />],
  invoice ? [t("Invoice"), <a href={buildHash("com-invoices", [p.invoice_id])}>{shortId(p.invoice_id)}</a>] : [t("Account"), p.account_id],
  [t("Amount"), <Money value={p.amount} currency={p.currency} />], [t("Status"), <StatusBadge>{p.status}</StatusBadge>], [t("Source"), humanize(p.source)],
  [t("Reference"), p.external_reference || "-"], [t("Note"), p.note || "-"],
  [t("Created by"), <Actor a={p.created_by} />], [t("Created"), <Ts value={p.created_at} />],
  [t("Approved by"), <Actor a={p.approved_by} />], [t("Approved"), <Ts value={p.approved_at} />],
  [t("Rejected by"), <Actor a={p.rejected_by} />], [t("Rejected"), <Ts value={p.rejected_at} />], [t("Rejection reason"), p.rejection_reason || "-"],
  ...(invoice ? [[t("Allocation"), p.allocation_id || "-"]] : []),
];

export default function ComPayments({ query }) {
  const { active, bar } = TabBar({ route: "com-payments", query, tabs: [{ id: "credit", label: t("Credit payments") }, { id: "invoice", label: t("Invoice payments") }] });
  const [open, setOpen] = useState(null);
  const invoice = active === "invoice";
  const detail = (p) => <Button className="small" aria-label={t("Details for payment {id}", { id: shortId(p.id) })} onClick={() => setOpen(p)}>{t("Details")}</Button>;
  return (
    <>
      {bar}
      {invoice ? (
        <ServerList key="inv" route="com-payments" tab="invoice" query={query} path="/admin/billing/invoice-payments" title={t("Invoice payments")} emptyTitle={t("No invoice payments yet")} emptyText={t("Payments recorded against invoices appear here.")}
          filters={[{ name: "enterprise_id", label: t("Enterprise ID") }, { name: "invoice_id", label: t("Invoice ID") }, { name: "status", label: t("Status"), options: STATUSES }]}
          cols={[
            { label: t("Payment"), render: (p) => <span className="mono">{shortId(p.id)}</span> },
            { label: t("Enterprise"), render: (p) => <EntLink id={p.enterprise_id} /> },
            { label: t("Invoice"), render: (p) => <a className="mono" href={buildHash("com-invoices", [p.invoice_id])}>{shortId(p.invoice_id)}</a> },
            { label: t("Amount"), num: true, render: (p) => <Money value={p.amount} currency={p.currency} /> },
            { label: t("Status"), render: (p) => <StatusBadge>{p.status}</StatusBadge> },
            { label: t("Reference"), hideSm: true, render: (p) => p.external_reference || "-" },
            { label: t("Created"), hideSm: true, render: (p) => <Ts value={p.created_at} /> },
            { label: "", render: detail },
          ]} />
      ) : (
        <ServerList key="cr" route="com-payments" query={query} path="/admin/money/payments" title={t("Credit payments")} emptyTitle={t("No credit payments yet")} emptyText={t("Payments that fund a credit account appear here.")}
          filters={[{ name: "enterprise_id", label: t("Enterprise ID") }, { name: "account_id", label: t("Account ID") }, { name: "status", label: t("Status"), options: STATUSES }]}
          cols={[
            { label: t("Payment"), render: (p) => <span className="mono">{shortId(p.id)}</span> },
            { label: t("Enterprise"), render: (p) => <EntLink id={p.enterprise_id} /> },
            { label: t("Account"), hideSm: true, render: (p) => <a className="mono" href={buildHash("com-credit", [p.account_id])}>{shortId(p.account_id)}</a> },
            { label: t("Amount"), num: true, render: (p) => <Money value={p.amount} currency={p.currency} /> },
            { label: t("Status"), render: (p) => <StatusBadge>{p.status}</StatusBadge> },
            { label: t("Reference"), hideSm: true, render: (p) => p.external_reference || "-" },
            { label: t("Created"), hideSm: true, render: (p) => <Ts value={p.created_at} /> },
            { label: t("Decided"), hideSm: true, render: (p) => <DateOnly value={p.approved_at || p.rejected_at} /> },
            { label: "", render: detail },
          ]} />
      )}
      {open && <DetailModal title={t("Payment")} rows={rows(open, invoice)} onClose={() => setOpen(null)} />}
    </>
  );
}
