import pytest

from tools.fake_ipad.cli import apply_provisioning_payload, build_parser
from tools.fake_ipad.errors import ClientError
from tools.fake_ipad.transport import ApiClient


def test_qr_provisioning_payload_selects_frozen_origin_and_token():
    args = build_parser().parse_args(
        [
            "adopt",
            "--provisioning-payload",
            '{"origin":"https://firedash.example.org","protocol":"firedash-provisioning-v1","token":"abc"}',
        ]
    )

    apply_provisioning_payload(args)

    assert args.server == "https://firedash.example.org"
    assert args.token == "abc"


@pytest.mark.parametrize(
    "payload",
    [
        '{"origin":"http://firedash.example.org","protocol":"firedash-provisioning-v1","token":"abc"}',
        '{"origin":"https://other.example.org/path","protocol":"firedash-provisioning-v1","token":"abc"}',
        '{"origin":"https://firedash.example.org","protocol":"wrong","token":"abc"}',
        '{"origin":"https://user@firedash.example.org","protocol":"firedash-provisioning-v1","token":"abc"}',
    ],
)
def test_qr_provisioning_payload_rejects_invalid_origin_or_protocol(payload):
    args = build_parser().parse_args(["adopt", "--provisioning-payload", payload])
    with pytest.raises(ClientError):
        apply_provisioning_payload(args)


def test_api_client_rejects_cross_origin_absolute_download_url():
    client = ApiClient("https://firedash.example.org")
    assert client.make_url("/api/v1/tablet/datasets/a/download") == (
        "https://firedash.example.org/api/v1/tablet/datasets/a/download"
    )
    with pytest.raises(ClientError, match="outside the API origin"):
        client.make_url("https://firedash.example.org.evil.example/download")
