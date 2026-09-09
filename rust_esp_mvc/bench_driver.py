"""Standalone benchmark of the Mem driver-comm layer (legacy_runtime.Mem).

Originally asked whether the ~15 ms/IOCTL floor was the driver or Python.
It answered that on 2026-08-26 and the answer was *neither*: the cost was
bimodal (~0.01 ms or ~15.1 ms, nothing between) and completely flat in batch
size (n=100 and n=1000 both landed on 15.1 ms), while python-side work never
exceeded 0.6 ms and bench_event_latency.py measured the driver's own round
trip at 30 us. A per-call cost that ignores how much work was asked for is a
missed wakeup, not work -- see legacy_runtime._WAIT_SPIN_SECONDS.

So what this now measures is whether the wait is catching completions:
`slow=N/M` on every line is how many driver waits fell out of Mem._wait's
spin budget and had to sleep. Read that column first. If it is ~0 and the
means are sub-millisecond, the transport is healthy and any remaining tick
cost is real work. If it climbs with batch size, the driver is genuinely
slower than the spin budget for large batches and the budget should rise.

Touches nothing but reads (no writes to game memory) -- safe to run
alongside the real overlay, but simplest run standalone so its numbers are
not itself perturbed by the render thread. Same bootstrap as st_try.py:
requires the driver loaded and RustClient.exe running.

Run with `python tools\\bench_driver.py` from the repo root.
"""

import statistics
import threading
import time

from .legacy_runtime import Mem


def _percentile(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _report(name, samples_ms, slow=None, calls=None):
    """One line per measurement.

    `slow` is how many driver waits fell out of Mem._wait's spin budget and
    had to sleep. It is the number to read first: the cost here is bimodal
    (a caught completion is tens of microseconds, a missed one is a sleep
    away), so a bad mean is always a story about how often we missed, never
    about the driver doing more work. See _WAIT_SPIN_SECONDS.
    """
    s = sorted(samples_ms)
    tail = ""
    if slow is not None and calls:
        tail = f" | slow={slow}/{calls} ({slow / calls * 100.0:.0f}%)"
    print(
        f"{name}: n={len(s)} min={s[0]:.2f}ms p50={_percentile(s, 0.5):.2f}ms "
        f"p90={_percentile(s, 0.9):.2f}ms p99={_percentile(s, 0.99):.2f}ms "
        f"max={s[-1]:.2f}ms mean={statistics.mean(s):.2f}ms{tail}",
        flush=True,
    )


def bench_single_read(m, addr, n=200):
    samples = []
    slow0 = m.io_slow_calls
    for _ in range(n):
        t0 = time.perf_counter()
        m.read(addr, 8)
        samples.append((time.perf_counter() - t0) * 1000.0)
    _report("read(8 bytes)                    ", samples,
            m.io_slow_calls - slow0, n)


def bench_camera_read(m, addr, n=200):
    """The camera sampler's exact read shape: one 64-byte view matrix.

    This is the read whose observed cost [CAM-HZ] reports, so benching it by
    name makes the two numbers directly comparable instead of inferring the
    camera's cost from an 8-byte read.
    """
    samples = []
    slow0 = m.io_slow_calls
    for _ in range(n):
        t0 = time.perf_counter()
        m.read_priority(addr, 64)
        samples.append((time.perf_counter() - t0) * 1000.0)
    _report("read_priority(64 bytes, camera)  ", samples,
            m.io_slow_calls - slow0, n)


def bench_batch(m, addr, count, n=100):
    # Deliberately re-reads a small, fixed set of pages across all iterations
    # rather than fresh addresses each time. This is what makes the plain-vs-
    # python-side split below meaningful (identical driver-side page-walk
    # cost run to run); it is NOT how a real world-entity or bone-composition
    # scan behaves (those touch thousands of distinct, changing pages), so
    # do not read these numbers as "what a real scan costs" -- only as
    # "what a single IOCTL of this size costs in isolation."
    addrs = [addr + (i % 64) * 8 for i in range(count)]
    total_samples = []
    python_samples = []
    slow0 = m.io_slow_calls
    calls0 = m.io_calls
    for _ in range(n):
        io0 = m.io_wait_s
        t0 = time.perf_counter()
        m.batch_u64(addrs, attempts=1)
        total = (time.perf_counter() - t0) * 1000.0
        driver_wait = (m.io_wait_s - io0) * 1000.0
        total_samples.append(total)
        python_samples.append(max(0.0, total - driver_wait))
    _report(f"batch_u64(n={count:<4})   total     ", total_samples,
            m.io_slow_calls - slow0, m.io_calls - calls0)
    _report(f"batch_u64(n={count:<4})   python-side", python_samples)


def bench_contention(m, addr, burst_count=200, small_n=60):
    """Race a slow-lane-style burst against the tick's priority reads.

    Mirrors the real shape: one thread hammering batch_u64() back-to-back
    (like _run_slow_lane's world-entity scan) while another thread wants a
    small, latency-sensitive read (like the tick's chain/pm_list refresh).
    """
    stop = threading.Event()
    burst_addrs = [addr + (i % 64) * 8 for i in range(burst_count)]
    burst_calls = [0]

    def burst_worker():
        while not stop.is_set():
            m.batch_u64(burst_addrs, attempts=1)
            burst_calls[0] += 1

    t = threading.Thread(target=burst_worker, daemon=True)
    t.start()
    time.sleep(0.2)  # let the burst thread ramp up before measuring

    small_addrs = [addr + i * 8 for i in range(2)]

    plain_samples = []
    slow0, calls0 = m.io_slow_calls, m.io_calls
    for _ in range(small_n):
        t0 = time.perf_counter()
        m.batch_u64(small_addrs, attempts=1)
        plain_samples.append((time.perf_counter() - t0) * 1000.0)
    plain_slow, plain_calls = m.io_slow_calls - slow0, m.io_calls - calls0

    priority_samples = []
    slow0, calls0 = m.io_slow_calls, m.io_calls
    for _ in range(small_n):
        t0 = time.perf_counter()
        m.batch_u64_priority(small_addrs, attempts=1)
        priority_samples.append((time.perf_counter() - t0) * 1000.0)

    stop.set()
    t.join(timeout=2.0)

    print(f"(burst thread completed {burst_calls[0]} calls of n={burst_count} during this section)", flush=True)
    _report("under contention: plain batch_u64(n=2)   ", plain_samples,
            plain_slow, plain_calls)
    _report("under contention: batch_u64_priority(n=2)", priority_samples,
            m.io_slow_calls - slow0, m.io_calls - calls0)


def main():
    m = Mem()
    print("[+] Connecting to driver...", flush=True)
    if not m.connect():
        print("[!] Driver not connected -- is it loaded?")
        return
    if not m.init_event():
        # Only the event-based driver build waits on this; a polling build
        # ignores it, so this is not fatal -- see the note in controller.py.
        print("[!] init_event() failed -- continuing without it (fine if "
              "the driver is in polling mode)")
    print("[+] Attaching to RustClient.exe (up to 120s)...", flush=True)
    if not m.attach(timeout=120.0):
        print("[!] RustClient.exe not found")
        return
    m.find_base()
    print(f"[+] base=0x{m.base:X} pid={m.pid}\n", flush=True)

    addr = m.base

    print("--- Baseline, single thread, no contention ---", flush=True)
    bench_single_read(m, addr)
    bench_camera_read(m, addr)
    # The sweep is finer than it used to be, and reaches the sizes the real
    # tick actually issues: ~560 addresses for the player frame batch at 50
    # players, ~4300 for the bone TRS batch (858 slots x 5 words). The old
    # (2, 27, 100, 300, 1000) sweep stopped short of both and had a
    # suspicious knee between 27 and 100 that turned out not to be about
    # batch size at all -- see _WAIT_SPIN_SECONDS.
    for count in (2, 8, 32, 64, 100, 300, 560, 1000, 4300):
        bench_batch(m, addr, count)

    print("\n--- Under simulated slow-lane contention (matches the WE burst shape) ---", flush=True)
    bench_contention(m, addr)


if __name__ == "__main__":
    main()
