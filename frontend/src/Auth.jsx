import { useEffect, useState } from "react";
import { api, setKey } from "./api.js";
import { Button, ErrorBox, Field } from "./ui.jsx";

function PasswordInput({ value, onChange, autoComplete, autoFocus, placeholder }) {
  const [show, setShow] = useState(false);
  return (
    <div className="pw">
      <input type={show ? "text" : "password"} value={value} onChange={(e) => onChange(e.target.value)} autoComplete={autoComplete} autoFocus={autoFocus} placeholder={placeholder} />
      <button type="button" className="link" onClick={() => setShow(!show)} aria-pressed={show}>{show ? "Hide" : "Show"}</button>
    </div>
  );
}
export { PasswordInput };

// Ndihmë e drejtpërdrejtë ndërsa shkruhet fjalëkalimi (rregullat e plota i kontrollon serveri).
export function PasswordHints({ value }) {
  const ok = value.length >= 10;
  return <small className={ok ? "good-t" : "muted"}>{ok ? "✓ Long enough" : `At least 10 characters (${value.length}/10). A few random words works well.`}</small>;
}

function Brand({ children }) {
  return <><div className="brand big">SMS<span>Platform</span></div>{children}</>;
}

function Forgot({ onBack }) {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try { await api.post("/v1/auth/forgot", { email }); setSent(true); }
    catch (err) { setError(err); } finally { setBusy(false); }
  };
  return (
    <div className="login">
      <form className="card login-card" onSubmit={submit}>
        <Brand />
        {sent ? (
          <>
            <div className="alert good" role="status"><span><b>Check your email.</b> If <b>{email}</b> has an account, we've sent a link to choose a new password. It works once and expires in 1 hour.</span></div>
            <small className="muted">Nothing after a few minutes? Check spam, or try again in 2 minutes. Your administrator can also send you a link.</small>
            <Button type="button" onClick={onBack}>Back to sign in</Button>
          </>
        ) : (
          <>
            <p className="muted">Enter your email and we'll send you a link to choose a new password.</p>
            <Field label="Email"><input type="email" autoFocus autoComplete="username" placeholder="you@company.com" value={email} onChange={(e) => setEmail(e.target.value)} /></Field>
            <ErrorBox error={error} />
            <Button variant="primary" busy={busy} disabled={!email.trim()}>Send reset link</Button>
            <button type="button" className="link" onClick={onBack}>Back to sign in</button>
          </>
        )}
      </form>
    </div>
  );
}

export function Login({ onLogin, notice }) {
  const [mode, setMode] = useState("password");
  const [forgot, setForgot] = useState(false);
  const [canReset, setCanReset] = useState(false);
  useEffect(() => { api.get("/v1/auth/config").then((c) => setCanReset(!!c.self_service_reset)).catch(() => {}); }, []);
  const [f, setF] = useState({ email: "", password: "", key: "", remember: false });
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (k) => (v) => setF((x) => ({ ...x, [k]: v }));

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      if (mode === "password") {
        const r = await api.post("/v1/auth/login", { email: f.email, password: f.password, remember: f.remember });
        setKey(r.token, f.remember);
      } else setKey(f.key.trim());
      onLogin(await api.get("/v1/me"));
    } catch (err) {
      setKey("");
      setError(mode === "key" && err.status === 401 ? new Error("That API key isn't valid. Check for a missing character at the end.") : err);
    } finally { setBusy(false); }
  };

  if (forgot) return <Forgot onBack={() => setForgot(false)} />;
  return (
    <div className="login">
      <form className="card login-card" onSubmit={submit}>
        <Brand />
        {notice && <div className="alert warn" role="status">{notice}</div>}
        {mode === "password" ? (
          <>
            <p className="muted">Sign in to your account.</p>
            <Field label="Email"><input type="email" autoFocus autoComplete="username" placeholder="you@company.com" value={f.email} onChange={(e) => set("email")(e.target.value)} /></Field>
            <Field label="Password"><PasswordInput value={f.password} onChange={set("password")} autoComplete="current-password" /></Field>
            {canReset && <button type="button" className="link" style={{ alignSelf: "flex-start" }} onClick={() => setForgot(true)}>Forgot your password?</button>}
            <label className="chip"><input type="checkbox" checked={f.remember} onChange={(e) => set("remember")(e.target.checked)} /> Keep me signed in for 30 days on this device</label>
          </>
        ) : (
          <>
            <p className="muted">Developers can sign in with an API key.</p>
            <Field label="API key"><input type="password" autoFocus autoComplete="off" placeholder="sms_xxxxxxxx_…" value={f.key} onChange={(e) => set("key")(e.target.value)} /></Field>
          </>
        )}
        <ErrorBox error={error} />
        <Button variant="primary" busy={busy} disabled={mode === "password" ? !f.email.trim() || !f.password : !f.key.trim()}>Sign in</Button>
        <button type="button" className="link" onClick={() => { setMode(mode === "password" ? "key" : "password"); setError(null); }}>
          {mode === "password" ? "Use an API key instead" : "Use email and password instead"}
        </button>
        <small className="muted">{mode === "password" ? canReset ? "" : "Forgot your password? Ask your account manager for a reset link." : "The key stays in this browser tab only and is sent securely with each request."}</small>
      </form>
    </div>
  );
}

// Faqja e hapur nga lidhja e ftesës ose e rivendosjes: #accept/<token>
export function Accept({ token, onLogin }) {
  const [info, setInfo] = useState(null);
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.get(`/v1/auth/invite/${token}`).then(setInfo).catch((e) => setError(e)); }, [token]);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      const r = await api.post(`/v1/auth/invite/${token}`, { password: pw });
      setKey(r.token);
      history.replaceState(null, "", "#");
      onLogin(await api.get("/v1/me"));
    } catch (err) { setError(err); } finally { setBusy(false); }
  };

  const reset = info?.kind === "reset";
  return (
    <div className="login">
      <form className="card login-card" onSubmit={submit}>
        <Brand />
        {!info && !error && <p className="muted">Checking your link…</p>}
        {!info && error && (
          <>
            <ErrorBox error={error} />
            <a className="btn" href="#" onClick={() => { history.replaceState(null, "", "#"); location.reload(); }}>Go to sign in</a>
          </>
        )}
        {info && (
          <>
            <p className="muted">{reset ? "Choose a new password for" : "Welcome! Choose a password for"} <b>{info.email}</b>.</p>
            <input type="email" value={info.email} autoComplete="username" readOnly hidden />
            <Field label="New password"><PasswordInput value={pw} onChange={setPw} autoComplete="new-password" autoFocus /></Field>
            <PasswordHints value={pw} />
            <Field label="Repeat password" error={pw2 && pw !== pw2 ? "The passwords don't match yet." : null}><PasswordInput value={pw2} onChange={setPw2} autoComplete="new-password" /></Field>
            <ErrorBox error={error} />
            <Button variant="primary" busy={busy} disabled={pw.length < 10 || pw !== pw2}>{reset ? "Set new password" : "Create my account"}</Button>
          </>
        )}
      </form>
    </div>
  );
}
