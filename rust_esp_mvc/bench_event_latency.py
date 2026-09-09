"""Round-trip latency of the event-based CMD_PING signaling path.

Sends 100 CMD_PING commands through Mem.post_command() (write command ->
SetEvent(cmd_event) -> poll shared.status) and reports mean/p99/max in
microseconds. If the driver's kernel wait loop is really blocking on
ZwWaitForSingleObject instead of polling, this should land well under
1000us (1ms) per round trip.

Run from the repo root: python tools\\rust_esp_mvc\\bench_event_latency.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rust_esp_mvc import legacy_runtime as legacy

SAMPLES = 100
TARGET_US = 1000.0


def percentile(sorted_values, pct):
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, int(round(pct / 100.0 * (len(sorted_values) - 1))))
    return sorted_values[idx]


def main():
    mem = legacy.Mem()
    if not mem.connect():
        print("[BENCH] connect() failed -- is the driver loaded and ready?", flush=True)
        return 1
    if not mem.init_event():
        print("[BENCH] init_event() failed -- could not open or create "
              f"{legacy.CMD_EVENT_NAME!r}", flush=True)
        return 1
    print(f"[BENCH] connected, cmd_event handle=0x{mem._cmd_event:X}", flush=True)

    latencies_us = []
    failures = 0
    for i in range(SAMPLES):
        t0 = time.perf_counter()
        status = mem.post_command(legacy.CMD_PING)
        elapsed_us = (time.perf_counter() - t0) * 1_000_000.0
        if status is None:
            failures += 1
            print(f"[BENCH] sample {i}: TIMEOUT", flush=True)
            continue
        latencies_us.append(elapsed_us)

    mem.cleanup()

    if not latencies_us:
        print("[BENCH] every CMD_PING timed out -- the kernel is not waking "
              "up on the event", flush=True)
        return 1

    latencies_us.sort()
    mean_us = sum(latencies_us) / len(latencies_us)
    p99_us = percentile(latencies_us, 99)
    max_us = latencies_us[-1]

    print(
        f"[BENCH] n={len(latencies_us)} failures={failures} "
        f"mean={mean_us:.1f}us p99={p99_us:.1f}us max={max_us:.1f}us",
        flush=True,
    )
    verdict = "PASS" if p99_us < TARGET_US else "FAIL"
    print(
        f"[BENCH] {verdict}: p99 {'<' if verdict == 'PASS' else '>='} "
        f"{TARGET_US:.0f}us target",
        flush=True,
    )
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
