import pytest

from apps.tablets.versions import AppVersionError, parse_app_build, parse_app_version


@pytest.mark.parametrize("value", ("0.0.0", "1.2.3", "10.0.0", "2147483647.0.0"))
def test_app_version_accepts_exact_numeric_triplets(value):
    assert str(parse_app_version(value)) == value


@pytest.mark.parametrize("value", ("1.0", "1.0.0-beta", "01.0.0", "1.0.0.0", "-1.0.0"))
def test_app_version_rejects_noncanonical_values(value):
    with pytest.raises(AppVersionError):
        parse_app_version(value)


def test_app_version_comparison_is_numeric():
    assert parse_app_version("10.0.0") > parse_app_version("2.0.0")


@pytest.mark.parametrize("value", ("1", 57, "9223372036854775807"))
def test_app_build_accepts_positive_bigints(value):
    assert parse_app_build(value) >= 1


@pytest.mark.parametrize("value", ("0", "-1", "1.0", "9223372036854775808"))
def test_app_build_rejects_invalid_values(value):
    with pytest.raises(AppVersionError):
        parse_app_build(value)
