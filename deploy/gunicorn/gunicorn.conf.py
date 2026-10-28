bind = "unix:/run/fire-backend/gunicorn.sock"
workers = 2
worker_class = "sync"
timeout = 90
graceful_timeout = 30
keepalive = 5
accesslog = None
errorlog = "-"
capture_output = True
# Even if access logging is enabled later, never include bearer paths, query
# strings, Referer or attacker-controlled user-agent values.
access_log_format = "%(h)s %(t)s %(m)s %(s)s %(b)s"


def post_fork(server, worker):
    from apps.report_transfer.security import TokenLogFilter

    # Worker diagnostics may contain paths independently of the access logger.
    server.log.error_log.addFilter(TokenLogFilter())
    server.log.access_log.addFilter(TokenLogFilter())
