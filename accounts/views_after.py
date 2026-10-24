"""The same endpoints, rewritten so the query count no longer grows with the user."""

from collections import defaultdict

from django.contrib.auth import authenticate
from django.contrib.auth.models import update_last_login
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)
from rest_framework_simplejwt.tokens import RefreshToken

from .invites import accept_pending_invites
from .models import AuditLog, Membership, ProjectMember


class CredentialsSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


def build_user_payload(user):
    """Two queries, however many organizations and projects the user has."""
    memberships = list(
        Membership.objects.filter(user=user, is_active=True)
        .select_related("organization", "role")
        .order_by("id")
    )
    projects_by_org = defaultdict(list)
    if memberships:
        rows = (
            ProjectMember.objects.filter(
                user=user,
                project__organization__in=[m.organization_id for m in memberships],
            )
            .select_related("project", "role")
            .order_by("id")
        )
        for pm in rows:
            projects_by_org[pm.project.organization_id].append(
                {"slug": pm.project.slug, "name": pm.project.name, "role": pm.role.name}
            )

    return {
        "id": user.id,
        "email": user.email,
        "memberships": [
            {
                "org": m.organization.slug,
                "name": m.organization.name,
                "is_admin": m.is_admin,
                "role": m.role.name,
                "projects": projects_by_org[m.organization_id],
            }
            for m in memberships
        ],
    }


class LoginView(APIView):
    def post(self, request):
        creds = CredentialsSerializer(data=request.data)
        creds.is_valid(raise_exception=True)

        # One SELECT. No separate "does the account exist?" query first:
        # authenticate() already answers that.
        user = authenticate(
            request,
            username=creds.validated_data["email"].lower(),
            password=creds.validated_data["password"],
        )
        if user is None:
            return Response(
                {"detail": "Invalid credentials"}, status=status.HTTP_400_BAD_REQUEST
            )

        # One SELECT that finds nothing on almost every login.
        accept_pending_invites(user)

        # Three writes, one commit.
        with transaction.atomic():
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
                "user": build_user_payload(user),
            }
        )


class _PresentedRefreshToken(RefreshToken):
    def check_blacklist(self):
        # Checked by the single SELECT in RefreshView instead.
        pass


class RefreshView(APIView):
    def post(self, request):
        raw = request.data.get("refresh")
        if not raw:
            raise InvalidToken("No refresh token")
        presented = _PresentedRefreshToken(raw)  # signature, expiry, type

        try:
            with transaction.atomic():
                # 1. Token row, its user, and "is it blacklisted?" in one query.
                outstanding = (
                    OutstandingToken.objects.select_related("user")
                    .annotate(
                        used=Exists(BlacklistedToken.objects.filter(token=OuterRef("pk")))
                    )
                    .filter(jti=presented["jti"])
                    .first()
                )
                if outstanding is None or outstanding.used:
                    raise InvalidToken("Token is blacklisted")
                if outstanding.user is None or not outstanding.user.is_active:
                    raise InvalidToken("No active account")

                # 2. Blacklist it. The one-to-one unique constraint means a
                #    second request racing with the same token fails here.
                BlacklistedToken.objects.create(token=outstanding)

                # 3. Issue and record the new refresh token.
                new = RefreshToken.for_user(outstanding.user)
        except IntegrityError:
            raise InvalidToken("Token is blacklisted")

        return Response({"access": str(new.access_token), "refresh": str(new)})
