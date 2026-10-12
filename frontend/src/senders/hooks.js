import { api } from "../api.js";
import { useLoad } from "../ui.jsx";
import { adaptSender } from "./model.js";

// Enterprise: lista e sender-ave të klientit (ose e llogarisë së zgjedhur nga stafi). Kufizim 300 nga serveri.
export function useSenderIds(filters = {}, refreshMs = 15000) {
  const key = JSON.stringify(filters);
  const r = useLoad((signal) => api.get("/v1/sender-ids", filters, { signal }).then((rows) => rows.map(adaptSender)), [key], refreshMs);
  return r;
}
