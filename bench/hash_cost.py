"""CPU cost of one Argon2id hash, measured in-process on an idle machine.

For each setting: wall time, CPU time (all threads), and how many extra OS
threads argon2-cffi started during the hash. Median of 15 hashes.

  python bench/hash_cost.py
"""

import os
import resource
import statistics
import threading
import time

from argon2.low_level import Type, hash_secret_raw

SETTINGS = [
    # label, time_cost, memory_cost (KiB), parallelism
    ("Django default", 2, 100 * 1024, 8),
    ("  same, 4 lanes", 2, 100 * 1024, 4),
    ("  same, 2 lanes", 2, 100 * 1024, 2),
    ("  same, 1 lane", 2, 100 * 1024, 1),
    ("RFC 9106 #2", 3, 64 * 1024, 4),
    ("OWASP minimum", 2, 19 * 1024, 1),
]


def os_threads():
    return len(os.listdir("/proc/self/task"))


def one_hash(t, m, p):
    peak = [0]
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            peak[0] = max(peak[0], os_threads())
            time.sleep(0.001)

    watcher = threading.Thread(target=watch)
    watcher.start()
    time.sleep(0.01)
    before = os_threads()  # this thread + the watcher
    r0 = resource.getrusage(resource.RUSAGE_SELF)
    w0 = time.perf_counter()
    hash_secret_raw(b"correct-horse-battery-staple", os.urandom(16), t, m, p, 32, Type.ID)
    wall = time.perf_counter() - w0
    r1 = resource.getrusage(resource.RUSAGE_SELF)
    stop.set()
    watcher.join()
    cpu = (r1.ru_utime + r1.ru_stime) - (r0.ru_utime + r0.ru_stime)
    return wall * 1000, cpu * 1000, peak[0] - before


if __name__ == "__main__":
    cores = os.cpu_count()
    print(f"cpus={cores}  one hash at a time, median of 15  "
          f"(ceiling = {cores} cores / CPU per hash)", flush=True)
    for label, t, m, p in SETTINGS:
        runs = [one_hash(t, m, p) for _ in range(15)]
        wall = statistics.median(r[0] for r in runs)
        cpu = statistics.median(r[1] for r in runs)
        threads = max(r[2] for r in runs)
        print(f"{label:<16} t={t} m={m // 1024:>3}MiB p={p}   wall={wall:6.1f}ms   "
              f"cpu={cpu:6.1f}ms   extra threads={threads}   "
              f"ceiling={cores / (cpu / 1000):5.1f} hashes/s", flush=True)
