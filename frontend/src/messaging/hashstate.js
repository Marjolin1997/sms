import { useEffect, useState } from "react";

// Gjendja e faqes në URL (#campaigns/12?tab=traffic&status=failed): lidhjet e thella riprodhojnë pamjen.
// Router-i i konsolës shpërfill pjesën pas "?", ndaj faqet e mesazheve e lexojnë vetë këtu.
export function parseLocation(hash = location.hash) {
  const raw = hash.replace(/^#/, "");
  const [path, qs = ""] = raw.split("?");
  const [id, ...rest] = path.split("/");
  return { id, rest: rest.filter(Boolean), query: Object.fromEntries(new URLSearchParams(qs)) };
}

export function buildHash(id, rest = [], query = {}) {
  const qs = new URLSearchParams(Object.entries(query).filter(([, v]) => v !== undefined && v !== null && v !== "")).toString();
  return `#${[id, ...rest].join("/")}${qs ? `?${qs}` : ""}`;
}

export function useHashState(routeId) {
  const [loc, setLoc] = useState(() => parseLocation());
  useEffect(() => {
    const h = () => setLoc(parseLocation());
    addEventListener("hashchange", h);
    return () => removeEventListener("hashchange", h);
  }, []);
  return { rest: loc.rest, query: loc.query, go: (rest = [], query = {}) => { location.hash = buildHash(routeId, rest, query); } };
}
