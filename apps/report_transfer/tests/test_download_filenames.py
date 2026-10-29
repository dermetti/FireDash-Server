import re
from uuid import uuid4

import pytest

from apps.report_transfer.portal import download_filename


@pytest.mark.parametrize(
    "title",
    [
        "Einsatzbericht Hauptstraße / Gebäude 7",
        '../../CON\\NUL:<>"|?*\r\nX-Injected: value\x00\x1f\x7f',
        "LPT1. ",
        "...",
        "树 🌲",
        "A" * 200,
    ],
)
def test_download_names_are_portable_bounded_and_stable(title):
    document = {
        "title": title,
        "report_id": str(uuid4()),
        "attachments": [
            {"id": str(uuid4()), "title": title},
            {"id": str(uuid4()), "title": title},
        ],
    }
    pdf = download_filename(document)
    assert pdf.endswith(f"-{document['report_id']}.pdf")
    names = [pdf]
    for number, attachment in enumerate(document["attachments"], start=1):
        name = download_filename(document, attachment_id=attachment["id"])
        assert f"-foto-{number:02d}-" in name
        assert name.endswith(f"-{document['report_id']}-{attachment['id']}.jpg")
        assert name == download_filename(document, attachment_id=attachment["id"])
        names.append(name)
    assert len(set(names)) == 3
    assert pdf == download_filename(document)
    for name in names:
        assert len(name) <= 182
        assert re.fullmatch(r"report-[a-z0-9_-]+\.(pdf|jpg)", name)


def test_photo_number_uses_document_order_and_suffix_survives_title_collisions():
    first, second = str(uuid4()), str(uuid4())
    document = {
        "report_id": str(uuid4()),
        "title": "TEST Bericht",
        "attachments": [{"id": second, "title": "Foto"}, {"id": first, "title": "Foto"}],
    }
    assert "-foto-01-foto-" in download_filename(document, attachment_id=second)
    assert "-foto-02-foto-" in download_filename(document, attachment_id=first)
    original = download_filename(document, attachment_id=second)
    document["report_id"] = str(uuid4())
    assert original != download_filename(document, attachment_id=second)
