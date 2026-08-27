from django.http import HttpResponse
from django.shortcuts import redirect

from apps.accounts.reauth import ReauthRedirect


class ReauthRedirectMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        except ReauthRedirect as redirect_exc:
            return self._response(request, redirect_exc)

    def process_exception(self, request, exception):
        if isinstance(exception, ReauthRedirect):
            return self._response(request, exception)
        return None

    @staticmethod
    def _response(request, redirect_exc: ReauthRedirect) -> HttpResponse:
        """Send HTMX requests through a real browser reauthentication navigation.

        HTMX follows ordinary 3xx responses internally.  Reauthentication is a
        complete page with a pending-action continuation, so an HTMX request
        must instead receive HX-Redirect and let the browser change location.
        """
        if request.headers.get("HX-Request") == "true":
            response = HttpResponse()
            response["HX-Redirect"] = redirect_exc.url
            return response
        return redirect(redirect_exc.url)
