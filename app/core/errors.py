"""Gabimet bazë të domain-it (M3-b(ii)): burimi i vetëm i `DomainError`, `NotFound`, `Conflict`.

Çdo gabim domain-i (kontakte, mesazhe, billing, rates, ...) trashëgon prej këtu, jo nga
`services.wallet`. `.code` është kontrata e gabimit që API-t e hartëzojnë në HTTP (fjalorët
`_STATUS` te `api/*`). Moduli s'importon asgjë nga `app`: përdoret nga çdo shtresë pa cikle.

Përputhshmëria: `services.wallet.WalletError` është ALIAS i `DomainError` (i njëjti objekt), dhe
`DomainError.code` mban parazgjedhjen e trashëguar `"wallet_error"` që sjellja të mos ndryshojë."""


class DomainError(Exception):
    code = "wallet_error"  # parazgjedhja e trashëguar (si WalletError); nënklasat e mbivendosin


class NotFound(DomainError):
    code = "not_found"


class Conflict(DomainError):
    code = "conflict"
