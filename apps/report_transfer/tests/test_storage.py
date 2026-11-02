import os
from types import SimpleNamespace

import pytest
from django.utils import timezone

from apps.report_transfer import storage


@pytest.mark.parametrize("receipt", [False, True])
def test_posix_storage_without_directory_fsync_fails_closed(
    tmp_path, settings, monkeypatch, receipt
):
    settings.REPORT_TRANSFER_ROOT = tmp_path / "spool"
    # Model a POSIX platform missing O_DIRECTORY without changing the host's os module.
    monkeypatch.setattr(
        storage,
        "os",
        SimpleNamespace(
            name="posix",
            open=os.open,
            fdopen=os.fdopen,
            fsync=os.fsync,
            replace=os.replace,
            O_WRONLY=os.O_WRONLY,
            O_CREAT=os.O_CREAT,
            O_EXCL=os.O_EXCL,
        ),
    )
    completed_at = timezone.now()
    with pytest.raises(OSError, match="requires directory fsync support"):
        if receipt:
            storage.write_download_receipt(1, completed_at)
        else:
            storage.write_payload(b"test ciphertext")
    assert not list(storage.root().glob("*.jwe"))
    assert not list(storage.root().glob("*.receipt-tmp"))
    receipts = list(storage.root().glob("*.receipt"))
    if receipt:
        # Preserve the existing recoverable receipt after rename, even when fsync fails.
        assert len(receipts) == 1
        assert storage.read_download_receipt(receipts[0]) == (1, completed_at)
    else:
        assert receipts == []
