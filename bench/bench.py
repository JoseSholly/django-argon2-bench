"""Benchmark Django's Argon2 settings against a real Gunicorn server.

Standard library only. Every run starts a fresh Gunicorn inside its own
memory cgroup, so peak memory comes from the kernel's own counter rather than
from polling. Raw results are appended as JSON lines to results.jsonl; the
terminal gets one readable line per setting.

  python bench/bench.py argon2      # one login vs bursts of 10, 3 runs each
  python bench/bench.py sweep       # sustained load at 1..16 clients
  python bench/bench.py end_to_end  # original vs optimised login code
"""

import argparse
import http.client
import json
import os
import resource
import signal
import statistics
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = os.path.join(ROOT, ".venv", "bin", "python")
GUNICORN = os.path.join(ROOT, ".venv", "bin", "gunicorn")
CGROUP = "/sys/fs/cgroup/memory/bench_gunicorn"  # cgroup v1 memory controller
PORT = 8077
PASSWORD = "correct-horse-battery-staple"
MIB = 1024 * 1024

SETTINGS = {
    # name: (label, parameters shown)
    "argon2-default": ("Django default", "t=2 m=100MiB p=8"),
    "argon2-rfc9106": ("RFC 9106 #2", "t=3 m=64MiB  p=4"),
    "argon2-owasp": ("OWASP minimum", "t=2 m=19MiB  p=1"),
}


# ── Gunicorn in its own memory cgroup ──────────────────────────────


def cgroup_available():
    """True when we may create a cgroup v1 memory group (Linux, root)."""
    if os.environ.get("BENCH_NO_CGROUP"):
        return False
    try:
        os.makedirs(CGROUP, exist_ok=True)
        return os.access(os.path.join(CGROUP, "cgroup.procs"), os.W_OK)
    except OSError:
        return False


USE_CGROUP = cgroup_available()
MEMORY_SOURCE = "cgroup peak" if USE_CGROUP else "summed worker RSS, polled every 2 ms"


def cg_read(name):
    with open(os.path.join(CGROUP, name)) as f:
        return int(f.read())


def cg_reset_peak():
    with open(os.path.join(CGROUP, "memory.max_usage_in_bytes"), "w") as f:
        f.write("0")  # cgroup v1: resets the peak to the current usage


def start_gunicorn(hasher, workers):
    env = dict(os.environ, BENCH_HASHER=hasher, DJANGO_SETTINGS_MODULE="teamhub.settings")
    cmd = (f"exec {GUNICORN} teamhub.wsgi:application --bind 127.0.0.1:{PORT} "
           f"--workers {workers} --worker-class sync --log-level warning")
    if USE_CGROUP:
        cmd = f"echo $$ > {CGROUP}/cgroup.procs && {cmd}"
    proc = subprocess.Popen(["sh", "-c", cmd], cwd=ROOT, env=env)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=1)
            c.request("GET", "/nope/")
            c.getresponse().read()
            break
        except OSError:
            time.sleep(0.2)
    time.sleep(1.0)  # let every worker finish booting
    return proc


def stop_gunicorn(proc):
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=30)


def worker_pids(master_pid):
    out = subprocess.run(["pgrep", "-P", str(master_pid)], capture_output=True, text=True)
    return [int(p) for p in out.stdout.split()]


def rss_bytes(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except FileNotFoundError:
        pass
    return 0


def cpu_seconds(pids):
    """utime + stime of the given processes (all their threads), from /proc."""
    tick = os.sysconf("SC_CLK_TCK")
    total = 0
    for pid in pids:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        total += int(fields[11]) + int(fields[12])  # utime, stime
    return total / tick


def postgres_pids():
    out = subprocess.run(["pgrep", "-u", "postgres"], capture_output=True, text=True)
    return [int(p) for p in out.stdout.split()]


def db_cpu_seconds():
    """CPU of every PostgreSQL process on this machine (same-host database)."""
    total = 0.0
    for pid in postgres_pids():
        try:
            total += cpu_seconds([pid])
        except (FileNotFoundError, ProcessLookupError):
            pass  # a backend that exited mid-measurement
    return total


def client_cpu_seconds():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


class Memory:
    """Peak memory above idle: the cgroup's kernel peak when available, and
    summed worker RSS polled every 2 ms (the fallback, and a cross-check)."""

    def __init__(self, master_pid):
        self.pids = worker_pids(master_pid)
        self.idle = cg_read("memory.usage_in_bytes") if USE_CGROUP else 0
        self.rss_idle = self.rss_total()
        self.rss_peak = self.rss_idle
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._poll, daemon=True)

    def rss_total(self):
        return sum(rss_bytes(p) for p in self.pids)

    def _poll(self):
        while not self._stop.is_set():
            self.rss_peak = max(self.rss_peak, self.rss_total())
            time.sleep(0.002)

    def __enter__(self):
        if USE_CGROUP:
            cg_reset_peak()
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join()
        if USE_CGROUP:
            self.peak = cg_read("memory.max_usage_in_bytes")
        else:
            self.idle, self.peak = self.rss_idle, self.rss_peak

    def report(self):
        return {"memory_source": MEMORY_SOURCE,
                "idle_mib": round(self.idle / MIB, 1),
                "peak_extra_mib": round((self.peak - self.idle) / MIB, 1),
                "rss_peak_extra_mib": round((self.rss_peak - self.rss_idle) / MIB, 1)}


# ── HTTP ────────────────────────────────────────────────────────────


def post(path, body, expect=200):
    data = json.dumps(body)
    started = time.perf_counter()
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=120)
    c.request("POST", path, body=data, headers={"Content-Type": "application/json"})
    r = c.getresponse()
    payload = r.read()
    elapsed = (time.perf_counter() - started) * 1000
    c.close()
    if r.status != expect:  # any unexpected status aborts the run
        raise RuntimeError(f"{path} -> {r.status}: {payload[:200]!r}")
    return {"ms": elapsed, "queries": int(r.getheader("X-DB-Queries")), "json": payload}


def login(version, shape, i, password=PASSWORD, expect=200):
    return post(f"/{version}/login/",
                {"email": f"{shape}_{i}@example.com", "password": password}, expect)


def burst(n, fn):
    """n requests released at the same instant by a barrier.
    Returns (wall ms until the last one finished, per-request results)."""
    barrier = threading.Barrier(n + 1)

    def task(i):
        barrier.wait()
        return fn(i)

    with ThreadPoolExecutor(n) as pool:
        futures = [pool.submit(task, i) for i in range(n)]
        time.sleep(0.05)  # every thread is now parked at the barrier
        started = time.perf_counter()
        barrier.wait()
        results = [f.result() for f in futures]
    return (time.perf_counter() - started) * 1000, results


def sustained(total, clients, fn):
    started = time.perf_counter()
    with ThreadPoolExecutor(clients) as pool:
        results = list(pool.map(fn, range(total)))
    return total / (time.perf_counter() - started), results


def pct(values, p):
    values = sorted(values)
    k = (len(values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def set_passwords(hasher):
    subprocess.run([PY, "manage.py", "set_passwords"], cwd=ROOT, check=True,
                   env=dict(os.environ, BENCH_HASHER=hasher), capture_output=True)


def warm_without_hashing(workers):
    """Warm every worker through the refresh endpoint, which never hashes a
    password, so the idle memory reading excludes Argon2's buffers."""
    out = os.path.join(HERE, "tokens.json")
    subprocess.run([PY, "manage.py", "mint_refresh", str(workers * 4), out], cwd=ROOT,
                   check=True, env=dict(os.environ, BENCH_HASHER="md5"), capture_output=True)
    with open(out) as f:
        for t in json.load(f):
            post("/after/refresh/", {"refresh": t})


def record(row):
    with open(os.path.join(HERE, "results.jsonl"), "a") as f:
        f.write(json.dumps(row) + "\n")


def med_range(values, fmt="{:.0f}"):
    return f"{fmt.format(statistics.median(values))} ({fmt.format(min(values))}-{fmt.format(max(values))})"


# ── Scenarios ───────────────────────────────────────────────────────


def scenario_argon2(args):
    print(f"cpus={os.cpu_count()}  gunicorn workers={args.workers} (sync)  "
          f"{args.runs} runs per setting, median (min-max)", flush=True)
    print(f"each run: {args.n} single logins, {args.bursts} bursts of 10, 3 bursts of 10 wrong "
          f"passwords; memory = {MEMORY_SOURCE}", flush=True)
    print(f"{'setting':<15} {'params':<17} {'1 login':>10} {'10 at once':>16} "
          f"{'p95 in bursts':>16} {'10 wrong pw':>14} {'peak RAM above idle':>21}", flush=True)
    for name, (label, params) in SETTINGS.items():
        set_passwords(name)
        runs = []
        for run in range(args.runs):
            proc = start_gunicorn(name, args.workers)
            try:
                warm_without_hashing(args.workers)
                with Memory(proc.pid) as mem:
                    single = [login("after", "small", i % 40)["ms"] for i in range(args.n)]
                    walls, lat = [], []
                    for b in range(args.bursts):
                        time.sleep(0.3)
                        wall, res = burst(10, lambda i: login("after", "small", i))
                        walls.append(wall)
                        lat += [r["ms"] for r in res]
                    bad = [burst(10, lambda i: login("after", "small", i, "wrong-password", 400))[0]
                           for _ in range(3)]
                row = {"scenario": "argon2", "hasher": name, "run": run,
                       "single_p50_ms": round(pct(single, 50), 1),
                       "burst_wall_ms_median": round(statistics.median(walls), 1),
                       "burst_request_p95_ms": round(pct(lat, 95), 1),
                       "failed_burst_wall_ms_median": round(statistics.median(bad), 1),
                       **mem.report()}
                record(row)
                runs.append(row)
            finally:
                stop_gunicorn(proc)
        col = lambda k: [r[k] for r in runs]
        print(f"{label:<15} {params:<17} "
              f"{med_range(col('single_p50_ms')) + 'ms':>14} "
              f"{med_range(col('burst_wall_ms_median')) + 'ms':>16} "
              f"{med_range(col('burst_request_p95_ms')) + 'ms':>16} "
              f"{med_range(col('failed_burst_wall_ms_median')) + 'ms':>16} "
              f"{med_range(col('peak_extra_mib')) + 'MiB':>20}", flush=True)
    print(f"idle Gunicorn ({args.workers} workers): "
          f"{statistics.median(r['idle_mib'] for r in runs):.0f}MiB", flush=True)


def print_sweep(points_by_setting, runs, workers):
    cores = os.cpu_count()
    print(f"cpus={cores}  gunicorn workers={workers} (sync)  sustained logins, "
          f"{runs} runs per point, median (min-max)", flush=True)
    print(f"{'setting':<15} {'clients':>7}  {'logins/s':<21} {'p95':<20} "
          f"{'gunicorn CPU/login':<20} {'postgres CPU/login':<20} {'load client CPU':<16}",
          flush=True)
    for name, points in points_by_setting.items():
        label = SETTINGS[name][0]
        for clients in sorted(points):
            col = lambda k: [r[k] for r in points[clients]]
            print(f"{label:<15} {clients:>7}  {med_range(col('rps'), '{:.1f}') + '/s':<21} "
                  f"{med_range(col('p95_ms')) + 'ms':<20} "
                  f"{med_range(col('server_cpu_ms_per_login')) + 'ms':<20} "
                  f"{med_range(col('db_cpu_ms_per_login')) + 'ms':<20} "
                  f"{med_range(col('client_cpu_pct_of_machine'), '{:.1f}') + '%':<16}", flush=True)


def scenario_sweep(args):
    if args.from_results:
        rows = [json.loads(l) for l in open(os.path.join(HERE, "results.jsonl"))]
        found = {}
        for r in rows:
            if r["scenario"] == "sweep":
                found.setdefault(r["hasher"], {}).setdefault(r["clients"], []).append(r)
        print_sweep({n: found[n] for n in SETTINGS if n in found}, args.runs, args.workers)
        return
    levels = [1, 2, 4, 8, 16]
    results = {}
    for name in SETTINGS:
        set_passwords(name)
        points = {c: [] for c in levels}
        for run in range(args.runs):
            proc = start_gunicorn(name, args.workers)
            try:
                warm_without_hashing(args.workers)
                pids = worker_pids(proc.pid)
                for clients in levels:
                    total = max(40, 12 * clients)
                    srv0, db0, cli0 = cpu_seconds(pids), db_cpu_seconds(), client_cpu_seconds()
                    t0 = time.perf_counter()
                    rps, res = sustained(total, clients, lambda i: login("after", "small", i % 40))
                    wall = time.perf_counter() - t0
                    srv = cpu_seconds(pids) - srv0
                    db = db_cpu_seconds() - db0
                    cli = client_cpu_seconds() - cli0
                    row = {"scenario": "sweep", "hasher": name, "run": run, "clients": clients,
                           "total": total, "rps": round(rps, 2),
                           "p50_ms": round(pct([r["ms"] for r in res], 50), 1),
                           "p95_ms": round(pct([r["ms"] for r in res], 95), 1),
                           "server_cpu_ms_per_login": round(srv / total * 1000, 1),
                           "db_cpu_ms_per_login": round(db / total * 1000, 1),
                           "client_cpu_pct_of_machine":
                               round(cli / (wall * os.cpu_count()) * 100, 1)}
                    record(row)
                    points[clients].append(row)
            finally:
                stop_gunicorn(proc)
        results[name] = points
    print_sweep(results, args.runs, args.workers)


def scenario_end_to_end(args):
    print(f"cpus={os.cpu_count()}  gunicorn workers={args.workers} (sync)  {args.n} logins, "
          f"{args.clients} clients, user with 50 projects, {args.runs} runs, median (min-max)",
          flush=True)
    print(f"memory = {MEMORY_SOURCE}", flush=True)
    for version, name in (("before", "argon2-default"), ("after", "argon2-default"),
                          ("after", "argon2-owasp")):
        set_passwords(name)
        runs = []
        for run in range(args.runs):
            proc = start_gunicorn(name, args.workers)
            try:
                warm_without_hashing(args.workers)
                pids = worker_pids(proc.pid)
                with Memory(proc.pid) as mem:
                    srv0, db0, cli0 = cpu_seconds(pids), db_cpu_seconds(), client_cpu_seconds()
                    t0 = time.perf_counter()
                    rps, res = sustained(args.n, args.clients,
                                         lambda i: login(version, "big", i % 40))
                    wall = time.perf_counter() - t0
                    srv, cli = cpu_seconds(pids) - srv0, client_cpu_seconds() - cli0
                    db = db_cpu_seconds() - db0
                ms = [r["ms"] for r in res]
                row = {"scenario": "end_to_end", "version": version, "hasher": name,
                       "run": run, "queries": res[0]["queries"], "rps": round(rps, 2),
                       "p50_ms": round(pct(ms, 50), 1), "p95_ms": round(pct(ms, 95), 1),
                       "server_cpu_ms_per_login": round(srv / args.n * 1000, 1),
                       "db_cpu_ms_per_login": round(db / args.n * 1000, 1),
                       "client_cpu_pct_of_machine": round(cli / (wall * os.cpu_count()) * 100, 1),
                       **mem.report()}
                record(row)
                runs.append(row)
            finally:
                stop_gunicorn(proc)
        col = lambda k: [r[k] for r in runs]
        label = f"{version} code, {SETTINGS[name][0]}"
        print(f"{label:<29} queries={runs[0]['queries']:<4} "
              f"logins/s={med_range(col('rps'), '{:.1f}'):<17} "
              f"p50={med_range(col('p50_ms')) + 'ms':<16} "
              f"p95={med_range(col('p95_ms')) + 'ms':<19} "
              f"peak RAM above idle={med_range(col('peak_extra_mib')) + 'MiB'}", flush=True)
        print(f"{'':<29} gunicorn CPU/login={med_range(col('server_cpu_ms_per_login'))}ms  "
              f"postgres CPU/login={med_range(col('db_cpu_ms_per_login'))}ms  "
              f"load client CPU={med_range(col('client_cpu_pct_of_machine'), '{:.1f}')}% of machine",
              flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=["argon2", "sweep", "end_to_end"])
    ap.add_argument("--workers", type=int, default=9)  # (2 x 4 CPUs) + 1
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--bursts", type=int, default=10)
    ap.add_argument("--clients", type=int, default=10)
    ap.add_argument("--from-results", action="store_true",
                    help="sweep only: print the table from results.jsonl without running")
    args = ap.parse_args()
    {"argon2": scenario_argon2, "sweep": scenario_sweep,
     "end_to_end": scenario_end_to_end}[args.scenario](args)
