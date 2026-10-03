import httpx
import pytest

from app.providers import ProviderError, SendRequest
from app.providers.http import HttpProvider

REQ = SendRequest("ref-1", "ACME", "355691230003", "hi", "gsm7", 1)


def provider(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpProvider("http", "https://sms.example/send", "KEY", client=client)


def test_success_sends_expected_request():
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers["authorization"]
        seen["json"] = request.read().decode()
        return httpx.Response(200, json={"id": "abc"})

    assert provider(handler).send(REQ).provider_message_id == "abc"
    assert seen["auth"] == "Bearer KEY" and '"reference":"ref-1"' in seen["json"].replace(" ", "")


@pytest.mark.parametrize(("status", "temporary"), [(500, True), (503, True), (429, True),
                                                   (408, True), (400, False), (401, False),
                                                   (422, False)])  # fmt: skip
def test_status_classification(status, temporary):
    with pytest.raises(ProviderError) as e:
        provider(lambda r: httpx.Response(status)).send(REQ)
    assert e.value.temporary is temporary


@pytest.mark.parametrize("resp", [httpx.Response(200, text="ok"), httpx.Response(200, json={}),
                                  httpx.Response(200, json={"id": 5})])  # fmt: skip
def test_bad_2xx_body_is_temporary_not_lost(resp):
    with pytest.raises(ProviderError) as e:
        provider(lambda r: resp).send(REQ)
    assert e.value.temporary and e.value.code == "bad_response"


def test_network_error_is_temporary():
    def handler(request):
        raise httpx.ConnectTimeout("t")

    with pytest.raises(ProviderError) as e:
        provider(handler).send(REQ)
    assert e.value.temporary
