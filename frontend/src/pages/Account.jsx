import { useState } from "react";
import qrcode from "qrcode-generator";
import { api } from "../api.js";
import { PasswordHints, PasswordInput } from "../Auth.jsx";
import { Badge, Button, Card, CopyButton, Empty, ErrorBox, Field, Table, Time, useAction, useLoad, useUi } from "../ui.jsx";

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

function Qr({ text }) {
  const qr = qrcode(0, "M");
  qr.addData(text); qr.make();
  return <div className="qr" role="img" aria-label="QR code to add this account to your authenticator app" dangerouslySetInnerHTML={{ __html: qr.createSvgTag({ scalable: true, margin: 2 }) }} />;
}

function RecoveryCodes({ codes, onDone }) {
  const text = codes.join("\n");
  const download = () => {
    const url = URL.createObjectURL(new Blob([`Recovery codes\nEach works once.\n\n${text}\n`], { type: "text/plain" }));
    Object.assign(document.createElement("a"), { href: url, download: "recovery-codes.txt" }).click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return (
    <div className="secret" role="alert">
      <div><b>Save your recovery codes now</b><div className="muted">If you lose your phone, each code lets you sign in once. They're shown only this time. Keep them somewhere safe, not on the same phone.</div></div>
      <code style={{ whiteSpace: "pre", lineHeight: 1.8 }}>{text}</code>
      <div className="row wrap"><CopyButton text={text} label="Copy all" /><Button onClick={download}>Download .txt</Button><Button variant="primary" onClick={onDone}>I've saved them</Button></div>
    </div>
  );
}

function TwoFactor() {
  const { confirm } = useUi();
  const st = useLoad(() => api.get("/v1/auth/2fa"), []);
  const [step, setStep] = useState("idle"); // idle | password | scan | disable | regen
  const [pw, setPw] = useState("");
  const [code, setCode] = useState("");
  const [setup, setSetup] = useState(null);
  const [codes, setCodes] = useState(null);
  const a = useAction();
  const reset = () => { setStep("idle"); setPw(""); setCode(""); setSetup(null); a.clear(); };
  const digits = (v) => v.replace(/\D/g, "").slice(0, 6);
  const on = st.data?.enabled;

  const start = async (e) => {
    e.preventDefault();
    const r = await a.run(() => api.post("/v1/auth/2fa/setup", { password: pw }));
    if (r) { setSetup(r); setStep("scan"); setPw(""); }
  };
  const enable = async (e) => {
    e.preventDefault();
    const r = await a.run(() => api.post("/v1/auth/2fa/enable", { code }), "Two-factor is on");
    if (r) { setCodes(r.recovery_codes); reset(); st.reload(); }
  };
  const turnOff = async (e) => {
    e.preventDefault();
    if (!(await confirm({ title: "Turn off two-factor?", body: "Your account will be protected by your password only.", danger: true, confirmLabel: "Turn off" }))) return;
    const r = await a.run(() => api.post("/v1/auth/2fa/disable", { password: pw, code }), "Two-factor is off");
    if (r) { reset(); st.reload(); }
  };
  const regen = async (e) => {
    e.preventDefault();
    const r = await a.run(() => api.post("/v1/auth/2fa/recovery-codes", { password: pw, code }));
    if (r) { setCodes(r.recovery_codes); reset(); st.reload(); }
  };
  const proof = (submit, label, danger) => (
    <form className="form" onSubmit={submit} style={{ maxWidth: 420 }}>
      <Field label="Your password"><PasswordInput value={pw} onChange={setPw} autoComplete="current-password" autoFocus /></Field>
      <Field label="Code from your app, or a recovery code"><input value={code} onChange={(e) => setCode(e.target.value)} autoComplete="one-time-code" placeholder="123456" /></Field>
      <ErrorBox error={a.error} />
      <div className="row"><Button variant={danger ? "danger" : "primary"} busy={a.busy} disabled={!pw || !code.trim()}>{label}</Button><Button type="button" onClick={reset}>Cancel</Button></div>
    </form>
  );

  return (
    <>
      {codes && <RecoveryCodes codes={codes} onDone={() => setCodes(null)} />}
      <Card title="Two-factor sign-in" subtitle="A second step at sign-in, so a stolen password isn't enough.">
        <ErrorBox error={st.error} retry={st.reload} />
        {st.data && step === "idle" && (
          on ? (
            <div className="row wrap">
              <span><Badge>on</Badge> <small className="muted">{st.data.recovery_left} recovery code{st.data.recovery_left === 1 ? "" : "s"} left{st.data.recovery_left <= 2 ? ". Make new ones soon." : ""}</small></span>
              <span style={{ flex: 1 }} />
              <Button onClick={() => setStep("regen")}>New recovery codes</Button>
              <Button variant="danger" onClick={() => setStep("disable")}>Turn off</Button>
            </div>
          ) : (
            <div className="row wrap"><span className="muted">Off. We recommend turning it on, especially for staff.</span><span style={{ flex: 1 }} /><Button variant="primary" onClick={() => setStep("password")}>Set up</Button></div>
          )
        )}
        {step === "password" && (
          <form className="form" onSubmit={start} style={{ maxWidth: 420 }}>
            <p className="muted">You'll need an authenticator app (Google Authenticator, Microsoft Authenticator, 1Password, Authy…). First, confirm your password.</p>
            <Field label="Your password"><PasswordInput value={pw} onChange={setPw} autoComplete="current-password" autoFocus /></Field>
            <ErrorBox error={a.error} />
            <div className="row"><Button variant="primary" busy={a.busy} disabled={!pw}>Continue</Button><Button type="button" onClick={reset}>Cancel</Button></div>
          </form>
        )}
        {step === "scan" && setup && (
          <form className="form" onSubmit={enable}>
            <div className="mfa-scan">
              <Qr text={setup.uri} />
              <div>
                <ol className="steps-list">
                  <li>Open your authenticator app and add an account by scanning the code.</li>
                  <li>Can't scan? Enter this key by hand: <code className="wrap-code">{setup.secret.match(/.{1,4}/g).join(" ")}</code> <CopyButton text={setup.secret} /></li>
                  <li>Type the 6-digit code the app shows to finish.</li>
                </ol>
                <Field label="6-digit code"><input inputMode="numeric" autoComplete="one-time-code" autoFocus value={code} onChange={(e) => setCode(digits(e.target.value))} placeholder="123456" style={{ maxWidth: 160, letterSpacing: "0.3em" }} /></Field>
                <ErrorBox error={a.error} />
                <div className="row"><Button variant="primary" busy={a.busy} disabled={code.length !== 6}>Turn on</Button><Button type="button" onClick={reset}>Cancel</Button></div>
              </div>
            </div>
          </form>
        )}
        {step === "disable" && proof(turnOff, "Turn off two-factor", true)}
        {step === "regen" && proof(regen, "Make new codes")}
      </Card>
    </>
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
      <TwoFactor />
      <Password />
      <Sessions />
    </>
  );
}
