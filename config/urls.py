from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.urls import include, path

from accounts.views import SignInView

urlpatterns = [
    path("owner/", admin.site.urls),
    path("accounts/login/", SignInView.as_view(), name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path("accounts/", include("accounts.urls")),
    path("", include("care.urls")),
]
