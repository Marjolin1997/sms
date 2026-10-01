class CentralError(Exception):
    """Baza e gabimeve të domain-it të Central (pa HTTP: s'ka ende API menaxhimi)."""


class NotFound(CentralError):
    pass


class Conflict(CentralError):
    pass


class Invalid(CentralError):
    pass
