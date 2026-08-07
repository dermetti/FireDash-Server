from django.test import Client


def test_setup_page_security_headers_allow_same_origin_form_posts() -> None:
    response = Client().get("/accounts/setup/a-valid-looking-token/", HTTP_HOST="localhost")

    assert response.status_code == 200
    assert "sandbox" not in response["Content-Security-Policy"].lower()
    assert "form-action 'self'" in response["Content-Security-Policy"]
    assert response["X-Frame-Options"] == "DENY"
    assert response["Referrer-Policy"] == "origin"


def test_setup_page_csrf_accepts_same_origin_and_rejects_null_origin() -> None:
    client = Client(enforce_csrf_checks=True)
    path = "/accounts/setup/a-valid-looking-token/"
    host = "127.0.0.1:8000"
    csrf_token = client.get(path, HTTP_HOST=host).cookies["csrftoken"].value

    same_origin = client.post(
        path,
        {"csrfmiddlewaretoken": csrf_token},
        HTTP_HOST=host,
        HTTP_ORIGIN="http://127.0.0.1:8000",
    )
    null_origin = client.post(
        path,
        {"csrfmiddlewaretoken": csrf_token},
        HTTP_HOST=host,
        HTTP_ORIGIN="null",
    )

    assert same_origin.status_code == 200
    assert null_origin.status_code == 403


def test_login_page_csrf_accepts_same_origin_and_rejects_null_origin() -> None:
    client = Client(enforce_csrf_checks=True)
    path = "/accounts/login/"
    host = "127.0.0.1:8000"
    login_page = client.get(path, HTTP_HOST=host)
    csrf_token = login_page.cookies["csrftoken"].value

    same_origin = client.post(
        path,
        {"csrfmiddlewaretoken": csrf_token},
        HTTP_HOST=host,
        HTTP_ORIGIN="http://127.0.0.1:8000",
    )
    null_origin = client.post(
        path,
        {"csrfmiddlewaretoken": csrf_token},
        HTTP_HOST=host,
        HTTP_ORIGIN="null",
    )

    assert login_page["Referrer-Policy"] == "origin"
    assert same_origin.status_code == 200
    assert null_origin.status_code == 403
