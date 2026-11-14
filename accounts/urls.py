from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("register/", views.register, name="register"),
    path("registration-done/", views.registration_done, name="registration-done"),
    path("resend/", views.resend, name="resend"),
    path("activate/<str:uid>/<str:token>/", views.activate, name="activate"),
    path("links/", views.links, name="links"),
    path("links/invite/", views.link_invite, name="link-invite"),
    path("links/<uuid:link_id>/<slug:action>/", views.link_action, name="link-action"),
]
