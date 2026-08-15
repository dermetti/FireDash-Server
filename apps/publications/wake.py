"""Best-effort one-bit wakeup for the systemd publication build socket."""

import logging
import socket

from django.conf import settings

logger = logging.getLogger(__name__)


def wake_publication_build_worker() -> bool:
    """Notify systemd that eligible build work may exist; PostgreSQL is authoritative."""
    try:
        with socket.socket(getattr(socket, "AF_UNIX", 1), socket.SOCK_STREAM) as client:
            client.settimeout(settings.PUBLICATION_BUILD_WAKE_TIMEOUT_SECONDS)
            client.connect(settings.PUBLICATION_BUILD_WAKE_SOCKET_PATH)
        return True
    except OSError:
        logger.warning(
            "Publication build wake socket is unavailable; nightly timer remains fallback."
        )
        return False
