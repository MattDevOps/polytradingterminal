"""Desktop toast notifications for held-position alerts."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def send_toast(title: str, body: str, url: str | None = None) -> None:
    """Send a Windows toast notification. Silently falls back to console."""
    try:
        from winotify import Notification

        toast = Notification(
            app_id="Poly Trading Terminal",
            title=title,
            msg=body,
            duration="long",
        )
        if url:
            toast.add_actions(label="View on Polymarket", launch=url)
        toast.show()
    except ImportError:
        log.warning("winotify not installed — printing to console")
        print(f"\n  [{title}] {body}\n")
    except Exception as exc:
        log.error("Toast failed: %s", exc)
