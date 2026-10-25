# Django Argon2 under load

Sample Django API and benchmarks for the article
"Ten Logins, One Gigabyte: Benchmarking Django's Argon2 Settings Under Load".

The full write-up, with the complete methodology, every result with its range and the
measurement caveats, is in [`docs/full-writeup.md`](docs/full-writeup.md). The screenshots
used in the article are in `shots/`.

The numbers in the article come from one 4-vCPU machine (Intel Xeon @ 2.10 GHz,
16 GB RAM, Python 3.13, PostgreSQL 16 on the same host). They are evidence from
that experiment, not a guarantee for your hardware.

## Requirements

- Linux (the scripts read `/proc`), Python 3.12+
- A local PostgreSQL with a `teamhub` role and database:

      createuser teamhub --pwprompt      # password: teamhub
      createdb -O teamhub teamhub        # or set TEAMHUB_DB to another name

- For the kernel memory peak: root and the cgroup v1 memory controller
  (`/sys/fs/cgroup/memory`). Without them the scripts fall back to summed
  worker RSS, polled every 2 ms, and print which source they used.
  `BENCH_NO_CGROUP=1` forces the fallback.

## Setup

    python -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python manage.py migrate
    .venv/bin/python manage.py seed      # 40 users with 1 org/1 project, 40 with 5 orgs/50 projects

## Run (about 30 minutes in total; run nothing else meanwhile)

    .venv/bin/python bench/hash_cost.py              # one hash: wall time, CPU time, threads
    .venv/bin/python bench/bench.py argon2           # single logins + bursts of 10, 3 runs per setting
    .venv/bin/python bench/bench.py sweep            # 1..16 clients, 3 runs per setting
    .venv/bin/python bench/bench.py end_to_end --n 200
    .venv/bin/python bench/bench.py sweep --from-results   # reprint the sweep table from results.jsonl

Every run starts a fresh Gunicorn (9 sync workers; `--workers`). Raw
per-run results are appended to `bench/results.jsonl`. The `bench/out_*.txt`
files and `bench/results.jsonl` in this repository are the data behind the article.

## What is measured

- Latency: client-side, per request. Tables show the median of 3 runs with min-max.
- Memory: cgroup kernel peak minus idle (or the RSS fallback); RSS cross-check in every row.
- CPU: utime + stime from `/proc` for Gunicorn workers, all PostgreSQL processes,
  and the load client itself (to size its effect on the server).

## Layout

- `accounts/hashers.py` - the Argon2 settings compared
- `accounts/views_before.py` - login as first written (N+1 queries)
- `accounts/views_after.py` - login with a fixed query count
- `accounts/middleware.py` - `X-DB-Queries` / `X-Server-Ms` response headers
- `bench/bench.py`, `bench/hash_cost.py` - the benchmarks
- `docs/full-writeup.md` - the long version of the article
- `shots/` - the terminal screenshots used in the article
