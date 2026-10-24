# Django Argon2 under load

Sample Django API and benchmark for the article
"Ten Logins, One Gigabyte: Benchmarking Django's Argon2 Settings Under Load".

## Setup

Needs Python 3.12+, a local PostgreSQL (database/user/password `teamhub`, see
`teamhub/settings.py`), and Linux with root and the cgroup v1 memory
controller for the memory measurement (on cgroup v2, read `memory.peak`).

    python -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python manage.py migrate
    .venv/bin/python manage.py seed      # 40 small users, 40 users with 5 orgs / 50 projects

## Run

    .venv/bin/python bench/bench.py argon2       # 1 login vs bursts of 10, 3 runs per setting
    .venv/bin/python bench/bench.py sweep        # sustained load at 1..16 clients
    .venv/bin/python bench/bench.py end_to_end   # original vs optimised login code

Each run starts a fresh Gunicorn (9 sync workers by default, `--workers`).
Raw per-run results are appended to `bench/results.jsonl`; the `out_*.txt`
files are the terminal output used in the article.

## Layout

- `accounts/views_before.py` - login as first written (N+1 queries)
- `accounts/views_after.py` - login with a fixed query count
- `accounts/hashers.py` - the Argon2 settings compared
- `accounts/middleware.py` - adds `X-DB-Queries` / `X-Server-Ms` headers
