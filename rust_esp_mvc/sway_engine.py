"""No-sway / no-bloom engine — zeroes spread and sway fields on the held weapon.

Writes directly onto BaseProjectile float fields (no sub-object indirection).
Caches the original per-weapon values the first time they are read, and restores
them when disabled or when the weapon changes.

Fields zeroed (offsets from BaseProjectile, matching offsets_decrypts_export.h):
    aimSway              0x400   visual weapon sway amplitude
    aimSwaySpeed         0x404   sway oscillation speed
    aimCone              0x418   ADS bullet spread cone
    hipAimCone           0x41C   hip-fire bullet spread cone
    aimconePenaltyPerShot 0x420  spread increase per shot fired
    aimConePenaltyMax    0x424   max accumulated spread penalty
    stancePenaltyScale   0x430   stance (crouch/stand) spread multiplier
"""

import struct

from . import legacy_runtime as legacy

OFF_AIM_SWAY                = 0x400
OFF_AIM_SWAY_SPEED          = 0x404
OFF_AIM_CONE                = 0x418
OFF_HIP_AIM_CONE            = 0x41C
OFF_AIMCONE_PENALTY_PER_SHOT = 0x420
OFF_AIMCONE_PENALTY_MAX     = 0x424
OFF_STANCE_PENALTY_SCALE    = 0x430

_FIELDS = (
    OFF_AIM_SWAY,
    OFF_AIM_SWAY_SPEED,
    OFF_AIM_CONE,
    OFF_HIP_AIM_CONE,
    OFF_AIMCONE_PENALTY_PER_SHOT,
    OFF_AIMCONE_PENALTY_MAX,
    OFF_STANCE_PENALTY_SCALE,
)


class SwayEngine:
    def __init__(self, mem, recoil_engine):
        self.mem = mem
        self._recoil_engine = recoil_engine
        self._last_weapon_ptr = 0
        self._originals = {}       # weapon_ptr -> {offset: float}
        self._needs_reset = False

    def _safe_f32(self, addr):
        if not legacy._valid_user_ptr(addr):
            return 0.0
        try:
            data = self.mem.read(addr, 4)
            if not data or len(data) < 4:
                return 0.0
            return struct.unpack('<f', data)[0]
        except Exception:
            return 0.0

    def _safe_write_f32(self, addr, value):
        if not legacy._valid_user_ptr(addr):
            return False
        try:
            probe = self.mem.read(addr, 4)
            if not probe or len(probe) < 4:
                return False
        except Exception:
            return False
        return self.mem.write(addr, struct.pack('<f', value))

    def _cache_originals(self, bp_proj):
        if bp_proj in self._originals:
            return
        vals = {}
        for off in _FIELDS:
            vals[off] = self._safe_f32(bp_proj + off)
        self._originals[bp_proj] = vals

    def _restore(self, bp_proj):
        vals = self._originals.get(bp_proj)
        if not vals:
            return
        for off, orig in vals.items():
            self._safe_write_f32(bp_proj + off, orig)

    def tick(self, model, vs):
        enabled = getattr(vs, 'nosway_enabled', False)

        if not enabled and not self._needs_reset:
            return

        bp_proj, _ = self._recoil_engine._resolve_weapon(model)
        if not bp_proj:
            return

        if enabled:
            self._cache_originals(bp_proj)
            self._needs_reset = True

            if bp_proj != self._last_weapon_ptr and self._last_weapon_ptr:
                self._restore(self._last_weapon_ptr)
            self._last_weapon_ptr = bp_proj

            for off in _FIELDS:
                cur = self._safe_f32(bp_proj + off)
                if abs(cur) > 0.0001:
                    self._safe_write_f32(bp_proj + off, 0.0)

        elif self._needs_reset:
            if self._last_weapon_ptr:
                self._restore(self._last_weapon_ptr)
            self._needs_reset = False
            self._last_weapon_ptr = 0
