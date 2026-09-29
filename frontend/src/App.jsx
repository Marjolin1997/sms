import { useEffect, useState } from "react";
import { api, getKey, setKey, setOwner } from "./api.js";
import { Button, ErrorBox, Field, UiProvider, useLoad } from "./ui.jsx";
import Dashboard from "./pages/Dashboard.jsx";
import Send from "./pages/Send.jsx";
import Messages from "./pages/Messages.jsx";
import Campaigns from "./pages/Campaigns.jsx";
import Contacts from "./pages/Contacts.jsx";
import Senders from "./pages/Senders.jsx";
import EmailDomains from "./pages/EmailDomains.jsx";
import Webhooks from "./pages/Webhooks.jsx";
import Wallet from "./pages/Wallet.jsx";
import Billing from "./pages/Billing.jsx";
import Keys from "./pages/Keys.jsx";
import Approvals from "./pages/Approvals.jsx";
import Accounts from "./pages/Accounts.jsx";
import Rates from "./pages/Rates.jsx";
import Finance from "./pages/Finance.jsx";
import Admin from "./pages/Admin.jsx";

// perm: string ose "a|b" (mjafton një); global: faqe që s'kërkon llogari të zgjedhur (staf)
const NAV = [
  { group: "", items: [{ id: "dashboard", label: "Overview", icon: "◧", perm: null, el: Dashboard, desc: "Where things stand right now." }] },
  { group: "Messaging", items: [
    { id: "send", label: "Send", icon: "➤", perm: "messages:send", el: Send, desc: "Send an SMS or an email. You see the price before you send." },
    { id: "messages", label: "Message history", icon: "☷", perm: "messages:read", el: Messages, desc: "Everything you've sent and what happened to it." },
    { id: "campaigns", label: "Campaigns", icon: "✉", perm: "campaigns:read", el: Campaigns, desc: "Send to a whole list on a schedule, with a budget cap." },
  ] },
  { group: "Audience", items: [
    { id: "contacts", label: "Contacts", icon: "☰", perm: "contacts:read", el: Contacts, desc: "Your people, their lists and what they agreed to receive." },
  ] },
  { group: "Setup", items: [
    { id: "senders", label: "Sender IDs & templates", icon: "✦", perm: "sender:request", el: Senders, desc: "The name people see, and reusable message templates. Both are reviewed by us." },
    { id: "email", label: "Email domains", icon: "@", perm: "email:read", el: EmailDomains, desc: "Prove you own your domain so mail lands in the inbox." },
    { id: "webhooks", label: "Webhooks & events", icon: "↯", perm: "events:read", el: Webhooks, desc: "Get delivery updates pushed to your own system." },
  ] },
  { group: "Money", items: [
    { id: "wallet", label: "Wallet", icon: "◎", perm: "wallet:read", el: Wallet, desc: "Prepaid balance for SMS. Every movement is on the ledger." },
    { id: "billing", label: "Billing", icon: "€", perm: "billing:read", el: Billing, desc: "Your plan, invoices and payments." },
  ] },
  { group: "Developers", items: [
    { id: "keys", label: "API keys", icon: "⚿", perm: "keys:self|keys:manage", el: Keys, desc: "Keys let your software talk to the platform." },
  ] },
  { group: "Staff", items: [
    { id: "approvals", label: "Approvals", icon: "✓", perm: "sender:review|template:review", el: Approvals, global: true, desc: "Sender IDs and templates waiting for a decision." },
    { id: "accounts", label: "Accounts", icon: "☖", perm: "monitor:read", el: Accounts, global: true, desc: "Every customer account at a glance." },
    { id: "rates", label: "Rates & routes", icon: "%", perm: "rates:read", el: Rates, global: true, desc: "Price lists, effective dates and which provider carries which country." },
    { id: "finance", label: "Finance", icon: "⊕", perm: "topup:confirm", el: Finance, global: true, desc: "Confirm top-ups and make audited balance corrections." },
    { id: "admin", label: "Admin", icon: "⚙", perm: "keys:manage", el: Admin, global: true, desc: "Kill switches and the audit log." },
  ] },
];

const can = (me, perm) =>
  !perm || me.permissions.includes("*") || perm.split("|").some((p) => me.permissions.includes(p));

function Login({ onLogin }) {
  const [key, setK] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setError(null);
    setKey(key.trim());
    try { onLogin(await api.get("/v1/me")); }
    catch (err) { setKey(""); setError(err.status === 401 ? new Error("That API key isn't valid. Check for a missing character at the end.") : err); }
    finally { setBusy(false); }
  };
  return (
    <div className="login">
      <form className="card login-card" onSubmit={submit}>
        <div className="brand big">SMS<span>Platform</span></div>
        <p className="muted">Sign in with your API key. Don't have one? Ask your account manager.</p>
        <Field label="API key">
          <input type="password" autoFocus autoComplete="off" placeholder="sms_xxxxxxxx_…" value={key} onChange={(e) => setK(e.target.value)} />
        </Field>
        <ErrorBox error={error} />
        <Button variant="primary" busy={busy} disabled={!key.trim()}>Sign in</Button>
        <small className="muted">The key stays in this browser tab only and is sent securely with each request.</small>
      </form>
    </div>
  );
}

function AccountPicker({ owner, setOwner: set, canList }) {
  const accounts = useLoad(() => (canList ? api.get("/v1/admin/accounts") : Promise.resolve([])), [canList]);
  return (
    <div className="row">
      <label className="muted small" htmlFor="acct">Account</label>
      <input id="acct" className="owner" list="accts" placeholder="Choose an account" value={owner} onChange={(e) => set(e.target.value.trim())} />
      <datalist id="accts">{(accounts.data || []).map((a) => <option key={a.owner_ref} value={a.owner_ref} />)}</datalist>
    </div>
  );
}

function Shell({ me, onLogout }) {
  const [owner, setOwnerState] = useState(() => sessionStorage.getItem("sms_owner") || "");
  const [page, setPage] = useState(location.hash.slice(1) || "dashboard");
  const [menu, setMenu] = useState(false);
  useEffect(() => {
    const h = () => { setPage(location.hash.slice(1) || "dashboard"); setMenu(false); scrollTo(0, 0); };
    addEventListener("hashchange", h);
    return () => removeEventListener("hashchange", h);
  }, []);
  const effectiveOwner = me.owner_ref || owner;
  useEffect(() => { sessionStorage.setItem("sms_owner", owner); }, [owner]);

  const isStaff = !me.owner_ref;
  // staf: vetëm faqet staf + ato që kanë kuptim me një llogari; klient: pa grupin Staff
  let groups = NAV.map((g) => ({ ...g, items: g.items.filter((i) => can(me, i.perm) && (isStaff || g.group !== "Staff")) })).filter((g) => g.items.length);
  // Stafi punon kryesisht te faqet e veta: grupi "Staff" del menjëherë pas Overview.
  if (isStaff) groups = [groups[0], ...groups.filter((g) => g.group === "Staff"), ...groups.slice(1).filter((g) => g.group !== "Staff")];
  const flat = groups.flatMap((g) => g.items);
  const current = flat.find((n) => n.id === page) || flat[0];
  const Page = current.el;
  const needsAccount = isStaff && !current.global && current.id !== "dashboard" && !effectiveOwner;
  // Faqet globale të stafit (miratime, tarifa...) s'duhet të filtrohen nga llogaria e zgjedhur më parë.
  setOwner(current.global ? "" : effectiveOwner);

  return (
    <div className={`shell ${menu ? "menu-open" : ""}`}>
      <a className="skip" href="#main-content">Skip to content</a>
      <aside className="side" aria-label="Main navigation">
        <div className="brand">SMS<span>Platform</span></div>
        <nav>
          {groups.map((g) => (
            <div key={g.group} className="nav-group">
              {g.group && <div className="nav-h">{g.group}</div>}
              {g.items.map((n) => (
                <a key={n.id} href={`#${n.id}`} aria-current={n.id === current.id ? "page" : undefined} className={n.id === current.id ? "active" : ""}>
                  <i aria-hidden>{n.icon}</i>{n.label}
                </a>
              ))}
            </div>
          ))}
        </nav>
        <div className="side-f">
          <div className="who"><b>{me.owner_ref || "Staff"}</b><small>{me.role} · {me.actor}</small></div>
          <Button onClick={onLogout}>Sign out</Button>
        </div>
      </aside>
      <div className="scrim" onClick={() => setMenu(false)} />
      <main id="main-content">
        <header className="top">
          <button className="hamburger" aria-label="Open menu" onClick={() => setMenu(!menu)}>☰</button>
          <div className="top-t"><h1>{current.label}</h1><p className="muted">{current.desc}</p></div>
          {isStaff && !current.global && <AccountPicker owner={owner} set={setOwnerState} canList={can(me, "monitor:read")} />}
        </header>
        {needsAccount ? (
          <div className="alert warn">Choose an account above to work on its data. Staff pages in the menu don't need one.</div>
        ) : (
          <Page key={`${current.id}:${effectiveOwner}`} me={me} owner={effectiveOwner} pick={(o) => { setOwnerState(o); location.hash = "dashboard"; }} />
        )}
      </main>
    </div>
  );
}

export default function App() {
  const [me, setMe] = useState(null);
  const [checking, setChecking] = useState(!!getKey());
  useEffect(() => {
    if (!getKey()) return;
    api.get("/v1/me").then(setMe).catch(() => setKey("")).finally(() => setChecking(false));
  }, []);
  const logout = () => { setKey(""); sessionStorage.removeItem("sms_owner"); setMe(null); location.hash = ""; };
  return (
    <UiProvider>
      {checking ? <div className="center muted">Loading…</div> : !me ? <Login onLogin={setMe} /> : <Shell me={me} onLogout={logout} />}
    </UiProvider>
  );
}
