import json

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from rest_framework_simplejwt.tokens import RefreshToken


class Command(BaseCommand):
    """Write N fresh refresh tokens (round-robin over users) to a JSON file."""

    def add_arguments(self, parser):
        parser.add_argument("count", type=int)
        parser.add_argument("out")

    def handle(self, count, out, **_):
        users = list(User.objects.order_by("id"))
        tokens = [str(RefreshToken.for_user(users[i % len(users)])) for i in range(count)]
        with open(out, "w") as f:
            json.dump(tokens, f)
        self.stdout.write(f"{count} tokens -> {out}")
