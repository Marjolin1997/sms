import { useState } from "react";
import { api } from "../api.js";
import { Badge, Button, Card, CopyButton, ErrorBox, Field, SecretBanner, Table, Time, useAction, useLoad, useUi } from "../ui.jsx";

const ROLES = ["client", "support", "finance", "pricing", "approver", "superadmin"];
const link = (token) => `${location.origin}${location.pathname}#accept/${token}`;

export default function Users() {
  const { confirm } = useUi();
  const users = useLoad(() => api.get("/v1/admin/users"), []);
  const accounts = useLoad(() => api.get("/v1/admin/accounts").catch(() => []), []);
  const [f, setF] = useState({ email: "", role: "client", owner_ref: "" });
  const [shown, setShown] = useState(null);
  const a = useAction(), b = useAction();
  const client = f.role === "client";

  const show = (email, token, reset) => setShown({ email, url: link(token), reset });
  const create = async () => {
    const r = await a.run(() => api.post("/v1/admin/users", { email: f.email, role: f.role, owner_ref: client ? f.owner_ref.trim() : null }), "User created");
    if (r?.invite_token) { show(r.email, r.invite_token); setF({ ...f, email: "" }); users.reload(); }
  };

  return (
    <>
      {shown && (
        <SecretBanner title={shown.reset ? `Password reset link for ${shown.email}` : `Invitation link for ${shown.email}`}
          note="Send it to them yourself (chat or email). It works once, for 72 hours, and can't be shown again. You can always make a new one." value={shown.url} onClose={() => setShown(null)} />
      )}
      <div className="help">People sign in with their email and a password they choose themselves, so you never see it. Customer users only see their own account; staff roles decide what staff can do.</div>
      <Card title="Invite a person">
        <div className="row wrap">
          <Field label="Email"><input type="email" value={f.email} placeholder="name@company.com" onChange={(e) => setF({ ...f, email: e.target.value })} /></Field>
          <Field label="Role"><select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>{ROLES.map((r) => <option key={r}>{r}</option>)}</select></Field>
          {client && (
            <Field label="Customer account" hint="Which account they belong to">
              <input list="acct-list" value={f.owner_ref} placeholder="e.g. acme" onChange={(e) => setF({ ...f, owner_ref: e.target.value })} />
              <datalist id="acct-list">{(accounts.data || []).map((x) => <option key={x.owner_ref} value={x.owner_ref} />)}</datalist>
            </Field>
          )}
          <Button variant="primary" busy={a.busy} disabled={!f.email.trim() || (client && !f.owner_ref.trim())} onClick={create}>Create invitation</Button>
        </div>
        <ErrorBox error={a.error} />
      </Card>
      <Card title="People">
        <ErrorBox error={b.error || users.error} retry={users.reload} />
        <Table rows={users.data || []} loading={users.loading} empty="No personal accounts yet. Invite the first person above." cols={[
          { label: "Email", render: (r) => <b>{r.email}</b> },
          { label: "Role", key: "role" },
          { label: "Account", render: (r) => r.owner_ref || "-" },
          { label: "Status", render: (r) => r.locked ? <Badge>locked</Badge> : r.invited ? <Badge>pending</Badge> : <Badge>{r.status}</Badge> },
          { label: "2FA", render: (r) => (r.mfa ? <Badge>on</Badge> : <span className="muted">off</span>) },
          { label: "Last sign-in", render: (r) => (r.last_login_at ? <Time value={r.last_login_at} /> : "never") },
          { label: "", render: (r) => (
            <div className="row wrap">
              {r.status === "active" && <Button className="small" busy={b.busy} onClick={async () => { const x = await b.run(() => api.post(`/v1/admin/users/${r.id}/reset`)); if (x?.invite_token) show(r.email, x.invite_token, !r.invited); }}>{r.invited ? "New invite link" : "Reset password"}</Button>}
              {r.mfa && <Button className="small" busy={b.busy} onClick={async () => { if (await confirm({ title: `Reset two-factor for ${r.email}?`, body: "Use this when they lost their phone and their recovery codes. Their 2FA is switched off and they're signed out everywhere. They can set it up again after signing in.", confirmLabel: "Reset 2FA", danger: true })) { await b.run(() => api.post(`/v1/admin/users/${r.id}/reset-2fa`), "Two-factor reset"); users.reload(); } }}>Reset 2FA</Button>}
              {r.status === "active"
                ? <Button variant="danger" className="small" busy={b.busy} onClick={async () => { if (await confirm({ title: `Disable ${r.email}?`, body: "They are signed out everywhere right away and can't sign in until you enable them again.", danger: true, confirmLabel: "Disable" })) { await b.run(() => api.post(`/v1/admin/users/${r.id}/disable`), "User disabled"); users.reload(); } }}>Disable</Button>
                : <Button className="small" busy={b.busy} onClick={async () => { await b.run(() => api.post(`/v1/admin/users/${r.id}/enable`), "User enabled"); users.reload(); }}>Enable</Button>}
            </div>
          ) },
        ]} />
      </Card>
    </>
  );
}
