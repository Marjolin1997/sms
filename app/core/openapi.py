"""OpenAPI për zhvilluesit: skemë e mbrojtur (vetëm me çelës API), e filtruar sipas rolit,
me etiketa, përmbledhje të qarta dhe Postman collection të gjeneruar prej saj."""

import re
from copy import deepcopy

from fastapi import FastAPI

from app.core.config import settings

# (prefiks i rrugës → etiketa). Rendi ka rëndësi: prefiksi më specifik më parë.
TAGS = [
    ("/v1/admin/", "Staff · Administration"),
    ("/v1/rate-cards", "Staff · Rates"),
    ("/v1/rate-card-versions", "Staff · Rates"),
    ("/v1/topups", "Staff · Rates"),
    ("/v1/sender-ids/{sender_id}/", "Staff · Approvals"),
    ("/v1/template-versions", "Staff · Approvals"),
    ("/v1/messages", "SMS"),
    ("/v1/email", "Email"),
    ("/v1/contacts", "Contacts"),
    ("/v1/lists", "Contacts"),
    ("/v1/consent", "Consent"),
    ("/v1/campaigns", "Campaigns"),
    ("/v1/templates", "Templates & sender IDs"),
    ("/v1/sender-ids", "Templates & sender IDs"),
    ("/v1/wallets", "Wallet"),
    ("/v1/billing", "Billing"),
    ("/v1/webhooks", "Webhooks & events"),
    ("/v1/events", "Webhooks & events"),
    ("/v1/inbox", "Inbox & keywords"),
    ("/v1/keywords", "Inbox & keywords"),
    ("/v1/reports", "Reports"),
    ("/v1/me", "Account"),
    ("/v1/portal", "Account"),
    ("/webhooks/", "Provider callbacks"),
    ("/u/", "Provider callbacks"),
]
TAG_DESCRIPTIONS = {
    "SMS": "Send SMS, check status and history. Send with an `Idempotency-Key` header.",
    "Email": "Send email from a verified domain, history and delivery events.",
    "Contacts": "Contacts and lists; GDPR export and erase.",
    "Consent": "Record and check opt-in / opt-out evidence.",
    "Campaigns": "Send to a whole list with a budget cap, rate limit and schedule.",
    "Templates & sender IDs": "Approved sender IDs and reusable message templates.",
    "Wallet": "Prepaid balance, ledger, top-ups and the low-balance alert.",
    "Billing": "Plan, invoices and payments.",
    "Webhooks & events": "Delivery events pushed to your HTTPS endpoint (HMAC-signed).",
    "Inbox & keywords": "Inbound SMS and keyword auto-replies.",
    "Reports": "Daily usage and CSV exports.",
    "Account": "Who you are, self-service API keys, onboarding.",
    "Provider callbacks": "Endpoints called by SMS/email/payment providers (not for customers).",
}
SUMMARIES = {
    ("POST", "/v1/messages"): "Send an SMS",
    ("GET", "/v1/messages"): "List sent SMS",
    ("GET", "/v1/messages/{public_id}"): "Get an SMS",
    ("GET", "/v1/messages/{public_id}/events"): "SMS status history",
    ("POST", "/v1/messages/quote"): "Price an SMS before sending",
    ("POST", "/v1/email/messages"): "Send an email",
    ("GET", "/v1/email/messages"): "List sent emails",
    ("GET", "/v1/email/messages/{public_id}"): "Get an email",
    ("GET", "/v1/email/messages/{public_id}/events"): "Email delivery events",
    ("POST", "/v1/email/domains"): "Add a sending domain",
    ("POST", "/v1/email/domains/{domain_id}/verify"): "Verify a sending domain (DNS)",
    ("GET", "/v1/me"): "Who am I (role and permissions)",
    ("GET", "/v1/inbox"): "List received SMS",
    ("PUT", "/v1/keywords"): "Create or update a keyword",
    ("GET", "/v1/reports/usage"): "Daily usage report",
    ("GET", "/v1/reports/messages.csv"): "Export SMS as CSV",
    ("GET", "/v1/reports/emails.csv"): "Export emails as CSV",
    ("GET", "/v1/contacts/{contact_id}/export"): "Export all data held about a contact (GDPR)",
    ("DELETE", "/v1/contacts/{contact_id}"): "Erase a contact (GDPR)",
    ("PUT", "/v1/wallets/{wallet_id}/alert"): "Set the low-balance alert",
    ("POST", "/v1/campaigns/{campaign_id}/schedule"): "Start or schedule a campaign",
}
_AUTH_HEADERS = {"authorization", "x_admin_key", "x-admin-key", "x_totp", "x-totp"}
_PUBLIC = ("/healthz", "/readyz", "/u/", "/webhooks/")

# vlera shembull sipas emrit të fushës
HINTS = {
    "owner_ref": "acme",
    "to": "+355691234567",
    "from": "+355691234567",
    "sender": "ACME",
    "text": "Your code is 481516",
    "subject": "Welcome",
    "from_email": "hello@example.com",
    "email": "ana@example.com",
    "phone": "+355691234567",
    "first_name": "Ana",
    "last_name": "Hoxha",
    "url": "https://example.com/hooks/sms",
    "name": "Example",
    "currency": "EUR",
    "amount": "25.00",
    "category": "transactional",
    "keyword": "help",
    "code": "123456",
    "country": "AL",
    "domain": "mail.example.com",
    "body": "Your code is {{code}}",
}


def _tag(path: str) -> str | None:
    if path in ("/healthz", "/readyz"):
        return None
    for prefix, tag in TAGS:
        if path.startswith(prefix):
            return tag
    return None


def _humanize(op_id: str, method: str) -> str:
    base = re.sub(r"_v1_.*$|_webhooks_.*$|_u_.*$", "", op_id)
    return base.replace("_", " ").strip().capitalize() or method


def build(app: FastAPI) -> dict:
    """Skema e plotë (stafi); filtrimi për klientët bëhet te for_role."""
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title="SMS Platform API",
        version="1.0",
        routes=app.routes,
        description=_DESCRIPTION,
        servers=[{"url": settings.public_base_url}],
    )
    schema["components"].setdefault("securitySchemes", {})["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "description": "API key: `Authorization: Bearer sms_<prefix>_<secret>`",
    }
    used: set[str] = set()
    for path, ops in list(schema["paths"].items()):
        tag = _tag(path)
        if path in ("/healthz", "/readyz"):
            del schema["paths"][path]
            continue
        for method, op in ops.items():
            m = method.upper()
            op["tags"] = [tag] if tag else ["Other"]
            used.add(op["tags"][0])
            op["summary"] = SUMMARIES.get((m, path)) or _humanize(op["operationId"], m)
            op.pop("description", None)  # shënime të brendshme të zhvillimit
            if op["summary"] == "Endpoint":  # funksione pa emër kuptimplotë
                verb = path.rsplit("/", 1)[-1].replace("-", " ").capitalize()
                op["summary"] = f"{verb} ({tag})"
            # kredencialet janë skema BearerAuth, jo parametra të veçantë
            if "parameters" in op:
                op["parameters"] = [p for p in op["parameters"] if p["name"] not in _AUTH_HEADERS]
            if not path.startswith(_PUBLIC):
                op["security"] = [{"BearerAuth": []}]
    schema["tags"] = [{"name": t, "description": TAG_DESCRIPTIONS.get(t, "")} for t in sorted(used)]
    app.openapi_schema = schema
    return schema


_DESCRIPTION = """Send SMS and email, manage contacts and consent, run campaigns, receive delivery
events and inbound messages. Authenticate with `Authorization: Bearer <api key>`.
Errors: `{"detail": {"code": "...", "message": "..."}}`. Money is returned as decimal strings.
Send endpoints require an `Idempotency-Key` header: retrying with the same key never sends twice."""


def for_role(schema: dict, staff: bool) -> dict:
    """Klientët nuk e shohin pjesën e stafit dhe callback-et e provider-ave."""
    if staff:
        return schema
    out = deepcopy(schema)
    hidden = {
        t
        for t in (x["name"] for x in out["tags"])
        if t.startswith("Staff") or t == "Provider callbacks"
    }
    out["paths"] = {
        p: {m: o for m, o in ops.items() if not (set(o["tags"]) & hidden)}
        for p, ops in out["paths"].items()
    }
    out["paths"] = {p: ops for p, ops in out["paths"].items() if ops}
    out["tags"] = [t for t in out["tags"] if t["name"] not in hidden]
    return out


# --- Shembuj dhe Postman ----------------------------------------------------------------


def _resolve(schema: dict, node: dict) -> dict:
    while "$ref" in node:
        node = schema["components"]["schemas"][node["$ref"].rsplit("/", 1)[-1]]
    return node


def example(schema: dict, node: dict, name: str = "", depth: int = 0):
    node = _resolve(schema, node)
    if "example" in node:
        return node["example"]
    if "default" in node and node["default"] is not None:
        return node["default"]
    if "anyOf" in node:  # p.sh. string | null
        for alt in node["anyOf"]:
            if alt.get("type") != "null":
                return example(schema, alt, name, depth)
    t = node.get("type")
    if t == "object" or "properties" in node:
        if depth > 3:
            return {}
        req = node.get("required", [])
        props = node.get("properties", {})
        return {
            k: example(schema, v, k, depth + 1)
            for k, v in props.items()
            if k in req or (depth == 0 and k in HINTS)
        }
    if t == "array":
        return [example(schema, node.get("items", {}), name, depth + 1)]
    if name in HINTS and t in (None, "string"):
        return HINTS[name]
    if "enum" in node:
        return node["enum"][0]
    if "pattern" in node and node["pattern"].startswith("^(") and "|" in node["pattern"]:
        return node["pattern"].lstrip("^(").split(")")[0].split("|")[0]
    return {"integer": 1, "number": 1, "boolean": True}.get(t, "string")


def postman(schema: dict) -> dict:
    """Collection v2.1: një folder për etiketë; {{base_url}} dhe {{api_key}} si variabla."""
    folders: dict[str, list] = {}
    for path, ops in sorted(schema["paths"].items()):
        for method, op in ops.items():
            url_path = re.sub(r"\{(\w+)\}", r":\1", path).strip("/").split("/")
            req = {
                "method": method.upper(),
                "header": [],
                "url": {
                    "raw": "{{base_url}}/" + "/".join(url_path),
                    "host": ["{{base_url}}"],
                    "path": url_path,
                },
            }
            if op.get("security"):
                req["auth"] = {
                    "type": "bearer",
                    "bearer": [{"key": "token", "value": "{{api_key}}", "type": "string"}],
                }
            var = [p for p in op.get("parameters", []) if p["in"] == "path"]
            if var:
                req["url"]["variable"] = [{"key": p["name"], "value": "1"} for p in var]
            query = [p for p in op.get("parameters", []) if p["in"] == "query"]
            if query:
                req["url"]["query"] = [
                    {"key": p["name"], "value": "", "disabled": True} for p in query
                ]
            hdrs = [p for p in op.get("parameters", []) if p["in"] == "header"]
            for h in hdrs:
                req["header"].append(
                    {
                        "key": h["name"].replace("_", "-").title(),
                        "value": "{{$guid}}" if "idempotency" in h["name"].lower() else "",
                    }
                )
            body = op.get("requestBody", {}).get("content", {}).get("application/json")
            if body:
                import json

                req["header"].append({"key": "Content-Type", "value": "application/json"})
                req["body"] = {
                    "mode": "raw",
                    "raw": json.dumps(example(schema, body["schema"]), indent=2),
                    "options": {"raw": {"language": "json"}},
                }
            folders.setdefault(op["tags"][0], []).append({"name": op["summary"], "request": req})
    return {
        "info": {
            "name": "SMS Platform API",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "variable": [
            {"key": "base_url", "value": settings.public_base_url.rstrip("/")},
            {"key": "api_key", "value": "sms_xxxxxxxx_xxxxxxxx"},
        ],
        "item": [{"name": t, "item": items} for t, items in sorted(folders.items())],
    }
