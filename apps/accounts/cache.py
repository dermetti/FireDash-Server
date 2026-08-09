class AuthenticatedNoStoreMiddleware:
    """Prevent browsers and intermediaries from reusing protected HTML after logout."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.user.is_authenticated and response.get("Content-Type", "").startswith(
            "text/html"
        ):
            response["Cache-Control"] = "no-store, private, must-revalidate"
            response["Pragma"] = "no-cache"
        return response
