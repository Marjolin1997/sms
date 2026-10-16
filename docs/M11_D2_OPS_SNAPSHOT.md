# M11-D2 — Enterprise operational snapshot → Central

Enterprise raporton asinkron te Central një pamje kompakte, të versionuar dhe pa të dhëna
sensitive (`sender.ops.v1`). Central ruan vetëm gjendjen e fundit për çdo service client dhe
`GET /admin/operations/sender` e bashkon me kontrollet e veta: ku më parë ishte UNKNOWN,
tani ka provë reale (kursori i sync, vonesa, backlog i reporter-it, shadow/drift, bootstrap,
cutover, alarmet).

## Rrjedha

```
Enterprise worker (sender_request_reporter)
  ├─ outbox i kërkesave (pa ndryshim)
  └─ ops.report_once()  ── ndërton snapshot (seancë e shkurtër, vetëm agregate, read-only)
                         ── mbyll seancën ── POST /internal/sender/operations (scope sender:report)
Central: ingest (FOR UPDATE, idempotent) → tabela sender_ops_snapshots (1 rresht / service client)
Admin UI → GET /admin/operations/sender → bashkim + freskia llogaritet në server
```

Asnjë thirrje sinkrone Central→Enterprise. Asnjë queue/histori e re. Dështimi i ops nuk prek
reporter-in e kërkesave, submit, dispatch apo sync (kod i veçantë, try/except i veçantë).

## Kontrata `sender.ops.v1`

Zarf strikt: `schema, snapshot_id, scope="deployment", generated_at (UTC …Z), source_revision, data`.
`data`: `sync, requests, shadow, bootstrap, cutover, alerts`. Max 16 KiB. Seksion `null` =
i padisponueshëm (Central ⇒ `unknown`); `0/false` = i matur. Pa vlera të papërpunuara sender,
PII, sekrete: vetëm numra, enum-e, hash 12-hex dhe numërues kategorish (≤24 çelësa).
Implementimi: `packages/contracts/control_plane/sender/ops_v1.py`.

## Ingest

- `POST /internal/sender/operations`, scope `sender:report`; 413 (>16384), 422 (JSON/skemë),
  403 `enterprise_not_authorized`.
- 201 `accepted`; 200 `duplicate` (i njëjti `snapshot_id` + përmbajtje) ose `ignored_stale`
  (`generated_at` ≤ i ruajturi). Njëjtë `snapshot_id` me përmbajtje ndryshe ⇒ 409.
- Migrim additiv `0030_sender_ops_snapshot` (up/down/up).

## Freskia (llogaritet në server, sipas `received_at`)

| Mosha | Gjendja |
|---|---|
| ≤ `CENTRAL_SENDER_OPS_STALE_SECONDS` (300) | fresh |
| ≤ 6× | stale ⇒ warn |
| më shumë | expired ⇒ fail |

Kur nuk është fresh, çdo `pass` i prejardhur nga Enterprise degradohet në `unknown`; warn/fail
mbeten. Pa snapshot ⇒ UNKNOWN (jo 500). Nivelet e alarmeve Enterprise ok/warning/critical ⇒
pass/warn/fail; Central nuk shpik pragje.

## Konfigurim

| Variabël | Parazgjedhje |
|---|---|
| `SMS_SENDER_OPS_REPORTING` (Enterprise) | `false` |
| `SMS_SENDER_OPS_INTERVAL_SECONDS` (15–3600) | `60` |
| `CENTRAL_SENDER_OPS_STALE_SECONDS` (30–86400) | `300` |
| `SMS_CODE_REVISION` (opsionale, për `source_revision`) | — |

Prodhimi kërkon https kur ops reporting është aktiv.

## Performanca (e matur)

- Enterprise `build`: 66 query ftohtë / 21 me cache alarmesh (cache 300 s), i pavarur nga vëllimi
  (3000 sender / 10000 krahasime: 158 ms ftohtë, 12 ms me cache); ~2.5 KB.
- Central endpoint i bashkuar (2000 sender): 20 query ftohtë (përfshirë auth), 15 ngrohtë; ~9.3 KB;
  asnjë shkrim, asnjë rresht audit.

## Kufizime

- Vetëm gjendja e fundit (pa histori). Alarmet vlerësohen me cache 300 s.
- Freskia matet nga `received_at` (ora e Central), jo nga ora e Enterprise.
