"""Auto-repair: find current Rust class RVAs by scanning Il2Cpp class names.

Why this is robust:
  - Class names ("BaseNetworkable", "MainCamera", "BasePlayer", ...) NEVER
    change between builds. Only their RVA slots in .data move.
  - The compiler always emits `MOV r64,[RIP+rel32]` when reading a class
    global, so we scan the code section for those instructions targeting
    .data, then verify each candidate slot by reading the Il2CppClass name.

Two data sources:
  - Disk (GameAssembly.dll file): fast, chunked local reads to build the
    candidate list. Only used to narrow the search; verification always
    happens against live memory when a driver is present.
  - Live memory: chunked reads through the driver (falls back to a plain
    `read()` provider so the whole algorithm can be unit-tested offline).

Integration: `repair_offsets(runtime)` patches `OFF` in memory and resets
the runtime chain caches, so the ESP keeps working after a Rust update
without any manual dump.
"""

import os
import struct
import threading
import time

try:
    from . import legacy_runtime as legacy
except ImportError:  # pragma: no cover - direct execution
    import legacy_runtime as legacy


# Il2CppClass layout (Unity 2022/6 x64, stable across builds)
KLASS_NAME_OFF = 0x10
KLASS_PARENT_OFF = 0x58
KLASS_STATIC_FIELDS_OFF = 0xB8

# Class names we need to resolve → OFF attribute to patch.
# LocalPlayer is obfuscated (name is a hash) and is *optional* at runtime
# (`_get_bp_context` degrades gracefully), so it is intentionally absent.
TARGET_CLASSES = (
    ("BaseNetworkable", "BaseNetworkable_c"),
    ("MainCamera", "MainCamera_c"),
    ("BasePlayer", "BasePlayer_c"),
    ("ListComponent_PlayerModel", "ListComponent_PlayerModel_c"),
)

MAX_USER_PTR = 0x7FFFFFFFFFFF
CODE_SCAN_CHUNK = 0x80000        # 512 KB per code-section read
VALID_RANGE = (0x10000, MAX_USER_PTR)


def _valid_ptr(v):
    return isinstance(v, int) and VALID_RANGE[0] < v < VALID_RANGE[1]


def _first8(name):
    """First 8 bytes of a class name as a little-endian u64 for pre-filtering."""
    raw = name.encode("ascii")[:8].ljust(8, b"\x00")
    return struct.unpack("<Q", raw)[0]


_FIRST8_FILTER = {_first8(name) for name, _ in TARGET_CLASSES}


# ---------------------------------------------------------------------------
# PE parsing (from a memory provider or from disk bytes)
# ---------------------------------------------------------------------------

def _parse_pe_sections(header):
    """Parse section table from the first 0x1000 bytes of a PE image.

    Returns (image_base, [{'name','va','raw_sz','fo'}...]) or (0, []).
    """
    if len(header) < 0x100 or header[:2] != b"MZ":
        return 0, []
    e_lfanew = struct.unpack_from("<I", header, 0x3C)[0]
    if not (0x40 <= e_lfanew <= 0x2000) or header[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
        return 0, []
    num_sects = struct.unpack_from("<H", header, e_lfanew + 6)[0]
    opt_size = struct.unpack_from("<H", header, e_lfanew + 20)[0]
    opt = e_lfanew + 24
    if len(header) < opt + opt_size + num_sects * 40:
        return 0, []
    magic = struct.unpack_from("<H", header, opt)[0]
    if magic == 0x20B:
        image_base = struct.unpack_from("<Q", header, opt + 24)[0]
    elif magic == 0x10B:
        image_base = struct.unpack_from("<I", header, opt + 28)[0]
    else:
        return 0, []
    so = opt + opt_size
    sects = []
    for i in range(num_sects):
        o = so + i * 40
        if o + 40 > len(header):
            break
        name = header[o:o + 8].rstrip(b"\x00").decode("ascii", "replace")
        sects.append({
            "name": name,
            "va": struct.unpack_from("<I", header, o + 12)[0],
            "fo": struct.unpack_from("<I", header, o + 20)[0],
            "raw_sz": struct.unpack_from("<I", header, o + 16)[0],
        })
    return image_base, sects


def _pick_sections(sects):
    """Choose the code section to scan and the data sections to target.

    Class globals live in .data on some builds and .rdata on others; accept
    both as valid MOV [RIP+rel32] targets.
    """
    code = next((s for s in sects if s["name"] in (".il2cpp", ".text")), None)
    data_sects = [s for s in sects if s["name"] in (".data", ".rdata")]
    return code, data_sects


# ---------------------------------------------------------------------------
# Candidate discovery
# ---------------------------------------------------------------------------

# REX.W (48/4C) + 8B (MOV) + ModRM mod=00 rm=101 (RIP-relative) + rel32
_MOV_RIP_MODRM = b"\x05\x0D\x15\x1D\x25\x2D\x35\x3D"


def _scan_code_for_data_refs(code_bytes, code_va, data_lo, data_hi):
    """Return sorted unique RVAs in .data referenced by MOV r64,[RIP+rel32].

    code_bytes is a chunk of the code section starting at VA `code_va`.
    """
    out = []
    i = 0
    n = len(code_bytes)
    while i < n - 7:
        b0 = code_bytes[i]
        if b0 in (0x48, 0x4C) and code_bytes[i + 1] == 0x8B and code_bytes[i + 2] in _MOV_RIP_MODRM:
            rel = struct.unpack_from("<i", code_bytes, i + 3)[0]
            next_va = code_va + i + 7
            target = (next_va + rel) & 0xFFFFFFFF
            if data_lo <= target < data_hi:
                out.append(target)
            i += 1
            continue
        i += 1
    return out


def _collect_candidates_from_disk(dll_path):
    """Scan GameAssembly.dll on disk and return (image_base, sects, rva_set)."""
    with open(dll_path, "rb") as f:
        header = f.read(0x1000)
    image_base, sects = _parse_pe_sections(header)
    code, data_sects = _pick_sections(sects)
    if not code or not data_sects:
        return image_base, sects, set()
    data_lo = min(s["va"] for s in data_sects)
    data_hi = max(s["va"] + s["raw_sz"] for s in data_sects)
    found = set()
    with open(dll_path, "rb") as f:
        f.seek(code["fo"])
        remaining = code["raw_sz"]
        va = code["va"]
        while remaining > 0:
            chunk = f.read(min(CODE_SCAN_CHUNK, remaining))
            if not chunk:
                break
            found.update(_scan_code_for_data_refs(chunk, va, data_lo, data_hi))
            va += len(chunk)
            remaining -= len(chunk)
    return image_base, sects, found


def _collect_candidates_from_memory(m, ga_base, sects):
    """Scan the code section through the memory provider."""
    code, data_sects = _pick_sections(sects)
    if not code or not data_sects:
        return set()
    data_lo = min(s["va"] for s in data_sects)
    data_hi = max(s["va"] + s["raw_sz"] for s in data_sects)
    found = set()
    va = code["va"]
    remaining = code["raw_sz"]
    while remaining > 0:
        take = min(CODE_SCAN_CHUNK, remaining)
        chunk = m.read(ga_base + va, take)
        if not chunk or len(chunk) < take:
            break
        found.update(_scan_code_for_data_refs(chunk, va, data_lo, data_hi))
        va += take
        remaining -= take
    return found


# ---------------------------------------------------------------------------
# Live verification (batch reads → name match)
# ---------------------------------------------------------------------------

def _verify_candidates(m, ga_base, rvas, names=None):
    """Given candidate slot RVAs, return {class_name: slot_rva} that match.

    Batches pointer + name-pointer + first-8-name reads through the driver
    to keep IOCTL count low (~3 batches regardless of candidate count).

    `names` overrides the class names to look for (default: TARGET_CLASSES),
    so the same scan-and-verify can resolve a one-off class such as
    CursorManager without touching the auto-repair target list.
    """
    if not rvas:
        return {}
    if names is None:
        names = tuple(n for n, _ in TARGET_CLASSES)
        first8 = _FIRST8_FILTER
    else:
        names = tuple(names)
        first8 = {_first8(n) for n in names}
    addrs = [ga_base + rva for rva in rvas]
    ptrs = m.batch_u64(addrs, attempts=2)
    valid = [(rva, ptr) for rva, ptr in zip(rvas, ptrs) if _valid_ptr(ptr)]
    if not valid:
        return {}

    name_ptr_addrs = [ptr + KLASS_NAME_OFF for _, ptr in valid]
    name_ptrs = m.batch_u64(name_ptr_addrs, attempts=2)
    keep = [
        (rva, ptr, nptr)
        for (rva, ptr), nptr in zip(valid, name_ptrs)
        if _valid_ptr(nptr)
    ]
    if not keep:
        return {}

    # Read the first 8 bytes of each name as RAW STRING data (not a pointer),
    # so providers that relocate pointer values must not touch it.
    filtered = []
    for rva, nptr in [(rva, nptr) for rva, _, nptr in keep]:
        raw8 = m.read(nptr, 8)
        if len(raw8) == 8 and struct.unpack("<Q", raw8)[0] in first8:
            filtered.append((rva, nptr))
    if not filtered:
        return {}

    found = {}
    for rva, nptr in filtered:
        name = _read_cstring(m, nptr, 64)
        for cls_name in names:
            if cls_name not in found and name == cls_name:
                found[cls_name] = rva
    return found


def _read_cstring(m, addr, max_len):
    raw = m.read(addr, max_len)
    if not raw:
        return ""
    end = raw.find(b"\x00")
    if end >= 0:
        raw = raw[:end]
    try:
        return raw.decode("ascii", errors="ignore")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_class_rvas(m, ga_base, dll_path=None, progress=None, names=None):
    """Return {class_name: slot_rva} for all TARGET_CLASSES found.

    Uses the disk file (if given) to build candidates quickly, then verifies
    against the live memory provider `m`. If no disk path, scans memory.

    `names` narrows (or replaces) the class names to look for; it is what
    lets a one-off lookup such as CursorManager reuse this scan without
    joining the auto-repair target list.
    """
    want = tuple(names) if names else tuple(n for n, _ in TARGET_CLASSES)
    if progress:
        progress("[auto] PE header")
    header = m.read(ga_base, 0x1000)
    image_base, sects = _parse_pe_sections(header)
    if not sects:
        # Some providers can't read the first page (image base 0); fall back
        # to reading through disk only.
        if dll_path and os.path.exists(dll_path):
            with open(dll_path, "rb") as f:
                header = f.read(0x1000)
            image_base, sects = _parse_pe_sections(header)
    if not sects:
        if progress:
            progress("[auto] PE header unreadable")
        return {}

    if dll_path and os.path.exists(dll_path):
        if progress:
            progress("[auto] scanning disk")
        _, _, rvas = _collect_candidates_from_disk(dll_path)
    else:
        if progress:
            progress("[auto] scanning memory")
        rvas = _collect_candidates_from_memory(m, ga_base, sects)
    # The base class's own global may be reached via a *mov* that loads a
    # pointer to the slot; also accept the parent-chain fallback later.
    # BaseNetworkable is sometimes referenced with a static-fields accessor
    # (mov rax,[rip]; mov rax,[rax+sf]) — its first load still lands in the
    # data range, so it is already covered above.
    if not rvas:
        if progress:
            progress("[auto] no code candidates")
        return {}
    if progress:
        progress(f"[auto] {len(rvas)} candidates, verifying")

    found = _verify_candidates(m, ga_base, list(rvas), names=want)
    if progress:
        progress(f"[auto] found {len(found)}/{len(want)}")
    return found


def apply_to_offsets(found):
    """Patch legacy.OFF with the resolved slot RVAs. Returns # patched."""
    patched = 0
    for cls_name, attr in TARGET_CLASSES:
        rva = found.get(cls_name)
        if rva:
            setattr(legacy.OFF, attr, rva)
            patched += 1
    return patched


# ---------------------------------------------------------------------------
# Runtime integration: repair the running RustGame instance
# ---------------------------------------------------------------------------

def repair_runtime(runtime, dll_path=None, progress=None):
    """Resolve class RVAs and rewire a RustGame instance after an update.

    Patches OFF, resets every cached chain pointer so the next tick
    re-resolves from the new class RVAs. Returns True on success.
    """
    m = getattr(runtime, "m", None)
    ga = getattr(runtime, "ga", 0)
    if m is None or not ga:
        return False

    found = resolve_class_rvas(m, ga, dll_path=dll_path, progress=progress)
    if not found:
        return False

    patched = apply_to_offsets(found)
    if patched == 0:
        return False

    # Drop all cached chain pointers so they get rebuilt from new RVAs.
    for key in (
        "_cached_arr", "_cached_blist", "_cached_list_dict",
        "_cached_lc_klass", "_cached_lc_sf", "_cached_lc_wrapper",
        "_cached_lc_pm_list", "_cached_lc_buf_arr", "_cached_lc_count",
        "_cached_bp_sf", "_cached_local_player",
    ):
        if hasattr(runtime, key):
            setattr(runtime, key, None if key.startswith("_cached_") and key not in ("_cached_lc_count",) else 0)
    runtime._lc_chain_cache_at = 0.0
    runtime._next_lc_refresh_at = 0.0
    runtime._static_candidate = None

    # Camera chain cache is module-global; force re-resolution.
    legacy._cam_native_cache = 0
    legacy._cam_chain_miss = 0

    return True


class AutoRepairer:
    """One-shot async repair with a failure throttle.

    - `maybe_trigger()` should be called from the worker tick when the
      entity chain is failing.
    - Spawns a daemon thread so the hot path is never blocked.
    - Retries at most every `retry_interval` seconds.
    """

    def __init__(self, runtime, dll_path=None, retry_interval=20.0):
        self._runtime = runtime
        self._dll_path = dll_path
        self._retry_interval = retry_interval
        self._lock = threading.Lock()
        self._busy = False
        self._last_attempt = 0.0
        self._success = False

    @property
    def success(self):
        return self._success

    def maybe_trigger(self):
        if self._success:
            return False
        now = time.perf_counter()
        with self._lock:
            if self._busy or now - self._last_attempt < self._retry_interval:
                return False
            self._busy = True
            self._last_attempt = now
        threading.Thread(
            target=self._run,
            name="esp-auto-repair",
            daemon=True,
        ).start()
        return True

    def _run(self):
        try:
            ok = repair_runtime(self._runtime, dll_path=self._dll_path, progress=print)
            self._success = ok
            if ok:
                print(f"[auto-repair] OK: patched {len(legacy.OFF.__dict__)} OFF attrs → chain will re-resolve", flush=True)
        except Exception as exc:  # pragma: no cover
            print(f"[auto-repair] failed: {type(exc).__name__}: {exc}", flush=True)
        finally:
            with self._lock:
                self._busy = False


def find_gameassembly_path():
    """Locate GameAssembly.dll on common install paths (used by auto-dump too)."""
    drives = ["F:", "C:", "D:", "E:", "G:"]
    rels = (
        r"\SteamLibrary\steamapps\common\Rust\GameAssembly.dll",
        r"\Program Files (x86)\Steam\steamapps\common\Rust\GameAssembly.dll",
    )
    for drive in drives:
        for rel in rels:
            p = os.path.join(drive, rel.lstrip("\\"))
            if os.path.exists(p):
                return p
    return None
