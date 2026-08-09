def navigation(request):
    if not request.user.is_authenticated:
        return {}
    from apps.portal.views import _nav_context

    return _nav_context(request)
