import { useState } from "react";
import { api } from "../api.js";
import { t } from "../i18n.jsx";
import { Button, Card, ErrorBox, Table, Tabs, Time, useAction, useLoad, useUi } from "../ui.jsx";
import { SenderReviewActions, SenderStatus, kindLabel } from "../senders/components.jsx";
import { adaptSender } from "../senders/model.js";

export default function Approvals({ me }) {
  const { confirm } = useUi();
  const canS = me.permissions.includes("*") || me.permissions.includes("sender:review");
  const canT = me.permissions.includes("*") || me.permissions.includes("template:review");
  const senders = useLoad((signal) => (canS ? api.get("/v1/sender-ids", { status: "pending" }, { signal }).then((rows) => rows.map(adaptSender)) : Promise.resolve([])), [canS], 10000);
  const templates = useLoad(() => (canT ? api.get("/v1/templates", { status: "pending" }) : Promise.resolve([])), [canT], 10000);
  const [tab, setTab] = useState(canS ? "senders" : "templates");
  const a = useAction();
  const decide = async (id, action) => { // vetëm template-t; sender-at kalojnë nga SenderReviewActions (kontrollon can_review_locally)
    const reason = action === "approve" ? null : await confirm({ title: action === "reject" ? t("Reject — tell them why") : t("Revoke — tell them why"), body: t("The customer sees this. Be specific so they can fix it."), input: t("Reason"), placeholder: t("e.g. Brand name doesn't match your company"), inputRequired: true, minLength: 3, confirmLabel: action === "reject" ? t("Reject") : t("Revoke"), danger: true });
    if (action !== "approve" && !reason) return;
    await a.run(() => api.post(`/v1/template-versions/${id}/${action}`, reason ? { reason } : {}), action === "approve" ? t("Approved") : t("Decision saved"));
    templates.reload();
  };
  const tplRows = (templates.data || []).flatMap((tp) => tp.versions.map((v) => ({ ...v, id: v.id, template: tp.name, owner_ref: tp.owner_ref })));
  return (
    <>
      <div className="help">{t("Approve what's legitimate: sender IDs should match the customer's brand; templates shouldn't be misleading or spam. Every decision is recorded with your name.")}</div>
      <Tabs value={tab} onChange={setTab} tabs={[...(canS ? [{ id: "senders", label: t("Sender IDs"), count: senders.data?.length }] : []), ...(canT ? [{ id: "templates", label: t("Templates"), count: tplRows.length }] : [])]} />
      <ErrorBox error={a.error || senders.error || templates.error} />
      {tab === "senders" && (
        <Card title={t("Sender IDs waiting for review")}>
          {(senders.data || []).some((r) => !r.canReviewLocally) && <div className="alert warn" role="status"><span>{t("Sender approvals are managed centrally.")}</span></div>}
          <Table rows={senders.data || []} loading={senders.loading} emptyTitle={t("All caught up")} empty={t("No sender IDs are waiting.")} cols={[{ label: t("Customer"), render: (r) => <b>{r.ownerRef}</b> }, { label: t("Sender"), render: (r) => <code>{r.value}</code> }, { label: t("Country"), key: "country" }, { label: t("Type"), render: (r) => kindLabel(r.kind) }, { label: t("Status"), render: (r) => <SenderStatus sender={r} staff /> }, { label: t("Requested"), render: (r) => <Time value={r.createdAt} /> },
            { label: "", render: (r) => <SenderReviewActions sender={r} me={me} onDone={senders.reload} /> }]} />
        </Card>
      )}
      {tab === "templates" && (
        <Card title={t("Template versions waiting for review")}>
          <Table rows={tplRows} loading={templates.loading} emptyTitle={t("All caught up")} empty={t("No templates are waiting.")} cols={[{ label: t("Customer"), render: (r) => <b>{r.owner_ref}</b> }, { label: t("Template"), render: (r) => `${r.template} v${r.version}` }, { label: t("Wording"), render: (r) => <code>{r.body}</code> }, { label: t("Blanks"), render: (r) => r.variables.join(", ") || "-" },
            { label: "", render: (r) => <div className="row"><Button variant="primary" className="small" busy={a.busy} onClick={() => decide(r.id, "approve")}>{t("Approve")}</Button><Button variant="danger" className="small" busy={a.busy} onClick={() => decide(r.id, "reject")}>{t("Reject")}</Button></div> }]} />
        </Card>
      )}
    </>
  );
}
