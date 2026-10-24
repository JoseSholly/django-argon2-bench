"""The login and refresh endpoints as first written: correct, readable, slow."""

from django.contrib.auth import authenticate
from django.contrib.auth.models import User, update_last_login
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from .invites import accept_pending_invites
from .models import AuditLog, Membership, ProjectMember


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        email = attrs["email"].lower()
        # "Does the account exist?" - used for lockout bookkeeping.
        if not User.objects.filter(username=email).exists():
            raise serializers.ValidationError("Invalid credentials")
        user = authenticate(
            self.context["request"], username=email, password=attrs["password"]
        )
        if user is None:
            raise serializers.ValidationError("Invalid credentials")
        attrs["user"] = user
        return attrs


class ProjectSerializer(serializers.ModelSerializer):
    slug = serializers.CharField(source="project.slug")
    name = serializers.CharField(source="project.name")
    role = serializers.CharField(source="role.name")

    class Meta:
        model = ProjectMember
        fields = ("slug", "name", "role")


class MembershipSerializer(serializers.ModelSerializer):
    org = serializers.CharField(source="organization.slug")
    name = serializers.CharField(source="organization.name")
    role = serializers.CharField(source="role.name")
    projects = serializers.SerializerMethodField()

    class Meta:
        model = Membership
        fields = ("org", "name", "is_admin", "role", "projects")

    def get_projects(self, membership):
        rows = ProjectMember.objects.filter(
            user=membership.user_id,
            project__organization=membership.organization_id,
        ).order_by("id")
        return ProjectSerializer(rows, many=True).data


class UserPayloadSerializer(serializers.ModelSerializer):
    memberships = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ("id", "email", "memberships")

    def get_memberships(self, user):
        rows = Membership.objects.filter(user=user, is_active=True).order_by("id")
        return MembershipSerializer(rows, many=True).data


class LoginView(APIView):
    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]

        # Org admins are never invited, so skip the invite check for them.
        if not Membership.objects.filter(user=user, is_admin=True).exists():
            accept_pending_invites(user)

        # Pick up any flags the invite step may have changed.
        user.refresh_from_db()

        update_last_login(None, user)
        AuditLog.objects.create(
            user=user,
            action="LOGIN",
            ip_address=request.META.get("REMOTE_ADDR"),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": UserPayloadSerializer(user).data,
            },
            status=status.HTTP_200_OK,
        )


class RefreshView(TokenRefreshView):
    """simplejwt's stock view, with rotation and blacklisting switched on."""
