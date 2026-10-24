import os
from datetime import timedelta

SECRET_KEY = "demo-only-not-a-secret-change-me-in-production-0123456789"
DEBUG = False
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "teamhub.urls"
WSGI_APPLICATION = "teamhub.wsgi.application"
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "accounts",
]

MIDDLEWARE = [
    "accounts.middleware.QueryCountMiddleware",
    "django.middleware.common.CommonMiddleware",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "teamhub",
        "USER": "teamhub",
        "PASSWORD": "teamhub",
        "HOST": "127.0.0.1",
        "PORT": "5432",
        # Persistent connections, as you would run in production.
        "CONN_MAX_AGE": 60,
    }
}

# BENCH_HASHER picks the password hasher for a benchmark run.
_HASHERS = {
    "md5": "django.contrib.auth.hashers.MD5PasswordHasher",
    "argon2-default": "django.contrib.auth.hashers.Argon2PasswordHasher",
    "argon2-64m": "accounts.hashers.Argon2Mid",
    "argon2-owasp": "accounts.hashers.Argon2Owasp",
    "argon2-rfc9106": "accounts.hashers.Argon2Rfc9106",
}
PASSWORD_HASHERS = [_HASHERS[os.environ.get("BENCH_HASHER", "argon2-default")]] + [
    h for h in _HASHERS.values()
]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "UNAUTHENTICATED_USER": None,
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}

LOGGING = {"version": 1, "disable_existing_loggers": True}
