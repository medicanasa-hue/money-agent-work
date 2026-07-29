"""Local Windows notifications for scheduled production jobs."""

import base64
import os
import re
import subprocess

from loguru import logger

from app.config import config


_ATTENTION_STATUS_TEXT = {
    "failed": "başarısız oldu",
    "blocked": "kalite kontrolünde durduruldu",
    "partial_success": "kısmen tamamlandı",
}
_MAX_NOTIFICATION_TEXT_LENGTH = 180


def _is_windows() -> bool:
    return os.name == "nt"


def _notifications_enabled() -> bool:
    return bool(config.app.get("scheduled_job_windows_notifications", False))


def _render_quality_notifications_enabled() -> bool:
    return bool(config.app.get("render_quality_windows_notifications", False))


def _display_text(value: object) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    return text[:_MAX_NOTIFICATION_TEXT_LENGTH]


def _toast_command(title: str, body: str) -> list[str]:
    title_value = base64.b64encode(title.encode("utf-8")).decode("ascii")
    body_value = base64.b64encode(body.encode("utf-8")).decode("ascii")
    script = f"""
$utf8 = [System.Text.Encoding]::UTF8
$title = $utf8.GetString([Convert]::FromBase64String('{title_value}'))
$body = $utf8.GetString([Convert]::FromBase64String('{body_value}'))
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$safeTitle = [System.Security.SecurityElement]::Escape($title)
$safeBody = [System.Security.SecurityElement]::Escape($body)
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$safeTitle</text><text>$safeBody</text></binding></visual></toast>")
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('MoneyPrinterTurbo').Show($toast)
""".strip()
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded_script,
    ]


def notify_scheduled_job_attention(job_name: object, status: object) -> bool:
    """Show a best-effort local toast only for scheduled jobs needing attention."""
    status_key = str(status or "").strip().casefold()
    status_text = _ATTENTION_STATUS_TEXT.get(status_key)
    if not status_text or not _notifications_enabled() or not _is_windows():
        return False

    job_text = _display_text(job_name) or "Zamanlanmış iş"
    command = _toast_command(
        "MoneyPrinterTurbo: zamanlanmış iş dikkat gerektiriyor",
        f"{job_text} {status_text}. Ayrıntılar için Geçmiş ekranını açın.",
    )
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning(f"Scheduled-job Windows notification failed: {error}")
        return False

    if result.returncode != 0:
        logger.warning("Scheduled-job Windows notification returned a nonzero status.")
        return False
    return True


def notify_render_quality_attention(summary: object) -> bool:
    """Show a best-effort local toast for a new rolling quality concern."""
    summary_text = _display_text(summary)
    if not summary_text or not _render_quality_notifications_enabled() or not _is_windows():
        return False

    command = _toast_command(
        "MoneyPrinterTurbo: kalite kontrolü dikkat gerektiriyor",
        f"{summary_text} Ayrıntılar için Geçmiş ekranını açın.",
    )
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning(f"Render-quality Windows notification failed: {error}")
        return False

    if result.returncode != 0:
        logger.warning("Render-quality Windows notification returned a nonzero status.")
        return False
    return True
