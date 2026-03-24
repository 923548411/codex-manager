import asyncio
import importlib
from types import SimpleNamespace


from src.core.upload import cpa_upload
from src.web.account_pool_controller import AccountPoolController

web_app_module = importlib.import_module("src.web.app")


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data


def build_settings(**overrides):
    defaults = dict(
        account_pool_enabled=True,
        account_pool_target_count=10,
        account_pool_poll_interval_seconds=300,
        account_pool_health_check_batch_size=20,
        account_pool_registration_email_service_type="tempmail",
        account_pool_registration_interval_min=5,
        account_pool_registration_interval_max=15,
        account_pool_registration_concurrency=1,
        account_pool_max_registration_burst=5,
        account_pool_retry_max_retries=3,
        account_pool_retry_base_delay_seconds=1,
        account_pool_retry_max_delay_seconds=30,
        account_pool_cpa_service_id=0,
        cpa_enabled=False,
        cpa_api_url="",
        cpa_api_token=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_get_cpa_pool_count_accepts_list_payload(monkeypatch):
    def fake_get(*args, **kwargs):
        return FakeResponse(status_code=200, json_data=[{"id": 1}, {"id": 2}])

    monkeypatch.setattr(cpa_upload.cffi_requests, "get", fake_get)

    success, count, error = cpa_upload.get_cpa_pool_count(
        api_url="https://example.com/v0/management",
        api_token="token",
    )

    assert success is True
    assert count == 2
    assert error is None


def test_get_cpa_pool_count_accepts_items_payload(monkeypatch):
    def fake_get(*args, **kwargs):
        return FakeResponse(status_code=200, json_data={"items": [{"id": 1}, {"id": 2}, {"id": 3}]})

    monkeypatch.setattr(cpa_upload.cffi_requests, "get", fake_get)

    success, count, error = cpa_upload.get_cpa_pool_count(
        api_url="https://example.com",
        api_token="token",
    )

    assert success is True
    assert count == 3
    assert error is None


def test_get_cpa_pool_count_handles_401(monkeypatch):
    def fake_get(*args, **kwargs):
        return FakeResponse(status_code=401, json_data={"error": "unauthorized"}, text="unauthorized")

    monkeypatch.setattr(cpa_upload.cffi_requests, "get", fake_get)

    success, count, error = cpa_upload.get_cpa_pool_count(
        api_url="https://example.com",
        api_token="token",
    )

    assert success is False
    assert count is None
    assert "Token" in error


def test_account_pool_run_once_deletes_invalid_and_triggers_fill(monkeypatch):
    deleted_ids = []
    registration_requests = []

    monkeypatch.setattr("src.web.account_pool_controller.get_settings", lambda: build_settings(account_pool_max_registration_burst=4))
    monkeypatch.setattr(
        "src.web.account_pool_controller.resolve_cpa_target_config",
        lambda: {"service_id": 1, "service_name": "cpa-1", "api_url": "https://example.com", "api_token": "token", "include_proxy_url": False, "source": "db"},
    )
    monkeypatch.setattr("src.web.account_pool_controller.get_cpa_pool_count", lambda **kwargs: (True, 3, None))
    monkeypatch.setattr("src.web.account_pool_controller.count_active_accounts", lambda: 6)
    monkeypatch.setattr("src.web.account_pool_controller.list_active_account_ids", lambda limit, offset: [11, 12])
    monkeypatch.setattr(
        "src.web.account_pool_controller.validate_account_health",
        lambda account_id: (False, "Token 无效或已过期") if account_id == 11 else (True, None),
    )
    monkeypatch.setattr(
        "src.web.account_pool_controller.delete_account_by_id",
        lambda account_id: deleted_ids.append(account_id) or True,
    )

    async def fake_trigger_registration(**kwargs):
        registration_requests.append(kwargs["count"])
        return {"count": kwargs["count"], "batch_id": "batch-1"}

    monkeypatch.setattr("src.web.account_pool_controller.trigger_registration_fill", fake_trigger_registration)

    controller = AccountPoolController()
    result = asyncio.run(controller.run_once())

    assert deleted_ids == [11]
    assert registration_requests == [4]
    assert result["external_available_count"] == 3
    assert result["deleted_account_ids"] == [11]


def test_account_pool_run_once_keeps_transient_failures(monkeypatch):
    deleted_ids = []

    monkeypatch.setattr("src.web.account_pool_controller.get_settings", lambda: build_settings(account_pool_target_count=2))
    monkeypatch.setattr(
        "src.web.account_pool_controller.resolve_cpa_target_config",
        lambda: {"service_id": 1, "service_name": "cpa-1", "api_url": "https://example.com", "api_token": "token", "include_proxy_url": False, "source": "db"},
    )
    monkeypatch.setattr("src.web.account_pool_controller.get_cpa_pool_count", lambda **kwargs: (True, 2, None))
    monkeypatch.setattr("src.web.account_pool_controller.count_active_accounts", lambda: 2)
    monkeypatch.setattr("src.web.account_pool_controller.list_active_account_ids", lambda limit, offset: [21])
    monkeypatch.setattr(
        "src.web.account_pool_controller.validate_account_health",
        lambda account_id: (False, "验证异常: network timeout"),
    )
    monkeypatch.setattr(
        "src.web.account_pool_controller.delete_account_by_id",
        lambda account_id: deleted_ids.append(account_id) or True,
    )

    async def fake_trigger_registration(**kwargs):
        raise AssertionError("不应触发补量")

    monkeypatch.setattr("src.web.account_pool_controller.trigger_registration_fill", fake_trigger_registration)

    controller = AccountPoolController()
    result = asyncio.run(controller.run_once())

    assert deleted_ids == []
    assert result["deleted_account_ids"] == []
    assert result["transient_validation_failures"] == [{"account_id": 21, "error": "验证异常: network timeout"}]


def test_app_startup_and_shutdown_manage_account_pool(monkeypatch):
    calls = []

    class FakeController:
        async def start_if_enabled(self):
            calls.append("start")

        async def stop(self):
            calls.append("stop")

    monkeypatch.setattr(web_app_module, "get_account_pool_controller", lambda: FakeController())
    monkeypatch.setattr(web_app_module.task_manager, "set_loop", lambda loop: calls.append("set_loop"))
    monkeypatch.setattr("src.database.init_db.initialize_database", lambda: calls.append("init_db"))

    app = web_app_module.create_app()
    asyncio.run(app.router.startup())
    asyncio.run(app.router.shutdown())

    assert "init_db" in calls
    assert "set_loop" in calls
    assert "start" in calls
    assert "stop" in calls
