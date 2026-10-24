from django.utils import timezone

from .models import Invite, ProjectMember


def accept_pending_invites(user):
    invites = Invite.objects.filter(
        email=user.email, status="pending", expires_at__gt=timezone.now()
    ).select_related("project", "role")
    for invite in invites:
        ProjectMember.objects.get_or_create(
            user=user, project=invite.project, defaults={"role": invite.role}
        )
        invite.status = "accepted"
        invite.save(update_fields=["status"])
