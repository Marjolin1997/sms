import { useState } from "react";
import { api } from "../api.js";
import { t } from "../i18n.jsx";
import { Badge, Button, Card, CopyButton, ErrorBox, Field, useAction } from "../ui.jsx";

export default function Security({ me }) {
  const a = useAction(), b = useAction();
  const [setup, setSetup] = useState(null);
  const [code, setCode] = useState("");
  if (!me.key_id)
    return <Card title={t("Two-factor authentication")}><div className="alert warn"><span>{t("You're signed in with the bootstrap key, which can't use two-factor. Create a personal staff key under API keys and sign in with it.")}</span></div></Card>;
  return (
    <>
      <div className="help"><b>{t("Why two-factor?")}</b> {t("Sensitive actions (balance corrections, confirming top-ups, keys, prices, routes, kill switches) ask for a 6-digit code from an authenticator app. A stolen key alone isn't enough.")}</div>
      <Card title={t("Two-factor authentication")} actions={<Badge>{me.two_factor ? "active" : "disabled"}</Badge>}>
        {me.two_factor ? <p>{t("Two-factor is on for this key. You'll be asked for a code when you do a sensitive action.")}</p> : (
          <div className="form">
            {me.two_factor_required && <div className="alert warn"><span>{t("Your organisation requires two-factor. Turn it on to keep using sensitive actions.")}</span></div>}
            {!setup ? (
              <div><Button variant="primary" busy={a.busy} onClick={async () => { const r = await a.run(() => api.post("/v1/me/2fa/enroll")); if (r && r !== true) setSetup(r); }}>{t("Set up two-factor")}</Button></div>
            ) : (
              <>
                <p>{t("1. In your authenticator app add a new account and enter this key (or open the link on your phone).")}</p>
                <div className="row wrap"><code className="wrap-code">{setup.secret}</code><CopyButton text={setup.secret} /></div>
                <small className="muted"><a href={setup.otpauth_uri}>{t("Open in authenticator app")}</a></small>
                <p>{t("2. Enter the 6-digit code it shows to confirm.")}</p>
                <div className="row wrap">
                  <Field label={t("6-digit code")}><input inputMode="numeric" maxLength={6} value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} placeholder="123456" /></Field>
                  <Button variant="primary" busy={b.busy} disabled={code.length !== 6} onClick={async () => { const r = await b.run(() => api.post("/v1/me/2fa/confirm", { code }), t("Two-factor is on")); if (r) setTimeout(() => location.reload(), 900); }}>{t("Confirm")}</Button>
                </div>
              </>
            )}
            <ErrorBox error={a.error || b.error} />
          </div>
        )}
      </Card>
    </>
  );
}
