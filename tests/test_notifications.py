from pathlib import Path


def test_notification_script_exposes_browser_api():
    source = Path("static/js/notifications.js").read_text(encoding="utf-8")
    assert "window.EvaNotifications" in source
    assert "showNotification" in source
    assert "setInlineAlert" in source


def test_index_template_has_notification_mounts():
    source = Path("templates/index.html").read_text(encoding="utf-8")
    assert "notification-stack" in source
    assert "id=\"errorAlert\"" in source
    assert "id=\"retryBar\"" in source
