import logging
import sys
from datetime import timedelta

import pytest
from django.test import Client, RequestFactory
from django.urls import resolve, reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.security import CredentialExceptionReporter, SetupSecurityMiddleware
from apps.accounts.services import create_setup_token
from apps.report_transfer.security import TokenLogFilter


@pytest.fixture
def setup_credential(db):
    actor = User.objects.create_user("issuer@example.test", "Issuer", "password")
    return create_setup_token(actor=actor, email="new@example.test", display_name="New")


def assert_safe(records, token):
    assert records
    assert all(token not in r.getMessage() and token not in (r.exc_text or "") for r in records)


@pytest.mark.parametrize("secure", [False, True])
def test_setup_get_and_csrf_valid_post_preserve_consumption(setup_credential, caplog, secure):
    record, token = setup_credential
    client = Client(enforce_csrf_checks=True)
    url = reverse("accounts-setup", args=(token,))
    assert client.get(url, secure=secure).status_code == 200
    password = "Disposable-password-937!"
    response = client.post(
        url,
        {
            "password": password,
            "password_confirm": password,
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        },
        secure=secure,
        HTTP_REFERER="https://testserver" + url,
    )
    assert response.status_code == 302
    record.refresh_from_db()
    record.user.refresh_from_db()
    assert record.used_at is not None
    assert record.user.is_active and record.user.check_password(password)
    assert not record.user.mfa_enabled
    assert "_auth_user_id" not in client.session
    response = client.post(
        reverse("accounts-login"),
        {
            "email": record.user.email,
            "password": password,
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        },
        secure=secure,
        HTTP_REFERER="https://testserver" + url,
    )
    assert response.status_code == 302 and response["Location"] == reverse("accounts-mfa-enroll")
    assert client.get(response["Location"]).status_code == 200
    assert all(token not in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("expired", [False, True])
def test_invalid_and_expired_setup_rejected(setup_credential, expired, caplog):
    record, token = setup_credential
    if expired:
        record.expires_at = timezone.now() - timedelta(seconds=1)
        record.save()
    else:
        token += "invalid"
    password = "Disposable-password-937!"
    response = Client().post(
        reverse("accounts-setup", args=(token,)),
        {
            "password": password,
            "password_confirm": password,
        },
    )
    assert response.status_code == 200
    assert b"invalid or expired" in response.content
    record.refresh_from_db()
    record.user.refresh_from_db()
    assert record.used_at is None and not record.user.is_active
    assert all(token not in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("boundary", ["render", "service", "middleware"])
def test_exception_diagnostics_redact_bare_token(setup_credential, monkeypatch, caplog, boundary):
    record, token = setup_credential

    def fail(*args, **kwargs):
        raise RuntimeError("Disposable failure " + token)

    if boundary == "middleware":
        monkeypatch.setattr("django.middleware.security.SecurityMiddleware.process_request", fail)
    else:
        monkeypatch.setattr(
            "apps.accounts.views." + ("render" if boundary == "render" else "consume_setup_token"),
            fail,
        )
    client = Client(raise_request_exception=False)
    url = reverse("accounts-setup", args=(token,))
    response = (
        client.get(url)
        if boundary == "render"
        else client.post(
            url,
            {
                "password": "Disposable-password-937!",
                "password_confirm": "Disposable-password-937!",
            },
        )
    )
    assert response.status_code == 500
    assert_safe(caplog.records, token)
    record.refresh_from_db()
    assert record.used_at is None


@pytest.mark.parametrize("suffix", ["/extra", "//extra", "?credential=duplicate", "%2Fextra"])
def test_malformed_setup_paths_are_redacted(setup_credential, suffix, caplog):
    _, token = setup_credential
    response = Client(enforce_csrf_checks=True).post("/accounts/setup/" + token + suffix)
    assert response.status_code in (301, 403, 404)
    logging.getLogger("django.request").warning(
        "Rejected %s", response.wsgi_request.get_full_path()
    )
    assert_safe(caplog.records, token)


def test_structured_logs_and_exception_reports(setup_credential):
    _, token = setup_credential
    request = RequestFactory().get(
        "/accounts/setup/" + token + "/?credential=" + token,
        HTTP_REFERER="https://example.test/accounts/setup/" + token,
    )
    request.resolver_match = resolve(request.path)
    log_filter = TokenLogFilter()

    def diagnose(request):
        try:
            local_credential = token
            raise RuntimeError("Disposable " + local_credential)
        except RuntimeError:
            info = sys.exc_info()
            record = logging.LogRecord(
                "django.request",
                logging.ERROR,
                __file__,
                1,
                "Path %(path)s",
                ({"path": request.get_full_path()},),
                info,
            )
            record.request = request
            record.details = {"uri": [request.get_full_path(), token]}
            assert log_filter.filter(record)
            assert token not in logging.Formatter().format(record)
            assert token not in str(record.__dict__["details"])
            assert token not in record.__dict__["request"].get_full_path()
            assert token not in str(record.__dict__["request"].META)
            assert token not in str(record.__dict__["request"].resolver_match)
            reporter = CredentialExceptionReporter(request, *info)
            assert token not in reporter.get_traceback_text()
            assert token not in reporter.get_traceback_html()
            assert token not in CredentialExceptionReporter(None, *info).get_traceback_text()
        return None

    SetupSecurityMiddleware(diagnose)(request)
    # Diagnostic protection must not change routing or the submitted credential.
    assert request.path == "/accounts/setup/" + token + "/"
    assert request.resolver_match.kwargs["token"] == token


def test_shared_filter_preserves_report_transfer_and_ordinary_logging():
    filter_ = TokenLogFilter()
    for path in ("/accounts/setup/disposable?key=disposable", "/reports/access/disposable"):
        record = logging.LogRecord("test", logging.ERROR, __file__, 1, "Rejected %s", (path,), None)
        filter_.filter(record)
        assert "disposable" not in record.getMessage()
    ordinary = logging.LogRecord(
        "test", logging.ERROR, __file__, 1, "Ordinary failure %s", ("database unavailable",), None
    )
    filter_.filter(ordinary)
    assert ordinary.getMessage() == "Ordinary failure database unavailable"
    server_record = logging.LogRecord(
        "django.server",
        logging.INFO,
        __file__,
        1,
        "GET /accounts/setup/disposable/ HTTP/1.1",
        (),
        None,
    )
    server_record.request = object()  # runserver supplies its socket, not an HttpRequest
    filter_.filter(server_record)
    assert "disposable" not in server_record.getMessage()


def test_diagnostic_email_subject_and_body_are_sanitized(setup_credential, monkeypatch):
    from django.utils.log import AdminEmailHandler

    _, token = setup_credential
    request = RequestFactory().get("/accounts/setup/" + token + "/?key=" + token)
    from django.contrib.auth.models import AnonymousUser

    request.user = AnonymousUser()
    messages = []

    class CapturingHandler(AdminEmailHandler):
        def send_mail(self, subject, message, **kwargs):
            messages.append((subject, message, kwargs))

    handler = CapturingHandler(include_html=True)
    handler.addFilter(TokenLogFilter())
    try:
        raise RuntimeError("Disposable diagnostic " + token)
    except RuntimeError:
        record = logging.LogRecord(
            "django.security.DisallowedHost",
            logging.ERROR,
            __file__,
            1,
            "Rejected %s",
            (request,),
            sys.exc_info(),
        )
        record.request = request
        handler.handle(record)
    assert messages
    assert token not in str(messages)


def test_unrelated_exception_diagnostics_are_preserved():
    try:
        raise RuntimeError("Ordinary database unavailable")
    except RuntimeError:
        record = logging.LogRecord(
            "ordinary", logging.ERROR, __file__, 1, "Ordinary failure", (), sys.exc_info()
        )
        original = record.exc_info
        TokenLogFilter().filter(record)
        assert record.exc_info is original
        assert "Ordinary database unavailable" in logging.Formatter().format(record)


@pytest.mark.parametrize(
    "path",
    [
        "/ordinary/?next=/accounts/setup/disposable",
        "/accounts%2Fsetup%2Fdisposable",
        "%252Faccounts%252Fsetup%252Fdisposable",
        "/ACCOUNTS/SETUP/disposable/extra",
    ],
)
def test_rejected_and_encoded_uri_diagnostics(path):
    record = logging.LogRecord(
        "django.request", logging.ERROR, __file__, 1, "Rejected %s", (path,), None
    )
    TokenLogFilter().filter(record)
    assert "disposable" not in record.getMessage()
