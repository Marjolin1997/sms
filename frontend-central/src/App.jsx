import { Suspense, lazy, useEffect, useLayoutEffect, useMemo, useState } from "react";
import { api, getToken, login, setSessionExpiredHandler, setToken } from "./api.js";
import { Button, ErrorBoundary, ErrorBox, Field, PageHeader, Skeleton, UiProvider } from "./ui.jsx";
import { I18nProvider, LangSwitch, t } from "./i18n.jsx";
import { NAV, canWrite, resolveRoute } from "./routes.js";
import { NotFound } from "./pages/ErrorPages.jsx";

const lazyPages = new Map();
const pageOf = (route) => {
  if (!lazyPages.has(route.id)) lazyPages.set(route.id, lazy(route.load));
  return lazyPages.get(route.id);
};

function Login({ onLogin, notice }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try { onLogin(await login(email, password)); }
    catch (err) { setError(err); setPassword(""); } // fjalëkalimi pastrohet; asnjëherë nuk ruhet
    finally { setBusy(false); }
  };
  return (
    <div className="login">
      <form className="card login-card" onSubmit={submit}>
        <div className="brand big">SMS<span>Central</span></div>
        <p className="muted">{t("Sign in with your staff account.")}</p>
        <Field label={t("Email")}><input type="email" autoFocus autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} /></Field>
        <Field label={t("Password")}><input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} /></Field>
        {notice && !error && <div className="alert warn" role="status">{notice}</div>}
        <ErrorBox error={error} />
        <Button variant="primary" busy={busy} disabled={!email.trim() || !password}>{t("Sign in")}</Button>
        <LangSwitch />
      </form>
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
  const [hash, setHash] = useState(location.hash);
  const [menu, setMenu] = useState(false);
  const online = useOnline();
  useEffect(() => {
    const h = () => { setHash(location.hash); setMenu(false); scrollTo(0, 0); };
    addEventListener("hashchange", h);
    return () => removeEventListener("hashchange", h);
  }, []);
  const resolved = useMemo(() => resolveRoute(hash), [hash]);
  const ok = resolved.status === "ok";
  const Page = ok ? pageOf(resolved.route) : null;
  const write = canWrite(me);
  const title = ok ? t(resolved.route.label) : t("Page not found");
  const crumbs = ok ? resolved.crumbs.map((c) => ({ ...c, label: t(c.label) })) : [];
  return (
    <div className={`shell ${menu ? "menu-open" : ""}`}>
      <a className="skip" href="#main-content">{t("Skip to content")}</a>
      <aside className="side">
        <div className="brand">SMS<span>Central</span></div>
        <nav aria-label={t("Main navigation")}>
          {NAV.map((g) => (
            <div key={g.group} className="nav-group">
              {g.group && <div className="nav-h">{t(g.group)}</div>}
              {g.items.map((n) => (
                <a key={n.id} href={`#${n.id}`} aria-current={ok && n.id === resolved.route.id ? "page" : undefined} className={ok && n.id === resolved.route.id ? "active" : ""}>
                  <i aria-hidden>{n.icon}</i>{t(n.label)}
                </a>
              ))}
            </div>
          ))}
        </nav>
        <div className="side-f">
          <div className="who"><b>{me.email}</b><small><span className="badge role">{t(me.role)}</span> {!write && <span className="readonly-note">{t("Read-only access")}</span>}</small></div>
          <LangSwitch />
          <Button onClick={onLogout}>{t("Sign out")}</Button>
        </div>
      </aside>
      <div className="scrim" onClick={() => setMenu(false)} />
      <main id="main-content">
        <PageHeader lead={<button className="hamburger" aria-label={t("Open menu")} onClick={() => setMenu(!menu)}>☰</button>} title={title} desc={ok ? t(resolved.route.desc) : null} crumbs={crumbs} />
        {!online && <div className="alert warn" role="status">{t("You're offline. Changes can't be saved until the connection is back.")}</div>}
        {!ok ? <NotFound /> : (
          <ErrorBoundary resetKey={hash}>
            <Suspense fallback={<Skeleton rows={4} />}>
              <Page key={resolved.rest.join("/")} me={me} write={write} rest={resolved.rest} query={resolved.query} />
            </Suspense>
          </ErrorBoundary>
        )}
      </main>
    </div>
  );
}

export default function App() {
  const [me, setMe] = useState(null);
  const [checking, setChecking] = useState(!!getToken());
  const [notice, setNotice] = useState(null);
  useEffect(() => {
    if (!getToken()) return;
    api.get("/auth/me").then(setMe).catch(() => setToken("")).finally(() => setChecking(false));
  }, []);
  // 401 gjatë sesionit (JWT skadon) ⇒ hyrje me njoftim; 403 ⇒ vetëm gabim (mbetet i kyçur).
  useLayoutEffect(() => {
    if (!me) return;
    setSessionExpiredHandler(() => { setToken(""); setMe(null); setNotice(t("Your session has ended. Please sign in again.")); location.hash = ""; });
    return () => setSessionExpiredHandler(null);
  }, [me]);
  const logout = () => { setToken(""); setNotice(null); setMe(null); location.hash = ""; };
  return (
    <I18nProvider><UiProvider>
      {checking ? <div className="center muted" role="status">{t("Loading…")}</div> : !me ? <Login onLogin={(m) => { setNotice(null); setMe(m); }} notice={notice} /> : <Shell me={me} onLogout={logout} />}
    </UiProvider></I18nProvider>
  );
}
