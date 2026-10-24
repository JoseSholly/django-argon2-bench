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


def cg_read(name):
    with open(os.path.join(CGROUP, name)) as f:
        return int(f.read())


def cg_reset_peak():
    with open(os.path.join(CGROUP, "memory.max_usage_in_bytes"), "w") as f:
        f.write("0")  # cgroup v1: resets the peak to the current usage


def start_gunicorn(hasher, workers):
    os.makedirs(CGROUP, exist_ok=True)
    env = dict(os.environ, BENCH_HASHER=hasher, DJANGO_SETTINGS_MODULE="teamhub.settings")
    cmd = (f"echo $$ > {CGROUP}/cgroup.procs && exec {GUNICORN} teamhub.wsgi:application "
           f"--bind 127.0.0.1:{PORT} --workers {workers} --worker-class sync --log-level warning")
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


class Memory:
    """Peak memory above idle: the cgroup's kernel peak, and summed worker RSS
    polled every 2 ms for comparison."""

    def __init__(self, master_pid):
        self.pids = worker_pids(master_pid)
        self.idle = cg_read("memory.usage_in_bytes")
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
        cg_reset_peak()
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join()
        self.peak = cg_read("memory.max_usage_in_bytes")

    def report(self):
        return {"idle_mib": round(self.idle / MIB, 1),
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
    print(f"{'setting':<15} {'params':<17} {'1 login':>10} {'10 at once':>16} "
          f"{'p95 of the 10':>16} {'10 wrong pw':>14} {'peak RAM above idle':>21}", flush=True)
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


def scenario_sweep(args):
    print(f"cpus={os.cpu_count()}  gunicorn workers={args.workers} (sync)  "
          f"sustained logins, logins/s | p95 at each client count", flush=True)
    levels = [1, 2, 4, 8, 16]
    print(f"{'setting':<15} " + "".join(f"{str(c) + (' clients' if c > 1 else ' client'):>20}" for c in levels), flush=True)
    for name, (label, _) in SETTINGS.items():
        set_passwords(name)
        proc = start_gunicorn(name, args.workers)
        cells = []
        try:
            warm_without_hashing(args.workers)
            for clients in levels:
                total = max(40, 12 * clients)
                rps, res = sustained(total, clients, lambda i: login("after", "small", i % 40))
                p95 = pct([r["ms"] for r in res], 95)
                record({"scenario": "sweep", "hasher": name, "clients": clients,
                        "total": total, "rps": round(rps, 2), "p95_ms": round(p95, 1),
                        "p50_ms": round(pct([r["ms"] for r in res], 50), 1)})
                cells.append(f"{rps:5.1f}/s | {p95:5.0f}ms")
        finally:
            stop_gunicorn(proc)
        print(f"{label:<15} " + "".join(f"{c:>20}" for c in cells), flush=True)


def scenario_end_to_end(args):
    print(f"cpus={os.cpu_count()}  gunicorn workers={args.workers} (sync)  {args.n} logins, "
          f"{args.clients} clients, user with 50 projects, {args.runs} runs, median (min-max)",
          flush=True)
    for version, name in (("before", "argon2-default"), ("after", "argon2-default"),
                          ("after", "argon2-owasp")):
        set_passwords(name)
        runs = []
        for run in range(args.runs):
            proc = start_gunicorn(name, args.workers)
            try:
                warm_without_hashing(args.workers)
                with Memory(proc.pid) as mem:
                    rps, res = sustained(args.n, args.clients,
                                         lambda i: login(version, "big", i % 40))
                ms = [r["ms"] for r in res]
                row = {"scenario": "end_to_end", "version": version, "hasher": name,
                       "run": run, "queries": res[0]["queries"], "rps": round(rps, 2),
                       "p50_ms": round(pct(ms, 50), 1), "p95_ms": round(pct(ms, 95), 1),
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


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=["argon2", "sweep", "end_to_end"])
    ap.add_argument("--workers", type=int, default=9)  # (2 x 4 CPUs) + 1
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--bursts", type=int, default=10)
    ap.add_argument("--clients", type=int, default=10)
    args = ap.parse_args()
    {"argon2": scenario_argon2, "sweep": scenario_sweep,
     "end_to_end": scenario_end_to_end}[args.scenario](args)
