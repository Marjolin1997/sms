import { t } from "../i18n.jsx";
import { DetailList, DetailRow } from "../ui.jsx";
import { CheckList, OpsPage, SectionCard, Stamp, duration, val } from "../ops/components.jsx";
import { section } from "../ops/model.js";

const counts = (o) => Object.entries(o || {}).map(([k, v]) => `${k}: ${v}`).join(" · ") || "-";

export default function OpsSync() {
  return (
    <OpsPage>
      {(o) => {
        const sync = section(o, "sync"), req = section(o, "requests");
        const s = o.metrics.sync, r = o.metrics.requests, es = o.metrics.enterprise?.sync, er = o.metrics.enterprise?.requests;
        return (
          <>
            <SectionCard title={t("Central → Enterprise sync feed")} status={sync.status}>
              <h4>{t("Central side")}</h4>
              <DetailList>
                <DetailRow label={t("Epoch")}><code>{s.epoch}</code></DetailRow>
                <DetailRow label={t("Latest sequence")}>{s.last_seq}</DetailRow>
                <DetailRow label={t("Oldest retained sequence")}>{s.floor_seq != null ? s.floor_seq + 1 : null}</DetailRow>
                <DetailRow label={t("Retained events")}>{s.retained_events}</DetailRow>
                <DetailRow label={t("Last event")}>{s.last_event_at ? <Stamp value={s.last_event_at} /> : null}</DetailRow>
                <DetailRow label={t("Oldest retained event")}>{s.oldest_retained_at ? <Stamp value={s.oldest_retained_at} /> : null}</DetailRow>
              </DetailList>
              <h4>{t("Enterprise side (reported snapshot)")}</h4>
              {!es ? <p className="muted">{t("Not connected to Central: evidence unavailable here.")}</p> : (
                <DetailList>
                  <DetailRow label={t("Sync enabled")}>{val(es.enabled)}</DetailRow>
                  <DetailRow label={t("Cursor initialized")}>{val(es.initialized)}</DetailRow>
                  <DetailRow label={t("Enterprise cursor")}>{val(es.cursor_seq)}</DetailRow>
                  <DetailRow label={t("Latest Central sequence known")}>{val(es.latest_central_seq)}</DetailRow>
                  <DetailRow label={t("Lag")}>{val(es.lag)}</DetailRow>
                  <DetailRow label={t("Last sync success")}>{es.last_success_age_seconds == null ? "-" : t("{when} ago", { when: duration(es.last_success_age_seconds) })}</DetailRow>
                  <DetailRow label={t("Snapshot age")}>{duration(es.snapshot_age_seconds)}</DetailRow>
                  <DetailRow label={t("Projection stale")}>{val(es.projection_stale)}</DetailRow>
                  <DetailRow label={t("Failure count")}>{val(es.failure_count)}</DetailRow>
                  <DetailRow label={t("Gap recoveries")}>{val(es.gap_recoveries)}</DetailRow>
                  <DetailRow label={t("Active policies")}>{val(es.policy_count)}</DetailRow>
                  <DetailRow label={t("Active authorizations")}>{val(es.authorization_count)}</DetailRow>
                  <DetailRow label={t("Withdrawn authorizations")}>{val(es.withdrawn_count)}</DetailRow>
                </DetailList>
              )}
              <CheckList checks={sync.checks} />
            </SectionCard>
            <SectionCard title={t("Enterprise → Central sender requests")} status={req.status}>
              <h4>{t("Central side")}</h4>
              <DetailList>
                <DetailRow label={t("Requests received")}>{r.total}</DetailRow>
                <DetailRow label={t("Received in 24 h")}>{r.received_24h}</DetailRow>
                <DetailRow label={t("Last request received")}>{r.last_received_at ? <Stamp value={r.last_received_at} /> : null}</DetailRow>
                <DetailRow label={t("By outcome")}>{counts(r.by_outcome)}</DetailRow>
                <DetailRow label={t("By automatic decision")}>{counts(r.by_auto)}</DetailRow>
              </DetailList>
              <h4>{t("Enterprise request reporter (reported snapshot)")}</h4>
              {!er ? <p className="muted">{t("Not connected to Central: evidence unavailable here.")}</p> : (
                <DetailList>
                  <DetailRow label={t("Reporting enabled")}>{val(er.enabled)}</DetailRow>
                  <DetailRow label={t("Pending")}>{val(er.pending)}</DetailRow>
                  <DetailRow label={t("Retry")}>{val(er.retry)}</DetailRow>
                  <DetailRow label={t("Sending")}>{val(er.sending)}</DetailRow>
                  <DetailRow label={t("Failed")}>{val(er.failed)}</DetailRow>
                  <DetailRow label={t("Stuck sending")}>{val(er.stuck_sending)}</DetailRow>
                  <DetailRow label={t("Delivered")}>{val(er.sent)}</DetailRow>
                  <DetailRow label={t("Oldest open request")}>{er.oldest_open_age_seconds == null ? t("None open") : duration(er.oldest_open_age_seconds)}</DetailRow>
                  <DetailRow label={t("Last successful report")}>{er.last_sent_age_seconds == null ? t("Never") : t("{when} ago", { when: duration(er.last_sent_age_seconds) })}</DetailRow>
                  <DetailRow label={t("Permanent failures by category")}>{counts(er.failed_by_category)}</DetailRow>
                </DetailList>
              )}
              <CheckList checks={req.checks} />
            </SectionCard>
          </>
        );
      }}
    </OpsPage>
  );
}
