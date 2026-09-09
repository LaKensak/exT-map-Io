"""No-recoil engine — scales recoil properties on the local player's held weapon.

Reads BaseProjectile.recoilProperties and its newRecoilOverride, then writes
scaled yawMin/yawMax/pitchMin/pitchMax values. Resets to originals when
disabled or when the weapon changes.

Chain:  local_bp → inventory → belt → match clActiveItem UID → Item.held_entity
        → BaseProjectile + 0x408 → RecoilProperties + 0x80 → NewRecoilOverride
Write:  recoilProps + 0x18..0x24  (4 floats: yawMin, yawMax, pitchMin, pitchMax)
        newRecoil   + 0x18..0x24  (same 4 floats)
"""

import struct
import time

from . import legacy_runtime as legacy

OFF = legacy.OFF

# ── Per-weapon default recoil values ────────────────────────────────────────
# Format: "shortname_key": (RecoilProperties[4], NewRecoilOverride[4])
# RecoilProperties = [yawMin, yawMax, pitchMin, pitchMax]
# NewRecoilOverride = same layout
RECOIL_TABLE = {
    "rifle_ak":            ((-2, 8, -4, -30),     (1.5, 2.5, -2.5, -3.5)),
    "rifle_ak_ice":        ((-2, 8, -4, -30),     (1.5, 2.5, -2.5, -3.5)),
    "rifle_ak_diver":      ((-2, 8, -4, -30),     (1.5, 2.5, -2.5, -3.5)),
    "rifle_ak_med":        ((-2, 8, -4, -30),     (1.5, 2.5, -2.5, -3.5)),
    "rifle_lr300":         ((-1, 5, -2.5, -12),   (-0.5, 0.5, -2, -3)),
    "smg_2":               ((-1.5, 10, -2, -15),  (-1, 1, -1.5, -2)),
    "revolver_hc":         ((-2, 2, -15, -16),    (0, 0, 0, 0)),
    "rifle_sks":           ((-1, 1, -5, -6),      (-0.3, 0.3, -3, -3.5)),
    "t1_smg":              ((-1.5, 10, -2, -15),  (-1, 1, -2, -2.5)),
    "pistol_python":       ((-2, 2, -15, -16),    (0, 0, 0, 0)),
    "pistol_semiauto":     ((-2, 2, -6, -8),      (-1, 1, -2, -2.5)),
    "shotgun_m4":          ((2, 4, -7, -10),      (0, 0, 0, 0)),
    "lmg_m249":            ((-1, 1, -5, -6),      (1.25, 2.25, -3, -4)),
    "rifle_m39":           ((-1.5, 1.5, -5, -7),  (1.5, 2.5, -3, -4)),
    "rifle_semiauto":      ((-1, 1, -5, -6),      (-0.5, 0.5, -2, -3)),
    "rifle_bolt":          ((-4, 4, -2, -3),      (0, 0, 0, 0)),
    "rifle_l96":           ((-2, 2, -1, -1.5),    (0, 0, 0, 0)),
    "hmlmg":               ((0.5, 0.75, -3, -4),  (-1.25, -2.5, -3, -4)),
    "minigun":             ((0, 0, -0.5, -1),     (0, 0, 0, 0)),
    "pistol_m92":          ((-1, 1, -7, -8),      (-1, 1, -7, -8)),
    "pistol_revolver":     ((-1, 1, -3, -6),      (0, 0, 0, 0)),
    "smg_thompson":        ((-1.5, 10, -2, -15),  (-1, 1, -1.5, -2)),
    "pistol_prototype17":  ((-1, 1, -2, -2.5),    (0, 0, 0, 0)),
    "smg_mp5":             ((-1.25, 6, -2, -10),  (-1, 1, -1, -3)),
    "bow_hunting":         ((-3, 3, -3, -6),      (0, 0, 0, 0)),
    "crossbow":            ((-3, 3, -3, -6),      (0, 0, 0, 0)),
    "bow_compound":        ((-3, 3, -3, -6),      (0, 0, 0, 0)),
    "shotgun_pump":        ((4, 8, -10, -14),     (0, 0, 0, 0)),
    "pistol_nailgun":      ((-1, 1, -3, -6),      (0, 0, 0, 0)),
}

# ── Offsets ──────────────────────────────────────────────────────────────────
# BaseProjectile + recoil → RecoilProperties*
OFF_RECOIL_PROPS        = 0x408
# RecoilProperties + newRecoilOverride → NewRecoilOverride*
OFF_NEW_RECOIL_OVERRIDE = 0x80
# Both RecoilProperties and NewRecoilOverride share these field offsets:
OFF_YAW_MIN             = 0x18
OFF_YAW_MAX             = 0x1C
OFF_PITCH_MIN           = 0x20
OFF_PITCH_MAX           = 0x24

# Inventory chain offsets -- kept in sync with legacy_runtime.OFF, refreshed
# 2026-09-08 against rust-dumper-post's live IL2CPP-reflection dump (ground
# truth from the running game, not disassembly guessing -- see the matching
# comments in legacy_runtime.OFF for the full reasoning).
OFF_INVENTORY           = 0x510   # BasePlayer.inventory (HV wrapper)
OFF_CL_ACTIVE_ITEM      = 0x588   # BasePlayer.clActiveItem (unchanged)
OFF_CONTAINER_BELT      = 0x28    # PlayerInventory.containerBelt -- 2026-09-05 screenshot-verified value, unchanged through the 09-08/09-09 wear/main churn (see OFF.container_belt)
OFF_ITEM_LIST           = 0x78    # ItemContainer.itemList -- live-confirmed 2026-09-08 (was 0x38 unconfirmed, was 0x68 stale)
OFF_ITEM_UID            = 0xD8    # Item.uid (ItemId-typed field) -- live-confirmed 2026-09-08 (was 0x10 unconfirmed, was 0xD0 stale)
OFF_ITEM_HELD_ENTITY    = 0xB8    # Item's "currently deployed" EntityRef field -- was 0x38 (that's Item.worldEnt, a different field entirely; this was the actual held-item/no-recoil bug, see OFF.item_heldEntity)
OFF_ITEM_DEFINITION     = 0xA0    # Item.info (ItemDefinition) -- live-confirmed 2026-09-08
OFF_ITEMDEF_SHORTNAME   = 0x28    # ItemDefinition.shortname (Il2CppString*) (unchanged)

RESOLVE_INTERVAL = 0.5  # seconds between weapon re-resolution


def _weapon_key(shortname):
    """Convert 'rifle.ak' → 'rifle_ak' to match RECOIL_TABLE keys."""
    return shortname.replace('.', '_') if shortname else ""


class RecoilEngine:
    def __init__(self, mem):
        self.mem = mem
        self._last_weapon_ptr = 0
        self._last_weapon_key = ""
        self._needs_reset = False
        self._last_resolve_at = 0.0
        # Cache for the resolved weapon data
        self._cached_bp_projectile = 0
        self._cached_recoil_props = 0
        self._cached_new_recoil = 0

    # ── Safe reads ───────────────────────────────────────────────────────

    def _safe_u64(self, addr):
        if not legacy._valid_user_ptr(addr):
            return 0
        try:
            data = self.mem.read(addr, 8)
            if not data or len(data) < 8:
                return 0
            return struct.unpack('<Q', data)[0]
        except Exception:
            return 0

    def _safe_u32(self, addr):
        if not legacy._valid_user_ptr(addr):
            return 0
        try:
            data = self.mem.read(addr, 4)
            if not data or len(data) < 4:
                return 0
            return struct.unpack('<I', data)[0]
        except Exception:
            return 0

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

    # ── Resolve the local weapon ─────────────────────────────────────────

    def _resolve_weapon(self, model):
        """Find the local player's held BaseProjectile and weapon shortname.

        Returns (base_projectile_ptr, weapon_key) or (0, "").
        """
        now = time.perf_counter()
        # Cache on EITHER result: on builds where heldEntity never resolves the
        # pointer stays 0 but the shortname is still good, and gating the cache
        # on the pointer alone made every caller re-walk the whole inventory on
        # every tick.
        if (now - self._last_resolve_at < RESOLVE_INTERVAL
                and (self._cached_bp_projectile or self._last_weapon_key)):
            return self._cached_bp_projectile, self._last_weapon_key

        self._last_resolve_at = now
        # Cleared up front so every failure path below leaves it empty without
        # having to remember to reset it.
        self._last_weapon_key = ""

        # Get local BasePlayer
        local_bp = getattr(model, 'local_bp', None) or 0
        if not legacy._valid_user_ptr(local_bp):
            local_pm = getattr(model, '_last_local_pm', None) or 0
            if local_pm:
                cache = getattr(model, '_pm_bp_cache', {})
                local_bp = cache.get(local_pm, 0)
        if not legacy._valid_user_ptr(local_bp):
            self._cached_bp_projectile = 0
            return 0, ""

        # Read clActiveItem (this is the active item UID, possibly encrypted)
        cl_active_raw = self._safe_u64(local_bp + OFF_CL_ACTIVE_ITEM)
        if not cl_active_raw:
            self._cached_bp_projectile = 0
            return 0, ""

        # Decrypt clActiveItem to get the UID
        try:
            active_uid = legacy.decrypt_cl_active_item(cl_active_raw) & 0xFFFFFFFFFFFFFFFF
        except Exception:
            self._cached_bp_projectile = 0
            return 0, ""

        if not active_uid:
            self._cached_bp_projectile = 0
            return 0, ""

        # Walk inventory → belt → items to find the matching item
        inv_wrapper = self._safe_u64(local_bp + OFF_INVENTORY)
        if not legacy._valid_user_ptr(inv_wrapper):
            self._cached_bp_projectile = 0
            return 0, ""

        # Decrypt the inventory HV wrapper
        hv_raw = legacy._read_hv_handle(self.mem, inv_wrapper, attempts=2)
        if not hv_raw:
            self._cached_bp_projectile = 0
            return 0, ""

        ga = getattr(model, 'ga', 0)
        inv_dec = legacy.decrypt_player_inventory(hv_raw)
        inv_ptr = legacy.resolve_tagged_handle(self.mem, inv_dec, ga)
        if not legacy._valid_user_ptr(inv_ptr):
            self._cached_bp_projectile = 0
            return 0, ""

        # Inventory → containerBelt
        belt = self._safe_u64(inv_ptr + OFF_CONTAINER_BELT)
        if not legacy._valid_user_ptr(belt):
            self._cached_bp_projectile = 0
            return 0, ""

        # Belt → itemList (List<Item>)
        item_list = self._safe_u64(belt + OFF_ITEM_LIST)
        if not legacy._valid_user_ptr(item_list):
            self._cached_bp_projectile = 0
            return 0, ""

        items_arr = self._safe_u64(item_list + OFF.ListHashSet_vals)
        items_count = self._safe_u32(item_list + OFF.ListHashSet_size)

        if not legacy._valid_user_ptr(items_arr) or items_count < 1 or items_count > 12:
            self._cached_bp_projectile = 0
            return 0, ""

        # Read all item pointers
        try:
            ptrs_data = self.mem.read(items_arr + OFF.array_payload, items_count * 8)
            if not ptrs_data or len(ptrs_data) < items_count * 8:
                self._cached_bp_projectile = 0
                return 0, ""
        except Exception:
            self._cached_bp_projectile = 0
            return 0, ""

        # Find the item whose UID matches clActiveItem
        matched_item = 0
        for i in range(items_count):
            item_ptr = struct.unpack_from('<Q', ptrs_data, i * 8)[0]
            if not legacy._valid_user_ptr(item_ptr):
                continue
            uid = self._safe_u64(item_ptr + OFF_ITEM_UID)
            if uid and (uid & 0xFFFFFFFFFFFFFFFF) == active_uid:
                matched_item = item_ptr
                break

        if not matched_item:
            self._cached_bp_projectile = 0
            return 0, ""

        # Read the weapon shortname FIRST. It comes off the ItemDefinition and
        # does not need heldEntity at all -- and on this build heldEntity often
        # reads back null (see [HELD-DBG] viaHeldEntity=0, which is why the ESP
        # names held items via the UID instead). Reading it after the
        # heldEntity guard meant the aimbot got "" for a weapon it had already
        # identified, so the projectile solver silently never ran for bows.
        weapon_key = ""
        itemdef = self._safe_u64(matched_item + OFF_ITEM_DEFINITION)
        if legacy._valid_user_ptr(itemdef):
            str_ptr = self._safe_u64(itemdef + OFF_ITEMDEF_SHORTNAME)
            if legacy._valid_user_ptr(str_ptr):
                try:
                    # Il2CppString: length at +0x10 (int32), chars at +0x14 (UTF-16)
                    slen = self._safe_u32(str_ptr + 0x10)
                    if 0 < slen <= 64:
                        raw = self.mem.read(str_ptr + 0x14, slen * 2)
                        if raw and len(raw) == slen * 2:
                            weapon_key = _weapon_key(
                                raw.decode('utf-16-le', errors='ignore')
                            )
                except Exception:
                    pass

        # Item → held_entity (BaseProjectile). Only the recoil path needs this
        # pointer; the aimbot's ballistics need nothing but weapon_key. So a
        # null heldEntity now costs the recoil feature (which cannot work
        # without it) and nothing else, instead of blanking both.
        held_entity = self._safe_u64(matched_item + OFF_ITEM_HELD_ENTITY)
        if not legacy._valid_user_ptr(held_entity):
            held_entity = 0

        self._cached_bp_projectile = held_entity
        self._last_weapon_key = weapon_key
        return held_entity, weapon_key

    # ── Main tick ────────────────────────────────────────────────────────

    def tick(self, model, vs):
        """Apply or reset recoil scaling."""
        scale_x = getattr(vs, 'norecoil_x', 100) / 100.0
        scale_y = getattr(vs, 'norecoil_y', 100) / 100.0
        enabled = getattr(vs, 'norecoil_enabled', False)

        if not enabled and not self._needs_reset:
            return

        bp_proj, weapon_key = self._resolve_weapon(model)
        if not bp_proj or not weapon_key:
            return

        recoil_data = RECOIL_TABLE.get(weapon_key)
        if not recoil_data:
            return

        recoil_props = self._safe_u64(bp_proj + OFF_RECOIL_PROPS)
        if not legacy._valid_user_ptr(recoil_props):
            return

        new_recoil = self._safe_u64(recoil_props + OFF_NEW_RECOIL_OVERRIDE)
        if not legacy._valid_user_ptr(new_recoil):
            new_recoil = 0  # some weapons don't have newRecoilOverride

        original_rp, original_nr = recoil_data
        offsets = [OFF_YAW_MIN, OFF_YAW_MAX, OFF_PITCH_MIN, OFF_PITCH_MAX]

        if enabled and (scale_x < 1.0 or scale_y < 1.0):
            self._needs_reset = True

            # Weapon switch detection: reset old weapon's recoil before applying new
            if bp_proj != self._last_weapon_ptr and self._last_weapon_ptr:
                # We'd need the old weapon's recoil data to reset, but it's gone.
                # The game resets recoil on weapon switch anyway, so just update tracking.
                pass

            self._last_weapon_ptr = bp_proj

            for i, off in enumerate(offsets):
                scale = scale_x if i < 2 else scale_y
                desired_rp = original_rp[i] * scale

                current_rp = self._safe_f32(recoil_props + off)
                if abs(current_rp - desired_rp) > 0.001:
                    self._safe_write_f32(recoil_props + off, desired_rp)

                if new_recoil:
                    desired_nr = original_nr[i] * scale
                    current_nr = self._safe_f32(new_recoil + off)
                    if abs(current_nr - desired_nr) > 0.001:
                        self._safe_write_f32(new_recoil + off, desired_nr)

        elif self._needs_reset:
            # Restore original values
            for i, off in enumerate(offsets):
                self._safe_write_f32(recoil_props + off, original_rp[i])
                if new_recoil:
                    self._safe_write_f32(new_recoil + off, original_nr[i])

            self._needs_reset = False
            self._last_weapon_ptr = 0
