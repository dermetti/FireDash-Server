from django.db import connections
from django.db.utils import DatabaseError
from django.http import JsonResponse
from django.http.request import HttpRequest
from django.views.decorators.http import require_GET


@require_GET
def live(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


@require_GET
def ready(request: HttpRequest) -> JsonResponse:
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT postgis_version()")
            cursor.fetchone()
    except DatabaseError:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})
