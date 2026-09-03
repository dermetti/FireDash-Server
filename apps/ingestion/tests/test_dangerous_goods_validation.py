import json

import pytest

from apps.ingestion.dangerous_goods import DangerousGoodsValidationError, validate_dangerous_goods


def dangerous_goods_document():
    return {
        "dataset_type": "dangerous_goods",
        "schema_version": 1,
        "metadata": {
            "publication_profile": "compact",
            "record_count": 1,
            "eri_card_count": 1,
            "placard_catalog": {
                "available_assets": {"3": "ADR_3.svg", "7A": "a", "7B": "b", "7C": "c", "7D": "d"},
                "special_values": {
                    "7X": {"kind": "variable", "candidate_codes": ["7A", "7B", "7C"]}
                },
            },
        },
        "goods": [
            {
                "id": "bam-1",
                "un_number": "1234",
                "names": {"official": {"it": "Nome"}},
                "adr": {"placards": ["3"]},
                "eri": ["3-01"],
            }
        ],
        "eri_defaults": {"1234": "3-01"},
        "eri_cards": {"3-01": [["title", "Title"], ["item", "Text"]]},
        "sources": [
            {
                "id": "bam",
                "provider": "BAM",
                "dataset": "ADR",
                "source_file": "a",
                "sha256": "a",
                "source_url": "https://example.test",
                "legal": {"legal_url": "x", "license": {}, "attribution": {}, "processing": {}},
            },
            {
                "id": "ericards",
                "provider": "Cefic",
                "dataset": "ERI",
                "source_file": "b",
                "sha256": "b",
                "source_url": "https://example.test",
                "legal": {
                    "terms_url": "x",
                    "guidance_url": "x",
                    "disclaimer_url": "x",
                    "attribution": {},
                    "reproduction": {},
                },
            },
        ],
    }


def encoded(document):
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()


def test_valid_compact_schema_accepts_any_official_name_language():
    _, summary = validate_dangerous_goods(encoded(dangerous_goods_document()))
    assert summary == {"goods_count": 1, "eri_card_count": 1}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda source: source["goods"][0].update(un_number="12"), "four-digit"),
        (lambda source: source["goods"][0].update(eri=["missing"]), "ERI references"),
        (lambda source: source["goods"][0]["adr"].update(placards=["missing"]), "asset catalog"),
        (
            lambda source: source["goods"][0]["adr"].update(
                placards=[
                    {
                        "kind": "variable",
                        "candidate_codes": ["7A", "7B", "7D"],
                        "selection_basis": "x",
                    }
                ]
            ),
            "7A, 7B, and 7C",
        ),
    ],
)
def test_rejects_contract_failures(mutate, message):
    source = dangerous_goods_document()
    mutate(source)
    with pytest.raises(DangerousGoodsValidationError, match=message):
        validate_dangerous_goods(encoded(source))
