# API për zhvilluesit

Baza: `https://api.your-platform.example` (ndryshojeni me domenin tuaj). Skema e plotë OpenAPI dhe Postman collection merren me çelësin tuaj (konsola → *Zhvilluesit*, ose direkt):

```bash
curl -H "Authorization: Bearer $API_KEY" https://api.your-platform.example/v1/openapi.json -o openapi.json
curl -H "Authorization: Bearer $API_KEY" https://api.your-platform.example/v1/postman.json -o sms.postman.json
```

Klientët shohin vetëm pjesën e tyre të API-së; stafi shikon edhe administrimin.

## 1. Autentikimi
Çdo kërkesë: `Authorization: Bearer sms_<prefix>_<sekret>`. Çelësin e krijoni te konsola → *Çelësat API* (shfaqet **një herë**). Praktika:
- një çelës për çdo sistem; rrotulloje periodikisht (`POST /v1/portal/api-keys/{id}/rotate`, i vjetri vazhdon 60 min);
- kufizoje me **IP të lejuara** (`allowed_cidrs`) kur serveri ka IP fikse;
- kurrë në kod front-end ose aplikacion mobil.

20 autentikime të dështuara brenda 10 min nga e njëjta IP japin `429 too_many_attempts`.

## 2. Gabimet
```json
{"detail": {"code": "insufficient_funds", "message": "insufficient available funds"}}
```
| HTTP | Kuptimi |
|---|---|
| 401 | Çelës i pavlefshëm/skaduar |
| 402 | Balancë e pamjaftueshme (`insufficient_funds`) |
| 403 | Pa leje, sender jo i miratuar, llogari e ndaluar, IP e palejuar |
| 404 | Nuk ekziston (ose s'është e jotja) |
| 409 | Konflikt (p.sh. çelësi i idempotencës me parametra të ndryshëm) |
| 422 | Të dhëna të pavlefshme (`no_rate`, `no_route`, `recipient_suppressed`…) |
| 429 | Kufi shpejtësie (`rate_limited`, `too_many_attempts`) |
| 503 | Dërgimi në pauzë (`sending_paused`) — riprovo më vonë |

Shumat kthehen si **string dhjetor** (`"0.045000"`), kurrë float.

## 3. Dërgo një SMS
`POST /v1/messages` me header **`Idempotency-Key`** (unik për çdo mesazh logjik; rikthimi me të njëjtin çelës **nuk dërgon dy herë**, kthen mesazhin origjinal). Përgjigje `202` me `id`, `status: queued`, çmimin e ngrirë.

**curl**
```bash
curl -X POST https://api.your-platform.example/v1/messages \
  -H "Authorization: Bearer $API_KEY" \
  -H "Idempotency-Key: order-1042-sms" \
  -H "Content-Type: application/json" \
  -d '{"owner_ref":"acme","to":"+355691234567","sender":"ACME","text":"Kodi juaj është 481516"}'
```

**PHP (Laravel)**
```php
$res = Http::withToken(config('services.sms.key'))
    ->withHeaders(['Idempotency-Key' => "order-{$order->id}-sms"])
    ->post('https://api.your-platform.example/v1/messages', [
        'owner_ref' => 'acme', 'to' => $order->phone, 'sender' => 'ACME',
        'text' => "Porosia #{$order->id} u nis.",
    ])->throw()->json();   // ['id' => '…', 'status' => 'queued', 'total_price' => '0.045000', …]
```

**JavaScript**
```js
const res = await fetch("https://api.your-platform.example/v1/messages", {
  method: "POST",
  headers: { Authorization: `Bearer ${process.env.SMS_KEY}`, "Idempotency-Key": `order-${id}-sms`, "Content-Type": "application/json" },
  body: JSON.stringify({ owner_ref: "acme", to: "+355691234567", sender: "ACME", text: "Kodi juaj është 481516" }),
});
if (!res.ok) throw new Error((await res.json()).detail.message);
```

**Python**
```python
r = httpx.post("https://api.your-platform.example/v1/messages", timeout=15,
    headers={"Authorization": f"Bearer {KEY}", "Idempotency-Key": f"order-{oid}-sms"},
    json={"owner_ref": "acme", "to": "+355691234567", "sender": "ACME", "text": "Kodi juaj është 481516"})
r.raise_for_status()
```

**Çmimi para dërgimit:** `POST /v1/messages/quote` kthen segmentet, kodimin (GSM-7 ose Unicode kur ka ë/ç/emoji: 70 karaktere për segment) dhe çmimin.
**Shabllon:** `template_id` + `values` në vend të `text`. **Marketing:** `category: "marketing"` kërkon pëlqim të regjistruar (`POST /v1/consent`); transaksionalet jo, por kush ka shkruar STOP bllokohet gjithmonë.

### Ciklet e statusit
`queued → sending → sent → delivered | failed`. Paratë rezervohen në pranim, tarifohen kur mesazhi dorëzohet dhe kthehen nëse dështon. Ndiq: `GET /v1/messages/{id}` dhe `/events`, ose (më mirë) webhook.

## 4. Dërgo një email
`POST /v1/email/messages` (me `Idempotency-Key`): `from_email` duhet të jetë në domen të verifikuar (`POST /v1/email/domains` → shto rekordet DNS → `/verify`).
```bash
curl -X POST .../v1/email/messages -H "Authorization: Bearer $API_KEY" -H "Idempotency-Key: welcome-42" \
  -H "Content-Type: application/json" \
  -d '{"from_email":"hello@mail.acme.com","to":"ana@example.com","subject":"Mirësevini","text":"Përshëndetje Ana","html":"<p>Përshëndetje <b>Ana</b></p>"}'
```
Emailet `marketing` marrin automatikisht lidhjen e çregjistrimit (RFC 8058 one-click).

## 5. Webhook-et (ngjarjet)
Krijo endpoint HTTPS: `POST /v1/webhooks/endpoints {"url":"https://…","event_types":["message.*"]}` → kthen `secret` (**një herë**). Ngjarjet: `message.sent|delivered|failed`, `message.received`, `email.sent|delivered|bounced|complained|failed`, `campaign.*`, `consent.opted_in|opted_out`, `invoice.issued|paid`, `payment.succeeded|failed`, `wallet.low_balance`.

Trupi (JSON i kompaktuar):
```json
{"id":"evt_812","type":"message.delivered","created_at":"2026-09-30T09:12:01+00:00",
 "data":{"resource_type":"message","resource_id":"6f1c…","status":"delivered"}}
```
Headers: `X-SMS-Signature: t=<unix>,v1=<hex>`, `X-SMS-Event-Id`, `X-SMS-Delivery-Id`. Përgjigju me **2xx**; ndryshe riprovohet me vonesa në rritje (deri në disa herë), `410` e çaktivizon endpoint-in.

**Verifiko gjithmonë nënshkrimin** (HMAC-SHA256 mbi `"{t}." + trupi_i_papërpunuar`) dhe refuzo `t` më të vjetër se 5 min:

**PHP**
```php
$raw = file_get_contents('php://input');
parse_str(str_replace(',', '&', $_SERVER['HTTP_X_SMS_SIGNATURE'] ?? ''), $p);   // t, v1
$ok = isset($p['t'], $p['v1'])
   && abs(time() - (int)$p['t']) < 300
   && hash_equals(hash_hmac('sha256', $p['t'].'.'.$raw, $secret), $p['v1']);
if (!$ok) { http_response_code(400); exit; }
```
**Node.js (Express: përdor `express.raw({type:"application/json"})`)**
```js
import { createHmac, timingSafeEqual } from "node:crypto";
function verify(raw, header, secret) {
  const p = Object.fromEntries(header.split(",").map((x) => x.split("=")));
  if (Math.abs(Date.now() / 1000 - Number(p.t)) > 300) return false;
  const mac = createHmac("sha256", secret).update(`${p.t}.`).update(raw).digest("hex");
  return mac.length === p.v1.length && timingSafeEqual(Buffer.from(mac), Buffer.from(p.v1));
}
```
**Python**
```python
def verify(raw: bytes, header: str, secret: str) -> bool:
    p = dict(x.split("=", 1) for x in header.split(","))
    if abs(time.time() - int(p["t"])) > 300:
        return False
    mac = hmac.new(secret.encode(), p["t"].encode() + b"." + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, p["v1"])
```
Një ngjarje mund të vijë më shumë se një herë: përdor `X-SMS-Event-Id` për dedupe. Pa endpoint mund t'i lexosh edhe me `GET /v1/events`.

## 6. SMS hyrës dhe fjalë kyçe
Përgjigjet e marrësve në numrin tuaj numerik shfaqen te `GET /v1/inbox` dhe si `message.received`. `STOP`/`START` (dhe `NDALO`/`FILLO`) trajtohen automatikisht. Fjalë kyçe me përgjigje automatike: `PUT /v1/keywords {"keyword":"help","reply_text":"…"}`.

## 7. Kontakte, pëlqim, fushata
- `POST /v1/contacts/import` (deri 1000 rreshta), `POST /v1/lists`, `POST /v1/lists/{id}/members`.
- Pëlqim me provë: `POST /v1/consent {"channel":"sms","address":"+355…","action":"opt_in","evidence":"forma e regjistrimit, 14 Gusht 2026","source":"web","reason":"signup"}`; kontroll: `GET /v1/consent/check`.
- Fushatë: `POST /v1/campaigns` (skicë) → `GET …/estimate` → `POST …/schedule`; me `max_cost` dhe `rate_per_minute`. Pauzë/rifillim/anulim janë të sigurta: mesazhet e pa-dërguara kthehen në portofol.
- GDPR: `GET /v1/contacts/{id}/export` dhe `DELETE /v1/contacts/{id}`.

## 8. Portofoli, raportet
`GET /v1/wallets` (bilanci), `/v1/wallets/{id}/ledger` (çdo lëvizje, e pandryshueshme), `PUT /v1/wallets/{id}/alert {"threshold":"5"}` → ngjarja `wallet.low_balance`. Raporte: `GET /v1/reports/usage?from=2026-09-01&to=2026-09-30`, CSV: `/v1/reports/messages.csv`, `/v1/reports/emails.csv`.

## 9. Faqosja dhe kufijt
Listat kthejnë më të rejat së pari me `next_before_id` (kalo si `before_id`). Trupi maksimal 256 KB. Kufiri i dërgimit për llogari (parazgjedhje 600/min) jepet `429 rate_limited`; riprovo pas 1 min me **të njëjtin** `Idempotency-Key`.

## 10. Kontrolle të pasme (rekomandim për integrimin)
1. Ruaj çelësin te sekretet e serverit, jo në repo.
2. Përdor `Idempotency-Key` të qëndrueshëm (p.sh. `order-{id}-{event}`), jo UUID të ri për çdo provë.
3. Trajto `202` si “pranuar”, jo “dorëzuar”: statusi final vjen me webhook.
4. Verifiko nënshkrimin e webhook-ut dhe përgjigju shpejt (< 10 s); punën e rëndë bëje në sfond.
5. Testo me numrin tënd përpara se të nisësh fushatë.
