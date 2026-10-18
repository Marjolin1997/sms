"""M2: kufiri mes logjikës së domain-it dhe mekanizmit të queue-së (aktualisht PostgreSQL).

Queue-ja është rreshti i domain-it (status + attempts + next_attempt_at), jo tabelë e veçantë.
Ky paketë NUK importon asgjë nga `app.models`/`app.services`: gjithçka domain-specifike vjen nga
`DispatchSpec` (predikata/kolona) dhe `DispatchHooks` (tranzicionet, ngjarjet, paratë)."""
