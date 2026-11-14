from accounts.throttles import account_throttle


class OwnerLoginThrottleMiddleware:
    """Apply the same bounded login policy to the approval owner's entry."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "POST" and request.path == "/owner/login/":
            blocked = account_throttle(request, "login", request.POST.get("username", ""))
            if blocked is not None:
                return blocked
        return self.get_response(request)
