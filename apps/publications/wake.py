"""Best-effort one-bit wakeup for the systemd publication build socket."""

import logging
import os
import socket

from django.conf import settings

logger = logging.getLogger(__name__)


SYSTEMD_LISTEN_FD_START = 3
PUBLICATION_BUILD_WAKE_FD_NAME = "publication-build-wake"


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


def drain_publication_build_activation_wakes() -> int:
    """Accept and discard systemd socket-activation wake connections.

    The build socket is deliberately a one-bit notification: each client only
    connects and closes.  With ``Accept=no``, systemd passes the listening
    socket to the oneshot build service.  Leaving a queued connection
    unaccepted would make systemd immediately activate the service again after
    it exits.  Timer/manual invocations do not receive this descriptor and are
    therefore a no-op here.
    """
    try:
        fd_count = int(os.environ.get("LISTEN_FDS", "0"))
    except ValueError:
        return 0
    if fd_count <= 0 or os.environ.get("LISTEN_PID") != str(os.getpid()):
        return 0

    names = os.environ.get("LISTEN_FDNAMES", "").split(":")
    if len(names) != fd_count:
        logger.warning("Publication build activation descriptors have invalid names.")
        return 0

    drained = 0
    for offset, name in enumerate(names):
        if name != PUBLICATION_BUILD_WAKE_FD_NAME:
            continue
        inherited_fd = SYSTEMD_LISTEN_FD_START + offset
        try:
            # socket.socket(fileno=...) assumes ownership and closes its FD.
            # Keep systemd's inherited listener open for the whole oneshot so
            # the post-build drain can consume wakes that arrived mid-cycle.
            duplicate_fd = os.dup(inherited_fd)
        except OSError:
            logger.warning("Could not duplicate the publication build wake socket.")
            continue
        try:
            listener = socket.socket(fileno=duplicate_fd)
        except OSError:
            os.close(duplicate_fd)
            logger.warning("Could not duplicate the publication build wake socket.")
            continue
        try:
            listener.setblocking(False)
            while True:
                try:
                    client, _address = listener.accept()
                except BlockingIOError:
                    break
                except OSError:
                    logger.warning("Could not drain the publication build wake socket.")
                    break
                client.close()
                drained += 1
        finally:
            # This closes only our duplicate, never the systemd activation FD.
            listener.close()
    return drained
