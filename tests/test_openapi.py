"""Faza 18: OpenAPI i mbrojtur dhe i filtruar, Postman collection, shembuj."""

from collections import Counter

from tests.test_console_api import key

BOOT = {"X-Admin-Key": "test-key"}


def test_openapi_requires_authentication(client, raw_client=None):
    from fastapi.testclient import TestClient

    from app.main import create_app

    anon = TestClient(create_app())
    assert anon.get("/v1/openapi.json").status_code == 401
    assert anon.get("/v1/postman.json").status_code == 401
    assert anon.get("/openapi.json").status_code == 404 and anon.get("/docs").status_code == 404


def test_client_spec_hides_staff_and_callbacks(client):
    from fastapi.testclient import TestClient

    from app.main import create_app

    c = TestClient(create_app())
    h = key(c, "client", "acme")
    spec = c.get("/v1/openapi.json", headers=h).json()
    paths = spec["paths"]
    assert "/v1/messages" in paths and "/v1/email/messages" in paths and "/v1/inbox" in paths
    assert not any(p.startswith(("/v1/admin", "/webhooks", "/u/")) for p in paths)
    assert not any(t["name"].startswith("Staff") for t in spec["tags"])
    assert "/v1/rate-cards" not in paths and "/v1/topups" not in paths


def test_staff_spec_includes_everything(client):
    spec = client.get("/v1/openapi.json", headers=BOOT).json()
    assert "/v1/admin/api-keys" in spec["paths"] and "/webhooks/dlr/{provider}" in spec["paths"]
    assert "/healthz" not in spec["paths"]


def test_spec_quality(client):
    spec = client.get("/v1/openapi.json", headers=BOOT).json()
    assert spec["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"
    dups = Counter(
        (o["tags"][0], o["summary"]) for ops in spec["paths"].values() for o in ops.values()
    )
    assert [k for k, n in dups.items() if n > 1] == []  # përmbledhje unike brenda etiketës
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            assert op["tags"] != ["Other"], f"{method} {path} has no tag"
            assert "description" not in op  # shënimet e brendshme (shqip) nuk dalin
            if path.startswith("/v1/"):
                assert op["security"] == [{"BearerAuth": []}]
            names = {p["name"] for p in op.get("parameters", [])}
            assert not names & {"authorization", "x-admin-key", "x-totp"}
    send = spec["paths"]["/v1/messages"]["post"]
    assert send["summary"] == "Send an SMS"
    assert any(p["name"] == "idempotency-key" for p in send["parameters"])


def test_postman_collection(client):
    from fastapi.testclient import TestClient

    from app.main import create_app

    c = TestClient(create_app())
    h = key(c, "client", "acme")
    pm = c.get("/v1/postman.json", headers=h).json()
    assert pm["info"]["schema"].endswith("collection.json")
    assert {v["key"] for v in pm["variable"]} == {"base_url", "api_key"}
    reqs = {i["name"]: i["request"] for f in pm["item"] for i in f["item"]}
    send = reqs["Send an SMS"]
    assert send["method"] == "POST" and send["url"]["raw"] == "{{base_url}}/v1/messages"
    assert send["auth"]["bearer"][0]["value"] == "{{api_key}}"
    import json

    body = json.loads(send["body"]["raw"])
    assert body["to"].startswith("+355") and body["sender"] == "ACME" and "text" in body
    assert {"key": "Idempotency-Key", "value": "{{$guid}}"} in send["header"]
    get = reqs["Get an SMS"]
    assert get["url"]["path"] == ["v1", "messages", ":public_id"]
    assert get["url"]["variable"][0]["key"] == "public_id"
    total = sum(len(f["item"]) for f in pm["item"])
    spec_ops = sum(
        len(ops) for ops in c.get("/v1/openapi.json", headers=h).json()["paths"].values()
    )
    assert total == spec_ops  # çdo operacion ka kërkesë


def test_example_generation():
    from app.core.openapi import example

    schema = {
        "components": {
            "schemas": {
                "X": {
                    "type": "object",
                    "required": ["to", "n"],
                    "properties": {
                        "to": {"type": "string"},
                        "n": {"type": "integer"},
                        "opt": {"type": "string"},
                        "text": {"type": "string"},
                    },
                }
            }
        }
    }
    out = example(schema, {"$ref": "#/components/schemas/X"})
    assert out == {"to": "+355691234567", "n": 1, "text": "Your code is 481516"}
