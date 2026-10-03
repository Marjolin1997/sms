import { useState } from "react";
import { api } from "../api.js";
import { t } from "../i18n.jsx";
import { Badge, Button, Card, ErrorBox, Table, Tabs, Time, useAction, useLoad, useUi } from "../ui.jsx";

export default function Approvals({ me }) {
  const { confirm } = useUi();
  const canS = me.permissions.includes("*") || me.permissions.includes("sender:review");
  const canT = me.permissions.includes("*") || me.permissions.includes("template:review");
  const senders = useLoad(() => (canS ? api.get("/v1/sender-ids", { status: "pending" }) : Promise.resolve([])), [canS], 10000);
  const templates = useLoad(() => (canT ? api.get("/v1/templates", { status: "pending" }) : Promise.resolve([])), [canT], 10000);
  const [tab, setTab] = useState(canS ? "senders" : "templates");
  const a = useAction();
  const decide = async (kind, id, action) => {
    const reason = action === "approve" ? null : await confirm({ title: action === "reject" ? t("Reject — tell them why") : t("Revoke — tell them why"), body: t("The customer sees this. Be specific so they can fix it."), input: t("Reason"), placeholder: t("e.g. Brand name doesn't match your company"), inputRequired: true, minLength: 3, confirmLabel: action === "reject" ? t("Reject") : t("Revoke"), danger: true });
    if (action !== "approve" && !reason) return;
    const path = kind === "sender" ? `/v1/sender-ids/${id}/${action}` : `/v1/template-versions/${id}/${action}`;
    await a.run(() => api.post(path, reason ? { reason } : {}), action === "approve" ? t("Approved") : t("Decision saved"));
    senders.reload(); templates.reload();
  };
  const tplRows = (templates.data || []).flatMap((tp) => tp.versions.map((v) => ({ ...v, id: v.id, template: tp.name, owner_ref: tp.owner_ref })));
  return (
    <>
      <div className="help">{t("Approve what's legitimate: sender IDs should match the customer's brand; templates shouldn't be misleading or spam. Every decision is recorded with your name.")}</div>
      <Tabs value={tab} onChange={setTab} tabs={[...(canS ? [{ id: "senders", label: t("Sender IDs"), count: senders.data?.length }] : []), ...(canT ? [{ id: "templates", label: t("Templates"), count: tplRows.length }] : [])]} />
      <ErrorBox error={a.error || senders.error || templates.error} />
      {tab === "senders" && (
        <Card title={t("Sender IDs waiting for review")}>
          <Table rows={senders.data || []} loading={senders.loading} emptyTitle={t("All caught up")} empty={t("No sender IDs are waiting.")} cols={[{ label: t("Customer"), render: (r) => <b>{r.owner_ref}</b> }, { label: t("Sender"), render: (r) => <code>{r.value}</code> }, { label: t("Country"), key: "country" }, { label: t("Type"), render: (r) => t(r.kind) }, { label: t("Requested"), render: (r) => <Time value={r.created_at} /> },
            { label: "", render: (r) => <div className="row"><Button variant="primary" className="small" busy={a.busy} onClick={() => decide("sender", r.id, "approve")}>{t("Approve")}</Button><Button variant="danger" className="small" busy={a.busy} onClick={() => decide("sender", r.id, "reject")}>{t("Reject")}</Button></div> }]} />
        </Card>
      )}
      {tab === "templates" && (
        <Card title={t("Template versions waiting for review")}>
          <Table rows={tplRows} loading={templates.loading} emptyTitle={t("All caught up")} empty={t("No templates are waiting.")} cols={[{ label: t("Customer"), render: (r) => <b>{r.owner_ref}</b> }, { label: t("Template"), render: (r) => `${r.template} v${r.version}` }, { label: t("Wording"), render: (r) => <code>{r.body}</code> }, { label: t("Blanks"), render: (r) => r.variables.join(", ") || "-" },
            { label: "", render: (r) => <div className="row"><Button variant="primary" className="small" busy={a.busy} onClick={() => decide("template", r.id, "approve")}>{t("Approve")}</Button><Button variant="danger" className="small" busy={a.busy} onClick={() => decide("template", r.id, "reject")}>{t("Reject")}</Button></div> }]} />
        </Card>
      )}
    </>
  );
}
