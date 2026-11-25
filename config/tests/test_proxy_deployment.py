"""Keep the deployment boundary used for client-IP attribution explicit."""

import re
from pathlib import Path


def test_nginx_replaces_forwarded_address_on_every_proxy_location():
    root = Path(__file__).resolve().parents[2]
    nginx = (root / "deploy/nginx/fire-backend.conf").read_text()
    proxy_count = nginx.count("proxy_pass http://fire_backend;")
    assert proxy_count > 0
    # Match this template's four-space outer locations, including their children.
    # Nginx inherits proxy_set_header as a set only when a child defines none.
    # Each outer proxy location must replace client headers; its nested proxy
    # locations must inherit the complete set without introducing overrides.
    proxy_locations = [
        match.group(1)
        for match in re.finditer(r"^    location [^\n]+\{\n(.*?)^    \}", nginx, re.M | re.S)
        if "proxy_pass" in match.group(1)
    ]
    assert sum(block.count("proxy_pass http://fire_backend;") for block in proxy_locations) == (
        proxy_count
    )
    for block in proxy_locations:
        headers = [line for line in block.splitlines() if "proxy_set_header" in line]
        assert headers
        assert all(line.startswith("        proxy_set_header ") for line in headers)
        for header in (
            "Host $host",
            "X-Real-IP $remote_addr",
            "X-Forwarded-For $remote_addr",
            "X-Forwarded-Proto $scheme",
        ):
            assert headers.count(f"        proxy_set_header {header};") == 1
    assert nginx.count("proxy_set_header X-Forwarded-For $remote_addr;") == len(proxy_locations)
    assert "$proxy_add_x_forwarded_for" not in nginx
    socket = (root / "deploy/systemd/fire-backend.socket").read_text()
    assert "ListenStream=/run/fire-backend/gunicorn.sock" in socket
    assert "SocketUser=fire_backend" in socket
    assert "SocketGroup=www-data" in socket
    assert "SocketMode=0660" in socket
    assert "DirectoryMode=0750" in socket
    gunicorn = (root / "deploy/gunicorn/gunicorn.conf.py").read_text()
    assert 'bind = "unix:/run/fire-backend/gunicorn.sock"' in gunicorn


def test_credential_logging_boundaries():
    import logging
    import runpy
    from types import SimpleNamespace

    root = Path(__file__).resolve().parents[2]
    nginx = (root / "deploy/nginx/fire-backend.conf").read_text()
    for listen in ("80", "[::]:80", "443 ssl", "[::]:443 ssl"):
        assert f"listen {listen} default_server;" in nginx
    assert nginx.count("error_log /dev/null crit;") == 4
    assert "location ^~ /accounts/setup {" in nginx
    # Preserve Report Transfer stripping and setup CSRF Referer fallback.
    assert nginx.count('proxy_set_header Referer "";') == 1
    format_ = nginx.split("log_format fire_backend_safe", 1)[1].split(";", 1)[0]
    assert format_.strip() == "'$request_id $status $body_bytes_sent $request_time'"
    config = runpy.run_path(str(root / "deploy/gunicorn/gunicorn.conf.py"))
    logs = SimpleNamespace(
        error_log=logging.Logger("gunicorn-test-error"),
        access_log=logging.Logger("gunicorn-test-access"),
    )
    config["on_starting"](SimpleNamespace(log=logs))
    for logger in (logs.error_log, logs.access_log):
        record = logging.LogRecord(
            logger.name,
            logging.ERROR,
            __file__,
            1,
            "Rejected /accounts/setup/disposable?token=disposable",
            (),
            None,
        )
        assert logger.filter(record)
        assert "disposable" not in record.getMessage()
