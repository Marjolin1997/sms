"""M7-e: klienti HTTP drejt Central (vetëm transport). Nuk di SQL, nuk aplikon gjendje, nuk prek
AccountPlan; nuk importon modele/DB të Enterprise as `apps.central`.

Autentikimi (kontrata e M7-c): assertion JWT Ed25519 (`alg=EdDSA`, `kid`; `iss`=`sub`=client_id;
`aud`=`sms-central-sync`; `iat`; `exp` ≤ iat+300; `jti` unik; `scope` përmban `sync:read`),
i RI për çdo kërkesë HTTP. Çelësi privat vjen vetëm nga skedari (mount sekret); nuk ruhet në DB
dhe kurrë nuk futet në log/përjashtim. Nuk përdoret asnjë sekret i stafit të Central."""

import logging
import stat
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.config import Settings

log = logging.getLogger("sms.cp.client")

AUDIENCE = "sms-central-sync"
SCOPE = "sync:read"
LIFETIME_S = 120  # ≤ 300 (kufiri i Central); i shkurtër: mbrojtje ndaj rrjedhjes
SNAPSHOT_REQUIRED_CODES = frozenset(
    {
        "sync_epoch_mismatch",
        "sync_authorization_changed",
        "sync_cursor_expired",
        "sync_cursor_ahead",
    }
)


class ConfigError(Exception):
    """Konfigurim i pavlefshëm (mesazhi s'përmban kurrë përmbajtje çelësi)."""


class CpError(Exception):
    """Baza e gabimeve të klientit."""


class CpAuthError(CpError):  # 401: auth/çelës/konfigurim
    pass


class CpForbidden(CpError):  # 403: scope/autorizim
    pass


class CpTransportError(CpError):  # rrjet, timeout, 5xx, 429, përgjigje e palexueshme
    pass


class CpProtocolError(CpError):  # përgjigje që s'përputhet me kontratën
    pass


class CpSnapshotRequired(CpError):  # 409/410 me action=snapshot
    def __init__(self, code: str, status: int):
        super().__init__(f"{status} {code}")
        self.code, self.status = code, status


@dataclass(frozen=True, slots=True)
class ControlPlaneConfig:
    base_url: str
    client_id: str
    key_id: str
    private_key: Ed25519PrivateKey
    timeout_s: float = 10.0

    def __repr__(self) -> str:  # asnjëherë çelësi në log/traceback
        return (
            f"ControlPlaneConfig(base_url={self.base_url!r}, client_id={self.client_id!r}, "
            f"key_id={self.key_id!r})"
        )


def load_private_key(path: str) -> Ed25519PrivateKey:
    """Ngarko çelësin privat Ed25519 nga skedari. Refuzon: mungesë, jo-PEM, çelës vetëm publik,
    çelës i enkriptuar, algoritëm tjetër. Mesazhet nuk përmbajnë përmbajtje çelësi."""
    if not path:
        raise ConfigError("SMS_CP_PRIVATE_KEY_PATH is not set")
    p = Path(path)
    try:
        data = p.read_bytes()
        mode = p.stat().st_mode
    except OSError as e:
        raise ConfigError(f"cannot read private key file {path!r}: {type(e).__name__}") from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        log.warning("private key file %s is accessible by group/others", path)
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except Exception:  # noqa: BLE001  (TypeError për të enkriptuar, ValueError për publik/jo-PEM)
        raise ConfigError(
            "SMS_CP_PRIVATE_KEY_PATH is not an unencrypted PEM private key "
            "(a public-only or malformed key file is rejected)"
        ) from None
    if not isinstance(key, Ed25519PrivateKey):
        raise ConfigError("SMS_CP_PRIVATE_KEY_PATH must hold an Ed25519 private key")
    return key


def config_from_settings(s: Settings) -> ControlPlaneConfig:
    """Valido konfigurimin e sinkronizimit (nisja e poller-it). Nuk gjeneron çelës."""
    missing = [
        n
        for n, v in (
            ("SMS_CP_BASE_URL", s.cp_base_url),
            ("SMS_CP_CLIENT_ID", s.cp_client_id),
            ("SMS_CP_KEY_ID", s.cp_key_id),
        )
        if not v.strip()
    ]
    if missing:
        raise ConfigError(f"missing configuration: {', '.join(missing)}")
    if not s.cp_base_url.startswith(("https://", "http://")):
        raise ConfigError("SMS_CP_BASE_URL must be an http(s) URL")
    if s.env == "production" and not s.cp_base_url.startswith("https://"):
        raise ConfigError("SMS_CP_BASE_URL must be https:// in production")
    return ControlPlaneConfig(
        s.cp_base_url.rstrip("/"), s.cp_client_id, s.cp_key_id,
        load_private_key(s.cp_private_key_path), float(s.cp_request_timeout_seconds),
    )  # fmt: skip


def make_assertion(cfg: ControlPlaneConfig, now: datetime | None = None) -> str:
    """Assertion i ri (jti unik) për NJË kërkesë."""
    iat = int((now or datetime.now(UTC)).timestamp())
    claims = {
        "iss": cfg.client_id, "sub": cfg.client_id, "aud": AUDIENCE, "iat": iat,
        "exp": iat + LIFETIME_S, "jti": uuid.uuid4().hex, "scope": SCOPE,
    }  # fmt: skip
    return jwt.encode(claims, cfg.private_key, algorithm="EdDSA", headers={"kid": cfg.key_id})


@dataclass(frozen=True, slots=True)
class ChangesPage:
    epoch: uuid.UUID
    authorization_generation: int
    events: Sequence[Any]  # dict-e të papaisura: i parson `control_plane_sync.parse_events`
    next_seq: int
    latest_seq: int
    has_more: bool


def _int(d: dict, key: str) -> int:
    v = d.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise CpProtocolError(f"response field {key!r} is not a non-negative integer")
    return v


def parse_changes(d: Any) -> ChangesPage:
    if not isinstance(d, dict) or not isinstance(d.get("events"), list):
        raise CpProtocolError("changes response is not an object with an events array")
    if not isinstance(d.get("has_more"), bool):
        raise CpProtocolError("response field 'has_more' is not a boolean")
    try:
        epoch = uuid.UUID(str(d["epoch"]))
    except (KeyError, ValueError):
        raise CpProtocolError("response field 'epoch' is not a UUID") from None
    return ChangesPage(
        epoch, _int(d, "authorization_generation"), d["events"], _int(d, "next_seq"),
        _int(d, "latest_seq"), d["has_more"],
    )  # fmt: skip


class ControlPlaneClient:
    """`get_snapshot` / `get_changes`; asgjë tjetër. `http` injektohet në teste (httpx.Client,
    MockTransport ose TestClient i Central); në prodhim krijohet me timeout-in e konfiguruar."""

    def __init__(self, cfg: ControlPlaneConfig, http: httpx.Client | None = None):
        self._cfg = cfg
        self._http = http or httpx.Client(timeout=cfg.timeout_s)

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, params: dict | None = None) -> Any:
        try:
            r = self._http.get(
                self._cfg.base_url + path, params=params, timeout=self._cfg.timeout_s,
                headers={"Authorization": f"Bearer {make_assertion(self._cfg)}"},
            )  # fmt: skip
        except httpx.HTTPError as e:  # timeout, lidhje, TLS, ...
            raise CpTransportError(f"{type(e).__name__} calling {path}") from None
        sc = r.status_code
        if sc == 200:
            try:
                return r.json()
            except ValueError:
                raise CpProtocolError(f"{path}: response is not JSON") from None
        if sc == 401:
            raise CpAuthError("401 unauthorized (check client id, key id and private key)")
        if sc == 403:
            raise CpForbidden("403 forbidden (scope or enterprise authorization)")
        if sc in (409, 410):
            code = _error_code(r)
            if code in SNAPSHOT_REQUIRED_CODES:
                raise CpSnapshotRequired(code, sc)
            raise CpProtocolError(f"{sc} with unexpected code {code!r}")
        if sc == 429 or sc >= 500:
            raise CpTransportError(f"{sc} from central")
        raise CpProtocolError(f"unexpected status {sc}")

    def get_snapshot(self, enterprise_id: uuid.UUID | None = None) -> Any:
        """Snapshot i plotë (pa `enterprise_id`) ose i pjesshëm. Kthen JSON-in e papaisur."""
        params = {"enterprise_id": str(enterprise_id)} if enterprise_id else None
        return self._get("/internal/sync/snapshot", params)

    def get_changes(
        self, after_seq: int, epoch: uuid.UUID, generation: int, limit: int = 200
    ) -> ChangesPage:
        d = self._get(
            "/internal/sync/changes",
            {"after_seq": after_seq, "epoch": str(epoch), "generation": generation, "limit": limit},
        )
        return parse_changes(d)


def _error_code(r: httpx.Response) -> str | None:
    try:
        detail = r.json().get("detail")
        return detail.get("code") if isinstance(detail, dict) else None
    except (ValueError, AttributeError):
        return None
