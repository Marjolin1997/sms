import { t } from "../i18n.jsx";
import { DetailList, DetailRow } from "../ui.jsx";
import { CheckList, OpsPage, SectionCard, Stamp } from "../ops/components.jsx";
import { section } from "../ops/model.js";

const counts = (o) => Object.entries(o || {}).map(([k, v]) => `${k}: ${v}`).join(" · ") || "-";

export default function OpsSync() {
  return (
    <OpsPage>
      {(o) => {
        const sync = section(o, "sync"), req = section(o, "requests");
        const s = o.metrics.sync, r = o.metrics.requests;
        return (
          <>
            <SectionCard title={t("Central → Enterprise sync feed")} status={sync.status}>
              <DetailList>
                <DetailRow label={t("Epoch")}><code>{s.epoch}</code></DetailRow>
                <DetailRow label={t("Latest sequence")}>{s.last_seq}</DetailRow>
                <DetailRow label={t("Oldest retained sequence")}>{s.floor_seq != null ? s.floor_seq + 1 : null}</DetailRow>
                <DetailRow label={t("Retained events")}>{s.retained_events}</DetailRow>
                <DetailRow label={t("Last event")}>{s.last_event_at ? <Stamp value={s.last_event_at} /> : null}</DetailRow>
                <DetailRow label={t("Oldest retained event")}>{s.oldest_retained_at ? <Stamp value={s.oldest_retained_at} /> : null}</DetailRow>
              </DetailList>
              <CheckList checks={sync.checks} />
            </SectionCard>
            <SectionCard title={t("Enterprise → Central sender requests")} status={req.status}>
              <DetailList>
                <DetailRow label={t("Requests received")}>{r.total}</DetailRow>
                <DetailRow label={t("Received in 24 h")}>{r.received_24h}</DetailRow>
                <DetailRow label={t("Last request received")}>{r.last_received_at ? <Stamp value={r.last_received_at} /> : null}</DetailRow>
                <DetailRow label={t("By outcome")}>{counts(r.by_outcome)}</DetailRow>
                <DetailRow label={t("By automatic decision")}>{counts(r.by_auto)}</DetailRow>
              </DetailList>
              <CheckList checks={req.checks} />
            </SectionCard>
          </>
        );
      }}
    </OpsPage>
  );
}
