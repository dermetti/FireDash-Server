"""Read-only diagnostics for one adoption request.

Prints only safe metadata so a live iPad request can be diagnosed without
exposing invitation tokens, the challenge nonce, the expected HMAC, private
keys, installation credentials, or the raw encrypted challenge.
"""

import hashlib

from django.core.management.base import BaseCommand, CommandError

from apps.publications.hpke import HPKE_CIPHERSUITE
from apps.tablets.models import AdoptionRequest
from apps.tablets.services import (
    ADOPTION_PROTOCOL,
    AdoptionChallengeContext,
    canonical_protocol_datetime,
)

_P256_ENC_BYTES = 65
_CHALLENGE_PLAINTEXT_BYTES = 32
_AEAD_TAG_BYTES = 16
_CIPHERTEXT_BYTES = _CHALLENGE_PLAINTEXT_BYTES + _AEAD_TAG_BYTES  # 48


class Command(BaseCommand):
    help = "Print safe diagnostic metadata for one adoption request."

    def add_arguments(self, parser):
        parser.add_argument("adoption_request_id")

    def handle(self, *args, **options):
        request = (
            AdoptionRequest.objects.select_related("invitation__tablet")
            .filter(pk=options["adoption_request_id"])
            .first()
        )
        if request is None:
            raise CommandError(f"No AdoptionRequest with id {options['adoption_request_id']}")

        tablet = request.invitation.tablet

        context = AdoptionChallengeContext(
            adoption_request_id=request.id,
            installation_uuid=request.installation_uuid,
            tablet_id=tablet.id,
            public_key_fingerprint=request.hpke_public_key_fingerprint,
            expires_at=request.expires_at,
            mode="adoption",
        )
        canonical_info = context.info()
        calculated_hash = hashlib.sha256(canonical_info).hexdigest()

        encrypted_challenge = bytes(request.encrypted_challenge)
        enc_bytes = len(encrypted_challenge) - _CIPHERTEXT_BYTES
        framing_consistent = (
            len(encrypted_challenge) == _P256_ENC_BYTES + _CIPHERTEXT_BYTES
            and enc_bytes == _P256_ENC_BYTES
        )

        self.stdout.write(
            "\n".join(
                (
                    f"adoption_request_id={request.id}",
                    f"installation_uuid={request.installation_uuid}",
                    f"tablet_id={tablet.id}",
                    "mode=adoption",
                    f"protocol={ADOPTION_PROTOCOL}",
                    f"hpke_ciphersuite={HPKE_CIPHERSUITE}",
                    f"hpke_public_key_fingerprint={request.hpke_public_key_fingerprint}",
                    f"db_expires_at={request.expires_at.isoformat()}",
                    f"canonical_expires_at={canonical_protocol_datetime(request.expires_at)}",
                    f"canonical_context_json={canonical_info.decode('ascii')}",
                    f"canonical_context_sha256={calculated_hash}",
                    f"stored_canonical_context_hash={request.canonical_context_hash}",
                    f"context_hash_matches={calculated_hash == request.canonical_context_hash}",
                    f"recipient_public_key_bytes={len(bytes(request.hpke_public_key))}",
                    f"encrypted_challenge_bytes={len(encrypted_challenge)}",
                    f"expected_enc_bytes={_P256_ENC_BYTES}",
                    f"expected_ciphertext_bytes={_CIPHERTEXT_BYTES}",
                    f"expected_plaintext_bytes={_CHALLENGE_PLAINTEXT_BYTES}",
                    f"framing_consistent={framing_consistent}",
                )
            )
        )
