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
import Inbox from "./pages/Inbox.jsx";
import Account from "./pages/Account.jsx";
import Users from "./pages/Users.jsx";
import { Accept, Login } from "./Auth.jsx";
import Reports from "./pages/Reports.jsx";
import Admin from "./pages/Admin.jsx";

// perm: string ose "a|b" (mjafton një); global: faqe që s'kërkon llogari të zgjedhur (staf)
const NAV = [
  { group: "", items: [{ id: "dashboard", label: "Overview", icon: "◧", perm: null, el: Dashboard, desc: "Where things stand right now." },
    { id: "account", label: "My account", icon: "☺", perm: null, el: Account, global: true, desc: "Your password and the devices you're signed in on." },
  ] },
  { group: "Messaging", items: [
    { id: "send", label: "Send", icon: "➤", perm: "messages:send", el: Send, desc: "Send an SMS or an email. You see the price before you send." },
    { id: "inbox", label: "Inbox", icon: "✆", perm: "messages:read", el: Inbox, desc: "Replies from the people you message. Read them and answer." },
    { id: "messages", label: "Message history", icon: "☷", perm: "messages:read", el: Messages, desc: "Everything you've sent and what happened to it." },
    { id: "reports", label: "Reports", icon: "▥", perm: "messages:read", el: Reports, desc: "How your messages perform: delivery, spend and what went wrong." },
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
    { id: "users", label: "People", icon: "☻", perm: "keys:manage", el: Users, global: true, desc: "Invite people, reset passwords and switch access on or off." },
    { id: "admin", label: "Admin", icon: "⚙", perm: "keys:manage", el: Admin, global: true, desc: "Kill switches and the audit log." },
  ] },
];

const can = (me, perm) =>
  !perm || me.permissions.includes("*") || perm.split("|").some((p) => me.permissions.includes(p));

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
  const [page, setPage] = useState(location.hash.slice(1).split("/")[0] || "dashboard");
  const [menu, setMenu] = useState(false);
  useEffect(() => {
    const h = () => { setPage(location.hash.slice(1).split("/")[0] || "dashboard"); setMenu(false); scrollTo(0, 0); };
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
  const unread = useLoad(() => (effectiveOwner && can(me, "messages:read") ? api.get("/v1/inbox/unread-count", { owner_ref: effectiveOwner }).catch(() => null) : Promise.resolve(null)), [effectiveOwner], 20000);
  const unreadCount = unread.data?.unread || 0;

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
                  <i aria-hidden>{n.icon}</i>{n.label}{n.id === "inbox" && unreadCount > 0 && <span className="count nav-count" aria-label={`${unreadCount} unread`}>{unreadCount > 99 ? "99+" : unreadCount}</span>}
                </a>
              ))}
            </div>
          ))}
        </nav>
        <div className="side-f">
          <div className="who"><b>{me.email || me.owner_ref || "Staff"}</b><small>{me.role}{me.owner_ref && me.email ? ` · ${me.owner_ref}` : me.email ? "" : ` · ${me.actor}`}</small></div>
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
          <Page key={`${current.id}:${effectiveOwner}`} me={me} owner={effectiveOwner} onUnreadChange={unread.reload} pick={(o) => { setOwnerState(o); location.hash = "dashboard"; }} />
        )}
      </main>
    </div>
  );
}

export default function App() {
  const [me, setMe] = useState(null);
  const [checking, setChecking] = useState(!!getKey());
  const [notice, setNotice] = useState("");
  const [accept, setAccept] = useState(() => /^#accept\/(.+)$/.exec(location.hash)?.[1] || "");
  const clear = () => { setKey(""); sessionStorage.removeItem("sms_owner"); setMe(null); };
  useEffect(() => {
    if (!getKey() || accept) { setChecking(false); return; }
    api.get("/v1/me").then(setMe).catch(() => setKey("")).finally(() => setChecking(false));
  }, []); // eslint-disable-line
  // Sesioni skadoi ose u mbyll diku tjetër: kthehu te hyrja me një shpjegim.
  useEffect(() => {
    const h = () => { clear(); setNotice("Your session ended. Please sign in again."); };
    addEventListener("sms:unauthorized", h);
    return () => removeEventListener("sms:unauthorized", h);
  }, []);
  const logout = async () => {
    if (me?.via === "password") await api.post("/v1/auth/logout").catch(() => {});
    clear(); setNotice(""); location.hash = "";
  };
  const signedIn = (m) => { setNotice(""); setAccept(""); setMe(m); };
  return (
    <UiProvider>
      {checking ? <div className="center muted">Loading…</div>
        : me ? <Shell me={me} onLogout={logout} />
        : accept ? <Accept token={accept} onLogin={signedIn} />
        : <Login onLogin={signedIn} notice={notice} />}
    </UiProvider>
  );
}
