from pathlib import Path


def test_settings_template_contains_account_pool_tab_and_controls() -> None:
    text = Path("templates/settings.html").read_text(encoding="utf-8")

    assert 'data-tab="account-pool"' in text
    assert 'id="account-pool-tab"' in text
    assert 'id="account-pool-settings-form"' in text
    assert 'id="account-pool-status-card"' in text
    assert 'id="account-pool-refresh-btn"' in text
    assert 'id="account-pool-start-btn"' in text
    assert 'id="account-pool-stop-btn"' in text
    assert 'id="account-pool-run-once-btn"' in text
    assert 'id="account-pool-deleted-details"' in text
    assert 'id="account-pool-deleted-list"' in text
    assert 'id="account-pool-transient-details"' in text
    assert 'id="account-pool-transient-list"' in text


def test_settings_js_contains_account_pool_handlers_and_endpoints() -> None:
    text = Path("static/js/settings.js").read_text(encoding="utf-8")

    assert "loadAccountPoolStatus" in text
    assert "startAccountPoolStatusPolling" in text
    assert "stopAccountPoolStatusPolling" in text
    assert "updateAccountPoolActionButtons" in text
    assert "renderAccountPoolStatusList" in text
    assert "handleSaveAccountPoolSettings" in text
    assert "handleStartAccountPool" in text
    assert "handleStopAccountPool" in text
    assert "handleRunAccountPoolOnce" in text
    assert "handleRefreshAccountPoolStatus" in text
    assert "setInterval" in text
    assert "tab === 'account-pool'" in text
    assert "/settings/account-pool" in text
    assert "/account-pool/status" in text
    assert "/account-pool/start" in text
    assert "/account-pool/stop" in text
    assert "/account-pool/run-once" in text
