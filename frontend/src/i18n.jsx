// Shtresë e vogël i18n: teksti anglisht në kod është ÇELËSI; fjalori shqip është te locales/sq.
// Gjuha ruhet në localStorage (parazgjedhje: shqip). Ndërrimi rimonton pemën, ndaj komponentët
// mund të përdorin drejtpërdrejt `t` pa hook.
import { Fragment, createContext, useContext, useState } from "react";
import sq from "./locales/sq/index.js";

const KEY = "sms_lang";
export const LANGS = { sq: "Shqip", en: "English" };

function initial() {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "sq" || v === "en") return v;
  } catch { /* localStorage i bllokuar: përdor parazgjedhjen */ }
  return "sq";
}
let current = initial();
if (typeof document !== "undefined") document.documentElement.lang = current;

export const getLang = () => current;
export const locale = () => (current === "sq" ? "sq-AL" : "en-GB");

const fill = (s, vars) => (vars ? s.replace(/\{(\w+)\}/g, (_, k) => (vars[k] === undefined ? `{${k}}` : vars[k])) : s);

/** Përkthen një tekst. Mungesa e përkthimit kthen tekstin anglisht (kontrollohet nga i18n:check). */
export function t(key, vars) {
  return fill(current === "sq" ? (sq[key] ?? key) : key, vars);
}
/** Shënues për tekste në konstante modulesh; përkthehen kur shfaqen me t(). */
export const T = (s) => s;
/** Shumës: tn(n, "{n} person", "{n} people"). Të dy format janë çelësa të veçantë në fjalor. */
export const tn = (n, one, other, vars) => t(n === 1 ? one : other, { n, ...vars });

const Ctx = createContext({ lang: current, setLang: () => {} });
export const useLang = () => useContext(Ctx);

export function I18nProvider({ children }) {
  const [lang, set] = useState(current);
  const setLang = (l) => {
    current = l;
    try { localStorage.setItem(KEY, l); } catch { /* s'ka rëndësi */ }
    document.documentElement.lang = l;
    set(l);
  };
  return (
    <Ctx.Provider value={{ lang, setLang }}>
      <Fragment key={lang}>{children}</Fragment>
    </Ctx.Provider>
  );
}

export function LangSwitch() {
  const { lang, setLang } = useLang();
  return (
    <div className="langsw" role="group" aria-label={t("Language")}>
      {Object.entries(LANGS).map(([code, name]) => (
        <button key={code} type="button" className={lang === code ? "on" : ""} aria-pressed={lang === code} onClick={() => setLang(code)}>{name}</button>
      ))}
    </div>
  );
}
