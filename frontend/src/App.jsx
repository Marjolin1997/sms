import { useEffect, useState } from "react";
import { api, getKey, setKey, setOwner } from "./api.js";
import { Button, ErrorBox, Field } from "./ui.jsx";
import Dashboard from "./pages/Dashboard.jsx";
import Send from "./pages/Send.jsx";
import Campaigns from "./pages/Campaigns.jsx";
import Contacts from "./pages/Contacts.jsx";
import EmailDomains from "./pages/EmailDomains.jsx";
import Webhooks from "./pages/Webhooks.jsx";
import Billing from "./pages/Billing.jsx";
import Keys from "./pages/Keys.jsx";
import Admin from "./pages/Admin.jsx";

const NAV = [
  { id: "dashboard", label: "Overview", icon: "◧", perm: null, el: Dashboard },
  { id: "send", label: "Send", icon: "➤", perm: "messages:send", el: Send },
  { id: "campaigns", label: "Campaigns", icon: "✉", perm: "campaigns:read", el: Campaigns },
  { id: "contacts", label: "Contacts", icon: "☰", perm: "contacts:read", el: Contacts },
  { id: "email", label: "Email domains", icon: "@", perm: "email:read", el: EmailDomains },
  { id: "webhooks", label: "Webhooks & events", icon: "↯", perm: "events:read", el: Webhooks },
  { id: "billing", label: "Billing", icon: "€", perm: "billing:read", el: Billing },
  { id: "keys", label: "API keys", icon: "⚿", perm: "any-keys", el: Keys },
  { id: "admin", label: "Admin", icon: "⚙", perm: "keys:manage", el: Admin },
];

const can = (me, perm) =>
  !perm || me.permissions.includes("*") || me.permissions.includes(perm) ||
  (perm === "any-keys" && (me.permissions.includes("keys:self") || me.permissions.includes("*")));

function Login({ onLogin }) {
  const [key, setK] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setError(null);
    setKey(key.trim());
    try { onLogin(await api.get("/v1/me")); }
    catch (err) { setKey(""); setError(err.status === 401 ? new Error("That API key is not valid.") : err); }
    finally { setBusy(false); }
  };
  return (
    <div className="login">
      <form className="card login-card" onSubmit={submit}>
        <div className="brand big">SMS<span>Platform</span></div>
        <p className="muted">Sign in with your API key.</p>
        <Field label="API key">
          <input type="password" autoFocus autoComplete="off" placeholder="sms_xxxxxxxx_…" value={key} onChange={(e) => setK(e.target.value)} />
        </Field>
        <ErrorBox error={error} />
        <Button variant="primary" busy={busy} disabled={!key.trim()}>Sign in</Button>
        <small className="muted">The key stays in this tab only (session storage) and is sent as a Bearer token.</small>
      </form>
    </div>
  );
}

export default function App() {
  const [me, setMe] = useState(null);
  const [checking, setChecking] = useState(!!getKey());
  const [owner, setOwnerState] = useState("");
  const [page, setPage] = useState(location.hash.slice(1) || "dashboard");

  useEffect(() => {
    if (!getKey()) return;
    api.get("/v1/me").then((m) => { setMe(m); }).catch(() => setKey("")).finally(() => setChecking(false));
  }, []);
  useEffect(() => {
    const h = () => setPage(location.hash.slice(1) || "dashboard");
    addEventListener("hashchange", h);
    return () => removeEventListener("hashchange", h);
  }, []);
  useEffect(() => { setOwner(me?.owner_ref || owner); }, [me, owner]);

  if (checking) return <div className="center muted">Loading…</div>;
  if (!me) return <Login onLogin={setMe} />;

  const isStaff = !me.owner_ref;
  const items = NAV.filter((n) => can(me, n.perm));
  const current = items.find((n) => n.id === page) || items[0];
  const Page = current.el;
  const logout = () => { setKey(""); setMe(null); setOwnerState(""); location.hash = ""; };

  return (
    <div className="shell">
      <aside className="side">
        <div className="brand">SMS<span>Platform</span></div>
        <nav>
          {items.map((n) => (
            <a key={n.id} href={`#${n.id}`} className={n.id === current.id ? "active" : ""}>
              <i>{n.icon}</i>{n.label}
            </a>
          ))}
        </nav>
        <div className="side-f">
          <div className="who">
            <b>{me.owner_ref || "Staff"}</b>
            <small>{me.role} · {me.actor}</small>
          </div>
          <Button onClick={logout}>Sign out</Button>
        </div>
      </aside>
      <main>
        <header className="top">
          <h1>{current.label}</h1>
          {isStaff && (
            <div className="row">
              <small className="muted">Account</small>
              <input className="owner" placeholder="owner_ref, e.g. acme" value={owner} onChange={(e) => setOwnerState(e.target.value.trim())} />
            </div>
          )}
        </header>
        {isStaff && !owner && current.id !== "admin" && current.id !== "dashboard" ? (
          <div className="alert warn">Staff view: enter an account (owner_ref) above to see its data.</div>
        ) : (
          <Page key={`${current.id}:${me.owner_ref || owner}`} me={me} owner={me.owner_ref || owner} />
        )}
      </main>
    </div>
  );
}
