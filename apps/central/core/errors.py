class CentralError(Exception):
    """Baza e gabimeve të domain-it të Central (pa HTTP: s'ka ende API menaxhimi)."""


class NotFound(CentralError):
    pass


class Conflict(CentralError):
    pass


class Invalid(CentralError):
    pass


class Forbidden(CentralError):
    """Aktori s'ka të drejtë për veprimin (p.sh. para: vetëm admin njeri)."""


class InsufficientFunds(Conflict):
    """Fondet tregtare të alokueshme nuk mjaftojnë (M9-b); asnjë ndryshim nuk bëhet."""


class TooManyRequests(CentralError):
    """Kuota e abuzimit u tejkalua (p.sh. kërkesa regjistrimi për email në 24h)."""


class AuthenticationFailed(CentralError):
    """Kredenciale të pavlefshme. `reason` është vetëm për log operacional, jo për klientin."""

    def __init__(self, reason: str):
        super().__init__("invalid credentials")
        self.reason = reason
