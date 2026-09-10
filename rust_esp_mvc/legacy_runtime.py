import contextlib
import ctypes
import ctypes.wintypes as wt
import struct
import time
import sys
import math
import threading
try:
    from .item_names import ITEM_DISPLAY_NAMES
except ImportError:
    try:
        from item_names import ITEM_DISPLAY_NAMES
    except ImportError:
        ITEM_DISPLAY_NAMES = {}
try:
    from _section_name import SECTION_NAME
except ImportError:
    try:
        from ._section_name import SECTION_NAME
    except ImportError:
        SECTION_NAME = ".data"


GUI_IMPORT_ERROR = None
try:
    import glfw
    import OpenGL.GL as gl
    from imgui_bundle import imgui, imgui_ctx
    from imgui_bundle.python_backends.glfw_backend import GlfwRenderer
except ImportError as exc:
    GUI_IMPORT_ERROR = exc
    glfw = None
    gl = None
    imgui = None
    imgui_ctx = None
    GlfwRenderer = None

# ---------------------------------------------------------------------------
# CommDriver shared memory  (copié 1:1 de esp.py Arc Raiders)
# ---------------------------------------------------------------------------
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi
kernel32.OpenFileMappingW.restype = ctypes.c_void_p
kernel32.MapViewOfFile.restype = ctypes.c_void_p
kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
kernel32.UnmapViewOfFile.restype = wt.BOOL
kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t
kernel32.Module32FirstW.argtypes = [wt.HANDLE, ctypes.c_void_p]
kernel32.Module32FirstW.restype = wt.BOOL
kernel32.Module32NextW.argtypes = [wt.HANDLE, ctypes.c_void_p]
kernel32.Module32NextW.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.OpenEventW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
kernel32.OpenEventW.restype = wt.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
kernel32.CreateEventW.restype = wt.HANDLE
kernel32.SetEvent.argtypes = [wt.HANDLE]
kernel32.SetEvent.restype = wt.BOOL
kernel32.GetLastError.argtypes = []
kernel32.GetLastError.restype = wt.DWORD
psapi.GetMappedFileNameW.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.LPWSTR, wt.DWORD]
psapi.GetMappedFileNameW.restype = wt.DWORD

TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MEM_COMMIT = 0x1000
MEM_IMAGE = 0x1000000
MAX_USER_ADDR = 0x7FFFFFFFFFFF
COMM_DATA_SIZE = 0x40000
EVENT_ALL_ACCESS = 0x1F0003
# The client only ever calls SetEvent() on this handle -- it never waits on
# it, the kernel does -- so EVENT_MODIFY_STATE is the only right it needs.
# Kernel-created named events commonly get a DACL that grants that to
# Authenticated Users but denies the STANDARD_RIGHTS_REQUIRED bits (WRITE_DAC/
# WRITE_OWNER/DELETE) bundled into EVENT_ALL_ACCESS, so asking for the full
# set fails with ERROR_ACCESS_DENIED (5) even though SetEvent would work
# fine. Always request the minimum right actually used.
EVENT_MODIFY_STATE = 0x0002
# StealthDriver's kernel wait loop blocks on ZwWaitForSingleObject against
# this named event instead of polling shared.command; post_command() must
# SetEvent() it after writing a new command or the kernel never wakes.
CMD_EVENT_NAME = (
    "Global\\{6E8A4D13-2F97-4C65-A1B0-9D73E5C8422F}-CMD-EVENT"
)
CMD_PING = 0xFF

# Driver-lock priority tiers, lowest number wins. A caller in tier N must
# yield to any tier < N that has currently claimed the driver (see
# Mem._claim_tier/_yield_to_tier). Replaces the old bespoke
# _camera_idle/_tick_idle Event pairs -- see the 2026-08-26 architecture
# review: those were copy-pasted per new priority need (trap #54 for the
# camera, then the tick), the copy for the tick never actually deferred to
# the camera (a real bug, fixed by going through this single mechanism
# instead of two independent ones), and half of each pair
# (_camera_waiting/_tick_waiting) was set/cleared but never read by
# anything -- dead state left behind by an earlier, incomplete design.
# Adding a new tier is one constant, not a new Event pair and a new method.
TIER_CAMERA = 0
TIER_TICK = 1
_PRIORITY_TIER_COUNT = 2


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("th32ModuleID", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("GlblcntUsage", wt.DWORD),
        ("ProccntUsage", wt.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_ubyte)),
        ("modBaseSize", wt.DWORD),
        ("hModule", wt.HMODULE),
        ("szModule", wt.WCHAR * 256),
        ("szExePath", wt.WCHAR * 260),
    ]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("PartitionId", wt.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


class COMM_SHARED(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("ready", ctypes.c_int32),
        ("command", ctypes.c_int32),
        ("status", ctypes.c_int32),
        ("process_name", ctypes.c_char * 260),
        ("pid", ctypes.c_uint64),
        ("cr3", ctypes.c_uint64),
        ("address", ctypes.c_uint64),
        ("size", ctypes.c_uint64),
        ("peb_address", ctypes.c_uint64),
        ("image_base", ctypes.c_uint64),
        ("alloc_size_req", ctypes.c_uint64),
        ("alloc_address", ctypes.c_uint64),
        ("protect_flags", ctypes.c_uint32),
        ("_pad", ctypes.c_uint32),
        ("entry_point", ctypes.c_uint64),
        ("target_dx", ctypes.c_int32),
        ("target_dy", ctypes.c_int32),
        ("stiffness", ctypes.c_uint32),
        ("button_flags", ctypes.c_uint16),
        ("data", ctypes.c_ubyte * COMM_DATA_SIZE),
    ]


# Precomputed XOR translation tables for all 256 possible byte keys
XOR_TABLES = [bytes([i ^ key for i in range(256)]) for key in range(256)]


_TIMER_RESOLUTION_RAISED = False


def _measure_sleep_granularity(request=0.0002, samples=12):
    """Mean wall time a `request`-second sleep actually costs, in ms."""
    total = 0.0
    for _ in range(samples):
        start = time.perf_counter()
        time.sleep(request)
        total += time.perf_counter() - start
    return total * 1000.0 / samples


def raise_timer_resolution():
    """Make this process able to react in under a millisecond, and say so.

    Two process-wide knobs, both defaulted for throughput, both sitting on
    this program's critical path:

    **The Windows timer tick.** Default 15.6 ms, so any `time.sleep()` that
    reaches the kernel is rounded up to it. `NtSetTimerResolution` sets it
    system-wide, `timeBeginPeriod` is the documented per-process request
    (Win10 2004+ scopes it to the caller); both are asked for and the
    before/after cost of a short sleep is printed, so the next run says
    plainly whether it took.

    **CPython's GIL switch interval.** Default 5 ms. That is a floor on how
    long a thread waits to run again after releasing the GIL, and this
    process has three threads that all need to react fast (worker tick,
    camera sampler, render loop). Measured here, with two CPU-bound Python
    threads and a driver stand-in that answers in well under a millisecond:
    a round trip cost p50=107ms mean=116ms at the 5 ms default, and
    p50=0.70ms mean=0.73ms at 0.5 ms. Same code, same load -- the entire
    difference was how long a thread had to wait for the interpreter.

    This is where the flat "~15 ms per IOCTL" that every earlier note in this
    file tried to explain as a driver or timer-tick cost actually came from.
    It was never the driver: bench_event_latency.py measures the round trip
    at 30us mean / 34us p99, and bench_driver.py shows the in-process cost is
    *bimodal* (~0.01 ms or ~15 ms, nothing between) and flat in batch size --
    n=100 and n=1000 both land on 15.1 ms. A cost that ignores how much work
    was asked for is a missed wakeup, not work.
    """
    global _TIMER_RESOLUTION_RAISED
    if _TIMER_RESOLUTION_RAISED:
        return
    _TIMER_RESOLUTION_RAISED = True
    # Same class of problem, same one-shot place to fix it: the Windows timer
    # tick bounds how fast a *sleep* can end, and CPython's switch interval
    # bounds how fast a *thread* can react to what another thread did. Both
    # are process-wide, both default to values chosen for throughput, and
    # both sit directly on this program's critical path. See
    # GIL_SWITCH_INTERVAL for the measurement.
    switch_before = sys.getswitchinterval()
    sys.setswitchinterval(GIL_SWITCH_INTERVAL)
    before = _measure_sleep_granularity()
    achieved_ns = 0
    try:
        ntdll = ctypes.WinDLL('ntdll')
        current = ctypes.c_ulong(0)
        # NtSetTimerResolution(DesiredResolution in 100ns units, Set, Current)
        ntdll.NtSetTimerResolution(10000, True, ctypes.byref(current))
        achieved_ns = current.value * 100
    except Exception as exc:
        print(f"[TIMER] NtSetTimerResolution failed: {exc}", flush=True)
    try:
        ctypes.WinDLL('winmm').timeBeginPeriod(1)
    except Exception as exc:
        print(f"[TIMER] timeBeginPeriod failed: {exc}", flush=True)
    after = _measure_sleep_granularity()
    achieved = f"{achieved_ns / 1e6:.3f}ms" if achieved_ns else "unknown"
    print(
        f"[TIMER] resolution={achieved} | sleep(0.2ms) costs "
        f"{before:.2f}ms -> {after:.2f}ms | gil switch "
        f"{switch_before * 1000.0:.2f}ms -> "
        f"{sys.getswitchinterval() * 1000.0:.2f}ms",
        flush=True,
    )


class Mem:
    

    def __init__(self):
        # Do this before the first IOCTL: the wait loop's backoff sleep is
        # quantised to the system timer tick, which is 15.6 ms by default.
        raise_timer_resolution()
        # Per-tick driver accounting, reset by the worker each tick. Without
        # this it is impossible to tell "the driver is slow" from "we are
        # issuing far more IOCTLs than we think".
        # Per-THREAD, not global. Three threads issue IOCTLs on this object --
        # the worker tick, CameraSampler, and the background pm->bp resolver --
        # and a shared counter mixes them: [POS-DBG] reported `bones=6io` on a
        # tick where the bone phase issues exactly one batch and `pread=0`
        # proved the parent cache had not read anything. Those five extra calls
        # were the background resolver's, attributed to whichever phase
        # happened to be running. An instrument that reports another thread's
        # work as yours is worse than none: every phase number derived from it
        # is inflated by an unknown amount.
        self._io_tls = threading.local()
        self.shared = None
        self._mapping_view = 0
        self._mapping_handle = 0
        self._cmd_event = 0
        self._cmd_event_warned = False
        self.pid = 0
        self.cr3 = 0
        self.base = 0
        self._drv_lock = threading.Lock()
        # One idle-Event per priority tier (TIER_CAMERA, TIER_TICK, ...),
        # each set == "nobody in this tier is mid-transaction". A caller
        # below a tier waits on it before touching the driver, so the tier
        # actually gets priority instead of merely announcing that it wants
        # it -- `time.sleep(0)` alone never worked: it is a bare GIL yield
        # that does not hold anyone back, and a Python Lock is not FIFO, so
        # the camera lost races to the slow lane's 17-36-IOCTL bursts
        # repeatedly (`slow=36io/1611ms`) before trap #54, and the tick's
        # once-a-second chain/pm_list refresh lost the same race to the same
        # bursts before this session's tick-priority work (chain=/pm_list=
        # inflating from ~15ms to 28-45ms with World Entities on, measured
        # 2026-08-25).
        self._tier_idle = [threading.Event() for _ in range(_PRIORITY_TIER_COUNT)]
        for _ev in self._tier_idle:
            _ev.set()
        # Deliberately NOT thread-local: this describes the driver's answer
        # latency, which is the same whichever thread asked. Races between
        # the three threads updating it are harmless -- it is a tuning
        # heuristic converging on one shared property, and a float
        # assignment cannot tear under the GIL.
        self.spin_budget = _WAIT_SPIN_START

    @contextlib.contextmanager
    def _claim_tier(self, tier, timeout=0.05):
        """Claim `tier` for one driver transaction: yield to every strictly
        higher tier first (so priority composes across tiers instead of
        each one only knowing about the specific tier below it -- the old
        _camera_idle/_tick_idle pair never made the tick's priority reads
        defer to the camera, which was a real bug this fixes), then
        announce this tier busy so lower tiers back off until it releases.
        """
        self._yield_to_higher_tiers(tier, timeout)
        idle = self._tier_idle[tier]
        idle.clear()
        try:
            yield
        finally:
            idle.set()

    def _yield_to_higher_tiers(self, tier, timeout=0.05):
        """Block briefly so any pending claim in a strictly higher tier (a
        lower tier number) than `tier` goes first. Bounded: a lost or
        never-cleared flag costs one timeout per higher tier, never a
        deadlock. Used by _claim_tier before it claims its own tier.
        """
        for t in range(tier):
            idle = self._tier_idle[t]
            if not idle.is_set():
                idle.wait(timeout)

    def _yield_to_this_tier(self, tier, timeout=0.05):
        """Block briefly so a pending claim on exactly this tier goes first.

        Unlike _yield_to_higher_tiers, this checks only `tier` itself --
        for an unprioritized caller (read()/batch_u64()) that must defer to
        each tier individually and in order, not for a tier deferring to
        the ones above it.
        """
        idle = self._tier_idle[tier]
        if not idle.is_set():
            idle.wait(timeout)

    def _yield_to_camera(self, timeout=0.05):
        """Block briefly so a pending camera read goes first.

        The camera path itself must not call this.
        """
        self._yield_to_this_tier(TIER_CAMERA, timeout)

    def _yield_to_tick(self, timeout=0.05):
        """Block briefly so the tick's pending priority read goes first.

        The tick's priority path itself must not call this.
        """
        self._yield_to_this_tier(TIER_TICK, timeout)

    @property
    def io_calls(self):
        return getattr(self._io_tls, 'calls', 0)

    @io_calls.setter
    def io_calls(self, value):
        self._io_tls.calls = value

    @property
    def io_wait_s(self):
        return getattr(self._io_tls, 'wait_s', 0.0)

    @io_wait_s.setter
    def io_wait_s(self, value):
        self._io_tls.wait_s = value

    # d_batch/d_bones in [POS-PROFILE] cost 23-37ms for what should be one
    # ~15ms IOCTL -- io_wait_s only starts timing after _drv_lock is already
    # held, so it cannot see time spent in _yield_to_camera/_yield_to_tick or
    # waiting to acquire the lock itself. These two TLS counters isolate that
    # previously-invisible gap instead of assuming it is lock contention.
    @property
    def yield_wait_s(self):
        return getattr(self._io_tls, 'yield_wait_s', 0.0)

    @yield_wait_s.setter
    def yield_wait_s(self, value):
        self._io_tls.yield_wait_s = value

    @property
    def lock_wait_s(self):
        return getattr(self._io_tls, 'lock_wait_s', 0.0)

    @lock_wait_s.setter
    def lock_wait_s(self, value):
        self._io_tls.lock_wait_s = value

    # How many of this thread's io_calls fell out of _wait's spin budget and
    # had to sleep. The mean IOCTL time hides this: the cost is bimodal (a
    # caught completion is ~0.03ms, a missed one is a sleep away), so the
    # only actionable number is *how often* we miss. ~0 means the spin is
    # catching completions; a large fraction means the answer simply is not
    # there yet and no client-side wait can help. See _WAIT_SPIN_MIN.
    @property
    def io_slow_calls(self):
        return getattr(self._io_tls, 'slow_calls', 0)

    @io_slow_calls.setter
    def io_slow_calls(self, value):
        self._io_tls.slow_calls = value

    def connect(self):
        h = kernel32.OpenFileMappingW(0xF001F, False, SECTION_NAME)
        if not h:
            return False
        v = kernel32.MapViewOfFile(h, 0xF001F, 0, 0, ctypes.sizeof(COMM_SHARED))
        if not v:
            kernel32.CloseHandle(h)
            return False
        self._mapping_handle = h
        self._mapping_view = v
        self.shared = COMM_SHARED.from_address(v)
        if self.shared.ready != 1:
            kernel32.UnmapViewOfFile(v)
            kernel32.CloseHandle(h)
            self.shared = None
            self._mapping_view = 0
            self._mapping_handle = 0
            return False
        return True

    def init_event(self):
        """Open (or create) StealthDriver's named command-wake event.

        The kernel side now blocks in ZwWaitForSingleObject on this event
        instead of polling shared.command in a loop. post_command() must
        SetEvent() it after writing a new command, or the kernel never
        wakes up and every call hangs until its own timeout.
        """
        handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, CMD_EVENT_NAME)
        if not handle:
            open_err = kernel32.GetLastError()
            handle = kernel32.CreateEventW(None, False, False, CMD_EVENT_NAME)
            if not handle:
                create_err = kernel32.GetLastError()
                print(
                    f"[EVENT-DBG] OpenEventW(0x{EVENT_MODIFY_STATE:X}, {CMD_EVENT_NAME!r}) "
                    f"failed, GetLastError={open_err}; "
                    f"CreateEventW fallback also failed, GetLastError={create_err}",
                    flush=True,
                )
        self._cmd_event = handle or 0
        return bool(handle)

    def post_command(self, cmd_id, timeout=5.0):
        """Write cmd_id, wake the kernel, and poll for completion.

        Same completion convention as the rest of this class (_wait(),
        read(), attach()): the driver signals "done" by clearing
        shared.command back to 0, with shared.status left as the result/
        error code (0 == success). Confirmed empirically 2026-08-26: after
        an event-based CMD_PING, command read back 0 while status stayed 0
        -- the driver finished almost instantly, this method was just
        watching the wrong field.

        Returns shared.status (0 on success) once shared.command clears, or
        None if init_event() was never called, SetEvent() failed, or the
        driver did not clear command within `timeout` seconds.
        """
        if not self._cmd_event:
            return None
        self.shared.status = 0
        self.shared.command = cmd_id
        if not kernel32.SetEvent(self._cmd_event):
            print(
                f"[POST-DBG] SetEvent failed, GetLastError={kernel32.GetLastError()}",
                flush=True,
            )
            return None
        # Same two-phase wait as _wait(): spin first so what this reports is
        # the driver's round trip and not the scheduler's opinion of it, then
        # sleep so a dead driver does not pin a core for `timeout` seconds.
        # This method exists to *measure* the driver (bench_event_latency),
        # so it must not be the thing adding the latency.
        start = time.perf_counter()
        spin_until = start + self.spin_budget
        deadline = start + timeout
        while time.perf_counter() < spin_until:
            if self.shared.command == 0:
                return self.shared.status
        while time.perf_counter() < deadline:
            if self.shared.command == 0:
                return self.shared.status
            time.sleep(_WAIT_BACKOFF_SECONDS)
        print(
            f"[POST-DBG] timeout waiting on command; final command="
            f"{self.shared.command} status={self.shared.status}",
            flush=True,
        )
        return None

    def cleanup(self):
        """Release the command-wake event handle."""
        if self._cmd_event:
            kernel32.CloseHandle(self._cmd_event)
            self._cmd_event = 0

    def _signal_command(self):
        """Wake the kernel's ZwWaitForSingleObject after writing a command.

        Every shared.command writer must call this -- there is no more
        kernel-side polling fallback, so a missed signal hangs that call
        until its own timeout. Warns once (not every call) if init_event()
        was never called, since that would otherwise silently hang every
        single driver transaction.
        """
        if self._cmd_event:
            kernel32.SetEvent(self._cmd_event)
        elif not self._cmd_event_warned:
            self._cmd_event_warned = True
            print(
                "[POST-DBG] _signal_command: no cmd_event -- init_event() "
                "was never called or failed; every command will hang until "
                "its own timeout",
                flush=True,
            )

    def _note_answer_latency(self, elapsed, caught):
        """Steer the spin budget towards the driver's real answer latency.

        Doing this on the *miss* path too is the whole point. Growing only
        from completions the spin already caught is a one-way ratchet: once
        the budget decays to the floor it can never see anything slower than
        the floor again, so a driver answering in 200us would be missed
        forever after one unlucky stretch.
        """
        if caught:
            # Precise: nothing slept, so `elapsed` really is the driver.
            want = min(_WAIT_SPIN_MAX, max(_WAIT_SPIN_MIN, elapsed * 2.0))
        elif elapsed <= _WAIT_SPIN_PROBE_LIMIT:
            # Imprecise -- see _WAIT_SPIN_PROBE_LIMIT. All we know is that
            # the answer came back soon, so probe upward instead of trusting
            # a number the sleep granularity set.
            want = min(_WAIT_SPIN_MAX, max(
                _WAIT_SPIN_MIN, self.spin_budget * 2.0, elapsed * 2.0,
            ))
        else:
            self._decay_spin_budget()
            return
        if want > self.spin_budget:
            self.spin_budget = want

    def _decay_spin_budget(self):
        """The answer is further away than any budget worth holding the GIL
        for. Stop paying to prove it again on every single call."""
        self.spin_budget = max(
            _WAIT_SPIN_MIN, self.spin_budget * _WAIT_SPIN_DECAY
        )

    def _wait(self, ms=500):
        """Block until the driver clears `command`, counting the time spent.

        Two phases:
          1. spin    -- a wall-clock-bounded busy loop that keeps the GIL, so
                        a completed IOCTL is seen the moment it completes
          2. backoff -- a real sleep, so a slow or dead driver does not pin a
                        core until the deadline

        There is deliberately no `time.sleep(0)` phase any more, and the
        spin budget tunes itself; see _WAIT_SPIN_MIN for both, and for the
        measurements behind them. `slow` counts the calls that had to reach
        phase 2 -- that number, not the mean, is what says whether the
        answer was there to be caught at all.
        """
        start = time.perf_counter()
        shared = self.shared
        budget = self.spin_budget
        spin_until = start + budget
        try:
            while True:
                for _ in range(_WAIT_SPIN_CHECK_EVERY):
                    if shared.command == 0:
                        self._note_answer_latency(
                            time.perf_counter() - start, True
                        )
                        return True
                if time.perf_counter() >= spin_until:
                    break
            self.io_slow_calls += 1
            deadline = start + ms / 1000
            while time.perf_counter() < deadline:
                if shared.command == 0:
                    self._note_answer_latency(
                        time.perf_counter() - start, False
                    )
                    return True
                time.sleep(_WAIT_BACKOFF_SECONDS)
            # No answer at all inside the deadline -- not "soon", whatever
            # the elapsed number says, so it must not reach the probe branch.
            self._decay_spin_budget()
            return False
        finally:
            self.io_calls += 1
            self.io_wait_s += time.perf_counter() - start

    def attach(self, timeout=0.0):
        names = ["RustClient.exe"]
        deadline = time.perf_counter() + timeout if timeout > 0 else 0
        attempt = 0
        while True:
            attempt += 1
            for n in names:
                t0 = time.perf_counter()
                self.shared.pid = 0
                self.shared.cr3 = 0
                self.shared.status = -1
                self.shared.process_name = n.encode()[:259]
                self.shared.command = 1  # CMD_FIND_PROCESS
                self._signal_command()
                completed = self._wait(4000)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                
                status_raw = self.shared.status
                status_u32 = status_raw & 0xFFFFFFFF
                pid = self.shared.pid
                cr3 = self.shared.cr3
                cmd = self.shared.command
                
                print(f"[ATTACH] attempt={attempt} name='{n}' completed={completed} "
                      f"status=0x{status_u32:08X} pid={pid} cr3=0x{cr3:X} command={cmd} elapsed={elapsed_ms:.2f}ms", flush=True)
                
                if completed and status_u32 == 0 and pid != 0 and cr3 != 0:
                    self.pid, self.cr3 = pid, cr3
                    return True
            if timeout <= 0 or time.perf_counter() >= deadline:
                break
            if attempt % 10 == 1:
                remaining = max(0, deadline - time.perf_counter())
                print(f"[~] Process non trouvé, retry... ({remaining:.0f}s restant)", flush=True)
            time.sleep(2.0)
        return False

    def find_base(self):
        for _ in range(3):
            self.shared.command = 4  # CMD_GET_PEB
            self._signal_command()
            if self._wait(2000) and self.shared.status == 0:
                self.base = self.shared.image_base
                if self.base:
                    return self.base
            time.sleep(0.02)

        try:
            for base_addr, base_name, full_name, _ in iter_vad_image_modules(self):
                if (
                    _module_matches(base_name, full_name, "rustclient.exe")
                    or _module_matches(base_name, full_name, "windowsplayer.exe")
                ):
                    self.base = base_addr
                    break
        except NameError:
            pass
        return self.base

    def read(self, addr, size):
        if size <= 0:
            return b''
        if not isinstance(addr, int) or addr < 0x10000 or addr > MAX_USER_ADDR:
            return b'\x00' * size
        if size > COMM_DATA_SIZE:
            chunks = []
            cur = addr
            remaining = size
            while remaining > 0:
                take = min(remaining, COMM_DATA_SIZE)
                chunks.append(self.read(cur, take))
                cur += take
                remaining -= take
            return b''.join(chunks)
        t_yield0 = time.perf_counter()
        self._yield_to_camera()
        self._yield_to_tick()
        self.yield_wait_s += time.perf_counter() - t_yield0
        t_lock0 = time.perf_counter()
        with self._drv_lock:
            self.lock_wait_s += time.perf_counter() - t_lock0
            self.shared.cr3 = self.cr3
            self.shared.address = addr
            self.shared.size = size
            self.shared.command = 2
            self._signal_command()
            if not self._wait():
                return b'\x00' * size
            if self.shared.status != 0:
                return b'\x00' * size
            key = (addr ^ 0x5A) & 0xFF
            return ctypes.string_at(ctypes.addressof(self.shared.data), size).translate(XOR_TABLES[key])

    def write(self, addr, data_bytes):
        size = len(data_bytes)
        if size <= 0:
            return False
        if not isinstance(addr, int) or addr < 0x10000 or addr > MAX_USER_ADDR:
            return False
        if size > COMM_DATA_SIZE:
            cur = addr
            remaining = size
            offset = 0
            while remaining > 0:
                take = min(remaining, COMM_DATA_SIZE)
                if not self.write(cur, data_bytes[offset:offset+take]):
                    return False
                cur += take
                offset += take
                remaining -= take
            return True
            
        t_yield0 = time.perf_counter()
        self._yield_to_camera()
        self._yield_to_tick()
        self.yield_wait_s += time.perf_counter() - t_yield0
        t_lock0 = time.perf_counter()
        with self._drv_lock:
            self.lock_wait_s += time.perf_counter() - t_lock0
            
            # XOR encrypt the data payload before sending to driver
            key = (addr ^ 0x5A) & 0xFF
            enc_data = data_bytes.translate(XOR_TABLES[key])
            ctypes.memmove(self.shared.data, enc_data, size)
            
            self.shared.cr3 = self.cr3
            self.shared.address = addr
            self.shared.size = size
            self.shared.command = 3 # CMD_WRITE_MEMORY
            self._signal_command()
            if not self._wait():
                return False
            return self.shared.status == 0

    def write_retry(self, addr, data_bytes, attempts=5):
        for _ in range(attempts):
            if self.write(addr, data_bytes):
                return True
            time.sleep(0.002)
        return False

    # ── Aim (CMD_CEREBRO_AIM = 12) ──
    # Fires a single Bezier+Perlin humanized swipe of (dx, dy) pixels through
    # the driver's mouse_worker_thread. `stiffness` scales the Fitts's-law
    # duration : higher = slower/smoother, lower = snappier. Typical 15-40.
    # The driver consumes the request atomically at swipe start and ignores
    # further CMD_CEREBRO_AIM until the current swipe completes -- so calling
    # this every tick queues nothing, it only refreshes the target for the
    # next swipe.
    def cmd_aim(self, dx, dy, stiffness=30):
        dx = int(max(-32768, min(32767, dx)))
        dy = int(max(-32768, min(32767, dy)))
        stiffness = int(max(1, min(1000, stiffness)))
        with self._drv_lock:
            self.shared.target_dx = dx
            self.shared.target_dy = dy
            self.shared.stiffness = stiffness
            self.shared.command = 12  # CMD_CEREBRO_AIM
            self._signal_command()
            if not self._wait():
                return False
            return self.shared.status == 0

    # ── Direct mouse move (CMD_MOUSE_MOVE = 8) ──
    # Injects ONE MOUSE_INPUT_DATA move via the same class-service callback
    # as the humanized aim, but WITHOUT Bezier / Fitts / Perlin. The full
    # (dx, dy) delta lands in a single kernel call so a v2 aim command
    # reaches the target in exactly one step -- what the angle math wants.
    # Wire format : dx packed in low 32 bits of shared.address, dy in high.
    def cmd_mouse_move_direct(self, dx, dy):
        dx = int(max(-32768, min(32767, dx)))
        dy = int(max(-32768, min(32767, dy)))
        # Mask to 32 bits (two's complement) then pack into a 64-bit field.
        packed = (dx & 0xFFFFFFFF) | ((dy & 0xFFFFFFFF) << 32)
        with self._drv_lock:
            self.shared.address = packed
            self.shared.command = 8  # CMD_MOUSE_MOVE
            self._signal_command()
            if not self._wait():
                return False
            return self.shared.status == 0

    # ── Mouse click (CMD_MOUSE_CLICK = 13) ──
    # button_flags follows Windows MOUSE_INPUT_DATA.ButtonFlags :
    #   0x0001 LEFT_BUTTON_DOWN   0x0002 LEFT_BUTTON_UP
    #   0x0004 RIGHT_BUTTON_DOWN  0x0008 RIGHT_BUTTON_UP
    #   0x0010 MIDDLE_BUTTON_DOWN 0x0020 MIDDLE_BUTTON_UP
    def cmd_click(self, button_flags):
        with self._drv_lock:
            self.shared.button_flags = int(button_flags) & 0xFFFF
            self.shared.command = 13  # CMD_MOUSE_CLICK
            self._signal_command()
            if not self._wait():
                return False
            return self.shared.status == 0

    def read_priority(self, addr, size):
        """Give the render camera the next available driver transaction."""
        if size <= 0:
            return b''
        if (
            not isinstance(addr, int)
            or addr < 0x10000
            or addr > MAX_USER_ADDR
            or size > COMM_DATA_SIZE
        ):
            return None
        with self._claim_tier(TIER_CAMERA):
            # Unlike read()/batch_u64(), this never had lock_wait_s
            # instrumentation -- so time spent blocked *acquiring* _drv_lock
            # (someone else's IOCTL already in flight; a tier claim only
            # stops new lower-tier callers from starting, it cannot preempt
            # one already running) was invisible. Added 2026-08-26 after
            # [CAM-HZ] measured a ~30ms floor per sample for what should be
            # one ~15ms IOCTL -- this is what finds the other ~15ms.
            t_lock0 = time.perf_counter()
            with self._drv_lock:
                self.lock_wait_s += time.perf_counter() - t_lock0
                self.shared.cr3 = self.cr3
                self.shared.address = addr
                self.shared.size = size
                self.shared.command = 2
                self._signal_command()
                if not self._wait() or self.shared.status != 0:
                    return None
                key = (addr ^ 0x5A) & 0xFF
                return ctypes.string_at(
                    ctypes.addressof(self.shared.data),
                    size,
                ).translate(XOR_TABLES[key])

    def read_retry(self, addr, size, attempts=5):
        data = b''
        for _ in range(attempts):
            data = self.read(addr, size)
            if data and any(data):
                return data
            time.sleep(0.002)
        return data

    def u64(self, a):
        return struct.unpack('<Q', self.read(a, 8))[0]

    def u64_retry(self, a, attempts=5):
        return struct.unpack('<Q', self.read_retry(a, 8, attempts))[0]

    def batch_u64(self, addrs, attempts=3, skip_camera_yield=False):
        """Read N u64 values in a single IOCTL (CMD_BATCH_READ_U64=5).

        skip_camera_yield: for the tick's own critical-path reads only
        (_read_player_frame_batch's d_batch, _sample_bones_multi's TRS
        batch, _read_parent_index_buffers on a cache miss). Measured
        2026-08-26: _yield_to_camera() alone cost ~14-15ms per call here,
        nearly doubling every tick's driver round-trip -- io+yield already
        accounted for ~59ms of a 62ms tick, leaving ~2ms of actual Python
        (bone composition was never the bottleneck the older notes assumed).
        Slow-lane/background callers (names, held items, world entities,
        parent-index misses from elsewhere) must keep yielding -- this flag
        must not become the default.
        """
        n = len(addrs)
        if n == 0:
            return []
        if n > 16000:
            # split
            out = []
            for i in range(0, n, 16000):
                out.extend(self.batch_u64(
                    addrs[i:i + 16000], attempts=attempts,
                    skip_camera_yield=skip_camera_yield,
                ))
            return out
        if n * 8 > COMM_DATA_SIZE:
            return [self.u64_retry(a, attempts=2) for a in addrs]

        # Sort addresses by page to trigger the driver's page cache optimization
        # This prevents invalidating the page walk cache on every read
        indexed_addrs = sorted(enumerate(addrs), key=lambda x: x[1])
        sorted_addrs = [x[1] for x in indexed_addrs]

        for attempt in range(max(1, attempts)):
            t_yield0 = time.perf_counter()
            if not skip_camera_yield:
                self._yield_to_camera()
            self._yield_to_tick()
            self.yield_wait_s += time.perf_counter() - t_yield0
            t_lock0 = time.perf_counter()
            with self._drv_lock:
                self.lock_wait_s += time.perf_counter() - t_lock0
                ctypes.memmove(self.shared.data, struct.pack(f'<{n}Q', *sorted_addrs), n * 8)
                self.shared.cr3 = self.cr3
                self.shared.size = n
                self.shared.command = 5
                self._signal_command()
                ok = self._wait(2000) and self.shared.status == 0
                raw = (
                    struct.unpack(f'<{n}Q', ctypes.string_at(ctypes.addressof(self.shared.data), n * 8))
                    if ok else None
                )
            if not raw:
                time.sleep(0.0005)
                continue

            # Decrypt and restore original order
            out = [0] * n
            for i, (orig_idx, addr) in enumerate(indexed_addrs):
                out[orig_idx] = raw[i] ^ (((addr ^ 0x5A) & 0xFF) * 0x0101010101010101)
            if any(out) or attempt == attempts - 1:
                return out
            time.sleep(0.0005)
        return [0] * n

    def batch_u64_priority(self, addrs, attempts=1):
        """Give the tick's once-a-second chain/pm_list refresh the next
        available driver transaction, the same way read_priority() does for
        the camera (see trap #54 and TIER_TICK).

        Only for the two small refresh reads on the tick's own thread -- the
        tick's large per-frame batches must stay on the normal batch_u64()
        path, or the slow lane would starve in turn. Also defers to the
        camera tier first (via _claim_tier) -- the previous hand-rolled
        version did not, so the tick's priority reads could win a race the
        camera was supposed to win outright.
        """
        n = len(addrs)
        if n == 0:
            return []
        indexed_addrs = sorted(enumerate(addrs), key=lambda x: x[1])
        sorted_addrs = [x[1] for x in indexed_addrs]
        with self._claim_tier(TIER_TICK):
            for attempt in range(max(1, attempts)):
                with self._drv_lock:
                    ctypes.memmove(self.shared.data, struct.pack(f'<{n}Q', *sorted_addrs), n * 8)
                    self.shared.cr3 = self.cr3
                    self.shared.size = n
                    self.shared.command = 5
                    self._signal_command()
                    ok = self._wait(2000) and self.shared.status == 0
                    raw = (
                        struct.unpack(f'<{n}Q', ctypes.string_at(ctypes.addressof(self.shared.data), n * 8))
                        if ok else None
                    )
                if not raw:
                    time.sleep(0.0005)
                    continue
                out = [0] * n
                for i, (orig_idx, addr) in enumerate(indexed_addrs):
                    out[orig_idx] = raw[i] ^ (((addr ^ 0x5A) & 0xFF) * 0x0101010101010101)
                if any(out) or attempt == attempts - 1:
                    return out
                time.sleep(0.0005)
            return [0] * n

    def read_ptr_array(self, addrs, attempts=3, fallback_limit=512):
        vals = self.batch_u64(addrs, attempts=attempts)
        if vals and any(vals):
            return vals
        if len(addrs) <= fallback_limit:
            return [self.u64_retry(a, attempts=2) for a in addrs]
        return vals

    def i32(self, a):
        return struct.unpack('<i', self.read(a, 4))[0]

    def i32_retry(self, a, attempts=5):
        return struct.unpack('<i', self.read_retry(a, 4, attempts))[0]

    def u32(self, a):
        return struct.unpack('<I', self.read(a, 4))[0]

    def f32(self, a):
        return struct.unpack('<f', self.read(a, 4))[0]

    def vec3f(self, a):
        d = self.read(a, 12)
        return struct.unpack('<fff', d) if len(d) == 12 else (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Offsets — dump 2026-09-08 (game update). Fields not in new dump marked STALE.
# ---------------------------------------------------------------------------
class OFF:
    # --- Klass RVAs (GameAssembly.dll) ---
    # 2026-09-08 fresh dump — updated after game update
    BaseNetworkable_c           = 0x118D4020  # game update 2026-09-10 (was 0x119B5410)
    BasePlayer_c                = 0x11999730  # STALE (2026-09-07) — not in new dump
    MainCamera_c                = 0x118B4098  # game update 2026-09-10 (was 0x119B0A40)
    LocalPlayer_c               = 0x1198FC28  # STALE (2026-09-07) — not in new dump
    ListComponent_PlayerModel_c = 0x118D8CB0  # game update 2026-09-10 (was 0x1196D5A8)
    BaseViewModel_c             = 0x11944670  # dump 2026-09-08
    TOD_Sky_c                   = 0x119CC870  # dump 2026-09-08
    BaseEntity_c                = 0x118F4970  # STALE (2026-09-07) — not in new dump
    BaseCombatEntity_c          = 0x119E4848  # STALE (2026-09-07) — not in new dump
    BaseProjectile_c            = 0x119E9418  # STALE (2026-09-07) — not in new dump
    GameManager_c               = 0x11866048  # STALE (2026-09-07) — not in new dump
    ItemIcon_c                  = 0x11623CF0  # STALE (2026-09-07) — not in new dump
    OreResourceEntity_c         = 0x115DEA50  # STALE (build 24840484)
    CollectibleEntity_c         = 0x115DDE18  # STALE (build 24840484)
    WorldItem_c                 = 0x115DDCF8  # STALE (build 24840484)
    DroppedItemContainer_c      = 0x115DEA10  # STALE (build 24840484)
    BuildingPrivlidge_c         = 0x1156FE20  # STALE (build 24840484)
    LootContainer_c             = 0x10889998  # STALE (build 24614784)
    IL2CppHandle_c              = 0x11A88F30  # STALE (2026-09-07) — not in new dump
    # Game update 2026-09-10, resolved. The user found it directly in a
    # fresh script.json TypeInfo listing: {"Address": 294759824, "Name":
    # "ListComponent<Projectile>_TypeInfo", "Signature":
    # "ListComponent_Projectile__c*"} -- 294759824 decimal = 0x1191AD90. The
    # class NAME confirms the guess this project made when the generic
    # ListComponent<PlayerModel> shape stopped resolving (see
    # find_projectile_klass.py's docstring): this build's IL2CPP codegen
    # literally names T-instantiations "ListComponent_<T>" in the class's
    # raw .name field. Sits in the same 0x118x/0x119x klass-RVA neighborhood
    # as BaseNetworkable_c/MainCamera_c/ListComponent_PlayerModel_c above,
    # which this update also moved -- a real sanity check, not just a
    # plausible-looking number.
    ProjectileList_c            = 0x1191AD90  # game update 2026-09-10 (was 0x1195F430, unverified NeoRed SDK v6 value)


    # --- IL2CPP API RVAs ---
    # Kept for diagnostics only: an external reader uses the table-page
    # algorithm in resolve_tagged_handle() instead of calling target code.
    il2cpp_gchandle_get_target_rva = 0x11E93210  # rust-dumper-post live dump, 2026-09-08 (was 0x87A2C0, build 24840484) — diagnostic only, no call site

    # --- Il2Cpp class internals ---
    klass_static_fields = 0xB8

    # --- Entity walker (HiddenValue chain, dump.txt chain-walker reference) ---
    # dump.cs + script.json: the class whose typeinfo lives at
    # BaseNetworkable_c is NOT `BaseNetworkable` (that class has ZERO
    # static fields). It's a static-only holder class (obfuscated name)
    # (TypeDefIndex 6721), a static-only holder class with exactly TWO fields:
    #     // 0x0  static HiddenValue<BaseNetworkable.ClientRealm>
    #     // 0x8  static HiddenValue<BaseNetworkable.ClientRealm>
    # i.e. clientEntities and serverEntities, in an order the obfuscated dump does
    # not name (offsets_decrypts_export.h exports `client_entities = 0`, meaning
    # the dumper could not resolve it either). Picking the wrong one yields a realm
    # whose entity list is empty, so every walk dies at "no array" -- entity ESP,
    # pm->bp mapping, health, names and held items all starve at once.
    # _resolve_entity_chain() therefore probes both and latches onto the one that
    # actually produces a validated entity buffer.
    # 2026-09-08: rust-dumper-post resolves this by TYPE (a static field
    # whose generic argument is BaseNetworkable.<ClientRealm-ish>), not by
    # "which slot is currently non-null" -- structurally reliable regardless
    # of which of the two statics happens to be populated at read time.
    # Result: clientEntities is at +0x0, not +0x8. Swapped the primary/alt
    # pair below; the existing probe still tries both, so this only saves
    # the wasted first attempt each run, not a correctness requirement.
    wrapper_in_static     = 0x0   # WRAPPER: clientEntities wrapper in static fields (was 0x8)
    wrapper_in_static_alt = 0x8   # the other of the two HiddenValue<ClientRealm> statics (was 0x0)
    hv_slot             = 0x18  # HV_HANDLE: HiddenValue<T>._handle encrypted u64
    hv_has_value        = 0x10  # HV_HAS_VALUE: HiddenValue<T>._hasValue init-flag
    # was 0x14 (build 24840484); new dump 2026-09-03 moves _hasValue to
    # 0x10 and _accessCount from 0x10 to 0x20 -- _handle stays at 0x18.
    # Per trap #2 we do NOT gate on _hasValue anyway (cl1kexternal reads
    # the handle unconditionally), so this update is for correctness only,
    # not runtime behaviour.
    parent_in_realm     = 0x10  # PARENT: EntityRealm -> entity_list/parent
    # list_dict + buffer_array IS the Il2CppArray itself (no separate
    # BufferList wrapper) — empirically confirmed, elements have valid
    # klass pointers. buffer_count is relative to the ARRAY, not list_dict:
    # standard Il2CppArray layout (klass@0, monitor@8, bounds@0x10 unused
    # for single-dim, max_length@0x18, data@0x20).
    # Windows scanned for the BufferList holding the entity array, matching
    # Cl1kExternal's EntityRealm_Scan_Low/High and Container_Scan_Low/High.
    # dump.txt's chain-walker puts it at ENTITIES=0x20 on this build, which is
    # inside both windows; scanning means a shift on the next patch self-heals.
    entity_realm_scan_low       = 0x08
    entity_realm_scan_high      = 0x58
    entity_container_scan_low   = 0x08
    entity_container_scan_high  = 0x40
    buffer_array        = 0x10  # BufferList -> Il2CppArray*
    buffer_count        = 0x18  # Il2CppArray.max_length (relative to the array, not list_dict)
    # Tried +0x28 for this buffer's element-0 offset (Cl1kExternal's
    # Il2Cpp::Array_DataBase, on the theory it's a BufferList variant) and it
    # nearly halved the valid-pointer yield on this build (2515/3472 -> 161/
    # 3625) — reverted to the same 0x20 the PlayerModel list below is LOCKED
    # to. Don't reintroduce a separate payload for this buffer without first
    # confirming yield goes *up*, not down.
    # NeoRed SDK v6 (build 24840484) independently re-proposes 0x28 here
    # (base_networkable.il2cpp_array_data_base, tagged [PROBED_HIGH] = medium
    # confidence auto-probe, not a validated read). Rejected on purpose: this
    # is the exact same value that regressed the yield above, and the doc's
    # own "stable across patches" list puts the Il2CppArray header among the
    # engine-level constants that shouldn't drift. Only flip this with a
    # fresh before/after yield count proving it helps.
    # A second, validated-on-discord dump (same build) independently backs
    # 0x20: its system_list.array_first_element and entitylist.first_elem
    # are both 0x20, with no mention of 0x28 anywhere.
    array_payload       = 0x20  # Il2CppArray managed T[] data offset (dropzoo: first_element=0x20)

    # --- BaseCombatEntity (dropzoo / export confirmed) ---
    lifestate           = 0x2A8
    _health             = 0x2B4
    _maxHealth          = 0x2B8

    # --- LocalPlayer_Static / BasePlayer_Static ---
    # Two same-build (24840484) dumps disagreed here: NeoRed v6's heuristic
    # "local_player_canonical" auto-probe said klass=0x1161ABD8, Entity=0x10
    # (self-tagged [AUTO_PROBED]/CANONICAL, i.e. not a plain metadata read).
    # A second, validated-on-discord dump gives a clean TypeDefinitionIndex-
    # anchored LocalPlayer_Static_Offsets (typeinfo=0x11587700, Entity=0x8)
    # with no uncertainty tags, and it independently cross-confirmed several
    # other NeoRed values exactly (playerModel, input, eyes, inventory,
    # container_belt/wear, item_list, mainCamera...), so it's trusted higher
    # where the two conflict. Went with this one; revert to the NeoRed pair
    # above if local-player resolution breaks in-game.
    LocalPlayer_Entity           = 0x8
    BasePlayer_visiblePlayerList = 0x138  # encrypted dictionary; no direct list traversal
    ListHashSet_vals             = 0x10   # ListHashSet<T>::_vals (T[] inner array at +0x10)
    ListHashSet_size             = 0x18   # ListHashSet<T>::_size (element count, NOT array capacity)

    # --- ListComponent<PlayerModel> (martin dumper) ---
    ListComponent_instance  = 0x8   # sf → singleton at sf+0x8
    ListComponent_parent    = 0x18  # game update 2026-09-10: parent=0x18 (was 0x10 on 2026-09-08 -- reverted, not a fresh guess: this build's export.h says the same 0x18 the 2026-09-05-and-earlier builds used)
    ListComponent_buffer    = 0x10  # dump 2026-09-08: buffer=0x10 (unchanged 2026-09-10)
    ListComponent_size      = 0x18  # BufferList.count at +0x18 (unchanged 2026-09-10)

    # --- BasePlayer (game update 2026-09-10, offsets_decrypts_export.h) ---
    playerModel         = 0x498  # game update 2026-09-10 (was 0x7A0)
    input               = 0x630  # game update 2026-09-10 (was 0x3E8) -- header calls this playerInput
    eyes                = 0x6F8  # game update 2026-09-10 (was 0x348) -- header calls this playerEyes
    inventory           = 0x3B8  # game update 2026-09-10 (was 0x510) -- header calls this playerInventory
    _displayName        = 0x2F8  # game update 2026-09-10 (was 0x438) -- header calls this username
    userID              = 0x720  # STALE (2026-09-07) — not in new dump
    userID_string       = 0x3C0  # STALE (2026-09-07) — not in new dump
    playerFlags         = 0x6D8  # dump 2026-09-08 (unchanged through 2026-09-10 update)
    cl_active_item      = 0x588  # dump 2026-09-08 (unchanged through 2026-09-10 update)
    held_entity_cache   = 0x5E0  # STALE (2026-09-07) — not in new dump
    base_movement       = 0x540  # STALE (2026-09-07) — not in new dump
    current_team        = 0x558  # confirmed by the user's own dump 2026-09-09 (unchanged through 2026-09-10 update)
    model_state         = 0x4B0  # STALE (2026-09-07) — not in new dump
    belt_shortcut       = 0x338  # STALE (2026-09-07) — not in new dump

    # --- WorldItem (dropped item entity) ---
    world_item_item     = 0x208  # WorldItem.item -> Item* (confirmed by rust-dumper SDK, 2026-08-21)

    # --- Item ---
    # Game update 2026-09-10 reshuffled this class -- item_definition moved
    # from a live-confirmed 0xA0 to 0x70, which alone proves every offset
    # below it needs re-verification, not just a klass RVA change. Read
    # directly out of the fresh dump.cs (TypeDefIndex 700), which prints real
    # FIELD TYPES even where names are hash-obfuscated -- this is stronger
    # evidence than offsets_decrypts_export.h's own "// test these" guesses,
    # which don't carry type info and got item_definition/item_list wrong.
    # New layout (instance fields only, statics omitted):
    #   0x10 (opaque struct), 0x18 bool, 0x20 EntityRef-shaped struct (type
    #   repeats at 0xC0), 0x30 Nullable<int>, 0x38 float [condition],
    #   0x3C int, 0x40 Item.Flag, 0x48 ItemContainer [contents],
    #   0x50 ulong, 0x58/0x5C float, 0x60 Action<Item>,
    #   0x68 ItemContainer [parent], 0x70 ItemDefinition [info],
    #   0x78 uint, 0x7C float, 0x80 (opaque, Pool-managed type),
    #   0x88 string, 0x90/0x94/0x98 float, 0x9C bool, 0xA0 int,
    #   0xA8 string, 0xB0 (opaque struct), 0xB8 ulong,
    #   0xC0 EntityRef-shaped struct (same type as 0x20),
    #   0xD0 List<...>, 0xD8 Nullable<int>, 0xE0 string.
    item_definition     = 0x70   # game update 2026-09-10 (was 0xA0). ItemDefinition-typed field, unambiguous in dump.cs -- confirmed by BOTH dump.cs and the header.
    # Item has exactly two ItemContainer-typed fields (0x48, 0x68), the same
    # parent/contents pair the class had before the update, just moved.
    # ItemContainer.parent (below) points BACK to this same field at Item's
    # 0x68, i.e. Item.parent<->ItemContainer.parent is a matched pair -- a
    # real structural cross-check, not a guess.
    item_parent_container = 0x68 # was 0x10 pre-update. UNCONFIRMED role (parent vs contents) but cross-checked against ItemContainer.parent (0x20) pointing back to a same-typed field. Unused in this codebase.
    item_contents       = 0x48   # was 0x28 pre-update. UNCONFIRMED role -- see item_parent_container. Unused in this codebase.
    # item_uid: RESOLVED by a second, independent dumper source (2026-09-10,
    # user-supplied `struct item { ... uid = 0x80; }`). That source's OTHER
    # values are strong corroboration -- it hands back item_list=0x48,
    # item.definition=0x70, and the wear=0x30/main=0x60/belt=0x78 triple bit
    # for bit identical to what this file already had (the triple from live
    # in-game correction, the other two from dump.cs field-typing), so its
    # uid=0x80 is trusted over the earlier reasoned guess of 0xB8.
    # The earlier guess had rejected 0x80 for looking like "an opaque
    # Pool-managed type" rather than a plain ulong in dump.cs -- that was a
    # misread, not a real conflict: Item.uid's real type is `ItemId`, a
    # readonly struct WRAPPING a single ulong, not a bare ulong field, so
    # dump.cs printing a struct-shaped type at 0x80 is exactly what a
    # correctly-identified ItemId field should look like.
    item_uid            = 0x80   # game update 2026-09-10, second-source confirmed (was 0xB8 reasoned guess, was 0xD8 pre-update)
    # item_worldEnt/item_heldEntity/item_held_entity_direct remain
    # unresolved. The same second source that settled item_uid gives
    # `item.held_entity = 0x0` for this field -- clearly a placeholder, not
    # a real class-relative offset (nothing in Item lives at 0x0), so it
    # independently confirms this field is HARD to pin down rather than
    # handing over a working number. Left at the pre-update values below
    # even though those are almost certainly wrong too (the old EntityRef
    # pair sat at 0x80/0xB8, both now claimed by other fields; dump.cs shows
    # the EntityRef-shaped struct pair moved to 0x20/0xC0). Unlike item_uid,
    # a wrong held-entity offset has NO clean self-check -- it just
    # validates as "some pointer" and silently poisons whatever reads it
    # next, which is exactly how this field broke two features at once
    # before. NEEDS A LIVE PROBE (read both 0x20 and 0xC0 off a matched
    # item, check which one's target class name matches a weapon/BaseEntity
    # prefab) before either value is used.
    item_worldEnt       = 0xB8   # STALE post-2026-09-10 update -- do not trust, see comment above
    item_heldEntity     = 0x80   # STALE post-2026-09-10 update -- do not trust, see comment above (also now collides with the corrected item_uid value; unrelated fields, not a bug)
    item_held_entity_direct = 0xB8  # STALE post-2026-09-10 update -- see item_heldEntity
    item_amount         = 0xA0   # second-source value (2026-09-10). Unused in this codebase.
    item_condition      = 0x68   # STALE (2026-09-07) — not in new dump, unused in this codebase
    item_max_condition  = 0xC4   # STALE (2026-09-07) — not in new dump, unused in this codebase
    item_position       = 0x90   # STALE post-2026-09-10 update. Unused in this codebase.
    item_clientAmmoCount = 0xB8  # STALE (2026-09-07) — not in new dump, unused in this codebase, COLLIDES with item_held_entity_direct

    # --- ItemDefinition (cross-checked against dump.cs TypeDefIndex 8552,
    # line 1206673 -- clean, non-obfuscated field names) ---
    itemdef_shortName       = 0x28  # dump.cs: `public string shortname` -- confirmed
    itemdef_displayName     = 0x40  # dump.cs: `public Phrase displayName` -- confirmed
    # itemdef_displayEnglish is UNUSED anywhere in this codebase (dead constant).
    # 0x20 in dump.cs is `public int itemid`, not a string -- definitely wrong
    # if ever read directly. Left as-is since nothing dereferences it; flag
    # for removal or correction before wiring up a real caller.
    itemdef_displayEnglish  = 0x20  # UNUSED -- dump.cs says 0x20 is `itemid` (int), NOT a name field
    # dump.cs line 1206729: `private ItemModWearable ... // 0x168` -- this IS
    # the real field. 0x178 (previous value) is an unrelated obfuscated type.
    itemdef_itemModWearable = 0x1A0 # dump 2026-09-08 (was 0x168)

    # --- Inventory (cross-checked against dump.cs TypeDefIndex 4636,
    # line 659966 -- the actual PlayerInventory field listing) ---
    # dump.cs shows exactly THREE fields of the identical ItemContainer type
    # (%d0bd7af15077344f2326e90024a578bfc7b0c5de) at 0x28/0x58/0x78, with
    # unrelated types at everything in between (0x38=Action<float,bool>,
    # 0x48=PlayerLoot, 0x50=float, 0x54=bool). dump.cs's field TYPES confirm
    # WHICH offsets hold a container, but not WHICH slot is wear/belt/main --
    # that ordering can only be told apart by content, live. The previous
    # wear=0x28/belt=0x58/main=0x78 assignment was live-verified wrong (2026-09-05
    # screenshot: "CLOTHING/ARMOR" showed weapons/tools, "MAIN INVENTORY"
    # showed a hat/gloves/boots/backpack) -- a 3-way rotation, not a 2-way
    # swap: whatever was read as wear was actually belt's content, what was
    # read as belt was actually main's, what was read as main was actually
    # wear's. Rotating the offset assignment to match:
    # REVERTED 2026-09-09. Two live-injected tools (rust-dumper-post 09-08,
    # NeoRed SDK v6 09-09) each proposed a DIFFERENT full 3-way rotation of
    # these three roles, and applying the second one made the whole
    # inventory panel disappear -- not just miscategorized, gone. Root
    # cause: max_len in _read_held_items_batch is keyed off the ctype label
    # (36 for "main", 12 for anything else) as a sanity cap, not a role
    # check. Whichever position actually holds the big (>12 item) main
    # inventory needs the label "main" or its contents get silently
    # rejected by that cap and the container vanishes from container_slots
    # entirely. NeoRed's rotation moved "main" off of 0x60 onto 0x78, and
    # 0x60 is evidently the one that actually holds >12 items live, so
    # everything there got capped out and disappeared.
    # This is back to the original 2026-09-05 screenshot-verified mapping,
    # which was already correct before either tool's rotation was applied --
    # the held-items/no-recoil bug both sessions were chasing was entirely
    # explained by item_heldEntity (0x38 -> 0xB8, see above), not by this.
    # Do not rotate these three again without a live screenshot confirming
    # which slot actually holds >12 items.
    #
    # Game update 2026-09-10: dump.cs's fresh field list for PlayerInventory
    # shows the three ItemContainer-typed fields at 0x30/0x60/0x78 (previously
    # 0x28/0x60/0x78). The FIRST attempt here assumed the same guess that
    # burned this exact area before -- "position in declaration order maps
    # to the same role across an update" -- and reassigned only 0x28->0x30
    # to belt, leaving wear=0x60/main=0x78 as they were. That was WRONG: the
    # user immediately saw wear-type items (clothes) rendered under the belt
    # label in-game. Swapped 0x30<->0x60 below on that live signal, exactly
    # the kind of confirmation this file's own history says to trust over
    # any dump-based reasoning (see the "do not rotate... without a live
    # screenshot" warning above -- this update's role assignment needed
    # that live check after all, the dump's numeric shift was not enough by
    # itself). main (0x78) is untouched -- not reported wrong.
    # Two independent live reports, combined directly rather than re-guessed:
    #   1st report: what was labelled "belt" (0x30) showed clothes -> 0x30 IS
    #      wear. (Confirmed, never re-reported wrong after this.)
    #   2nd report: after fixing that (belt=0x60, main=0x78), belt and main
    #      came back inverted -> 0x60 is actually main, 0x78 is actually belt.
    # Combining both: wear=0x30, main=0x60, belt=0x78. Do not re-derive this
    # from the dump.cs field-order reasoning again -- that reasoning produced
    # BOTH wrong guesses above; only the live reports settled it.
    container_wear      = 0x30   # confirmed by live feedback 2026-09-10
    container_belt      = 0x78   # confirmed by live feedback 2026-09-10 (2nd correction)
    container_main      = 0x60   # confirmed by live feedback 2026-09-10 (2nd correction)
    # ItemContainer.list: dump.cs (fresh TypeDefIndex 2783) shows exactly one
    # `List<Item>`-typed field on ItemContainer, at 0x48 -- unambiguous by
    # type, the same kind of positive ID rust-dumper-post used pre-update.
    # Both the header's 0x40 guess (that offset is actually `int capacity`
    # per dump.cs) and the pre-update 0x78 are wrong for this build.
    item_list           = 0x48   # game update 2026-09-10 (was 0x78). List<Item>-typed field, unambiguous in dump.cs.

    # --- BaseEntity ---
    # 0x1B8 matches offsets_decrypts_export.h generated same-day from THIS
    # running build (C:\Users\rara8\Desktop\rust dumper release\new), which is
    # the authoritative source for game-code field offsets — those shift
    # almost every Rust patch. Cl1kExternal's 0x1A8 is from a different,
    # unknown-vintage build; briefly tried it here on the theory that one
    # mismatch against cl1kexternal must be the bug, but every *other*
    # BasePlayer/PlayerModel/Model field in this class already matches the
    # fresh dump exactly, so that theory doesn't hold — reverted.
    model               = 0x1B8
    entity_flags        = 0x1C0  # BaseEntity namespace: flags=0x1C0 — unused
    prefab_id           = 0x54   # dropzoo: base_networkable::prefab_id

    # --- BaseEntity world position ---
    # PositionLerp chain (2026-09-04): BaseEntity_Offsets.positionLerp,
    # PositionLerp_Offsets.interpolator, Interpolator_Offsets.last.
    # All from latest dump and self-consistent; update together if any wrong.
    be_position_lerp        = 0x110  # was 0xC8 (build 24840484)
    # `public Bounds bounds;` — a *value* field (AABB center + extents, Vector3+Vector3),
    # confirmed against full deobfuscated dump.cs for this build (0x18C).
    # No pointer indirection, present on every BaseEntity regardless of Model/PositionLerp.
    be_bounds_center         = 0x18C
    be_lerp_nested           = 0x10  # PositionLerp.interpolator → nested Interpolator (was 0x18)
    be_lerp_world_pos        = 0x10  # Interpolator.last → Vec3 world position (was 0x14)

    # --- Il2CppClass ---
    klass_name          = 0x10   # Il2Cpp::Klass_NamePtr → const char*

    # --- Model (BaseEntity.model — dropzoo confirmed) ---
    boneTransforms      = 0x50
    boneNames           = 0x58   # Il2CppString*[] parallel to boneTransforms — dump.txt / offsets_decrypts_export_24840484.h
    rootBone_Model      = 0x28
    headBone_Model      = 0x30

    # --- PlayerModel (offsets_decrypts_export.h) ---
    position_pm         = 0x2F8  # offsets_decrypts_export.h: position
    velocity_pm         = 0x31C  # PlayerModel namespace: newVelocity=0x31C
    # PlayerModel carries four consecutive Vector3s — dump.cs 839117+ lists
    # 0x2F8 (internal, = position), 0x304 (private), 0x310 (internal), 0x31C
    # (private), then a Quaternion at 0x328. Two independent dumps name 0x31C
    # `newVelocity`, but an in-code comment on the position path names 0x310
    # velocity instead, and the field names in this build are obfuscated so
    # neither claim can be settled from a dump. Both are read and scored against
    # the position-differenced velocity at runtime (see _score_velocity_probe);
    # whichever actually tracks player motion wins, and if neither does the
    # extrapolator falls back to differencing alone.
    velocity_pm_a       = 0x310  # candidate A
    velocity_pm_b       = 0x31C  # candidate B
    pm_rootBone         = 0x98   # Transform[] managed array on PlayerModel (dump.cs: `Shoulders`, type-confirmed Transform[])
    pm_is_local_player  = 0xC4   # bool: this PlayerModel belongs to the local player (dump.cs: Nullable<bool> at 0xC4)
    # dump.cs TypeDefIndex 5635 line 795907: `private SkinnedMultiMesh ... // 0x398`
    # -- confirms offsets_decrypts_export.h's PlayerModel.SkinnedMultiMesh=0x398.
    pm_multiMesh        = 0x3A8  # dump 2026-09-08 (was 0x398)

    # --- SkinnedMultiMesh & Renderers (Chams) ---
    smm_rendererList       = 0x58   # dump 2026-09-08 (was 0x50)
    renderer_materialArray = 0x140  # Renderer.materialArray
    renderer_materialCount = 0x150  # Renderer.materialCount (often unused, we read from array size)
    list_items             = 0x10   # List<T>._items (array)
    list_size              = 0x18   # List<T>._size
    array_base             = 0x20   # T[] first element

    # --- Chams Material IDs (from LO's working source) ---
    MATERIAL_IDS = {
        "GreenGlow":        116322,
        "RedGlow":          116580,
        "GlowGreen":        671786,
        "GlowRed":          1231864,
        "NeonOn":           121180,
        "NeonOff":          853306,
        "LaserFlareGreen":  656220,
        "LaserFlareBlue":   557168,
        "LaserFlareYellow": 656222,
        "Emissive":         968310,
        "RedEmissive":      206462,
        "GreenEmissive":    174384,
    }
    # 0x3B8 (previous value) is `private SoundDefinition` in dump.cs -- wrong type entirely.
    pm_skin_renderers   = 0x3A8  # dump 2026-09-08 (was 0x398)

    # --- PlayerEyes (confirmed by fresh dump 2026-09-07) ---
    viewOffset          = 0x40   # confirmed
    bodyRotation        = 0x50   # confirmed (Quaternion: qx=0x50 qy=0x54 qz=0x58 qw=0x5C)
    eyes_head_angles_x  = 0x60   # NEW (2026-09-07): headAngles.x (pitch)
    eyes_head_angles_y  = 0x64   # NEW (2026-09-07): headAngles.y (yaw)
    eyes_wrapper_flag   = 0x10   # HiddenValue wrapper flag byte
    eyes_wrapper_handle = 0x18   # HiddenValue encrypted handle

    # --- PlayerInput (confirmed by fresh dump 2026-09-07) ---
    pi_state            = 0x28   # PlayerInput.state → InputState*
    pi_bodyAngles       = 0x44   # PlayerInput.bodyAngles (Vector2: pitch=+0x44, yaw=+0x48)
    pi_rotation         = 0x34   # PlayerInput.rotation (Quaternion)
    # InputState sub-offsets
    input_state_current  = 0x20  # InputState.current
    input_state_previous = 0x18  # InputState.previous

    # --- Projectile ballistics (NeoRed SDK v6, 2026-09-09) ---
    # Real per-weapon arrow/bolt/nail physics, read off live in-flight
    # Projectile entities instead of guessing muzzle speeds. The Projectile
    # field layout is self-consistent, which is what makes it trustworthy
    # without a live probe: initialVelocity is a Vector3 at 0x28 (12 bytes),
    # so drag lands exactly at 0x34 and gravityModifier at 0x38, and the
    # next field (thickness) at 0x3C -- packed with no holes.
    proj_initial_velocity   = 0x28  # Projectile.initialVelocity (Vector3, m/s)
    proj_drag               = 0x34  # Projectile.drag (float)
    proj_gravity_modifier   = 0x38  # Projectile.gravityModifier (float)
    # ListComponent<Projectile> -> ListHashSet<Projectile> -> Projectile*[]
    # (ListHashSet_vals/_size and array_payload above are the generic part).
    # DEAD (2026-09-09): this was a guess and it was wrong -- it never
    # resolved in-game ("ListHashSet instance invalid" on every scan). The
    # generic instantiation lays out its statics exactly like
    # ListComponent<PlayerModel> after all: static_fields + 0x8 is a WRAPPER,
    # and the list hangs off wrapper + ListComponent_parent (0x10). Two hops,
    # not one. aim_engine.ProjectileBallistics now mirrors the live-proven
    # PlayerModel walk and probes if that ever stops matching; nothing reads
    # this value. Kept only so the wrong number is not re-derived.
    projlist_instance_in_static = 0x28  # DEAD -- do not use
    # BaseProjectile.projectileVelocityScale -- the weapon's multiplier on
    # the ammo's base velocity. Unused while the live-projectile learner
    # covers us, kept because it's the other half of the static formula
    # (muzzle_speed = ammo.projectileVelocity * this).
    bp_projectile_velocity_scale = 0x394

    # --- MainCamera (dump 2026-09-08) ---
    mainCamera          = 0x8    # dump 2026-09-08: instance=0x8 (was 0x28)
    mainCameraTransform = 0x8    # dump 2026-09-08 (was 0x28, currently unused)

    # --- UnityEngine.Camera (native) ---
    # cam_viewMatrix MUST stay exactly cam_projMatrix + 0x170: read_view_proj_matrix()
    # reads ONE contiguous 0x1B0-byte block starting at cam_projMatrix and pulls
    # the view matrix out of that same block at a hardcoded +0x170 (see
    # legacy_runtime.py's read_view_proj_matrix). NeoRed SDK v6's build-24840484
    # camera.projectionMatrix=0x748 broke this (0x2FC-0x748 is negative), which
    # collapsed every player's screen position onto a near-vertical line —
    # reported live, screenshot 2026-08-21. This is a native Unity engine struct
    # layout (not IL2CPP game code), so unlike BasePlayer.* it shouldn't drift
    # per patch — reverted to the value that keeps the +0x170 gap intact.
    cam_viewMatrix      = 0x2FC
    cam_projMatrix      = 0x18C

    # --- Transform (Unity 6) ---
    bone_list           = 0x20
    bone_transform      = 0x10
    transform_access    = 0x28  # unity_transform_native.access_struct_off = 0x28
    td_pos_base         = 0x18
    td_stride           = 0x30
    td_parent_indices   = 0x20   # dropzoo: parent_indices

    # --- Object ---
    m_CachedPtr         = 0x10

    # --- Unity native GameObject/Component chain (rust-dumper SDK,
    # generated 2026-08-21) — the entity IS a UnityEngine.Object (BaseEntity
    # is a MonoBehaviour), so its own m_CachedPtr (m_CachedPtr/0x10 above)
    # already gives the native Component; from there this walks to the
    # owning GameObject, its Transform (always Components[0]), and that
    # Transform's already-composed world position (no parent-chain walk
    # needed — Unity caches it directly on TransformData). Cross-checked
    # against values this project already confirmed independently:
    # native_transform_data (0x28) == transform_access above,
    # native_td_local_trs (0x18) == td_pos_base, native_td_parent_indices
    # (0x20) == td_parent_indices. See OFFSET_RECOVERY.md trap #15.
    native_component_gameobject  = 0x20  # native Component -> owning GameObject
    native_gameobject_components = 0x20  # native GameObject -> Components[] array
    native_component_entry_ptr   = 0x8   # ptr within one Components[] entry (stride 0x10; Transform is always entry 0)
    native_transform_world_pos   = 0x90  # TransformData -> cached world position (Vector3)

    # --- SingletonComponent ---
    Instance            = 0x8

    # --- Player prefab ID ---
    k_player_prefab_id  = 4108440852  # dropzoo confirmed


# ---------------------------------------------------------------------------
# IL2CPP HiddenValue decryption — updated with latest offsets_decrypts_export.h
# Each function: 2-iteration, per-DWORD in-place on the 64-bit value
# ---------------------------------------------------------------------------
def _hv_decrypt(hv_value, ops):
    arr = list(struct.unpack('<II', struct.pack('<Q', hv_value & 0xFFFFFFFFFFFFFFFF)))
    for _ in range(2):
        v = arr[0] & 0xFFFFFFFF
        for op, arg in ops:
            if op == 'add':
                v = (v + arg) & 0xFFFFFFFF
            elif op == 'sub':
                v = (v - arg) & 0xFFFFFFFF
            elif op == 'xor':
                v = (v ^ arg) & 0xFFFFFFFF
            elif op == 'rol':
                v = ((v << arg) | (v >> (32 - arg))) & 0xFFFFFFFF
        arr.pop(0)
        arr.append(v)
    return struct.unpack('<Q', struct.pack('<II', arr[0], arr[1]))[0]


def decrypt_bn0(hv_value):
    # Game update 2026-09-10. offsets_decrypts_export.h's own auto-generated
    # client_entities()/entity_list() came out with EMPTY loop bodies this
    # run (DecrypterGen failed to decode them post-update); the user supplied
    # the real chain separately as base_networkable_0(), decoded from the
    # same a1+0x18 read. Verified bit-exact against a literal transcription
    # of that disassembly over 2000+ random 64-bit inputs. XOR/SUB/XOR chain.
    return _hv_decrypt(hv_value, [('xor', 0xCC2B1C5C), ('sub', 0x196BCA9E), ('xor', 0x1A846030)])


def decrypt_bn1(hv_value):
    # Game update 2026-09-10 (see decrypt_bn0 above; this is
    # base_networkable_1(), the other broken auto-export). XOR/ROL(8)/XOR/ADD.
    return _hv_decrypt(hv_value, [('xor', 0x2573BC7C), ('rol', 8), ('xor', 0xB2F825D8), ('add', 0x617F688B)])


def decrypt_local_player(hv_value):
    # Dump 2026-09-07 (local_player): XOR/ADD/XOR/ROL
    return _hv_decrypt(hv_value, [('xor', 0xF63B094C), ('add', 0x2C3FFE20), ('xor', 0xF46A1867), ('rol', 14)])


def decrypt_player_inventory(hv_value):
    # Game update 2026-09-10: offsets_decrypts_export.h's player_inventory_decrypt,
    # auto-generated (unlike the previous chain, which its own poster flagged
    # as Ghidra-derived and untested -- this run's export has a real, non-empty
    # loop body). ROL(19)/ADD/XOR. Verified bit-exact against a literal
    # transcription of the exported disassembly over 2000+ random inputs.
    return _hv_decrypt(hv_value, [('rol', 19), ('add', 0x2E7609BD), ('xor', 0x2C9CD73D)])


def decrypt_player_eyes(hv_value):
    # Game update 2026-09-10 (offsets_decrypts_export.h, player_eyes_decrypt).
    # ROL(13)/ADD/ROL(31)/SUB -- replaces the 2026-09-08 ROL/ADD/ROL/ADD chain.
    return _hv_decrypt(hv_value, [('rol', 13), ('add', 0x7CD29FA9), ('rol', 31), ('sub', 0x6A7D41C2)])


def decrypt_cl_active_item(value):
    # Game update 2026-09-10 (offsets_decrypts_export.h, cl_active_item).
    # ROL(9)/XOR/ROL(10)/SUB -- replaces the 2026-09-08 ROL/XOR/ROL chain
    # (gained a trailing SUB). Note this decrypts the raw handle VALUE
    # directly (no memory read), matching this function's existing signature.
    return _hv_decrypt(value, [('rol', 9), ('xor', 0xBC12F754), ('rol', 10), ('sub', 0x2D51B831)])


def _read_hv_handle(m, wrapper, attempts=3):
    """Read a HiddenValue<T>._handle, gated on ._hasValue at +0x14.

    Reading the encrypted handle at +0x18 while +0x14 is still false is
    indistinguishable from a genuinely-unset field — both read back as 0 — so
    every prior call site treated "not populated yet" the same as "walk
    failed" and burned its retry budget on it. Checking has_value first turns
    that into an explicit not-ready-yet signal.
    """
    if not _valid_user_ptr(wrapper):
        return 0
    for _ in range(max(1, attempts)):
        # _hasValue is advisory ONLY. Gating on it (returning 0 when it reads
        # false) killed the chain outright -- "[WE-DBG] entity chain resolve
        # FAILED" where it had been resolving fine. Cl1kExternal's
        # Decrypt::BaseNetworkableKey reads HvOffset::Wrapper_EncryptedHV
        # (0x18) unconditionally and never looks at 0x14, so the handle is
        # authoritative on this build even when the flag disagrees.
        handle = m.u64_retry(wrapper + OFF.hv_slot, attempts=2)
        if handle:
            return handle
        time.sleep(0.002)
    return 0


def resolve_tagged_handles(m, handles, _ga_base=None):
    """Resolve many IL2CPP handles in two IOCTLs instead of two *each*.

    Same algorithm as resolve_tagged_handle below -- this is the batched form,
    and the two must stay in step. It exists because the single-handle version
    was being called inside a per-player loop in _read_held_items_batch: at two
    `batch_u64` calls apiece and ~20 unmapped players that is the `held=41io`
    /`disp=1190ms` spike in the 2026-08-25 log, and it is the same "driver call
    inside a per-item loop" shape as OFFSET_RECOVERY.md trap #29.

    Returns {handle: resolved_ptr} containing only the handles that resolved.
    """
    plans = []
    for handle in dict.fromkeys(handles):
        if not isinstance(handle, int) or not 0x10000 <= handle <= MAX_USER_ADDR:
            continue
        table_base = handle & 0xFFFFFFFFFFFFE000
        slot_delta = handle - table_base - 40
        if slot_delta < 0 or (slot_delta & 7):
            continue
        plans.append((handle, table_base, slot_delta >> 3))
    if not plans:
        return {}

    meta_addrs = []
    for _, table_base, _ in plans:
        meta_addrs.extend((table_base + 16, table_base + 28, table_base + 32))
    metadata = m.batch_u64(meta_addrs, attempts=2)
    if len(metadata) < len(meta_addrs):
        return {}

    stage2 = []
    slot_addrs = []
    for index, (handle, table_base, slot_index) in enumerate(plans):
        bitmap_ptr = metadata[index * 3]
        table_size = metadata[index * 3 + 1] & 0xFFFFFFFF
        type_flag = metadata[index * 3 + 2] & 0xFF
        if (
            type_flag >= 4
            or slot_index >= table_size
            or not 0x10000 <= bitmap_ptr <= MAX_USER_ADDR
        ):
            continue
        slot_addr = table_base + 8 * (slot_index + 5)
        stage2.append((handle, slot_index, type_flag))
        slot_addrs.extend(
            (bitmap_ptr + 4 * (slot_index >> 5), slot_addr)
        )
    if not stage2:
        return {}

    values = m.batch_u64(slot_addrs, attempts=2)
    if len(values) < len(slot_addrs):
        return {}

    out = {}
    for index, (handle, slot_index, type_flag) in enumerate(stage2):
        bitmap_word = values[index * 2] & 0xFFFFFFFF
        if not ((bitmap_word >> (slot_index & 0x1F)) & 1):
            continue
        raw = values[index * 2 + 1]
        # type_flag<=1 uses the "inverted" encoding, and the invert is only
        # over the low 32 bits -- see the note on resolve_tagged_handle.
        resolved = raw if type_flag > 1 else (~raw & 0xFFFFFFFF)
        if 0x10000 <= resolved <= MAX_USER_ADDR:
            out[handle] = resolved
    return out


def resolve_tagged_handle(m, handle, _ga_base=None):
    """Resolve an IL2CPP handle slot without executing code in the target.

    Current IL2CPP handles point inside an 0x2000-byte table page. The page
    header supplies its bitmap, slot count and direct/inverted storage type.
    """
    if not isinstance(handle, int) or not 0x10000 <= handle <= MAX_USER_ADDR:
        return 0

    table_base = handle & 0xFFFFFFFFFFFFE000
    slot_delta = handle - table_base - 40
    if slot_delta < 0 or (slot_delta & 7):
        return 0
    slot_index = slot_delta >> 3

    metadata = m.batch_u64(
        [table_base + 16, table_base + 28, table_base + 32],
        attempts=2,
    )
    if len(metadata) != 3:
        return 0
    bitmap_ptr = metadata[0]
    table_size = metadata[1] & 0xFFFFFFFF
    type_flag = metadata[2] & 0xFF
    if (
        type_flag >= 4
        or slot_index >= table_size
        or not 0x10000 <= bitmap_ptr <= MAX_USER_ADDR
    ):
        return 0

    slot_addr = table_base + 8 * (slot_index + 5)
    values = m.batch_u64(
        [bitmap_ptr + 4 * (slot_index >> 5), slot_addr],
        attempts=2,
    )
    if len(values) != 2:
        return 0
    bitmap_word = values[0] & 0xFFFFFFFF
    if not ((bitmap_word >> (slot_index & 0x1F)) & 1):
        return 0

    # type_flag<=1 uses the "inverted" encoding, but the invert is only over
    # the low 32 bits (~Read<uint32_t>), not the full 64-bit slot value —
    # inverting all 64 bits here produced a garbage handle for that path.
    resolved = values[1] if type_flag > 1 else (~values[1] & 0xFFFFFFFF)
    return resolved if 0x10000 <= resolved <= MAX_USER_ADDR else 0


# ---------------------------------------------------------------------------
# Module finder (PEB LDR walk + Toolhelp fallback)
# ---------------------------------------------------------------------------
def _valid_user_ptr(addr):
    return isinstance(addr, int) and 0x10000 < addr < MAX_USER_ADDR


def _read_remote_unicode(m, entry, unicode_string_off, max_len):
    length = m.u32(entry + unicode_string_off) & 0xFFFF
    buffer = m.u64(entry + unicode_string_off + 8)
    if not _valid_user_ptr(buffer) or length <= 0 or length > max_len:
        return ""
    try:
        return m.read(buffer, length).decode('utf-16le', errors='ignore')
    except Exception:
        return ""


def iter_peb_modules(m):
    """Yield (base, base_name, full_name, source) from the target PEB LDR lists."""
    peb = m.shared.peb_address
    if not _valid_user_ptr(peb):
        return

    ldr = m.u64(peb + 0x18)
    if not _valid_user_ptr(ldr):
        return

    lists = (
        ("peb:load", 0x10, 0x00),
        ("peb:memory", 0x20, 0x10),
        ("peb:init", 0x30, 0x20),
    )
    seen_entries = set()

    for source, head_off, link_off in lists:
        list_head = ldr + head_off
        cur = m.u64(list_head)
        seen_links = set()

        for _ in range(512):
            if not _valid_user_ptr(cur) or cur == list_head or cur in seen_links:
                break
            seen_links.add(cur)

            entry = cur - link_off
            if entry in seen_entries:
                cur = m.u64(cur)
                continue
            seen_entries.add(entry)

            base_addr = m.u64(entry + 0x30)
            full_name = _read_remote_unicode(m, entry, 0x48, 1024)
            base_name = _read_remote_unicode(m, entry, 0x58, 512)
            if _valid_user_ptr(base_addr) and (base_name or full_name):
                yield base_addr, base_name, full_name, source

            cur = m.u64(cur)


def iter_toolhelp_modules(pid):
    """Yield (base, base_name, full_name, source) using Windows Toolhelp when allowed."""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, int(pid))
    if not snap or snap == INVALID_HANDLE_VALUE:
        return

    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(MODULEENTRY32W)
        if not kernel32.Module32FirstW(snap, ctypes.byref(entry)):
            return

        while True:
            base = ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0
            yield base, entry.szModule, entry.szExePath, "toolhelp"
            entry.dwSize = ctypes.sizeof(MODULEENTRY32W)
            if not kernel32.Module32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snap)


def _read_c_string(m, addr, max_len=260):
    if not _valid_user_ptr(addr):
        return ""
    raw = _read_retry(m, addr, max_len)
    end = raw.find(b'\x00')
    if end >= 0:
        raw = raw[:end]
    try:
        return raw.decode('ascii', errors='ignore')
    except Exception:
        return ""


def _basename(path):
    return (path or "").replace("/", "\\").rsplit("\\", 1)[-1]


def _mapped_file_name(h_proc, addr):
    buf = ctypes.create_unicode_buffer(1024)
    n = psapi.GetMappedFileNameW(h_proc, ctypes.c_void_p(addr), buf, len(buf))
    return buf.value if n else ""


def _read_retry(m, addr, size, attempts=3):
    data = b''
    for _ in range(attempts):
        data = m.read(addr, size)
        if data and any(data):
            return data
        time.sleep(0.002)
    return data


def _read_pe_export_name(m, base):
    headers = _read_retry(m, base, 0x1000)
    if len(headers) < 0x100 or headers[:2] != b'MZ':
        return ""

    e_lfanew = struct.unpack_from('<I', headers, 0x3C)[0]
    if e_lfanew <= 0 or e_lfanew > 0x2000:
        return ""
    if e_lfanew + 0x108 > len(headers):
        headers = _read_retry(m, base, e_lfanew + 0x200)

    if len(headers) < e_lfanew + 0x108 or headers[e_lfanew:e_lfanew + 4] != b'PE\x00\x00':
        return ""

    size_opt = struct.unpack_from('<H', headers, e_lfanew + 20)[0]
    opt = e_lfanew + 24
    if size_opt < 0x70 or len(headers) < opt + size_opt:
        return ""

    magic = struct.unpack_from('<H', headers, opt)[0]
    if magic == 0x20B:
        data_dir = opt + 0x70
    elif magic == 0x10B:
        data_dir = opt + 0x60
    else:
        return ""

    if data_dir + 8 > len(headers):
        return ""

    export_rva, export_size = struct.unpack_from('<II', headers, data_dir)
    if export_rva == 0 or export_size == 0:
        return ""

    export_dir = _read_retry(m, base + export_rva, 40)
    if len(export_dir) < 40:
        return ""

    name_rva = struct.unpack_from('<I', export_dir, 12)[0]
    if name_rva == 0:
        return ""

    return _read_c_string(m, base + name_rva)


def iter_vad_image_modules(m, named_only=True):
    """Yield MEM_IMAGE allocation bases discovered with VirtualQueryEx."""
    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(m.pid))
    if not h_proc:
        return

    try:
        addr = 0
        seen_allocs = set()
        mbi = MEMORY_BASIC_INFORMATION()
        queries = 0

        while addr < MAX_USER_ADDR and queries < 200000:
            queries += 1
            got = kernel32.VirtualQueryEx(
                h_proc,
                ctypes.c_void_p(addr),
                ctypes.byref(mbi),
                ctypes.sizeof(mbi),
            )
            if not got:
                break

            base_addr = mbi.BaseAddress or addr
            region_size = mbi.RegionSize or 0x1000
            alloc_base = mbi.AllocationBase or 0

            if mbi.State == MEM_COMMIT and mbi.Type == MEM_IMAGE and alloc_base not in seen_allocs:
                seen_allocs.add(alloc_base)
                mapped_name = _mapped_file_name(h_proc, alloc_base)
                base_name = _basename(mapped_name)
                source = "vad:map" if mapped_name else "vad:export"
                if not base_name:
                    base_name = _read_pe_export_name(m, alloc_base)
                    mapped_name = base_name
                if base_name or not named_only:
                    yield alloc_base, base_name, mapped_name, source

            next_addr = base_addr + region_size
            addr = next_addr if next_addr > addr else addr + 0x1000
    finally:
        kernel32.CloseHandle(h_proc)


def _module_matches(base_name, full_name, needle):
    needle = needle.lower()
    base_l = (base_name or "").lower()
    full_l = (full_name or "").lower()
    return base_l == needle or full_l.endswith("\\" + needle) or needle in full_l


def find_modules(m):
    """Find GameAssembly.dll and UnityPlayer.dll bases."""
    ga_base = 0
    up_base = 0

    for base_addr, base_name, full_name, _ in iter_peb_modules(m):
        if _module_matches(base_name, full_name, "gameassembly.dll"):
            ga_base = base_addr
        elif _module_matches(base_name, full_name, "unityplayer.dll"):
            up_base = base_addr

    if ga_base and up_base:
        return ga_base, up_base

    for base_addr, base_name, full_name, _ in iter_toolhelp_modules(m.pid):
        if not ga_base and _module_matches(base_name, full_name, "gameassembly.dll"):
            ga_base = base_addr
        elif not up_base and _module_matches(base_name, full_name, "unityplayer.dll"):
            up_base = base_addr

    if ga_base and up_base:
        return ga_base, up_base

    for _ in range(3):
        for base_addr, base_name, full_name, _ in iter_vad_image_modules(m):
            if not ga_base and _module_matches(base_name, full_name, "gameassembly.dll"):
                ga_base = base_addr
            elif not up_base and _module_matches(base_name, full_name, "unityplayer.dll"):
                up_base = base_addr
            if ga_base and up_base:
                break
        if ga_base and up_base:
            break
        time.sleep(0.02)

    return ga_base, up_base


def print_module_diagnostics(m, limit=12):
    peb_modules = list(iter_peb_modules(m))
    toolhelp_modules = list(iter_toolhelp_modules(m.pid))
    vad_modules = list(iter_vad_image_modules(m, named_only=True))
    print(f"[i] PEB: 0x{m.shared.peb_address:X}  modules PEB: {len(peb_modules)}  Toolhelp: {len(toolhelp_modules)}  VAD images nommees: {len(vad_modules)}")

    preview = peb_modules if peb_modules else (toolhelp_modules if toolhelp_modules else vad_modules)
    if not preview:
        print("[i] Aucun module lisible via PEB/Toolhelp/VAD.")
        return

    for base, base_name, full_name, source in preview[:limit]:
        name = base_name or full_name or "<sans nom>"
        print(f"    [{source}] 0x{base:X} {name}")


# ---------------------------------------------------------------------------
# Unity Transform → world position  (Unity 6 engine)
# ---------------------------------------------------------------------------
def quat_mult_vec(q, v):
    x2 = q[0] * 2.0
    y2 = q[1] * 2.0
    z2 = q[2] * 2.0
    xx, yy, zz = q[0] * x2, q[1] * y2, q[2] * z2
    xy, xz, yz = q[0] * y2, q[0] * z2, q[1] * z2
    wx, wy, wz = q[3] * x2, q[3] * y2, q[3] * z2
    
    return (
        (1.0 - (yy + zz)) * v[0] + (xy - wz) * v[1] + (xz + wy) * v[2],
        (xy + wz) * v[0] + (1.0 - (xx + zz)) * v[1] + (yz - wx) * v[2],
        (xz - wy) * v[0] + (yz + wx) * v[1] + (1.0 - (xx + yy)) * v[2]
    )

def get_transform_pos(m, transform_ptr):
    """Read world position from a Unity Transform component by iterating hierarchy."""
    if not transform_ptr:
        return None
    internal = m.u64_retry(transform_ptr + OFF.bone_transform)
    if not internal:
        return None

    td = m.u64_retry(internal + OFF.transform_access)
    idx = m.u32_retry(internal + OFF.transform_access + 8)
    if not td:
        return None

    # offsets for TransformData
    localTransforms = m.u64_retry(td + 0x18)
    parentIndices = m.u64_retry(td + 0x20)
    
    if not localTransforms or not parentIndices:
        return None

    def read_trsX(i):
        addr = localTransforms + i * 0x30
        data = m.read(addr, 0x30)
        if not data or len(data) < 0x30:
            return None
        t = struct.unpack('<3f', data[0x0:0xC])
        q = struct.unpack('<4f', data[0x10:0x20])
        s = struct.unpack('<3f', data[0x20:0x2C])
        return t, q, s

    trs = read_trsX(idx)
    if not trs: return None
    
    worldPos = trs[0]
    
    # Read parent index
    data_pidx = m.read(parentIndices + idx * 4, 4)
    if not data_pidx or len(data_pidx) < 4:
        return worldPos
    p_idx = struct.unpack('<i', data_pidx)[0]
    
    loops = 0
    while p_idx >= 0 and loops < 50:
        p_trs = read_trsX(p_idx)
        if not p_trs: break
        
        pt, pq, ps = p_trs
        
        # Scale: worldPos = worldPos * p.s
        worldPos = (worldPos[0] * ps[0], worldPos[1] * ps[1], worldPos[2] * ps[2])
        
        # Rotate: worldPos = p.q * worldPos
        worldPos = quat_mult_vec(pq, worldPos)
        
        # Translate: worldPos = worldPos + p.t
        worldPos = (worldPos[0] + pt[0], worldPos[1] + pt[1], worldPos[2] + pt[2])
        
        data_pidx = m.read(parentIndices + p_idx * 4, 4)
        if not data_pidx or len(data_pidx) < 4:
            break
        p_idx = struct.unpack('<i', data_pidx)[0]
        loops += 1
        
    return worldPos


# ---------------------------------------------------------------------------
# Diagnostic: scan PlayerModel structure for valid ptrs / candidate positions
# ---------------------------------------------------------------------------
def _diag_pm_structure(m, pm):
    import sys
    def _p(*a): print(*a, file=sys.stderr)

    _p(f"\n[DIAG] PlayerModel=0x{pm:X}")

    block = m.read(pm, 0x500)
    if len(block) < 0x400:
        _p("[DIAG] failed to read PM block"); return

    # ── A. Step-by-step trace: SkinnedMultiMesh bone chain ─────────────────
    _p("\n[DIAG-A] SkinnedMultiMesh bone chain (step by step):")
    smm = m.u64(pm + 0x3D0)
    _p(f"  PM+0x3D0 = SMM=0x{smm:X}")
    if _valid_user_ptr(smm):
        bt = m.u64(smm + 0x50)
        _p(f"  SMM+0x50 = boneArr=0x{bt:X}")
        if _valid_user_ptr(bt):
            cnt = m.u32(bt + 0x18)
            _p(f"  boneArr+0x18 = count={cnt}")
            # try element 0 at both +0x20 (SZArray) and +0x10 (header skip)
            for elem_off in (0x20, 0x10):
                t = m.u64(bt + elem_off)
                _p(f"  boneArr+0x{elem_off:X} = t=0x{t:X}")
                if _valid_user_ptr(t):
                    # trace get_transform_pos steps
                    step1 = m.u64(t + 0x10)
                    _p(f"    t+0x10 (m_CachedPtr)=0x{step1:X}")
                    if _valid_user_ptr(step1):
                        td  = m.u64(step1 + 0x28)
                        idx = m.u32(step1 + 0x30)
                        _p(f"    m_CachedPtr+0x28 (td)=0x{td:X}  idx={idx}")
                        if _valid_user_ptr(td):
                            parr = m.u64(td + 0x18)
                            _p(f"    td+0x18 (pos_arr)=0x{parr:X}")
                            if _valid_user_ptr(parr):
                                slot = parr + 0x30 * idx
                                pos = m.vec3f(slot)
                                _p(f"    pos_arr+0x30*{idx}=0x{slot:X} → {pos}")
                            else:
                                # try alternative td layout: pos at td+0x10
                                parr2 = m.u64(td + 0x10)
                                _p(f"    td+0x10 alt=0x{parr2:X}")
                                if _valid_user_ptr(parr2):
                                    pos2 = m.vec3f(parr2 + 0x30 * idx)
                                    _p(f"    alt pos → {pos2}")

    # ── B. Try PM+0x10 as back-ref to BasePlayer ─────────────────────────
    _p("\n[DIAG-B] PM+0x10 back-ref chain (MonoBehaviour → BP → Model → bone):")
    ref10 = m.u64(pm + 0x10)
    _p(f"  PM+0x10=0x{ref10:X}")
    if _valid_user_ptr(ref10):
        # try as BasePlayer directly: BP+0x1A8=Model
        model_ptr = m.u64(ref10 + 0x1A8)
        _p(f"  ref10+0x1A8 (Model)=0x{model_ptr:X}")
        if _valid_user_ptr(model_ptr):
            rb = m.u64(model_ptr + 0x28)
            _p(f"  Model+0x28 (rootBone)=0x{rb:X}")
            if _valid_user_ptr(rb):
                pos = get_transform_pos(m, rb)
                _p(f"  get_transform_pos(rootBone) → {pos}")
        # try as GameObject: +0x30 → Component array
        go_comps = m.u64(ref10 + 0x30)
        _p(f"  ref10+0x30 (go_comps?)=0x{go_comps:X}")

    # ── C. Plausible Vec3 in PM block (relaxed — any Y > -500) ──────────
    _p("\n[DIAG-C] Plausible Vec3 candidates in PM block (|x|<8000 |z|<8000 -500<y<3000):")
    found_any = False
    for off in range(0, len(block) - 11, 4):
        x, y, z = struct.unpack_from('<fff', block, off)
        if (abs(x) > 1.0 and abs(x) < 8000
                and abs(z) < 8000 and -500 < y < 3000
                and not (x == 0.0 and y == 0.0 and z == 0.0)):
            _p(f"  PM+0x{off:X}: ({x:.2f},{y:.2f},{z:.2f})")
            found_any = True
    if not found_any:
        _p("  (none)")

    # ── D. Scan ptrs reachable from PM for Vec3 (1-hop from valid ptrs) ──
    _p("\n[DIAG-D] Vec3 scan 1-hop from PM valid ptrs (first 12 ptrs):")
    hop_checked = 0
    for off in range(0, len(block) - 7, 8):
        v = struct.unpack_from('<Q', block, off)[0]
        if not _valid_user_ptr(v):
            continue
        hop_checked += 1
        if hop_checked > 12:
            break
        sub = m.read(v, 0x60)
        if len(sub) < 12:
            continue
        for soff in range(0, len(sub) - 11, 4):
            x, y, z = struct.unpack_from('<fff', sub, soff)
            if (abs(x) > 10.0 and abs(x) < 8000
                    and abs(z) < 8000 and -500 < y < 3000
                    and not (x == 0.0 and y == 0.0 and z == 0.0)):
                _p(f"  PM+0x{off:X}→0x{v:X}+0x{soff:X}: ({x:.2f},{y:.2f},{z:.2f})")

    # ── E. Raw bytes around position fields ──────────────────────────────
    _p("\n[DIAG-E] Raw floats at 0x2F8-0x380:")
    for off in range(0x2F8, min(0x380, len(block) - 3), 4):
        v = struct.unpack_from('<f', block, off)[0]
        _p(f"  +0x{off:X}: {v:.4f}")

    # ── F. Raw bytes at PM+0x98 neighborhood ─────────────────────────────
    _p("\n[DIAG-F] Raw at PM+0x98 (NeoRed pm_rootBone):")
    for off in range(0x90, min(0xD0, len(block) - 7), 8):
        q = struct.unpack_from('<Q', block, off)[0]
        _p(f"  +0x{off:X}: 0x{q:X}")


# ---------------------------------------------------------------------------
# Camera: read ViewProjection matrix from MainCamera singleton
# ---------------------------------------------------------------------------
# Cached camera chain pointers — resolved once, reused every tick.
_cam_native_cache = 0
_cam_chain_miss = 0
_cam_proj_cache = None
_cam_proj_native = 0
_cam_proj_refresh_at = 0.0

def _resolve_cam_chain(m, ga_base):
    """Resolve klass → sf → cam_obj → native_cam. Returns native_cam or 0."""
    mc_klass = m.u64(ga_base + OFF.MainCamera_c)
    if mc_klass < 0x10000:
        return 0
    sf = m.u64(mc_klass + OFF.klass_static_fields)
    if sf < 0x10000:
        return 0
    cam_obj = m.u64(sf + OFF.mainCamera)
    if cam_obj < 0x10000:
        return 0
    native_cam = m.u64(cam_obj + OFF.m_CachedPtr)
    if native_cam < 0x10000:
        return 0
    return native_cam

def read_view_proj_matrix(m, ga_base):
    """
    MainCamera: klass → static_fields → [+0x10] Camera* → [+0x10] native cam
    Current dump: instance=0x10, nativeCachedPtr=0x10
    Cached: resolves chain once, then just reads 128 bytes of matrices per tick.
    """
    global _cam_native_cache, _cam_chain_miss
    global _cam_proj_cache, _cam_proj_native, _cam_proj_refresh_at

    native_cam = _cam_native_cache

    # Re-resolve chain if no cache or after repeated matrix failures
    if not native_cam or _cam_chain_miss > 3:
        native_cam = _resolve_cam_chain(m, ga_base)
        if not native_cam:
            _cam_chain_miss += 1
            return None
        _cam_native_cache = native_cam
        _cam_chain_miss = 0

    # Projection/FOV changes rarely, while the view matrix changes every camera
    # frame. Refresh projection at 2 Hz and read only the 64-byte view matrix in
    # between. The priority transaction guarantees that projection uses a fresh
    # camera sample instead of a stale matrix left behind by entity batches.
    now = time.perf_counter()
    if _cam_proj_native != native_cam:
        _cam_proj_cache = None
        _cam_proj_native = native_cam

    refresh_projection = (
        _cam_proj_cache is None
        or now >= _cam_proj_refresh_at
    )
    if refresh_projection:
        block = m.read_priority(native_cam + OFF.cam_projMatrix, 0x1B0)
        if block is None or len(block) != 0x1B0:
            _cam_chain_miss += 1
            return None
        pm = struct.unpack('<16f', block[0:64])
        vm = struct.unpack('<16f', block[0x170:0x170 + 64])
        _cam_proj_cache = pm
        _cam_proj_refresh_at = now + CAM_PROJECTION_REFRESH_INTERVAL
    else:
        vm_raw = m.read_priority(native_cam + OFF.cam_viewMatrix, 64)
        if vm_raw is None or len(vm_raw) != 64:
            _cam_chain_miss += 1
            return None
        vm = struct.unpack('<16f', vm_raw)
        pm = _cam_proj_cache

    # Quick sanity: check BOTH matrices' diagonals aren't all zero
    # (driver returns zeros on failed reads → P*V = 0 → flickering)
    if vm[0] == 0.0 and vm[5] == 0.0 and vm[10] == 0.0:
        _cam_chain_miss += 1
        return None
    if pm[0] == 0.0 and pm[5] == 0.0 and pm[10] == 0.0:
        _cam_chain_miss += 1
        return None

    _cam_chain_miss = 0

    # Multiply: VP = P * V  (column-major)
    vp = [0.0] * 16
    for row in range(4):
        for col in range(4):
            s = 0.0
            for k in range(4):
                s += pm[row + k * 4] * vm[k + col * 4]
            vp[row + col * 4] = s

    # Final VP sanity: reject all-zero result
    if vp[0] == 0.0 and vp[5] == 0.0 and vp[10] == 0.0 and vp[15] == 0.0:
        return None

    # Camera world position from view-matrix inversion (column-major).
    # WARNING : this path is UNRELIABLE on this Rust build. Empirical
    # test showed it returns ~(0, 0, -1) instead of real world coords.
    # We tried two paths and both fail :
    #   1. C = -R^T * T from the view matrix : gives ~(0, 0, -1),
    #      Rust's Camera.viewMatrix doesn't hold translation in the
    #      column expected by the inversion identity.
    #   2. Direct read at native_cam + 0x444 (cl1kexternal's
    #      WorldPositionRead) : gives the camera's local forward
    #      vector (0, 0, -1), not the world position.
    # The aim controller SHOULD use local_pos + eye offset instead of
    # this value. We keep the inversion for backward compat with view.py
    # distance displays (which fall back to local_pos anyway).
    tx, ty, tz = vm[12], vm[13], vm[14]
    cx = -(vm[0] * tx + vm[1] * ty + vm[2] * tz)
    cy = -(vm[4] * tx + vm[5] * ty + vm[6] * tz)
    cz = -(vm[8] * tx + vm[9] * ty + vm[10] * tz)
    cam_pos = (cx, cy, cz)

    # Camera basis vectors in WORLD space. R's rows in world -> camera
    # direction are vm[0..3], vm[4..7], vm[8..11]. R^T rotates camera-local
    # unit vectors into world; its columns = R's rows.
    #
    # NOTE row 2 is NOT negated. The old comment here claimed "ESP
    # world_to_screen works with the same vm, so its rotation encoding IS
    # the standard column-major Unity convention (-Z forward)" -- that's
    # a false inference: world_to_screen only ever uses the full vp
    # product (pm @ vm), never these extracted rows in isolation, so it
    # can't validate this row's sign. Live [AIM-DBG] telemetry (v2 aim,
    # 2026-09-05) caught the real bug: pitch (atan2(along_up, horiz), where
    # horiz=sqrt(along_right^2+along_forward^2) is sign-blind to forward)
    # stayed small and sane every tick, while yaw (atan2(along_right,
    # along_forward), sign-sensitive) reported ~179 deg for a target only
    # 18.8px from the crosshair -- textbook "180 - true_yaw" from a
    # negated along_forward. This native struct is read directly out of
    # engine memory (not the managed Camera.worldToCameraMatrix property,
    # which Unity docs guarantee is always OpenGL/-Z convention), so the
    # assumed -Z negation doesn't hold for it on this build.
    cam_right   = (vm[0], vm[4], vm[8])
    cam_up      = (vm[1], vm[5], vm[9])
    cam_forward = (vm[2], vm[6], vm[10])

    return vp, cam_pos, cam_right, cam_up, cam_forward


class CameraSampler:
    """Sample the game camera without ever blocking the render thread."""

    def __init__(self, memory, ga_base, sample_hz):
        self._memory = memory
        self._ga_base = ga_base
        self._period = 1.0 / max(1.0, float(sample_hz))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._latest = None
        self._thread = None
        # [CAM-HZ] instrumentation: the achieved sample rate (and, more
        # importantly, the worst single gap between two successful samples)
        # was previously only ever estimated from a code comment
        # ("~48-62Hz"), never actually measured. A player-reported "camera
        # feels like 10Hz when I move" needs this measured, not guessed at.
        self._hz_samples = 0
        self._hz_gap_sum = 0.0
        self._hz_gap_max = 0.0
        self._hz_gap_n = 0
        self._hz_last_sample_at = 0.0
        self._hz_window_start = time.perf_counter()
        self._hz_io_wait_sum = 0.0
        self._hz_lock_wait_sum = 0.0
        # Per-thread, like every other counter on Mem: how many of this
        # thread's reads missed _wait's spin budget. `io=` alone cannot say
        # whether a 10ms average is one slow call in ten or ten mediocre
        # ones, and only the first is fixable by widening the budget.
        self._hz_slow = 0

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="esp-camera-sampler",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.25)

    def get_latest(self):
        with self._lock:
            return self._latest

    def _run(self):
        next_sample_at = time.perf_counter()
        while not self._stop.is_set():
            io_wait0 = self._memory.io_wait_s
            lock_wait0 = self._memory.lock_wait_s
            slow0 = self._memory.io_slow_calls
            result = read_view_proj_matrix(self._memory, self._ga_base)
            sampled_at = time.perf_counter()
            self._hz_io_wait_sum += self._memory.io_wait_s - io_wait0
            self._hz_lock_wait_sum += self._memory.lock_wait_s - lock_wait0
            self._hz_slow += self._memory.io_slow_calls - slow0
            if result is not None:
                vp, cam_pos, *_ = result  # basis vectors are for aim only
                sample = (tuple(vp), tuple(cam_pos), sampled_at)
                with self._lock:
                    self._latest = sample
                self._hz_samples += 1
                if self._hz_last_sample_at:
                    gap = sampled_at - self._hz_last_sample_at
                    self._hz_gap_sum += gap
                    self._hz_gap_n += 1
                    if gap > self._hz_gap_max:
                        self._hz_gap_max = gap
                self._hz_last_sample_at = sampled_at

            if sampled_at - self._hz_window_start >= CAM_HZ_LOG_INTERVAL:
                elapsed = sampled_at - self._hz_window_start
                hz = self._hz_samples / elapsed if elapsed > 0 else 0.0
                avg_gap_ms = (
                    self._hz_gap_sum / self._hz_gap_n * 1000.0
                    if self._hz_gap_n else 0.0
                )
                denom = self._hz_samples or 1
                avg_io_ms = self._hz_io_wait_sum / denom * 1000.0
                avg_lock_ms = self._hz_lock_wait_sum / denom * 1000.0
                print(
                    f"[CAM-HZ] {hz:.1f}Hz samples={self._hz_samples} "
                    f"avg_gap={avg_gap_ms:.1f}ms "
                    f"max_gap={self._hz_gap_max * 1000.0:.1f}ms "
                    f"(io={avg_io_ms:.1f}ms lock={avg_lock_ms:.1f}ms "
                    f"slow={self._hz_slow})",
                    flush=True,
                )
                self._hz_samples = 0
                self._hz_gap_sum = 0.0
                self._hz_gap_max = 0.0
                self._hz_gap_n = 0
                self._hz_io_wait_sum = 0.0
                self._hz_lock_wait_sum = 0.0
                self._hz_slow = 0
                self._hz_window_start = sampled_at

            next_sample_at += self._period
            wait_for = next_sample_at - time.perf_counter()
            if wait_for <= 0.0:
                # Give the tick and slow lane a real chance to acquire the
                # shared driver after a slow camera transaction. See
                # CAMERA_MIN_IDLE_GAP: the read itself (~15ms) far exceeds
                # any achievable sample period, so this branch fires almost
                # every cycle and this constant, not sample_hz, sets the
                # camera's actual duty cycle on _drv_lock.
                next_sample_at = time.perf_counter()
                wait_for = CAMERA_MIN_IDLE_GAP
            self._stop.wait(wait_for)


# ---------------------------------------------------------------------------
# World-to-Screen using VP matrix
# ---------------------------------------------------------------------------
def w2s(pos, vp, sw, sh):
    """WorldToScreen using a pre-multiplied ViewProjection matrix (column-major)."""
    x = pos[0] * vp[0] + pos[1] * vp[4] + pos[2] * vp[8]  + vp[12]
    y = pos[0] * vp[1] + pos[1] * vp[5] + pos[2] * vp[9]  + vp[13]
    w = pos[0] * vp[3] + pos[1] * vp[7] + pos[2] * vp[11] + vp[15]
    if w < 0.001:
        return None
    nx = x / w
    ny = y / w
    sx = (sw / 2.0) * (1.0 + nx)
    sy = (sh / 2.0) * (1.0 - ny)
    if sx < -200 or sx > sw + 200 or sy < -200 or sy > sh + 200:
        return None
    return (sx, sy)


def calculate_box(world_pos, vp, sw, sh, player_height=1.75):
    """Calculate 3D perspective-projected screen bounding box (left, top, right, bottom)."""
    feet_world = world_pos
    head_world = (world_pos[0], world_pos[1] + player_height, world_pos[2])

    feet_screen = w2s(feet_world, vp, sw, sh)
    head_screen = w2s(head_world, vp, sw, sh)

    if feet_screen is None and head_screen is None:
        return None

    if feet_screen and head_screen:
        box_h = abs(feet_screen[1] - head_screen[1])
        center_x = (feet_screen[0] + head_screen[0]) * 0.5
        top = min(head_screen[1], feet_screen[1])
    elif feet_screen:
        center_x, cy = feet_screen
        box_h = 30.0
        top = cy - box_h
    else:
        center_x, cy = head_screen
        box_h = 30.0
        top = cy

    box_h = max(4.0, min(box_h, 350.0))
    box_w = max(2.0, min(box_h * 0.45, 160.0))
    half_w = box_w * 0.5

    left = center_x - half_w
    right = center_x + half_w
    bottom = top + box_h

    return left, top, right, bottom


def calculate_sleeping_box(world_pos, vp, sw, sh, player_height=1.75):
    """Calculate a wide, ground-hugging box for a sleeping player."""
    standing_box = calculate_box(world_pos, vp, sw, sh, player_height)
    feet_screen = w2s(world_pos, vp, sw, sh)
    if standing_box is None or feet_screen is None:
        return standing_box

    _, standing_top, _, standing_bottom = standing_box
    standing_height = max(4.0, standing_bottom - standing_top)

    body_width = max(12.0, min(standing_height * 0.95, 320.0))
    body_height = max(6.0, min(standing_height * 0.28, 90.0))
    center_x, ground_y = feet_screen

    left = center_x - body_width * 0.5
    right = center_x + body_width * 0.5
    bottom = ground_y + body_height * 0.12
    top = bottom - body_height
    return left, top, right, bottom


# ---------------------------------------------------------------------------
# Game state
# ---------------------------------------------------------------------------
DEBUG_PLAYERS = True  # TEMP: bone-anatomy diagnostic — set back to False once the skeleton bug is found
DEBUG_LOG_INTERVAL = 1.0
ENABLE_BP_MAPPING = True   # Enable fast direct PM->BP mapping to activate Path A (real-time visual model Transform)
ENABLE_BP_DEEP_SCAN = False  # Disabled
ENABLE_HEAVY_DIAG = False  # Disabled
# The two brute-force world-entity offset scanners below. They found the native
# transform chain (traps #12-#15) and that chain is now settled -- [WE-POS]
# reports native_resolved == pos_resolved == matched -- so every later run just
# paid their IOCTLs to print `candidates=[]`. Kept, not deleted: they are the
# tool to reach for if entity positions ever break again on a new build.
ENABLE_WE_OFFSET_SCAN = False
# _wait() tuning -- rewritten 2026-08-27 after measuring, rather than
# assuming, what a driver round trip costs. Three numbers, all measured:
#
#   * the driver itself answers in 30us mean / 34us p99
#     (bench_event_latency.py CMD_PING, PASS);
#   * one ctypes shared-field read costs 0.030us here, so the old
#     _WAIT_SPIN_ITERS = 1500 spin covered *45 microseconds* of waiting --
#     barely more than the driver's own best case, so anything heavier than a
#     ping fell straight through it;
#   * what it fell through into was `time.sleep(0)`, and that is the single
#     most expensive line this file ever had. It releases the GIL, and
#     reacquiring it is bounded by sys.getswitchinterval(). At the 5 ms
#     default, with two CPU-bound Python threads running (i.e. exactly our
#     shape: worker tick + render loop), one time.sleep(0) measured
#     p50=0.000ms p90=29.8ms p99=63.0ms mean=5.6ms. At 0.5 ms it measured
#     p90=0.015ms.
#
# That is the whole story behind the flat "~15 ms per IOCTL" in
# [TICK-LATENCY], [CAM-HZ] and [POS-PROFILE]. bench_driver.py sees the same
# thing from the other side: the cost is *bimodal* -- ~0.01 ms when the spin
# happens to catch the completion and ~15 ms when it does not, with nothing
# in between -- and it does not scale with batch size at all (n=100 and
# n=1000 both land on 15.1 ms). A per-call cost that ignores the amount of
# work asked for is not the work; it is a missed wakeup.
#
# So phase 1 now spins on a wall clock instead of an iteration count, and
# there is no longer a phase that calls time.sleep(0). Spinning holds the GIL
# on purpose: nothing can take the wakeup away from us.
#
# 2026-08-27, in-game, with a fixed 1.5 ms budget: `slow=` came back at
# *100%* -- 2 of 2 IOCTLs per tick, 32 of 34 camera samples, every window,
# with io still pinned at 15.4 ms each. So the completion genuinely arrives
# ~15 ms after the command is posted; the client was not missing a fast
# answer, there was no fast answer to miss. That is the driver's polling
# cadence on the other side of the shared memory (the event-based wake path
# was removed from this build), and no client-side wait can shorten it.
#
# Which makes a fixed budget wrong in *both* directions: 1.5 ms of spinning
# per IOCTL on three threads is pure waste against a polling driver, and a
# small fixed budget would throw away the event-based driver's 30 us answer
# if it ever comes back. So the budget tunes itself against whatever driver
# is actually loaded: it grows when the spin catches completions and decays
# when it does not. [TICK-LATENCY] prints it as `spin=`.
_WAIT_SPIN_MIN = 0.00005
_WAIT_SPIN_MAX = 0.002
_WAIT_SPIN_START = 0.0015
# A caught completion at time t raises the budget towards 2t (headroom for
# jitter); a miss decays it by this factor. Decay is slow enough that one
# unlucky call does not blind us to a driver that is usually fast, and fast
# enough that a polling driver stops being spun for within a second or two.
_WAIT_SPIN_DECAY = 0.9
# An answer that arrived within this counts as "close enough that a wider
# spin might have caught it", and the budget probes upward. It has to be
# comfortably wider than _WAIT_SPIN_MAX plus a backoff sleep, because once
# we are sleeping we no longer measure the driver -- we measure the sleep.
# A 200us answer noticed on the first ~1ms wake reads as 1ms, and a rule
# that grew from `elapsed * 2` would then decide 2ms was not enough and
# decay, locking the budget at the floor forever. So past the spin, grow
# geometrically on the fact that an answer came back soon, not on a number
# the sleep granularity already destroyed.
_WAIT_SPIN_PROBE_LIMIT = 0.004
# Field reads between two perf_counter() calls. perf_counter costs ~2x a
# field read, so checking it every iteration would spend most of the budget
# on reading the clock instead of the flag.
_WAIT_SPIN_CHECK_EVERY = 32
# CPython hands the GIL from one thread to another at most once per switch
# interval, so that interval is a floor on how fast any thread can react to
# something another thread (or the driver) did. Three latency-critical
# threads share this process -- worker tick, camera sampler, render loop --
# and the default 5 ms is far longer than any of the work they hand off. See
# the measurement above; this buys ~2000x on the tail for a few more context
# switches per second.
GIL_SWITCH_INTERVAL = 0.0005
# How long a sampled skeleton may be re-anchored onto a fresher position before
# it is dropped. This used to be a bare 0.25 s, which silently became "no
# skeletons at all" the moment a tick took longer than that -- and ticks
# routinely take 100-770 ms here. The floor is what the *current* tick rate can
# actually deliver: a skeleton sampled last tick must survive until this tick,
# or nothing is ever drawn. See OFFSET_RECOVERY.md trap #24.
BONE_REANCHOR_TTL_MIN = 0.25
BONE_REANCHOR_TTL_MAX = 1.20
# Beyond this the re-anchored pose is too old to be worth showing: it is the
# right *shape* but the wrong *action*, which is the "bones frozen in a
# position" the user reported. Fade it out rather than pretending it is live.
BONE_REANCHOR_TICKS = 2.5
# A drawn skeleton whose sample is older than this has visibly stopped
# animating. The tick is ~6 ms, so a healthy sample is under 10 ms old --
# 0.25 s is ~40 ticks without a resample, well past "one unlucky tick".
BONE_AGE_STALE_S = 0.25
# [BONE-AGE] prints on this cadence unconditionally, healthy or not. A gate
# that only speaks when it already suspects a problem cannot establish the
# baseline a reader needs to recognise the problem.
BONE_AGE_LOG_INTERVAL = 2.0
# Per-bone extrapolation speed ceiling, m/s. A limb genuinely moves faster
# than the body -- a sprinting arm swing is several m/s at the hand -- but a
# bone whose sample is noisy would fling off at absurd speed and read as a
# broken skeleton. Anything above this falls back to the body's own motion for
# that bone, which is always safe. See OFFSET_RECOVERY.md trap #47.
BONE_EXTRAPOLATION_MAX_SPEED = 14.0
# Hard ceiling on how far a limb residual may be projected, as a multiple of
# the interval it was measured over. The body's velocity is smoothed and
# engine-backed, so it earns the full MAX_PLAYER_PREDICTION; a limb residual is
# a raw two-sample difference with no smoothing at all, and prediction_time
# (<=0.12s) over dt_snap (~0.06s) was scaling it by up to 2.0 -- doubling every
# bit of sample noise. That is the trembling the user reported the moment
# per-limb extrapolation shipped. 1.0 means "never claim a limb kept moving
# longer than we actually watched it move". See trap #48.
BONE_LIMB_SCALE_MAX = 1.0
# Squared metres. How far a bone's offset-from-body may move between two
# snapshots before the blend is refused. A real limb covers well under this in
# 60 ms; anything larger is a rig swap or a bad sample, and blending towards it
# would smear the skeleton across the gap instead of snapping cleanly.
BONE_POSE_BLEND_MAX_SQ = 4.0

# Fallback once the spin budget is exhausted: a real sleep, so a genuinely
# slow (or dead) driver does not pin a core for the full 500 ms deadline.
# 0.0005 rather than the old 0.0002 because the measured cost of a short
# sleep here is ~0.6 ms either way ([TIMER] prints it every run) -- asking
# for less than the timer can deliver only misreports what we are doing.
_WAIT_BACKOFF_SECONDS = 0.0005
WORKER_HZ = 120.0
WORKER_DT = 1.0 / WORKER_HZ
HEALTH_REFRESH_INTERVAL = 1.0
TICK_LOG_INTERVAL = 1.0
# A tick this slow is a visible freeze: the whole overlay -- box, name, health
# and skeleton alike -- holds its last pose for the duration, then snaps. The
# cadence print above cannot catch one (it samples ~1 tick in 120), so a slow
# tick prints on its own, tagged SLOW, throttled separately so a sustained
# stall reports without flooding.
TICK_SLOW_MS = 50.0
TICK_SLOW_LOG_INTERVAL = 0.5
POSE_FREEZE_LOG_INTERVAL = 1.0
# Consecutive _resolve_pm_to_bp walks a pm must survive unmapped before
# _log_pm2bp_stuck prints its one-shot diagnostic. 3 filters out the
# ordinary one-walk convergence delay every join/reconnect pays; a pm still
# missing after 3 walks in a row is not spawn latency.
PM2BP_STUCK_STREAK = 3
# Throttle for [BONE-PHASE] -- see _track_bone_phase_mix. Named for what
# it originally hunted (a *mix* of blended and raw bones inside one
# skeleton); that mix is now impossible by construction, and what the
# tag reports is the price of making it impossible.
BONE_PHASE_MIX_LOG_INTERVAL = 1.0
# A directly connected pair of joints further apart than this is not a
# pose, it is a bone that left the body. Real segments (forearm, shin)
# top out around 0.45 m on a 1.8 m player, so 1.0 m has a wide margin
# and still catches anything visible. Same threshold [BONE-LINK] uses
# at composition time, so the two are directly comparable.
BONE_STRETCH_LIMIT_M = 1.0
# One player per N render-frame-players, round-robin. See
# _track_bone_stretch for why this is sampled and not exhaustive.
BONE_STRETCH_SAMPLE_EVERY = 20
BONE_STRETCH_LOG_INTERVAL = 1.0
# Throttle for CameraSampler's [CAM-HZ] diagnostic -- see the class.
CAM_HZ_LOG_INTERVAL = 1.0
# Throttle for [SNAP-PROFILE]. get_snapshot() runs once per *rendered*
# frame -- 144x a second against a worker tick an order of magnitude
# slower -- and re-blends every bone of every player each time, yet it
# was the only hot path in this program with no instrument on it at
# all: [RENDER-PROFILE] times the drawing, [TICK-LATENCY] the worker,
# and neither covers this. Measure it before assuming either way.
SNAP_PROFILE_LOG_INTERVAL = 1.0
# Weight for the EWMA that smooths get_snapshot()'s alpha denominator across
# several real snapshot boundaries instead of trusting the single last-
# measured dt_snap. See the 2026-08-26 [POSE-FREEZE] finding: a bare dt_snap
# is only a good predictor of the *next* inter-snapshot gap if tick time is
# constant, and it never is -- any tick slower than the one before it made
# alpha saturate (pose freeze) before the real next snapshot landed. ~0.3
# means roughly the last 3-4 real ticks, smoothing outliers while still
# tracking a genuine, sustained cadence change (e.g. population rising).
DT_SNAP_EWMA_WEIGHT = 0.3
CAM_PROJECTION_REFRESH_INTERVAL = 0.10
CAMERA_SAMPLE_HZ = 120.0
# CameraSampler's read (read_view_proj_matrix) costs ~15ms, far more than any
# achievable CAMERA_SAMPLE_HZ period, so its loop always falls into the
# "wait_for <= 0" branch -- meaning this constant, not the target Hz, is the
# real idle gap between camera reads. Measured 2026-08-26: at 1ms the camera
# holds _drv_lock ~94% of the time (15ms busy / 16ms cycle), so the tick's
# own reads pay ~15ms of lock-wait on nearly every call regardless of
# whether they yield first or race straight for the lock -- moving the wait
# from one bucket to another did not reduce it (see the reverted
# skip_camera_yield attempt). Widening this is the actual lever: it trades a
# small amount of camera freshness (achieved rate drops from ~62Hz to
# ~48-53Hz) for a real window other readers can use.
# 2026-08-26: added [CAM-HZ] instrumentation because a player reported the
# camera feeling like "10Hz" when turning. Measured achieved rate was
# 22-33Hz, not the ~48-62Hz this comment used to claim -- that number was
# never actually measured, just estimated from the 94%-duty-cycle finding
# below. Trying 0.002 (down from 0.005) as a first step toward closing that
# gap; this directly trades against the 5ms widening that fixed the tick/
# slow-lane starvation, so it needs the same [CAM-HZ] + [TICK-LATENCY]/
# `bg=` verification before going any further either direction.
CAMERA_MIN_IDLE_GAP = 0.002
PLAYER_LIST_REFRESH_INTERVAL = 1.0
TRANSFORM_RESOLVE_RETRY_INTERVAL = 2.0
MAX_PLAYER_PREDICTION = 0.12
# ── Prediction accounting ([PRED-ERR] / [RIG-LAG]) ────────────────────────
# The 2026-08-27 recording showed the whole skeleton sitting *ahead* of the
# rendered body along the direction of travel on moving players, while
# static players at the same range sat exactly on theirs. [BONE-STRETCH]
# never fired in that run, so the rig is not deformed and not mixed-phase:
# the shape is right and the anchor is wrong. Two different faults produce
# that picture and only measurement separates them.
#
#   [PRED-ERR]  is the predictor judged against its own target. Each tick,
#               re-run last tick's prediction forward to this tick's
#               timestamp and compare it with the position that actually
#               arrived. A positive along-track number means we overshoot
#               the transform we are aiming at, i.e. velocity or lead is
#               too large.
#
#   [RIG-LAG]   is the target itself judged against what the game draws.
#               The bone transforms *are* the rendered rig, so the hips
#               bone minus the entity position, projected on the direction
#               of travel, is how far the drawn model sits behind the
#               position we anchor to. If that is non-zero while
#               [PRED-ERR] is ~0, the predictor is faithful and it is the
#               anchor that leads the render -- a fixed negative lead, not
#               a smaller velocity, is then the fix.
#
# Both are reported in metres and in milliseconds at the measured speed,
# because milliseconds are what any correction is eventually written in.
PRED_ERR_LOG_INTERVAL = 1.0
# Below this speed the along-track direction is noise, so those players
# only contribute to the at-rest baseline. Above it, a walking player is
# already fast enough for a one-tick error to be visible.
PRED_ERR_MOVING_MIN_SPEED = 2.0
PRED_ERR_RESTING_MAX_SPEED = 0.2
PLAYER_FLAG_SLEEPING = 1 << 4
# ── Position teleport gate ────────────────────────────────────────────────
# Symptom this exists for: with the local player above ground, a player who
# walks into the train tunnels / sewers suddenly renders on the far side of the
# map and stays there until they come back out, at which point it self-corrects.
# Whatever the client does to a PlayerModel that leaves the local network group
# (pooling, recycling the slot for another player, or simply parking it), the
# result reaches us as a single position sample that jumps hundreds of metres in
# one tick. _select_pm_position used to accept it unconditionally: its 120 m /
# 180 m guards only ever discriminated *between* candidate offsets, and there is
# exactly one candidate offset now (PlayerModel+0x2F8), so the final
# `candidates[0]` fallback always took the jump.
#
# So gate it on physics instead of on offset agreement: a jump is only believed
# once several consecutive samples agree on the new spot, or once the hold
# expires. A real teleport (respawn, boat, minicopter) still comes through
# within a few ticks; a recycled slot never agrees with itself and is dropped.
POS_JUMP_MIN_DIST = 25.0     # m — below this a sample is never suspect
POS_JUMP_MAX_SPEED = 60.0    # m/s — above this it cannot be real player motion
POS_JUMP_CONFIRM = 3         # consecutive agreeing samples before we believe it
POS_JUMP_AGREE_DIST = 12.0   # m — how close two samples must be to "agree"
POS_JUMP_HOLD_MAX = 1.5      # s — hard ceiling on holding a stale position
# The bone sampler batches all tracked rigs in one driver transaction. 250 m
# made boxes/names continue while their skeletons disappeared (`far=N` in
# BONE-DBG), which is especially obvious on open maps. A 1 km ceiling still
# keeps obviously irrelevant map-wide entities out while covering normal
# visible targets; BONE_MAX_TRACKED_PLAYERS remains the hard batch cap.
SKELETON_MAX_DISTANCE = 1000.0
WORLD_ENTITY_SCAN_INTERVAL = 2.0
WORLD_ENTITY_MAX_RENDER = 400
# Build 24840484: the live BaseNetworkable buffer's capacity is 16384
# (2**14 — a growth-doubling number, not garbage). The old 16000 ceiling sat
# just below it, so _read_il2cpp_array_ptrs() silently returned [] without
# reading anything ("[WE-DBG] buffer empty ... count=16384"), which cascaded
# into _entity_baseplayers() too (same ceiling), starving pm_to_bp mapping
# and leaving bone rig resolution stuck at rig=0/job=0 forever. Raised with
# headroom above the observed capacity.
WORLD_ENTITY_MAX_SCAN = 20000
# Longest IL2CPP class name we bother reading; every Rust entity type we
# classify is far shorter, and this is a multiple of 8 so it maps onto whole
# u64 words in a batch read.
KLASS_NAME_BYTES = 64
# Entity pointers are stable while alive and their klass never changes, so the
# klass lookup only needs to be re-read for pointers we haven't seen before.
# Periodically flush the cache anyway to recover from IL2CPP heap slot reuse
# (a destroyed entity's memory handed to a new, differently-typed entity).
WORLD_ENTITY_KLASS_CACHE_TTL = 60.0
# _scan_world_entities (every 2s, inside the disp= phase) and
# _entity_baseplayers (whenever a PlayerModel is unmapped, inside pos=) walk the
# SAME BaseNetworkable backing array. When both landed on one tick the buffer
# was read twice, which is why [TICK-LATENCY] showed pos=487 and disp=531 in a
# single 1132ms tick. One walk is now shared for:
ENTITY_BUFFER_CACHE_TTL = 1.0
# How long the resolved chain (list_dict/arr) is trusted before the full
# HiddenValue walk runs again. Every use still revalidates the array with the
# element-count read it needs anyway, so this only skips the ~6 sequential
# decrypt hops plus the container probe, not the sanity check.
ENTITY_CHAIN_CACHE_TTL = 10.0
# How many already-known entity pointers _entity_baseplayers re-reads per call
# on top of the genuinely new ones. Dead entities leave the buffer and get
# dropped from the prefab cache, so the only stale-entry window is an address
# freed and reused between two scans; this rotating window closes it at a fixed
# cost, without the periodic full-rescan spike a TTL flush would reintroduce.
ENTITY_PREFAB_REVALIDATE_PER_SCAN = 512
# 0.5 s was self-defeating: the scan is a chain of ~6 dependent round-trips at
# a ~14 ms driver floor, so it costs ~90-240 ms, which slowed the tick enough
# that the interval was *always* expired and a heavy scan landed on ~87% of
# ticks. A held weapon changes on a swap; 1.5 s of latency on that is
# invisible, and it cuts the scan rate threefold. See OFFSET_RECOVERY.md #32.
HELD_ITEM_SCAN_INTERVAL = 0.3
# How often [HELD-DBG] may re-report while no held item resolves at all. The
# diag used to be one-shot for the life of the process, so it always fired on
# the first tick -- before pm->bp had mapped anybody -- printed "pm_to_bp
# empty", and then stayed silent no matter how long held items kept coming back
# blank. A gate that only ever speaks about the state before the pipeline
# started is worse than no gate.
HELD_ITEM_DIAG_INTERVAL = 5.0
# ... and a slow heartbeat while it IS working, so "no [HELD-DBG] line" is not
# the only evidence that held items resolve. Silence proves the cache is
# non-empty but never says for how many players, which is the number that
# matters once the offsets are right.
HELD_ITEM_OK_INTERVAL = 30.0
PLAYER_NAME_SCAN_INTERVAL = 1.0  # scan for new player names every second

# Bone layout for the current player rig, confirmed 2026-08-21 by reading
# Model.boneNames (offset OFF.boneNames, parallel array to boneTransforms)
# live in-game for every index 0-93 — see OFFSET_RECOVERY.md trap #11. The
# previous list (8/10/11 for the right leg, 14/15/16/17 for spine, 18/19/20/23
# for left arm, 46/47 for neck/head, 54/55/56/59 for right arm) was wrong: the
# live name dump showed those indices actually belong to genital-censor mesh
# bones, the *right leg*, a mix of the right foot/toe and spine bones, left
# fingers, and face bones (eyetransform/jaw/eyelids), respectively. Only the
# principal joints are sampled; finger/twist bones add cost without improving
# the ESP.
SCI_BONE_IDS = (
    0,               # pelvis
    1, 3, 4,         # left leg: l_hip, l_knee, l_foot
    14, 16, 17,      # right leg: r_hip, r_knee, r_foot
    20, 21, 22, 23,  # spine: spine1, spine2, spine3, spine4
    24, 25, 26, 29,  # left arm: l_clavicle, l_upperarm, l_forearm, l_hand
    52, 53,          # neck, head
    60, 61, 62, 65,  # right arm: r_clavicle, r_upperarm, r_forearm, r_hand
)
SCI_BONE_LINKS = (
    # Spine
    (0, 20),
    (20, 21),
    (21, 22),
    (22, 23),

    # Neck / head
    (23, 52),
    (52, 53),

    # Left shoulder / arm
    (23, 24),       # torso -> left clavicle
    (24, 25),       # clavicle -> upper arm
    (25, 26),       # upper arm -> forearm
    (26, 29),       # forearm -> hand

    # Right shoulder / arm
    (23, 60),       # torso -> right clavicle
    (60, 61),       # clavicle -> upper arm
    (61, 62),       # upper arm -> forearm
    (62, 65),       # forearm -> hand

    # Left leg
    (0, 1),
    (1, 3),
    (3, 4),

    # Right leg
    (0, 14),
    (14, 16),
    (16, 17),
)
SCI_MIN_ARRAY_COUNT = max(SCI_BONE_IDS) + 1


class RustGame:
    def __init__(self, m, ga_base):
        self.m = m
        self.ga = ga_base
        self.players = []
        self.vp_matrix = None
        self.cam_pos = None
        self.local_pos = None
        # Local player's PlayerEyes.viewOffset (Vector3, world-space offset
        # from player root to eye position). Updated every snapshot when we
        # can resolve the local BasePlayer's PlayerEyes chain, else stays
        # at the last known value. Default = Rust standing viewOffset.
        self.local_eye_offset = (0.0, 1.6, 0.0)
        # Cache : (local_bp_ptr, resolved_at, player_eyes_ptr). The
        # PlayerEyes pointer is only invalidated on respawn, so 5 s of
        # cache saves ~500 IOs/sec vs re-resolving the HV chain every tick.
        self._local_eyes_cache = None
        # Live pointers exposed for the aim/probe layer: the last successfully-
        # resolved local BasePlayer and its PlayerEyes native pointer. Set by
        # _read_local_eye_offset, since it already does that walk anyway.
        # These are None until the first successful eye-offset read; consumers
        # must guard.
        self.local_bp = None
        self.local_player_eyes = None
        self.tick_ms = 0.0
        # Worst tick since the last [TICK-LATENCY] cadence print. The printed
        # line is one sampled tick; this is the peak every other tick hid.
        self._tick_worst_ms = 0.0
        self._next_slow_tick_log_at = 0.0
        self._heavy_scan_budget = 1
        # Which overlay features are actually switched on. The worker reads
        # these to skip whole scans: a feature the user turned off must cost
        # zero IOCTLs, not "scan it anyway and discard it at draw time".
        # The controller pushes the view's Settings in here every frame.
        self.wanted = {
            'world_entities': True,
            'names': True,
            'held_item': True,
        }
        self.diag = ""
        self._lock = threading.Lock()
        # Double-buffered snapshots (same pattern as esp.py)
        self._snap_prev = None  # (ts, players, vp_matrix)
        self._snap_cur = None
        # [POSE-FREEZE] instrumentation: get_snapshot() is called once per
        # render frame, far more often than the worker produces a new
        # snapshot. alpha saturates at 1.0 the moment `now - cur[0]` reaches
        # the *previous* inter-snapshot gap (dt_snap) -- which is only a
        # correct predictor of the *next* gap if tick time is constant. Any
        # tick slower than the one before it (bone-job admission bursts,
        # BP-mapping walks, camera-lock contention) makes alpha hit 1.0
        # before the real next snapshot lands: the pose then holds bit-for-
        # bit at the last blended shape (frozen) while the body keeps
        # sliding via velocity extrapolation, until the next snapshot
        # arrives and the pose visibly jumps to catch up. This tracks how
        # often and how long that saturation actually lasts, instead of
        # guessing from how it looks.
        self._pose_frames_total = 0
        self._pose_frames_saturated = 0
        self._pose_freeze_streak_start = None
        self._pose_freeze_streak_ms_sum = 0.0
        self._pose_freeze_streak_count = 0
        self._pose_freeze_streak_max_ms = 0.0
        self._last_pose_freeze_log_at = 0.0
        # EWMA of recent real inter-snapshot gaps, used as alpha's
        # denominator instead of the bare last dt_snap -- see
        # DT_SNAP_EWMA_WEIGHT.
        self._dt_snap_ewma = None
        self._pose_last_cur_ts = None
        self._last_debug_at = 0.0
        self._last_player_debug_at = 0.0
        self._last_sample_debug_at = 0.0
        self._last_chain = None
        self._last_static_scan_at = 0.0
        self._static_candidate = None
        self._last_fallback = None
        # Cached stable chain pointers (re-resolved only on None/miss)
        self._cached_arr = None
        self._cached_arr = None
        self._cached_list_dict = None
        self._cached_lc_klass = 0
        self._cached_lc_sf = 0
        self._cached_lc_wrapper = 0
        self._cached_lc_pm_list = 0
        self._cached_lc_buf_arr = 0
        self._cached_lc_count = 0
        self._lc_chain_cache_at = 0.0
        self._lc_cache_ttl = 5.0
        self._next_lc_refresh_at = 0.0
        self._lc_refresh_interval = PLAYER_LIST_REFRESH_INTERVAL
        # Whether the last _get_lc_buffer() call took the free time-gate path
        # (True) or actually issued a driver transaction (False). Needed
        # because [TICK-LATENCY]/[DBG] print at most once per
        # TICK_LOG_INTERVAL/DEBUG_LOG_INTERVAL (~1s) while real ticks run
        # every ~90ms underneath -- a single printed sample is not evidence
        # of what most real ticks do. _hit_window_* below accumulate the
        # true hit/miss count across every real tick between prints.
        self._lc_fast_path = True
        self._hit_window_chain_hits = 0
        self._hit_window_chain_total = 0
        self._hit_window_pm_list_hits = 0
        self._hit_window_pm_list_total = 0
        # BasePlayer static fields cache (stable across ticks, reset on miss)
        self._cached_bp_sf = None
        self._cached_local_player = None
        self._pm_ptr_cache = []
        self._pm_ptr_cache_updated_at = 0.0
        self._next_pm_list_refresh_at = 0.0
        self._pm_payload = 0x20
        self._pm_list_miss = 0
        self._last_local_pm = None
        self._pm_bp_cache = {}
        self._bp_list_cache = []
        # Shared BaseNetworkable buffer walk + per-entity prefabID cache.
        # See _entity_buffer() / _entity_baseplayers().
        self._entity_ptr_cache = []
        self._entity_ptr_cache_count = 0
        self._entity_ptr_cache_at = 0.0
        self._entity_chain_at = 0.0
        self._entity_prefab_cache = {}
        self._entity_prefab_cursor = 0
        # Set by _entity_baseplayers_locked every call; read by
        # [PM2BP-STUCK] so "live_bps=27" comes with WHY it's 27 instead of
        # needing a second investigation the next time it looks too low.
        self._entity_bp_diag = "not run yet"
        self._player_model_offset = OFF.playerModel
        self._last_bp_source = "none"
        # A pm's consecutive _resolve_pm_to_bp miss count -- see
        # _log_pm2bp_stuck. Distinguishes "the entity-buffer walk never
        # produced a candidate for this player" from "it did, but the
        # +offset read on it didn't match", instead of guessing why a
        # handful of players never get a skeleton (2026-08-26 review).
        self._pm2bp_miss_streak = {}
        # [BONE-PHASE] instrumentation. The blend guard
        # (BONE_POSE_BLEND_MAX_SQ) used to decide per *bone* whether that one
        # bone was interpolated or snapped to its raw current offset, with no
        # requirement that connected bones in the same skeleton agree -- and
        # these counters proved that mix real: 1-2% of skeleton-frames,
        # worst case one bone blended against twenty raw, which is exactly
        # the "bones fly out of the body while running" that was reported.
        # The decision is per skeleton now (see _predict_from_snapshots), so
        # what these measure is the price of that: how often a whole
        # skeleton falls back to raw, and how few dissenting bones forced it.
        self._bone_phase_total_skeletons = 0
        self._bone_phase_raw = 0
        self._bone_phase_worst = None  # (pm, dissenting bones, total bones)
        self._last_bone_phase_log_at = 0.0
        # [PRED-ERR] / [RIG-LAG], see PRED_ERR_LOG_INTERVAL. Set here
        # as well as lazily so the real object never depends on the
        # self-healing path the tests are the only ones to exercise.
        self._reset_prediction_error_window()
        self._next_pred_err_log_at = 0.0
        self._last_disp_io = (0, 0, 0)
        self._slow_lane_pm_to_bp = {}
        self._slow_lane_last = (0, 0.0)
        # ItemDefinition* -> shortName. An ItemDefinition is a per-item-type
        # singleton and its shortName never changes, so this needs no TTL and
        # stays small (one entry per item type the player has ever seen). It
        # removes the last two dependent round-trips from the held-item chain.
        self._itemdef_name_cache = {}
        # The BaseNetworkable pointer buffer and its prefab cache are now
        # touched by two threads: the worker tick (_scan_world_entities) and
        # the background pm->bp resolver. Both *rebuild* those dicts by
        # comprehension, so an unguarded overlap is a "dictionary changed size
        # during iteration" crash, not merely a stale read.
        self._entity_lock = threading.RLock()
        self._next_bp_deep_scan_at = 0.0
        self._bp_deep_fail_count = 0
        self._bp_deep_scan_interval = 8.0
        self._pm_pos_offset_cache = {}
        self._pm_raw_pos_cache = {}
        # pm -> why its position was rejected, for [PM-DROP].
        self._pos_reject_why = {}
        self._pm_drop_logged = {}
        self._pm_smooth_pos_cache = {}
        # Teleport gate state, see POS_JUMP_* above.
        self._pm_pos_time_cache = {}   # pm -> perf_counter of last accepted pos
        self._pm_pos_bp_cache = {}     # pm -> bp the accepted pos belonged to
        self._pm_jump_state = {}       # pm -> {'cand', 'count', 'since'}
        # 'expired' counts holds that ran the full POS_JUMP_HOLD_MAX instead of
        # being confirmed -- those are the ones long enough to see on screen.
        # 'worst_hold_ms' is the longest single freeze this session.
        self._pos_jump_stats = {
            'held': 0, 'accepted': 0, 'reset': 0,
            'expired': 0, 'worst_hold_ms': 0.0,
        }
        self._next_pos_jump_log_at = 0.0
        # Engine-velocity probe, see OFF.velocity_pm_a/_b.
        self._vel_choice = None        # None=probing, 'a'/'b'=latched, ''=unusable
        self._vel_probe = {'a': 0.0, 'b': 0.0, 'n': 0}
        self._vel_probe_prev = {}      # pm -> (pos, t)
        self._pm_vel_cache = {}        # pm -> (vx, vy, vz) from the latched slot
        self._transform_slot_cache = {}
        self._transform_slot_cache_at = 0.0
        self._transform_slot_refresh_interval = 1.0
        self._next_transform_resolve_at = 0.0
        self._bone_slot_cache = {}
        self._bone_position_cache = {}
        self._next_bone_age_log_at = 0.0
        self._bone_resolve_retry_at = {}
        self._health_cache = {}
        self._sleeping_cache = {}
        # pm -> perf_counter() of the last successful playerFlags read; bounds
        # how long _sleeping_cache may be used as a seed (see SLEEPING_STALE_TTL).
        self._sleeping_seen_at = {}
        self._last_health_refresh_at = 0.0
        self._last_tick_latency_log_at = 0.0
        self._render_motion_cache = {}
        # Held item & Inventory ESP state
        self._held_item_cache = {}   # pm -> display_name
        self._belt_cache = {}        # pm -> list of 6 items (short_name, disp_name)
        self._wear_cache = {}        # pm -> list of 6 items (short_name, disp_name)
        self._main_cache = {}        # pm -> list of 30 items (short_name, disp_name)
        self._next_held_item_scan_at = 0.0
        self._held_list_cache = {}   # (pm, ctype) -> List<Item>* (stable while alive)
        # Player name ESP state
        self._player_name_cache = {}  # pm -> display_name string
        self._next_player_name_scan_at = 0.0
        # World entity ESP state
        self.world_entities = []
        self._next_world_scan_at = 0.0
        self._world_klass_map = {}  # legacy RVA map, no longer consulted
        self._world_klass_resolved = False
        self._we_klass_cache = {}  # entity_ptr -> klass_ptr, TTL-flushed
        self._world_klass_labels = {}  # klass_ptr -> ESP label or None, permanent
        self._klass_name_cache = {}    # klass_ptr -> IL2CPP class name, permanent
        self._next_we_cache_flush_at = 0.0
        self._we_chain_warned = False
        self._we_match_diag_done = False
        self._we_pos_diag_done = False

    # ------------------------------------------------------------------
    # World entity ESP (ore nodes, hemp, dropped loot, boxes, TCs)
    # ------------------------------------------------------------------
    #
    # Classification is by IL2CPP *class name*, not by klass pointer.
    #
    # The previous approach read a klass pointer from a hard-coded RVA
    # (OFF.OreResourceEntity_c and friends) and compared entity+0x0 against it.
    # RVAs move on every Rust build: on the current one only 2 of the 6 RVAs
    # resolved at all ("[WE-DBG] RVA klass hints: 2/6"), and the two that did
    # pointed at the wrong types, so klass matching reported 0/N even though the
    # entity buffer itself was being walked correctly.
    #
    # Cl1kExternal (main.cpp isOreClass / isDroppedItemClass) instead reads
    # entity+0x0 -> klass, klass+0x10 -> const char*, and substring-matches the
    # name. That is build-independent, which is why we mirror it here. Cost is
    # kept low by caching klass_ptr -> label: an entity's klass never changes and
    # a populated server exposes a few hundred distinct klasses at most, versus
    # tens of thousands of entities.
    #
    # Order matters below: "DroppedItemContainer" also contains "droppeditem",
    # so the more specific rules have to be tested first.
    _WORLD_CLASS_RULES = (
        ('TC',    ('buildingprivlidge', 'buildingprivilege')),
        ('Bag',   ('droppeditemcontainer',)),
        ('Crate', ('lootcontainer',)),
        ('Ore',   ('oreresource', 'resourcerock', 'rockresource', 'noderesource',
                   'orenode', 'sulfurore', 'metalore', 'stoneore', 'rockfileentity')),
        ('Hemp',  ('collectibleentity', 'collectible')),
        ('Item',  ('worlditem', 'droppeditem')),
    )

    @staticmethod
    def _classify_class_name(name):
        """Map an IL2CPP class name to an ESP label, or None to ignore it."""
        if not name:
            return None
        lowered = name.lower()
        for label, needles in RustGame._WORLD_CLASS_RULES:
            for needle in needles:
                if needle in lowered:
                    return label
        return None

    def _label_klasses(self, klass_ptrs):
        """Resolve klass pointers to ESP labels, caching every answer forever.

        Two batched stages, both keyed on klass pointers we have never seen:
          klass + 0x10          -> const char* name
          name[0:NAME_BYTES]    -> NUL-terminated ASCII

        Names are fetched as u64 words through batch_u64 so a few hundred
        previously-unknown klasses cost two IOCTLs, not two per klass.

        Entries that classify to nothing are cached as None — that negative
        result is what keeps steady-state scans nearly free, since the vast
        majority of BaseNetworkable entities are scenery we do not draw.
        """
        cache = self._world_klass_labels
        for klass, name in self._read_klass_names(klass_ptrs).items():
            if klass not in cache:
                cache[klass] = self._classify_class_name(name)
        return cache

    def _read_klass_names(self, klass_ptrs):
        """Resolve klass pointers to IL2CPP class-name strings, cached forever.

        klass + 0x10 -> const char*, then KLASS_NAME_BYTES of NUL-terminated
        ASCII, both fetched through batch_u64 so a few hundred previously
        unknown klasses cost two IOCTLs rather than two apiece. A klass never
        renames itself, so this cache needs no TTL.

        Split out of _label_klasses because _try_accept_entity_buffer needs the
        raw names too: Cl1kExternal validates a candidate entity buffer by
        checking that its first few elements have readable class names, which
        is a question about names, not about whether we happen to draw them.
        """
        cache = self._klass_name_cache
        unknown = [k for k in {p for p in klass_ptrs if _valid_user_ptr(p)}
                   if k not in cache]
        if not unknown:
            return cache

        name_ptrs = self.m.batch_u64(
            [k + OFF.klass_name for k in unknown],
            attempts=1,
        )
        pending = []
        for klass, name_ptr in zip(unknown, name_ptrs):
            if _valid_user_ptr(name_ptr):
                pending.append((klass, name_ptr))
            else:
                cache[klass] = ''
        if not pending:
            return cache

        words = KLASS_NAME_BYTES // 8
        addresses = []
        for _, name_ptr in pending:
            addresses.extend(name_ptr + offset for offset in range(0, KLASS_NAME_BYTES, 8))
        values = self.m.batch_u64(addresses, attempts=1)

        for index, (klass, _) in enumerate(pending):
            chunk = values[index * words:(index + 1) * words]
            if len(chunk) < words:
                cache[klass] = None
                continue
            raw = struct.pack('<%dQ' % words, *chunk)
            terminator = raw.find(b'\x00')
            # >= 0, not > 0. A klass whose name pointer read back as zero
            # starts with a null at index 0, and `> 0` skipped the slice
            # entirely -- so the "name" became the full 64 null bytes and was
            # cached under that klass forever. It showed up verbatim in
            # [WE-NAMES] as a 64-character garbage entry next to 'BasePlayer'.
            # An empty name is the honest answer for a failed read.
            if terminator >= 0:
                raw = raw[:terminator]
            try:
                cache[klass] = raw.decode('ascii')
            except UnicodeDecodeError:
                cache[klass] = ''
        return cache

    def _scan_world_entities(self, on_worker=False):
        """Classify the BaseNetworkable buffer and batch-read entity positions."""
        if not self.wanted.get('world_entities', True):
            # Nothing draws them, so resolving 200+ transform chains is pure
            # cost. Drop what we hold so the overlay does not show a frozen
            # set if the user turns them back on.
            if self.world_entities:
                self.world_entities = []
            return
        now = time.perf_counter()
        if now < self._next_world_scan_at:
            return
        # Same guard as the other two scans: off the tick, the tick's budget
        # does not apply and touching it from another thread would corrupt the
        # tick's own accounting.
        if not on_worker and not self._take_heavy_slot():
            return
        # Never wait behind the background pm->bp resolver holding the same
        # buffer: this scan is throttled to 2 s, so skipping a round costs
        # nothing, whereas blocking here would put the 500 ms walk straight
        # back onto the tick that trap #27 just took it off.
        if not self._entity_lock.acquire(blocking=False):
            return
        try:
            self._next_world_scan_at = now + WORLD_ENTITY_SCAN_INTERVAL
            return self._scan_world_entities_locked(now)
        finally:
            self._entity_lock.release()

    def _scan_world_entities_locked(self, now):

        # payload defaults to 0x20 (plain Il2CppArray header). This buffer was
        # briefly read at +0x28 on the theory that it is a BufferList variant
        # (cl1kexternal's Il2Cpp::Array_DataBase), but that halved the valid-
        # pointer yield on this build (2515/3472 -> 161/3625) instead of fixing
        # anything, and the player-list reader next to this one already carries
        # a hard-won note that +0x28 breaks it on this game version. Reverted.
        buf = self._entity_buffer(WORLD_ENTITY_MAX_SCAN)
        if buf is None:
            if not self._we_chain_warned:
                self._we_chain_warned = True
                print(f"[WE-DBG] entity chain resolve FAILED: {self.diag}", flush=True)
            return
        self._we_chain_warned = False
        list_dict, arr, ptrs, count = buf
        if not ptrs:
            print(f"[WE-DBG] buffer empty — list_dict=0x{list_dict:X} arr=0x{arr:X} count={count}", flush=True)
            return

        # Stage 1: entity + 0x0 -> klass. Immutable per live entity, so only
        # pointers we have not classified yet are fetched. The entity->klass
        # cache is TTL-flushed (entities die and their addresses get reused);
        # the klass->label cache below never needs flushing.
        if now >= self._next_we_cache_flush_at:
            self._we_klass_cache = {}
            self._next_we_cache_flush_at = now + WORLD_ENTITY_KLASS_CACHE_TTL
        cache = self._we_klass_cache
        new_ptrs = [p for p in ptrs if p not in cache]
        if new_ptrs:
            for p, kv in zip(new_ptrs, self.m.batch_u64(new_ptrs, attempts=1)):
                cache[p] = kv
        klass_values = [cache.get(p, 0) for p in ptrs]

        # Stage 2: klass -> class name -> ESP label.
        labels = self._label_klasses(klass_values)
        matched = [
            (ptr, labels.get(klass))
            for ptr, klass in zip(ptrs, klass_values)
            if labels.get(klass) is not None
        ]

        if not self._we_match_diag_done:
            self._we_match_diag_done = True
            named = sum(1 for v in labels.values() if v is not None)
            print(
                f"[WE-DBG] {len(ptrs)} entities (count={count}), "
                f"{len(labels)} distinct klasses, {named} of them drawable, "
                f"matched {len(matched)}",
                flush=True,
            )
            # TEMP: real class names, not just counts — _WORLD_CLASS_RULES'
            # substrings ('worlditem', 'droppeditem', ...) were never
            # verified against this build's actual names. Ground truth
            # beats guessing again here, same as the bone-ID fix.
            distinct_names = sorted({
                self._klass_name_cache.get(k, '')
                for k in set(klass_values)
                if self._klass_name_cache.get(k)
            })
            print(f"[WE-NAMES] {distinct_names}", flush=True)

        if not matched:
            self.world_entities = []
            return
        if len(matched) > WORLD_ENTITY_MAX_RENDER:
            matched = matched[:WORLD_ENTITY_MAX_RENDER]

        # Stage 3: world position, four tiers, most-universal first:
        #
        #   0. entity's OWN native Unity Transform: entity (a UnityEngine.
        #      Object itself) -> m_CachedPtr -> native Component -> owning
        #      GameObject -> Components[0] (Transform is always slot 0) ->
        #      TransformData -> its cached world position. Every GameObject
        #      always has exactly one Transform, so this doesn't depend on
        #      whether Model/bounds/PositionLerp happen to be populated for
        #      this entity type — see OFFSET_RECOVERY.md trap #15.
        #   1. entity + OFF.be_bounds_center — `public Bounds bounds;` is a
        #      *value* field (no pointer to chase), world-space, present on
        #      every BaseEntity regardless of whether it has a Model or an
        #      active PositionLerp. Confirmed against the full deobfuscated
        #      dump.cs class listing for this exact build. See trap #14.
        #   2. entity -> Model -> rootBone -> Transform, world position via
        #      the same parent-chain compose already used for player bones —
        #      for the rare case bounds hasn't been computed yet.
        #   3. PositionLerp (entity+0xC8 -> nested -> Vec3) — last resort,
        #      matching the reference's own priority order. Rust appears to
        #      leave this null/inactive once an entity settles and stops
        #      needing network position smoothing, which is why it alone
        #      (what this used to be) covered almost nothing static: live-
        #      measured 2026-08-21, 49 matched via tier 3 only -> 4 resolved,
        #      thousands of units from the player. See trap #13.
        ent_ptrs = [entity for entity, _ in matched]
        ent_names = [name for _, name in matched]

        positions = self._read_entity_native_transform_positions(ent_ptrs)
        native_resolved = sum(1 for p in positions if p is not None)

        retry = [i for i, pos in enumerate(positions) if pos is None]
        if retry:
            bounds_retry = self._batch_vec3(
                [ent_ptrs[i] for i in retry], OFF.be_bounds_center
            )
            for slot, pos in zip(retry, bounds_retry):
                if pos is not None:
                    positions[slot] = pos
        bounds_resolved = sum(
            1 for p in positions if p is not None
        ) - native_resolved

        retry = [i for i, pos in enumerate(positions) if pos is None]
        root_resolved = 0
        if retry:
            root_positions = self._read_entity_root_positions(
                [ent_ptrs[i] for i in retry]
            )
            for slot, pos in zip(retry, root_positions):
                if pos is not None:
                    positions[slot] = pos
                    root_resolved += 1

        retry = [i for i, pos in enumerate(positions) if pos is None]
        if retry:
            lerps = self.m.batch_u64(
                [ent_ptrs[i] + OFF.be_position_lerp for i in retry],
                attempts=1,
            )
            nested = self.m.batch_u64(
                [
                    (lerp + OFF.be_lerp_nested) if _valid_user_ptr(lerp) else 0
                    for lerp in lerps
                ],
                attempts=1,
            )
            fallback = self._batch_vec3(
                [n if _valid_user_ptr(n) else 0 for n in nested],
                OFF.be_lerp_world_pos,
            )
            for slot, pos in zip(retry, fallback):
                positions[slot] = pos

        self.world_entities = [
            {'type': ent_names[i], 'pos': pos, 'entity': ent_ptrs[i]}
            for i, pos in enumerate(positions)
            if pos is not None
        ]

        item_entities = [
            we['entity'] for we in self.world_entities if we['type'] == 'Item'
        ]
        if item_entities:
            names = self._read_world_item_names(item_entities)
            for we in self.world_entities:
                if we['type'] == 'Item':
                    we['name'] = names.get(we['entity'], '')

        if not self._we_pos_diag_done:
            self._we_pos_diag_done = True
            resolved = len(self.world_entities)
            sample = self.world_entities[:5]
            print(
                f"[WE-POS] matched={len(matched)} native_resolved={native_resolved} "
                f"bounds_resolved={bounds_resolved} "
                f"root_resolved={root_resolved} pos_resolved={resolved} "
                f"sample={sample}",
                flush=True,
            )
            # Always run the raw scan once, even if something "resolved" --
            # Model.rootBone gave 3 different 'Item' entities near-identical
            # positions (live-measured: same X, Y/Z differing in the 3rd
            # decimal), which smells like a shared/pooled template Model
            # rather than each instance's real position. A resolved count
            # > 0 doesn't mean *correct*.
            if ENABLE_WE_OFFSET_SCAN:
                self._debug_scan_entity_offsets(matched, ent_names)
                self._debug_scan_dropped_item_collider(matched, ent_names)

    def _debug_scan_entity_offsets(self, matched, ent_names, scan_len=0x400):
        """TEMP: brute-force which byte offset actually holds a world Vec3.

        All three reasoned guesses (bounds, Model.rootBone, PositionLerp)
        resolved zero positions live — see conversation. Instead of guessing
        a fourth offset, read raw bytes from a few entities *of the same
        class* and check every 4-byte-aligned window for a plausible
        position; an offset that hits in every sampled entity (and, if we
        know our own position, lands within a generous radius of it) is
        almost certainly the real field. No offset is applied anywhere from
        this — it only prints candidates for a human to confirm.
        """
        origin = self.local_pos
        by_label = {}
        for (entity, _), label in zip(matched, ent_names):
            by_label.setdefault(label, []).append(entity)
        label = 'Item' if by_label.get('Item') else max(
            by_label, key=lambda k: len(by_label[k])
        )
        samples = by_label.get(label, [])[:4]
        if len(samples) < 2:
            print(f"[WE-SCAN] not enough same-type samples (label={label})", flush=True)
            return

        raws = []
        for entity in samples:
            raw = self.m.read(entity, scan_len)
            if raw and len(raw) == scan_len:
                raws.append(raw)
        if len(raws) < 2:
            print(f"[WE-SCAN] raw reads failed (label={label})", flush=True)
            return

        hits_per_offset = {}
        for raw in raws:
            for off in range(0, scan_len - 12, 4):
                x, y, z = struct.unpack_from('<3f', raw, off)
                if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                    continue
                if not (abs(x) < 6000 and abs(z) < 6000 and -200 < y < 2000):
                    continue
                if x * x + y * y + z * z < 1.0:
                    continue
                if origin is not None:
                    d = math.sqrt(
                        (x - origin[0]) ** 2
                        + (y - origin[1]) ** 2
                        + (z - origin[2]) ** 2
                    )
                    if d > 2000.0:
                        continue
                hits_per_offset.setdefault(off, []).append((round(x, 1), round(y, 1), round(z, 1)))

        candidates = sorted(
            (
                (off, vecs)
                for off, vecs in hits_per_offset.items()
                if len(vecs) == len(raws)
            ),
            key=lambda kv: kv[0],
        )
        print(
            f"[WE-SCAN] label={label} samples={len(raws)} origin={origin} "
            f"candidates={[(hex(off), vecs) for off, vecs in candidates[:15]]}",
            flush=True,
        )

        # Phase 2: one level of pointer indirection. The flat scan only
        # looks at the entity's own bytes; if the real position lives on a
        # child/component object (e.g. a cached native Transform reference,
        # or a Rigidbody), it's reachable through a pointer *inside* the
        # entity, not a value on the entity itself. Every 8-byte-aligned
        # QWORD in a smaller prefix of each entity is treated as a candidate
        # pointer; every candidate pointer's own small window is scanned the
        # same way, and only (entity_offset, pointee_offset) pairs that hit
        # in *every* sampled entity survive.
        ptr_scan_len = 0x200
        pointee_words = 12  # 0x60 bytes
        candidates_by_entity = []  # [{entity_off: ptr, ...}, ...]
        for raw in raws:
            found = {}
            for eoff in range(0, min(ptr_scan_len, len(raw)) - 8, 8):
                (val,) = struct.unpack_from('<Q', raw, eoff)
                if _valid_user_ptr(val):
                    found[eoff] = val
            candidates_by_entity.append(found)

        all_ptrs = sorted({p for found in candidates_by_entity for p in found.values()})
        if all_ptrs:
            addresses = [
                ptr + word * 8 for ptr in all_ptrs for word in range(pointee_words)
            ]
            values = self.m.batch_u64(addresses, attempts=1)
            pointee_bytes = {}
            for i, ptr in enumerate(all_ptrs):
                chunk = values[i * pointee_words:(i + 1) * pointee_words]
                if len(chunk) == pointee_words:
                    pointee_bytes[ptr] = struct.pack('<%dQ' % pointee_words, *chunk)

            hits_by_pair = {}
            for entity_idx, found in enumerate(candidates_by_entity):
                for eoff, ptr in found.items():
                    raw = pointee_bytes.get(ptr)
                    if raw is None:
                        continue
                    for poff in range(0, len(raw) - 12, 4):
                        x, y, z = struct.unpack_from('<3f', raw, poff)
                        if not (
                            math.isfinite(x) and math.isfinite(y) and math.isfinite(z)
                        ):
                            continue
                        if not (abs(x) < 6000 and abs(z) < 6000 and -200 < y < 2000):
                            continue
                        if x * x + y * y + z * z < 1.0:
                            continue
                        if origin is not None:
                            d = math.sqrt(
                                (x - origin[0]) ** 2
                                + (y - origin[1]) ** 2
                                + (z - origin[2]) ** 2
                            )
                            if d > 2000.0:
                                continue
                        key = (eoff, poff)
                        hits_by_pair.setdefault(key, {})[entity_idx] = (
                            round(x, 1), round(y, 1), round(z, 1)
                        )

            pair_candidates = sorted(
                (
                    (eoff, poff, hits)
                    for (eoff, poff), hits in hits_by_pair.items()
                    if len(hits) == len(raws)
                ),
                key=lambda kv: (kv[0], kv[1]),
            )
            print(
                f"[WE-SCAN2] label={label} ptrs_tried={len(all_ptrs)} "
                f"candidates={[(hex(eo), hex(po), list(h.values())) for eo, po, h in pair_candidates[:15]]}",
                flush=True,
            )
        else:
            print(f"[WE-SCAN2] label={label} no candidate pointers found", flush=True)

    def _debug_scan_dropped_item_collider(self, matched, ent_names):
        """TEMP: targeted follow-up to _debug_scan_entity_offsets.

        dump.cs shows `DroppedItem` (not WorldItem/BaseEntity) declares its
        own `private Collider ...; // 0x220` field — a real physics
        component, always maintained by Unity's physics engine regardless of
        Model/PositionLerp state. Unlike the blind pointer scan in
        _debug_scan_entity_offsets (which tries every plausible pointer in
        the entity), this follows exactly one chain: entity+0x220 -> managed
        Collider -> +0x10 (m_CachedPtr, the same managed->native convention
        already used for Transform) -> native Collider, then scans a wider
        window there since it's the one candidate we actually trust.
        """
        origin = self.local_pos
        samples = [
            entity for (entity, _), label in zip(matched, ent_names)
            if label == 'Item'
        ][:4]
        if not samples:
            print("[WE-SCAN3] no Item samples", flush=True)
            return

        DROPPED_ITEM_COLLIDER = 0x220
        colliders = self.m.batch_u64(
            [entity + DROPPED_ITEM_COLLIDER for entity in samples], attempts=1,
        )
        natives = self.m.batch_u64(
            [
                (c + OFF.bone_transform) if _valid_user_ptr(c) else 0
                for c in colliders
            ],
            attempts=1,
        )
        valid = [(i, n) for i, n in enumerate(natives) if _valid_user_ptr(n)]
        if not valid:
            print(
                f"[WE-SCAN3] no valid native colliders "
                f"(colliders={[hex(c) if c else 0 for c in colliders]} "
                f"natives={[hex(n) if n else 0 for n in natives]})",
                flush=True,
            )
            return

        scan_len = 0x400  # widened from 0x200 — phase A found nothing there
        raws = {}
        for i, native in valid:
            raw = self.m.read(native, scan_len)
            if raw and len(raw) == scan_len:
                raws[i] = raw
        if len(raws) < 2:
            print(f"[WE-SCAN3] raw reads failed (got {len(raws)})", flush=True)
            return

        # Phase A: flat Vec3 scan, same method as _debug_scan_entity_offsets,
        # just anchored at the native Collider instead of the entity.
        hits_per_offset = {}
        for raw in raws.values():
            for off in range(0, scan_len - 12, 4):
                x, y, z = struct.unpack_from('<3f', raw, off)
                if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                    continue
                if not (abs(x) < 6000 and abs(z) < 6000 and -200 < y < 2000):
                    continue
                if x * x + y * y + z * z < 1.0:
                    continue
                if origin is not None:
                    d = math.sqrt(
                        (x - origin[0]) ** 2
                        + (y - origin[1]) ** 2
                        + (z - origin[2]) ** 2
                    )
                    if d > 2000.0:
                        continue
                hits_per_offset.setdefault(off, []).append(
                    (round(x, 1), round(y, 1), round(z, 1))
                )

        candidates = sorted(
            (
                (off, vecs)
                for off, vecs in hits_per_offset.items()
                if len(vecs) == len(raws)
            ),
            key=lambda kv: kv[0],
        )
        print(
            f"[WE-SCAN3] samples={len(raws)} origin={origin} "
            f"candidates={[(hex(off), vecs) for off, vecs in candidates[:15]]}",
            flush=True,
        )

        # Phase B: "try as native transform". Every QWORD found inside the
        # native Collider's own memory is treated as if it might *already
        # be* a native Transform pointer (not a managed Transform requiring
        # the +0x10 m_CachedPtr hop — we're already one level native).  For
        # each, probe native+OFF.transform_access (0x28) / +8 (slot index);
        # a plausible hierarchy pointer + small index there is composed into
        # a full world position via the exact same parent-chain walk already
        # used for player bones and Model.rootBone, since Unity always
        # stores parents before children (no re-read needed beyond idx+1
        # slots). This is the collider reaching its own owning GameObject's
        # Transform, if Unity caches it somewhere findable this way.
        ptr_candidates_by_sample = {}
        for i, raw in raws.items():
            found = []
            for off in range(0, scan_len - 8, 8):
                (val,) = struct.unpack_from('<Q', raw, off)
                if _valid_user_ptr(val):
                    found.append((off, val))
            ptr_candidates_by_sample[i] = found

        all_candidate_ptrs = sorted(
            {v for found in ptr_candidates_by_sample.values() for _, v in found}
        )
        if not all_candidate_ptrs:
            print("[WE-SCAN3b] no candidate pointers inside native collider", flush=True)
            return

        td_idx_addrs = []
        for p in all_candidate_ptrs:
            td_idx_addrs.append(p + OFF.transform_access)
            td_idx_addrs.append(p + OFF.transform_access + 8)
        td_idx_values = self.m.batch_u64(td_idx_addrs, attempts=1)

        good_ptrs = {}
        for k, p in enumerate(all_candidate_ptrs):
            td = td_idx_values[k * 2]
            idx = td_idx_values[k * 2 + 1] & 0xFFFFFFFF
            if _valid_user_ptr(td) and idx < 5000:
                good_ptrs[p] = (td, idx)

        if not good_ptrs:
            print(
                "[WE-SCAN3b] no candidate pointer looked like a native Transform "
                f"(tried {len(all_candidate_ptrs)})",
                flush=True,
            )
            return

        td_list = sorted({td for td, _ in good_ptrs.values()})
        buf_addrs = []
        for td in td_list:
            buf_addrs.append(td + OFF.td_pos_base)
            buf_addrs.append(td + OFF.td_parent_indices)
        bufs = self.m.batch_u64(buf_addrs, attempts=1)
        td_bufs = {}
        for t, td in enumerate(td_list):
            local_ptr = bufs[t * 2]
            parent_ptr = bufs[t * 2 + 1]
            if _valid_user_ptr(local_ptr) and _valid_user_ptr(parent_ptr):
                td_bufs[td] = (local_ptr, parent_ptr)

        def compose(td, idx):
            bufs_for_td = td_bufs.get(td)
            if bufs_for_td is None:
                return None
            local_ptr, parent_ptr = bufs_for_td
            trs_data = self.m.read(local_ptr, (idx + 1) * 0x30)
            pidx_data = self.m.read(parent_ptr, (idx + 1) * 4)
            if (
                not trs_data or not pidx_data
                or len(trs_data) < (idx + 1) * 0x30
                or len(pidx_data) < (idx + 1) * 4
            ):
                return None
            pos = struct.unpack_from('<3f', trs_data, idx * 0x30)
            p_idx = struct.unpack_from('<i', pidx_data, idx * 4)[0]
            hops = 0
            while p_idx >= 0 and hops < 256:
                hops += 1
                if len(trs_data) < (p_idx + 1) * 0x30 or len(pidx_data) < (p_idx + 1) * 4:
                    return None
                po = p_idx * 0x30
                pt = struct.unpack_from('<3f', trs_data, po)
                pq = struct.unpack_from('<4f', trs_data, po + 0x10)
                ps = struct.unpack_from('<3f', trs_data, po + 0x20)
                pos = (pos[0] * ps[0], pos[1] * ps[1], pos[2] * ps[2])
                pos = quat_mult_vec(pq, pos)
                pos = (pos[0] + pt[0], pos[1] + pt[1], pos[2] + pt[2])
                p_idx = struct.unpack_from('<i', pidx_data, p_idx * 4)[0]
            if not all(math.isfinite(c) for c in pos):
                return None
            return pos

        hits_by_offset = {}
        for i, found in ptr_candidates_by_sample.items():
            for eoff, ptr in found:
                hit = good_ptrs.get(ptr)
                if hit is None:
                    continue
                td, idx = hit
                pos = compose(td, idx)
                if pos is None:
                    continue
                x, y, z = pos
                if not (abs(x) < 6000 and abs(z) < 6000 and -200 < y < 2000):
                    continue
                if origin is not None:
                    d = math.sqrt(
                        (x - origin[0]) ** 2
                        + (y - origin[1]) ** 2
                        + (z - origin[2]) ** 2
                    )
                    if d > 2000.0:
                        continue
                hits_by_offset.setdefault(eoff, {})[i] = (
                    round(x, 1), round(y, 1), round(z, 1)
                )

        pair_candidates = sorted(
            (
                (eoff, hits)
                for eoff, hits in hits_by_offset.items()
                if len(hits) == len(raws)
            ),
            key=lambda kv: kv[0],
        )
        print(
            f"[WE-SCAN3b] samples={len(raws)} candidate_ptrs={len(all_candidate_ptrs)} "
            f"transform_like={len(good_ptrs)} "
            f"candidates={[(hex(eo), list(h.values())) for eo, h in pair_candidates[:15]]}",
            flush=True,
        )

    def _read_entity_native_transform_positions(self, ent_ptrs):
        """World position for each entity via its OWN native Unity Transform.

        entity is itself a UnityEngine.Object (BaseEntity is a
        MonoBehaviour), so OFF.m_CachedPtr (0x10) already gives its native
        Component — no separate Collider/Model needed. From there:
            native Component -> owning GameObject (+0x20)
            GameObject -> Components[] array (+0x20)
            Components[0] -> native Transform (Transform is always slot 0;
                each entry is 0x10 bytes, pointer at +0x8)
            Transform -> TransformData (+0x28, same hop as OFF.transform_access)
            TransformData -> cached world position (+0x90)
        No parent-chain walk needed: Unity caches the composed world
        position directly on TransformData. Confirmed via a fresh
        rust-dumper SDK header (unity_object/unity_component/
        unity_game_object/unity_transform namespaces) whose other values
        matched this project's independently-confirmed offsets exactly.

        Returns a list of (x, y, z) or None, positionally aligned with
        'ent_ptrs'.
        """
        if not ent_ptrs:
            return []
        natives = self.m.batch_u64(
            [entity + OFF.m_CachedPtr for entity in ent_ptrs], attempts=1,
        )
        game_objects = self.m.batch_u64(
            [
                (n + OFF.native_component_gameobject) if _valid_user_ptr(n) else 0
                for n in natives
            ],
            attempts=1,
        )
        components_arrays = self.m.batch_u64(
            [
                (g + OFF.native_gameobject_components) if _valid_user_ptr(g) else 0
                for g in game_objects
            ],
            attempts=1,
        )
        transforms = self.m.batch_u64(
            [
                (c + OFF.native_component_entry_ptr) if _valid_user_ptr(c) else 0
                for c in components_arrays
            ],
            attempts=1,
        )
        tds = self.m.batch_u64(
            [
                (t + OFF.transform_access) if _valid_user_ptr(t) else 0
                for t in transforms
            ],
            attempts=1,
        )
        return self._batch_vec3(
            [td if _valid_user_ptr(td) else 0 for td in tds],
            OFF.native_transform_world_pos,
        )

    def _read_entity_root_positions(self, ent_ptrs):
        """World position for each entity via Model.rootBone's Transform.

        entity -> Model (OFF.model) -> rootBone (OFF.rootBone_Model) ->
        native (OFF.bone_transform) -> TransformAccess/idx (OFF.transform_access)
        -> the same TransformHierarchy parent-chain compose used for player
        bones (scale -> rotate -> translate at every ancestor). Batched per
        distinct hierarchy so entities that share one only pay for its
        localTransforms/parentIndices buffers once.

        Returns a list of (x, y, z) or None, positionally aligned with
        'ent_ptrs'.
        """
        if not ent_ptrs:
            return []

        models = self.m.batch_u64(
            [entity + OFF.model for entity in ent_ptrs], attempts=1,
        )
        roots = self.m.batch_u64(
            [
                (model + OFF.rootBone_Model) if _valid_user_ptr(model) else 0
                for model in models
            ],
            attempts=1,
        )
        internals = self.m.batch_u64(
            [
                (root + OFF.bone_transform) if _valid_user_ptr(root) else 0
                for root in roots
            ],
            attempts=1,
        )
        tds = self.m.batch_u64(
            [
                (internal + OFF.transform_access) if _valid_user_ptr(internal) else 0
                for internal in internals
            ],
            attempts=1,
        )
        idxs = self.m.batch_u64(
            [
                (internal + OFF.transform_access + 8) if _valid_user_ptr(internal) else 0
                for internal in internals
            ],
            attempts=1,
        )

        by_td = {}
        for i, td in enumerate(tds):
            if not _valid_user_ptr(td):
                continue
            idx = idxs[i] & 0xFFFFFFFF
            if idx >= 5000:  # sanity cap, mirrors TRSX_MAX_CAPACITY in model.py
                continue
            by_td.setdefault(td, []).append((i, idx))

        out = [None] * len(ent_ptrs)
        if not by_td:
            return out

        td_list = list(by_td)
        buf_ptrs = self.m.batch_u64(
            [
                addr
                for td in td_list
                for addr in (td + OFF.td_pos_base, td + OFF.td_parent_indices)
            ],
            attempts=1,
        )

        for t, td in enumerate(td_list):
            local_ptr = buf_ptrs[t * 2]
            parent_ptr = buf_ptrs[t * 2 + 1]
            if not (_valid_user_ptr(local_ptr) and _valid_user_ptr(parent_ptr)):
                continue
            entries = by_td[td]
            max_idx = max(idx for _, idx in entries)
            trs_data = self.m.read(local_ptr, (max_idx + 1) * 0x30)
            pidx_data = self.m.read(parent_ptr, (max_idx + 1) * 4)
            if not trs_data or not pidx_data:
                continue
            for i, idx in entries:
                if len(trs_data) < (idx + 1) * 0x30 or len(pidx_data) < (idx + 1) * 4:
                    continue
                pos = struct.unpack_from('<3f', trs_data, idx * 0x30)
                p_idx = struct.unpack_from('<i', pidx_data, idx * 4)[0]
                valid = True
                hops = 0
                while p_idx >= 0 and hops < 256:
                    hops += 1
                    if (
                        len(trs_data) < (p_idx + 1) * 0x30
                        or len(pidx_data) < (p_idx + 1) * 4
                    ):
                        valid = False
                        break
                    po = p_idx * 0x30
                    pt = struct.unpack_from('<3f', trs_data, po)
                    pq = struct.unpack_from('<4f', trs_data, po + 0x10)
                    ps = struct.unpack_from('<3f', trs_data, po + 0x20)
                    pos = (pos[0] * ps[0], pos[1] * ps[1], pos[2] * ps[2])
                    pos = quat_mult_vec(pq, pos)
                    pos = (pos[0] + pt[0], pos[1] + pt[1], pos[2] + pt[2])
                    p_idx = struct.unpack_from('<i', pidx_data, p_idx * 4)[0]
                if (
                    valid
                    and all(math.isfinite(c) for c in pos)
                    and abs(pos[0]) < 6000
                    and -200 < pos[1] < 2000
                    and abs(pos[2]) < 6000
                ):
                    out[i] = pos
        return out

    def _batch_vec3(self, base_ptrs, offset):
        """Read one Vec3 per base pointer in a single batch.

        A zero base pointer yields None, as does any vector that fails the world
        sanity box. Returns a list positionally aligned with 'base_ptrs'.
        """
        addresses = []
        for base in base_ptrs:
            if base:
                addresses.append(base + offset)
                addresses.append(base + offset + 8)
            else:
                addresses.append(0)
                addresses.append(0)
        values = self.m.batch_u64([a for a in addresses if a], attempts=1)

        cursor = 0
        out = []
        for index in range(len(base_ptrs)):
            if not addresses[index * 2]:
                out.append(None)
                continue
            if cursor + 1 >= len(values):
                out.append(None)
                continue
            raw = struct.pack('<QQ', values[cursor], values[cursor + 1])
            cursor += 2
            px, py, pz = struct.unpack_from('<fff', raw)
            if (
                math.isfinite(px) and math.isfinite(py) and math.isfinite(pz)
                and abs(px) < 6000 and abs(pz) < 6000 and -200 < py < 2000
                and (px * px + py * py + pz * pz) > 1.0
            ):
                out.append((px, py, pz))
            else:
                out.append(None)
        return out

    def _read_il2cpp_strings(self, pairs, max_chars=128):
        """Read many Il2CppStrings in two IOCTLs instead of one per string.

        `pairs` is [(key, string_ptr), ...]; returns {key: text}.

        Il2CppString layout: length (i32) at +0x10, UTF-16LE chars at +0x14.
        The old code batched the lengths and then issued a separate
        m.read(str_ptr + 0x14, n) per string, which is one driver round-trip
        each. With a full server that is ~45 round-trips per scan and two such
        scans (names + held items) -- the source of the 133-220 IOCTL ticks in
        [TICK-LATENCY], at ~12ms apiece.

        Character data is fetched as u64 words through batch_u64 so the whole
        set costs a single round-trip regardless of how many strings there are.
        """
        if not pairs:
            return {}
        lengths = self.m.batch_u64(
            [ptr + 0x10 for _, ptr in pairs], attempts=1
        )

        plan = []          # (key, ptr, byte_count, word_count)
        addresses = []
        for (key, ptr), raw_len in zip(pairs, lengths):
            length = raw_len & 0xFFFFFFFF
            if length <= 0 or length > max_chars:
                continue
            byte_count = length * 2
            word_count = (byte_count + 7) // 8
            plan.append((key, ptr, byte_count, word_count))
            addresses.extend(
                ptr + 0x14 + offset * 8 for offset in range(word_count)
            )
        if not plan:
            return {}

        values = self.m.batch_u64(addresses, attempts=1)

        out = {}
        cursor = 0
        for key, _, byte_count, word_count in plan:
            chunk = values[cursor:cursor + word_count]
            cursor += word_count
            if len(chunk) < word_count:
                break
            raw = struct.pack('<%dQ' % word_count, *chunk)[:byte_count]
            try:
                text = raw.decode('utf-16-le', errors='ignore').strip(chr(0))
            except Exception:
                continue
            if text:
                out[key] = text
        return out

    def _read_held_items_batch(self, pm_to_bp, on_worker=False):
        """Batch-read the held item for all mapped players.

        clActiveItem does NOT decrypt to a pointer or IL2CPP handle — its
        type is `ItemId`, and live-measured values (2026-08-22) decrypt to a
        small ~5-million-range number, far too small to be a real heap
        address. That number is an item UID, matching `Item.uid` (also typed
        `ItemId`, but stored plain — a fresh rust-dumper SDK's own note ties
        it directly to "a decrypted clActiveItem"). So the actual chain has
        to find the Item whose uid matches, by walking the belt:

          BasePlayer + inventory (0x510) -> HV wrapper
            -> wrapper + hv_slot (0x18) -> decrypt_player_inventory
            -> resolve_tagged_handle -> PlayerInventory*
          PlayerInventory + containerBelt (0x28) / wear (0x78) / main (0x60)
            -> ItemContainer*
          ItemContainer + itemList (0x78) -> List<Item>
            -> +0x10 (_items array), +0x18 (_size) — standard IL2CPP List<T>
               layout, same as BasePlayer_visiblePlayerList/renderers list
          for each Item* read: Item + uid (0xD8) == decrypted clActiveItem?
          matched Item -> itemDefinition (0xA0) -> shortName (0x28) -> string
          (offsets live-confirmed 2026-09-08 via rust-dumper-post's IL2CPP
           reflection dump -- see OFF.container_* / OFF.item_* comments)

        See OFFSET_RECOVERY.md trap #17.
        """
        if not self.wanted.get('held_item', True):
            return self._held_item_cache
        now = time.perf_counter()
        if now < self._next_held_item_scan_at:
            return self._held_item_cache
        # The tick's one-scan-per-tick budget only applies on the tick. Off it,
        # claiming a slot would take one away from a scan that is still on the
        # critical path, and decrementing shared per-tick state from another
        # thread would corrupt the tick's own accounting.
        if not on_worker and not self._take_heavy_slot():
            return self._held_item_cache
        self._next_held_item_scan_at = now + HELD_ITEM_SCAN_INTERVAL

        had_any = bool(self._held_item_cache)
        if had_any:
            diag = now >= getattr(self, '_next_held_item_ok_at', 0.0)
            if diag:
                self._next_held_item_ok_at = now + HELD_ITEM_OK_INTERVAL
        else:
            diag = now >= getattr(self, '_next_held_item_diag_at', 0.0)
            if diag:
                self._next_held_item_diag_at = now + HELD_ITEM_DIAG_INTERVAL

        if not pm_to_bp:
            if diag:
                print("[HELD-DBG] pm_to_bp empty", flush=True)
            self._held_item_cache = {}
            return self._held_item_cache

        m = self.m
        ga = self.ga
        pairs = [
            (pm, bp)
            for pm, bp in pm_to_bp.items()
            if _valid_user_ptr(bp)
        ]
        if not pairs:
            if diag:
                print(f"[HELD-DBG] 0/{len(pm_to_bp)} had a valid bp", flush=True)
            self._held_item_cache = {}
            return self._held_item_cache

        # Target: decrypted clActiveItem UID per player
        raw_active = m.batch_u64(
            [bp + OFF.cl_active_item for _, bp in pairs], attempts=1,
        )
        target_uid = {}
        for (pm, _), raw in zip(pairs, raw_active):
            if raw:
                target_uid[pm] = decrypt_cl_active_item(raw) & 0xFFFFFFFFFFFFFFFF

        active_set = set(pm_to_bp)
        all_pms = set(pm for pm, _ in pairs)
        self._held_list_cache = {
            k: v for k, v in self._held_list_cache.items()
            if (k[0] if isinstance(k, tuple) else k) in active_set
        }
        lists = dict(self._held_list_cache)
        need_resolve = [pm for pm in all_pms if (pm, 'belt') not in lists or (pm, 'main') not in lists]
        cache_hits = len(all_pms) - len(need_resolve)

        # Per-stage counts for the "item_list_valid=0" diagnostic below --
        # that single collapsed message can't say whether the chain died at
        # the inventory wrapper, the resolved PlayerInventory*, the
        # container pointers, or the item_list read itself.
        n_wrap = n_hv_nonzero = n_decoded = n_inv = n_containers = n_list_vals = 0
        n_ptr_belt = n_ptr_wear = n_ptr_main = 0
        n_listptr_belt = n_listptr_wear = n_listptr_main = 0
        sample_hv = sample_decoded = 0

        if need_resolve:
            resolve_pairs = [(pm, bp) for pm, bp in pairs if pm in need_resolve]
            inv_wrappers = m.batch_u64(
                [bp + OFF.inventory for _, bp in resolve_pairs], attempts=1,
            )
            wrap_pairs = [
                (pm, w) for (pm, _), w in zip(resolve_pairs, inv_wrappers)
                if _valid_user_ptr(w)
            ]
            n_wrap = len(wrap_pairs)
            if wrap_pairs:
                hv_vals = m.batch_u64(
                    [w + OFF.hv_slot for _, w in wrap_pairs], attempts=1,
                )
                decoded = {}
                for (pm, _), hv in zip(wrap_pairs, hv_vals):
                    if hv:
                        n_hv_nonzero += 1
                        if not sample_hv:
                            sample_hv = hv
                        decoded[pm] = decrypt_player_inventory(hv)
                        if not sample_decoded:
                            sample_decoded = decoded[pm]
                n_decoded = len(decoded)
                resolved = resolve_tagged_handles(m, decoded.values(), ga)
                inv_ptrs = {}
                for pm, handle in decoded.items():
                    ptr = resolved.get(handle, 0)
                    if _valid_user_ptr(ptr):
                        inv_ptrs[pm] = ptr
                n_inv = len(inv_ptrs)
                if inv_ptrs:
                    pm_list = list(inv_ptrs)
                    c_addrs = []
                    for pm in pm_list:
                        iptr = inv_ptrs[pm]
                        c_addrs.append(iptr + OFF.container_belt)
                        c_addrs.append(iptr + OFF.container_wear)
                        c_addrs.append(iptr + OFF.container_main)
                    c_vals = m.batch_u64(c_addrs, attempts=1)

                    containers = {}
                    n_ptr_belt = n_ptr_wear = n_ptr_main = 0
                    for i, pm in enumerate(pm_list):
                        cb, cw, cm = c_vals[i * 3], c_vals[i * 3 + 1], c_vals[i * 3 + 2]
                        if _valid_user_ptr(cb): containers[(pm, 'belt')] = cb; n_ptr_belt += 1
                        if _valid_user_ptr(cw): containers[(pm, 'wear')] = cw; n_ptr_wear += 1
                        if _valid_user_ptr(cm): containers[(pm, 'main')] = cm; n_ptr_main += 1
                    n_containers = len(containers)

                    if containers:
                        c_keys = list(containers)
                        list_vals = m.batch_u64(
                            [containers[k] + OFF.item_list for k in c_keys],
                            attempts=1,
                        )
                        for k, v in zip(c_keys, list_vals):
                            if _valid_user_ptr(v):
                                lists[k] = v
                                ctype_of_k = k[1] if isinstance(k, tuple) else 'belt'
                                if ctype_of_k == 'belt': n_listptr_belt += 1
                                elif ctype_of_k == 'wear': n_listptr_wear += 1
                                elif ctype_of_k == 'main': n_listptr_main += 1
                        n_list_vals = sum(1 for v in list_vals if _valid_user_ptr(v))
                        self._held_list_cache.update(
                            {k: lists[k] for k in c_keys if k in lists}
                        )

        if not lists:
            if diag:
                print(
                    f"[HELD-DBG] pairs={len(pairs)} item_list_valid=0 "
                    f"(wrap={n_wrap} hv_nz={n_hv_nonzero} decoded={n_decoded} "
                    f"inv={n_inv} containers={n_containers} list_vals={n_list_vals} "
                    f"sample_hv=0x{sample_hv:X} sample_decoded=0x{sample_decoded:X})",
                    flush=True,
                )
            self._held_item_cache = {}
            self._belt_cache = {}
            self._wear_cache = {}
            self._main_cache = {}
            return self._held_item_cache

        # List<Item>._items (+0x10) / ._size (+0x18) — standard IL2CPP List<T>.
        c_keys = list(lists)
        addrs = []
        for k in c_keys:
            addrs.append(lists[k] + OFF.ListHashSet_vals)
            addrs.append(lists[k] + OFF.ListHashSet_size)
        vals = m.batch_u64(addrs, attempts=1)
        container_slots = {}  # (pm, ctype) -> (items_array_ptr, count)
        # Raw size seen per label, BEFORE the max_len cap -- if the label
        # roles are still swapped, whichever position is actually the big
        # backpack will show up with a much higher raw size under the WRONG
        # label (and possibly get rejected by that label's cap entirely).
        ctype_sizes = {'belt': [], 'wear': [], 'main': []}
        for i, k in enumerate(c_keys):
            arr = vals[i * 2]
            size = vals[i * 2 + 1] & 0xFFFFFFFF
            ctype_str = k[1] if isinstance(k, tuple) else 'belt'
            if _valid_user_ptr(arr) and 0 < size < 1000:
                ctype_sizes.setdefault(ctype_str, []).append(size)
            max_len = 36 if ctype_str == 'main' else 12
            if _valid_user_ptr(arr) and 0 < size <= max_len:
                container_slots[k] = (arr, size)
            else:
                self._held_list_cache.pop(k, None)

        if not container_slots:
            if diag:
                print(f"[HELD-DBG] item_list_valid={len(lists)} belt_slots=0", flush=True)
            self._held_item_cache = {}
            self._belt_cache = {}
            self._wear_cache = {}
            self._main_cache = {}
            return self._held_item_cache

        item_addrs = []
        item_owner = []
        item_slots = []
        for k, (arr, size) in container_slots.items():
            pm = k[0] if isinstance(k, tuple) else k
            ctype = k[1] if isinstance(k, tuple) else 'belt'
            for i in range(size):
                item_addrs.append(arr + OFF.array_payload + i * 8)
                item_owner.append((pm, ctype))
                item_slots.append(i)

        item_ptrs = m.batch_u64(item_addrs, attempts=1)
        valid_items = [
            (owner, ptr, slot_i) for owner, ptr, slot_i in zip(item_owner, item_ptrs, item_slots)
            if _valid_user_ptr(ptr)
        ]
        if not valid_items:
            self._held_item_cache = {}
            self._belt_cache = {}
            self._wear_cache = {}
            self._main_cache = {}
            return self._held_item_cache

        uid_vals = m.batch_u64(
            [ptr + OFF.item_uid for _, ptr, _ in valid_items], attempts=1,
        )
        matched = {}  # pm -> item_ptr
        for (owner, ptr, _), uid in zip(valid_items, uid_vals):
            pm, _ctype = owner
            want = target_uid.get(pm)
            # A zero uid is what an unread/uninitialised slot looks like on both
            # sides, so requiring both to be non-zero is what keeps "nobody is
            # holding anything" from matching the first empty slot it sees.
            if not want:
                continue
            if (uid & 0xFFFFFFFFFFFFFFFF) == want:
                # Not restricted to ctype == 'belt' any more: the active item is
                # normally a belt slot, but making the match depend on the
                # belt/wear/main labelling being right coupled held-item
                # detection to a container ordering that this build already
                # rotated once (see OFF.container_*). The uid is unique across
                # the whole inventory, so matching on it alone is both safe and
                # label-independent.
                matched[pm] = ptr

        # Fallback for players whose clActiveItem is not replicated. In the
        # 2026-09-05 run 34 of 41 mapped players read a literal 0 at
        # BasePlayer+0x588, so the uid path can only ever explain the 7 that
        # were populated (it matched all 7). Item.heldEntity is an inline
        # EntityRef whose BaseEntity* is non-null only for the item the player
        # currently has deployed, and it is replicated like any other entity
        # field -- so it covers the rest. One extra batch read over pointers we
        # already hold.
        from_held_entity = 0
        unmatched = [
            (owner[0], ptr) for owner, ptr, _ in valid_items
            if owner[0] not in matched
        ]
        if unmatched:
            he_vals = m.batch_u64(
                [ptr + OFF.item_heldEntity for _, ptr in unmatched], attempts=1,
            )
            claims = {}
            for (pm, ptr), he in zip(unmatched, he_vals):
                if _valid_user_ptr(he):
                    claims.setdefault(pm, []).append(ptr)
            for pm, claimed in claims.items():
                # A player can only deploy one item at a time. More than one
                # claim means a stale EntityRef is still set somewhere, and
                # picking either would be a guess -- leave that player blank.
                if len(claimed) == 1:
                    matched[pm] = claimed[0]
                    from_held_entity += 1

        itemdef_vals = m.batch_u64(
            [ptr + OFF.item_definition for _, ptr, _ in valid_items], attempts=1,
        )
        stage3 = [
            (owner, itemdef, slot_i, ptr)
            for (owner, ptr, slot_i), itemdef in zip(valid_items, itemdef_vals)
            if _valid_user_ptr(itemdef)
        ]
        if not stage3:
            self._held_item_cache = {}
            self._belt_cache = {}
            self._wear_cache = {}
            self._main_cache = {}
            return self._held_item_cache

        name_cache = self._itemdef_name_cache
        cold = [(idx, itemdef) for idx, (owner, itemdef, slot_i, ptr) in enumerate(stage3)
                if itemdef not in name_cache]

        if cold:
            str_vals = m.batch_u64(
                [itemdef + OFF.itemdef_shortName for _, itemdef in cold],
                attempts=1,
            )
            stage4 = []
            itemdef_by_idx = {}
            for (idx, itemdef), str_ptr in zip(cold, str_vals):
                if _valid_user_ptr(str_ptr):
                    stage4.append((idx, str_ptr))
                    itemdef_by_idx[idx] = itemdef
            if stage4:
                for idx, short_name in self._read_il2cpp_strings(
                    stage4, max_chars=128
                ).items():
                    if short_name:
                        name_cache[itemdef_by_idx[idx]] = short_name

        all_pms = set(owner[0] for owner, _, _, _ in stage3)
        belt_results = {pm: [None] * 6 for pm in all_pms}
        wear_results = {pm: [None] * 6 for pm in all_pms}
        main_results = {pm: [None] * 30 for pm in all_pms}
        held_results = {}

        for owner, itemdef, slot_i, ptr in stage3:
            pm, ctype = owner
            cached_name = name_cache.get(itemdef)
            if cached_name:
                disp_name = ITEM_DISPLAY_NAMES.get(cached_name, cached_name)
                item_info = (cached_name, disp_name)
                if ctype == 'belt' and 0 <= slot_i < 6:
                    belt_results[pm][slot_i] = item_info
                elif ctype == 'wear' and 0 <= slot_i < 6:
                    wear_results[pm][slot_i] = item_info
                elif ctype == 'main' and 0 <= slot_i < 30:
                    main_results[pm][slot_i] = item_info

                if matched.get(pm) == ptr:
                    held_results[pm] = disp_name

        if diag or (held_results and not had_any):
            # Every stage count on one line: which one is zero says where the
            # chain stops, without another round of guess-and-check.
            # size=belt(max)/wear(max)/main(max) is the raw List<Item>.size
            # seen per label BEFORE the max_len cap -- whichever label is
            # actually the big backpack should show a much higher max than
            # the other two. If "main" isn't the highest, the role labels
            # are still swapped (see OFF.container_* /
            # [[rust-esp-offset-sources-conflict-trust-live-diagnostic]]).
            size_str = "/".join(
                f"{ct}={max(v) if v else 0}({len(v)})"
                for ct, v in ctype_sizes.items()
            )
            print(
                f"[HELD-DBG] held={len(held_results)}/{len(pairs)} "
                f"uid={sum(1 for v in target_uid.values() if v)} "
                f"viaHeldEntity={from_held_entity} "
                f"containers={len(container_slots)} items={len(valid_items)} "
                f"named={len(stage3)} sizes[{size_str}] "
                f"ptr[belt={n_ptr_belt} wear={n_ptr_wear} main={n_ptr_main}] "
                f"listptr[belt={n_listptr_belt} wear={n_listptr_wear} main={n_listptr_main}]",
                flush=True,
            )

        self._held_item_cache = held_results
        self._belt_cache = belt_results
        self._wear_cache = wear_results
        self._main_cache = main_results
        return self._held_item_cache

    def _read_world_item_names(self, ent_ptrs):
        """Batch-resolve the real item name for a set of DroppedItem entities.

        Chain: DroppedItem/WorldItem + OFF.world_item_item (0x208) -> Item*
          -> Item + OFF.item_definition (0xC0) -> ItemDefinition*
            -> ItemDefinition + OFF.itemdef_shortName (0x28) -> Il2CppString*

        Same fields _read_held_items_batch already uses from Item onward
        (item_definition/itemdef_shortName, both confirmed independently) —
        only the first hop differs: a plain field on WorldItem, no
        HiddenValue/decrypt needed, per the rust-dumper SDK dump 2026-08-21.

        Returns {entity_ptr: display_name}.
        """
        if not ent_ptrs:
            return {}
        items = self.m.batch_u64(
            [entity + OFF.world_item_item for entity in ent_ptrs], attempts=1,
        )
        stage1 = [
            (entity, item) for entity, item in zip(ent_ptrs, items)
            if _valid_user_ptr(item)
        ]
        if not stage1:
            return {}

        itemdefs = self.m.batch_u64(
            [item + OFF.item_definition for _, item in stage1], attempts=1,
        )
        stage2 = [
            (entity, itemdef) for (entity, _), itemdef in zip(stage1, itemdefs)
            if _valid_user_ptr(itemdef)
        ]
        if not stage2:
            return {}

        str_ptrs = self.m.batch_u64(
            [itemdef + OFF.itemdef_shortName for _, itemdef in stage2],
            attempts=1,
        )
        stage3 = [
            (entity, str_ptr) for (entity, _), str_ptr in zip(stage2, str_ptrs)
            if _valid_user_ptr(str_ptr)
        ]
        if not stage3:
            return {}

        return {
            entity: ITEM_DISPLAY_NAMES.get(short_name, short_name)
            for entity, short_name in self._read_il2cpp_strings(
                stage3, max_chars=128
            ).items()
        }

    def _read_player_names_batch(self, pm_to_bp, on_worker=False):
        """Batch-read player display names from BasePlayer._displayName.

        Chain: BasePlayer + _displayName (0x390) → Il2CppString*
          → length @ +0x10, chars (UTF-16LE) @ +0x14

        Names are stable for a given (pm, bp) pair — only reads for players
        not already cached under their *current* bp.
        """
        if not self.wanted.get('names', True):
            return self._player_name_cache
        now = time.perf_counter()
        if now < self._next_player_name_scan_at:
            return self._player_name_cache
        # Same reasoning as the held-item scan (trap #42): off the tick, the
        # tick's one-scan budget does not apply, and decrementing it from
        # another thread would corrupt the tick's own accounting.
        if not on_worker and not self._take_heavy_slot():
            return self._player_name_cache
        self._next_player_name_scan_at = now + PLAYER_NAME_SCAN_INTERVAL

        if not pm_to_bp:
            return self._player_name_cache

        m = self.m
        if not hasattr(self, '_player_name_bp_cache'):
            self._player_name_bp_cache = {}  # pm -> bp the cached name came from

        # Evict every pass (not only when nothing is missing), and key
        # freshness on (pm, bp) identity, not just pm. A departed player's
        # `pm` address gets reused — by a new player, or by a bot/NPC — and
        # the *same* pm can end up bound to a *different* bp without ever
        # leaving pm_to_bp (immediate reuse, no visible gap). Checking only
        # "pm not in cache" treated the reused pm as "already has a name"
        # and kept the previous occupant's name forever, since a busy server
        # almost always has *some* other player still resolving, so the old
        # "only evict when nothing is missing" branch rarely ran at all.
        # Reported live: bots showing real player names, or a player's fake
        # name only flipping to their real one "after a while". Now bounded
        # to at most PLAYER_NAME_SCAN_INTERVAL (1s) staleness in every case.
        active = set(pm_to_bp)
        stale = {
            pm for pm in self._player_name_cache
            if pm not in active or self._player_name_bp_cache.get(pm) != pm_to_bp.get(pm)
        }
        for pm in stale:
            self._player_name_cache.pop(pm, None)
            self._player_name_bp_cache.pop(pm, None)

        # Only scan players we don't have a name for under their current bp
        missing = [
            (pm, bp)
            for pm, bp in pm_to_bp.items()
            if _valid_user_ptr(bp) and pm not in self._player_name_cache
        ]
        if not missing:
            return self._player_name_cache

        # Stage 1: read _displayName pointer from each BasePlayer
        str_ptrs = m.batch_u64(
            [bp + OFF._displayName for _, bp in missing],
            attempts=1,
        )
        stage1 = []  # (pm, str_ptr)
        bp_by_pm = {}
        for (pm, bp), sp in zip(missing, str_ptrs):
            if _valid_user_ptr(sp):
                stage1.append((pm, sp))
                bp_by_pm[pm] = bp
        if not stage1:
            return self._player_name_cache

        # Stage 2: lengths + characters, two IOCTLs total for every name.
        resolved = self._read_il2cpp_strings(stage1, max_chars=64)
        self._player_name_cache.update(resolved)
        for pm in resolved:
            self._player_name_bp_cache[pm] = bp_by_pm[pm]
        return self._player_name_cache

    def _take_snapshot(self):
        """Capture a coherent snapshot of players + VP for the render thread."""
        players = []
        for player in self.players:
            copied = dict(player)
            pos = copied.get('pos')
            if pos is not None:
                copied['pos'] = tuple(pos)
            players.append(copied)
        vp = tuple(self.vp_matrix) if self.vp_matrix is not None else None
        world_ents = list(self.world_entities)
        snap = (time.perf_counter(), players, vp, world_ents)
        with self._lock:
            self._snap_prev = self._snap_cur
            self._snap_cur = snap

    def _track_pose_freeze(self, alpha, now):
        """Measure how often/how long the bone-blend alpha sits saturated
        at 1.0 between get_snapshot() calls -- see the comment on
        _pose_frames_total in __init__ for why that is the freeze-then-jump
        symptom, not a guess about it."""
        if not hasattr(self, "_pose_frames_total"):
            # Many tests build a RustGame/RustGameModel via object.__new__
            # and never run __init__ -- self-heal instead of requiring every
            # one of them to know about this counter.
            self._pose_frames_total = 0
            self._pose_frames_saturated = 0
            self._pose_freeze_streak_start = None
            self._pose_freeze_streak_ms_sum = 0.0
            self._pose_freeze_streak_count = 0
            self._pose_freeze_streak_max_ms = 0.0
            self._last_pose_freeze_log_at = 0.0
        self._pose_frames_total += 1
        saturated = alpha >= 1.0 - 1e-9
        if saturated:
            self._pose_frames_saturated += 1
            if self._pose_freeze_streak_start is None:
                self._pose_freeze_streak_start = now
        elif self._pose_freeze_streak_start is not None:
            streak_ms = (now - self._pose_freeze_streak_start) * 1000.0
            self._pose_freeze_streak_ms_sum += streak_ms
            self._pose_freeze_streak_count += 1
            self._pose_freeze_streak_max_ms = max(
                self._pose_freeze_streak_max_ms, streak_ms
            )
            self._pose_freeze_streak_start = None

        if now - self._last_pose_freeze_log_at >= POSE_FREEZE_LOG_INTERVAL:
            total = self._pose_frames_total
            frozen_pct = (
                100.0 * self._pose_frames_saturated / total if total else 0.0
            )
            streaks = self._pose_freeze_streak_count
            avg_ms = (
                self._pose_freeze_streak_ms_sum / streaks if streaks else 0.0
            )
            print(
                f"[POSE-FREEZE] frozen={frozen_pct:.0f}% "
                f"({self._pose_frames_saturated}/{total} frames) "
                f"streaks={streaks} avg={avg_ms:.1f}ms "
                f"max={self._pose_freeze_streak_max_ms:.1f}ms",
                flush=True,
            )
            self._pose_frames_total = 0
            self._pose_frames_saturated = 0
            self._pose_freeze_streak_ms_sum = 0.0
            self._pose_freeze_streak_count = 0
            self._pose_freeze_streak_max_ms = 0.0
            self._last_pose_freeze_log_at = now

    def _track_bone_stretch(self, pm, shifted, blended):
        """Measure limb stretch on the *rendered* pose, one player a frame.

        `[BONE-LINK]` already checks this at composition time and has never
        fired, which means whatever pulls a bone out of a running player's
        body happens after composition -- in the body prediction or the pose
        blend here. Nothing measured that, so it stayed a guess across three
        sessions of reports.

        Sampling one player per frame rather than all of them keeps it at
        ~20 distance checks a frame instead of ~460: at 144 fps that is
        still 144 player-frames a second, far more than enough to catch a
        1-2% event, and it does not repeat the mistake that made this pass
        5x more expensive in the first place.
        """
        if not hasattr(self, '_stretch_samples'):
            self._stretch_samples = 0
            self._stretch_over = 0
            self._stretch_worst = None
            self._stretch_cursor = 0
            self._last_stretch_log_at = time.perf_counter()
        self._stretch_cursor += 1
        if self._stretch_cursor % BONE_STRETCH_SAMPLE_EVERY:
            return
        self._stretch_samples += 1
        worst = 0.0
        worst_link = None
        for a, b in SCI_BONE_LINKS:
            pa = shifted.get(a)
            pb = shifted.get(b)
            if pa is None or pb is None:
                continue
            d = math.dist(pa, pb)
            if d > worst:
                worst = d
                worst_link = (a, b)
        if worst > BONE_STRETCH_LIMIT_M:
            self._stretch_over += 1
            if self._stretch_worst is None or worst > self._stretch_worst[1]:
                self._stretch_worst = (pm, worst, worst_link, blended)
        now = time.perf_counter()
        if now - self._last_stretch_log_at < BONE_STRETCH_LOG_INTERVAL:
            return
        self._last_stretch_log_at = now
        over, seen, worst_seen = (
            self._stretch_over, self._stretch_samples, self._stretch_worst
        )
        self._stretch_over = 0
        self._stretch_samples = 0
        self._stretch_worst = None
        if not over or not seen:
            return
        pm_w, dist_w, link_w, blend_w = worst_seen
        print(
            f"[BONE-STRETCH] over={over}/{seen} "
            f"worst={dist_w:.2f}m link={link_w} pm=0x{pm_w:X} "
            f"phase={'blended' if blend_w else 'raw'}",
            flush=True,
        )

    def _track_bone_phase_mix(self, pm, blended, dissent, total):
        """Report how often a whole skeleton had to snap, and how narrowly.

        Originally counted a *mix* of blended and raw bones inside one
        skeleton, because that mix was the suspected cause of limbs flying
        out of a running player. It was: 1-2% of skeleton-frames, worst case
        one bone blended against twenty raw. The mix cannot happen any more
        -- the decision is per skeleton now -- so what is worth watching is
        the price of that: how many skeletons fall back to raw, and how few
        dissenting bones it took. A `worst=1/21` says one noisy bone is
        costing twenty good ones their interpolation, which would be an
        argument for widening BONE_POSE_BLEND_MAX_SQ; `worst=21/21` says the
        pose genuinely changed and snapping is correct.

        Self-healing (hasattr): the tests build these objects with
        object.__new__ and never run __init__.
        """
        # Guard on _bone_phase_raw, not on _bone_phase_total_skeletons.
        # Those two used to be initialised in different places with
        # different names, so the guard passed on a fully-constructed object
        # while the increment below found nothing -- an AttributeError on
        # the render thread, in-game, on the first skeleton. Whatever this
        # checks has to be the thing it creates.
        if not hasattr(self, '_bone_phase_raw'):
            self._bone_phase_total_skeletons = 0
            self._bone_phase_raw = 0
            self._bone_phase_worst = None
            self._last_bone_phase_log_at = time.perf_counter()
        self._bone_phase_total_skeletons += 1
        if not blended:
            self._bone_phase_raw += 1
            worst = self._bone_phase_worst
            if worst is None or dissent < worst[1]:
                self._bone_phase_worst = (pm, dissent, total)
        now = time.perf_counter()
        if now - self._last_bone_phase_log_at < BONE_PHASE_MIX_LOG_INTERVAL:
            return
        self._last_bone_phase_log_at = now
        raw = self._bone_phase_raw
        seen = self._bone_phase_total_skeletons
        worst = self._bone_phase_worst
        self._bone_phase_raw = 0
        self._bone_phase_total_skeletons = 0
        self._bone_phase_worst = None
        if not raw:
            return
        detail = ""
        if worst is not None:
            detail = (
                f" narrowest pm=0x{worst[0]:X} {worst[1]}/{worst[2]} bones"
            )
        print(
            f"[BONE-PHASE] raw={raw}/{seen} ({raw / seen * 100.0:.0f}%)"
            f"{detail}",
            flush=True,
        )
    def get_snapshot(self, render_at=None):
        """Predict moving players from the latest two coherent worker samples."""
        t_snap0 = time.perf_counter()
        target_at = render_at
        if not isinstance(target_at, (int, float)) or not math.isfinite(target_at):
            target_at = t_snap0
        try:
            return self._predict_from_snapshots(target_at)
        finally:
            self._track_snapshot_cost(t_snap0)

    def _track_snapshot_cost(self, started):
        """Report what one render frame pays for extrapolation.

        Self-healing (hasattr) like _track_pose_freeze: the tests build these
        objects with object.__new__ and never run __init__.
        """
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if not hasattr(self, '_snap_cost_n'):
            self._snap_cost_n = 0
            self._snap_cost_sum = 0.0
            self._snap_cost_max = 0.0
            self._last_snap_profile_at = started
        self._snap_cost_n += 1
        self._snap_cost_sum += elapsed_ms
        if elapsed_ms > self._snap_cost_max:
            self._snap_cost_max = elapsed_ms
        now = time.perf_counter()
        window = now - self._last_snap_profile_at
        if window < SNAP_PROFILE_LOG_INTERVAL or not self._snap_cost_n:
            return
        print(
            f"[SNAP-PROFILE] {self._snap_cost_sum / self._snap_cost_n:.2f}ms avg "
            f"max={self._snap_cost_max:.2f}ms n={self._snap_cost_n} "
            f"({self._snap_cost_n / window:.0f}/s, "
            f"{self._snap_cost_sum / window / 10.0:.0f}% of the render thread)",
            flush=True,
        )
        self._snap_cost_n = 0
        self._snap_cost_sum = 0.0
        self._snap_cost_max = 0.0
        self._last_snap_profile_at = now

    def _reset_prediction_error_window(self):
        self._pred_err_n = 0
        self._pred_err_overshoot_sum = 0.0
        self._pred_err_perp_sum = 0.0
        self._pred_err_speed_sum = 0.0
        self._pred_err_worst = 0.0
        self._rig_lag_n = 0
        self._rig_lag_sum = 0.0
        self._rig_lag_speed_sum = 0.0
        self._rig_rest_n = 0
        self._rig_rest_sum = 0.0

    def _track_prediction_error(self, prior, cur_pos, cur_ts, velocity, bones):
        """Score the prediction against the two things it can be wrong about.

        Called once per player per *new snapshot*, from the same branch that
        recomputes velocity -- not once per rendered frame -- so it costs a
        handful of flops per player per tick.

        Both numbers are horizontal only. Vertical is separately clamped
        (see the `abs(velocity[1]) < 0.35` guard below) and mixing it in
        would put crouch/jump noise into a measurement about ground travel.

        Self-healing (hasattr) like the other trackers: tests build these
        objects with object.__new__ and never run __init__.
        """
        if not hasattr(self, '_pred_err_n'):
            self._reset_prediction_error_window()
            self._next_pred_err_log_at = 0.0

        hips = bones.get(0) if bones else None
        speed = math.sqrt(velocity[0] * velocity[0] + velocity[2] * velocity[2])

        if speed <= PRED_ERR_RESTING_MAX_SPEED:
            # The baseline: on a standing player the hips sit directly over
            # the entity origin, so whatever this reads is the rig's own
            # constant offset and has to be subtracted from the moving
            # number before that one means anything.
            if hips is not None:
                dx = hips[0] - cur_pos[0]
                dz = hips[2] - cur_pos[2]
                self._rig_rest_n += 1
                self._rig_rest_sum += math.sqrt(dx * dx + dz * dz)
        elif speed >= PRED_ERR_MOVING_MIN_SPEED:
            ux = velocity[0] / speed
            uz = velocity[2] / speed

            if hips is not None:
                # Positive => the drawn rig trails the position we anchor to.
                dx = hips[0] - cur_pos[0]
                dz = hips[2] - cur_pos[2]
                self._rig_lag_n += 1
                self._rig_lag_sum += -(dx * ux + dz * uz)
                self._rig_lag_speed_sum += speed

            if prior is not None and 'pos' in prior:
                gap = cur_ts - prior.get('snapshot_ts', cur_ts)
                if 0.0 < gap <= 0.5:
                    pv = prior.get('velocity', (0.0, 0.0, 0.0))
                    horizon = gap + prior.get('lead', 0.0)
                    ex = prior['pos'][0] + pv[0] * horizon - cur_pos[0]
                    ez = prior['pos'][2] + pv[2] * horizon - cur_pos[2]
                    # Positive => we predicted further along than the player
                    # actually got, i.e. the skeleton runs ahead of the body.
                    overshoot = ex * ux + ez * uz
                    self._pred_err_n += 1
                    self._pred_err_overshoot_sum += overshoot
                    self._pred_err_perp_sum += abs(ex * uz - ez * ux)
                    self._pred_err_speed_sum += speed
                    if abs(overshoot) > abs(self._pred_err_worst):
                        self._pred_err_worst = overshoot

        now = time.perf_counter()
        if now < self._next_pred_err_log_at:
            return
        self._next_pred_err_log_at = now + PRED_ERR_LOG_INTERVAL
        if not (self._pred_err_n or self._rig_lag_n or self._rig_rest_n):
            return

        if self._pred_err_n:
            n = self._pred_err_n
            speed_avg = self._pred_err_speed_sum / n
            overshoot_avg = self._pred_err_overshoot_sum / n
            ms = overshoot_avg / speed_avg * 1000.0 if speed_avg > 0 else 0.0
            print(
                f"[PRED-ERR] n={n} overshoot={overshoot_avg:+.3f}m "
                f"({ms:+.1f}ms at {speed_avg:.1f}m/s) "
                f"|perp|={self._pred_err_perp_sum / n:.3f}m "
                f"worst={self._pred_err_worst:+.3f}m",
                flush=True,
            )
        if self._rig_lag_n or self._rig_rest_n:
            parts = []
            if self._rig_lag_n:
                speed_avg = self._rig_lag_speed_sum / self._rig_lag_n
                lag = self._rig_lag_sum / self._rig_lag_n
                ms = lag / speed_avg * 1000.0 if speed_avg > 0 else 0.0
                parts.append(
                    f"moving n={self._rig_lag_n} rig behind anchor "
                    f"{lag:+.3f}m ({ms:+.1f}ms at {speed_avg:.1f}m/s)"
                )
            if self._rig_rest_n:
                parts.append(
                    f"at rest n={self._rig_rest_n} "
                    f"|offset|={self._rig_rest_sum / self._rig_rest_n:.3f}m"
                )
            print("[RIG-LAG] " + " | ".join(parts), flush=True)
        self._reset_prediction_error_window()

    def _predict_from_snapshots(self, now):
        """The prediction pass itself: two worker snapshots in, one
        render-ready player list out. Split out of get_snapshot() only so
        that method can time it -- see _track_snapshot_cost.
        """
        with self._lock:
            prev = self._snap_prev
            cur = self._snap_cur
        if cur is None:
            return [], [], None, "", 0.0

        world_ents = cur[3] if len(cur) > 3 else []

        if prev is None:
            return cur[1], world_ents, cur[2], self.diag, self.tick_ms

        dt_snap = cur[0] - prev[0]
        if dt_snap <= 1e-4 or dt_snap > 0.5:
            return cur[1], world_ents, cur[2], self.diag, self.tick_ms

        # dt_snap is the just-measured gap between the two latest snapshots
        # -- accurate, but a single sample, and used bare as alpha's
        # denominator it made alpha saturate (pose freeze) the moment any
        # tick ran slower than the one before it. Smooth it across several
        # real snapshot boundaries; the update fires only when a *new*
        # snapshot has actually landed (cur[0] changed), not once per
        # render call, since dt_snap itself is constant between two ticks.
        if cur[0] != getattr(self, "_pose_last_cur_ts", None):
            self._pose_last_cur_ts = cur[0]
            prior_ewma = getattr(self, "_dt_snap_ewma", None)
            self._dt_snap_ewma = (
                dt_snap if prior_ewma is None
                else prior_ewma + (dt_snap - prior_ewma) * DT_SNAP_EWMA_WEIGHT
            )
        alpha_dt = getattr(self, "_dt_snap_ewma", None) or dt_snap

        # Same alpha for every player in this call -- it depends only on
        # `now`, cur[0] and alpha_dt, none of which vary per-player. Computed
        # once here (was recomputed per-player below, always to the same
        # value) and fed straight into the [POSE-FREEZE] accounting.
        alpha = max(0.0, min(1.15, (now - cur[0]) / alpha_dt))
        self._track_pose_freeze(alpha, now)

        prev_by_pm = {p.get('pm'): p for p in prev[1]}
        predicted_players = []
        active_pms = set()
        for p in cur[1]:
            pm = p.get('pm')
            active_pms.add(pm)
            prev_p = prev_by_pm.get(pm)
            if not prev_p:
                predicted_players.append(p)
                continue

            if p.get('sleeping', False):
                self._render_motion_cache[pm] = {
                    'velocity': (0.0, 0.0, 0.0),
                    'snapshot_ts': cur[0],
                    'last_seen': now,
                }
                predicted_players.append(p)
                continue

            prev_pos = prev_p['pos']
            cur_pos = p['pos']
            distance = self._dist3(prev_pos, cur_pos)
            state = self._render_motion_cache.get(pm)
            prior_state = state
            old_velocity = (
                state.get('velocity', (0.0, 0.0, 0.0))
                if state
                else (0.0, 0.0, 0.0)
            )

            game_vel = p.get('vel')
            if state is None or state.get('snapshot_ts') != cur[0]:
                if distance > 4.0:
                    velocity = (0.0, 0.0, 0.0)
                elif game_vel is not None:
                    # The engine's own velocity is instantaneous. Differencing
                    # two position samples gives the *average* over the last
                    # interval, so it is inherently half a tick stale before the
                    # 0.70 lerp below adds any more — which is what made boxes
                    # and skeletons trail a moving player. Smooth this one only
                    # enough to kill single-sample noise.
                    speed = math.sqrt(sum(v * v for v in game_vel))
                    if speed > 25.0:
                        scale = 25.0 / speed
                        game_vel = tuple(v * scale for v in game_vel)
                    velocity = tuple(
                        old + (raw - old) * 0.85
                        for old, raw in zip(old_velocity, game_vel)
                    )
                elif distance > 0.002:
                    raw_velocity = (
                        (cur_pos[0] - prev_pos[0]) / dt_snap,
                        (cur_pos[1] - prev_pos[1]) / dt_snap,
                        (cur_pos[2] - prev_pos[2]) / dt_snap,
                    )
                    speed = math.sqrt(sum(v * v for v in raw_velocity))
                    if speed > 20.0:
                        scale = 20.0 / speed
                        raw_velocity = tuple(v * scale for v in raw_velocity)
                    velocity = tuple(
                        old + (raw - old) * 0.70
                        for old, raw in zip(old_velocity, raw_velocity)
                    )
                else:
                    # A repeated network sample should not instantly stop a
                    # running player; decay the last measured velocity instead.
                    velocity = tuple(v * 0.88 for v in old_velocity)

                state = {
                    'velocity': velocity,
                    # Kept so the *next* snapshot can re-run this one's
                    # prediction and see where it landed -- see
                    # _track_prediction_error. Without the position, the
                    # residual is not computable after the fact.
                    'pos': cur_pos,
                    'snapshot_ts': cur[0],
                    'last_seen': now,
                }
                self._render_motion_cache[pm] = state
                self._track_prediction_error(
                    prior_state, cur_pos, cur[0], velocity, p.get('bones')
                )
            else:
                velocity = old_velocity

            # Snapshot time is taken after the batch completes, so `now - cur[0]`
            # is the sample's age. The extra lead covers the read itself: a full
            # half-batch when differencing (that velocity is centred half an
            # interval in the past), only the IOCTL round-trip when the engine
            # gave us an instantaneous vector.
            lead = (
                min(dt_snap * 0.25, 0.012)
                if game_vel is not None
                else min(dt_snap * 0.5, 0.030)
            )
            state['lead'] = lead
            prediction_time = min(
                max(0.0, now - cur[0]) + lead,
                MAX_PLAYER_PREDICTION,
            )
            predicted_pos = (
                cur_pos[0] + velocity[0] * prediction_time,
                cur_pos[1] + velocity[1] * prediction_time,
                cur_pos[2] + velocity[2] * prediction_time,
            )

            # Avoid visible vertical pumping from noisy grounded positions.
            if abs(velocity[1]) < 0.35:
                predicted_pos = (
                    predicted_pos[0],
                    cur_pos[1],
                    predicted_pos[2],
                )

            np = dict(p)
            np['pos'] = predicted_pos
            np['velocity'] = velocity
            np['prediction_ms'] = prediction_time * 1000.0
            bones = p.get('bones')
            if bones:
                prediction_delta = (
                    predicted_pos[0] - cur_pos[0],
                    predicted_pos[1] - cur_pos[1],
                    predicted_pos[2] - cur_pos[2],
                )
                # Body extrapolated, pose interpolated.
                #
                # Extrapolating the limbs makes the residual grow across every
                # rendered frame and then snap back to zero the moment a new
                # snapshot lands. At a ~60 ms worker tick against a 144 Hz
                # overlay that is a visible hitch six times a second, and no
                # amount of tuning removes it -- it is what extrapolation *is*.
                #
                # Interpolating instead means every frame shows a pose that
                # lies between two genuinely measured ones: continuous by
                # construction, never invented, and there is nothing to snap
                # back from. The cost is that the pose trails the newest
                # sample by up to one snapshot (~60 ms), which is far less
                # than the networked position already lags reality.
                #
                # The body keeps its extrapolation, so the skeleton still sits
                # on the player with no added latency. Only the *shape* is
                # interpolated: bone offsets are taken relative to each
                # snapshot's own position before blending, which is what keeps
                # the two decisions independent.
                prev_bones = prev_p.get('bones') or {}
                prev_pos = prev_p.get('pos')
                # The blend is decided once for the whole skeleton, never
                # per bone. It used to be per bone, and [BONE-PHASE-MIX]
                # caught what that costs: 1-2% of skeleton-frames came out
                # mixed, worst case *one* bone interpolated against twenty
                # held raw. A blended bone sits at prev + (cur-prev)*alpha
                # while a raw one sits at cur, so a mixed skeleton is a pose
                # the player never actually held -- and the bone that
                # disagreed is the one seen flying out of the body during a
                # sprint. Consistency beats smoothness here: a whole
                # skeleton arriving one snapshot early is still a real pose,
                # a detached limb never is.
                # Decide first, build second -- and store nothing in
                # between. Keeping a dict of per-bone offsets across the two
                # passes cost 5x: [SNAP-PROFILE] went from 0.3 ms to 1.5 ms
                # a frame, 19-22% of the render thread at 22 skeletons, for
                # ~460 tuples allocated and thrown away every frame. The
                # first pass now only answers one yes/no question and bails
                # at the first bone that says no.
                dissent = 0
                total_bones = len(bones)
                if prev_pos is None or not prev_bones:
                    dissent = total_bones
                else:
                    for bone_id, bone_pos in bones.items():
                        was = prev_bones.get(bone_id)
                        if was is None:
                            dissent = total_bones
                            break
                        dx = (bone_pos[0] - cur_pos[0]) - (was[0] - prev_pos[0])
                        dy = (bone_pos[1] - cur_pos[1]) - (was[1] - prev_pos[1])
                        dz = (bone_pos[2] - cur_pos[2]) - (was[2] - prev_pos[2])
                        # Guard against a rig swap between snapshots: a pose
                        # that jumped this far is a different skeleton, not
                        # a movement, and blending towards it would smear.
                        if dx * dx + dy * dy + dz * dz > BONE_POSE_BLEND_MAX_SQ:
                            dissent = total_bones
                            break
                blend = dissent == 0
                shifted = {}
                for bone_id, bone_pos in bones.items():
                    shifted[bone_id] = (
                        predicted_pos[0] + bone_pos[0] - cur_pos[0],
                        predicted_pos[1] + bone_pos[1] - cur_pos[1],
                        predicted_pos[2] + bone_pos[2] - cur_pos[2],
                    )
                np['bones'] = shifted
                self._track_bone_phase_mix(pm, blend, dissent, total_bones)
                self._track_bone_stretch(pm, shifted, blend)
                # The body-level shift, kept so the view can undo the whole
                # thing for the A/B toggle and so `pred=` has something to
                # report.
                np['bone_shift'] = prediction_delta
            predicted_players.append(np)

        for pm in list(self._render_motion_cache):
            if pm not in active_pms:
                del self._render_motion_cache[pm]

        return predicted_players, world_ents, cur[2], self.diag, self.tick_ms

    def start_worker(self):
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _debug(self, msg, interval=DEBUG_LOG_INTERVAL, force=False):
        if not DEBUG_PLAYERS:
            return
        now = time.perf_counter()
        if force or now - self._last_debug_at >= interval:
            print(f"[DBG] {msg}", flush=True)
            self._last_debug_at = now

    def _debug_players(self, msg, interval=DEBUG_LOG_INTERVAL):
        if not DEBUG_PLAYERS:
            return
        now = time.perf_counter()
        if now - self._last_player_debug_at >= interval:
            print(f"[DBG] {msg}", flush=True)
            self._last_player_debug_at = now

    def _debug_sample(self, msg, interval=5.0):
        if not DEBUG_PLAYERS:
            return
        now = time.perf_counter()
        if now - self._last_sample_debug_at >= interval:
            print(f"[DBG] {msg}", flush=True)
            self._last_sample_debug_at = now

    def _set_diag(self, msg, interval=DEBUG_LOG_INTERVAL):
        self.diag = msg
        self._debug(msg, interval=interval)

    def _log_chain_if_changed(self, bp_klass, sf, list_dict, blist, arr, count):
        chain = (bp_klass, sf, list_dict, blist, arr)
        if chain == self._last_chain:
            return
        self._last_chain = chain
        self._debug(
            "chain "
            f"BP=0x{bp_klass:X} sf=0x{sf:X} list=0x{list_dict:X} "
            f"blist=0x{blist:X} arr=0x{arr:X} count={count}",
            force=True,
        )

    def _scan_static_fields(self, sf, force=False):
        now = time.perf_counter()
        if not force and now - self._last_static_scan_at < 5.0:
            return
        self._last_static_scan_at = now

        def read_u64(buf, off):
            return struct.unpack_from('<Q', buf, off)[0] if off + 8 <= len(buf) else 0

        def read_i32(buf, off):
            return struct.unpack_from('<i', buf, off)[0] if off + 4 <= len(buf) else 0

        candidates = []
        sf_buf = self.m.read_retry(sf, 0x300, attempts=2)

        for static_off in range(0, 0x300, 8):
            obj = read_u64(sf_buf, static_off)
            if not _valid_user_ptr(obj):
                continue

            obj_buf = self.m.read_retry(obj, 0x120, attempts=2)
            checks = [("direct", obj, obj_buf)]
            for inner_off in range(0, 0x100, 8):
                inner = read_u64(obj_buf, inner_off)
                if _valid_user_ptr(inner):
                    checks.append((f"+0x{inner_off:X}", inner, self.m.read_retry(inner, 0x60, attempts=2)))

            for label, cand, cand_buf in checks[:40]:
                for arr_off, count_off in ((0x10, 0x18), (0x18, 0x20), (0x20, 0x18), (0x28, 0x30), (0x30, 0x38)):
                    arr = read_u64(cand_buf, arr_off)
                    count = read_i32(cand_buf, count_off)
                    if not (_valid_user_ptr(arr) and 0 < count <= 1000):
                        continue

                    sample_count = min(count, 8)
                    elems_raw = self.m.read_retry(arr + 0x20, sample_count * 8, attempts=2)
                    valid_elems = 0
                    for i in range(sample_count):
                        if _valid_user_ptr(read_u64(elems_raw, i * 8)):
                            valid_elems += 1
                    if valid_elems:
                        candidates.append((static_off, label, cand, arr_off, count_off, arr, count, valid_elems))

        if not candidates:
            self._debug(f"static scan sf=0x{sf:X}: no array/count candidates", force=True)
            return []

        parts = [
            f"sf+0x{so:X} {label} cand=0x{cand:X} "
            f"arr_off=0x{ao:X} cnt_off=0x{co:X} arr=0x{arr:X} count={cnt} valid={valid}"
            for so, label, cand, ao, co, arr, cnt, valid in candidates[:8]
        ]
        suffix = "" if len(candidates) <= 8 else f" (+{len(candidates) - 8} more)"
        self._debug(f"static scan sf=0x{sf:X}: " + " | ".join(parts) + suffix, force=True)
        return candidates

    def _best_static_list_candidate(self, sf):
        if self._static_candidate and self._static_candidate[0] == sf:
            return self._static_candidate[1]

        candidates = self._scan_static_fields(sf)
        if not candidates:
            return None

        candidate = max(candidates, key=lambda c: (c[7], c[6]))
        self._static_candidate = (sf, candidate)
        return candidate

    def _loop(self):
        next_tick = time.perf_counter()
        while True:
            t0 = time.perf_counter()
            # One heavy scan per tick. World entities (2 s), player names (1 s)
            # and held items (0.5 s) run on independent timers, so they
            # periodically land on the same tick — that is where the 700 ms
            # `disp=` figures in [TICK-LATENCY] came from, ~47 IOCTLs stacked
            # into one tick. At a fixed per-IOCTL latency the total work is the
            # same either way, but spread over three ticks it never exceeds the
            # render thread's extrapolation window, so it stops being a freeze.
            self._heavy_scan_budget = 1
            try:
                self._tick()
            except Exception as e:
                self._set_diag(f"ERR {type(e).__name__}: {e}")
            dt = (time.perf_counter() - t0) * 1000
            self.tick_ms = dt
            next_tick += WORKER_DT
            now = time.perf_counter()
            if next_tick < now - WORKER_DT:
                next_tick = now
            sleep_for = next_tick - now
            if sleep_for > 0:
                time.sleep(sleep_for)

    def _try_accept_entity_buffer(self, arr, count):
        """Port of Cl1kExternal's tryAccept lambda (sdk/classes.h).

        A candidate (array, count) pair is the real BaseNetworkable entity
        buffer only if it survives all of:
          * count in a sane range (the reference uses >10 and <100000),
          * the Il2CppArray's own max_length is at least that count,
          * at least 2 of the first 5 elements resolve to a klass with a
            readable class-name string.

        That last check is the whole point of the design: rather than trusting
        a hardcoded field offset that drifts every Rust build, the reference
        *searches* for the buffer and proves it found the right one by reading
        real type names out of it. We previously hardcoded list_dict+0x10 and
        annotated it "empirically confirmed" -- exactly the guess this replaces.
        """
        if not _valid_user_ptr(arr):
            return False
        if count <= 10 or count >= 100000:
            return False
        array_length = self.m.i32_retry(arr + OFF.buffer_count, attempts=2)
        if array_length < count:
            return False
        probe = min(5, count)
        ptrs = self.m.batch_u64(
            [arr + OFF.array_payload + i * 8 for i in range(probe)],
            attempts=1,
        )
        live = [p for p in ptrs if _valid_user_ptr(p)]
        if not live:
            return False
        klasses = self.m.batch_u64(live, attempts=1)
        names = self._read_klass_names(klasses)
        named = sum(1 for k in klasses if names.get(k))
        return named >= 2

    def _probe_entity_container(self, container, low, high):
        """Scan container[low..high] for a BufferList holding the entity array.

        Mirrors the reference's erOff/cOff loops: every 8-byte slot in the
        window is treated as a candidate BufferList, whose array lives at
        +0x10 and whose live count lives at +0x18. The first candidate that
        passes _try_accept_entity_buffer wins.

        This is the indirection the old code was missing entirely. It read
        `arr = list_dict + 0x10` directly, but dump.txt's chain-walker notes
        spell the real shape out: ListDictionary.vals/buffer_list sits at
        ENTITIES(0x20), and only *then* does BUFFER_LIST_ARRAY(0x10) /
        BUFFER_LIST_COUNT(0x18) apply. Scanning the window finds it whether
        it is at 0x20 on this build or somewhere else on the next.
        """
        if not _valid_user_ptr(container):
            return None
        offsets = list(range(low, high + 1, 8))
        objs = self.m.batch_u64(
            [container + off for off in offsets], attempts=1
        )
        candidates = [o for o in objs if _valid_user_ptr(o)]
        if not candidates:
            return None
        arrays = self.m.batch_u64(
            [o + OFF.buffer_array for o in candidates], attempts=1
        )
        counts = self.m.batch_u64(
            [o + OFF.ListComponent_size for o in candidates], attempts=1
        )
        for obj, arr, raw_count in zip(candidates, arrays, counts):
            count = raw_count & 0xFFFFFFFF
            if count & 0x80000000:
                continue
            if self._try_accept_entity_buffer(arr, count):
                return obj, arr
        return None

    def _resolve_entity_chain(self):
        """Walk the full HiddenValue chain and cache list_dict/arr. Returns (list_dict, arr) or None."""
        m, ga = self.m, self.ga
        bn_klass = m.u64_retry(ga + OFF.BaseNetworkable_c)
        if not bn_klass:
            self._set_diag(f"no BN klass 0x{ga + OFF.BaseNetworkable_c:X}")
            return None
        sf = m.u64_retry(bn_klass + OFF.klass_static_fields)
        if not sf:
            self._set_diag(f"no BN sf 0x{bn_klass:X}")
            return None
        # Two static HiddenValue<ClientRealm> slots (+0x0 / +0x8), only one of
        # which is clientEntities -- see OFF.wrapper_in_static. Probe both, then
        # remember the winner so steady-state costs exactly one read.
        latched = getattr(self, '_bn_static_slot', None)
        slots = [OFF.wrapper_in_static, OFF.wrapper_in_static_alt]
        if latched is not None:
            slots = [latched] + [sl for sl in slots if sl != latched]

        # Pass 1 refuses the unvalidated `list_dict + 0x10` last resort: that
        # fallback accepts any non-null pointer, so a wrong static slot would
        # "succeed" with garbage and latch itself in. Only if BOTH slots fail
        # validation does pass 2 re-enable it, keeping the old behaviour as the
        # floor rather than the discriminator.
        # (slot, retries, allow_unvalidated). The latched slot keeps the original
        # 8-retry budget -- those retries exist to ride out a tick where the
        # handle is not populated yet, and shortening them would reintroduce the
        # flapping they were added to fix. A speculative slot gets 2, so probing
        # costs a few milliseconds rather than a second.
        plan = [(slots[0], 8, False)]
        plan += [(sl, 2, False) for sl in slots[1:]]
        plan += [(sl, 2, True) for sl in slots]

        tried = []
        for slot, retries, allow_unvalidated in plan:
            wrapper = m.u64_retry(sf + slot)
            if not _valid_user_ptr(wrapper):
                tried.append(f"+0x{slot:X}=bad")
                continue
            found = self._walk_entity_realm(wrapper, retries, allow_unvalidated)
            if found is None:
                tried.append(f"+0x{slot:X}=dead")
                continue
            if latched != slot:
                print(
                    f"[ENT-DBG] BaseNetworkable static slot latched to "
                    f"+0x{slot:X} (wrapper=0x{wrapper:X}"
                    f"{', unvalidated' if allow_unvalidated else ''})",
                    flush=True,
                )
            self._bn_static_slot = slot
            container, arr = found
            self._cached_list_dict = container
            self._cached_arr = arr
            # _entity_buffer() reads this back; before it did, these two fields
            # were written and never used, so every caller paid the full walk.
            self._entity_chain_at = time.perf_counter()
            return container, arr
        # A slot that used to work and stopped is worth re-probing from scratch.
        self._bn_static_slot = None
        self._set_diag(f"no BN chain sf=0x{sf:X} [{' '.join(tried)}]")
        return None

    def _walk_entity_realm(self, wrapper, retries=8, allow_unvalidated=True):
        """HiddenValue<ClientRealm> wrapper -> (entity container, Il2CppArray).

        Returns None if this wrapper is not the live client realm. Split out of
        _resolve_entity_chain so the two candidate static slots can be probed
        with identical logic.
        """
        m, ga = self.m, self.ga
        for attempt in range(retries):
            # _read_hv_handle gates on HiddenValue<T>._hasValue (+0x14) before
            # trusting the encrypted handle at +0x18 — see dump.txt's chain-
            # walker note. Reading the handle unconditionally treated "not
            # populated yet this tick" the same as "the walk is broken" and
            # burned retries on it instead of just waiting one more pass.
            hv1 = _read_hv_handle(m, wrapper, attempts=3)
            if not hv1:
                time.sleep(0.005)
                continue
            dec1 = decrypt_bn0(hv1)
            entity_realm = resolve_tagged_handle(m, dec1, ga)
            if not entity_realm:
                self._set_diag(f"no entityRealm hv1=0x{hv1:X} dec1=0x{dec1:X}")
                time.sleep(0.005)
                continue
            parent = m.u64_retry(entity_realm + OFF.parent_in_realm, attempts=3)
            if not parent:
                self._set_diag(f"no BN parent realm=0x{entity_realm:X}")
                time.sleep(0.005)
                continue
            hv2 = _read_hv_handle(m, parent, attempts=3)
            if not hv2:
                time.sleep(0.005)
                continue
            dec2 = decrypt_bn1(hv2)
            list_dict = resolve_tagged_handle(m, dec2, ga)
            if not list_dict:
                self._set_diag(f"no listDict hv2=0x{hv2:X} dec2=0x{dec2:X}")
                time.sleep(0.005)
                continue
            # Locate the entity buffer the way the reference does: scan the
            # container's slots and validate candidates by reading real class
            # names out of them (_try_accept_entity_buffer), instead of
            # trusting one hardcoded offset. Pass order mirrors
            # objects_basenetworkable in Cl1kExternal sdk/classes.h.
            found = self._probe_entity_container(
                list_dict,
                OFF.entity_container_scan_low,
                OFF.entity_container_scan_high,
            )
            # Reference pass 1: the realm itself can hold the BufferList.
            if found is None:
                found = self._probe_entity_container(
                    entity_realm,
                    OFF.entity_realm_scan_low,
                    OFF.entity_realm_scan_high,
                )
            # Last resort: the pre-scan behaviour (list_dict + 0x10 treated as
            # the array outright), kept so a build where the scan comes up
            # empty is no worse off than before.
            if found is None and allow_unvalidated:
                legacy_arr = m.u64_retry(
                    list_dict + OFF.buffer_array, attempts=3
                )
                if _valid_user_ptr(legacy_arr):
                    found = (list_dict, legacy_arr)
            if found is None:
                self._set_diag(f"no array dict=0x{list_dict:X}")
                time.sleep(0.005)
                continue
            # The caller owns _cached_list_dict/_cached_arr/_entity_chain_at:
            # writing them here too would publish a candidate slot's result
            # before it has been accepted.
            return found
        return None

    def _get_bp_context(self):
        """Walk BasePlayer_Class → static_fields (bp_sf), then optionally LocalPlayer HV.
        Returns (local_player_ptr_or_None, bp_sf_or_None).
        bp_sf is always attempted first — VPL does not require local_player."""
        m, ga = self.m, self.ga
        # bp_sf is independent: BasePlayer_c.static_fields
        bp_klass = m.u64_retry(ga + OFF.BasePlayer_c)
        if not _valid_user_ptr(bp_klass):
            return None, None
        bp_sf = m.u64_retry(bp_klass + OFF.klass_static_fields)
        if not _valid_user_ptr(bp_sf):
            return None, None
        # Try to resolve local player for filtering (optional — failures return (None, bp_sf))
        lp_klass = m.u64_retry(ga + OFF.LocalPlayer_c)
        if not _valid_user_ptr(lp_klass):
            return None, bp_sf
        lp_sf = m.u64_retry(lp_klass + OFF.klass_static_fields)
        if not _valid_user_ptr(lp_sf):
            return None, bp_sf
        # Current dump: LocalPlayer static entity is sf+0x8 (HiddenValue<BasePlayer>).
        hv_wrapper = m.u64_retry(lp_sf + OFF.LocalPlayer_Entity)
        if not _valid_user_ptr(hv_wrapper):
            return None, bp_sf
        hv_raw = _read_hv_handle(m, hv_wrapper, attempts=2)
        if not hv_raw:
            return None, bp_sf
        hv_dec = decrypt_local_player(hv_raw)
        local_player = resolve_tagged_handle(m, hv_dec, ga)
        if not _valid_user_ptr(local_player):
            return None, bp_sf
        return local_player, bp_sf

    def _read_il2cpp_array_ptrs(self, arr, count, max_count=4096, payload=0x20):
        """Read `count` object pointers out of an Il2CppArray.

        `payload` is where element 0 starts; 0x20 (klass/monitor/bounds/
        max_length header) on every buffer measured so far on this build,
        including the BaseNetworkable entity buffer — despite Cl1kExternal
        naming a distinct 0x28 for its BufferList variant, using 0x28 here
        measurably regressed the valid-pointer yield (2515/3472 -> 161/3625)
        rather than fixing anything. The PlayerModel list additionally has its
        own hard LOCK to 0x20: flipping it to 0x28 makes it oscillate. Only
        override this if you have paired before/after yield numbers proving
        the new value is actually better, not just a name match to a reference
        that was reverse-engineered against a different build.
        """
        if not _valid_user_ptr(arr) or count <= 0 or count > max_count:
            return [], payload
        # The addresses are contiguous by construction (arr + payload + i * 8),
        # so read the payload in one shot instead of handing batch_u64 a list of
        # `count` addresses it has to treat as scattered. For the BaseNetworkable
        # buffer (16384 elements) the batch path split the request into two
        # CMD_BATCH_READ_U64 IOCTLs, made the driver translate 16384 addresses
        # one at a time, and ran two 16k-iteration Python loops (the page-sort
        # and the per-element XOR decrypt). A plain CMD_READ of count * 8 bytes
        # is a single IOCTL while it fits in COMM_DATA_SIZE (0x40000 = 32768
        # pointers), and both the decrypt and the unpack then happen in C.
        data = self.m.read(arr + payload, count * 8)
        if data and len(data) == count * 8:
            vals = struct.unpack('<%dQ' % count, data)
            ptrs = [p for p in vals if _valid_user_ptr(p)]
            if ptrs:
                return ptrs, payload
        # Contiguous read came back empty/short (page not resident, driver miss).
        # Fall back to the scattered path, which retries per address.
        vals = self.m.read_ptr_array([arr + payload + i * 8 for i in range(count)])
        ptrs = [p for p in vals if _valid_user_ptr(p)]
        return ptrs, payload

    def _read_player_model_list(self, buf_arr, lc_count):
        # martin dumper: first_element = 0x20 (Il2CppArray header is 0x20 bytes)
        # LOCKED to 0x20 — do NOT flip to 0x28 which causes flickering oscillation
        payload = 0x20
        vals = self.m.batch_u64_priority([buf_arr + payload + i * 8 for i in range(lc_count)], attempts=1)
        pm_ptrs = [p for p in vals if _valid_user_ptr(p)]
        if pm_ptrs:
            # Only update cache if we got MORE pointers than last time (avoid shrink-flicker)
            if len(pm_ptrs) >= len(self._pm_ptr_cache) - 1:
                self._pm_ptr_cache = pm_ptrs
            else:
                # Merge: keep old pointers that are still valid, add new ones
                old_set = set(self._pm_ptr_cache)
                new_set = set(pm_ptrs)
                merged = [p for p in self._pm_ptr_cache if p in new_set] + [p for p in pm_ptrs if p not in old_set]
                self._pm_ptr_cache = merged if merged else pm_ptrs
            self._pm_payload = payload
            self._pm_list_miss = 0
            self._pm_ptr_cache_updated_at = time.perf_counter()
            return list(self._pm_ptr_cache), payload, False

        self._pm_list_miss += 1
        cache_age = time.perf_counter() - self._pm_ptr_cache_updated_at
        if self._pm_ptr_cache and cache_age <= self._lc_cache_ttl:
            return list(self._pm_ptr_cache), self._pm_payload, True
        return [], payload, False

    def _filter_local_pm(self, pm_ptrs):
        if not pm_ptrs:
            return None, []
        flags = self.m.batch_u64([p + OFF.pm_is_local_player for p in pm_ptrs], attempts=1)
        local_pm = None
        enemies = []
        for pm, flag_raw in zip(pm_ptrs, flags):
            if (flag_raw & 0xFF) != 0:
                local_pm = pm
            else:
                enemies.append(pm)

        # Sticky local: if flag read failed but cached local is still in the list, trust cache
        if local_pm:
            self._last_local_pm = local_pm
        elif self._last_local_pm and self._last_local_pm in pm_ptrs:
            local_pm = self._last_local_pm
            enemies = [pm for pm in pm_ptrs if pm != local_pm]

        return local_pm, enemies

    def _read_pm_server_pos(self, pm):
        block = self.m.read(pm + OFF.position_pm, 12)
        if len(block) == 12:
            p = struct.unpack('<fff', block)
            if (not (p[0] == 0.0 and p[1] == 0.0 and p[2] == 0.0)
                    and abs(p[0]) < 6000 and abs(p[2]) < 6000
                    and -200 < p[1] < 2000):
                return p, OFF.position_pm
        return None, 0

    def _read_pm_server_positions_batch(self, pm_ptrs):
        offsets = (OFF.position_pm,)
        query = []
        for pm in pm_ptrs:
            for off in offsets:
                query.extend((pm + off, pm + off + 8))
            query.extend((pm + OFF.velocity_pm_a, pm + OFF.velocity_pm_a + 8))

        vals = self.m.batch_u64(query, attempts=1)
        out = {}
        idx = 0
        for pm in pm_ptrs:
            candidates = []
            for off in offsets:
                if idx + 3 >= len(vals):
                    break
                lo = vals[idx]
                hi = vals[idx + 1]
                v_lo = vals[idx + 2]
                v_hi = vals[idx + 3]
                idx += 4

                x, y = struct.unpack('<ff', struct.pack('<Q', lo))
                z = struct.unpack('<f', struct.pack('<I', hi & 0xFFFFFFFF))[0]
                p = (x, y, z)

                vx, vy = struct.unpack('<ff', struct.pack('<Q', v_lo))
                vz = struct.unpack('<f', struct.pack('<I', v_hi & 0xFFFFFFFF))[0]
                if abs(vx) < 50.0 and abs(vy) < 50.0 and abs(vz) < 50.0:
                    self._pm_vel_cache[pm] = (vx, vy, vz)

                # A player who fails every clause here is dropped from the
                # ESP ENTIRELY further down (skipped_pos -> continue), not
                # merely drawn without a position -- which is the "some
                # players are invisible" report. So record which clause
                # rejected them instead of leaving it a silent drop.
                if p[0] == 0.0 and p[1] == 0.0 and p[2] == 0.0:
                    why = "read returned zeros"
                elif not (abs(p[0]) < 6000 and abs(p[2]) < 6000):
                    why = f"x/z out of map bounds ({p[0]:.0f},{p[2]:.0f})"
                elif not (-200 < p[1] < 2000):
                    why = f"y out of range ({p[1]:.0f})"
                elif abs(p[0]) < 2.0 and abs(p[2]) < 2.0 and abs(p[1]) < 2.0:
                    # NB the map is centred on the origin, so this 2 m cube is
                    # a real place to stand -- kept as-is for now, but it is a
                    # genuine false-positive source worth knowing about.
                    why = f"inside the 2m origin guard ({p[0]:.1f},{p[1]:.1f},{p[2]:.1f})"
                else:
                    why = None
                why_map = getattr(self, '_pos_reject_why', None)
                if why_map is None:
                    why_map = self._pos_reject_why = {}
                if why is None:
                    candidates.append((off, p))
                    why_map.pop(pm, None)
                else:
                    why_map[pm] = why
            selected = self._select_pm_position(pm, candidates)
            if selected is not None:
                out[pm] = selected
        return out

    @staticmethod
    def _dist3(a, b):
        dx = a[0] - b[0]
        dy = a[1] - b[1]
        dz = a[2] - b[2]
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    def _select_pm_position(self, pm, candidates):
        if not candidates:
            return None

        prev = self._pm_raw_pos_cache.get(pm)
        stable_off = self._pm_pos_offset_cache.get(pm)
        selected_off = None
        selected = None

        if stable_off is not None:
            for off, pos in candidates:
                if off == stable_off and (prev is None or self._dist3(prev, pos) < 120.0):
                    selected_off = off
                    selected = pos
                    break

        if selected is None and prev is not None:
            selected_off, selected = min(candidates, key=lambda item: self._dist3(prev, item[1]))
            if self._dist3(prev, selected) > 180.0:
                selected = None

        if selected is None:
            selected_off, selected = candidates[0]

        selected = self._gate_position_jump(pm, selected, prev)
        if selected is None:
            return None

        self._pm_pos_offset_cache[pm] = selected_off
        self._pm_raw_pos_cache[pm] = selected
        return selected

    def _gate_position_jump(self, pm, pos, prev):
        """Reject a single physically impossible position jump (see POS_JUMP_*).

        Returns the position to use: ``pos`` when it is believable, the previous
        one while a suspect jump is still unconfirmed, or ``None`` if there is
        nothing trustworthy to show yet.
        """
        now = time.perf_counter()
        bp = self._pm_bp_cache.get(pm, 0)
        prev_bp = self._pm_pos_bp_cache.get(pm)
        self._pm_pos_bp_cache[pm] = bp

        # A PlayerModel slot reused by a different BasePlayer is a *new* player,
        # not a teleport: reset the history instead of holding the old body's
        # position on top of them. Same identity check that fixed stale names.
        if prev is not None and prev_bp is not None and prev_bp != bp:
            self._pm_jump_state.pop(pm, None)
            self._pm_pos_time_cache[pm] = now
            self._pos_jump_stats['reset'] += 1
            return pos

        last_at = self._pm_pos_time_cache.get(pm)
        if prev is None or last_at is None:
            self._pm_jump_state.pop(pm, None)
            self._pm_pos_time_cache[pm] = now
            return pos

        distance = self._dist3(prev, pos)
        dt = max(now - last_at, 1e-3)
        if distance < POS_JUMP_MIN_DIST or (distance / dt) <= POS_JUMP_MAX_SPEED:
            self._pm_jump_state.pop(pm, None)
            self._pm_pos_time_cache[pm] = now
            return pos

        # Suspect. Believe it only once it repeats near the same spot.
        # 'since' timestamps when the *hold* began, not when the current
        # candidate did: a position that never settles would otherwise keep
        # resetting it and stay frozen forever, and a ghost stuck on a stale
        # spot is no better than one on the wrong side of the map.
        state = self._pm_jump_state.get(pm)
        if state is None:
            state = {'cand': pos, 'count': 1, 'since': now}
        elif self._dist3(state['cand'], pos) > POS_JUMP_AGREE_DIST:
            state = {'cand': pos, 'count': 1, 'since': state['since']}
        else:
            state['cand'] = pos
            state['count'] += 1
        self._pm_jump_state[pm] = state

        if (
            state['count'] >= POS_JUMP_CONFIRM
            or (now - state['since']) >= POS_JUMP_HOLD_MAX
        ):
            # How long the player was actually frozen, and why the freeze
            # ended. The instantaneous confirm=N/3 could not tell a 3-tick
            # hold (~25 ms, invisible) from one that ran the full
            # POS_JUMP_HOLD_MAX (1.5 s, very visible: the body and its
            # skeleton stop dead, then snap forward) -- and the log throttle
            # is global, so consecutive lines are usually different players
            # each at their first sample.
            held_ms = (now - state['since']) * 1000.0
            by_timeout = state['count'] < POS_JUMP_CONFIRM
            self._pm_jump_state.pop(pm, None)
            self._pm_pos_time_cache[pm] = now
            self._pos_jump_stats['accepted'] += 1
            if by_timeout:
                self._pos_jump_stats['expired'] += 1
            if held_ms > self._pos_jump_stats['worst_hold_ms']:
                self._pos_jump_stats['worst_hold_ms'] = held_ms
            # An accepted jump is the one that actually reaches the screen, so
            # it matters more than a held one. Same throttle, same budget.
            if now >= self._next_pos_jump_log_at:
                self._next_pos_jump_log_at = now + 2.0
                stats = self._pos_jump_stats
                print(
                    f"[POS-JUMP] ACCEPTED pm=0x{pm:X} "
                    f"bp=0x{self._pm_bp_cache.get(pm, 0):X} {distance:.0f}m "
                    f"from=({prev[0]:.1f},{prev[1]:.1f},{prev[2]:.1f}) "
                    f"to=({pos[0]:.1f},{pos[1]:.1f},{pos[2]:.1f}) "
                    f"after {state['count']} samples / {held_ms:.0f}ms "
                    f"({'TIMEOUT' if by_timeout else 'confirmed'}) | totals "
                    f"held={stats['held']} accepted={stats['accepted']} "
                    f"expired={stats['expired']} reset={stats['reset']} "
                    f"worst_hold={stats['worst_hold_ms']:.0f}ms",
                    flush=True,
                )
            return pos

        self._pos_jump_stats['held'] += 1
        if now >= self._next_pos_jump_log_at:
            self._next_pos_jump_log_at = now + 2.0
            stats = self._pos_jump_stats
            # from -> to and the bp, because "held 30m in 61ms" alone cannot
            # say *which* of the two readings is the lie. The three cases look
            # identical in the old line and need opposite fixes:
            #   * bp changed        -> the pm address was recycled; identity
            #                          bug, and `reset` would be climbing too.
            #   * `to` is far from  -> a bad read landing inside the sanity
            #     everyone else        range; the position source is at fault.
            #   * `to` is plausible -> the player really did move and the gate
            #     local terrain        is what is wrong.
            # Live 2026-08-25: held=82 accepted=28 with reset stuck at 12, so
            # bp is stable and this is not address reuse.
            print(
                f"[POS-JUMP] pm=0x{pm:X} bp=0x{self._pm_bp_cache.get(pm, 0):X} "
                f"held {distance:.0f}m in {dt * 1000:.0f}ms "
                f"({distance / dt:.0f}m/s) "
                f"from=({prev[0]:.1f},{prev[1]:.1f},{prev[2]:.1f}) "
                f"to=({pos[0]:.1f},{pos[1]:.1f},{pos[2]:.1f}) "
                f"confirm={state['count']}/{POS_JUMP_CONFIRM} "
                f"for={(now - state['since']) * 1000.0:.0f}ms | totals "
                f"held={stats['held']} accepted={stats['accepted']} "
                f"expired={stats['expired']} reset={stats['reset']} "
                f"worst_hold={stats['worst_hold_ms']:.0f}ms",
                flush=True,
            )
        # Hold the last believable position. Do not refresh _pm_pos_time_cache:
        # dt keeps growing, so a genuine slow drift eventually falls back under
        # POS_JUMP_MAX_SPEED and is accepted on its own.
        return prev

    def _smooth_pm_position(self, pm, raw_pos):
        return raw_pos

    def _read_local_eye_offset(self, local_bp):
        """Read PlayerEyes.viewOffset (Vector3) for the local player.

        Chain : BasePlayer + OFF.eyes -> HV<PlayerEyes>._handle
             -> decrypt_player_eyes -> resolve_tagged_handle
             -> PlayerEyes native, +0x40 = viewOffset (Vector3, 12 bytes)

        The PlayerEyes pointer only invalidates on respawn / entity swap,
        so we cache it per local_bp for 5 s and only pay the resolve cost
        once per lifecycle. The viewOffset itself changes with crouch /
        prone / dismount, so we always re-read the 12 bytes.

        Returns (dx, dy, dz) or None if any step fails.
        """
        if not local_bp:
            return None
        m = self.m
        cache = self._local_eyes_cache
        now = time.perf_counter()
        player_eyes = 0
        if (cache is not None
            and cache[0] == local_bp
            and now - cache[1] < 5.0):
            player_eyes = cache[2]
        else:
            wrapper = m.u64_retry(local_bp + OFF.eyes, attempts=2)
            if not _valid_user_ptr(wrapper):
                return None
            hv_raw = _read_hv_handle(m, wrapper, attempts=2)
            if not hv_raw:
                return None
            hv_dec = decrypt_player_eyes(hv_raw)
            player_eyes = resolve_tagged_handle(m, hv_dec, self.ga)
            if not _valid_user_ptr(player_eyes):
                return None
            self._local_eyes_cache = (local_bp, now, player_eyes)

        # Expose the resolved pointers so the aim/probe layer can write to
        # angle fields under PlayerEyes without duplicating this HV walk.
        self.local_bp = local_bp
        self.local_player_eyes = player_eyes

        raw = m.read(player_eyes + OFF.viewOffset, 12)
        if not raw or len(raw) != 12:
            return None
        try:
            dx, dy, dz = struct.unpack('<3f', raw)
        except Exception:
            return None
        if not (math.isfinite(dx) and math.isfinite(dy) and math.isfinite(dz)):
            return None
        # Sanity : Rust viewOffset stays within a metre of (0, 1.6, 0) even
        # when prone (~0.3 Y) or in odd mounts. Reject wild values that
        # indicate we mis-resolved the chain -- keeps a bad read from
        # sending the aim into the ground.
        if not (-2.0 < dx < 2.0 and -1.0 < dy < 3.0 and -2.0 < dz < 2.0):
            return None
        return (dx, dy, dz)

    def _format_disp_io(self):
        """Attribute the last disp= spike to a scan, in calls.

        `disp=148ms` alone cannot say whether that was held items, names or
        world entities, and at a fixed ~14 ms per call the call count is the
        only actionable number.
        """
        held, names, we = getattr(self, '_last_disp_io', (0, 0, 0))
        slow = getattr(self, '_slow_lane_last', (0, 0.0))
        parts = []
        if slow[0]:
            # Off-tick, so it costs the tick nothing -- but it still costs the
            # driver, and hiding that would make the next profile a lie.
            parts.append(f"slow={slow[0]}io/{slow[1]:.0f}ms")
        if not (held or names or we or slow[0]):
            return ""
        if held:
            parts.append(f"held={held}io")
        if names:
            parts.append(f"names={names}io")
        if we:
            parts.append(f"we={we}io")
        return " [" + " ".join(parts) + "]"

    def _take_heavy_slot(self):
        """Claim this tick's single heavy-scan slot; False means try next tick.

        Callers must check this *before* pushing their own `_next_*_scan_at`
        forward, so a deferred scan retries immediately rather than waiting out
        another full interval.
        """
        budget = getattr(self, '_heavy_scan_budget', 1)
        if budget <= 0:
            return False
        self._heavy_scan_budget = budget - 1
        return True

    # ------------------------------------------------------------------
    # Engine velocity probe
    # ------------------------------------------------------------------
    VEL_PROBE_SAMPLES = 40      # moving samples to collect before latching
    VEL_PROBE_MAX_ERROR = 2.5   # m/s — mean error a winner must stay under

    def _velocity_offsets(self):
        """Which PlayerModel velocity slots the frame batch should read.

        Both while probing; only the winner once latched, so the steady-state
        cost is two extra u64 per player inside an already-batched IOCTL.
        """
        choice = self._vel_choice
        if choice is None:
            return (
                ("vel_a", OFF.velocity_pm_a),
                ("vel_b", OFF.velocity_pm_b),
            )
        if choice == 'a':
            return (("vel_a", OFF.velocity_pm_a),)
        if choice == 'b':
            return (("vel_b", OFF.velocity_pm_b),)
        return ()

    def _track_velocity(self, pm, pos, samples, now):
        """Score the candidate slots against real motion, return the winner's value.

        `samples` maps 'a'/'b' to the decoded Vec3 (or None) for this player this
        tick. Scoring only counts ticks where the player demonstrably moved: a
        standing player makes every candidate look perfect, including a slot that
        is not a velocity at all.
        """
        prev = self._vel_probe_prev.get(pm)
        # Only remember a sample when the position actually *changed*. The
        # position is networked at ~10-20 Hz while this loop ticks far faster,
        # so most ticks re-read the same value and then one tick shows the whole
        # interval's movement. Differencing across a tick period instead of the
        # interval between two distinct positions inflates the measured speed
        # several-fold — that bad ruler is why the first probe scored both
        # candidates at 3-4 m/s error and latched neither.
        if prev is None or self._dist3(prev[0], pos) > 1e-4:
            self._vel_probe_prev[pm] = (pos, now)

        if self._vel_choice is None and prev is not None:
            prev_pos, prev_t = prev
            dt = now - prev_t
            if self._dist3(prev_pos, pos) <= 1e-4:
                return None
            if 0.005 <= dt <= 0.5:
                measured = (
                    (pos[0] - prev_pos[0]) / dt,
                    (pos[1] - prev_pos[1]) / dt,
                    (pos[2] - prev_pos[2]) / dt,
                )
                speed = math.sqrt(sum(c * c for c in measured))
                if 1.5 <= speed <= 20.0:
                    probe = self._vel_probe
                    probe['n'] += 1
                    for key in ('a', 'b'):
                        value = samples.get(key)
                        # An unreadable/implausible slot is worse than a bad
                        # one: charge it more than the accept threshold.
                        probe[key] += (
                            math.dist(value, measured)
                            if value is not None
                            else self.VEL_PROBE_MAX_ERROR * 4.0
                        )
                    if probe['n'] >= self.VEL_PROBE_SAMPLES:
                        self._latch_velocity_choice()

        if self._vel_choice in ('a', 'b'):
            return samples.get(self._vel_choice)
        return None

    def _latch_velocity_choice(self):
        probe = self._vel_probe
        count = max(probe['n'], 1)
        err_a = probe['a'] / count
        err_b = probe['b'] / count
        best, err = ('a', err_a) if err_a <= err_b else ('b', err_b)
        self._vel_choice = best if err <= self.VEL_PROBE_MAX_ERROR else ''
        print(
            f"[VEL-PROBE] samples={probe['n']} mean|err| "
            f"a(0x{OFF.velocity_pm_a:X})={err_a:.2f}m/s "
            f"b(0x{OFF.velocity_pm_b:X})={err_b:.2f}m/s -> "
            f"{self._vel_choice or 'none, differencing only'}",
            flush=True,
        )

    def _read_transform_positions_batch(self, pm_to_bp):
        """DEPRECATED — do not use as a player world-position source.

        This resolves bp → model → rootBone → Transform → native →
        TransformAccess(td, idx) and then reads a Vec3 straight out of
        ``td[0x18] + 0x30 * idx``. That address is *not* a world position: it is
        the entry in Unity's TransformHierarchy local-TRS array, i.e. the root
        bone's translation **relative to its parent**. Getting a world position
        out of it requires walking ``td[0x20]`` (the parent-index array) upward
        and composing scale/rotation/translation at every ancestor, exactly like
        Cl1kExternal's ReadBonePosition does (and like
        model._compose_position_from_buffers does for bones).

        Reading the raw slot instead produced two visible bugs:
          * many distinct players resolving to the *same* coordinates, because
            un-composed local translations alias heavily;
          * `transform hits=0 of N` once the sanity filter below started
            rejecting those bogus vectors outright.

        The fix is not to compose here but to drop the path: Cl1kExternal's
        BasePlayer::GetPosition is just ``PlayerModel + 0x2F8``, which the frame
        batch already reads and which reports ~13/14 hits in the field. The
        transform hierarchy is still used — but only for bones, where the full
        parent-chain composition is actually performed.

        Kept for reference/diagnostics only; nothing in the tick calls it.
        """
        if not pm_to_bp:
            return {}

        now = time.perf_counter()
        active_pms = set(pm_to_bp)
        cached_slots = {
            pm: slot
            for pm, slot in self._transform_slot_cache.items()
            if pm in active_pms and _valid_user_ptr(slot)
        }
        refresh_slots = (
            now - self._transform_slot_cache_at
            >= self._transform_slot_refresh_interval
            or not active_pms.issubset(cached_slots)
        )

        def read_ptr_stage(source, address_fn):
            pairs = [
                (pm, address_fn(value))
                for pm, value in source.items()
                if _valid_user_ptr(value)
            ]
            if not pairs:
                return {}
            values = self.m.batch_u64([address for _, address in pairs], attempts=1)
            return {
                pm: value
                for (pm, _), value in zip(pairs, values)
                if _valid_user_ptr(value)
            }

        if refresh_slots:
            models = read_ptr_stage(pm_to_bp, lambda bp: bp + OFF.model)
            roots = read_ptr_stage(models, lambda model: model + OFF.rootBone_Model)
            internals = read_ptr_stage(roots, lambda root: root + OFF.bone_transform)

            access_pairs = []
            for pm, internal in internals.items():
                access_pairs.append((pm, 'td', internal + OFF.transform_access))
                access_pairs.append((pm, 'idx', internal + OFF.transform_access + 8))
            access_values = self.m.batch_u64(
                [address for _, _, address in access_pairs],
                attempts=1,
            ) if access_pairs else []
            access = {}
            for (pm, kind, _), value in zip(access_pairs, access_values):
                access.setdefault(pm, {})[kind] = value

            transform_data = {
                pm: values
                for pm, values in access.items()
                if _valid_user_ptr(values.get('td', 0))
                and (values.get('idx', 0) & 0xFFFFFFFF) < 100000
            }
            pos_arrays = read_ptr_stage(
                {pm: values['td'] for pm, values in transform_data.items()},
                lambda td: td + OFF.td_pos_base,
            )

            refreshed_slots = {}
            for pm, pos_array in pos_arrays.items():
                idx = transform_data[pm]['idx'] & 0xFFFFFFFF
                refreshed_slots[pm] = pos_array + OFF.td_stride * idx
            cached_slots.update(refreshed_slots)
            self._transform_slot_cache = cached_slots
            self._transform_slot_cache_at = now

        position_pairs = list(cached_slots.items())
        if not position_pairs:
            return {}

        queries = []
        for _, slot in position_pairs:
            queries.extend((slot, slot + 8))
        values = self.m.batch_u64(queries, attempts=1)

        out = {}
        for index, (pm, _) in enumerate(position_pairs):
            value_index = index * 2
            if value_index + 1 >= len(values):
                break
            raw = struct.pack(
                '<QQ',
                values[value_index],
                values[value_index + 1],
            )
            pos = struct.unpack_from('<fff', raw)
            if (
                all(math.isfinite(component) for component in pos)
                and abs(pos[0]) < 6000
                and abs(pos[2]) < 6000
                and -200 < pos[1] < 2000
                and pos != (0.0, 0.0, 0.0)
            ):
                out[pm] = pos
        return out

    def _resolve_bone_slots_batch(self, pm_to_bp):
        """Resolve SCI Transform slots once; hot-frame reads then stay batched."""
        now = time.perf_counter()
        active_set = set(pm_to_bp)
        self._bone_slot_cache = {
            pm: slots
            for pm, slots in self._bone_slot_cache.items()
            if pm in active_set
        }
        self._bone_resolve_retry_at = {
            pm: retry_at
            for pm, retry_at in self._bone_resolve_retry_at.items()
            if pm in active_set
        }
        missing = {
            pm: bp
            for pm, bp in pm_to_bp.items()
            if _valid_user_ptr(bp)
            and len(self._bone_slot_cache.get(pm, {}).get('bones', {})) < len(SCI_BONE_IDS)
            and now >= self._bone_resolve_retry_at.get(pm, 0.0)
        }
        if not missing:
            return
        for pm in missing:
            self._bone_resolve_retry_at[pm] = now + 15.0

        model_values = self.m.batch_u64(
            [bp + OFF.model for bp in missing.values()],
            attempts=1,
        )
        models = {
            pm: model
            for pm, model in zip(missing, model_values)
            if _valid_user_ptr(model)
        }
        if not models:
            return

        # Current Model.boneTransforms is the primary path. PM+0x98 is kept as
        # a compatible direct Transform[] path because both layouts have
        # existed across recent SCI updates.
        array_descriptors = []
        array_addresses = []
        for pm, model in models.items():
            array_descriptors.append((pm, 'model'))
            array_addresses.append(model + OFF.boneTransforms)
            array_descriptors.append((pm, 'player_model'))
            array_addresses.append(pm + OFF.pm_rootBone)
        array_values = self.m.batch_u64(array_addresses, attempts=1)
        array_candidates = []
        for (pm, source), array in zip(array_descriptors, array_values):
            if _valid_user_ptr(array):
                array_candidates.append((pm, source, array))
        if not array_candidates:
            return

        element_descriptors = []
        element_addresses = []
        for pm, source, array in array_candidates:
            element_descriptors.append((pm, source, 'count'))
            element_addresses.append(array + 0x18)
            for bone_id in SCI_BONE_IDS:
                element_descriptors.append((pm, source, bone_id))
                element_addresses.append(array + OFF.bone_list + bone_id * 8)
        element_values = self.m.batch_u64(element_addresses, attempts=1)

        decoded_candidates = {}
        for (pm, source, key), value in zip(
            element_descriptors,
            element_values,
        ):
            decoded_candidates.setdefault((pm, source), {})[key] = value

        transforms_by_pm = {}
        for pm in models:
            best = {}
            for source in ('model', 'player_model'):
                values = decoded_candidates.get((pm, source), {})
                count = values.get('count', 0) & 0xFFFFFFFF
                if not (SCI_MIN_ARRAY_COUNT <= count <= 256):
                    continue
                transforms = {
                    bone_id: values.get(bone_id, 0)
                    for bone_id in SCI_BONE_IDS
                    if _valid_user_ptr(values.get(bone_id, 0))
                }
                # model.boneTransforms is the confirmed-correct source (see
                # OFFSET_RECOVERY.md trap #10); player_model (PM+0x98) is an
                # unverified compatible-layout guess that can pass the same
                # pointer-validity check while indexing an unrelated array,
                # so it must never outrank a model result just for resolving
                # more (wrong) pointers -- only used when model fails outright.
                if source == 'model' and len(transforms) >= 12:
                    best = transforms
                    break
                if len(transforms) > len(best):
                    best = transforms
            if best:
                transforms_by_pm[pm] = best
        if not transforms_by_pm:
            return

        transform_pairs = [
            (pm, bone_id, transform)
            for pm, transforms in transforms_by_pm.items()
            for bone_id, transform in transforms.items()
        ]
        internal_values = self.m.batch_u64(
            [transform + OFF.bone_transform for _, _, transform in transform_pairs],
            attempts=1,
        )
        internals = [
            (pm, bone_id, internal)
            for (pm, bone_id, _), internal in zip(
                transform_pairs,
                internal_values,
            )
            if _valid_user_ptr(internal)
        ]
        if not internals:
            return

        access_descriptors = []
        access_addresses = []
        for pm, bone_id, internal in internals:
            access_descriptors.append((pm, bone_id, 'td'))
            access_addresses.append(internal + OFF.transform_access)
            access_descriptors.append((pm, bone_id, 'idx'))
            access_addresses.append(internal + OFF.transform_access + 8)
        access_values = self.m.batch_u64(access_addresses, attempts=1)
        access_by_bone = {}
        for (pm, bone_id, kind), value in zip(
            access_descriptors,
            access_values,
        ):
            access_by_bone.setdefault((pm, bone_id), {})[kind] = value

        valid_access = []
        for (pm, bone_id), values in access_by_bone.items():
            td = values.get('td', 0)
            idx = values.get('idx', 0) & 0xFFFFFFFF
            if _valid_user_ptr(td) and idx < 100000:
                valid_access.append((pm, bone_id, td, idx))
        if not valid_access:
            return

        unique_tds = {td for _, _, td, _ in valid_access}
        td_to_buffers = {}
        if unique_tds:
            td_queries = []
            td_list = list(unique_tds)
            for td in td_list:
                td_queries.extend([td + 0x18, td + 0x20])
            td_values = self.m.batch_u64(td_queries, attempts=1)
            for i, td in enumerate(td_list):
                td_to_buffers[td] = (td_values[i * 2], td_values[i * 2 + 1])

        refreshed = {}
        for pm, bone_id, td, idx in valid_access:
            localTransforms, parentIndices = td_to_buffers.get(td, (0, 0))
            if _valid_user_ptr(localTransforms) and _valid_user_ptr(parentIndices):
                entry = refreshed.setdefault(pm, {'localTransforms': localTransforms, 'parentIndices': parentIndices, 'bones': {}})
                entry['bones'][bone_id] = idx

        for pm, data in refreshed.items():
            if len(data['bones']) >= 12:
                self._bone_slot_cache[pm] = data
                self._bone_resolve_retry_at.pop(pm, None)

    def _read_health_batch(self, pm_to_bp, active_pms):
        """Refresh slowly-changing health data at 10 Hz in one driver batch."""
        now = time.perf_counter()
        active_set = set(active_pms)
        if now - self._last_health_refresh_at < HEALTH_REFRESH_INTERVAL:
            return {
                pm: value
                for pm, value in self._health_cache.items()
                if pm in active_set
            }

        pairs = [
            (pm, bp)
            for pm, bp in pm_to_bp.items()
            if pm in active_set and _valid_user_ptr(bp)
        ]
        queries = []
        for _, bp in pairs:
            queries.extend((
                bp + OFF.lifestate,
                bp + OFF.lifestate + 8,
                bp + OFF.lifestate + 16,
            ))
        values = self.m.batch_u64(queries, attempts=1) if queries else []

        refreshed = {
            pm: value
            for pm, value in self._health_cache.items()
            if pm in active_set
        }
        for index, (pm, _) in enumerate(pairs):
            value_index = index * 3
            if value_index + 2 >= len(values):
                break
            raw = struct.pack(
                '<QQQ',
                values[value_index],
                values[value_index + 1],
                values[value_index + 2],
            )
            life_state = struct.unpack_from('<I', raw, 0)[0]
            health = struct.unpack_from('<f', raw, OFF._health - OFF.lifestate)[0]
            max_health = struct.unpack_from('<f', raw, OFF._maxHealth - OFF.lifestate)[0]
            if (
                life_state <= 10
                and math.isfinite(health)
                and math.isfinite(max_health)
                and 0.0 <= health <= 10000.0
                and 1.0 <= max_health <= 10000.0
            ):
                refreshed[pm] = (life_state, health, max_health)

        self._health_cache = refreshed
        self._last_health_refresh_at = now
        return refreshed

    def _read_player_frame_batch(self, pm_ptrs):
        """Read every per-frame player field in one shared-driver transaction."""
        if not pm_ptrs:
            return None, [], {}, {}, {}, {}, {}, {}

        descriptors = []
        addresses = []

        def add(pm, field, address):
            descriptors.append((pm, field))
            addresses.append(address)

        for pm in pm_ptrs:
            add(pm, 'local', pm + OFF.pm_is_local_player)
            add(pm, 'bp', pm + 0x18)
            add(pm, 'server_0_lo', pm + OFF.position_pm)
            add(pm, 'server_0_hi', pm + OFF.position_pm + 8)

            slot = self._transform_slot_cache.get(pm)
            if _valid_user_ptr(slot):
                add(pm, 'transform_lo', slot)
                add(pm, 'transform_hi', slot + 8)

            bp_hint = self._pm_bp_cache.get(pm)
            if _valid_user_ptr(bp_hint):
                add(pm, 'player_flags', bp_hint + OFF.playerFlags)
                add(pm, 'health_0', bp_hint + OFF.lifestate)
                add(pm, 'health_1', bp_hint + OFF.lifestate + 8)
                add(pm, 'health_2', bp_hint + OFF.lifestate + 16)

        values = self.m.batch_u64(addresses, attempts=1)
        by_pm = {}
        for (pm, field), value in zip(descriptors, values):
            by_pm.setdefault(pm, {})[field] = value

        def decode_position(lo, hi):
            """Returns (pos, why) -- why is None on success.

            This is the REAL, live position-decode path (unlike the
            same-shaped checks in _read_pm_server_positions_batch, which is
            dead code -- never called -- so wiring the [PM-DROP] reject
            reason into that one instead of here made every drop report
            the same "no candidate produced" default regardless of the
            real cause. Fixed by moving the reasoning here, where reads
            actually happen.
            """
            raw = struct.pack('<QQ', lo, hi)
            pos = struct.unpack_from('<fff', raw)
            if not all(math.isfinite(c) for c in pos):
                return None, "non-finite read (garbage or failed read)"
            if pos == (0.0, 0.0, 0.0):
                return None, "read returned zeros"
            if not (abs(pos[0]) < 6000 and abs(pos[2]) < 6000):
                return None, f"x/z out of map bounds ({pos[0]:.0f},{pos[2]:.0f})"
            if not (-200 < pos[1] < 2000):
                return None, f"y out of range ({pos[1]:.0f})"
            return pos, None

        active_set = set(pm_ptrs)
        pm_to_bp = {}
        transform_positions = {}
        server_positions = {}
        health_by_pm = {
            pm: health
            for pm, health in self._health_cache.items()
            if pm in active_set
        }
        sleeping_by_pm = {
            pm: sleeping
            for pm, sleeping in self._sleeping_cache.items()
            if pm in active_set
        }
        bone_positions_by_pm = {}
        local_pm = None

        for pm in pm_ptrs:
            fields = by_pm.get(pm, {})
            if fields.get('local', 0) & 0xFF:
                local_pm = pm

            bp = fields.get('bp', 0)
            if _valid_user_ptr(bp):
                self._pm_bp_cache[pm] = bp
            else:
                bp = self._pm_bp_cache.get(pm, 0)
            if _valid_user_ptr(bp):
                pm_to_bp[pm] = bp

            if 'player_flags' in fields:
                player_flags = fields['player_flags'] & 0xFFFFFFFF
                sleeping_by_pm[pm] = bool(
                    player_flags & PLAYER_FLAG_SLEEPING
                )

            transform_pos, _ = decode_position(
                fields.get('transform_lo', 0),
                fields.get('transform_hi', 0),
            )
            if transform_pos is not None:
                transform_positions[pm] = transform_pos

            candidates = []
            reject_why = None
            for off, prefix in ((OFF.position_pm, 'server_0'),):
                pos, why = decode_position(
                    fields.get(prefix + '_lo', 0),
                    fields.get(prefix + '_hi', 0),
                )
                if pos is None:
                    reject_why = why
                    continue
                if abs(pos[0]) < 2.0 and abs(pos[1]) < 2.0 and abs(pos[2]) < 2.0:
                    # The map is centred on the origin, so this 2 m cube is a
                    # real place to stand -- kept as a reject for now (see
                    # OFF.position_pm's history), but flagged as such rather
                    # than silently folded into "no candidate produced".
                    reject_why = f"inside the 2m origin guard {pos}"
                    continue
                candidates.append((off, pos))
            why_map = getattr(self, '_pos_reject_why', None)
            if why_map is None:
                why_map = self._pos_reject_why = {}
            if candidates:
                why_map.pop(pm, None)
            elif reject_why is not None:
                why_map[pm] = reject_why
            selected = self._select_pm_position(pm, candidates)
            if selected is not None:
                server_positions[pm] = selected

            anchor = transform_pos if transform_pos is not None else selected
            bones = {}
            had_bone_fields = False
            hierarchy = self._bone_slot_cache.get(pm)
            if hierarchy and anchor is not None:
                localTransforms = hierarchy['localTransforms']
                parentIndices = hierarchy['parentIndices']
                bone_dict = hierarchy['bones']
                
                if bone_dict:
                    max_idx = max(bone_dict.values())
                    if max_idx < 1000:
                        trs_data = self.m.read(localTransforms, (max_idx + 1) * 0x30)
                        pidx_data = self.m.read(parentIndices, (max_idx + 1) * 4)
                        
                        if trs_data and pidx_data:
                            for bone_id, idx in bone_dict.items():
                                if idx > max_idx: continue
                                if len(trs_data) < (idx + 1) * 0x30 or len(pidx_data) < (idx + 1) * 4:
                                    continue
                                    
                                t_offset = idx * 0x30
                                worldPos = struct.unpack_from('<3f', trs_data, t_offset)
                                p_idx = struct.unpack_from('<i', pidx_data, idx * 4)[0]
                                
                                valid = True
                                iters = 0
                                while p_idx >= 0 and iters < 256:
                                    iters += 1
                                    if len(trs_data) < (p_idx + 1) * 0x30 or len(pidx_data) < (p_idx + 1) * 4:
                                        valid = False
                                        break
                                        
                                    p_t_offset = p_idx * 0x30
                                    pt = struct.unpack_from('<3f', trs_data, p_t_offset)
                                    pq = struct.unpack_from('<4f', trs_data, p_t_offset + 0x10)
                                    ps = struct.unpack_from('<3f', trs_data, p_t_offset + 0x20)
                                    
                                    # Scale
                                    worldPos = (worldPos[0] * ps[0], worldPos[1] * ps[1], worldPos[2] * ps[2])
                                    # Rotate
                                    worldPos = quat_mult_vec(pq, worldPos)
                                    # Translate
                                    worldPos = (worldPos[0] + pt[0], worldPos[1] + pt[1], worldPos[2] + pt[2])
                                    
                                    p_idx = struct.unpack_from('<i', pidx_data, p_idx * 4)[0]
                                    
                                if valid and all(math.isfinite(c) for c in worldPos) and worldPos != (0.0, 0.0, 0.0):
                                    bones[bone_id] = worldPos

            if len(bones) >= 12:
                bone_positions_by_pm[pm] = bones
                pelvis = bones.get(0)
                if pelvis is not None:
                    dists = {
                        bid: round(math.dist(pelvis, pos), 2)
                        for bid, pos in bones.items()
                    }
                    suspect = {bid: d for bid, d in dists.items() if d > 2.2}
                    self._debug(
                        f"[BONE-ANATOMY] pm=0x{pm:X} dists={dists}"
                        + (f" SUSPECT={suspect}" if suspect else ""),
                        interval=2.0,
                    )
            elif hierarchy:
                self._bone_slot_cache.pop(pm, None)

            if all(
                key in fields
                for key in ('health_0', 'health_1', 'health_2')
            ):
                raw = struct.pack(
                    '<QQQ',
                    fields['health_0'],
                    fields['health_1'],
                    fields['health_2'],
                )
                life_state = struct.unpack_from('<I', raw, 0)[0]
                health = struct.unpack_from(
                    '<f',
                    raw,
                    OFF._health - OFF.lifestate,
                )[0]
                max_health = struct.unpack_from(
                    '<f',
                    raw,
                    OFF._maxHealth - OFF.lifestate,
                )[0]
                if (
                    life_state <= 10
                    and math.isfinite(health)
                    and math.isfinite(max_health)
                    and 0.0 <= health <= 10000.0
                    and 1.0 <= max_health <= 10000.0
                ):
                    health_by_pm[pm] = (
                        life_state,
                        health,
                        max_health,
                    )

        if local_pm:
            self._last_local_pm = local_pm
        elif self._last_local_pm in active_set:
            local_pm = self._last_local_pm

        enemies = [pm for pm in pm_ptrs if pm != local_pm]
        self._pm_bp_cache = {
            pm: bp
            for pm, bp in self._pm_bp_cache.items()
            if pm in active_set
        }
        self._health_cache = health_by_pm
        self._sleeping_cache = sleeping_by_pm
        return (
            local_pm,
            enemies,
            pm_to_bp,
            transform_positions,
            server_positions,
            health_by_pm,
            sleeping_by_pm,
            bone_positions_by_pm,
        )

    def _read_list_like_ptrs(self, obj, max_count=1024):
        if not _valid_user_ptr(obj):
            return []

        shapes = (
            (OFF.ListHashSet_vals, OFF.ListHashSet_size),
            (0x10, 0x18),
            (0x18, 0x20),
            (0x20, 0x18),
            (0x28, 0x30),
            (0x30, 0x38),
        )
        best = []
        for arr_off, count_off in shapes:
            arr = self.m.u64_retry(obj + arr_off, attempts=2)
            count = self.m.i32_retry(obj + count_off, attempts=2)
            ptrs, _ = self._read_il2cpp_array_ptrs(arr, count, max_count=max_count)
            if len(ptrs) > len(best):
                best = ptrs
        return best

    def _visible_baseplayers(self):
        local_player, _bp_sf = self._get_bp_context()

        # The current BasePlayer static field is a HiddenValue<Dictionary<...>>,
        # not the direct ListHashSet used by older builds. Its per-build decrypt
        # is not exported, so interpreting it as a list produces false pointers.
        # BaseNetworkable is the verified fallback used by _resolve_pm_to_bp().
        #
        # This used to `return [], local_player` when bp_sf was invalid. bp_sf is
        # not read below -- the list comes from _bp_list_cache, which the
        # BaseNetworkable walk fills -- so all that gate did was let ONE stale
        # klass RVA (BasePlayer_c, which was build-24840484 vintage until
        # 2026-09-05) discard the whole cached candidate list on every tick.
        # _resolve_pm_to_bp then had nothing to match against and had to fall
        # through to the ~600 ms BaseNetworkable walk, which is rationed by
        # BP_MAPPING_MIN_INTERVAL and backs off to BP_MAPPING_MAX_BACKOFF: that is
        # the "everything except the box disappears until it refreshes, and it
        # refreshes badly" symptom. Never gate the cache on an unrelated read.
        return list(self._bp_list_cache), local_player

    def _entity_buffer(self, max_count=WORLD_ENTITY_MAX_SCAN):
        """Walk the BaseNetworkable backing array once for all consumers.

        Returns (list_dict, arr, ptrs, count), or None if the chain is dead.

        Holds _entity_lock: callers on the tick thread and on the background
        pm->bp resolver both rebuild the caches this touches.
        """
        with self._entity_lock:
            return self._entity_buffer_locked(max_count)

    def _entity_buffer_locked(self, max_count):
        now = time.perf_counter()

        # Tier 1: reuse the walked pointer list outright.
        if (
            self._entity_ptr_cache
            and now - self._entity_ptr_cache_at <= ENTITY_BUFFER_CACHE_TTL
        ):
            return (
                self._cached_list_dict,
                self._cached_arr,
                self._entity_ptr_cache,
                self._entity_ptr_cache_count,
            )

        # Tier 2: skip the chain walk but prove the cached array is still live.
        # The element-count read is needed either way, so validating costs
        # nothing on top of it: a count outside the plausible range means the
        # pointer went stale and the full walk has to run.
        arr, count = self._cached_arr, 0
        for from_cache in (True, False):
            if from_cache:
                if (
                    not _valid_user_ptr(arr)
                    or now - self._entity_chain_at > ENTITY_CHAIN_CACHE_TTL
                ):
                    continue
                count = self.m.i32_retry(arr + OFF.buffer_count, attempts=2)
                if not 0 < count <= max_count:
                    continue
            else:
                chain = self._resolve_entity_chain()
                if not chain:
                    return None
                _, arr = chain
                count = self.m.i32_retry(arr + OFF.buffer_count, attempts=3)

            ptrs, _ = self._read_il2cpp_array_ptrs(
                arr, count, max_count=max_count
            )
            if ptrs:
                self._entity_ptr_cache = ptrs
                self._entity_ptr_cache_count = count
                self._entity_ptr_cache_at = now
                return self._cached_list_dict, arr, ptrs, count
            # The cached array produced nothing: it is far more likely to be
            # dead than genuinely empty, so fall through to the real walk.
        return self._cached_list_dict, arr, [], count

    def _entity_baseplayers(self, max_count=WORLD_ENTITY_MAX_SCAN):
        with self._entity_lock:
            return self._entity_baseplayers_locked(max_count)

    def _entity_baseplayers_locked(self, max_count):
        buf = self._entity_buffer_locked(max_count)
        if buf is None:
            # _resolve_entity_chain() itself failed -- distinct from "found
            # the chain but the array read came back empty" below.
            self._entity_bp_diag = "no entity chain (see [DBG] no BN chain)"
            return []
        _list_dict, _arr, ptrs, raw_count = buf
        if not ptrs:
            # raw_count is the array's OWN element count, read before
            # _read_il2cpp_array_ptrs runs -- if this is > max_count
            # (WORLD_ENTITY_MAX_SCAN, 20000), that function hard-rejects the
            # WHOLE read (see its own top line) instead of truncating, so a
            # busy server's true entity count silently produces nothing here
            # rather than a partial list. That is the one case worth
            # distinguishing from "genuinely 0 entities right now".
            self._entity_bp_diag = (
                f"array read empty (raw_count={raw_count}, "
                f"max_count={max_count}{' -- OVER THE CAP' if raw_count > max_count else ''})"
            )
            return []

        # prefabID is a plain uint at BaseNetworkable+0x54 and the player prefab
        # hash is stable, so the value is immutable for the life of an entity.
        # Cache it per pointer rather than re-reading all ~6000 live entities
        # every time a PlayerModel goes unmapped: that full re-read is what made
        # _refresh_pm_to_bp_cache cost 400-800ms ([TICK-LATENCY] pos=).
        cache = self._entity_prefab_cache
        live = set(ptrs)
        if len(cache) > len(live):
            # Dead entities leave the buffer. Dropping them here is also what
            # makes address reuse safe: a recycled address is simply "new".
            cache = {q: v for q, v in cache.items() if q in live}
            self._entity_prefab_cache = cache
        stale = [q for q in ptrs if q not in cache]
        # Plus a rotating window, covering an address freed and reused between
        # two scans.
        start = self._entity_prefab_cursor % len(ptrs)
        window = ptrs[start:start + ENTITY_PREFAB_REVALIDATE_PER_SCAN]
        self._entity_prefab_cursor = start + len(window)
        known = set(stale)
        stale.extend(q for q in window if q not in known)
        if stale:
            prefab_words = self.m.batch_u64(
                [q + OFF.prefab_id for q in stale],
                attempts=1,
            )
            for q, word in zip(stale, prefab_words):
                prefab = word & 0xFFFFFFFF
                # Never cache 0: that is what a failed read looks like, and
                # caching it would hide a real player until the rotating window
                # came back around. Leaving it out simply retries next call.
                if prefab:
                    cache[q] = prefab

        players = [q for q in ptrs if cache.get(q) == OFF.k_player_prefab_id]
        # Diagnostic for [PM2BP-STUCK] (see OFF.k_player_prefab_id -- that
        # hash is independently confirmed correct, 2026-09-10, against a
        # public StringPool reference for assets/prefabs/player/player.prefab
        # -- so if this ever shows a low player count against a busy server,
        # look at raw_count/uncached below, not the hash).
        uncached = sum(1 for q in ptrs if q not in cache)
        self._entity_bp_diag = (
            f"raw_count={raw_count} ptrs={len(ptrs)} players={len(players)} "
            f"uncached_prefab={uncached} "
            f"fallback={'YES (0 players matched)' if not players else 'no'}"
        )
        # If the prefab hash ever changes, retain the structurally safe fallback
        # instead of making every PlayerModel mapping disappear.
        return players or ptrs

    def _map_baseplayers_to_pm(self, bp_ptrs, pm_set):
        if not bp_ptrs or not pm_set:
            return {}

        bp_ptrs = [bp for bp in bp_ptrs if _valid_user_ptr(bp)]
        if not bp_ptrs:
            return {}

        def map_at(offset):
            vals = self.m.read_ptr_array([bp + offset for bp in bp_ptrs])
            mapped = {}
            for bp, pm in zip(bp_ptrs, vals):
                if pm in pm_set:
                    mapped[pm] = bp
            return mapped

        mapped = map_at(self._player_model_offset)
        if mapped:
            return mapped

        # 0x18 (not 0x300) so this also covers offsets seen in the wild well
        # below where BasePlayer.playerModel has historically sat on this build
        # (e.g. 0xE8, 0xD0) as well as the higher range from older dumps.
        offsets = list(range(0x18, 0x901, 8))
        sample_bps = bp_ptrs[:min(len(bp_ptrs), 24)]
        # Track the top TWO, not just the best -- see the acceptance check
        # below for why the runner-up matters.
        best_off = second_off = 0
        best_hits = second_hits = 0
        for i in range(0, len(offsets), 64):
            chunk = offsets[i:i + 64]
            addrs = [bp + off for bp in sample_bps for off in chunk]
            vals = self.m.read_ptr_array(addrs, attempts=2, fallback_limit=0)
            hits_by_off = {off: 0 for off in chunk}
            idx = 0
            for _bp in sample_bps:
                for off in chunk:
                    if idx < len(vals) and vals[idx] in pm_set:
                        hits_by_off[off] += 1
                    idx += 1
            for off, hits in hits_by_off.items():
                if hits > best_hits:
                    second_off, second_hits = best_off, best_hits
                    best_off, best_hits = off, hits
                elif hits > second_hits:
                    second_off, second_hits = off, hits

        # `self._player_model_offset` is SHARED, cached state -- every future
        # tick's fast path (map_known_offset(cached_bps, ...)) trusts it
        # blindly, for every player, not just the ones this call is chasing.
        # The old bar here was `if best_hits:`, i.e. ONE coincidental pointer
        # match anywhere across up to 24 BasePlayers x ~280 offsets was
        # enough to overwrite it -- and unlike a per-call miss, this failure
        # doesn't go away next tick: it corrupts the shared offset
        # permanently (nothing here ever un-latches it), so every player who
        # hits this fallback afterward inherits the wrong value until the
        # process restarts. That silent-corruption shape is exactly the "a
        # player never comes back, only a relaunch fixes it" report this was
        # added to explain. offsets_decrypts_export.h independently confirms
        # 0x498 for this build, so this scanner should now rarely if ever
        # legitimately need to override it -- raising the bar costs nothing
        # real and closes the false-positive path.
        #
        # Confidence bar: enough of pm_set actually matched (not just one
        # lucky pointer -- more than half of what COULD have matched, given
        # how many BasePlayers were sampled), AND the winner clearly beats
        # the runner-up (a near-tie means the data doesn't distinguish them,
        # which is exactly the situation a single coincidental hit produces).
        achievable = min(len(sample_bps), len(pm_set))
        confident = (
            best_hits >= 2
            and best_hits >= max(2, (achievable + 1) // 2)
            and best_hits > second_hits
        )
        if confident:
            changed = best_off != self._player_model_offset
            self._player_model_offset = best_off
            if changed:
                # DEBUG_PLAYERS gates self._debug and defaults False, which would
                # silently eat exactly the message someone needs when the fixed
                # offset has drifted. Print unconditionally, once per change.
                print(
                    f"[PM2BP-DBG] BasePlayer.playerModel offset changed to "
                    f"0x{best_off:X} ({best_hits}/{len(sample_bps)} sample hits, "
                    f"runner-up 0x{second_off:X}={second_hits})",
                    flush=True,
                )
            return map_at(best_off)
        if best_hits:
            # Did not clear the confidence bar -- report it (this used to be
            # silent, indistinguishable from a genuine 0/24 miss) but do NOT
            # touch self._player_model_offset. This call's own contribution
            # fails; the shared, known-good offset survives for next time.
            print(
                f"[PM2BP-DBG] offset scan found only weak evidence, ignoring "
                f"it: best=0x{best_off:X} hits={best_hits} vs runner-up "
                f"0x{second_off:X}={second_hits} (sampled {len(sample_bps)} "
                f"bps against {len(pm_set)} still-missing pm) -- keeping "
                f"0x{self._player_model_offset:X}",
                flush=True,
            )
        return {}

    def _resolve_pm_to_bp(self, pm_ptrs):
        if not pm_ptrs:
            return {}

        # PlayerModel inherits MonoBehaviour: +0x18 is the inherited
        # m_CancellationTokenSource, not a BasePlayer back-reference. Resolve
        # the relationship in the direction exported by the current dump:
        # BasePlayer.playerModel (+0x3F0) -> PlayerModel.
        pm_set = set(pm_ptrs)
        cached_bps, local_player = self._visible_baseplayers()

        def map_known_offset(bp_values, wanted):
            unique_bps = list(dict.fromkeys(
                bp for bp in bp_values if _valid_user_ptr(bp)
            ))
            if not unique_bps or not wanted:
                return {}, {}
            # self._player_model_offset, not the OFF.playerModel constant: the
            # scanner below (_map_baseplayers_to_pm) updates this field the
            # moment the fixed constant stops working, and that update is
            # useless if this closure keeps reading the stale constant instead.
            model_ptrs = self.m.batch_u64(
                [bp + self._player_model_offset for bp in unique_bps],
                attempts=1,
            )
            # Full bp->pm read kept alongside the wanted-filtered result so
            # _log_pm2bp_stuck can tell "this pm never showed up in the
            # candidate pool" from "it did, the offset read just didn't
            # match it" -- at zero extra IOCTL cost, the batch already read
            # every value.
            full = dict(zip(unique_bps, model_ptrs))
            matched = {pm: bp for bp, pm in full.items() if pm in wanted}
            return matched, full

        if _valid_user_ptr(local_player):
            cached_bps.append(local_player)
        pm_to_bp, _ = map_known_offset(cached_bps, pm_set)

        # Refresh BaseNetworkable only for unresolved PlayerModels. Existing
        # mappings therefore remain cheap while joins/reconnects still converge.
        missing = pm_set.difference(pm_to_bp)
        source = "cache"
        live_bps = []
        live_full = {}
        if missing:
            live_bps = self._entity_baseplayers()
            if _valid_user_ptr(local_player):
                live_bps.append(local_player)
            if live_bps:
                live_matched, live_full = map_known_offset(live_bps, missing)
                pm_to_bp.update(live_matched)
                self._bp_list_cache = list(dict.fromkeys(live_bps))
                source = "base_networkable"

        # Last resort: brute-force offset scan. BasePlayer.playerModel is a
        # plain (unencrypted) pointer field, but its offset is not stable
        # across Rust builds the way the IL2CPP struct layout constants
        # (Model.boneTransforms, Transform.InternalPtr, ...) are — those come
        # from the engine, this one is game code and moves on nearly every
        # patch. _entity_baseplayers() can return real BasePlayer pointers and
        # _resolve_entity_chain()/_scan_world_entities can succeed, and the
        # bone pipeline still starves forever ([BONE-DBG] rig=0 job=0) if the
        # one fixed offset used to confirm bp<->pm identity has drifted. This
        # was previously dead code — defined, never called.
        still_missing = pm_set.difference(pm_to_bp)
        if still_missing:
            scan_bps = live_bps or self._bp_list_cache or cached_bps
            if _valid_user_ptr(local_player) and local_player not in scan_bps:
                scan_bps = scan_bps + [local_player]
            if scan_bps:
                found = self._map_baseplayers_to_pm(scan_bps, still_missing)
                if found:
                    pm_to_bp.update(found)
                    source = "offset_scan"

        self._log_pm2bp_stuck(pm_set, pm_to_bp, live_bps, live_full)
        self._last_bp_source = source if pm_to_bp else f"{source}_miss"
        return pm_to_bp

    def _log_pm2bp_stuck(self, pm_set, pm_to_bp, live_bps, live_full):
        """Diagnose a pm that survives every stage of _resolve_pm_to_bp
        still unmapped, across several consecutive walks -- instead of
        guessing why a handful of players never get a skeleton (the
        2026-08-26 review's `norig=3-5 cold=1` finding). Fires once per pm
        per streak, at zero extra IOCTL cost: live_full already holds every
        bp->pm pair the entity-buffer-walk pass actually read.
        """
        still = pm_set.difference(pm_to_bp)
        streak = self._pm2bp_miss_streak
        for pm in list(streak):
            if pm not in still:
                del streak[pm]
        live_values = set(live_full.values()) if live_full else set()
        for pm in still:
            streak[pm] = streak.get(pm, 0) + 1
            if streak[pm] == PM2BP_STUCK_STREAK:
                print(
                    f"[PM2BP-STUCK] pm=0x{pm:X} unresolved for "
                    f"{PM2BP_STUCK_STREAK} consecutive walks | "
                    f"live_bps={len(live_bps)} "
                    f"seen_in_entity_walk={pm in live_values} "
                    f"offset=0x{self._player_model_offset:X} "
                    f"entity_walk[{self._entity_bp_diag}]",
                    flush=True,
                )

    def _log_pm_drop(self, pm, gate, detail, pm_to_bp):
        """Name a PlayerModel that was dropped from the player list entirely.

        These two gates do not degrade a player -- they remove them, so the
        overlay shows no box, no name and no skeleton for someone the game is
        still drawing on screen. That is the "players not affected by the ESP"
        report, and it was silent: skipped_pos only ever surfaced as an
        aggregate `skip=N`, and skipped_hp was counted and never printed at
        all. Throttled per pm so a persistently unreadable player does not
        flood the console.
        """
        now = time.perf_counter()
        # getattr, not self._x: the tests build this object with
        # object.__new__ so __init__ never runs, and a diagnostic that
        # crashes the tick it was added to diagnose is worse than no
        # diagnostic at all.
        seen = getattr(self, '_pm_drop_logged', None)
        if seen is None:
            seen = self._pm_drop_logged = {}
        if now < seen.get(pm, 0.0):
            return
        seen[pm] = now + 5.0
        for q in list(seen):
            if seen[q] < now - 60.0:
                del seen[q]
        bp = pm_to_bp.get(pm, 0) if pm_to_bp else 0
        print(
            f"[PM-DROP] pm=0x{pm:X} dropped by {gate}: {detail} | "
            f"bp={'0x%X' % bp if bp else 'UNRESOLVED'} "
            f"cached_pos={'y' if pm in getattr(self, '_pm_pos_cache', {}) else 'n'}",
            flush=True,
        )

    def _get_lc_buffer(self):
        """Return a live or recently cached ListComponent buffer/count pair."""
        m, ga = self.m, self.ga
        now = time.perf_counter()
        cache_valid = (
            _valid_user_ptr(self._cached_lc_buf_arr)
            and 0 < self._cached_lc_count <= 500
            and now - self._lc_chain_cache_at <= self._lc_cache_ttl
        )
        if cache_valid and now < self._next_lc_refresh_at:
            self._lc_fast_path = True
            return (
                self._cached_lc_buf_arr,
                self._cached_lc_count,
                True,
                "",
            )
        self._lc_fast_path = False

        self._next_lc_refresh_at = now + self._lc_refresh_interval

        # Once the BufferList pointer is known, refresh its buffer and count in
        # one batch. Re-walking klass → static fields → wrapper → list cost four
        # complete driver transactions (roughly 120 ms on the live system).
        if _valid_user_ptr(self._cached_lc_pm_list):
            values = m.batch_u64_priority(
                [
                    self._cached_lc_pm_list + OFF.ListComponent_buffer,
                    self._cached_lc_pm_list + OFF.ListComponent_size,
                ],
                attempts=1,
            )
            if len(values) >= 2:
                buf_arr = values[0]
                lc_count = values[1] & 0xFFFFFFFF
                if lc_count & 0x80000000:
                    lc_count -= 0x100000000
                if _valid_user_ptr(buf_arr) and 0 < lc_count <= 500:
                    self._cached_lc_buf_arr = buf_arr
                    self._cached_lc_count = lc_count
                    self._lc_chain_cache_at = now
                    return buf_arr, lc_count, False, ""
            if cache_valid:
                return (
                    self._cached_lc_buf_arr,
                    self._cached_lc_count,
                    True,
                    "",
                )

        lc_klass = self._cached_lc_klass
        if not _valid_user_ptr(lc_klass):
            lc_klass = m.u64(ga + OFF.ListComponent_PlayerModel_c)
            if _valid_user_ptr(lc_klass):
                self._cached_lc_klass = lc_klass
        if not _valid_user_ptr(lc_klass):
            if cache_valid:
                return self._cached_lc_buf_arr, self._cached_lc_count, True, ""
            return 0, 0, False, (
                f"no LC klass ga=0x{ga:X} "
                f"rva=0x{OFF.ListComponent_PlayerModel_c:X}"
            )

        lc_sf = self._cached_lc_sf
        if not _valid_user_ptr(lc_sf):
            lc_sf = m.u64(lc_klass + OFF.klass_static_fields)
            if _valid_user_ptr(lc_sf):
                self._cached_lc_sf = lc_sf
        if not _valid_user_ptr(lc_sf):
            if cache_valid:
                return self._cached_lc_buf_arr, self._cached_lc_count, True, ""
            return 0, 0, False, f"no LC sf klass=0x{lc_klass:X}"

        wrapper = m.u64(lc_sf + OFF.ListComponent_instance)
        if _valid_user_ptr(wrapper):
            self._cached_lc_wrapper = wrapper
        else:
            wrapper = self._cached_lc_wrapper
        if not _valid_user_ptr(wrapper):
            if cache_valid:
                return self._cached_lc_buf_arr, self._cached_lc_count, True, ""
            return 0, 0, False, f"no LC wrapper sf=0x{lc_sf:X}"

        pm_list = m.u64(wrapper + OFF.ListComponent_parent)
        if _valid_user_ptr(pm_list):
            self._cached_lc_pm_list = pm_list
        else:
            pm_list = self._cached_lc_pm_list
        if not _valid_user_ptr(pm_list):
            if cache_valid:
                return self._cached_lc_buf_arr, self._cached_lc_count, True, ""
            return 0, 0, False, f"no LC list wrapper=0x{wrapper:X}"

        buf_arr = m.u64(pm_list + OFF.ListComponent_buffer)
        lc_count = m.i32(pm_list + OFF.ListComponent_size)
        if _valid_user_ptr(buf_arr) and 0 < lc_count <= 500:
            self._cached_lc_buf_arr = buf_arr
            self._cached_lc_count = lc_count
            self._lc_chain_cache_at = now
            return buf_arr, lc_count, False, ""

        if cache_valid:
            return self._cached_lc_buf_arr, self._cached_lc_count, True, ""
        return 0, 0, False, f"bad LC count={lc_count} buf=0x{buf_arr:X}"

    def _tick(self):
        m, ga = self.m, self.ga
        t_start = time.perf_counter()
        # Reset per-tick driver accounting so [TICK-LATENCY] can attribute time
        # to "waiting on the driver" vs "our own Python work".
        m.io_calls = 0
        m.io_slow_calls = 0
        m.io_wait_s = 0.0
        m.yield_wait_s = 0.0
        m.lock_wait_s = 0.0

        # ── 1. VP matrix (Handled in real-time by render loop to avoid lock contention) ──
        vp, cam_pos = None, None
        t_vp = 0.0

        # ── 2. ListComponent<PlayerModel> → all player models ─────────────
        t0 = time.perf_counter()
        buf_arr, lc_count, lc_cache_used, lc_error = self._get_lc_buffer()
        if lc_error:
            self._set_diag(lc_error)
            return
        t_chain = (time.perf_counter() - t0) * 1000
        self._hit_window_chain_total += 1
        self._hit_window_chain_hits += 1 if self._lc_fast_path else 0

        # Refresh the stable pointer list at 1 Hz. Reading it on every tick costs
        # a full 15-30 ms transaction without improving moving-player positions.
        t0 = time.perf_counter()
        now_players = time.perf_counter()
        pm_list_fast_path = (
            self._pm_ptr_cache
            and now_players < self._next_pm_list_refresh_at
        )
        if pm_list_fast_path:
            pm_ptrs = list(self._pm_ptr_cache)
            payload = self._pm_payload
            used_pm_cache = True
        else:
            pm_ptrs, payload, used_pm_cache = self._read_player_model_list(
                buf_arr,
                lc_count,
            )
            self._next_pm_list_refresh_at = (
                now_players + PLAYER_LIST_REFRESH_INTERVAL
            )
        t_pm_list = (time.perf_counter() - t0) * 1000
        self._hit_window_pm_list_total += 1
        self._hit_window_pm_list_hits += 1 if pm_list_fast_path else 0

        # Transform chains are stable for the lifetime of a PlayerModel. Resolve
        # only missing slots and keep direct positions in the hot frame batch.
        cached_pm_to_bp = {
            pm: self._pm_bp_cache.get(pm, 0)
            for pm in pm_ptrs
            if _valid_user_ptr(self._pm_bp_cache.get(pm, 0))
        }
        # Only the *rig* (bone Transform → native → TransformAccess) still needs
        # a resolve pass here. Player world position no longer comes from a
        # transform slot at all — see _read_transform_positions_batch's docstring.
        missing_bone_slots = any(
            len(self._bone_slot_cache.get(pm, {})) < 12
            for pm in cached_pm_to_bp
        )
        now_transform = time.perf_counter()
        if (
            cached_pm_to_bp
            and missing_bone_slots
            and now_transform >= self._next_transform_resolve_at
        ):
            self._next_transform_resolve_at = (
                now_transform + TRANSFORM_RESOLVE_RETRY_INTERVAL
            )
            self._resolve_bone_slots_batch(cached_pm_to_bp)

        # One transaction now contains local flags, BasePlayer links, both
        # position sources, cached Transform slots and cached health addresses.
        t0 = time.perf_counter()
        (
            local_pm,
            enemy_pm_ptrs,
            pm_to_bp,
            transform_positions,
            pm_server_positions,
            health_by_pm,
            sleeping_by_pm,
            bone_positions_by_pm,
        ) = self._read_player_frame_batch(pm_ptrs)
        t_pos_batch = (time.perf_counter() - t0) * 1000
        t_filter = 0.0

        # ── 3. Batch: all PlayerModel pointers ──
        self._debug_players(
            f"LC count={lc_count} valid_pm={len(pm_ptrs)} local={'0x%X'%local_pm if local_pm else 'none'} "
            f"enemies={len(enemy_pm_ptrs)} payload=+0x{payload:X}"
            f"{' ptr-cache' if used_pm_cache else ''}"
            f"{' lc-cache' if lc_cache_used else ''}"
            # No vp= here: the tick sets `vp, cam_pos = None, None` unconditionally
            # (the render loop owns the matrix now, see step 1), so this could only
            # ever print "miss" -- a permanently-red indicator that says nothing
            # about the run and trains the reader to skip the whole line.
        )

        if not enemy_pm_ptrs:
            with self._lock:
                if vp:
                    self.vp_matrix = vp
            self._scan_world_entities()
            self._take_snapshot()
            return

        pm_ptrs = enemy_pm_ptrs

        # ── 4. Map PM → BasePlayer ──
        t_mapping = 0.0
        if not ENABLE_BP_MAPPING:
            pm_to_bp = {}
            self._last_bp_source = "off"
        else:
            pm_to_bp = {
                pm: bp
                for pm, bp in pm_to_bp.items()
                if pm in set(pm_ptrs)
            }
            # Do NOT overwrite the resolver's own label here. model.py's
            # frame batch does not read the BasePlayer pointer at all -- it
            # takes it from _pm_bp_cache, which _resolve_pm_to_bp fills on a
            # background thread. Stamping "frame_batch" over that made
            # [POS-DBG] bp_src= report a path that does not exist, which is
            # worse than no label at all.
        _pm_parent_cands = []
        if ENABLE_HEAVY_DIAG:
            t0 = time.perf_counter()
            _pm_parent_cands = m.read_ptr_array([p + 0x18 for p in pm_ptrs], attempts=2)
            _bp_cands = [(pm, bp) for pm, bp in zip(pm_ptrs, _pm_parent_cands)
                         if _valid_user_ptr(bp)]
            if _bp_cands:
                _bp_pm_back = m.read_ptr_array([bp + self._player_model_offset for _, bp in _bp_cands], attempts=2)
                for (pm, bp_cand), pm_back in zip(_bp_cands, _bp_pm_back):
                    if pm_back == pm:
                        pm_to_bp[pm] = bp_cand
            t_mapping = (time.perf_counter() - t0) * 1000

        # One-shot PARENT-DIAG + BP-SCAN: find real BasePlayer field offset in PM
        if ENABLE_HEAVY_DIAG and not getattr(self, '_parent_diag_done', False):
            self._parent_diag_done = True
            import sys
            _sp = lambda *a: print(*a, file=sys.stderr)
            _sp(f"\n[PARENT-DIAG] pm_count={len(pm_ptrs)} verified_at_0x18={len(pm_to_bp)}")
            for pm, bp_raw in zip(pm_ptrs[:3], _pm_parent_cands[:3]):
                pm_back = m.u64(bp_raw + self._player_model_offset) if _valid_user_ptr(bp_raw) else 0
                match = "OK" if pm_back == pm else f"MISMATCH(back=0x{pm_back:X})"
                _sp(f"  pm=0x{pm:X} +0x18=0x{bp_raw:X} {match}")
            _offsets = list(range(0x18, 0x201, 8))
            for scan_pm in pm_ptrs[:3]:
                scan_vals = m.batch_u64([scan_pm + o for o in _offsets])
                found = None
                for o, v in zip(_offsets, scan_vals):
                    if not _valid_user_ptr(v):
                        continue
                    pm_back = m.u64(v + self._player_model_offset)
                    if pm_back == scan_pm:
                        found = o
                        _sp(f"  pm=0x{scan_pm:X} → BP at +0x{o:X}=0x{v:X}  *** FOUND ***")
                        break
                if not found:
                    _sp(f"  pm=0x{scan_pm:X} → no BP found in 0x18-0x200")

        _vpl_diag = f"pm2bp={len(pm_to_bp)}:{self._last_bp_source}@0x{self._player_model_offset:X}"

        # ── 5. Build player list from PlayerModel positions ──
        _hp_off  = OFF._health    - OFF.lifestate
        _mhp_off = OFF._maxHealth - OFF.lifestate
        new_players = []
        skipped_pos = 0
        skipped_hp  = 0
        sample_rows = []
        pm_pos_cache = getattr(self, '_pm_pos_cache', {})

        local_pos = None
        if local_pm:
            pos_raw = transform_positions.get(local_pm)
            if pos_raw is None:
                pos_raw = pm_server_positions.get(local_pm)
            if pos_raw is not None:
                local_pos = self._smooth_pm_position(local_pm, pos_raw)

        pathA_hits = pathB_hits = pathC_hits = 0
        _diag_done = getattr(self, '_diag_pm_done', False)
        if not hasattr(self, '_diag_pm_done'):
            self._diag_pm_done = False
        for pm in pm_ptrs:
            pos = None

            # Path A: retired. It used to read the root bone's raw TRS slot and
            # call it a world position, which made unrelated players collapse
            # onto identical coordinates. transform_positions is now always
            # empty; the lookup stays so the diagnostic below still has a hook,
            # and so re-enabling a *properly composed* transform source later is
            # a one-line change. See _read_transform_positions_batch.
            bp = pm_to_bp.get(pm)
            if bp:
                pos = transform_positions.get(pm)
                if pos is not None:
                    pathA_hits += 1
                # One-shot diagnostic: trace chain for the first bp-mapped pm
                if ENABLE_HEAVY_DIAG and not _diag_done:
                    import sys
                    model_ptr = m.u64(bp + OFF.model)
                    root_tf = (
                        m.u64(model_ptr + OFF.rootBone_Model)
                        if _valid_user_ptr(model_ptr)
                        else 0
                    )
                    _pr = lambda *a: print(*a, file=sys.stderr)
                    _pr(f"\n[MODEL-DIAG] pm=0x{pm:X} bp=0x{bp:X}")
                    _pr(f"  bp+0x{OFF.model:X} (model_ptr)=0x{model_ptr:X}")
                    if _valid_user_ptr(model_ptr):
                        _pr(f"  model+0x{OFF.rootBone_Model:X} (root_tf)=0x{root_tf:X}")
                        if _valid_user_ptr(root_tf):
                            _nv = m.u64(root_tf + 0x10)
                            _pr(f"  root_tf+0x10 (native)=0x{_nv:X}")
                            if _valid_user_ptr(_nv):
                                _td  = m.u64(_nv + 0x28)
                                _idx = m.u32(_nv + 0x30)
                                _pr(f"  native+0x28 (td)=0x{_td:X}  idx={_idx}")
                                if _valid_user_ptr(_td):
                                    _pa = m.u64(_td + 0x18)
                                    _pr(f"  td+0x18 (pos_arr)=0x{_pa:X}")
                                    if _valid_user_ptr(_pa):
                                        _pr(f"  pos[stride=0x30] = {m.vec3f(_pa + 0x30 * _idx)}")
                                        _pr(f"  pos[stride=0x10] = {m.vec3f(_pa + 0x10 * _idx)}")
                                        _pr(f"  pos[stride=0x18] = {m.vec3f(_pa + 0x18 * _idx)}")
                    _pr(f"  → final pos = {pos}")
                    _diag_done = True
                    self._diag_pm_done = True

            # Path B (now the primary source): PlayerModel::position at 0x2F8,
            # read in the frame batch. This is exactly what Cl1kExternal's
            # BasePlayer::GetPosition uses. Offset 0x310 is velocity in this
            # build and must never be treated as a position; 0x35C/0x368 are
            # small rotation-like vectors in live logs, not world positions.
            if pos is None:
                pos = pm_server_positions.get(pm)
                if pos is not None:
                    pos = self._smooth_pm_position(pm, pos)
                    pathB_hits += 1

            # Path C: stale cache (keeps visible across ticks with zero-position)
            if pos is None:
                if ENABLE_HEAVY_DIAG and not _diag_done:
                    _diag_pm_structure(m, pm)
                    self._diag_pm_done = True
                    _diag_done = True
                pos = pm_pos_cache.get(pm)
                if pos is None:
                    skipped_pos += 1
                    self._log_pm_drop(
                        pm, "no position",
                        getattr(self, '_pos_reject_why', {}).get(
                            pm, "no candidate produced"),
                        pm_to_bp,
                    )
                    continue
                pathC_hits += 1

            pm_pos_cache[pm] = pos

            frame_bones = bone_positions_by_pm.get(pm)
            if frame_bones:
                self._bone_position_cache[pm] = (
                    pos,
                    dict(frame_bones),
                    time.perf_counter(),
                )

            # Default: no health data
            hp = max_hp = -1.0

            vital = health_by_pm.get(pm)
            if vital is not None:
                ls, hp, max_hp = vital
                # max_hp < 1.0 is dead code: the producer only records an
                # entry when 1.0 <= max_health <= 10000, so this reduces to
                # the lifestate test. Kept explicit so the real condition is
                # readable -- a player is removed from the ESP ENTIRELY here,
                # which is a heavy price for one attribute reading oddly.
                if ls > 1:
                    skipped_hp += 1
                    self._log_pm_drop(
                        pm, "bad lifestate", f"lifestate={ls}", pm_to_bp,
                    )
                    continue

            new_players.append({
                'pm': pm,
                'pos': pos,
                'hp': hp,
                'max_hp': max_hp,
                'sleeping': sleeping_by_pm.get(pm, False),
                'bones': frame_bones,
                # None until the velocity probe latches a slot, or forever if
                # neither candidate tracks motion. get_snapshot falls back to
                # differencing two position samples in that case.
                'vel': self._pm_vel_cache.get(pm),
            })
            if len(sample_rows) < 5:
                hp_str = f"{hp:.3g}/{max_hp:.3g}" if max_hp > 0 else "?"
                sample_rows.append(
                    f"TARGET pm=0x{pm:X} hp={hp_str} pos=({pos[0]:.1f},{pos[1]:.1f},{pos[2]:.1f})"
                )

        # ── Cache: TTL-based eviction (keeps positions for 5 ticks after last seen) ──
        _cache_age = getattr(self, '_pm_cache_age', {})
        active_set = set(pm_ptrs)
        for k in list(_cache_age):
            if k in active_set:
                _cache_age[k] = 0
            else:
                _cache_age[k] = _cache_age.get(k, 0) + 1
                if _cache_age[k] > 1:
                    del _cache_age[k]
                    pm_pos_cache.pop(k, None)
                    self._pm_pos_offset_cache.pop(k, None)
                    self._pm_raw_pos_cache.pop(k, None)
                    self._pm_smooth_pos_cache.pop(k, None)
                    self._pm_pos_time_cache.pop(k, None)
                    self._pm_pos_bp_cache.pop(k, None)
                    self._pm_jump_state.pop(k, None)
                    self._bone_slot_cache.pop(k, None)
                    self._bone_position_cache.pop(k, None)
                    self._bone_resolve_retry_at.pop(k, None)
        for pm in active_set:
            _cache_age.setdefault(pm, 0)
        self._pm_cache_age = _cache_age
        self._pm_pos_cache = pm_pos_cache

        t0_disp = time.perf_counter()
        # ── Display: build from full cache so older ticks don't blank the screen ──
        # Update/persist camera position and local player position
        if cam_pos is not None:
            self.cam_pos = cam_pos
        else:
            cam_pos = getattr(self, 'cam_pos', None)

        if local_pos is not None:
            self.local_pos = local_pos
        else:
            local_pos = getattr(self, 'local_pos', None)

        # ── Local eye offset (aim origin precision) ──
        # We need the EXACT eye height, not the hardcoded 1.6 m the fallback
        # uses. Rust stores it in PlayerEyes.viewOffset (Vector3, applied
        # directly as `eye_pos = root_pos + viewOffset` -- see PlayerEyes.
        # position property in the game). It changes with crouch (~0.5),
        # prone (~0.3), and any pose transition, so we refresh it every
        # tick where we have a local BasePlayer. Failure keeps the last
        # known value.
        local_bp_for_eyes = pm_to_bp.get(local_pm) if local_pm else 0
        if local_bp_for_eyes:
            eye_off = self._read_local_eye_offset(local_bp_for_eyes)
            if eye_off is not None:
                self.local_eye_offset = eye_off

        # A skeleton sampled on tick N must still be valid on tick N+1, so the
        # window has to track the tick rate instead of being a constant that a
        # slow tick silently invalidates.
        bone_ttl = min(
            max(self.tick_ms / 1000.0 * BONE_REANCHOR_TICKS,
                BONE_REANCHOR_TTL_MIN),
            BONE_REANCHOR_TTL_MAX,
        )

        # [BONE-AGE]: how long the *sample* behind each drawn skeleton has been
        # frozen. [POSE-FREEZE] structurally cannot see this. The renderer
        # re-anchors the cached pose onto the live position every frame
        # (bones[i] = sample[i] + (pos - bone_anchor)), so the numbers keep
        # moving -- and the pose keeps sliding along with the player -- while
        # the pose itself has not been resampled in seconds. That is exactly
        # the reported "the bones freeze while someone runs, then snap back to
        # real time": a sliding stale stance, then the sample lands again.
        # Pair this with [BONE-DBG]'s short= / anchor_only= / norig= to say
        # *why* the sample stopped: a rig invalidation (norig, then a
        # BONE_RIG_RETRY_BASE..MAX backoff), or the anchor gate rejecting every
        # bone (anchor_only -- which deliberately takes no corrective action,
        # so it freezes until the rig comes back inside the radius).
        bone_age_max = 0.0
        bone_age_pm = 0
        bone_age_stale = 0
        bone_age_drawn = 0
        bone_age_sum = 0.0
        # Samples so old the renderer refuses them outright (age > bone_ttl):
        # the skeleton is simply not drawn, only the box. The first version of
        # this counter measured inside the `age <= bone_ttl` branch, so the
        # worst case -- the sample going fully stale -- was the one case it
        # could not see. Count it here, before the branch.
        bone_age_expired = 0
        bone_age_expired_max = 0.0
        display_players = []
        for pm_key, pos in pm_pos_cache.items():
            hp = max_hp = -1.0
            vital = health_by_pm.get(pm_key)
            if vital is not None:
                ls, hp, max_hp = vital
                if ls > 1 or max_hp < 1.0:
                    hp = max_hp = -1.0
            
            # Calculate distance to local player (if available) or camera
            dist = -1.0
            if local_pos is not None:
                dx = pos[0] - local_pos[0]
                dy = pos[1] - local_pos[1]
                dz = pos[2] - local_pos[2]
                dist = math.sqrt(dx*dx + dy*dy + dz*dz)
            elif cam_pos is not None:
                dx = pos[0] - cam_pos[0]
                dy = pos[1] - cam_pos[1]
                dz = pos[2] - cam_pos[2]
                dist = math.sqrt(dx*dx + dy*dy + dz*dz)

            bones = None
            bone_sample = self._bone_position_cache.get(pm_key)
            if bone_sample is not None:
                bone_anchor, sampled_bones, sampled_at = bone_sample
                bone_age = time.perf_counter() - sampled_at
                if bone_age > bone_ttl:
                    bone_age_expired += 1
                    if bone_age > bone_age_expired_max:
                        bone_age_expired_max = bone_age
                else:
                    bone_age_drawn += 1
                    bone_age_sum += bone_age
                    if bone_age > bone_age_max:
                        bone_age_max = bone_age
                        bone_age_pm = pm_key
                    if bone_age > BONE_AGE_STALE_S:
                        bone_age_stale += 1
                if bone_age <= bone_ttl:
                    delta = (
                        pos[0] - bone_anchor[0],
                        pos[1] - bone_anchor[1],
                        pos[2] - bone_anchor[2],
                    )
                    bones = {
                        bone_id: (
                            bone_pos[0] + delta[0],
                            bone_pos[1] + delta[1],
                            bone_pos[2] + delta[2],
                        )
                        for bone_id, bone_pos in sampled_bones.items()
                    }

            display_players.append({
                'pm': pm_key,
                'pos': pos,
                'hp': hp,
                'max_hp': max_hp,
                'dist': dist,
                'sleeping': sleeping_by_pm.get(pm_key, False),
                'bones': bones,
                'held_item': self._held_item_cache.get(pm_key, ''),
                'belt': self._belt_cache.get(pm_key, []),
                'wear': self._wear_cache.get(pm_key, []),
                'main': self._main_cache.get(pm_key, []),
                'name': self._player_name_cache.get(pm_key, ''),
                # None until the velocity probe latches a slot -- see the
                # 'vel' comment on the new_players append above. Needed here
                # too for aim_engine's projectile lead (bows/crossbow/nailgun).
                'vel': self._pm_vel_cache.get(pm_key),
            })

        with self._lock:
            if display_players:
                self.players = display_players
            # Never clear self.players — keep the last valid list on transient failures
            if vp:
                self.vp_matrix = vp
            if cam_pos:
                self.cam_pos = cam_pos
            if local_pos:
                self.local_pos = local_pos
            cache_tag = " cache" if (lc_cache_used or used_pm_cache) else ""
            skeleton_count = sum(
                1 for player in display_players if player.get('bones')
            )
            sleeping_count = sum(
                1 for player in display_players if player.get('sleeping')
            )
            self.diag = (
                f"LC={lc_count} tgt={len(display_players)} "
                f"bp={len(pm_to_bp)} sl={sleeping_count} "
                f"sk={skeleton_count}{cache_tag}"
            )

        # Scan held items (throttled at 0.5s interval)
        # Which scan actually paid, in calls. `disp=` alone cannot say whether
        # a 148 ms spike was held items, names or world entities, and at a
        # fixed ~14 ms per call only the call count is actionable.
        _io_disp0 = m.io_calls
        # Held items run on the slow lane (see _slow_lane_worker): the chain is
        # ~10 *dependent* round-trips, so it cannot be batched below ~11 IOCTLs
        # and was the last consistent source of 118-300 ms tick spikes. A held
        # weapon is decoration, not pose -- the tick must not wait for it.
        self._slow_lane_pm_to_bp = dict(pm_to_bp)
        _io_held = 0

        # Names run on the slow lane too. A name never changes for a
        # connected player, so it is decoration by definition -- and it was
        # the last recurring `disp=` spike, 88-133 ms every second.
        _io_names = 0

        # World entities run on the slow lane as well. This was the last
        # heavy scan left on the tick, and the most expensive of the three:
        # 21+ IOCTLs, i.e. a ~300 ms stall. Frame-differencing the user's
        # overlay capture measured the result directly -- 15 consecutive
        # identical frames, 250 ms of the overlay completely frozen, then a
        # large jump. Nothing on the critical path needs an ore node.
        _io_we = 0
        self._scan_world_entities(on_worker=True)
        # Unconditional: a quiet tick must report (0,0,0) and print nothing,
        # not keep showing the previous spike's attribution. With all three
        # scans off the tick this is now (0,0,0) every tick, and `slow=` is
        # where the driver cost shows up.
        self._last_disp_io = (_io_held, _io_names, _io_we)

        # Take a coherent snapshot for the render thread (like esp.py)
        self._take_snapshot()

        t_display = (time.perf_counter() - t0_disp) * 1000
        t_total = (time.perf_counter() - t_start) * 1000
        latency_now = time.perf_counter()
        if t_total > self._tick_worst_ms:
            self._tick_worst_ms = t_total
        # [TICK-LATENCY] prints at most once per TICK_LOG_INTERVAL, i.e. it
        # samples ONE arbitrary tick per second out of ~120. A stall that lands
        # between two prints is invisible: the 2026-09-05 capture showed every
        # printed line at total=5-9ms while self.tick_ms reached 312 ms (read
        # off [BONE-DBG]'s anchor limit=12.31m, which is 4.5 + tick_ms/1000*25,
        # and confirmed by [POSE-FREEZE] max=305.9ms in the same window). A
        # ~300 ms tick freezes the box AND the skeleton together, then snaps --
        # which is what the freeze actually looks like. So a slow tick now
        # prints on its own, tagged, bypassing the cadence.
        slow_tick = t_total > TICK_SLOW_MS
        cadence_due = (
            t_total > 5.0
            and latency_now - self._last_tick_latency_log_at >= TICK_LOG_INTERVAL
        )
        slow_due = slow_tick and latency_now >= self._next_slow_tick_log_at
        if cadence_due or slow_due:
            if slow_due:
                self._next_slow_tick_log_at = (
                    latency_now + TICK_SLOW_LOG_INTERVAL
                )
            io_ms = m.io_wait_s * 1000.0
            yield_ms = m.yield_wait_s * 1000.0
            lock_ms = m.lock_wait_s * 1000.0
            print(
                f"[TICK-LATENCY]{' SLOW' if slow_tick else ''} "
                f"total={t_total:.1f}ms (vp={t_vp:.1f}ms, "
                f"chain={t_chain:.1f}ms, pm_list={t_pm_list:.1f}ms, "
                f"filter={t_filter:.1f}ms, mapping={t_mapping:.1f}ms, "
                f"pos={t_pos_batch:.1f}ms, disp={t_display:.1f}ms{self._format_disp_io()}) "
                f"| io={m.io_calls} calls ({m.io_slow_calls} slow, "
                f"spin={getattr(m, 'spin_budget', 0.0) * 1000.0:.2f}ms) "
                f"{io_ms:.1f}ms "
                f"({io_ms / t_total * 100.0 if t_total else 0.0:.0f}% of tick, "
                f"{io_ms / m.io_calls if m.io_calls else 0.0:.2f}ms each) "
                f"| yield={yield_ms:.1f}ms lock={lock_ms:.1f}ms "
                f"| worst={self._tick_worst_ms:.1f}ms since last report",
                flush=True,
            )
        if cadence_due:
            self._last_tick_latency_log_at = latency_now
            self._tick_worst_ms = 0.0
            # [TICK-LATENCY] itself only prints once per TICK_LOG_INTERVAL
            # (~1s), but real ticks run every ~90ms underneath -- one printed
            # sample's chain=/pm_list= is not evidence of what most real
            # ticks paid. This is the true hit rate across every real tick
            # since the last print.
            print(
                f"[REFRESH-HITRATE] chain={self._hit_window_chain_hits}/"
                f"{self._hit_window_chain_total} "
                f"pm_list={self._hit_window_pm_list_hits}/"
                f"{self._hit_window_pm_list_total}",
                flush=True,
            )
            self._hit_window_chain_hits = 0
            self._hit_window_chain_total = 0
            self._hit_window_pm_list_hits = 0
            self._hit_window_pm_list_total = 0

        # Heartbeat, not an alarm. The first version only spoke when it already
        # believed something was wrong (worst > BONE_AGE_STALE_S), so a run
        # where it never fired proved nothing to anyone reading the log -- the
        # same mistake [HELD-DBG] made. Print it on a fixed cadence so there is
        # a baseline to compare a freeze against: healthy is a worst= in the
        # single-digit milliseconds with expired=0.
        now_age = time.perf_counter()
        if (
            (bone_age_drawn or bone_age_expired)
            and now_age >= self._next_bone_age_log_at
        ):
            self._next_bone_age_log_at = now_age + BONE_AGE_LOG_INTERVAL
            mean_ms = (
                bone_age_sum / bone_age_drawn * 1000.0 if bone_age_drawn else 0.0
            )
            print(
                f"[BONE-AGE] worst={bone_age_max * 1000:.0f}ms "
                f"mean={mean_ms:.0f}ms pm=0x{bone_age_pm:X} "
                f"stale={bone_age_stale}/{bone_age_drawn} "
                f"expired={bone_age_expired}"
                f"{f'(worst {bone_age_expired_max * 1000:.0f}ms)' if bone_age_expired else ''} "
                f"ttl={bone_ttl * 1000:.0f}ms",
                flush=True,
            )

        self._debug_players(
            f"LC={lc_count} pm={len(pm_ptrs)} {_vpl_diag} "
            f"posA={pathA_hits} posB={pathB_hits} posC={pathC_hits} "
            f"drop[pos={skipped_pos} hp={skipped_hp}] "
            f"cache={len(pm_pos_cache)} disp={len(display_players)}"
            # Same reason the LC-count line above dropped its vp=: the tick
            # never holds the matrix (the render loop owns it), so this could
            # only ever print "miss".
        )
        if sample_rows:
            self._debug_sample("samples " + " | ".join(sample_rows))


# ---------------------------------------------------------------------------
# Main / Render
# ---------------------------------------------------------------------------
SW, SH = 1920, 1080
ESP_WINDOW_FLAGS = 0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x20 | 0x80


def get_screen_size():
    try:
        user32 = ctypes.windll.user32
        w = int(user32.GetSystemMetrics(0))
        h = int(user32.GetSystemMetrics(1))
        if w > 0 and h > 0:
            return w, h
    except Exception:
        pass
    return SW, SH


def get_monitor_refresh_rate(default=144):
    try:
        monitor = glfw.get_primary_monitor()
        mode = glfw.get_video_mode(monitor) if monitor else None
        refresh_rate = int(getattr(mode, "refresh_rate", 0)) if mode else 0
        if 30 <= refresh_rate <= 500:
            return refresh_rate
    except Exception:
        pass
    return default


def imgui_set_next_window_pos(pos):
    fn = getattr(imgui, "set_next_window_pos", None)
    if fn is None:
        fn = imgui.set_next_window_position
    fn(pos)


def imgui_set_next_window_size(size):
    fn = getattr(imgui, "set_next_window_size", None)
    if fn is None:
        raise AttributeError("imgui has no set_next_window_size")
    fn(size)


def _imgui_text_size(text):
    """Return an ImGui text extent as plain floats."""
    size = imgui.calc_text_size(str(text))
    try:
        return float(size.x), float(size.y)
    except AttributeError:
        return float(size[0]), float(size[1])


def _draw_outlined_text(draw_list, x, y, color, text, outline=0xE8000000):
    """Small readable HUD text with a crisp one-pixel dark keyline."""
    text = str(text)
    for dx, dy in ((-1.0, 0.0), (1.0, 0.0), (0.0, -1.0), (0.0, 1.0)):
        draw_list.add_text((x + dx, y + dy), outline, text)
    draw_list.add_text((x, y), color, text)


def _draw_centered_outlined_text(draw_list, center_x, y, color, text):
    width, _ = _imgui_text_size(text)
    _draw_outlined_text(draw_list, center_x - width * 0.5, y, color, text)


def _draw_corner_box(draw_list, left, top, right, bottom, color):
    """Draw compact tactical corner brackets around a target."""
    width = max(0.0, right - left)
    height = max(0.0, bottom - top)
    corner = max(6.0, min(18.0, min(width * 0.28, height * 0.16)))
    segments = (
        ((left, top), (left + corner, top)),
        ((left, top), (left, top + corner)),
        ((right, top), (right - corner, top)),
        ((right, top), (right, top + corner)),
        ((left, bottom), (left + corner, bottom)),
        ((left, bottom), (left, bottom - corner)),
        ((right, bottom), (right - corner, bottom)),
        ((right, bottom), (right, bottom - corner)),
    )

    # A dark under-stroke keeps the cyan readable against bright terrain.
    for p1, p2 in segments:
        draw_list.add_line(p1, p2, 0xD8000000, 3.4)
    for p1, p2 in segments:
        draw_list.add_line(p1, p2, color, 1.45)


def _draw_player_skeleton(draw_list, bones, vp, sw, sh, color):
    """Project and draw one coherent, already body-anchored SCI skeleton."""
    if not bones:
        return False

    screen_bones = {}
    for bone_id, world_pos in bones.items():
        screen_pos = w2s(world_pos, vp, sw, sh)
        if screen_pos is not None:
            screen_bones[bone_id] = screen_pos

    drawn = False
    for first_id, second_id in SCI_BONE_LINKS:
        first = screen_bones.get(first_id)
        second = screen_bones.get(second_id)
        if first is None or second is None:
            continue
        dx = first[0] - second[0]
        dy = first[1] - second[1]
        if dx * dx + dy * dy > 250000.0:
            continue
        draw_list.add_line(first, second, 0xB8000000, 3.0)
        draw_list.add_line(first, second, color, 1.25)
        drawn = True

    head = screen_bones.get(53)
    if head is not None:
        draw_list.add_circle_filled(head, 3.3, 0xC8000000, 12)
        draw_list.add_circle_filled(head, 1.8, color, 12)
        drawn = True
    return drawn


def main():
    global SW, SH
    winmm = None
    timer_period_set = False

    def cleanup_timer():
        if timer_period_set and winmm is not None:
            try:
                winmm.timeEndPeriod(1)
            except Exception:
                pass

    try:
        winmm = ctypes.windll.winmm
        winmm.timeBeginPeriod(1)
        timer_period_set = True
        print("[+] Resolvant scheduler Windows mis à 1ms")
    except:
        pass

    m = Mem()
    if not m.connect():
        cleanup_timer()
        print("[!] Driver non connecté — vérifie que le driver est chargé")
        return
    if not m.init_event():
        cleanup_timer()
        print("[!] Impossible d'ouvrir l'event de commande du driver")
        return
    if not m.attach(timeout=120.0):
        cleanup_timer()
        print("[!] RustClient.exe non trouvé après 2 minutes")
        return
    m.find_base()
    print(f"[+] Base: 0x{m.base:X}  PID: {m.pid}")

    ga_base, up_base = find_modules(m)
    print(f"[+] GameAssembly.dll: 0x{ga_base:X}")
    print(f"[+] UnityPlayer.dll:  0x{up_base:X}")

    if not ga_base:
        cleanup_timer()
        print_module_diagnostics(m)
        print("[!] GameAssembly.dll introuvable — abandon")
        return

    if GUI_IMPORT_ERROR is not None:
        cleanup_timer()
        print("[!] imgui-bundle ou glfw manquant.")
        print(f"    {GUI_IMPORT_ERROR}")
        return

    g = RustGame(m, ga_base)

    if not glfw.init():
        cleanup_timer()
        return
    SW, SH = get_screen_size()
    glfw.window_hint(glfw.DECORATED, 0)
    glfw.window_hint(glfw.TRANSPARENT_FRAMEBUFFER, 1)
    glfw.window_hint(glfw.FLOATING, 1)

    win = glfw.create_window(SW, SH, "Rust ESP", None, None)
    if not win:
        glfw.terminate()
        cleanup_timer()
        return
    glfw.set_window_pos(win, 0, 0)
    glfw.make_context_current(win)

    # Present on the monitor cadence. Unsynchronised 144 Hz pacing on a
    # different refresh rate produced uneven frame delivery in the recording.
    monitor_refresh_hz = get_monitor_refresh_rate()
    glfw.swap_interval(1)

    # Make the overlay click-through (WS_EX_TRANSPARENT | WS_EX_LAYERED)
    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x80000
    WS_EX_TRANSPARENT = 0x20
    user32 = ctypes.windll.user32
    hwnd = glfw.get_win32_window(win)
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)

    imgui.create_context()
    impl = GlfwRenderer(win)

    print("[+] Rust ESP prêt")
    g.start_worker()
    camera_sampler = CameraSampler(m, ga_base, CAMERA_SAMPLE_HZ)
    camera_sampler.start()

    frame = 0
    last_diag = time.perf_counter()
    last_render_console_at = last_diag
    fps_acc = 0
    fps_disp = 0.0
    target_render_dt = 1.0 / monitor_refresh_hz
    next_frame_time = time.perf_counter()
    print(f"[+] Rendu synchronisé à {monitor_refresh_hz} Hz")

    COL_ACCENT = 0xFFFFD400  # RGB #00D4FF, packed as ImGui ABGR
    COL_SKELETON = 0xE8FFD400
    COL_WHITE  = 0xFFFFFFFF

    # Render-side VP cache: keep drawing with last good VP when reads fail
    last_valid_vp = None
    last_valid_cam_pos = None
    last_valid_vp_at = 0.0

    while not glfw.window_should_close(win):
        glfw.poll_events()
        impl.process_inputs()

        # Get coherent snapshot from worker thread (same pattern as esp.py)
        players, _, diag, tick = g.get_snapshot()

        # Retrieve local player position from worker thread under lock
        with g._lock:
            local_pos = getattr(g, 'local_pos', None)

        # Consume the newest camera sample without waiting on the driver. The
        # OpenGL/ImGui loop can now run at the monitor cadence independently.
        camera_sample = camera_sampler.get_latest()
        if camera_sample is not None:
            vp, cam_pos, camera_sampled_at = camera_sample
            last_valid_vp = vp
            last_valid_cam_pos = cam_pos
            last_valid_vp_at = camera_sampled_at
        else:
            vp = last_valid_vp
            cam_pos = last_valid_cam_pos

        frame += 1
        fps_acc += 1
        now = time.perf_counter()
        if now - last_diag >= 1.0:
            fps_disp = fps_acc / (now - last_diag)
            fps_acc = 0
            last_diag = now

        # --- Render ---
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        imgui.new_frame()
        imgui.set_next_window_pos((0, 0))
        imgui.set_next_window_size((SW, SH))
        imgui.push_style_color(imgui.Col_.window_bg, (0, 0, 0, 0))

        with imgui_ctx.begin("##esp", None, 0x7F):
            dl = imgui.get_window_draw_list()

            dl.add_text((10, 10), COL_WHITE,
                        f"Rust ESP | {fps_disp:.0f}fps | tick={tick:.0f}ms | {diag}")

            n_drawn = 0
            n_behind = 0
            if vp and players:
                for p in players:
                    pos = p['pos']
                    if p.get('sleeping', False):
                        box = calculate_sleeping_box(pos, vp, SW, SH)
                    else:
                        box = calculate_box(pos, vp, SW, SH)
                    if box is None:
                        n_behind += 1
                        continue
                    n_drawn += 1

                    left, top, right, bottom = box

                    # Distance is resolved before drawing so all HUD elements
                    # are laid out as one centered visual unit.
                    dist = -1.0
                    if local_pos:
                        dx = pos[0] - local_pos[0]
                        dy = pos[1] - local_pos[1]
                        dz = pos[2] - local_pos[2]
                        dist = math.sqrt(dx*dx + dy*dy + dz*dz)
                    elif cam_pos:
                        dx = pos[0] - cam_pos[0]
                        dy = pos[1] - cam_pos[1]
                        dz = pos[2] - cam_pos[2]
                        dist = math.sqrt(dx*dx + dy*dy + dz*dz)
                    else:
                        dist = p.get('dist', -1.0)

                    center_x = (left + right) * 0.5

                    _draw_corner_box(dl, left, top, right, bottom, COL_ACCENT)
                    if 0.0 <= dist <= SKELETON_MAX_DISTANCE:
                        _draw_player_skeleton(
                            dl,
                            p.get('bones'),
                            vp,
                            SW,
                            SH,
                            COL_SKELETON,
                        )

                    if dist >= 0.0:
                        distance_y = (
                            bottom + 4.0
                            if bottom <= SH - 20.0
                            else bottom - 16.0
                        )
                        _draw_centered_outlined_text(
                            dl, center_x, distance_y, COL_WHITE, f"{dist:.0f}m"
                        )

            # Render diagnostic on overlay
            cam_age_ms = (
                (now - last_valid_vp_at) * 1000.0
                if last_valid_vp_at > 0.0
                else -1.0
            )
            dl.add_text((10, 28), COL_WHITE,
                        f"drawn={n_drawn} behind={n_behind} pl={len(players) if players else 0} "
                        f"vp={'yes' if vp else 'NO'} cam={cam_age_ms:.0f}ms")

            # Print render diagnostic to console once per second
            if now - last_render_console_at >= 1.0:
                last_render_console_at = now
                vp_str = "None"
                if vp:
                    vp_str = f"[{vp[0]:.3f},{vp[5]:.3f},{vp[10]:.3f},{vp[15]:.3f}]"
                print(f"[RENDER] drawn={n_drawn} behind={n_behind} pl={len(players) if players else 0} vp={vp_str}", flush=True)

        imgui.pop_style_color()
        imgui.render()
        impl.render(imgui.get_draw_data())
        glfw.swap_buffers(win)

        # Precise frame pacing (same as esp.py)
        next_frame_time += target_render_dt
        sleep_for = next_frame_time - time.perf_counter()
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            next_frame_time = time.perf_counter()

    camera_sampler.stop()
    glfw.terminate()
    cleanup_timer()


if __name__ == "__main__":
    main()
