import { T, t } from "../i18n.jsx";
import AuditLog from "../audit/AuditLog.jsx";
import { Card } from "../ui.jsx";
import { buildHash } from "../routes.js";

export default function AuditCommercial({ query = {} }) {
  return (
    <>
      <AuditLog routeId="audit-commercial" fixedScope="commercial" query={query} title={t("Commercial audit")}
        subtitle={t("Staff actions on plans, prices, subscriptions, invoices, payments and credit. Read-only; days are UTC.")} />
      <Card title={t("Payment, grant and credit-note records")}>
        <p className="muted">{t("Who decided a payment, grant or credit note is also recorded on the record itself. Open the record to see it.")}</p>
        <p><a href={buildHash("com-payments")}>{t("Payments")}</a> · <a href={buildHash("com-credit")}>{t("Credit & Grants")}</a> · <a href={buildHash("com-invoices")}>{t("Invoices")}</a> · <a href={buildHash("com-recon")}>{t("Reconciliation")}</a></p>
      </Card>
    </>
  );
}
