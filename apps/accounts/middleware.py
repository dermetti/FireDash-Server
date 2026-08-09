from django.shortcuts import redirect

from apps.accounts.reauth import ReauthRedirect


class ReauthRedirectMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        except ReauthRedirect as redirect_exc:
            return redirect(redirect_exc.url)

    def process_exception(self, request, exception):
        if isinstance(exception, ReauthRedirect):
            return redirect(exception.url)
        return None
