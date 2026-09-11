"""Aim engine — direct bodyAngles write into PlayerInput.

Replaces the old mouse-injection aim (CMD_MOUSE_MOVE / CMD_CEREBRO_AIM) which
could not reach Rust's Raw Input, with a straight memory write of pitch/yaw
floats into the local player's PlayerInput structure.

Anti-detection features:
  WRITE-LEVEL:
  • Dual-write:  bodyAngles (PI+0x44) AND headAngles (PE+0x60) simultaneously
                 → eliminates 1-frame desync that server integrity can flag

  MOVEMENT-LEVEL (humanization — toggle OFF for ragebot):
  • MousePhysicsModel — temporally-correlated Perlin-like tremor (~10Hz)
    replacing gaussian IID noise (which ML classifiers flag as synthetic)
  • Smoothstep velocity profile — natural S-curve (fast acquire → slow
    precision) replacing discrete threshold steps
  • Overshoot simulation — 15% chance to overshoot near target with
    micro-correction next tick (humans NEVER converge monotonically)
  • Reaction time delay — 150-300ms pause when acquiring a new target
  • Angular velocity cap (deg/s, human-plausible)
  • Random write skip (breaks constant-rate pattern)
  • Interval jitter (±30% timing randomization)
  • Target stickiness (no inhuman instant switching)

Chain:  local_pm → local_bp (via pm_to_bp) → bp + OFF.input → PlayerInput*
Read:   PI + 0x44 (pitch float)  PI + 0x48 (yaw float)
Write:  PI + 0x44 (8 bytes: new_pitch, new_yaw)
        PE + 0x60 (8 bytes: headAngles pitch, yaw)  [dual-write mode]
"""

import ctypes
import math
import random
import struct
import time

from . import legacy_runtime as legacy


# Same GetAsyncKeyState path controller.py uses for the aim key, so a bind
# configured here behaves identically to the one that already works.
_USER32 = ctypes.windll.user32
_USER32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_USER32.GetAsyncKeyState.restype = ctypes.c_short


def _key_down(vk):
    """True while virtual-key `vk` is held. 0 or a bad bind reads as False."""
    if not vk:
        return False
    try:
        return bool(_USER32.GetAsyncKeyState(int(vk)) & 0x8000)
    except Exception:
        return False

OFF = legacy.OFF


# ---------------------------------------------------------------------------
# Mouse physics model — temporally-correlated noise
# ---------------------------------------------------------------------------

class MousePhysicsModel:
    """Simulates human hand tremor using temporally-correlated noise.

    Real mouse movement noise is NOT gaussian IID — it's a continuous
    tremor signal with ~8-12 Hz frequency (physiological tremor) plus
    higher-frequency micro-corrections.  Layered sines with irrational
    frequency ratios approximate Perlin noise cheaply.

    Why this matters: ML classifiers (LSTM) flag gaussian noise because
    consecutive samples are uncorrelated.  Real tremor has autocorrelation
    at ~100ms lag.  This model has that naturally because sin is smooth.
    """

    def __init__(self):
        self._phase_x = random.random() * 1000.0
        self._phase_y = random.random() * 1000.0
        self._tremor_freq = 10.0  # Hz — physiological tremor band

    def tremor(self, t, amplitude_deg):
        """Return (dx, dy) noise in degrees at time t.

        The amplitude_deg parameter scales the output.  At 0.4° (the
        default aim_jitter_deg), the peak tremor is ~0.4° per axis.
        """
        if amplitude_deg <= 0.0:
            return 0.0, 0.0
        f = self._tremor_freq
        # Three octaves with irrational frequency ratios → pseudo-random,
        # but temporally smooth (autocorrelation > 0 at short lag).
        nx = (math.sin(self._phase_x + t * f * 2.1) * 0.50
              + math.sin(self._phase_x + t * f * 5.3) * 0.30
              + math.sin(self._phase_x + t * f * 11.7) * 0.20)
        ny = (math.sin(self._phase_y + t * f * 1.9) * 0.50
              + math.sin(self._phase_y + t * f * 4.7) * 0.30
              + math.sin(self._phase_y + t * f * 13.1) * 0.20)
        return nx * amplitude_deg, ny * amplitude_deg


# ---------------------------------------------------------------------------
# Angle helpers
# ---------------------------------------------------------------------------

def _shortest_yaw(current, target):
    """Signed shortest-path delta from current yaw to target yaw (degrees)."""
    d = target - current
    while d > 180.0:
        d -= 360.0
    while d < -180.0:
        d += 360.0
    return d


def _clamp_pitch(p):
    """Clamp pitch to Rust's [-89, 89] range."""
    return max(-89.0, min(89.0, p))


def _target_angles(eye_origin, target_pos):
    """Compute absolute (pitch, yaw) in degrees from eye_origin to target_pos.

    Returns (pitch, yaw) or None if the direction is degenerate.
    """
    dx = target_pos[0] - eye_origin[0]
    dy = target_pos[1] - eye_origin[1]
    dz = target_pos[2] - eye_origin[2]
    dist = math.sqrt(dx * dx + dy * dy + dz * dz)
    if dist < 1e-3:
        return None
    yaw = math.degrees(math.atan2(dx, dz))
    pitch = -math.degrees(math.asin(max(-1.0, min(1.0, dy / dist))))
    return (_clamp_pitch(pitch), yaw)


def _angular_distance(cur_pitch, cur_yaw, tgt_pitch, tgt_yaw):
    """Approximate angular distance in degrees between two look directions."""
    dp = tgt_pitch - cur_pitch
    dy = _shortest_yaw(cur_yaw, tgt_yaw)
    return math.sqrt(dp * dp + dy * dy)


# ---------------------------------------------------------------------------
# Projectile ballistics — bow/crossbow/nailgun drop + lead
#
# The numbers below are only the cold-start fallback. The real per-weapon
# physics is read off live in-flight Projectile entities by
# ProjectileBallistics (see below), which walks
# ListComponent<Projectile> -> ListHashSet.vals -> Projectile*[] and reads
# initialVelocity/drag/gravityModifier straight out of the game. Once you
# have fired one shot with a weapon, its measured values replace the guess
# below for the rest of the session.
#
# So these only matter for the very first shot with a given weapon. They are
# community-known ballpark figures, not measured. If a first shot is
# consistently off and later shots land, that is this table being wrong, not
# the model.
# ---------------------------------------------------------------------------
# Base gravity magnitude. This is the ONE value in the ballistics chain that
# is assumed rather than read: speed, drag and gravityModifier all come off
# the live Projectile, but UnityEngine.Physics.gravity is a native extern
# property (`static Vector3 gravity { get; set; }` backed by
# get_gravity_Injected), not a managed static field -- there is no offset to
# read it from. 9.81 is Unity's default and Rust is not known to override it.
#
# No longer a user-facing setting (removed 2026-09-10 along with the
# open-loop drop solve it fed): ProjectileHoming's continuous, closed-loop
# correction re-aims every tick against the target's ACTUAL live position,
# which self-corrects for a slightly-wrong gravity constant in a way a
# one-shot solve never could. If arrows land consistently off by a
# proportional amount at every range with homing ON, this constant is still
# the thing to change -- just by editing it here, not via a slider.
GRAVITY_MPS2 = 9.81

PROJECTILE_TABLE = {
    # weapon_key (matches recoil_engine.RECOIL_TABLE's shortname keys):
    #     (initial_speed_m_s, drag, gravity_scale)
    "bow_hunting":    (40.0, 0.0, 1.0),
    "bow_compound":   (45.0, 0.0, 1.0),
    "crossbow":       (60.0, 0.0, 1.0),
    "pistol_nailgun": (90.0, 0.0, 1.0),
    "rifle_ak" : (300, 0.15, 1.0),
}


# Rust does NOT integrate projectiles per frame, and it does not use the
# continuous (textbook) trajectory. Projectile.SimulateProjectile steps a
# fixed 1/32 s grid and carries a `partialTime` accumulator precisely so that
# a variable Time.deltaTime still only ever advances velocity on that grid:
#
#     num = 0.03125f;
#     for (i = 0; i < num3; i++) {
#         position += velocity * num;          // position FIRST, old velocity
#         velocity += gravity * num;           // then gravity
#         velocity -= velocity * drag * num;   // then drag, on the NEW velocity
#     }
#     partialTime = travelTime - num * num3;
#     if (partialTime > 0) position += velocity * partialTime;
#
# Two consequences, both of which the previous continuous model got wrong:
#
#  1. The trajectory is framerate-independent (good -- nothing to measure),
#     but it is forward Euler. Position is advanced with the PRE-gravity
#     velocity, so the real projectile falls short of the textbook parabola by
#     g*t*dt/2: ~15 cm after 1 s of flight, ~31 cm after 2 s. Solving the
#     continuous trajectory aims high by exactly that -- a clean miss over a
#     head past ~40 m, and it grows with range.
#  2. Drag is applied AFTER gravity within a step, to the already-accelerated
#     velocity, and decays as (1 - drag*dt)^n rather than e^(-drag*t).
#
# The helpers below reproduce that recurrence exactly. It is a geometric
# series in both axes, so this stays closed-form -- no simulation loop, no
# per-tick cost worth worrying about.
PROJECTILE_STEP = 0.03125


def _steps_to_range(x, speed, pitch, drag):
    """(full 1/32 s steps, leftover seconds) to cover horizontal distance `x`.

    Horizontal motion is independent of gravity: the game applies drag
    per-axis as v -= v*drag*dt, so x never couples to y. With a = 1-drag*dt
    the horizontal position after n steps is a geometric series,
        x_n = vx0 * (1 - a^n) / drag
    which inverts directly for a real n. Flooring that gives the last full
    step, and the remainder is covered at that step's constant velocity --
    which is exactly the game's trailing `position += velocity * partialTime`.

    Returns None when drag stalls the projectile short of `x` at this pitch.
    """
    vx0 = speed * math.cos(pitch)
    if vx0 <= 1e-6 or x < 0.0:
        return None
    dt = PROJECTILE_STEP
    dragged = drag > 1e-4
    a = 1.0 - drag * dt if dragged else 1.0
    if dragged and a <= 1e-6:
        return None

    if dragged:
        ratio = x * drag / vx0
        if ratio >= 0.999:
            return None              # asymptotically short -- out of range
        n_real = math.log(1.0 - ratio) / math.log(a)
    else:
        n_real = x / (vx0 * dt)

    def state(n):
        """(horizontal distance, horizontal speed) after n full steps."""
        if dragged:
            an = a ** n
            return vx0 * (1.0 - an) / drag, vx0 * an
        return vx0 * n * dt, vx0

    n_full = max(int(n_real), 0)
    x_n, v_n = state(n_full)
    # n_real is exact, so int() lands on the right step or one either side of
    # it from float rounding alone -- settle it instead of trusting the floor.
    if x_n > x and n_full > 0:
        n_full -= 1
        x_n, v_n = state(n_full)
    elif x - x_n > v_n * dt:
        n_full += 1
        x_n, v_n = state(n_full)
    if v_n <= 1e-9:
        return None
    return n_full, min(max((x - x_n) / v_n, 0.0), dt)


def _flight_time_at_range(x, speed, pitch, drag):
    """Time to cover horizontal distance `x` when launched at `pitch` rad."""
    stepped = _steps_to_range(x, speed, pitch, drag)
    if stepped is None:
        return None
    n_full, partial = stepped
    return n_full * PROJECTILE_STEP + partial


def _height_at_range(x, speed, pitch, gravity, drag):
    """Height (relative to the muzzle) when the projectile is `x` metres out.

    Same fixed-step recurrence as _steps_to_range, on the vertical axis:
        v_{n+1} = (v_n - g*dt) * a,   a = 1 - drag*dt
    whose fixed point is v_inf = -a*g/drag, giving the closed forms below.
    The leftover partial step advances position at v_n with no further
    velocity update, matching the game's trailing partialTime term.
    """
    stepped = _steps_to_range(x, speed, pitch, drag)
    if stepped is None:
        return None
    n_full, partial = stepped
    dt = PROJECTILE_STEP
    vy0 = speed * math.sin(pitch)
    t_n = n_full * dt

    if drag > 1e-4:
        a = 1.0 - drag * dt
        an = a ** n_full
        v_inf = -a * gravity / drag
        y_n = v_inf * t_n + (vy0 - v_inf) * (1.0 - an) / drag
        v_n = v_inf + an * (vy0 - v_inf)
    else:
        # dt^2 * n(n-1)/2 == (t_n^2 - t_n*dt)/2 -- the -t_n*dt is the forward
        # Euler deficit, i.e. exactly the drop the old model over-predicted.
        y_n = vy0 * t_n - 0.5 * gravity * (t_n * t_n - t_n * dt)
        v_n = vy0 - gravity * t_n
    return y_n + v_n * partial


def _solve_launch_pitch(x, y, speed, gravity, drag):
    """Launch elevation (radians) so the projectile passes through (x, y).

    `x` is horizontal distance, `y` the target's height above the muzzle.
    This is the part that makes the aim exact at any range: rather than
    nudging the aim point up by the computed drop -- a first-order
    approximation that under-compensates more and more as the angle grows --
    it solves for the angle whose trajectory actually intersects the target.

    There is no closed form for either branch. The old code used the vacuum
    root of u^2 - (2v^2/(g x)) u + (2 v^2 y/(g x^2) + 1) = 0 whenever drag was
    zero, but that solves the CONTINUOUS parabola -- and Rust's projectiles
    follow a fixed-step forward-Euler one that falls g*t*dt/2 less (see
    PROJECTILE_STEP). So both branches now search on _height_at_range, which
    reproduces the game's recurrence exactly; only the bracket differs.

    Returns None if the target is out of range at this speed, in which case
    the caller should fall back to direct aim.
    """
    if speed <= 1e-3 or x <= 1e-3:
        return None

    g = max(gravity, 1e-6)

    if drag > 1e-4:
        # With drag, horizontal reach is capped at vx0/k, and vx0 shrinks as
        # the shot steepens -- so the reachable pitch band is |pitch| <
        # acos(x*k/v). (Bracketing on a fixed steep angle instead is what made
        # this report "out of range" for a bow at 40 m: at 85 degrees the
        # horizontal reach collapses to ~35 m even though a 7-degree shot
        # covers it easily.)
        reach = x * drag / speed
        if reach >= 0.999:
            return None                   # cannot cover x at ANY angle
        p_max = math.acos(reach) * 0.999
    else:
        # No drag means no reach cap, so the band is limited only by keeping
        # flight time finite as cos(pitch) -> 0.
        p_max = math.radians(85.0)

    def height(p):
        h = _height_at_range(x, speed, p, g, drag)
        return -1e18 if h is None else h

    # Height-at-x is unimodal in pitch (steeper buys altitude until the
    # extra flight time costs more to gravity than it gains), so locate the
    # peak by ternary search, then bisect the rising branch below it. A
    # plain bisection over the whole band could straddle the peak and
    # converge onto the lobbed high arc instead of the direct shot.
    lo, hi = -p_max, p_max
    for _ in range(30):
        m1 = lo + (hi - lo) / 3.0
        m2 = hi - (hi - lo) / 3.0
        if height(m1) < height(m2):
            lo = m1
        else:
            hi = m2
    peak = 0.5 * (lo + hi)
    if height(peak) < y:
        return None                       # out of range once drag is counted

    lo, hi = -p_max, peak
    for _ in range(30):                   # ~1e-9 rad, far below aim precision
        mid = 0.5 * (lo + hi)
        if height(mid) < y:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


class ProjectileBallistics:
    """Learns a weapon's real ballistics from its projectiles in flight.

    Chain -- last two hops are the live-proven ListComponent<PlayerModel>
    shape from legacy_runtime ([DBG] LC count=N); the first hop is
    Projectile's OWN instance offset (PROJECTILE_INSTANCE_OFF, confirmed by
    the user from a live cheat's decompiled chain 2026-09-10 -- see that
    constant's comment for why it legitimately differs from PlayerModel's):
      ga + OFF.ProjectileList_c              -> ListComponent<Projectile> klass
        + OFF.klass_static_fields (0xB8)     -> static fields
        + PROJECTILE_INSTANCE_OFF (0x20)     -> wrapper
        + OFF.ListComponent_parent (0x18)    -> the list
        + OFF.ListComponent_buffer/_size     -> Projectile*[] and its count
      Projectile + OFF.proj_initial_velocity -> Vector3 + drag + gravityModifier
      (read as one 20-byte block: the three fields are packed with no holes)

    Reading the list straight out of the static field (a guessed +0x28, the
    ORIGINAL wrong value, distinct from the now-confirmed +0x20) is what
    made every scan report "ListHashSet instance invalid"; the static field
    holds a wrapper, so it is two hops, not one.

    Attribution is "whatever weapon is held right now" -- you observe an
    arrow while holding the bow that fired it. Swapping weapons mid-flight
    could in principle mis-attribute one sample, which is why every value is
    range-checked before it is accepted and why a learned entry is only
    replaced by another plausible reading, never by garbage.
    """

    SCAN_INTERVAL = 0.25   # seconds between walks (cheap, but not per-frame)
    # On a busy server ours may not be in the first handful. The whole array
    # is one read and the owner check is one batch regardless of the count,
    # so a bigger cap costs payload, not IOCTLs -- and IOCTL count is the
    # cost model that matters (the driver polls at ~15 ms).
    MAX_PROJECTILES = 32

    def __init__(self, memory):
        self.mem = memory
        self._learned = {}       # weapon_key -> (speed, drag, gravity_scale)
        self._next_scan_at = 0.0
        self._klass = 0
        # Where the last walk stopped. Reported alongside src=table so a cold
        # start ("you have not fired yet") is distinguishable from a broken
        # offset chain -- they look identical from the aim line otherwise.
        self.last_status = "not scanned yet"
        self.last_count = 0
        self.last_rejected = 0
        # Offsets that actually worked, once probed (see _find_list).
        self._instance_off = None
        self._via_parent = True
        self._bad_scans = 0
        self._owner_off = None
        self._live_off = None

    def _list_at(self, sf, instance_off, via_parent):
        """(array, count) for the ListComponent singleton at sf+instance_off.

        Mirrors the PlayerModel ListComponent walk in legacy_runtime, which is
        live-proven ([DBG] LC count=N): the static field holds a WRAPPER, and
        the list hangs off wrapper+ListComponent_parent -- two hops, not one.
        Reading the list straight out of the static field (the old guessed
        +0x28) is what produced "ListHashSet instance invalid" forever.
        """
        node = self.mem.u64(sf + instance_off)
        if not legacy._valid_user_ptr(node):
            return None
        if via_parent:
            node = self.mem.u64(node + OFF.ListComponent_parent)
            if not legacy._valid_user_ptr(node):
                return None
        vals = self.mem.u64(node + OFF.ListComponent_buffer)
        count = self.mem.i32(node + OFF.ListComponent_size)
        if not legacy._valid_user_ptr(vals):
            return None
        if not (0 <= count <= 4096):
            return None
        return vals, count

    # ListComponent<Projectile>'s own instance-wrapper offset, confirmed by
    # the user from a live cheat's decompiled chain (2026-09-10):
    #   static_fields + 0x20 -> wrapper -> +0x18 (parent) -> +0x10 (buffer)
    # The last two hops are bit-identical to ListComponent_instance's
    # PlayerModel shape (parent=0x18, buffer=0x10); only the FIRST hop
    # differs (0x20 here vs 0x8 for PlayerModel) -- confirmation that
    # different ListComponent<T> instantiations really do lay out their
    # statics differently by T, which the original dead 0x28 guess had the
    # right idea about, just the wrong number.
    PROJECTILE_INSTANCE_OFF = 0x20

    def _find_list(self, sf):
        """Locate the projectile list, remembering which offsets worked.

        Tries the confirmed Projectile shape first, then the PlayerModel
        shape (harmless if wrong -- same validated probe as everywhere
        else), then falls back to scanning the first few static slots for
        any build where neither holds.
        """
        if self._instance_off is not None:
            got = self._list_at(sf, self._instance_off, self._via_parent)
            if got is not None:
                return got
            self._instance_off = None      # layout moved; re-probe

        # The known-good pairs first, so a coincidentally-valid-looking slot
        # earlier in the statics cannot win over a shape we already trust.
        candidates = [
            (self.PROJECTILE_INSTANCE_OFF, True),
            (OFF.ListComponent_instance, True),
        ]
        candidates += [(o, True) for o in range(0x0, 0x48, 8)]
        candidates += [(o, False) for o in range(0x0, 0x48, 8)]
        for off, via_parent in candidates:
            got = self._list_at(sf, off, via_parent)
            if got is None:
                continue
            self._instance_off, self._via_parent = off, via_parent
            return got
        return None

    # Window searched for Projectile.owner. The reference layout has owner at
    # +0xD0 with initialVelocity at +0x18; ours has initialVelocity at +0x28,
    # a uniform +0x10 shift that puts owner near +0xE0 -- but that is an
    # inference, so it is probed and confirmed against the known local
    # BasePlayer rather than hardcoded.
    OWNER_SEARCH = (0x80, 0x160)

    def _own_projectiles(self, ptrs, local_bp):
        """The subset of `ptrs` owned by the local player.

        Returns None if the owner field could not be located at all (so the
        caller can say so instead of silently learning someone else's ammo),
        otherwise the -- possibly empty -- list of our own projectiles.
        """
        if not legacy._valid_user_ptr(local_bp):
            return None

        if self._owner_off is None:
            # Probe: ONE batch over every (projectile, candidate offset)
            # pair at once (see ARCHITECTURE.md 3.3 -- never read in a
            # loop), not one IOCTL per projectile. MAX_PROJECTILES caps
            # `ptrs` at 32 and the window is 16 offsets, so this is at most
            # 512 addresses -- comfortably one transaction.
            lo, hi = self.OWNER_SEARCH
            offs = list(range(lo, hi, 8))
            addrs = [p + o for p in ptrs for o in offs]
            vals = self.mem.batch_u64(addrs, attempts=1) or []
            # addrs (and vals, in lockstep) are laid out p-major/off-minor,
            # so idx already lands on the right slot without any extra
            # bookkeeping -- it advances by exactly len(offs) per p whether
            # or not the inner loop found a match first.
            idx = 0
            for p in ptrs:
                for off in offs:
                    if idx < len(vals) and vals[idx] == local_bp:
                        self._owner_off = off
                    idx += 1
                    if self._owner_off is not None:
                        break
                if self._owner_off is not None:
                    break
            if self._owner_off is None:
                return None

        owners = self.mem.batch_u64(
            [p + self._owner_off for p in ptrs], attempts=1
        )
        if not owners:
            return None
        return [p for p, o in zip(ptrs, owners) if o == local_bp]

    # currentVelocity and currentPosition sit next to each other (the public
    # reference has them at +0x118 and +0x124, i.e. vel3 then pos3 with no
    # gap). Ours is a uniform +0x10 shift off that reference -- owner probed
    # out at +0xE0 against its +0xD0 -- which puts the pair near +0x128, but
    # the shift is an inference so the pair is located by validation.
    LIVE_SEARCH = (0x100, 0x170)
    MAP_LIMIT = 6000.0     # Rust worlds top out well inside this

    def live_state(self, p, launch_speed, near_pos):
        """(position, velocity) of an in-flight projectile, or None.

        Both come out of one 24-byte read, which is also what pins them: a
        candidate offset only passes if the first 12 bytes are a velocity of
        roughly the launch speed AND the next 12 are a world position near
        us. Either test alone matches plenty of garbage; together they do not.
        """
        lo, hi = self.LIVE_SEARCH
        offs = [self._live_off] if self._live_off is not None else \
            list(range(lo, hi, 4))
        for off in offs:
            raw = self.mem.read(p + off, 24)
            if not raw or len(raw) < 24:
                continue
            try:
                vx, vy, vz, px, py, pz = struct.unpack('<6f', raw)
            except struct.error:
                continue
            speed = math.sqrt(vx * vx + vy * vy + vz * vz)
            # Drag and gravity bend it, so allow a wide band -- but a live
            # projectile never sits at 0 or at ten times its launch speed.
            if not (0.35 * launch_speed <= speed <= 1.6 * launch_speed + 5.0):
                continue
            if not all(abs(c) < self.MAP_LIMIT for c in (px, py, pz)):
                continue
            if math.dist((px, py, pz), near_pos) > 500.0:
                continue
            self._live_off = off
            return (px, py, pz), (vx, vy, vz)
        if self._live_off is not None:
            self._live_off = None      # latched offset stopped validating
        return None

    def write_velocity(self, p, vel):
        """Overwrite an in-flight projectile's currentVelocity."""
        if self._live_off is None:
            return False
        try:
            return bool(self.mem.write(
                p + self._live_off, struct.pack('<3f', *vel)
            ))
        except Exception:
            return False

    def own_in_flight(self, ga, local_bp):
        """Our own live projectiles, as a list of pointers (never None)."""
        if self.mem is None or not ga or not legacy._valid_user_ptr(local_bp):
            return []
        try:
            klass = self._klass
            if not klass:
                klass = self.mem.u64(ga + OFF.ProjectileList_c)
                if not legacy._valid_user_ptr(klass):
                    return []
                self._klass = klass
            sf = self.mem.u64(klass + OFF.klass_static_fields)
            if not legacy._valid_user_ptr(sf):
                return []
            found = self._find_list(sf)
            if found is None:
                return []
            vals, count = found
            n = min(count, self.MAX_PROJECTILES)
            if n <= 0:
                return []
            arr = self.mem.read(vals + OFF.array_payload, n * 8)
            if not arr or len(arr) < n * 8:
                return []
            ptrs = [struct.unpack_from('<Q', arr, i * 8)[0] for i in range(n)]
            ptrs = [q for q in ptrs if legacy._valid_user_ptr(q)]
            return self._own_projectiles(ptrs, local_bp) or []
        except Exception:
            return []

    def learned(self, weapon_key):
        """Measured (speed, drag, gravity_scale), or None if never observed."""
        return self._learned.get(weapon_key)

    def observe(self, ga, weapon_key, now, local_bp=0):
        """Sample one of OUR live projectiles and attribute it to `weapon_key`.

        Returns the newly-learned tuple, or None when nothing was learned
        this call (no projectile of ours in flight, throttled, or read
        failure).
        """
        if self.mem is None or not ga or not weapon_key:
            self.last_status = "no memory/ga/weapon"
            return None
        if not legacy._valid_user_ptr(local_bp):
            self.last_status = "no local BasePlayer -- cannot tell my arrows apart"
            return None
        if now < self._next_scan_at:
            return None
        self._next_scan_at = now + self.SCAN_INTERVAL

        try:
            klass = self._klass
            if not klass:
                klass = self.mem.u64(ga + OFF.ProjectileList_c)
                if not legacy._valid_user_ptr(klass):
                    self.last_status = "ProjectileList_c klass invalid"
                    return None
                self._klass = klass
            sf = self.mem.u64(klass + OFF.klass_static_fields)
            if not legacy._valid_user_ptr(sf):
                self.last_status = "klass.static_fields invalid"
                return None
            found = self._find_list(sf)
            if found is None:
                self.last_status = (
                    f"no projectile list under static_fields=0x{sf:X} "
                    f"(probed +0x00..+0x40, both shapes)"
                )
                return None
            vals, count = found
            self.last_count = count
            if count == 0:
                # The overwhelmingly likely case: nothing is in the air.
                self.last_status = (
                    f"list ok at sf+0x{self._instance_off:X}"
                    f"{'/parent' if self._via_parent else ''} but empty "
                    f"-- no projectile in flight (fire one)"
                )
                return None

            # One read for the whole pointer array instead of one IOCTL per
            # entry -- the driver polls at ~15 ms, so call count is the whole
            # cost model here.
            n = min(count, self.MAX_PROJECTILES)
            arr = self.mem.read(vals + OFF.array_payload, n * 8)
            if not arr or len(arr) < n * 8:
                self.last_status = "could not read the projectile array"
                return None
            ptrs = [
                struct.unpack_from('<Q', arr, i * 8)[0] for i in range(n)
            ]
            ptrs = [p for p in ptrs if legacy._valid_user_ptr(p)]
            if not ptrs:
                self.last_status = f"list has {count} entries, none a valid pointer"
                return None

            # THE list is every projectile in the world, including other
            # players'. Taking the first entry made the learned speed flip
            # between weapons tick to tick (observed: 75 m/s then 50 m/s for
            # the same bow at the same range, i.e. the drop more than
            # doubling), so the sample must be attributed by owner.
            mine = self._own_projectiles(ptrs, local_bp)
            if mine is None:
                self.last_status = (
                    f"{len(ptrs)} projectile(s) in flight but cannot tell which "
                    f"are mine (owner field not located)"
                )
                return None
            if not mine:
                self.last_status = (
                    f"{len(ptrs)} projectile(s) in flight, none of them mine "
                    f"(owner at +0x{self._owner_off:X})"
                )
                return None

            rejected = 0
            for p in mine:
                raw = self.mem.read(p + OFF.proj_initial_velocity, 20)
                if not raw or len(raw) < 20:
                    rejected += 1
                    continue
                vx, vy, vz, drag, gravity = struct.unpack('<5f', raw)
                speed = math.sqrt(vx * vx + vy * vy + vz * vz)
                # Range checks: a real Rust projectile sits well inside
                # these. Anything outside means we read a dead/pooled entry
                # or the wrong object, and must not poison the cache.
                if not (5.0 <= speed <= 500.0):
                    rejected += 1
                    continue
                if not (0.0 <= drag <= 5.0) or not (0.0 <= gravity <= 5.0):
                    rejected += 1
                    continue
                value = (speed, drag, gravity)
                self._learned[weapon_key] = value
                self.last_rejected = rejected
                self._bad_scans = 0
                self.last_status = (
                    f"ok via sf+0x{self._instance_off:X}"
                    f"{'/parent' if self._via_parent else ''}, "
                    f"owner+0x{self._owner_off:X}, {len(mine)}/{len(ptrs)} mine"
                )
                return value
            self.last_rejected = rejected
            self.last_status = (
                f"all {rejected} of my {len(mine)} projectiles failed the "
                f"range check (list at sf+0x{self._instance_off:X})"
            )
            # A non-empty list whose entries never look like projectiles means
            # the probe latched onto the wrong list, not that the projectiles
            # are odd. Drop the latch and let the next scan try elsewhere.
            self._bad_scans += 1
            if self._bad_scans >= 3:
                self._instance_off = None
                self._bad_scans = 0
        except Exception as exc:
            # A transient bad read must never take the aim path down.
            self.last_status = f"exception {exc!r}"
            return None
        return None


class TeamFilter:
    """Keeps teammates out of the target list, by BasePlayer.currentTeam.

    CONFIRMED 0x558 (user's own dump, 2026-09-09:
    `inline constexpr std::uintptr_t team = 0x558;`). Do not re-derive it.
    tools/rust_esp_mvc/dump.txt says 0x550 and is wrong for this build; it
    is kept only as a fallback if 0x558 ever reads back pointer-shaped, and
    [AIM-TEAM] prints both raw values so a mismatch is visible immediately
    rather than silently disabling the filter.

    Plausibility: a Rust team id is 0 (no team) or a generated ulong. What it
    is NOT is a pointer, and a pointer is exactly what a wrong offset yields
    here, since the neighbouring fields are references. So a candidate is
    rejected when it looks like a user-space pointer.

    Fails OPEN on purpose: if the field cannot be read the player is treated
    as an enemy. The alternative -- treating unknowns as teammates -- would
    silently switch the aimbot off, which is far harder to notice than the
    occasional teammate slipping into the target list.
    """

    REFRESH = 1.0          # teams change rarely; one batch a second is plenty
    CANDIDATES = (OFF.current_team, 0x550)

    def __init__(self, memory):
        self.mem = memory
        self._off = None
        self._teams = {}       # pm -> team id
        self._local_team = 0
        self._next_at = 0.0
        self.last_status = "not read yet"

    # This is a guard against pointer-shaped garbage, NOT a claim about how
    # large a team id can be -- I could not find that documented, so it is
    # deliberately generous. A wrong offset lands on a neighbouring
    # reference field, and heap pointers in this process sit up around
    # 0x24A0_0000_0000 (~2.5e12, straight out of the live logs); 2^40 is
    # ~1.1e12, comfortably below that and far above any plausible id.
    #
    # Note "not a valid pointer" would be the WRONG test and was the first
    # attempt: a team id is a plain integer that passes a pointer range
    # check perfectly well. Magnitude is the only usable tell here, and the
    # real confirmation is the raw values printed in [AIM-TEAM].
    MAX_TEAM_ID = 1 << 40

    @classmethod
    def _plausible(cls, v):
        return v == 0 or 0 < v < cls.MAX_TEAM_ID

    def refresh(self, model, local_bp, pms, now):
        if self.mem is None or not legacy._valid_user_ptr(local_bp):
            return
        if now < self._next_at:
            return
        self._next_at = now + self.REFRESH

        bp_cache = getattr(model, '_pm_bp_cache', {}) or {}
        pairs = [(pm, bp_cache.get(pm, 0)) for pm in pms]
        pairs = [(pm, bp) for pm, bp in pairs if legacy._valid_user_ptr(bp)]

        try:
            if self._off is None:
                # Decide the offset on the local player, the one BasePlayer we
                # are certain about. One batch over both candidates, not a
                # loop of individual reads (see ARCHITECTURE.md 3.3) --
                # CANDIDATES has only two entries, but the pattern should
                # still be batch-first, not "loop until good enough".
                cand_vals = self.mem.batch_u64(
                    [local_bp + off for off in self.CANDIDATES], attempts=1,
                )
                for off, v in zip(self.CANDIDATES, cand_vals or ()):
                    if self._plausible(v):
                        self._off = off
                        break
                if self._off is None:
                    self.last_status = "no plausible currentTeam offset"
                    return

            addrs = [local_bp + self._off] + [bp + self._off for _, bp in pairs]
            vals = self.mem.batch_u64(addrs, attempts=1)
            if not vals or len(vals) < 1:
                return
            local_team = vals[0]
            if not self._plausible(local_team):
                self._off = None       # offset stopped making sense; re-decide
                self.last_status = "currentTeam read back as a pointer"
                return
            self._local_team = local_team
            self._teams = {
                pm: v for (pm, _), v in zip(pairs, vals[1:]) if self._plausible(v)
            }
            mates = sum(1 for v in self._teams.values()
                        if v and v == local_team)
            # Both candidates are printed, not just the winner: the two
            # dumps disagree and only a real teammate in-game settles it.
            seen = " ".join(
                f"[0x{o:X}]={self.mem.u64(local_bp + o)}"
                for o in self.CANDIDATES
            )
            self.last_status = (
                f"using 0x{self._off:X} my_team={local_team} "
                f"known={len(self._teams)} teammates={mates} | raw {seen}"
            )
        except Exception as exc:
            self.last_status = f"exception {exc!r}"

    def is_teammate(self, pm):
        if not self._local_team:
            return False                    # solo: nobody is a teammate
        return self._teams.get(pm, 0) == self._local_team


class ProjectileHoming:
    """Steers our own in-flight projectiles onto a target.

    Every tick, for each projectile we own: read where it is and how fast it
    is going, solve the launch velocity that would take it from THERE to the
    target (the same exact fixed-step solver the aimbot uses, so the curve it
    is steered onto is one the game's own integrator will actually fly), then
    rotate the current velocity toward that by a bounded step and write it
    back. Speed is preserved -- only direction changes.

    Two things to understand before turning this on.

    It is not the same class of trick as the rest of this project. ESP reads
    are invisible; this writes to game state that the SERVER re-simulates.
    Facepunch added verified periodic position updates for projectiles, and
    the server re-runs SimulateProjectile against the velocity it was told at
    launch. A projectile that deviates from that is a trajectory violation --
    logged, not merely ineffective. TURN_CAP_DPS exists because of this: a
    gentle correction stays inside the tolerance the server allows for sway
    and movement, a hard turn does not. Small nudges near the target are the
    usable regime; bending an arrow 90 degrees is not.

    The reference implementation additionally forges the "sent" trajectory
    state (prevSentPosition/prevSentVelocity/sentTraveledTime) so the server
    is fed a legal flight while the real one goes elsewhere. It does that
    from inside the process, in-line with the client's own update, so it
    cannot be raced. We are outside at ~46 Hz -- fast compared to the
    periodic sends, which is why steering works at all, but we are not
    synchronised with them, so that forgery is not attempted here.
    """

    TURN_CAP_DPS = 90.0    # default ceiling on how fast a projectile may turn
    MAX_RANGE = 300.0      # do not chase a target further than this
    ACQUIRE_DEG = 25.0     # only steer toward targets already near the path

    def __init__(self, memory, ballistics):
        self.mem = memory
        self._ballistics = ballistics
        self._last_at = 0.0
        self.last_status = "idle"
        self.steered = 0

    @staticmethod
    def _rotate_toward(cur, want, max_rad):
        """Rotate `cur` toward `want` by at most max_rad, keeping |cur|."""
        cm = math.sqrt(sum(c * c for c in cur))
        wm = math.sqrt(sum(c * c for c in want))
        if cm < 1e-6 or wm < 1e-6:
            return cur
        cu = [c / cm for c in cur]
        wu = [c / wm for c in want]
        dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(cu, wu))))
        ang = math.acos(dot)
        if ang <= 1e-6:
            return cur
        f = 1.0 if ang <= max_rad else max_rad / ang
        # Spherical interpolation, so the magnitude cannot drift as it turns.
        s = math.sin(ang)
        if s < 1e-6:
            return cur
        a = math.sin((1.0 - f) * ang) / s
        b = math.sin(f * ang) / s
        out = [a * cu[i] + b * wu[i] for i in range(3)]
        om = math.sqrt(sum(c * c for c in out))
        if om < 1e-6:
            return cur
        return tuple(c / om * cm for c in out)

    def tick(self, ga, local_bp, local_pos, players, now,
             speed_hint, drag, gravity_scale, base_gravity,
             turn_dps=None, bone_id=53):
        dt = now - self._last_at if self._last_at else 0.02
        self._last_at = now
        if dt <= 0.0 or dt > 0.5:
            dt = 0.02
        max_rad = math.radians(turn_dps or self.TURN_CAP_DPS) * dt
        gravity = (GRAVITY_MPS2 if base_gravity is None
                   else base_gravity) * gravity_scale

        ptrs = self._ballistics.own_in_flight(ga, local_bp)
        if not ptrs:
            self.last_status = "no projectile of mine in flight"
            self.steered = 0
            return 0

        alive = [p for p in players if not p.get("sleeping")]
        if not alive:
            self.last_status = "no target"
            self.steered = 0
            return 0

        steered = 0
        for pp in ptrs:
            state = self._ballistics.live_state(pp, speed_hint, local_pos)
            if state is None:
                self.last_status = "could not locate currentPosition/Velocity"
                continue
            pos, vel = state
            speed = math.sqrt(sum(c * c for c in vel))
            if speed < 1.0:
                continue

            best, best_ang = None, math.radians(self.ACQUIRE_DEG)
            fwd = [c / speed for c in vel]
            for pl in alive:
                # Same bone resolution (and same crouch/prone-aware fallback
                # chain) the aimbot uses, so both agree on where a player is.
                aim = AimEngine._resolve_aim_point(pl, bone_id)
                if aim is None:
                    continue
                d = [aim[i] - pos[i] for i in range(3)]
                dm = math.sqrt(sum(c * c for c in d))
                if dm < 0.5 or dm > self.MAX_RANGE:
                    continue
                dot = max(-1.0, min(1.0, sum(
                    fwd[i] * d[i] / dm for i in range(3))))
                ang = math.acos(dot)
                if ang < best_ang:
                    best_ang, best = ang, aim
            if best is None:
                continue

            # Solve from where the projectile IS, not from the muzzle: the
            # remaining flight is its own ballistic problem.
            dx, dz = best[0] - pos[0], best[2] - pos[2]
            gx = math.hypot(dx, dz)
            pitch = _solve_launch_pitch(
                gx, best[1] - pos[1], speed, gravity, drag)
            if pitch is None:
                continue
            want = (
                dx / gx * speed * math.cos(pitch),
                speed * math.sin(pitch),
                dz / gx * speed * math.cos(pitch),
            )
            new_vel = self._rotate_toward(vel, want, max_rad)
            if self._ballistics.write_velocity(pp, new_vel):
                steered += 1

        self.steered = steered
        if steered:
            self.last_status = f"steering {steered}/{len(ptrs)}"
        return steered


# ---------------------------------------------------------------------------
# AimEngine
# ---------------------------------------------------------------------------

class AimEngine:
    """Stateful aimbot: resolves the local PlayerInput chain once, then reads
    and writes bodyAngles every tick the aim key is held.

    Anti-detection:
      • Dual-write (bodyAngles + headAngles) for state consistency
      • Optional humanization layer (dynamic smooth, jitter, velocity cap,
        random skip, target stickiness)
    """

    # PlayerInput layout (verified via PI-DUMP probing in controller.py):
    #   +0x44  float  pitch  (bodyAngles.x)
    #   +0x48  float  yaw    (bodyAngles.y)
    PI_PITCH_OFF = 0x44
    PI_YAW_OFF = 0x48

    def __init__(self, memory, recoil_engine=None):
        self.mem = memory
        # Shares recoil_engine's already-resolved (belt walk -> Item.uid
        # match -> ItemDefinition.shortName) weapon identity, on its own
        # 0.5s cache -- so identifying "the current weapon is a bow" here
        # costs nothing extra. See _projectile_lead_for_weapon.
        self._recoil_engine = recoil_engine
        # Learns real arrow/bolt/nail physics off live projectiles; falls
        # back to PROJECTILE_TABLE until the first shot with a given weapon.
        self._ballistics = ProjectileBallistics(memory)
        # No open-loop drop/lead aim-point adjustment any more --
        # ProjectileHoming corrects the arrow onto the target continuously,
        # off its own live position, once it is in flight instead. That
        # made the upfront solve (and the jitter it needed VelocitySmoother
        # to fight) both redundant and no longer worth its own complexity.
        self._homing = ProjectileHoming(memory, self._ballistics)
        self._teams = TeamFilter(memory)
        self._team_said = ""
        # Cached pointers
        self._pi_addr = 0           # PlayerInput*
        self._pi_bp = 0             # the BasePlayer* it was resolved from
        self._pi_resolve_at = 0.0   # perf_counter of last successful resolve
        self._pi_resolve_ttl = 2.0  # re-resolve every 2 s (respawn-safe)
        # Direct LP chain resolution cache
        self._lp_cached = 0         # last successfully resolved local BP
        self._lp_resolve_at = 0.0   # when we last tried the direct walk
        self._lp_source = ""        # which fallback worked (for debug)
        # PlayerEyes cache for dual-write
        self._pe_addr = 0           # PlayerEyes native pointer
        self._pe_resolve_at = 0.0
        self._pe_resolve_ttl = 5.0  # PlayerEyes only invalidates on respawn
        # Rate limiting
        self._last_fire_at = 0.0
        self._last_write_dt = 0.016  # delta time between writes (for velocity cap)
        # Humanization state
        self._sticky_target_pm = 0   # PM pointer of the currently locked target
        self._sticky_until = 0.0     # don't switch until this perf_counter
        self._mouse_model = MousePhysicsModel()  # correlated tremor noise
        self._overshoot_active = False  # True = we overshot last tick, need to correct
        self._reaction_deadline = 0.0   # don't aim until this time (new target delay)
        self._last_target_pm = 0        # for detecting target switches
        # Debug print cadence
        self._next_debug_at = 0.0

    # ------------------------------------------------------------------
    # PlayerInput resolution (multi-fallback)
    # ------------------------------------------------------------------

    def _resolve_local_bp(self, model):
        """Resolve the local BasePlayer* with multiple fallback strategies.

        Returns (local_bp, source_name) or (0, "").
        """
        # Strategy 1: model.local_bp (set by _read_local_eye_offset)
        local_bp = getattr(model, 'local_bp', None) or 0
        if local_bp and legacy._valid_user_ptr(local_bp):
            return local_bp, "eye_reader"

        # Strategy 2: pm_bp_cache (the ESP's entity-walk mapping)
        local_pm = getattr(model, '_last_local_pm', None) or 0
        if local_pm:
            cache = getattr(model, '_pm_bp_cache', {})
            local_bp = cache.get(local_pm, 0)
            if local_bp and legacy._valid_user_ptr(local_bp):
                model.local_bp = local_bp
                return local_bp, "pm_bp_cache"

        # Strategy 3: walk LocalPlayer static chain directly
        local_bp = self._resolve_local_bp_direct(model)
        if local_bp and legacy._valid_user_ptr(local_bp):
            model.local_bp = local_bp
            return local_bp, "direct_chain"

        # Strategy 4: controller's old scan fallback
        local_bp = getattr(model, '_local_bp_scanned', 0) or 0
        if local_bp and legacy._valid_user_ptr(local_bp):
            return local_bp, "scanned"

        return 0, ""

    def _resolve_local_bp_direct(self, model):
        """Walk LocalPlayer_c → static_fields → HV → decrypt → BasePlayer*."""
        now = time.perf_counter()
        if self._lp_cached and legacy._valid_user_ptr(self._lp_cached):
            if now - self._lp_resolve_at < 5.0:
                return self._lp_cached
        if not self._lp_cached and now - self._lp_resolve_at < 1.0:
            return 0

        self._lp_resolve_at = now
        ga = getattr(model, 'ga', 0)
        if not ga:
            return 0
        m = self.mem

        lp_klass = m.u64_retry(ga + OFF.LocalPlayer_c)
        if not legacy._valid_user_ptr(lp_klass):
            self._lp_cached = 0
            return 0
        lp_sf = m.u64_retry(lp_klass + OFF.klass_static_fields)
        if not legacy._valid_user_ptr(lp_sf):
            self._lp_cached = 0
            return 0
        hv_wrapper = m.u64_retry(lp_sf + OFF.LocalPlayer_Entity)
        if not legacy._valid_user_ptr(hv_wrapper):
            self._lp_cached = 0
            return 0
        hv_raw = legacy._read_hv_handle(m, hv_wrapper, attempts=2)
        if not hv_raw:
            self._lp_cached = 0
            return 0
        hv_dec = legacy.decrypt_local_player(hv_raw)
        local_bp = legacy.resolve_tagged_handle(m, hv_dec, ga)
        if not legacy._valid_user_ptr(local_bp):
            self._lp_cached = 0
            return 0

        self._lp_cached = local_bp
        return local_bp

    def _resolve_pi(self, model):
        """Resolve LocalPlayer → BasePlayer → PlayerInput*."""
        now = time.perf_counter()
        local_bp, source = self._resolve_local_bp(model)

        if not local_bp:
            self._pi_addr = 0
            self._lp_source = ""
            return 0

        self._lp_source = source

        if (self._pi_addr
                and self._pi_bp == local_bp
                and now - self._pi_resolve_at < self._pi_resolve_ttl):
            return self._pi_addr

        pi = self.mem.u64_retry(local_bp + OFF.input, attempts=3)
        if not legacy._valid_user_ptr(pi):
            self._pi_addr = 0
            return 0

        self._pi_addr = pi
        self._pi_bp = local_bp
        self._pi_resolve_at = now
        return pi

    # ------------------------------------------------------------------
    # PlayerEyes resolution (for dual-write)
    # ------------------------------------------------------------------

    def _resolve_player_eyes(self, model):
        """Resolve the PlayerEyes native pointer for headAngles writes.

        Uses model.local_player_eyes (already cached by _read_local_eye_offset
        in legacy_runtime), or resolves via the HV chain if needed.
        """
        now = time.perf_counter()
        # Check cache
        if (self._pe_addr
                and legacy._valid_user_ptr(self._pe_addr)
                and now - self._pe_resolve_at < self._pe_resolve_ttl):
            return self._pe_addr

        # Try model's cached pointer first
        pe = getattr(model, 'local_player_eyes', None) or 0
        if pe and legacy._valid_user_ptr(pe):
            self._pe_addr = pe
            self._pe_resolve_at = now
            return pe

        # Resolve from local_bp + OFF.eyes → HV → decrypt → PlayerEyes*
        local_bp = self._pi_bp
        if not local_bp or not legacy._valid_user_ptr(local_bp):
            return 0
        ga = getattr(model, 'ga', 0)
        if not ga:
            return 0
        m = self.mem

        wrapper = m.u64_retry(local_bp + OFF.eyes, attempts=2)
        if not legacy._valid_user_ptr(wrapper):
            return 0
        hv_raw = legacy._read_hv_handle(m, wrapper, attempts=2)
        if not hv_raw:
            return 0
        hv_dec = legacy.decrypt_player_eyes(hv_raw)
        pe = legacy.resolve_tagged_handle(m, hv_dec, ga)
        if not legacy._valid_user_ptr(pe):
            return 0

        self._pe_addr = pe
        self._pe_resolve_at = now
        # Also set on model for other consumers
        model.local_player_eyes = pe
        return pe

    # ------------------------------------------------------------------
    # Angle read / write
    # ------------------------------------------------------------------

    def _read_angles(self, pi):
        """Read current (pitch, yaw) from PlayerInput.bodyAngles."""
        raw = self.mem.read(pi + self.PI_PITCH_OFF, 8)
        if not raw or len(raw) != 8:
            return None
        pitch, yaw = struct.unpack('<2f', raw)
        if not (math.isfinite(pitch) and math.isfinite(yaw)):
            return None
        if abs(pitch) > 90.5 or abs(yaw) > 400.0:
            return None
        return (pitch, yaw)

    def _write_angles(self, pi, pitch, yaw, model=None, dual=False):
        """Write bodyAngles (and optionally headAngles) atomically.

        When dual=True, also writes to PlayerEyes.headAngles at +0x60/+0x64
        to keep the client state consistent. This eliminates the 1-frame
        desync between PlayerInput and PlayerEyes that server integrity
        checks can detect.
        """
        pitch = _clamp_pitch(pitch)
        while yaw > 180.0:
            yaw -= 360.0
        while yaw < -180.0:
            yaw += 360.0

        data = struct.pack('<2f', pitch, yaw)
        ok_pi = self.mem.write(pi + self.PI_PITCH_OFF, data)

        ok_pe = True
        if dual and model is not None:
            pe = self._resolve_player_eyes(model)
            if pe:
                ok_pe = self.mem.write(
                    pe + OFF.eyes_head_angles_x, data
                )

        return ok_pi and ok_pe

    # ------------------------------------------------------------------
    # Target selection (world-space FOV cone)
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_aim_point(p, bone_id):
        """Find the best world-space aim point for a player.

        When the exact bone_id is available, use it directly.  When it is
        missing (e.g. head bone rejected by anchor gate, or rig not yet
        resolved), fall back through nearby bones with a small vertical
        offset to approximate the requested target.

        Every bone position already accounts for crouching/prone because
        it comes from the game's own transform hierarchy.

        Fallback chain (head=53 selected):
          53 → 52+0.10m → 23+0.25m → pos+1.5m
        Fallback chain (neck=52 selected):
          52 → 23+0.15m → pos+1.4m
        Fallback chain (chest=23 selected):
          23 → pos+1.2m
        """
        bones = p.get("bones")
        if bones and bone_id in bones:
            return bones[bone_id]

        if bone_id == 47:
            if bones:
                if 52 in bones:
                    neck = bones[52]
                    return (neck[0], neck[1] + 0.10, neck[2])
                if 23 in bones:
                    sp4 = bones[23]
                    return (sp4[0], sp4[1] + 0.25, sp4[2])
            pos = p.get("pos")
            return (pos[0], pos[1] + 1.5, pos[2]) if pos else None

        if bone_id == 52:
            if bones and 23 in bones:
                sp4 = bones[23]
                return (sp4[0], sp4[1] + 0.15, sp4[2])
            pos = p.get("pos")
            return (pos[0], pos[1] + 1.4, pos[2]) if pos else None

        # chest (23) or any other bone
        pos = p.get("pos")
        return (pos[0], pos[1] + 1.2, pos[2]) if pos else None

    def select_target(self, players, eye_origin, cur_pitch, cur_yaw,
                      fov_deg, bone_id=53, sticky_pm=0, sticky_until=0.0,
                      max_dist=0.0, dist_bias=0.0, is_teammate=None,
                      exclude_npcs=True):
        """Pick the best player inside a world-space FOV cone.

        Ranking is angular distance from the crosshair, optionally biased
        toward nearer players. Pure angle is what made it lock "someone
        else": a player 200 m out but dead centre beats the one at 20 m and
        three degrees off, even though the near one is obviously who you are
        shooting at. `dist_bias` scales that -- 0 restores pure angle.

        `max_dist` drops targets beyond a range entirely (0 = no limit),
        `is_teammate(pm)` drops your own team, and `exclude_npcs` drops
        scientists/zombies/etc (see legacy_runtime.NpcClassifier) -- their
        `is_npc` flag is already on the player dict, no separate lookup
        needed the way team status needs TeamFilter.

        A sticky target still in the cone wins over a closer one, so the pick
        does not flap between two players at similar angles.
        """
        now = time.perf_counter()
        best = None
        best_pos = None
        best_score = float('inf')

        # If sticky target is still alive and in FOV, keep it
        sticky_candidate = None
        sticky_head = None

        for p in players:
            if p.get("sleeping"):
                continue
            if exclude_npcs and p.get("is_npc", False):
                continue
            pm = p.get("pm", 0)
            if is_teammate is not None and is_teammate(pm):
                continue
            head = self._resolve_aim_point(p, bone_id)
            if head is None:
                continue

            tgt = _target_angles(eye_origin, head)
            if tgt is None:
                continue
            tgt_pitch, tgt_yaw = tgt
            ang_dist = _angular_distance(cur_pitch, cur_yaw, tgt_pitch, tgt_yaw)
            if ang_dist >= fov_deg:
                continue

            dist = math.dist(eye_origin, head)
            if max_dist > 0.0 and dist > max_dist:
                continue

            # Check if this is the sticky target
            if (pm == sticky_pm
                    and sticky_pm != 0
                    and now < sticky_until):
                sticky_candidate = p
                sticky_head = head

            # Additive, in degrees: `dist_bias` degrees of penalty per 25 m
            # of range. Multiplying instead made the penalty proportional to
            # the angle, so the far target -- which is far BECAUSE it sits
            # near the crosshair -- barely paid it: at bias 0.5 a player at
            # 200 m and 1 deg still beat one at 20 m and 3 deg, i.e. exactly
            # the case this exists to fix.
            score = ang_dist + dist_bias * dist / 25.0
            if score < best_score:
                best_score = score
                best = p
                best_pos = head

        # Prefer sticky target if valid
        if sticky_candidate is not None:
            return sticky_candidate, sticky_head
        return best, best_pos

    # ------------------------------------------------------------------
    # Humanization layer
    # ------------------------------------------------------------------

    @staticmethod
    def _natural_velocity_profile(ang_dist, max_smooth):
        """Smoothstep velocity profile — natural S-curve.

        Replaces the discrete-threshold curve which ML classifiers can
        identify by its sharp transitions.  Uses cubic smoothstep:
            speed = max_smooth * (3t² - 2t³)
        where t = clamp(ang_dist / 15°, 0, 1).

        Result: slow start → fast ballistic mid → slow corrective end.
        Random ±15% per-tick variation adds entropy.
        """
        t = min(ang_dist / 15.0, 1.0)
        # Cubic smoothstep — smooth at both ends, fast in the middle
        speed = max_smooth * (3.0 * t * t - 2.0 * t * t * t)
        # Per-tick entropy (±15%) so the curve isn't perfectly repeatable
        speed *= 0.85 + random.random() * 0.30
        return max(0.01, min(speed, max_smooth))

    def _apply_overshoot(self, d_pitch, d_yaw, ang_dist):
        """Simulate human overshoot + micro-correction.

        Humans almost never converge monotonically on a target — they
        overshoot by 20-40% then correct.  This is the #1 signal that
        LSTM classifiers use to distinguish bots from humans.

        Only triggers during the final approach (<5°) where overshoot
        is visible.  15% probability per tick.
        """
        if ang_dist > 5.0:
            # Too far — overshoot wouldn't be realistic
            self._overshoot_active = False
            return d_pitch, d_yaw

        if self._overshoot_active:
            # We overshot last tick — apply a smaller correction back
            self._overshoot_active = False
            return d_pitch * 0.6, d_yaw * 0.6

        # 15% chance to overshoot
        if random.random() < 0.15:
            self._overshoot_active = True
            extra = 1.0 + random.uniform(0.20, 0.40)
            return d_pitch * extra, d_yaw * extra

        return d_pitch, d_yaw

    @staticmethod
    def _clamp_velocity(d_pitch, d_yaw, dt, max_dps):
        """Limit angular velocity to human-plausible speeds.

        max_dps=0 → unlimited (ragebot mode).
        Human range: sustained tracking ~80-150°/s, fast flick ~300-500°/s.
        """
        if max_dps <= 0.0 or dt <= 0.0:
            return d_pitch, d_yaw
        max_delta = max_dps * dt
        total = math.sqrt(d_pitch * d_pitch + d_yaw * d_yaw)
        if total > max_delta and total > 1e-6:
            scale = max_delta / total
            return d_pitch * scale, d_yaw * scale
        return d_pitch, d_yaw

    # ------------------------------------------------------------------
    # Main tick
    # ------------------------------------------------------------------

    def tick(self, model, players, vs):
        """Called every frame while the aim key is held.

        `vs` is the view_base.Settings instance.
        Returns True if an angle write happened, False otherwise.
        """
        now = time.perf_counter()
        dbg = vs.aim_debug_prints
        cadence_ok = now >= self._next_debug_at

        # Rate limiting (with optional jitter for anti-detection)
        base_interval = max(0.005, vs.aim_interval_ms / 1000.0)
        if vs.aim_humanize:
            # Add ±30% random jitter to the interval
            interval_s = base_interval * (0.7 + random.random() * 0.6)
        else:
            interval_s = base_interval
        if now - self._last_fire_at < interval_s:
            return False

        # Random skip (humanization only)
        if vs.aim_humanize and vs.aim_skip_pct > 0.0:
            if random.random() < vs.aim_skip_pct:
                self._last_fire_at = now  # still count as a "tick" for rate
                return False

        # Resolve PlayerInput chain
        pi = self._resolve_pi(model)
        if not pi:
            if dbg and cadence_ok:
                self._next_debug_at = now + 0.5
                local_bp = getattr(model, 'local_bp', None)
                local_pm = getattr(model, '_last_local_pm', None)
                cache_bp = 0
                if local_pm:
                    cache_bp = getattr(model, '_pm_bp_cache', {}).get(local_pm, 0)
                print(
                    f"[AIM-FAIL] PI resolution failed — ALL 4 strategies: "
                    f"model.local_bp={('0x%X' % local_bp) if local_bp else 'None'} "
                    f"local_pm={('0x%X' % local_pm) if local_pm else 'None'} "
                    f"cache_bp={('0x%X' % cache_bp) if cache_bp else 'None'} "
                    f"direct_lp=0x{self._lp_cached:X} "
                    f"OFF.input=0x{OFF.input:X} "
                    f"OFF.LocalPlayer_c=0x{OFF.LocalPlayer_c:X}",
                    flush=True,
                )
            return False

        # Read current angles
        cur = self._read_angles(pi)
        if cur is None:
            if dbg and cadence_ok:
                self._next_debug_at = now + 0.5
                raw = self.mem.read(pi + self.PI_PITCH_OFF, 8)
                hexd = raw.hex() if raw else "None"
                print(
                    f"[AIM-FAIL] read_angles failed: pi=0x{pi:X} "
                    f"raw[+0x44..+0x4C]={hexd}",
                    flush=True,
                )
            return False
        cur_pitch, cur_yaw = cur

        # Eye origin for angle computation
        local_pos = getattr(model, 'local_pos', None)
        eye_off = getattr(model, 'local_eye_offset', (0.0, 1.6, 0.0))
        if local_pos is None:
            if dbg and cadence_ok:
                self._next_debug_at = now + 0.5
                print("[AIM-FAIL] model.local_pos is None", flush=True)
            return False
        eye_origin = (
            local_pos[0] + eye_off[0],
            local_pos[1] + eye_off[1],
            local_pos[2] + eye_off[2],
        )

        # Teammates are filtered here rather than upstream so the ESP still
        # draws them -- you want to see your team, just not aim at them.
        local_bp_for_team = self._resolve_local_bp(model) or 0
        if getattr(vs, 'aim_team_check', True):
            self._teams.refresh(
                model, local_bp_for_team,
                [p.get("pm", 0) for p in players], now,
            )
            teammate_fn = self._teams.is_teammate
            if dbg and cadence_ok and self._teams.last_status != self._team_said:
                self._team_said = self._teams.last_status
                print(f"[AIM-TEAM] {self._teams.last_status}", flush=True)
        else:
            teammate_fn = None

        # Manual lock: while the lock key is held, the current target is kept
        # no matter what else walks into the cone. This is the direct answer
        # to "lock the one I actually want" -- the scoring below is a
        # heuristic, and a heuristic will always pick wrong sometimes.
        lock_key = getattr(vs, 'aim_lock_key', 0)
        hard_locked = bool(lock_key) and _key_down(lock_key)
        if hard_locked and self._sticky_target_pm:
            locked = next((p for p in players
                           if p.get("pm", 0) == self._sticky_target_pm
                           and not p.get("sleeping")), None)
            if locked is not None:
                head = self._resolve_aim_point(locked, vs.aim_head_bone_id)
                if head is not None:
                    target, head_pos = locked, head
                else:
                    hard_locked = False
            else:
                hard_locked = False
        else:
            hard_locked = False

        # Select target inside world FOV cone (with stickiness)
        if not hard_locked:
            target, head_pos = self.select_target(
                players, eye_origin,
                cur_pitch, cur_yaw,
                vs.aim_fov_deg,
                bone_id=vs.aim_head_bone_id,
                max_dist=getattr(vs, 'aim_max_distance_m', 0.0),
                dist_bias=getattr(vs, 'aim_distance_bias', 0.5),
                is_teammate=teammate_fn,
                exclude_npcs=getattr(vs, 'aim_exclude_npcs', True),
                # NOT gated on aim_humanize. Holding a target is aim
                # correctness, not a human-imitation flourish: with it off the
                # pick was redone from scratch every tick and flapped between
                # players in the cone (observed 35m -> 22m -> 37m -> 30m on
                # consecutive ticks, each switch restarting the approach 3-4
                # degrees out). Smoothing then needs several ticks to
                # converge, and anything fired during that travel misses.
                # Humanize still adds its reaction delay on top.
                sticky_pm=self._sticky_target_pm,
                sticky_until=self._sticky_until,
            )
        if target is None or head_pos is None:
            if dbg and cadence_ok:
                self._next_debug_at = now + 0.5
                n_alive = sum(1 for p in players if not p.get("sleeping"))
                print(
                    f"[AIM-FAIL] no target in FOV cone: "
                    f"fov={vs.aim_fov_deg:.1f}° "
                    f"cur=({cur_pitch:+.2f}, {cur_yaw:+.2f}) "
                    f"players={len(players)} alive={n_alive} "
                    f"bone={vs.aim_head_bone_id}",
                    flush=True,
                )
            return False

        # Update target stickiness + reaction time
        target_pm = target.get("pm", 0)
        self._sticky_target_pm = target_pm
        # Refreshed EVERY tick we are tracking this target, not only when the
        # target changes. Renewing only on change meant the window expired
        # 350 ms into a lock and never came back, so the pick went free again
        # for the rest of the engagement -- stickiness covered the opening
        # third of a second and nothing after it. The lock still breaks by
        # itself: select_target only honours it while the target is inside
        # the FOV cone, so leaving the cone or dying releases it, and the
        # window is what keeps them preferred briefly after that.
        self._sticky_until = now + (vs.aim_sticky_ms / 1000.0)

        # Reaction time delay (humanization) — simulate human reaction
        # to a NEW target appearing.  150-300ms delay before tracking.
        if vs.aim_humanize and target_pm != self._last_target_pm:
            self._last_target_pm = target_pm
            react_ms = getattr(vs, 'aim_reaction_ms', 200)
            react_s = react_ms / 1000.0
            # Random ±30% around the configured value
            self._reaction_deadline = now + react_s * (0.7 + random.random() * 0.6)
            self._overshoot_active = False  # reset overshoot state
        elif not vs.aim_humanize:
            self._last_target_pm = target_pm

        if vs.aim_humanize and now < self._reaction_deadline:
            return False  # still "reacting" — don't aim yet

        # Weapon identification for homing -- bow/crossbow/nailgun only.
        # There is no more separate aim-point adjustment here: the aimbot
        # aims straight at the current target position, and ProjectileHoming
        # (below) does 100% of the ballistics compensation, continuously,
        # from the arrow's own live position -- which makes an upfront
        # open-loop drop/lead solve redundant on top of it. This block's
        # only job now is: identify the weapon, and learn its real
        # speed/drag/gravityModifier off a live arrow so homing's own
        # correction solve uses real numbers instead of the hardcoded table.
        # Every skip below reports itself. A silent skip is indistinguishable
        # from a broken offset chain: this block quietly did nothing for bows
        # because _resolve_weapon returned "" whenever heldEntity read null,
        # and there was no way to tell that from "you are holding a rifle".
        skip_reason = None
        weapon_key = None
        if not getattr(vs, 'aim_projectile_homing', False):
            skip_reason = "homing turned off in settings -- nothing to feed"
        elif self._recoil_engine is None:
            skip_reason = "no recoil_engine -- weapon identity unavailable"
        else:
            _, weapon_key = self._recoil_engine._resolve_weapon(model)
            proj = PROJECTILE_TABLE.get(weapon_key)
            if proj is None:
                skip_reason = (
                    f"weapon not identified -- {getattr(self._recoil_engine, 'last_status', 'no status')}"
                    if not weapon_key else
                    "hitscan weapon, nothing for homing to steer"
                )
            else:
                # Measured values (read off a real arrow in flight) beat the
                # hardcoded guess as soon as one shot has been fired with
                # this weapon. The scan is throttled internally.
                # local_bp is what tells our arrows from everyone else's in
                # the shared projectile list.
                local_bp = getattr(model, 'local_bp', 0) or 0
                if not legacy._valid_user_ptr(local_bp):
                    local_pm = getattr(model, '_last_local_pm', 0) or 0
                    if local_pm:
                        local_bp = getattr(model, '_pm_bp_cache', {}).get(local_pm, 0)
                self._ballistics.observe(
                    getattr(model, 'ga', 0), weapon_key, now, local_bp,
                )
                learned = self._ballistics.learned(weapon_key)
                speed, drag, gravity_scale = learned if learned is not None else proj
                if dbg and cadence_ok:
                    self._next_debug_at = now + 0.5
                    src = "measured" if learned is not None else "table"
                    why = "" if learned is not None else \
                        f" why_table='{self._ballistics.last_status}'"
                    print(
                        f"[AIM-BALLISTIC] weapon={weapon_key} src={src}{why} "
                        f"speed={speed:.1f}m/s drag={drag:.3f} gmod={gravity_scale:.2f}",
                        flush=True,
                    )

                # GRAVITY_MPS2 is now purely internal to homing's own
                # continuous correction solve -- there is no more user-facing
                # "Gravity" setting. A closed loop that re-aims every tick off
                # the target's ACTUAL live position is self-correcting for a
                # slightly-wrong gravity constant in a way the old one-shot
                # open-loop drop solve never was, so tuning it stopped being
                # worth exposing.
                n = self._homing.tick(
                    getattr(model, 'ga', 0), local_bp, local_pos, players,
                    now, speed, drag, gravity_scale, GRAVITY_MPS2,
                    turn_dps=getattr(vs, 'aim_homing_turn_dps', 60.0),
                    bone_id=vs.aim_head_bone_id,
                )
                if dbg and n and cadence_ok:
                    print(
                        f"[AIM-HOMING] {self._homing.last_status} "
                        f"cap={getattr(vs, 'aim_homing_turn_dps', 60.0):.0f}deg/s",
                        flush=True,
                    )

        if skip_reason is not None and dbg and cadence_ok:
            self._next_debug_at = now + 0.5
            print(
                f"[AIM-BALLISTIC] skipped: {skip_reason} "
                f"(weapon={weapon_key!r}, known={sorted(PROJECTILE_TABLE)})",
                flush=True,
            )

        # Compute absolute target angles
        tgt = _target_angles(eye_origin, head_pos)
        if tgt is None:
            return False
        tgt_pitch, tgt_yaw = tgt

        # Apply tremor noise (humanization only)
        # Uses MousePhysicsModel (temporally-correlated Perlin noise)
        # instead of gaussian IID which ML classifiers flag.
        if vs.aim_humanize and vs.aim_jitter_deg > 0:
            tr_p, tr_y = self._mouse_model.tremor(now, vs.aim_jitter_deg)
            tgt_pitch += tr_p
            tgt_yaw += tr_y

        # Deadzone check
        ang_dist = _angular_distance(cur_pitch, cur_yaw, tgt_pitch, tgt_yaw)
        if ang_dist < vs.aim_deadzone_deg:
            return False

        # Smoothing — recoil-aware
        #
        # The core problem: AK recoil adds ~2-3° per tick to bodyAngles.
        # With smooth=0.35, we only correct 35% of the delta each tick,
        # so recoil accumulates faster than we correct → aim drifts above
        # the head.
        #
        # Fix:
        #   Ragebot (humanize OFF): write ABSOLUTE target angles every tick.
        #     No delta, no smooth — we overwrite recoil completely. The aim
        #     locks perfectly on the bone because recoil only lives for 1
        #     frame before we crush it.
        #   Human (humanize ON): detect recoil (pitch drifting up from target)
        #     and boost the smooth factor to overpower it.

        if not vs.aim_humanize:
            # RAGEBOT: absolute angle write — ignore recoil entirely
            # Apply user's smooth as a blending factor: 1.0 = perfect lock,
            # 0.5 = still very tight (recoil visible for ~1 frame)
            s = max(0.01, min(1.0, vs.aim_smooth))
            # The [AIM-WRITE] debug line below prints the blend factor for
            # both modes, so it has to be bound on this path too -- reading
            # it here when only the humanize branch assigned it was an
            # UnboundLocalError that killed the whole overlay mid-session.
            smooth = s
            if s >= 0.8:
                # Near-snap mode: write exact target angles
                new_pitch = _clamp_pitch(tgt_pitch)
                new_yaw = tgt_yaw
            else:
                # Partial smooth but with recoil detection
                d_pitch = (tgt_pitch - cur_pitch) * s
                d_yaw = _shortest_yaw(cur_yaw, tgt_yaw) * s
                # Recoil boost: if we're ABOVE the target (recoil pushed up,
                # pitch is more negative), double the pitch correction
                if cur_pitch < tgt_pitch - 0.5:
                    d_pitch *= 2.0
                new_pitch = _clamp_pitch(cur_pitch + d_pitch)
                new_yaw = cur_yaw + d_yaw
        else:
            # HUMAN: smoothstep + recoil compensation
            smooth = self._natural_velocity_profile(ang_dist, vs.aim_smooth)

            d_pitch = (tgt_pitch - cur_pitch) * smooth
            d_yaw = _shortest_yaw(cur_yaw, tgt_yaw) * smooth

            # Recoil detection: if pitch delta is > 1° and we're above
            # target, boost correction by 1.5x to fight recoil drift
            if (tgt_pitch - cur_pitch) > 1.0:
                d_pitch *= 1.5

            # Overshoot simulation
            if getattr(vs, 'aim_overshoot', True):
                d_pitch, d_yaw = self._apply_overshoot(d_pitch, d_yaw, ang_dist)

            # Angular velocity cap
            if vs.aim_max_speed_dps > 0:
                dt = now - self._last_fire_at
                if dt > 0.0:
                    d_pitch, d_yaw = self._clamp_velocity(
                        d_pitch, d_yaw, dt, vs.aim_max_speed_dps
                    )

            new_pitch = _clamp_pitch(cur_pitch + d_pitch)
            new_yaw = cur_yaw + d_yaw

        # Write (single or dual)
        ok = self._write_angles(
            pi, new_pitch, new_yaw,
            model=model,
            dual=vs.aim_dual_write,
        )

        self._last_write_dt = now - self._last_fire_at if self._last_fire_at else 0.016
        self._last_fire_at = now

        # Debug logging
        if dbg and cadence_ok:
            self._next_debug_at = now + 0.15
            name = target.get('name', '?')
            dist_m = math.sqrt(
                (head_pos[0] - eye_origin[0]) ** 2 +
                (head_pos[1] - eye_origin[1]) ** 2 +
                (head_pos[2] - eye_origin[2]) ** 2
            )
            # Read-back test
            readback = self._read_angles(pi)
            if readback:
                rb_p, rb_y = readback
                rb_str = f"rb=({rb_p:+.2f},{rb_y:+.2f})"
                match_p = abs(rb_p - new_pitch) < 0.1
                match_y = abs(_shortest_yaw(rb_y, new_yaw)) < 0.1
                rb_str += " ok" if (match_p and match_y) else " OVERWRITTEN"
            else:
                rb_str = "rb=FAIL"

            mode = "HUMAN" if vs.aim_humanize else "RAW"
            dual_str = "+PE" if vs.aim_dual_write and self._pe_addr else ""
            print(
                f"[AIM-WRITE] [{mode}{dual_str}] {name!r} "
                f"cur=({cur_pitch:+.2f},{cur_yaw:+.2f}) "
                f"-> ({new_pitch:+.2f},{new_yaw:+.2f}) "
                f"d={ang_dist:.2f}deg s={smooth:.3f} "
                f"{dist_m:.0f}m ok={ok} "
                f"src={self._lp_source} {rb_str}",
                flush=True,
            )

        return ok
