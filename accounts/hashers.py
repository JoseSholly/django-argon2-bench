from django.contrib.auth.hashers import Argon2PasswordHasher


class Argon2Owasp(Argon2PasswordHasher):
    """OWASP's minimum Argon2id setting: 19 MiB, 2 passes, 1 lane."""

    time_cost = 2
    memory_cost = 19 * 1024  # KiB
    parallelism = 1


class Argon2Rfc9106(Argon2PasswordHasher):
    """RFC 9106's second recommended option, for memory-constrained systems."""

    time_cost = 3
    memory_cost = 64 * 1024  # KiB
    parallelism = 4
