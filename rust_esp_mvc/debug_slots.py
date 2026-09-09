"""Debug: are the .data slot values on disk RVAs into sections, or obfuscated?"""
import struct
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_auto_offsets_disk as t
from rust_esp_mvc import legacy_runtime as legacy

with open(t.DLL, "rb") as f:
    header = f.read(0x1000)
img, sects = t.parse_pe_imagebase(header)
print(f"ImageBase 0x{img:X}  sections:")
for s in sects:
    print(f"  {s['name']:<10} VA=0x{s['va']:X} sz=0x{s['raw_sz']:X} fo=0x{s['fo']:X}")


def rva_in_sections(rva):
    for s in sects:
        if s["va"] <= rva < s["va"] + s["raw_sz"]:
            return s["name"]
    # virtual-only tail (raw_sz < virtual size)? we only know raw_sz here
    return None


mem = t.FakeMem(t.DLL, img)
try:
    targets = {
        "BN": legacy.OFF.BaseNetworkable_c,
        "MC": legacy.OFF.MainCamera_c,
        "BP": legacy.OFF.BasePlayer_c,
        "LC": legacy.OFF.ListComponent_PlayerModel_c,
    }
    for name, rva in targets.items():
        raw = mem.read(img + rva, 8)
        val = struct.unpack("<Q", raw)[0] if len(raw) == 8 else None
        sec = rva_in_sections(val) if val else None
        print(f"{name}: slot@0x{rva:X} = 0x{val:X}  -> section {sec}  (is RVA into image?)")
finally:
    mem.close()
