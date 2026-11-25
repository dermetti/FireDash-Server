import os
from types import SimpleNamespace

import pytest
from django.utils import timezone

from apps.report_transfer import storage
from apps.report_transfer.contract import TransferError


def test_capacity_counts_orphans_and_reserves_incoming_space(tmp_path, settings, monkeypatch):
    settings.REPORT_TRANSFER_ROOT = tmp_path / "spool"
    settings.REPORT_TRANSFER_SPOOL_MAX_BYTES = 10
    settings.REPORT_TRANSFER_MIN_FREE_BYTES = 20
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda _: SimpleNamespace(free=30))
    storage.check_capacity(10)
    key = storage.write_payload(b"123456")
    storage.check_capacity(4)
    with pytest.raises(TransferError):
        storage.check_capacity(5)
    assert storage.payload_path(key).read_bytes() == b"123456"
    storage.remove_payload(key)
    storage.check_capacity(10)
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda _: SimpleNamespace(free=29))
    with pytest.raises(TransferError):
        storage.check_capacity(10)


def test_capacity_inspection_error_fails_closed(tmp_path, settings, monkeypatch):
    settings.REPORT_TRANSFER_ROOT = tmp_path / "spool"

    def fail(_):
        raise OSError("disposable inspection failure")

    monkeypatch.setattr(storage.shutil, "disk_usage", fail)
    with pytest.raises(TransferError) as error:
        storage.check_capacity(1)
    assert error.value.code == "report_storage_unavailable"


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
