"""One-off: find ListComponent<Projectile>'s klass RVA for this Rust build.

`OFF.ProjectileList_c` is only used by the "steer arrows in flight" feature
and the ballistics measurement it depends on -- nothing else in the ESP
touches it, so this is safe to run without disturbing anything working.

HOW TO RUN
    1. Close the overlay if it's running (only one process should hold the
       driver connection at a time).
    2. Make sure Rust is running and you are logged into a character.
    3. From the repo root:  python -m tools.rust_esp_mvc.find_projectile_klass
    4. Paste the printed RVA back.

WHY THIS NEEDS THE LIVE GAME, NOT JUST THE on-disk .dll:
    Finding CANDIDATE slots is a pure disk scan (fast, no game needed) --
    the compiler always emits `MOV r64,[RIP+rel32]` to load a class global,
    so every such instruction pointing into .data is a candidate. But the
    VALUE stored in that slot is anti-tamper obfuscated at rest on disk and
    only becomes a real Il2CppClass* once the running game initializes it,
    so verifying which candidate is the right one (by reading its class
    name back) needs a live process.

WHY "ListComponent_Projectile" (not "Projectile"):
    We need the GENERIC INSTANTIATION `ListComponent<Projectile>`'s own
    static-fields slot (it holds the live registry of in-flight
    projectiles), not the `Projectile` class itself. This project's
    existing ListComponent_PlayerModel_c entry proves this build's IL2CPP
    codegen names such instantiations literally "ListComponent_<T>" in the
    class's raw .name field (auto_offsets.py compares that exact string,
    not a reconstructed generic display name) -- so the analogous name for
    T=Projectile is "ListComponent_Projectile".
"""
import sys

sys.path.insert(0, r"F:\raid\pythonProject4\tools")

from rust_esp_mvc import legacy_runtime as legacy
from rust_esp_mvc import auto_offsets

TARGET_NAME = "ListComponent_Projectile"


def main():
    memory = legacy.Mem()
    if not memory.connect():
        print("[!] Driver not connected -- is it loaded?")
        return 1
    memory.init_event()
    if not memory.attach(timeout=60.0):
        print("[!] RustClient.exe not found after 60s")
        return 1
    memory.find_base()
    print(f"[+] Base: 0x{memory.base:X}  PID: {memory.pid}")

    ga_base, up_base = legacy.find_modules(memory)
    if not ga_base:
        print("[!] GameAssembly.dll not found")
        return 1
    print(f"[+] GameAssembly.dll: 0x{ga_base:X}")

    dll_path = auto_offsets.find_gameassembly_path()
    print(f"[+] Disk DLL for candidate scan: {dll_path or '(not found, using memory scan)'}")

    def progress(msg):
        print(msg, flush=True)

    found = auto_offsets.resolve_class_rvas(
        memory, ga_base, dll_path=dll_path, progress=progress,
        names=[TARGET_NAME],
    )

    rva = found.get(TARGET_NAME)
    if not rva:
        print(f"\n[!] '{TARGET_NAME}' not found. Possible reasons:")
        print("    - the actual runtime name differs slightly (try the exact")
        print("      spelling from a fresh dump.cs 'ListComponent<Projectile>'")
        print("      entry's obfuscated/real name if it differs)")
        print("    - the class has no static-field slot referenced this way")
        print("      (e.g. if nobody in Rust's own code currently holds an")
        print("      in-flight projectile list open -- try again with an")
        print("      arrow actually in the air)")
        return 1

    print(f"\n[+] FOUND: {TARGET_NAME} slot RVA = 0x{rva:X}")
    print(f"    Set legacy_runtime.OFF.ProjectileList_c = 0x{rva:X}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
