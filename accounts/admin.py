from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .forms import AdminUserChangeForm, AdminUserCreationForm
from .models import Hospital, User


class OwnerOnlyAdmin:
    """Approval administration belongs only to the trusted application owner."""

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Hospital)
class HospitalAdmin(OwnerOnlyAdmin, admin.ModelAdmin):
    list_display = ("name", "is_approved", "created_at")
    list_filter = ("is_approved",)
    search_fields = ("name",)
    readonly_fields = ("id", "created_at")


@admin.register(User)
class UserAdmin(OwnerOnlyAdmin, BaseUserAdmin):
    form = AdminUserChangeForm
    add_form = AdminUserCreationForm
    ordering = ("email",)
    list_display = (
        "email",
        "first_name",
        "role",
        "hospital",
        "is_active",
        "email_verified_at",
        "doctor_verified",
    )
    list_filter = ("role", "is_active", "doctor_verified")
    search_fields = ("email", "first_name", "registration_number")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Named account", {"fields": ("first_name", "last_name", "role", "hospital")}),
        (
            "Owner verification",
            {
                "fields": (
                    "email_verified_at",
                    "registration_number",
                    "doctor_verified",
                    "verification_reference",
                )
            },
        ),
        (
            "Owner access",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "first_name", "role", "hospital", "password1", "password2"),
            },
        ),
    )
    readonly_fields = ("last_login", "date_joined")
