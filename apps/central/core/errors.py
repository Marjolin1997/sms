class CentralError(Exception):
    """Baza e gabimeve të domain-it të Central (pa HTTP: s'ka ende API menaxhimi)."""


class NotFound(CentralError):
    pass


class Conflict(CentralError):
    pass


class Invalid(CentralError):
    pass


class TooManyRequests(CentralError):
    """Kuota e abuzimit u tejkalua (p.sh. kërkesa regjistrimi për email në 24h)."""


class AuthenticationFailed(CentralError):
    """Kredenciale të pavlefshme. `reason` është vetëm për log operacional, jo për klientin."""

    def __init__(self, reason: str):
        super().__init__("invalid credentials")
        self.reason = reason
