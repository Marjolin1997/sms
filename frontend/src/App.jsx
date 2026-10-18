import { Suspense, lazy, useEffect, useLayoutEffect, useMemo, useState } from "react";
import { api, getKey, setKey, setOwner, setSessionExpiredHandler } from "./api.js";
import { Button, ErrorBoundary, ErrorBox, Field, PageHeader, Skeleton, UiProvider, useLoad } from "./ui.jsx";
import { I18nProvider, LangSwitch, t } from "./i18n.jsx";
import { can, isStaffPrincipal, resolveRoute } from "./routes.js";
import { Forbidden, NotFound } from "./pages/ErrorPages.jsx";

// Faqet ngarkohen sipas kërkesës (code splitting): një komponent lazy për rrugë, i ruajtur një herë.
const lazyPages = new Map();
const pageOf = (route) => {
  if (!lazyPages.has(route.id)) lazyPages.set(route.id, lazy(route.load));
  return lazyPages.get(route.id);
};

function Login({ onLogin, notice }) {
  const [key, setK] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setError(null);
    setKey(key.trim());
    try { onLogin(await api.get("/v1/me")); }
    catch (err) { setKey(""); setError(err.status === 401 ? new Error(t("That API key isn't valid. Check for a missing character at the end.")) : err); }
    finally { setBusy(false); }
  };
  return (
    <div className="login">
      <form className="card login-card" onSubmit={submit}>
        <div className="brand big">SMS<span>Platform</span></div>
        <p className="muted">{t("Sign in with your API key. Don't have one? Ask your account manager.")}</p>
        <Field label={t("API key")}>
          <input type="password" autoFocus autoComplete="off" placeholder="sms_xxxxxxxx_…" value={key} onChange={(e) => setK(e.target.value)} />
        </Field>
        {notice && !error && <div className="alert warn" role="status">{notice}</div>}
        <ErrorBox error={error} />
        <Button variant="primary" busy={busy} disabled={!key.trim()}>{t("Sign in")}</Button>
        <small className="muted">{t("The key stays in this browser tab only and is sent securely with each request.")}</small>
        <LangSwitch />
      </form>
    </div>
  );
}

function AccountPicker({ owner, setOwner: set, canList }) {
  const accounts = useLoad(() => (canList ? api.get("/v1/admin/accounts") : Promise.resolve([])), [canList]);
  return (
    <div className="row">
      <label className="muted small" htmlFor="acct">{t("Account")}</label>
      <input id="acct" className="owner" list="accts" placeholder={t("Choose an account")} value={owner} onChange={(e) => set(e.target.value.trim())} />
      <datalist id="accts">{(accounts.data || []).map((a) => <option key={a.owner_ref} value={a.owner_ref} />)}</datalist>
    </div>
  );
}

function useOnline() {
  const [on, set] = useState(() => (typeof navigator === "undefined" ? true : navigator.onLine !== false));
  useEffect(() => {
    const up = () => set(true), down = () => set(false);
    addEventListener("online", up); addEventListener("offline", down);
    return () => { removeEventListener("online", up); removeEventListener("offline", down); };
  }, []);
  return on;
}

function Shell({ me, onLogout }) {
  const [owner, setOwnerState] = useState(() => sessionStorage.getItem("sms_owner") || "");
  const [hash, setHash] = useState(location.hash);
  const [menu, setMenu] = useState(false);
  const online = useOnline();
  useEffect(() => {
    const h = () => { setHash(location.hash); setMenu(false); scrollTo(0, 0); };
    addEventListener("hashchange", h);
    return () => removeEventListener("hashchange", h);
  }, []);
  const effectiveOwner = me.owner_ref || owner;
  useEffect(() => { sessionStorage.setItem("sms_owner", owner); }, [owner]);

  const isStaff = isStaffPrincipal(me);
  const [unread, setUnread] = useState(0);
  useEffect(() => {
    if (isStaff || !can(me, "inbox:read")) return;
    const load = () => !document.hidden && api.get("/v1/inbox/unread").then((r) => setUnread(r.unread)).catch(() => {});
    load();
    const timer = setInterval(load, 30000);
    return () => clearInterval(timer);
  }, [isStaff, hash]); // eslint-disable-line
  const resolved = useMemo(() => resolveRoute(me, hash), [me, hash]);
  const { groups, route } = resolved;
  const ok = resolved.status === "ok";
  const needsAccount = ok && isStaff && !route.global && route.id !== "dashboard" && !effectiveOwner && !(route.detail && resolved.rest.length);
  // Faqet globale të stafit (miratime, tarifa...) s'duhet të filtrohen nga llogaria e zgjedhur më parë.
  setOwner(ok && route.global ? "" : effectiveOwner);
  const Page = ok ? pageOf(route) : null;
  const title = ok ? t(route.label) : resolved.status === "forbidden" ? t("No access") : t("Page not found");
  const crumbs = ok ? resolved.crumbs.map((c) => ({ ...c, label: t(c.label) })) : [];

  return (
    <div className={`shell ${menu ? "menu-open" : ""}`}>
      <a className="skip" href="#main-content">{t("Skip to content")}</a>
      <aside className="side">
        <div className="brand">SMS<span>Platform</span></div>
        <nav aria-label={t("Main navigation")}>
          {groups.map((g) => (
            <div key={g.group} className="nav-group">
              {g.group && <div className="nav-h">{t(g.group)}</div>}
              {g.items.map((n) => (
                <a key={n.id} href={`#${n.id}`} aria-current={ok && n.id === route.id ? "page" : undefined} className={ok && n.id === route.id ? "active" : ""}>
                  <i aria-hidden>{n.icon}</i>{t(n.label)}{n.id === "inbox" && unread > 0 && <span className="count">{unread}</span>}
                </a>
              ))}
            </div>
          ))}
        </nav>
        <div className="side-f">
          <div className="who"><b>{me.owner_ref || t("Staff")}</b><small>{me.role} · {me.actor}</small></div>
          <LangSwitch />
          <Button onClick={onLogout}>{t("Sign out")}</Button>
        </div>
      </aside>
      <div className="scrim" onClick={() => setMenu(false)} />
      <main id="main-content">
        <PageHeader lead={<button className="hamburger" aria-label={t("Open menu")} onClick={() => setMenu(!menu)}>☰</button>} title={title} desc={ok ? t(route.desc) : null} crumbs={crumbs}
          actions={ok && isStaff && !route.global ? <AccountPicker owner={owner} set={setOwnerState} canList={can(me, "monitor:read")} /> : null} />
        {!online && <div className="alert warn" role="status">{t("You're offline. Changes can't be saved until the connection is back.")}</div>}
        {isStaff && me.two_factor_required && !me.two_factor && (
          <div className="alert warn"><span>{t("Two-factor is required for sensitive actions.")} <a href="#security">{t("Set it up now")}</a></span></div>
        )}
        {resolved.status === "forbidden" ? <Forbidden /> : resolved.status === "not_found" ? <NotFound /> : needsAccount ? (
          <div className="alert warn">{t("Choose an account above to work on its data. Staff pages in the menu don't need one.")}</div>
        ) : (
          <ErrorBoundary resetKey={`${route.id}:${effectiveOwner}`}>
            <Suspense fallback={<Skeleton rows={4} />}>
              <Page key={`${route.id}:${effectiveOwner}`} me={me} owner={effectiveOwner} rest={resolved.rest} pick={(o) => { setOwnerState(o); location.hash = "dashboard"; }} />
            </Suspense>
          </ErrorBoundary>
        )}
      </main>
    </div>
  );
}

export default function App() {
  const [me, setMe] = useState(null);
  const [checking, setChecking] = useState(!!getKey());
  const [notice, setNotice] = useState(null);
  useEffect(() => {
    if (!getKey()) return;
    api.get("/v1/me").then(setMe).catch(() => setKey("")).finally(() => setChecking(false));
  }, []);
  // Burimi i vetëm i gjendjes së auth: `me` (+ çelësi në sessionStorage). 401 gjatë sesionit ⇒ hyrje me njoftim; 403 ⇒ vetëm gabim.
  useLayoutEffect(() => { // sinkron me commit-in: asnjë 401 pas hyrjes nuk humbet para regjistrimit
    if (!me) return;
    setSessionExpiredHandler(() => { setKey(""); sessionStorage.removeItem("sms_owner"); setMe(null); setNotice(t("Your session has ended. Please sign in again.")); location.hash = ""; });
    return () => setSessionExpiredHandler(null);
  }, [me]);
  const logout = () => { setKey(""); sessionStorage.removeItem("sms_owner"); setNotice(null); setMe(null); location.hash = ""; };
  return (
    <I18nProvider><UiProvider>
      {checking ? <div className="center muted" role="status">{t("Loading…")}</div> : !me ? <Login onLogin={(m) => { setNotice(null); setMe(m); }} notice={notice} /> : <Shell me={me} onLogout={logout} />}
    </UiProvider></I18nProvider>
  );
}
