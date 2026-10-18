import { t } from "../i18n.jsx";
import AuditLog from "../audit/AuditLog.jsx";

export default function AuditStaff({ query = {} }) {
  return <AuditLog routeId="audit-staff" query={query} title={t("Staff activity")} subtitle={t("Every audited action by staff and system processes, newest first. Entries can't be edited or removed. Days are UTC.")} />;
}
