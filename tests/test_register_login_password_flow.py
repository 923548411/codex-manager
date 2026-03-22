import json
import sys
from types import SimpleNamespace
import types

if "curl_cffi" not in sys.modules:
    curl_cffi_stub = types.ModuleType("curl_cffi")
    curl_cffi_requests_stub = types.ModuleType("curl_cffi.requests")
    curl_cffi_requests_stub.Session = object
    curl_cffi_requests_stub.Response = object
    curl_cffi_stub.requests = curl_cffi_requests_stub
    sys.modules["curl_cffi"] = curl_cffi_stub
    sys.modules["curl_cffi.requests"] = curl_cffi_requests_stub

from src.config.constants import OPENAI_API_ENDPOINTS, OPENAI_PAGE_TYPES
from src.core import register as register_module
from src.core.register import RegistrationEngine, SignupFormResult


class DummyResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


class RecordingSession:
    def __init__(self, response: DummyResponse):
        self.response = response
        self.calls: list[dict] = []

    def post(self, url: str, headers=None, data=None):
        self.calls.append({"url": url, "headers": headers or {}, "data": data})
        return self.response


class DummyHttpClient:
    def __init__(self, proxy_url=None):
        self.proxy_url = proxy_url
        self.session = object()

    def close(self):
        return None


class DummyEmailService:
    service_type = SimpleNamespace(value="dummy")


def make_engine() -> RegistrationEngine:
    engine = object.__new__(RegistrationEngine)
    engine.email_service = DummyEmailService()
    engine.proxy_url = None
    engine.callback_logger = None
    engine.task_uuid = None
    engine.email = "user@example.com"
    engine.password = "secret-password"
    engine.email_info = None
    engine.oauth_start = None
    engine.session = None
    engine.session_token = None
    engine.logs = []
    engine._otp_sent_at = None
    engine._is_existing_account = False
    engine._login_password_page_data = None
    return engine


def test_submit_login_password_uses_authorize_continue_endpoint():
    response = DummyResponse(
        200,
        payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}},
    )
    session = RecordingSession(response)
    engine = make_engine()
    engine.session = session
    engine._login_password_page_data = {
        "page": {
            "type": "password",
            "backstack_behavior": "default",
            "payload": {
                "fields": [
                    {"name": "login_password", "type": "password"},
                ],
            },
        }
    }

    result = engine._submit_login_password("device-123", "sentinel-456")

    assert result.success is True
    assert result.page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]
    assert len(session.calls) == 1

    call = session.calls[0]
    assert call["url"] == OPENAI_API_ENDPOINTS["signup"]
    assert call["headers"]["referer"] == "https://auth.openai.com/login/password"
    assert json.loads(call["headers"]["openai-sentinel-token"])["flow"] == "authorize_continue"
    assert json.loads(call["data"]) == {
        "login_password": {"value": "secret-password", "kind": "password"}
    }


def test_submit_login_form_caches_password_variant_page_data():
    payload = {
        "page": {
            "type": "login-password",
            "backstack_behavior": "default",
            "payload": {
                "fields": [
                    {"name": "login_password", "type": "password"},
                ],
            },
        }
    }
    session = RecordingSession(DummyResponse(200, payload=payload))
    engine = make_engine()
    engine.session = session

    result = engine._submit_login_form("device-123", "sentinel-456")

    assert result.success is True
    assert result.page_type == "login-password"
    assert result.is_existing_account is True
    assert engine._login_password_page_data == payload
    assert any(
        "登录密码页 page keys: ['backstack_behavior', 'payload', 'type']" in entry
        for entry in engine.logs
    )
    assert any(
        '登录密码页 payload: {"fields": [{"name": "login_password", "type": "password"}]}'
        in entry
        for entry in engine.logs
    )


def test_validate_verification_code_uses_requested_flow():
    response = DummyResponse(200, payload={})
    session = RecordingSession(response)
    engine = make_engine()
    engine.session = session

    ok = engine._validate_verification_code(
        "123456",
        "device-123",
        "sentinel-456",
        flow="authorize_continue",
    )

    assert ok is True
    assert len(session.calls) == 1
    assert json.loads(session.calls[0]["headers"]["openai-sentinel-token"])["flow"] == "authorize_continue"


def test_run_stops_when_login_password_submission_fails(monkeypatch):
    engine = make_engine()
    engine.http_client = DummyHttpClient()
    engine.session = object()

    monkeypatch.setattr(register_module, "OpenAIHTTPClient", DummyHttpClient)

    engine._check_ip_location = lambda: (True, "US")

    def create_email():
        engine.email = "user@example.com"
        return True

    engine._create_email = create_email
    engine._init_session = lambda: True
    engine._start_oauth = lambda: True
    engine._get_device_id = lambda: "device-123"
    engine._check_sentinel = lambda did, flow="authorize_continue": "sentinel-456"

    def register_password(did, sen_token):
        engine.password = "secret-password"
        return True, "secret-password"

    engine._submit_signup_form = lambda did, sen_token: SignupFormResult(success=True)
    engine._register_password = register_password
    engine._send_verification_code = lambda did, sen_token: True

    otp_calls = {"count": 0}

    def get_verification_code():
        otp_calls["count"] += 1
        if otp_calls["count"] == 1:
            return "123456"
        raise AssertionError("login password failure should stop before requesting another OTP")

    engine._get_verification_code = get_verification_code
    engine._validate_verification_code = lambda code, did, sen_token, flow="create_account": True
    engine._create_user_account = lambda did, sen_token: True
    engine._submit_login_form = lambda did, sen_token: SignupFormResult(
        success=True,
        page_type="password",
        is_existing_account=True,
    )
    engine._submit_login_password = lambda did, sen_token: SignupFormResult(
        success=False,
        error_message="HTTP 400: invalid_auth_step",
    )

    result = engine.run()

    assert result.success is False
    assert result.error_message == "提交登录密码失败: HTTP 400: invalid_auth_step"
    assert otp_calls["count"] == 1
