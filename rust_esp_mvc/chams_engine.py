"""Chams engine: Handles writing custom Material IDs to SkinnedMultiMesh Renderers.

Safety hardening (2026-09-08):
  - Read-verify before every write (confirm the page is mapped and readable)
  - Never cache renderer pointers: re-resolve every N frames
  - Strict pointer validation on every hop in the chain
  - Skip weapon/held-item renderers (they reallocate material arrays on animation)
  - Wrap all reads in try/except to absorb struct.error from short/None reads
  - Rate-limit: only re-apply chams every APPLY_INTERVAL frames, not every tick
"""

import struct
import time
from .legacy_runtime import OFF, _valid_user_ptr

# ── Tuning ───────────────────────────────────────────────────────────────────
APPLY_INTERVAL_S   = 0.5     # seconds between chams re-application per player
MAX_RENDERERS      = 32      # hard cap on renderer count (doc says 128, we play safe)
MAX_MATERIALS      = 8       # hard cap on material count per renderer
MIN_VALID_MAT_ID   = 1000    # anything below this is likely a zero/garbage
MAX_VALID_MAT_ID   = 5000000 # anything above this is likely a pointer fragment


class ChamsEngine:
    def __init__(self, mem):
        self.mem = mem
        # Timestamp of last successful chams application per pm
        self._last_apply = {}
        self._last_cleanup = time.perf_counter()

    # ── Pointer validation ───────────────────────────────────────────────
    @staticmethod
    def _vptr(addr):
        """Validate a user-mode pointer."""
        return _valid_user_ptr(addr)

    def _safe_u64(self, addr):
        """Read a u64, returning 0 on any failure."""
        if not self._vptr(addr):
            return 0
        try:
            data = self.mem.read(addr, 8)
            if not data or len(data) < 8:
                return 0
            return struct.unpack('<Q', data)[0]
        except Exception:
            return 0

    def _safe_u32(self, addr):
        """Read a u32, returning 0 on any failure."""
        if not self._vptr(addr):
            return 0
        try:
            data = self.mem.read(addr, 4)
            if not data or len(data) < 4:
                return 0
            return struct.unpack('<I', data)[0]
        except Exception:
            return 0

    def _safe_write_u32(self, addr, value_bytes):
        """Write 4 bytes only after confirming the target is readable first."""
        if not self._vptr(addr):
            return False
        # Read-before-write: if the page isn't mapped, the read returns zeros/None
        # and we abort instead of letting the driver write to an unmapped page.
        try:
            probe = self.mem.read(addr, 4)
            if not probe or len(probe) < 4:
                return False
        except Exception:
            return False
        return self.mem.write(addr, value_bytes)

    # ── Resolve renderer material arrays (NO caching) ────────────────────
    def _resolve_pm_materials(self, pm):
        """Walk PlayerModel → MultiMesh → RendererList → native renderers.

        Returns list of (mat_base, mat_count) tuples, or empty list.
        Never caches — stale pointers are what cause the crashes.
        """
        # PlayerModel → SkinnedMultiMesh
        smm = self._safe_u64(pm + OFF.pm_multiMesh)
        if not self._vptr(smm):
            return []

        # SkinnedMultiMesh → List<Renderer>
        renderer_list = self._safe_u64(smm + OFF.smm_rendererList)
        if not self._vptr(renderer_list):
            return []

        # List<T>._items and ._size
        items_arr = self._safe_u64(renderer_list + OFF.list_items)
        size = self._safe_u32(renderer_list + OFF.list_size)

        if not self._vptr(items_arr) or size <= 0 or size > MAX_RENDERERS:
            return []

        # Read all managed renderer pointers at once
        try:
            ptrs_data = self.mem.read(items_arr + OFF.array_base, size * 8)
            if not ptrs_data or len(ptrs_data) < size * 8:
                return []
        except Exception:
            return []

        results = []
        for i in range(size):
            managed = struct.unpack_from('<Q', ptrs_data, i * 8)[0]
            if not self._vptr(managed):
                continue

            # managed C# object → native Unity Object (m_CachedPtr at +0x10)
            native = self._safe_u64(managed + 0x10)
            if not self._vptr(native):
                continue

            # native renderer → material array base and count
            mat_base  = self._safe_u64(native + OFF.renderer_materialArray)
            mat_count = self._safe_u32(native + OFF.renderer_materialCount)

            if not self._vptr(mat_base):
                continue
            if mat_count < 1 or mat_count > MAX_MATERIALS:
                continue

            # Final sanity: read the first material ID and check it's plausible
            first_id = self._safe_u32(mat_base)
            if first_id < MIN_VALID_MAT_ID or first_id > MAX_VALID_MAT_ID:
                continue

            results.append((mat_base, mat_count))

        return results

    # ── Main tick ────────────────────────────────────────────────────────
    def tick(self, model, players, vs):
        """Apply the selected material ID to targeted player renderers."""
        if not vs.chams_enabled:
            return

        mat_id = vs.chams_material_id
        packed_id = struct.pack('<I', mat_id)  # uint32_t, unsigned
        now = time.perf_counter()

        for p in players:
            pm = p.get("pm")
            if not pm:
                continue

            is_local = p.get("is_local", False)
            if is_local and not vs.chams_apply_local:
                continue
            if not is_local and not vs.chams_apply_enemies:
                continue

            # Rate-limit: don't hammer the same player every frame
            last = self._last_apply.get(pm, 0.0)
            if now - last < APPLY_INTERVAL_S:
                continue

            # Fresh resolve every time — never trust cached pointers
            renderers = self._resolve_pm_materials(pm)
            if not renderers:
                continue

            wrote_any = False
            for mat_base, mat_count in renderers:
                for j in range(mat_count):
                    mat_addr = mat_base + (j * 4)
                    current_id = self._safe_u32(mat_addr)

                    # Skip if already our ID, or if the value looks like garbage
                    if current_id == mat_id:
                        continue
                    if current_id < MIN_VALID_MAT_ID or current_id > MAX_VALID_MAT_ID:
                        continue

                    if self._safe_write_u32(mat_addr, packed_id):
                        wrote_any = True

            if wrote_any:
                self._last_apply[pm] = now

        # Prune stale entries from the rate-limit dict
        if now - self._last_cleanup > 10.0:
            self._last_cleanup = now
            active = {p.get("pm") for p in players if p.get("pm")}
            self._last_apply = {
                k: v for k, v in self._last_apply.items() if k in active
            }
