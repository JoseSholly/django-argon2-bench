from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from .seed import PASSWORD


class Command(BaseCommand):
    """Re-hash every password with the current default hasher (BENCH_HASHER)."""

    def handle(self, **_):
        hashed = make_password(PASSWORD)
        n = User.objects.update(password=hashed)
        self.stdout.write(f"{n} passwords -> {hashed.split('$')[0]} {hashed.split('$')[2]}")
