// Kontrollon që çdo tekst i shfaqur ka përkthim shqip, pa çelësa të papërdorur dhe pa tekste të
// shkruara direkt në JSX. Përdorim: npm run i18n:check   (del me kod ≠ 0 nëse ka problem)
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const SRC = new URL("../src/", import.meta.url).pathname;
const files = [];
(function walk(d) {
  for (const f of readdirSync(d)) {
    const p = join(d, f);
    if (statSync(p).isDirectory()) { if (!["locales", "__tests__", "test"].includes(f)) walk(p); }
    else if (/\.(jsx?|mjs)$/.test(f)) files.push(p);
  }
})(SRC);

const STR = String.raw`(["'\x60])((?:\\.|(?!\1)[^\\])*)\1`;
const used = new Map(); // çelës → skedari i parë
const add = (k, f) => { const key = k.replace(/\\(["'`\\])/g, "$1").replace(/\\n/g, "\n"); if (!used.has(key)) used.set(key, f); };
const problems = [];
const dict = (await import(pathToFileURL(join(SRC, "locales/sq/index.js")).href)).default;

for (const f of files) {
  const src = readFileSync(f, "utf8");
  const rel = f.replace(SRC, "src/");
  for (const m of src.matchAll(new RegExp(String.raw`(?<![\w.])(?:t|T)\(\s*${STR}`, "g"))) add(m[2], rel);
  for (const m of src.matchAll(/(?<![\w.])tn\(\s*[^,()]+,\s*(["'`])((?:\\.|(?!\1)[^\\])*)\1\s*,\s*(["'`])((?:\\.|(?!\3)[^\\])*)\3/g)) { add(m[2], rel); add(m[4], rel); }

  if (f.endsWith("i18n.jsx")) continue;
  // tekst i shkruar direkt në JSX (mes > dhe <) që s'është shprehje {..}
  const stripped = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
  for (const m of stripped.matchAll(/>\s*([A-Za-zÀ-ž][^<>{}=;]*[A-Za-zÀ-ž.!?:])\s*</g)) {
    const txt = m[1].trim();
    if (/^[A-Z][A-Z0-9_ ]*$/.test(txt) && txt.length < 8) continue; // p.sh. "SMS", "EUR"
    if (/=>|&&|\|\||\?|\.\.\.\s*$/.test(txt) && !/[a-z]{3}/.test(txt)) continue;
    if (txt === "Platform") continue; // pjesë e logos
    if (/^[a-z_]+$/.test(txt) && txt in dict) continue; // status ynë, përkthehet nga Badge
    if (/^[\w.$()\[\]]+\s*(\?|&&|\|\|)/.test(txt)) continue; // shprehje JS, jo tekst
    if (!/\s/.test(txt) && /^[a-z]+\.?[a-z]*$/i.test(txt) && txt.length < 4) continue;
    problems.push(`${rel}: tekst i drejtpërdrejtë në JSX: “${txt.slice(0, 60)}”`);
  }
  // atribute me tekst të drejtpërdrejtë që shihen nga përdoruesi
  for (const m of stripped.matchAll(/\b(placeholder|aria-label|title|label|hint|subtitle|empty|emptyTitle|confirmLabel|body|error|note)="([^"{}]*[A-Za-z]{3}[^"{}]*)"/g)) {
    if (m[1] === "placeholder" && !/\s/.test(m[2])) continue; // vlerë shembull pa hapësira
    if (/^[\w.@:/+#-]+$/.test(m[2]) && (/[.@:/#]/.test(m[2]) || /^[A-Z0-9]+$/.test(m[2]))) continue; // p.sh. mail.example.com, ACME
    problems.push(`${rel}: atribut ${m[1]} me tekst të drejtpërdrejtë: “${m[2].slice(0, 50)}”`);
  }
}

const vars = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((x) => x[1]).sort().join(",");
const missing = [...used.keys()].filter((k) => !(k in dict));
// çelësat me një fjalë të vogël (statuset e API-së) përdoren dinamikisht nga Badge/t(vlerë)
const unused = Object.keys(dict).filter((k) => !used.has(k) && !/^[a-z_]+$/.test(k));
const badVars = Object.keys(dict).filter((k) => used.has(k) && vars(k) !== vars(dict[k]));
const empty = Object.keys(dict).filter((k) => !String(dict[k]).trim());

for (const k of missing) problems.push(`MUNGON përkthimi: “${process.env.I18N_FULL ? k : k.slice(0, 70)}” (${used.get(k)})`);
for (const k of unused) problems.push(`I papërdorur në fjalor: “${k.slice(0, 70)}”`);
for (const k of badVars) problems.push(`Ndryshojnë {variablat}: “${k.slice(0, 70)}”`);
for (const k of empty) problems.push(`Përkthim bosh: “${k.slice(0, 70)}”`);

console.log(`i18n: ${used.size} tekste, ${Object.keys(dict).length} përkthime`);
if (problems.length) { console.error(problems.join("\n")); console.error(`\n${problems.length} problem(e)`); process.exit(1); }
console.log("i18n: në rregull");
