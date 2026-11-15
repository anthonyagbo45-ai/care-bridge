from django.urls import path

from . import media_views, views

app_name = "care"

urlpatterns = [
    path("", views.case_list, name="case-list"),
    path("updates/", views.inbox_updates, name="inbox-updates"),
    path("new/", views.case_create, name="case-create"),
    path("<uuid:case_id>/", views.case_detail, name="case-detail"),
    path("<uuid:case_id>/updates/", views.case_updates, name="case-updates"),
    path("<uuid:case_id>/actions/<slug:action>/", views.case_action, name="case-action"),
    path("<uuid:case_id>/respond/", views.case_respond, name="case-respond"),
    path("<uuid:case_id>/reports/", media_views.upload, name="document-upload"),
    path("reports/<uuid:report_id>/download/", media_views.download, name="document-download"),
    path("reports/<uuid:report_id>/preview/", media_views.preview, name="document-preview"),
    path("<uuid:case_id>/video/", media_views.video, name="video"),
    path("<uuid:case_id>/video/start/", media_views.video_start, name="video-start"),
    path("<uuid:case_id>/video/token/", media_views.video_token, name="video-token"),
    path("<uuid:case_id>/video/end/", media_views.video_end, name="video-end"),
    path("<uuid:case_id>/video/status/", media_views.video_status, name="video-status"),
]
