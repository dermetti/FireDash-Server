"""Keep the deployment boundary used for client-IP attribution explicit."""

from pathlib import Path


def test_nginx_replaces_forwarded_address_on_every_proxy_location():
    root = Path(__file__).resolve().parents[2]
    nginx = (root / "deploy/nginx/fire-backend.conf").read_text()
    proxy_count = nginx.count("proxy_pass http://fire_backend;")
    assert proxy_count > 0
    assert nginx.count("proxy_set_header X-Forwarded-For $remote_addr;") == proxy_count
    assert "$proxy_add_x_forwarded_for" not in nginx
    socket = (root / "deploy/systemd/fire-backend.socket").read_text()
    assert "ListenStream=/run/fire-backend/gunicorn.sock" in socket
    assert "SocketUser=fire_backend" in socket
    assert "SocketGroup=www-data" in socket
    assert "SocketMode=0660" in socket
    assert "DirectoryMode=0750" in socket
    gunicorn = (root / "deploy/gunicorn/gunicorn.conf.py").read_text()
    assert 'bind = "unix:/run/fire-backend/gunicorn.sock"' in gunicorn
