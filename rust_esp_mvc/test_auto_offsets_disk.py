"""Offline tests for auto_offsets.

1. Candidate discovery on the real GameAssembly.dll: the disk scan for
   `MOV r64,[RIP+rel32] → .data` must return the KNOWN class slot RVAs
   (BaseNetworkable_c, MainCamera_c, BasePlayer_c, ListComponent_PlayerModel_c).

2. Name verification logic against a synthetic live-memory model, because the
   slot VALUES on disk are anti-tamper obfuscated (the game decrypts them at
   runtime). This proves the full algorithm works when the driver is live.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rust_esp_mvc import auto_offsets
from rust_esp_mvc import legacy_runtime as legacy

DLL = r"F:\SteamLibrary\steamapps\common\Rust\GameAssembly.dll"
GA_BASE = 0x180000000


def parse_pe_imagebase(header):
    e = struct.unpack_from("<I", header, 0x3C)[0]
    num_s = struct.unpack_from("<H", header, e + 6)[0]
    opt_size = struct.unpack_from("<H", header, e + 20)[0]
    opt = e + 24
    so = opt + opt_size
    magic = struct.unpack_from("<H", header, opt)[0]
    image_base = (
        struct.unpack_from("<Q", header, opt + 24)[0]
        if magic == 0x20B
        else struct.unpack_from("<I", header, opt + 28)[0]
    )
    sects = []
    for i in range(num_s):
        o = so + i * 40
        nm = header[o:o + 8].rstrip(b"\x00").decode("ascii", "replace")
        sects.append({
            "name": nm,
            "va": struct.unpack_from("<I", header, o + 12)[0],
            "fo": struct.unpack_from("<I", header, o + 20)[0],
            "raw_sz": struct.unpack_from("<I", header, o + 16)[0],
        })
    return image_base, sects


class FakeMem:
    """Raw byte provider mapping ga_base+addr → file offsets (no relocation)."""

    def __init__(self, dll_path, ga_base):
        self._f = open(dll_path, "rb")
        self._ga_base = ga_base
        self._f.seek(0)
        header = self._f.read(0x1000)
        self._image_base, self._sects = parse_pe_imagebase(header)

    def close(self):
        self._f.close()

    def _rva_to_fo(self, rva):
        for s in self._sects:
            if s["va"] <= rva < s["va"] + s["raw_sz"]:
                return s["fo"] + (rva - s["va"])
        return None

    def read(self, addr, size):
        rva = addr - self._ga_base
        fo = self._rva_to_fo(rva)
        if fo is None:
            return b"\x00" * size
        self._f.seek(fo)
        return self._f.read(size)

    def batch_u64(self, addrs, attempts=1):
        out = []
        for a in addrs:
            raw = self.read(a, 8)
            out.append(struct.unpack("<Q", raw)[0] if len(raw) == 8 else 0)
        return out


def test_candidate_discovery():
    """The disk scan must return the 4 known class slot RVAs."""
    if not os.path.exists(DLL):
        print("[SKIP] GameAssembly.dll not found")
        return True

    with open(DLL, "rb") as f:
        header = f.read(0x1000)
    _, sects = parse_pe_imagebase(header)
    _, _, rvas = auto_offsets._collect_candidates_from_disk(DLL)

    expected = {
        "BaseNetworkable": legacy.OFF.BaseNetworkable_c,
        "MainCamera": legacy.OFF.MainCamera_c,
        "BasePlayer": legacy.OFF.BasePlayer_c,
        "ListComponent_PlayerModel": legacy.OFF.ListComponent_PlayerModel_c,
    }
    print(f"[*] {len(rvas)} candidate RVAs from disk scan")
    ok = True
    for name, rva in expected.items():
        found = rva in rvas
        ok = ok and found
        print(f"    {name:<26} slot 0x{rva:X}  {'FOUND' if found else 'MISSING'}")
    return ok


# ---------------------------------------------------------------------------
# Synthetic live-memory model
# ---------------------------------------------------------------------------

class LiveMem:
    """Simulate live memory: slots hold class pointers; names are readable."""

    def __init__(self, ga_base, slots):
        self._ga_base = ga_base
        self._slots = slots          # {slot_rva: class_ptr}
        self._classes = {}           # {class_ptr: name}
        self._ptr = ga_base + 0x200000

    def _next_ptr(self):
        p = self._ptr
        self._ptr += 0x100
        return p

    def add_class(self, name, slot_rva):
        cls = self._next_ptr()
        self._slots[slot_rva] = cls
        self._classes[cls] = name
        return cls

    def read(self, addr, size):
        rva = addr - self._ga_base
        if rva in self._classes:
            name = self._classes[rva]
            raw = name.encode("ascii") + b"\x00" * 64
            return raw[:size]
        return b"\x00" * size

    def batch_u64(self, addrs, attempts=1):
        out = []
        for a in addrs:
            rva = a - self._ga_base
            out.append(self._slots.get(rva, 0))
        return out


def test_name_verification():
    """With a live model containing the 4 classes, the full pipeline resolves."""
    mem = LiveMem(GA_BASE, {})
    slot_rvas = [
        legacy.OFF.BaseNetworkable_c,
        legacy.OFF.MainCamera_c,
        legacy.OFF.BasePlayer_c,
        legacy.OFF.ListComponent_PlayerModel_c,
    ]
    names = ["BaseNetworkable", "MainCamera", "BasePlayer", "ListComponent_PlayerModel"]
    for name, slot in zip(names, slot_rvas):
        mem.add_class(name, slot)

    found = auto_offsets._verify_candidates(mem, GA_BASE, slot_rvas + [0x999999])
    ok = True
    for name, slot in zip(names, slot_rvas):
        got = found.get(name)
        match = got == slot
        ok = ok and match
        print(f"    {name:<26} -> 0x{got if got else 0:X}  {'MATCH' if match else 'MISMATCH'}")
    return ok


def test_repair_applies():
    """apply_to_offsets patches OFF and returns the count."""
    from rust_esp_mvc import auto_offsets as ao
    found = {"BaseNetworkable": 0x111, "MainCamera": 0x222,
             "BasePlayer": 0x333, "ListComponent_PlayerModel": 0x444}
    ao.apply_to_offsets(found)
    ok = (
        legacy.OFF.BaseNetworkable_c == 0x111
        and legacy.OFF.MainCamera_c == 0x222
        and legacy.OFF.BasePlayer_c == 0x333
        and legacy.OFF.ListComponent_PlayerModel_c == 0x444
    )
    # restore
    ao.apply_to_offsets({
        "BaseNetworkable": 0x1074E028, "MainCamera": 0x107948D0,
        "BasePlayer": 0xFCDBAF0, "ListComponent_PlayerModel": 0x10720220,
    })
    print(f"    apply_to_offsets -> {'PASS' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    print("[1] Candidate discovery (real DLL):")
    r1 = test_candidate_discovery()
    print("\n[2] Name verification (synthetic live model):")
    r2 = test_name_verification()
    print("\n[3] apply_to_offsets:")
    r3 = test_repair_applies()
    print(f"\n[{'PASS' if r1 and r2 and r3 else 'FAIL'}] all tests")
    sys.exit(0 if (r1 and r2 and r3) else 1)
