import { useState } from "react";
import { api } from "../api.js";
import { t } from "../i18n.jsx";
import { Button, Card, CopyButton, ErrorBox, Tabs, useAction } from "../ui.jsx";

const BASE = location.origin;

// Shembuj kodi (anglisht: kodi nuk përkthehet). {owner} zëvendësohet me llogarinë e përdoruesit.
const SEND = {
  curl: (o) => `curl -X POST ${BASE}/v1/messages \\
  -H "Authorization: Bearer $API_KEY" \\
  -H "Idempotency-Key: order-1042-sms" \\
  -H "Content-Type: application/json" \\
  -d '{"owner_ref":"${o}","to":"+355691234567","sender":"ACME","text":"Your code is 481516"}'`,
  php: (o) => `$res = Http::withToken(config('services.sms.key'))
    ->withHeaders(['Idempotency-Key' => "order-{$order->id}-sms"])
    ->post('${BASE}/v1/messages', [
        'owner_ref' => '${o}', 'to' => $order->phone, 'sender' => 'ACME',
        'text' => "Order #{$order->id} has shipped.",
    ])->throw()->json();`,
  js: (o) => `const res = await fetch("${BASE}/v1/messages", {
  method: "POST",
  headers: { Authorization: \`Bearer \${process.env.SMS_KEY}\`, "Idempotency-Key": \`order-\${id}-sms\`, "Content-Type": "application/json" },
  body: JSON.stringify({ owner_ref: "${o}", to: "+355691234567", sender: "ACME", text: "Your code is 481516" }),
});
if (!res.ok) throw new Error((await res.json()).detail.message);`,
  python: (o) => `r = httpx.post("${BASE}/v1/messages", timeout=15,
    headers={"Authorization": f"Bearer {KEY}", "Idempotency-Key": f"order-{oid}-sms"},
    json={"owner_ref": "${o}", "to": "+355691234567", "sender": "ACME", "text": "Your code is 481516"})
r.raise_for_status()`,
};
const VERIFY = {
  php: `$raw = file_get_contents('php://input');
parse_str(str_replace(',', '&', $_SERVER['HTTP_X_SMS_SIGNATURE'] ?? ''), $p);   // t, v1
$ok = isset($p['t'], $p['v1'])
   && abs(time() - (int)$p['t']) < 300
   && hash_equals(hash_hmac('sha256', $p['t'].'.'.$raw, $secret), $p['v1']);
if (!$ok) { http_response_code(400); exit; }`,
  js: `import { createHmac, timingSafeEqual } from "node:crypto";
function verify(raw, header, secret) {   // raw = trupi i papërpunuar (Buffer)
  const p = Object.fromEntries(header.split(",").map((x) => x.split("=")));
  if (Math.abs(Date.now() / 1000 - Number(p.t)) > 300) return false;
  const mac = createHmac("sha256", secret).update(\`\${p.t}.\`).update(raw).digest("hex");
  return mac.length === p.v1.length && timingSafeEqual(Buffer.from(mac), Buffer.from(p.v1));
}`,
  python: `def verify(raw: bytes, header: str, secret: str) -> bool:
    p = dict(x.split("=", 1) for x in header.split(","))
    if abs(time.time() - int(p["t"])) > 300:
        return False
    mac = hmac.new(secret.encode(), p["t"].encode() + b"." + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, p["v1"])`,
};
const LANGS = [["curl", "curl"], ["php", "PHP (Laravel)"], ["js", "JavaScript"], ["python", "Python"]];

function Code({ text }) {
  return (
    <div className="codeblock">
      <pre><code>{text}</code></pre>
      <CopyButton text={text} />
    </div>
  );
}

export default function Developers({ me, owner }) {
  const [lang, setLang] = useState("curl");
  const [vlang, setVlang] = useState("php");
  const a = useAction();
  const o = me.owner_ref || owner || "acme";
  const grab = (path, file) => a.run(() => api.download(path, {}, file), t("Download started"));
  return (
    <>
      <div className="help"><b>{t("Start in 3 steps.")}</b> {t("1) Create an API key. 2) Get a sender ID approved. 3) Send your first SMS with the example below.")} <a href="#keys">{t("Create a key")}</a> · <a href="#senders">{t("Sender IDs & templates")}</a></div>
      <Card title={t("Send an SMS")} subtitle={t("Always send an Idempotency-Key: retrying with the same key never sends twice.")}>
        <Tabs value={lang} onChange={setLang} tabs={LANGS.map(([id, label]) => ({ id, label }))} />
        <Code text={SEND[lang](o)} />
        <small className="muted">{t("The answer is 202 with the message id and the price. The final status arrives by webhook (recommended) or with GET /v1/messages/{id}.")}</small>
      </Card>
      <Card title={t("Verify webhooks")} subtitle={t("Check the signature of every event and reject anything older than 5 minutes.")}>
        <Tabs value={vlang} onChange={setVlang} tabs={LANGS.filter(([id]) => id !== "curl").map(([id, label]) => ({ id, label }))} />
        <Code text={VERIFY[vlang]} />
        <small className="muted">{t("Header:")} <code>X-SMS-Signature: t=&lt;unix&gt;,v1=&lt;hex&gt;</code> · {t("Set up endpoints under Webhooks & events.")}</small>
      </Card>
      <Card title={t("Reference and tools")} subtitle={t("The full API description for your account, ready for Postman or code generators.")}>
        <div className="row wrap">
          <Button busy={a.busy} onClick={() => grab("/v1/openapi.json", "openapi.json")}>{t("Download OpenAPI (JSON)")}</Button>
          <Button busy={a.busy} onClick={() => grab("/v1/postman.json", "sms-platform.postman_collection.json")}>{t("Download Postman collection")}</Button>
        </div>
        <ErrorBox error={a.error} />
        <ul className="muted">
          <li>{t("Errors always look like")} <code>{`{"detail":{"code":"…","message":"…"}}`}</code></li>
          <li>{t("Money is returned as decimal strings, never floats.")}</li>
          <li>{t("Base URL")}: <code>{BASE}</code></li>
        </ul>
      </Card>
    </>
  );
}
