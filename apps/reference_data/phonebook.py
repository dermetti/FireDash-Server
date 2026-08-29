"""Canonical phonebook normalization and deterministic duplicate comparison."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from itertools import combinations

from apps.reference_data.models import PhonebookDuplicateDecision, PhonebookEntry


def normalize_phone_number(value: str) -> str:
    """Keep domestic presentation while repairing the common Hamburg compact form."""
    value = " ".join((value or "").strip().split())
    digits = re.sub(r"\D", "", value)
    if digits.startswith("04042851") and len(digits) == 12:
        return f"040 42851 {digits[-4:]}"
    return value


def _comparison_value(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").casefold().strip()


def entry_fingerprint(entry: PhonebookEntry) -> str:
    values = (
        entry.department_id,
        entry.station_id or "",
        entry.first_name,
        entry.last_name,
        entry.organization_unit,
        entry.function,
        entry.phone_number,
    )
    return hashlib.sha256("\x1f".join(map(str, values)).encode()).hexdigest()


def _signals(entry: PhonebookEntry) -> dict[str, str]:
    name = " ".join(
        filter(None, (_comparison_value(entry.first_name), _comparison_value(entry.last_name)))
    )
    return {
        "Name": name,
        "Organization unit": _comparison_value(entry.organization_unit),
        "Function": _comparison_value(entry.function),
        "Phone number": re.sub(r"\D", "", entry.phone_number),
        "Scope": str(entry.station_id or "department"),
    }


@dataclass(frozen=True)
class DuplicateCandidate:
    first: PhonebookEntry
    second: PhonebookEntry
    reasons: tuple[str, ...]
    conflicts: tuple[str, ...]
    first_fingerprint: str
    second_fingerprint: str
    exact: bool


def find_duplicate_candidates(*, department) -> list[DuplicateCandidate]:
    entries = list(
        PhonebookEntry.objects.filter(department=department)
        .select_related("station")
        .order_by("id")
    )
    candidates: list[DuplicateCandidate] = []
    for first, second in combinations(entries, 2):
        candidate = compare_phonebook_entries(first, second)
        if candidate is None:
            continue
        if PhonebookDuplicateDecision.objects.filter(
            department=department,
            first_entry=first,
            second_entry=second,
            first_fingerprint=candidate.first_fingerprint,
            second_fingerprint=candidate.second_fingerprint,
        ).exists():
            continue
        candidates.append(candidate)
    return sorted(
        candidates,
        key=lambda candidate: (
            not candidate.exact,
            -len(candidate.reasons),
            len(candidate.conflicts),
            str(candidate.first.id),
            str(candidate.second.id),
        ),
    )


def find_entry_duplicate_candidates(
    *, entry: PhonebookEntry, department
) -> list[DuplicateCandidate]:
    """Rank a proposed entry against one department's canonical entries.

    Import reconciliation and manual creation both use this adapter around the
    canonical comparison rule so their candidate ordering cannot drift.
    """
    candidates = [
        candidate
        for candidate in (
            compare_phonebook_entries(entry, existing)
            for existing in PhonebookEntry.objects.filter(department=department)
            .select_related("station")
            .order_by("id")
        )
        if candidate is not None
    ]
    return sorted(
        candidates,
        key=lambda candidate: (
            not candidate.exact,
            -len(candidate.reasons),
            len(candidate.conflicts),
            str(candidate.second.id),
        ),
    )


def compare_phonebook_entries(
    first: PhonebookEntry, second: PhonebookEntry
) -> DuplicateCandidate | None:
    """Compare canonical or staged entries with the one duplicate rule set."""
    first_signals, second_signals = _signals(first), _signals(second)
    reasons = tuple(
        label for label, value in first_signals.items() if value and value == second_signals[label]
    )
    # Scope is useful evidence but cannot turn a single data-field coincidence
    # into a review candidate.
    if len(tuple(reason for reason in reasons if reason != "Scope")) < 2:
        return None
    conflicts = tuple(
        label
        for label, value in first_signals.items()
        if value and second_signals[label] and value != second_signals[label]
    )
    return DuplicateCandidate(
        first,
        second,
        reasons,
        conflicts,
        entry_fingerprint(first),
        entry_fingerprint(second),
        not conflicts,
    )
