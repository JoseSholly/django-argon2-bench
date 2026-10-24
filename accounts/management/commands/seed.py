from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Membership, Organization, Project, ProjectMember, Role

PASSWORD = "correct-horse-battery-staple"


class Command(BaseCommand):
    """Users small_<i> (1 org, 1 project) and big_<i> (5 orgs, 50 projects)."""

    def add_arguments(self, parser):
        parser.add_argument("--users", type=int, default=40)
        parser.add_argument("--orgs", type=int, default=5)
        parser.add_argument("--projects-per-org", type=int, default=10)

    @transaction.atomic
    def handle(self, users, orgs, projects_per_org, **_):
        User.objects.all().delete()
        Organization.objects.all().delete()
        Role.objects.all().delete()
        roles = [Role.objects.create(name=n) for n in ("Owner", "Manager", "Engineer")]
        org_rows, projects = [], []
        for o in range(orgs):
            org = Organization.objects.create(name=f"Org {o}", slug=f"org-{o}")
            org_rows.append(org)
            for p in range(projects_per_org):
                projects.append(
                    Project.objects.create(
                        organization=org, name=f"Project {o}.{p}", slug=f"p-{o}-{p}"
                    )
                )

        hashed = make_password(PASSWORD)
        for i in range(users):
            for kind, my_orgs in (("small", org_rows[:1]), ("big", org_rows)):
                user = User.objects.create(
                    username=f"{kind}_{i}@example.com",
                    email=f"{kind}_{i}@example.com",
                    password=hashed,
                )
                for n, org in enumerate(my_orgs):
                    Membership.objects.create(
                        user=user, organization=org, role=roles[n % 3]
                    )
                my_projects = (
                    projects[:1]
                    if kind == "small"
                    else [p for p in projects if p.organization in my_orgs]
                )
                ProjectMember.objects.bulk_create(
                    ProjectMember(user=user, project=p, role=roles[2])
                    for p in my_projects
                )
        self.stdout.write(f"seeded {users * 2} users, {len(projects)} projects")
