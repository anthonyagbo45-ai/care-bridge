from urllib.parse import urlsplit

from django.conf import settings
from django.utils.cache import patch_cache_control


class PrivateHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not request.path.startswith(settings.STATIC_URL):
            patch_cache_control(response, private=True, no_store=True, max_age=0)
        connections = ["'self'"]
        for value in (settings.LIVEKIT_URL, settings.LIVEKIT_API_URL):
            try:
                parsed = urlsplit(value)
            except (ValueError, TypeError):
                continue
            if parsed.scheme in {"wss", "https"} and parsed.netloc and not parsed.username:
                connections.append(f"{parsed.scheme}://{parsed.netloc}")
        response.setdefault(
            "Content-Security-Policy",
            (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' blob:; media-src 'self' blob:; "
                f"connect-src {' '.join(connections)}; "
                "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            ),
        )
        response["Permissions-Policy"] = "camera=(self), microphone=(self), geolocation=()"
        response["X-Content-Type-Options"] = "nosniff"
        return response
