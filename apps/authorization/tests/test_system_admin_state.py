from types import SimpleNamespace

from apps.authorization.services import classify_system_admin_state


def _role(is_active: bool):
    return SimpleNamespace(user=SimpleNamespace(is_active=is_active))


def test_classify_none():
    assert classify_system_admin_state([]) == "none"


def test_classify_single_active():
    assert classify_system_admin_state([_role(True)]) == "active"


def test_classify_single_inactive():
    assert classify_system_admin_state([_role(False)]) == "inactive"


def test_classify_multiple_wins_over_active():
    assert classify_system_admin_state([_role(True), _role(False)]) == "multiple"
    assert classify_system_admin_state([_role(True), _role(True)]) == "multiple"


def test_classify_multiple_wins_over_inactive():
    assert classify_system_admin_state([_role(False), _role(False)]) == "multiple"
