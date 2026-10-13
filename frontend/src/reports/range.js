// Intervali i datave të raporteve: ditë UTC, të dyja skajet përfshihen (serveri është autoritet; këtu vetëm UX).
export const MAX_DAYS = 366;
export const isDay = (s) => /^\d{4}-\d{2}-\d{2}$/.test(s || "") && !Number.isNaN(Date.parse(`${s}T00:00:00Z`));
export const utcToday = (now = new Date()) => now.toISOString().slice(0, 10);
export const shiftDay = (iso, n) => new Date(Date.parse(`${iso}T00:00:00Z`) + n * 86400000).toISOString().slice(0, 10);
export const daysBetween = (from, to) => Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86400000) + 1;

/** Nga query-ja e URL-së → {from, to}; e munguara/e pavlefshme ⇒ 30 ditët e fundit (UTC). */
export function rangeFrom(query = {}, now = new Date()) {
  const to = isDay(query.to) ? query.to : utcToday(now);
  const from = isDay(query.from) ? query.from : shiftDay(to, -29);
  return { from, to };
}

/** → null kur është e vlefshme, ose "order" | "long" | "format". */
export function rangeProblem(from, to) {
  if (!isDay(from) || !isDay(to)) return "format";
  if (from > to) return "order";
  if (daysBetween(from, to) > MAX_DAYS) return "long";
  return null;
}
