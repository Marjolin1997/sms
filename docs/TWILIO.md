# Lidhja me Twilio

Adapteri: `app/providers/twilio.py`, callback-et: `app/api/twilio.py`. **Ndërtuar nga API-ja publike e Twilio dhe e testuar me transport të simuluar; nuk është provuar ende kundër Twilio të vërtetë.** Algoritmi i nënshkrimit të callback-eve u verifikua me shembullin zyrtar të dokumentacionit. Testi i parë real është më poshtë; bëje para se ta besosh.

## 1. Çfarë të bësh te Twilio (Console)
1. Krijo llogari provë. Verifiko numrin tënd të telefonit (Trial lejon mesazhe vetëm te numra të verifikuar; mesazhet marrin prefiks “Sent from your Twilio trial account”).
2. Merr një numër Twilio (Phone Numbers → Buy/Get a number) që mbështet SMS.
3. **Messaging → Settings → Geo permissions**: aktivizo shtetet ku dërgon (Shqipëri, Kosovë…). Pa këtë Twilio refuzon me gabim regjioni (21408).
4. Kopjo **Account SID** dhe **Auth Token** (Console → Account info). Vendosi **vetëm** te `.env` i serverit, jo në chat/repo:
   ```
   SMS_TWILIO_ACCOUNT_SID=AC...
   SMS_TWILIO_AUTH_TOKEN=...
   SMS_PUBLIC_BASE_URL=https://api.your-platform.example   # URL-ja që Twilio arrin (HTTPS publik)
   ```
   Opsionale: `SMS_TWILIO_MESSAGING_SERVICE_SID=MG...` (përdoret në vend të numrit From).
5. Te numri Twilio → **Messaging → “A message comes in”** → Webhook, HTTP POST: `https://api.your-platform.example/webhooks/twilio/inbound`.
   (Statusi i dorëzimit nuk kërkon konfigurim: platforma dërgon `StatusCallback` me çdo mesazh.)

Për zhvillim lokal, ekspozo API-n me një tunel HTTPS (p.sh. ngrok) dhe vendos `SMS_PUBLIC_BASE_URL` në URL-në e tunelit: nënshkrimi verifikohet mbi këtë URL.

## 2. Test i parë real (pa platformën): një SMS
```bash
export SMS_TWILIO_ACCOUNT_SID=AC... SMS_TWILIO_AUTH_TOKEN=...   # ose në .env
python -m scripts.twilio_smoke --to +355XXXXXXXXX --from +1XXXXXXXXXX            # shfaq çfarë do të dërgojë
python -m scripts.twilio_smoke --to +355XXXXXXXXX --from +1XXXXXXXXXX --confirm  # dërgon vërtet
```
Nëse pranohet, do të shohësh `U pranua nga Twilio: SM…`; gabimet dalin me kodin e Twilio (p.sh. `twilio_21608` = numër i paverifikuar në provë, `twilio_21408` = shteti s'është aktivizuar).

## 3. Lidhja në platformë
1. Rrugë për prefiksin (staf, kërkon 2FA): `PUT /v1/admin/routes {"prefix":"355","country":"AL","provider":"twilio","priority":100,"enabled":true}` (konsola → Tarifat & rrugët → Rrugët). Një rrugë me prioritet më të lartë mund të mbajë `fake` për teste.
2. Sender ID për klientin: numri Twilio si sender numerik (`+1415…`, shteti i destinacionit) miratohet nga stafi; përdoret si `From`. (Sender alfanumerik drejt Shqipërisë/Kosovës varet nga rregullat e Twilio dhe operatorëve: **konfirmo në Twilio** para se ta premtosh.)
3. Dërgo nga konsola (Dërgo) te numri yt i verifikuar. Kontrollo te *Historiku* që statusi kalon `në radhë → dërguar → dorëzuar`.

## 4. Sjellja që duhet ditur
- **Pa dyfishim:** Twilio nuk ka çelës idempotence për mesazhet. Nëse lidhja ndërpritet **pasi** kërkesa mund të ketë mbërritur (read timeout, 500), platforma **nuk riprovon**: mesazhi dështon me kodin `twilio_outcome_unknown`, paratë kthehen dhe stafi e rakordon me Twilio Console (Monitor → Logs). Vetëm gabimet para lidhjes (`connect`), 429 dhe 502/503/504 riprovohen.
- **Statuset:** `delivered` → mesazhi tarifohet; `undelivered`/`failed` → paratë kthehen me kodin `twilio_<ErrorCode>` (p.sh. `twilio_30003`). Statuset e ndërmjetme (`queued`, `sent`…) injorohen. Një status i vonuar/kontradiktor pas gjendjes finale injorohet (200). Mesazh i panjohur → 503 që Twilio të riprovojë.
- **Kredencialet e gabuara (401/403)** trajtohen si të përkohshme: rregullo `.env`, rinis, radha vazhdon.
- **SMS hyrës:** ruhet në inbox; `STOP`/`START` përditësojnë pëlqimin; përgjigjen automatike e bën platforma (fjalët kyçe), jo Twilio (kthehet TwiML bosh). Twilio vetë bllokon numrat që kanë shkruar STOP dhe kthen 21610.
- **Çmimi:** tarifa jote te klienti (listat e çmimeve) është e pavarur nga çmimi që të tarifon Twilio; llogarit marzhin sipas ofertës.

## 5. Lista e kontrollit para prodhimit
- [ ] Testi `twilio_smoke --confirm` u pranua dhe SMS mbërriti.
- [ ] Një mesazh nga platforma kaloi deri te `dorëzuar` (callback statusi punon me nënshkrim).
- [ ] Një SMS hyrës (dërgo nga telefoni te numri Twilio) del në Kutinë hyrëse; `STOP` bllokon.
- [ ] Geo permissions dhe rregullat e sender-it për shtetet reale të konfirmuara me Twilio.
- [ ] Llogari e pagesës (jo provë), Auth Token i rrotulluar nëse ka rrjedhur; ruaje si sekret.
