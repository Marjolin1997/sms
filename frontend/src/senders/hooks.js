import { api } from "../api.js";
import { useLoad } from "../ui.jsx";
import { adaptSender } from "./model.js";

export const LIST_CAP = 300; // kufiri i palëvizshëm i serverit për GET /v1/sender-ids (pa faqosje/kërkim server-side)

// GET /v1/sender-ids ka timestamp-et (created_at, reviewed_by) por GET /v1/sender-ids/{id} jo (SenderOut): detaji i merr nga lista e ngarkuar së fundi.
const hints = new Map();
export const hintFor = (id) => hints.get(Number(id)) || {};

// Enterprise: sender-at e klientit (ose të llogarisë së zgjedhur nga stafi). Filtri `status` zbatohet nga serveri.
export function useSenderIds(filters = {}, refreshMs = 15000) {
  const key = JSON.stringify(filters);
  return useLoad(async (signal) => {
    const rows = (await api.get("/v1/sender-ids", filters, { signal })).map(adaptSender);
    for (const r of rows) hints.set(r.id, { createdAt: r.createdAt, reviewedBy: r.reviewedBy });
    return rows;
  }, [key], refreshMs);
}

export function useSenderDetail(id, refreshMs = 15000) {
  return useLoad(async (signal) => {
    const s = adaptSender(await api.get(`/v1/sender-ids/${id}`, {}, { signal }));
    const h = hintFor(id);
    return { ...s, createdAt: s.createdAt || h.createdAt || null, reviewedBy: s.reviewedBy || h.reviewedBy || null };
  }, [id], refreshMs);
}
