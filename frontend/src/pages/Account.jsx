import { useState } from "react";
import { api } from "../api.js";
import { PasswordHints, PasswordInput } from "../Auth.jsx";
import { Badge, Button, Card, Empty, ErrorBox, Field, Table, Time, useAction, useLoad, useUi } from "../ui.jsx";

const device = (ua) => {
  if (!ua) return "Unknown device";
  const os = /Windows/.test(ua) ? "Windows" : /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iOS" : /Mac OS/.test(ua) ? "macOS" : /Linux/.test(ua) ? "Linux" : "";
  const br = /Edg\//.test(ua) ? "Edge" : /Chrome\//.test(ua) ? "Chrome" : /Firefox\//.test(ua) ? "Firefox" : /Safari\//.test(ua) ? "Safari" : "Browser";
  return `${br}${os ? ` on ${os}` : ""}`;
};

function Password() {
  const { toast } = useUi();
  const [f, setF] = useState({ cur: "", next: "", again: "" });
  const a = useAction();
  const submit = async (e) => {
    e.preventDefault();
    const ok = await a.run(() => api.post("/v1/auth/change-password", { current_password: f.cur, new_password: f.next }), "Password changed. Your other devices were signed out.");
    if (ok) setF({ cur: "", next: "", again: "" });
  };
  return (
    <Card title="Change password" subtitle="You'll stay signed in here. Every other device is signed out.">
      <form className="form" onSubmit={submit} style={{ maxWidth: 420 }}>
        <Field label="Current password"><PasswordInput value={f.cur} onChange={(v) => setF({ ...f, cur: v })} autoComplete="current-password" /></Field>
        <Field label="New password"><PasswordInput value={f.next} onChange={(v) => setF({ ...f, next: v })} autoComplete="new-password" /></Field>
        <PasswordHints value={f.next} />
        <Field label="Repeat new password" error={f.again && f.again !== f.next ? "The passwords don't match yet." : null}><PasswordInput value={f.again} onChange={(v) => setF({ ...f, again: v })} autoComplete="new-password" /></Field>
        <ErrorBox error={a.error} />
        <div><Button variant="primary" busy={a.busy} disabled={!f.cur || f.next.length < 10 || f.next !== f.again}>Change password</Button></div>
      </form>
    </Card>
  );
}

function Sessions() {
  const { confirm } = useUi();
  const list = useLoad(() => api.get("/v1/auth/sessions"), []);
  const a = useAction();
  return (
    <Card title="Where you're signed in" subtitle="Sign out any device you don't recognise.">
      <ErrorBox error={list.error || a.error} retry={list.reload} />
      <Table rows={list.data || []} loading={list.loading} empty="No active sessions." cols={[
        { label: "Device", render: (r) => <><b>{device(r.user_agent)}</b> {r.current && <Badge>this device</Badge>}</> },
        { label: "Signed in", render: (r) => <Time value={r.created_at} /> },
        { label: "Last active", render: (r) => <Time value={r.last_seen_at} /> },
        { label: "", render: (r) => !r.current && (
          <Button variant="danger" className="small" busy={a.busy} onClick={async () => { if (await confirm({ title: "Sign out this device?", body: "It will need the password again.", confirmLabel: "Sign out device", danger: true })) { await a.run(() => api.del(`/v1/auth/sessions/${r.id}`), "Device signed out"); list.reload(); } }}>Sign out</Button>
        ) },
      ]} />
    </Card>
  );
}

export default function Account({ me }) {
  if (me.via !== "password")
    return (
      <Card title="You're signed in with an API key">
        <Empty icon="⚿" title="Personal sign-in isn't active for this session">Password and device settings belong to personal accounts. Ask your administrator to invite you by email, then sign in with email and password.</Empty>
      </Card>
    );
  return (
    <>
      <Card title="Your account"><div className="row wrap"><div><small className="muted">Email</small><div><b>{me.email}</b></div></div><div><small className="muted">Role</small><div><Badge>{me.role}</Badge></div></div>{me.owner_ref && <div><small className="muted">Account</small><div><b>{me.owner_ref}</b></div></div>}</div></Card>
      <Password />
      <Sessions />
    </>
  );
}
