"""Model layer: coherent player and skeleton snapshots.

Uses the 'stable bones' TransformInternal bulk-buffer method:
  1. Rig resolve: Model → boneTransforms[] → Transform → native → TransformAccess
  2. Sample: bulk-read TRS buffer + parent indices, walk chains locally (zero extra reads)
"""

import math
import struct
try:
    import numpy as _np
except ImportError:  # falls back to the pure-Python skeleton path
    _np = None
import threading
import time

from . import legacy_runtime as legacy
from .legacy_runtime import (
    Mem,
    find_modules,
    print_module_diagnostics,
    read_view_proj_matrix,
)


BONE_MIN_VALID = 12
BONE_MAX_INDEX = 4096
# _sample_bones_multi resolves every tracked player in ONE batch_u64, so the
# round-robin that used to hand out 4 players per tick no longer buys anything —
# it only meant most players had a stale or missing skeleton ("les bones ne sont
# pas sur tout le monde"). Sample the whole tracked set every tick instead.
#
# The cap below is the *same* idea applied one level up, and it was still set
# for the old cost model. Measured 2026-08-25: the driver charges ~14 ms per
# call whatever the payload, so 16 players and 48 players in one batch cost the
# same single IOCTL — only the address count grows (~132 addresses per player:
# ~22 slots x 6 u64 per TRS). At 16 the log read `cand=39 sampled=16` with
# `drawn=24 sk=16`: two thirds of the visible players had a box and no
# skeleton, for no saving at all. 48 stays comfortably inside one batch
# (batch_u64 splits at 16000 addresses, i.e. ~121 players) and covers every
# candidate inside SKELETON_MAX_DISTANCE on a normal server.
# The one-shot Model.boneNames dump. It is the tool that settled the bone IDs
# against live ground truth instead of a stale dump (see
# [[verify-bone-ids-against-name-array]]), and it stays for the next build --
# but 94 lines in every single session's console is noise in every log anyone
# has to read, and unreadable logs are how real signals get missed.
ENABLE_BONE_NAME_DUMP = False

BONE_MAX_TRACKED_PLAYERS = 48
BONE_MAX_PLAYERS_PER_BATCH = BONE_MAX_TRACKED_PLAYERS
# How many brand-new rig jobs _resolve_bone_slots_batch is allowed to admit
# in a single tick. Admission itself used to be uncapped: when the rig cache
# invalidates for many players at once (local-player flag flip, a big
# pm_to_bp remap after a scene change), every affected pm got a fresh job in
# the same tick, and they all then advance stage-in-lockstep — so their
# "elements"/"native"/"access" stages (up to ~len(SCI_BONE_IDS)*2 reads each)
# land in the same tick together too. Measured live: a 17-job burst produced
# a single tick costing 300-980ms (io calls jumping from the normal 2-4 up to
# 40-66). Staggering admission spreads that burst back out over several
# ticks instead of paying it all at once. 6 matches the steady-state
# cand=6/sampled=6/queued=6 throughput already seen in normal operation.
BONE_MAX_NEW_JOBS_PER_TICK = 6
# Rig resolution retried every 0.35 s forever, with no failure counter. Some
# entities in the PlayerModel list simply have no usable skeleton -- a corpse,
# a bot on a different model, a player still streaming in -- so those pms sat
# in a permanent 0.35 s loop: that is the steady `norig=5..7` with `job=0` in
# the logs. Each retry also consumed one of the six admission slots per tick,
# starving players who *could* have resolved. Back off per pm instead, and
# reset the moment one succeeds. See OFFSET_RECOVERY.md trap #46.
BONE_RIG_RETRY_BASE = 0.35
BONE_RIG_RETRY_MAX = 20.0
# Consecutive ticks a cached BasePlayer must read playerModel == 0 before the
# pm->bp pair is evicted. One zero is a missed batch entry; a run of them is a
# BasePlayer the game destroyed when that player respawned into a new one.
NULL_BACKREF_TICKS = 3
# A player whose BasePlayer the game destroyed is dead: the skeleton goes the
# same tick, but the PlayerModel stays in the list (and its box on screen) for
# up to a second more. The pm is hidden until it is alive again, which is one of:
# a new BasePlayer mapped to it, or its position teleporting (a respawn) --
# DEAD_RESPAWN_JUMP between two reads is far beyond any ragdoll. DEAD_MARK_MAX
# only bounds a mark nothing ever cleared.
DEAD_RESPAWN_JUMP = 4.0
DEAD_MARK_MAX = 30.0

# Above this, _read_player_frame_batch prints a [POS-DBG] breakdown of its own
# phases. [TICK-LATENCY] only reports the whole thing as one pos= number, which
# is not enough to tell a slow BasePlayer mapping apart from a slow rig resolve
# or a slow bone sample. Only slow ticks log, so the steady state stays quiet.
POS_PHASE_LOG_MS = 120.0
# 0.0 = sample every tick, unconditionally. This gate can only ever *skip* a
# bone sample, never make one cheaper: the whole tracked set rides in one
# batch_u64 either way (see BONE_MAX_TRACKED_PLAYERS). At the current 60-90 ms
# tick it never fires, but it is a trap waiting for the tick to get faster --
# which is the entire goal. The tt/ branch runs 0.0 for the same reason.
BONE_SAMPLE_INTERVAL = 0.0
BONE_ANCHOR_RADIUS = 4.5
BONE_ANCHOR_RADIUS_SQ = BONE_ANCHOR_RADIUS * BONE_ANCHOR_RADIUS
# Extra metres of anchor tolerance granted per second of tick time, capped.
# See the anchor_radius comment in _sample_bones_multi: a long tick makes the
# networked position and the rendered pose legitimately disagree, and a fixed
# radius turns that into a rig-invalidation storm.
BONE_ANCHOR_SLACK_MAX = 8.0
# Beyond this, an anchor rejection is not "the player moved faster than the
# adaptive radius allows" -- it is a broken rig. The radius tops out around
# 12.5 m (BONE_ANCHOR_RADIUS + BONE_ANCHOR_SLACK_MAX) and even a 15 m/s
# vehicle desynced for a full bone TTL is under 20 m. Live 2026-09-05 caught
# a skeleton composing 2432 m from its anchor, in the same tick as stale=1
# (the hierarchy pointers moved between plan and read) and rej quat=7: the
# TRS words described a rig that no longer existed. 'anchor_only' takes no
# corrective action by design, so that player's pose simply stopped updating
# until it healed on its own -- the skeleton froze, then expired past the TTL
# and vanished ([BONE-AGE] expired=1, worst 316 ms, in that same tick).
# Squared, to compare against offset_sq without a sqrt.
BONE_ANCHOR_BROKEN = 50.0
BONE_ANCHOR_BROKEN_SQ = BONE_ANCHOR_BROKEN * BONE_ANCHOR_BROKEN
BP_MAPPING_RETRY_INTERVAL = 2.0
# _resolve_pm_to_bp's fallback walks the entire live BaseNetworkable buffer
# (thousands of entities) when any player is still unmapped — expensive, so
# back off exponentially for players it keeps failing on instead of paying
# that cost on a fixed 2s cadence forever.
BP_MAPPING_MAX_BACKOFF = 30.0
# Hard floor between two BaseNetworkable walks, whatever else says go.
# Trap #30 let a never-tried pm bypass the retry backoff so a spawning squad
# would not wait out a 30 s timer. On a busy server the player list churns
# every tick, so there is *always* a never-tried pm -- and the bypass turned
# into "walk the whole buffer continuously": the 2026-08-25 log showed
# `bg=11x/622ms/busy` with `bp_src` permanently stuck on `base_networkable`,
# i.e. a ~620 ms driver walk running back to back forever. The floor keeps
# trap #30's benefit (a new player waits at most this long, not 30 s) while
# capping the cost at one walk per interval. See trap #43.
BP_MAPPING_MIN_INTERVAL = 0.75
# How long a pm stays "already attempted" after it drops out of the player
# list. `LC count` bounces every single tick (7, 9, 10, 13, 14, 15 ...) as
# PlayerModels flicker in and out, and pruning the attempted-set against the
# live roster meant a flickering pm was forgotten and counted as brand new the
# moment it came back -- which re-armed the freshness bypass roughly once per
# floor interval. Measured: `bg=26x` at ~650 ms a walk against a 0.75 s floor,
# i.e. the background thread walking the buffer ~83% of the time. A player who
# is genuinely gone this long is a reconnect and *should* count as new; one
# that blinked for three ticks is the same player. See trap #44.
BP_ATTEMPT_MEMORY = 12.0

# How long a cached "this player is asleep" survives with no fresh playerFlags
# read before it is discarded. The seed exists so one failed read does not make
# a sleeping player flicker awake; the bound exists because an unbounded seed
# is how a stale True outlived the player it described.
SLEEPING_STALE_TTL = 3.0
# TransformInternal trsX struct: Vec3 t (12) + pad(4) + Vec4 q (16) + Vec3 s (12) + pad(4) = 48 bytes
TRSX_SIZE = 48
TRSX_WORD_OFFSETS = tuple(range(0, TRSX_SIZE, 8))
_TRSX_WORD_OFFSETS_NP = (
    _np.array(TRSX_WORD_OFFSETS, dtype=_np.uint64) if _np is not None else None
)
TRSX_MAX_CAPACITY = 5000
PARENT_WALK_LIMIT = 200
# Parent indices describe rig topology, not pose, so they survive between
# frames. Re-reading them every tick was pure overhead.
PARENT_INDEX_CACHE_TTL = 3.0
# Same reasoning, same lifetime, for the hierarchy's TRS/parent pointers
# (see RustGameModel._hierarchy_ptr_cache). It is only a safety net: the
# entry is refreshed every tick for every player actually being sampled,
# so the TTL only ever expires one that stopped being drawn.
HIERARCHY_PTR_CACHE_TTL = 3.0
# ── [HEALTH-RAW] ─────────────────────────────────────────────────────────
# What is known so far: [HEALTH-DBG] reports read=fresh=49 cached=0 on every
# tick, so the three words at OFF.lifestate are being read and accepted
# every time and nothing is being served stale. And a 44-second recording
# (2026-08-27) contains not one health bar that ever leaves full green,
# while the player's own HUD counts Hits going 20 -> 27 and Kills 4 -> 6 in
# the same clip. Read correctly, accepted every tick, and constant: the
# field we decode is simply not the field that changes.
#
# So dump the memory around it and let one shot identify the right offset.
# The window starts before lifestate because _health could as easily sit
# below it as above, and 12 words is 96 bytes -- one player, once a second,
# next to a batch that already carries hundreds of addresses.
HEALTH_PROBE_BACK = 0x20
HEALTH_PROBE_WORDS = 12
HEALTH_PROBE_INTERVAL = 1.0


def _normalize_quaternion(value):
    if not all(math.isfinite(c) for c in value):
        return None
    norm_sq = sum(c * c for c in value)
    if norm_sq < 0.25 or norm_sq > 4.0:
        return None
    inv = 1.0 / math.sqrt(norm_sq)
    return (value[0] * inv, value[1] * inv, value[2] * inv, value[3] * inv)


def _rotate_vector(q, v):
    """Rotate vector v by normalized Unity quaternion q in XYZW order."""
    qx, qy, qz, qw = q
    vx, vy, vz = v
    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)
    return (
        vx + qw * tx + (qy * tz - qz * ty),
        vy + qw * ty + (qz * tx - qx * tz),
        vz + qw * tz + (qx * ty - qy * tx),
    )


def _parse_trs_buffer(raw_bytes, count):
    """Parse a raw TRS buffer into a list of (t, q, s) tuples.

    trsX layout (48 bytes):
      Vec3 t  @ 0x00 (12 bytes)
      pad     @ 0x0C (4 bytes)
      Vec4 q  @ 0x10 (16 bytes)  -- XYZW
      Vec3 s  @ 0x20 (12 bytes)
      pad     @ 0x2C (4 bytes)
    """
    result = [None] * count
    for i in range(count):
        off = i * TRSX_SIZE
        if off + TRSX_SIZE > len(raw_bytes):
            break
        tx, ty, tz = struct.unpack_from('<3f', raw_bytes, off)
        qx, qy, qz, qw = struct.unpack_from('<4f', raw_bytes, off + 0x10)
        sx, sy, sz = struct.unpack_from('<3f', raw_bytes, off + 0x20)
        result[i] = ((tx, ty, tz), (qx, qy, qz, qw), (sx, sy, sz))
    return result


def _parse_parent_indices(raw_bytes, count):
    """Parse a raw parent indices buffer into a list of signed int32."""
    result = [0] * count
    for i in range(count):
        off = i * 4
        if off + 4 > len(raw_bytes):
            break
        val = struct.unpack_from('<i', raw_bytes, off)[0]
        result[i] = val
    return result



class _ChainBlock:
    """One hierarchy's ancestor chains, cached and laid out for numpy.

    A bone's chain is pure topology -- it only changes when the parent-index
    array does, and _read_parent_index_buffers hands back the *same* list
    object for as long as its cache entry lives. Walking every chain from
    scratch each tick was ~14k dict steps at 40 players.

    slots:  distinct TRS slots the chains touch (local position = list index)
    matrix: one row per bone index -- its chain as local positions, -1 padded
    """
    __slots__ = ("parent_buffer", "slots", "slots_np", "chains", "rows", "matrix")

    def __init__(self, parent_buffer, indices):
        self.parent_buffer = parent_buffer
        position = {}
        self.chains = {}
        for index in sorted(indices):
            chain = [index]
            cursor = parent_buffer[index]
            depth = 0
            while cursor >= 0 and depth < PARENT_WALK_LIMIT:
                if cursor >= len(parent_buffer):
                    break
                chain.append(cursor)
                cursor = parent_buffer[cursor]
                depth += 1
            self.chains[index] = chain
            for slot in chain:
                position.setdefault(slot, len(position))
        self.slots = list(position)
        self.slots_np = _np.array(self.slots, dtype=_np.uint64)
        self.rows = {index: row for row, index in enumerate(self.chains)}
        width = max(len(chain) for chain in self.chains.values())
        self.matrix = _np.full((len(self.chains), width), -1, dtype=_np.int64)
        for row, chain in enumerate(self.chains.values()):
            self.matrix[row, :len(chain)] = [position[slot] for slot in chain]


def _compose_position_from_buffers(trs_buf, parent_buf, index, capacity):
    """Walk the parent chain from 'index' upward, composing world position.

    Mirrors Cl1kExternal's ReadBonePosition SSE kernel (sdk/classes.h), which is
    Unity's Transform.TransformPoint applied once per ancestor:

        result = trs[index].t                       # seed: bone local position
        i      = parentIndices[index]
        while i >= 0:
            result = parent.t + parent.q * (parent.s * result)
            i      = parentIndices[i]

    Order matters: the reference computes ``tmp7 = v3 * result`` (scale) *before*
    feeding it through the quaternion rotate, then adds ``v1`` (translation).
    Doing rotate-then-scale only agrees with that for uniform scale, and Rust
    bone rigs do carry non-uniform scale on some joints.
    """
    if index < 0 or index >= capacity or trs_buf[index] is None:
        return None

    pos = trs_buf[index][0]  # (tx, ty, tz)
    pidx = parent_buf[index]
    safety = 0

    while pidx >= 0 and safety < PARENT_WALK_LIMIT:
        if pidx >= capacity or trs_buf[pidx] is None:
            break
        pt, pq, ps = trs_buf[pidx]

        # Validate quaternion
        nq = _normalize_quaternion(pq)
        if nq is None:
            break

        # worldPos = parent.s * worldPos   (scale first — matches the reference)
        pos = (pos[0] * ps[0], pos[1] * ps[1], pos[2] * ps[2])
        # worldPos = parent.q * worldPos
        pos = _rotate_vector(nq, pos)
        # worldPos = worldPos + parent.t
        pos = (pos[0] + pt[0], pos[1] + pt[1], pos[2] + pt[2])

        pidx = parent_buf[pidx]
        safety += 1

    if not all(math.isfinite(c) for c in pos):
        return None
    return pos


class RustGameModel(legacy.RustGame):
    """Game-state model with UC 'stable bones' TransformInternal method."""

    def __init__(self, memory, ga_base):
        super().__init__(memory, ga_base)
        # Rig resolve state
        self._bone_resolve_jobs = {}
        self._bone_rig_identity = {}
        self._bone_sample_failures = {}
        # pm -> (bones composed, bones the anchor gate rejected) for the last
        # sample, so a short result can be attributed before it is punished.
        self._bone_last_outcome = {}
        self._bone_reject = {}
        self._bone_resolve_fails = {}   # pm -> consecutive rig failures
        # [HEALTH-RAW]: the player whose health window gets dumped, chosen
        # each tick as the nearest one and read on the next.
        self._health_probe_pm = None
        self._next_health_probe_at = 0.0
        # PlayerModel -> BasePlayer resolution runs off the tick. See
        # _refresh_pm_to_bp_cache.
        self._bp_worker = None
        self._bp_worker_lock = threading.Lock()
        self._bp_result = None
        self._bp_runs = 0
        self._slow_lane_cursor = 0
        # hierarchyAddr -> (local_transforms_ptr, parent_indices_ptr,
        # expires_at). Topology, not pose: a rig keeps the same TRS array
        # for as long as the hierarchy lives, which is exactly why
        # _read_parent_index_buffers already caches the sibling array.
        # Caching this one is what lets _plan_bone_reads run *before* the
        # frame batch instead of after it, collapsing the tick's two
        # IOCTLs into one. The pointers are still re-read every tick for
        # every sampled player and compared against what was planned --
        # see the staleness check in _read_player_frame_batch -- so a rig
        # that does move costs one skipped sample, never a wrong bone.
        self._hierarchy_ptr_cache = {}
        # Samples dropped because a hierarchy pointer moved between
        # being planned against and being re-read in the same batch.
        # Expected to sit at 0; a non-zero rate means rigs are being
        # rebuilt often enough that planning a tick ahead is costing
        # real samples, which is the one thing that would argue against
        # the merged transaction.
        self._bone_stale_rigs = 0
        # pms a resolution has actually been attempted for. The
        # retry backoff may only ration *retries*; a player it has
        # never tried is not a retry. See _refresh_pm_to_bp_cache.
        self._bp_attempted = {}   # pm -> last time it was in the roster
        self._next_bp_fresh_at = 0.0
        self._bp_last_ms = 0.0
        # Wall time is what got logged as `bg=Nx/Wms`, but the walk itself
        # is a handful of serial IOCTLs (~700ms typical) -- a 2627ms outlier
        # measured 2026-08-26 was suspected to be _yield_to_tick()/lock
        # contention against the main tick rather than the walk doing more
        # work. Split so that is measured, not guessed.
        self._bp_last_io_wait_ms = 0.0
        self._bp_last_yield_wait_ms = 0.0
        self._bp_last_lock_wait_ms = 0.0
        # Distinguishes "this walk waited more" from "this walk did more
        # work" -- yield_ms alone can't tell the two apart, and they need
        # different fixes (priority tuning vs. fewer/cheaper round-trips).
        self._bp_last_io_calls = 0
        self._bone_sample_cursor = 0
        self._next_bone_sample_at = 0.0
        self._next_bp_mapping_at = 0.0
        self._bp_mapping_backoff = BP_MAPPING_RETRY_INTERVAL
        # hierarchyAddr -> (parent_indices_ptr, capacity, [parent_index...], expires_at)
        self._parent_index_cache = {}
        # Per-tick counters for [BONE-DBG]; every gate the skeleton has to pass
        # increments one of these so a single log line says where it died.
        self._bone_debug = {}
        self._next_bone_debug_at = 0.0
        # [POS-DBG] only prints when a tick's whole phase exceeds
        # POS_PHASE_LOG_MS (120ms), so it never shows the steady-state
        # ~60ms tick's own d_map/d_rig/d_batch/d_bones split -- exactly the
        # numbers needed to say whether _sample_bones_multi's pure-Python
        # composition (not its one IOCTL) is really the ~30ms/tick cost, or
        # something else is. Accumulate every tick, print the average once a
        # second, same pattern as [REFRESH-HITRATE]/[RENDER-PROFILE].
        self._pos_profile_sum = {
            'd_map': 0.0, 'd_rig': 0.0, 'd_batch': 0.0, 'd_bones': 0.0,
            'io_batch': 0, 'io_bones': 0, 'n': 0,
        }
        self._next_pos_profile_at = 0.0

    # ------------------------------------------------------------------
    # PlayerModel → BasePlayer mapping
    # ------------------------------------------------------------------
    def _refresh_pm_to_bp_cache(self, pm_ptrs):
        """Keep pm->bp fresh without ever blocking the tick on the slow path.

        Resolving a PlayerModel that is not already mapped costs a walk of the
        whole live BaseNetworkable buffer -- `map=515.4ms` in the 2026-08-25
        log -- and running that inline stalled *every* player's position and
        bones for half a second in order to find the extras of one. The
        relationship is stable for as long as a player stays connected, so it
        belongs off the critical path: the tick uses whatever mapping exists
        right now, and a newly-seen player gets health/name/held-item a
        fraction of a second later instead of freezing the overlay for
        everyone. See OFFSET_RECOVERY.md trap #27.
        """
        active = set(pm_ptrs)
        now_seen = time.perf_counter()
        # The game drops a PlayerModel from its list for 1-4 s while its player is
        # dead and returns the same address. Keep its bp through that gap so a
        # player who did not respawn is not re-walked behind the retry backoff;
        # one who did respawn got a NEW BasePlayer, and the null backref check in
        # _read_player_frame_batch evicts the destroyed one within 3 ticks.
        seen_at = getattr(self, "_pm_bp_seen_at", None) or {}
        for pm in active:
            seen_at[pm] = now_seen
        self._pm_bp_seen_at = {
            pm: t for pm, t in seen_at.items()
            if now_seen - t <= BP_ATTEMPT_MEMORY
        }
        self._pm_bp_cache = {
            pm: bp
            for pm, bp in self._pm_bp_cache.items()
            if pm in self._pm_bp_seen_at and legacy._valid_user_ptr(bp)
        }

        # Always collect first: a finished mapping is free to apply, and making
        # it wait behind the retry backoff would delay the very thing the
        # backoff exists to ration.
        mapped = self._collect_bp_result()
        if mapped:
            self._pm_bp_cache.update({
                pm: bp
                for pm, bp in mapped.items()
                if pm in active and legacy._valid_user_ptr(bp)
            })

        # Refresh the ones still present, then drop only what has been gone
        # longer than BP_ATTEMPT_MEMORY. Pruning against `active` directly is
        # what let roster flicker re-arm the freshness bypass.
        for pm in active:
            if pm in self._bp_attempted:
                self._bp_attempted[pm] = now_seen
        if self._bp_attempted:
            self._bp_attempted = {
                pm: seen for pm, seen in self._bp_attempted.items()
                if now_seen - seen <= BP_ATTEMPT_MEMORY
            }
        missing = active.difference(self._pm_bp_cache)
        if not missing:
            self._bp_mapping_backoff = BP_MAPPING_RETRY_INTERVAL
            # Nothing to resolve, but the slow lane still has work: without
            # this, held items would only ever refresh while some player was
            # unmapped -- i.e. almost never on a settled server.
            self._start_bp_resolution(None)
            return

        now = time.perf_counter()
        # Players nobody has tried to map yet. The backoff exists to stop
        # hammering the BaseNetworkable walk for players that cannot be
        # resolved -- it must not also punish players that just appeared. A
        # squad spawning in used to inherit a backoff already doubled up to
        # BP_MAPPING_MAX_BACKOFF (30 s), so they sat with no bp, therefore no
        # rig job, therefore no skeleton, for up to half a minute: that is the
        # `cand=2(local=1 norig=47)` with `job=0` in the log.
        fresh = missing.difference(self._bp_attempted.keys())
        if fresh:
            self._bp_mapping_backoff = BP_MAPPING_RETRY_INTERVAL
        elif mapped is not None:
            # A resolution just finished and the same players are still
            # unmapped, so they are probably not resolvable right now (not in
            # the BaseNetworkable buffer yet). That is a real retry: ration it.
            self._bp_mapping_backoff = min(
                self._bp_mapping_backoff * 2, BP_MAPPING_MAX_BACKOFF
            )
            self._next_bp_mapping_at = now + self._bp_mapping_backoff
            self._start_bp_resolution(None)
            return
        elif now < self._next_bp_mapping_at:
            # Names/held items/world entities live on the slow lane, which
            # only ever ran as a tail of a bp resolution while any player was
            # unmapped. One unresolvable player (sleeper, out-of-buffer) kept
            # the backoff at up to 30 s -- and every name and inventory waited
            # that long. Run the slow lane on its own meanwhile.
            self._start_bp_resolution(None)
            return

        # The floor guards the *spawn*, not one branch of the decision above.
        # Gating only the freshness bypass left a hole: a tick where nothing
        # was missing consumed the pending result without ever advancing the
        # retry deadline, so the next tick with a missing pm walked
        # immediately -- which pure roster flicker triggers every few ticks.
        # The walk costs ~620 ms of driver time; nothing may start one more
        # often than this, whatever the reason.
        if now < self._next_bp_fresh_at:
            self._start_bp_resolution(None)
            return
        # A slow-lane-only pass may still be running (see above). Starting now
        # would silently no-op after the attempt was already recorded, turning
        # a first attempt into a backed-off retry -- wait for the next tick.
        if self._bp_worker is not None and self._bp_worker.is_alive():
            return
        self._next_bp_fresh_at = now + BP_MAPPING_MIN_INTERVAL
        for pm in missing:
            self._bp_attempted[pm] = now_seen
        self._start_bp_resolution(list(pm_ptrs))

    def _format_bp_bg(self):
        """Background resolver cost, so moving it off the tick does not hide it.

        `map=` in [POS-DBG] now measures only the cheap cached path; without
        this the 500 ms walk would simply vanish from the log rather than be
        seen to have moved.
        """
        if not self._bp_runs:
            return ""
        running = self._bp_worker is not None and self._bp_worker.is_alive()
        return (f" bg={self._bp_runs}x/{self._bp_last_ms:.0f}ms"
                f"({self._bp_last_io_calls}io "
                f"yield={self._bp_last_yield_wait_ms:.0f}ms "
                f"lock={self._bp_last_lock_wait_ms:.0f}ms "
                f"io_wait={self._bp_last_io_wait_ms:.0f}ms)"
                f"{'/busy' if running else ''}")

    def _collect_bp_result(self):
        """Take the finished background mapping, if there is one."""
        with self._bp_worker_lock:
            result = self._bp_result
            self._bp_result = None
        return result

    def _start_bp_resolution(self, pm_ptrs):
        """Resolve pm->bp on a background thread; never blocks the tick.

        At most one resolution is in flight. The driver serialises concurrent
        callers on its own lock (CameraSampler already relies on this), so the
        only shared state needing care is the handoff below.
        """
        with self._bp_worker_lock:
            if self._bp_worker is not None and self._bp_worker.is_alive():
                return
            target = (
                self._bp_resolution_worker if pm_ptrs is not None
                else self._slow_lane_only_worker
            )
            worker = threading.Thread(
                target=target,
                args=(pm_ptrs,) if pm_ptrs is not None else (),
                daemon=True,
                name="slow-lane",
            )
            self._bp_worker = worker
        worker.start()

    def _bp_resolution_worker(self, pm_ptrs):
        started = time.perf_counter()
        # This thread's own TLS counters (threading.local, separate from the
        # main tick thread's) -- deltas across the call split wall time into
        # raw IOCTL cost vs. time spent waiting on _yield_to_camera/
        # _yield_to_tick/_drv_lock. See the comment on _bp_last_io_wait_ms.
        # `mem` is None in tests that build this object via object.__new__
        # and monkeypatch _resolve_pm_to_bp directly (never touching self.m)
        # -- fall back to zero rather than crash the background thread
        # before it ever publishes _bp_result.
        mem = getattr(self, "m", None)
        yield0 = mem.yield_wait_s if mem is not None else 0.0
        lock0 = mem.lock_wait_s if mem is not None else 0.0
        io_wait0 = mem.io_wait_s if mem is not None else 0.0
        io_calls0 = mem.io_calls if mem is not None else 0
        try:
            mapped = self._resolve_pm_to_bp(pm_ptrs)
        except Exception as exc:
            # A background thread that dies silently would look exactly like
            # "players never get health", which is the symptom this is meant
            # to fix. Say so.
            print(f"[PM2BP-DBG] background resolve failed: {exc!r}", flush=True)
            mapped = {}
        elapsed = (time.perf_counter() - started) * 1000.0
        with self._bp_worker_lock:
            # An empty dict still has to be published: it is what tells the
            # tick to back off rather than re-arming immediately.
            self._bp_result = mapped or {}
            self._bp_runs += 1
            self._bp_last_ms = elapsed
            self._bp_last_io_wait_ms = (
                (mem.io_wait_s - io_wait0) * 1000.0 if mem is not None else 0.0
            )
            self._bp_last_yield_wait_ms = (
                (mem.yield_wait_s - yield0) * 1000.0 if mem is not None else 0.0
            )
            self._bp_last_lock_wait_ms = (
                (mem.lock_wait_s - lock0) * 1000.0 if mem is not None else 0.0
            )
            self._bp_last_io_calls = (
                mem.io_calls - io_calls0 if mem is not None else 0
            )
        self._run_slow_lane()

    def _run_slow_lane(self):
        """Off-tick work that is decoration, not pose.

        The held-item chain is ~10 *dependent* driver round-trips -- each
        stage's addresses come from the previous stage's results -- so it
        cannot be batched below ~11 IOCTLs and was the last consistent source
        of 118-300 ms tick spikes. Nothing on the critical path needs it, so
        the tick publishes `pm_to_bp` and this runs behind it.
        """
        pm_to_bp = self._slow_lane_pm_to_bp or {}
        started = time.perf_counter()
        before = self.m.io_calls

        # HELD runs twice per cycle so inventory populates fast; NAME and WE
        # are decoration that can afford lower cadence.  Pattern is
        # HELD → NAME → HELD → WE → repeat.
        scans = (
            ("HELD", lambda: self._read_held_items_batch(
                pm_to_bp, on_worker=True)),
            ("NAME", lambda: self._read_player_names_batch(
                pm_to_bp, on_worker=True)),
            ("HELD", lambda: self._read_held_items_batch(
                pm_to_bp, on_worker=True)),
            ("WE", lambda: self._scan_world_entities(on_worker=True)),
        )
        now = time.perf_counter()
        names = getattr(self, "_player_name_cache", None) or {}
        if (any(pm not in names for pm in pm_to_bp)
                and self._slow_lane_due("NAME", now)):
            # A player with no name yet is the most visible gap on screen and
            # the scan only reads the missing ones (~3 IOCTLs), so it does not
            # wait for its turn in the rotation.
            tag, run = scans[1]
        else:
            # First scan in rotation whose own interval has elapsed. Blindly
            # taking the cursor's scan spent whole passes on scans that return
            # at once ("not due yet") while another one was waiting.
            start = self._slow_lane_cursor % len(scans)
            index = start
            for step in range(len(scans)):
                i = (start + step) % len(scans)
                if self._slow_lane_due(scans[i][0], now):
                    index = i
                    break
            self._slow_lane_cursor = index + 1
            tag, run = scans[index]
        try:
            run()
        except Exception as exc:
            print(f"[{tag}-DBG] slow lane failed: {exc!r}", flush=True)

        calls = self.m.io_calls - before
        if calls:
            self._slow_lane_last = (
                calls, (time.perf_counter() - started) * 1000.0
            )

    def _slow_lane_due(self, tag, now):
        """Would this slow-lane scan do any work if run right now?"""
        wanted = getattr(self, "wanted", None) or {}
        if tag == "HELD":
            return (wanted.get("held_item", True)
                    and now >= getattr(self, "_next_held_item_scan_at", 0.0))
        if tag == "NAME":
            return (wanted.get("names", True)
                    and now >= getattr(self, "_next_player_name_scan_at", 0.0))
        if tag == "WE":
            return (wanted.get("world_entities", True)
                    and now >= getattr(self, "_next_world_scan_at", 0.0))
        return True

    def _slow_lane_only_worker(self):
        """Everything is mapped; just do the off-tick decoration work."""
        self._run_slow_lane()

    # ------------------------------------------------------------------
    # Rig resolution: Model → boneTransforms[] → Transform → native →
    #   TransformAccess (hierarchyAddr, index)
    # ------------------------------------------------------------------
    def _resolve_bone_slots_batch(self, pm_to_bp):
        """Progressive rig resolution using batch reads."""
        now = time.perf_counter()
        active = set(pm_to_bp)
        self._bone_slot_cache = {
            pm: plan
            for pm, plan in self._bone_slot_cache.items()
            if pm in active
        }
        self._bone_resolve_jobs = {
            pm: job
            for pm, job in self._bone_resolve_jobs.items()
            if pm in active
        }
        self._bone_rig_identity = {
            pm: identity
            for pm, identity in self._bone_rig_identity.items()
            if pm in active
        }
        if self._bone_resolve_fails:
            self._bone_resolve_fails = {
                pm: n for pm, n in self._bone_resolve_fails.items()
                if pm in active
            }
        fail_info = getattr(self, "_bone_resolve_fail_info", None) or {}
        if fail_info:
            fail_info = self._bone_resolve_fail_info = {
                pm: info for pm, info in fail_info.items() if pm in active
            }

        admitted_this_tick = 0
        for pm, bp in pm_to_bp.items():
            if not legacy._valid_user_ptr(bp):
                continue
            identity = self._bone_rig_identity.get(pm)
            if identity is not None and identity[0] != bp:
                self._invalidate_bone_rig(pm, retry_delay=0.0)
            failed = fail_info.get(pm)
            if failed is not None and failed[1] and failed[1] != bp:
                # A respawn hands the recycled PlayerModel a new BasePlayer. The
                # backoff was earned by the destroyed one, whose Model was null;
                # `identity` cannot catch this because that rig never resolved.
                fail_info.pop(pm, None)
                self._bone_resolve_fails.pop(pm, None)
                self._bone_resolve_retry_at.pop(pm, None)
            if len(self._bone_slot_cache.get(pm, {})) >= BONE_MIN_VALID:
                continue
            if pm in self._bone_resolve_jobs:
                continue
            if now < self._bone_resolve_retry_at.get(pm, 0.0):
                continue
            # Cap new-job admission per tick (see BONE_MAX_NEW_JOBS_PER_TICK)
            # so a mass cache invalidation doesn't dump dozens of jobs into
            # lockstep and spike a single tick's read count. Anyone not
            # admitted this pass simply has no job yet, so the next call
            # picks them back up — no state is lost, just staggered.
            if admitted_this_tick >= BONE_MAX_NEW_JOBS_PER_TICK:
                break
            self._bone_resolve_jobs[pm] = {
                "stage": "model",
                "bp": bp,
            }
            admitted_this_tick += 1

    def _invalidate_bone_rig(self, pm, retry_delay=0.35):
        self._bone_slot_cache.pop(pm, None)
        self._bone_rig_identity.pop(pm, None)
        self._bone_resolve_jobs.pop(pm, None)
        self._bone_position_cache.pop(pm, None)
        self._bone_sample_failures.pop(pm, None)
        self._bone_resolve_retry_at[pm] = time.perf_counter() + retry_delay

    @staticmethod
    def _append_rig_job_reads(pm, job, add):
        stage = job.get("stage")
        if stage == "model":
            add(pm, ("rig", "model"), job["bp"] + legacy.OFF.model)
        elif stage == "array":
            add(
                pm,
                ("rig", "array"),
                job["model"] + legacy.OFF.boneTransforms,
            )
            add(
                pm,
                ("rig", "array_fallback"),
                pm + legacy.OFF.pm_rootBone,
            )
        elif stage == "elements":
            array = job["array"]
            add(pm, ("rig", "count"), array + 0x18)
            for bone_id in legacy.SCI_BONE_IDS:
                add(
                    pm,
                    ("rig", "transform", bone_id),
                    array + legacy.OFF.bone_list + bone_id * 8,
                )
        elif stage == "native":
            for bone_id, transform in job["transforms"].items():
                add(
                    pm,
                    ("rig", "native", bone_id),
                    transform + legacy.OFF.bone_transform,
                )
        elif stage == "access":
            for bone_id, native in job["natives"].items():
                # TransformAccess: hierarchyAddr (u64 @+0x38) + index (i32 @+0x40)
                add(
                    pm,
                    ("rig", "hierarchy", bone_id),
                    native + legacy.OFF.transform_access,
                )
                add(
                    pm,
                    ("rig", "idx", bone_id),
                    native + legacy.OFF.transform_access + 8,
                )

    def _fail_rig_job(self, pm, delay=None):
        """Drop the job and schedule a retry that gets rarer as it keeps failing.

        `delay=None` means "use the backoff". An explicit delay is still
        honoured for the caller that wants an immediate retry.
        """
        job = self._bone_resolve_jobs.pop(pm, None)
        if delay is None:
            stage = job.get("stage", "?") if job else "?"
            stages = getattr(self, "_rig_fail_stages", None)
            if stages is None:
                stages = self._rig_fail_stages = {}
            stages[stage] = stages.get(stage, 0) + 1
            # What failed and on which bp: lets a model-stage failure retry the
            # moment the Model pointer appears, and a new bp drop the backoff.
            info = getattr(self, "_bone_resolve_fail_info", None)
            if info is None:
                info = self._bone_resolve_fail_info = {}
            info[pm] = (stage, job.get("bp", 0) if job else 0)
            fails = self._bone_resolve_fails.get(pm, 0) + 1
            self._bone_resolve_fails[pm] = fails
            delay = min(
                BONE_RIG_RETRY_BASE * (2 ** (fails - 1)), BONE_RIG_RETRY_MAX
            )
        self._bone_resolve_retry_at[pm] = time.perf_counter() + delay

    def _consume_rig_job(self, pm, fields):
        job = self._bone_resolve_jobs.get(pm)
        if job is None:
            return
        stage = job.get("stage")

        if stage == "model":
            model = fields.get(("rig", "model"), 0)
            if not legacy._valid_user_ptr(model):
                self._fail_rig_job(pm)
                return
            if ENABLE_BONE_NAME_DUMP and not getattr(
                self, "_bone_names_dumped", False
            ):
                self._bone_names_dumped = True
                self._debug_dump_bone_names(model)
            job.update(stage="array", model=model)
            return

        if stage == "array":
            array = fields.get(("rig", "array"), 0)
            if not legacy._valid_user_ptr(array):
                array = fields.get(("rig", "array_fallback"), 0)
            if not legacy._valid_user_ptr(array):
                self._fail_rig_job(pm)
                return
            job.update(stage="elements", array=array)
            return

        if stage == "elements":
            count = fields.get(("rig", "count"), 0) & 0xFFFFFFFF
            if not (legacy.SCI_MIN_ARRAY_COUNT <= count <= 256):
                self._fail_rig_job(pm)
                return
            transforms = {
                bone_id: fields.get(("rig", "transform", bone_id), 0)
                for bone_id in legacy.SCI_BONE_IDS
                if legacy._valid_user_ptr(
                    fields.get(("rig", "transform", bone_id), 0)
                )
            }
            if len(transforms) < BONE_MIN_VALID:
                self._fail_rig_job(pm)
                return
            job.update(stage="native", count=count, transforms=transforms)
            return

        if stage == "native":
            natives = {
                bone_id: fields.get(("rig", "native", bone_id), 0)
                for bone_id in job["transforms"]
                if legacy._valid_user_ptr(
                    fields.get(("rig", "native", bone_id), 0)
                )
            }
            if len(natives) < BONE_MIN_VALID:
                self._fail_rig_job(pm)
                return
            job.update(stage="access", natives=natives)
            return

        if stage == "access":
            # Final stage: extract (hierarchyAddr, index) per bone
            plan = {}
            for bone_id in job["natives"]:
                hierarchy = fields.get(("rig", "hierarchy", bone_id), 0)
                idx_raw = fields.get(("rig", "idx", bone_id), 0) & 0xFFFFFFFF
                if (
                    legacy._valid_user_ptr(hierarchy)
                    and idx_raw < BONE_MAX_INDEX
                ):
                    plan[bone_id] = (hierarchy, idx_raw)

            if len(plan) >= BONE_MIN_VALID:
                self._bone_slot_cache[pm] = plan
                self._bone_resolve_fails.pop(pm, None)
                (getattr(self, "_bone_resolve_fail_info", None) or {}).pop(pm, None)
                self._bone_rig_identity[pm] = (
                    job["bp"],
                    job.get("model", 0),
                    job.get("array", 0),
                    job.get("count", 0),
                )
                self._bone_resolve_retry_at.pop(pm, None)
            else:
                self._fail_rig_job(pm)
                return
            self._bone_resolve_jobs.pop(pm, None)
            return

    def _debug_dump_bone_names(self, model_ptr):
        """TEMP: one-shot ground truth for SCI_BONE_IDS via Model.boneNames.

        Reads the live String[] parallel to boneTransforms so bone_id -> name
        can be checked directly instead of inferred from a stale dump. See
        conversation: bone 54 (assumed r_clavicle) measures 1.52m from bone 17
        (torso) live, while every other skeletal link is anatomically sane.
        """
        try:
            names_array = self.m.batch_u64(
                [model_ptr + legacy.OFF.boneNames], attempts=1
            )[0]
            if not legacy._valid_user_ptr(names_array):
                self._debug(
                    f"[BONE-NAMES] boneNames array invalid ptr=0x{names_array:X}",
                    force=True,
                )
                return
            indices = list(range(0, 94))
            addrs = [
                names_array + legacy.OFF.bone_list + i * 8 for i in indices
            ]
            ptrs = self.m.batch_u64(addrs, attempts=1)
            pairs = [
                (i, p) for i, p in zip(indices, ptrs)
                if legacy._valid_user_ptr(p)
            ]
            names = self._read_il2cpp_strings(pairs)
            text = ", ".join(
                f"{i}={names.get(i, '?')}" for i in indices
            )
            self._debug(f"[BONE-NAMES] {text}", force=True)
        except Exception as exc:
            self._debug(f"[BONE-NAMES] error: {exc!r}", force=True)

    # ------------------------------------------------------------------
    # Bone sampling — Cl1kExternal ReadAllBones, batched across players
    # ------------------------------------------------------------------
    def _read_parent_index_buffers(self, needed):
        """Fetch (and cache) the parent-index array of each live hierarchy.

        'needed' maps hierarchyAddr -> (parent_indices_ptr, capacity).

        The parent-index array is pure topology: a rig's joints keep the same
        parent for as long as the hierarchy lives, so this only has to be
        re-read when the hierarchy pointer changes or the TTL lapses. It is
        also the cheapest of the two arrays (4 bytes/slot vs 48), which is why
        it is worth reading whole — knowing the full topology up front is what
        lets the TRS stage below touch *only* the slots that actually matter.

        Returns {hierarchyAddr: [parent_index, ...]}.
        """
        now = time.perf_counter()
        if len(self._parent_index_cache) > 64:
            self._parent_index_cache = {
                key: entry
                for key, entry in self._parent_index_cache.items()
                if now < entry[3] or key in needed
            }
        out = {}
        misses = []
        for hierarchy, (parent_ptr, capacity) in needed.items():
            cached = self._parent_index_cache.get(hierarchy)
            if (
                cached is not None
                and cached[0] == parent_ptr
                and cached[1] >= capacity
                and now < cached[3]
            ):
                out[hierarchy] = cached[2]
                continue
            if capacity <= 0 or capacity > TRSX_MAX_CAPACITY:
                continue
            misses.append((hierarchy, parent_ptr, capacity))

        # One IOCTL for every hierarchy that missed, not one *per* hierarchy.
        # This loop used to issue a separate self.m.read() per rig, which is
        # where `bones=166.1ms/12io` with `hier=11/11` came from: 11 parent
        # reads plus the single TRS batch. At a fixed ~14 ms driver round-trip
        # the call count *is* the cost, so the shape of the read matters more
        # than its size. The parent array is 4 bytes/slot, so it is fetched as
        # u64 words and sliced back apart here.
        self._bone_debug['parent_reads'] = len(misses)
        if misses:
            addresses = []
            spans = []
            for hierarchy, parent_ptr, capacity in misses:
                words = (capacity * 4 + 7) // 8
                spans.append((hierarchy, parent_ptr, capacity, len(addresses), words))
                addresses.extend(parent_ptr + i * 8 for i in range(words))
            values = self.m.batch_u64(addresses, attempts=1)
            for hierarchy, parent_ptr, capacity, start, words in spans:
                chunk = values[start:start + words]
                if len(chunk) < words:
                    continue
                raw = struct.pack('<%dQ' % words, *chunk)[:capacity * 4]
                if len(raw) < capacity * 4:
                    continue
                buffer = _parse_parent_indices(raw, capacity)
                self._parent_index_cache[hierarchy] = (
                    parent_ptr,
                    capacity,
                    buffer,
                    now + PARENT_INDEX_CACHE_TTL,
                )
                out[hierarchy] = buffer
        return out

    def _format_bone_rejects(self):
        """Render the non-zero candidate rejection reasons, e.g. "(sleeping=15)"."""
        reject = getattr(self, '_bone_reject', None) or {}
        parts = [f"{k}={v}" for k, v in reject.items() if v]
        # How many of the norig players are simply in retry backoff rather
        # than waiting for an admission slot. Without this, a permanently
        # unresolvable entity and a player about to resolve look identical.
        fails = getattr(self, '_bone_resolve_fails', None) or {}
        backing_off = sum(1 for n in fails.values() if n >= 3)
        if backing_off:
            parts.append(f"cold={backing_off}")
        return "(" + " ".join(parts) + ")" if parts else ""

    def _bone_anchor_radius(self):
        """How far the rendered pose may sit from the networked anchor.

        The anchor is the *networked* position; the bones are the pose being
        rendered, which the client is still interpolating towards it. Those two
        legitimately diverge while a player moves, and by more the longer the
        sample is stale -- so a fixed 4.5 m radius starts rejecting good bones
        exactly during fast movement, which then trips the three-strike rig
        invalidation, which forces a full re-resolve, which lengthens the tick,
        which widens the divergence further. That runaway is what the user sees
        as a micro-freeze when someone sprints past. Widen the gate by what the
        last tick's duration can actually account for (a sprint is ~7 m/s, a
        vehicle ~25 m/s) instead of letting the loop close.
        """
        return BONE_ANCHOR_RADIUS + min(
            max(self.tick_ms, 0.0) / 1000.0 * 25.0, BONE_ANCHOR_SLACK_MAX
        )

    def _record_bone_sample_outcome(self, pm, bones):
        """Score one player's bone sample and decide whether the rig is at fault.

        Returns 'ok', 'anchor_only' or 'short'. Only 'short' counts as a strike;
        three of those invalidate the rig and force the full five-stage
        re-resolve.

        'anchor_only' is the case that used to be miscounted: the rig walked
        fine and produced a full skeleton, but the pose landed further from the
        networked position than _bone_anchor_radius allows. Re-resolving fixes
        nothing there, and doing it while the player sprints is exactly the
        runaway the adaptive radius exists to break.
        """
        if len(bones) >= BONE_MIN_VALID:
            self._bone_sample_failures.pop(pm, None)
            return 'ok'

        outcome = self._bone_last_outcome.get(pm, (0, 0, 0.0))
        composed_here, anchor_rejected = outcome[0], outcome[1]
        worst_sq = outcome[2] if len(outcome) > 2 else 0.0
        if composed_here >= BONE_MIN_VALID and anchor_rejected:
            if worst_sq > BONE_ANCHOR_BROKEN_SQ:
                # Not a fast player the radius failed to keep up with: at this
                # magnitude the composed positions are garbage, and leaving the
                # rig alone means the pose stays frozen until it happens to
                # heal. Re-resolve, and count it as a strike like any other
                # broken sample. See BONE_ANCHOR_BROKEN.
                self._bone_sample_failures.pop(pm, None)
                self._invalidate_bone_rig(pm)
                return 'broken'
            return 'anchor_only'

        failures = self._bone_sample_failures.get(pm, 0) + 1
        self._bone_sample_failures[pm] = failures
        if failures >= 3:
            self._invalidate_bone_rig(pm)
        return 'short'

    def _sample_bones_multi(self, jobs):
        """Sample every tracked player's skeleton in one coherent TRS batch.

        'jobs' is a list of (pm, plan, anchor, hierarchy_ptrs) where 'plan' maps
        bone_id -> (hierarchyAddr, slot_index) and 'hierarchy_ptrs' maps
        hierarchyAddr -> (local_transforms_ptr, parent_indices_ptr), both already
        resolved by the caller's frame batch.

        Structure follows Cl1kExternal's ReadAllBones (sdk/classes.h): resolve
        the slot index per bone, then walk each bone's parent chain composing
        local TRS into a world position. The difference is where the reads go:

          * the reference issues one small batch per hierarchy level and keeps a
            per-frame SlotCache so shared ancestors are only read once;
          * here the topology is already known (see _read_parent_index_buffers),
            so the exact set of slots — bones plus their distinct ancestors,
            across *all* players — is computed first and fetched in a single
            batch_u64. Shared spine/hip joints collapse into one entry for free.

        The previous implementation instead bulk-read the whole TRS array from
        slot 0 to the highest bone slot. That is fine for a rig that owns its
        hierarchy (~100 slots, ~5 KB) but catastrophic when the player is
        parented into a large shared hierarchy: slot 3000 meant a 144 KB read
        *per player per frame*, which is where the multi-hundred-millisecond
        `pos=` spikes in [TICK-LATENCY] came from.

        Returns {pm: {bone_id: (x, y, z)}}.
        """
        planned = self._plan_bone_reads(jobs)
        if planned is None:
            return {}
        chains, slot_keys, addresses = planned
        values = self.m.batch_u64(addresses, attempts=1)
        return self._compose_bones(jobs, chains, slot_keys, values)

    def _plan_bone_reads(self, jobs):
        """Work out which TRS slots this frame needs, and where they live.

        Split out of _sample_bones_multi so the caller can decide *when* the
        read happens. The tick's frame batch and this TRS batch used to be
        two IOCTLs because this one needs `local_ptr`, which the frame batch
        reads -- but that pointer is topology, not pose (see
        _hierarchy_ptr_cache), so planning against the cached value lets both
        ride in a single transaction. At ~15.5 ms of fixed driver latency per
        call, halving the tick's call count is its largest remaining lever;
        nothing else in this file costs a tenth of it.

        Returns (chains, slot_keys, addresses), or None when there is
        nothing to read.
        """
        stats = self._bone_debug
        stats.update(
            hier_needed=0, hier_parents=0, slots_req=0, slots_ok=0,
            rej_chain=0, rej_quat=0, rej_box=0, rej_anchor=0,
            worst_anchor=0.0, composed=0, parent_reads=0,
        )
        self._bone_last_outcome = {}
        if not jobs:
            return None

        # -- Stage 1: which hierarchies, and how deep into each -------------
        needed = {}
        for _, plan, _, hierarchy_ptrs in jobs:
            for hierarchy, index in plan.values():
                local_ptr, parent_ptr = hierarchy_ptrs.get(hierarchy, (0, 0))
                if not legacy._valid_user_ptr(local_ptr):
                    continue
                if not legacy._valid_user_ptr(parent_ptr):
                    continue
                capacity = index + 1
                current = needed.get(hierarchy)
                if current is None or capacity > current[1]:
                    needed[hierarchy] = (parent_ptr, capacity)
        parents = self._read_parent_index_buffers(needed)
        stats['hier_needed'] = len(needed)
        stats['hier_parents'] = len(parents)
        if not parents:
            return None

        if _np is not None:
            return self._plan_bone_reads_np(jobs, parents)

        # -- Stage 2: expand every bone into its ancestor chain -------------
        # Unity stores parents before children, so a chain only ever walks
        # towards lower indices and stays inside the capacity we already read.
        chains = {}
        slot_addresses = {}
        for pm, plan, _, hierarchy_ptrs in jobs:
            for bone_id, (hierarchy, index) in plan.items():
                parent_buffer = parents.get(hierarchy)
                if parent_buffer is None or index >= len(parent_buffer):
                    continue
                local_ptr = hierarchy_ptrs[hierarchy][0]
                chain = [index]
                cursor = parent_buffer[index]
                depth = 0
                while cursor >= 0 and depth < PARENT_WALK_LIMIT:
                    if cursor >= len(parent_buffer):
                        break
                    chain.append(cursor)
                    cursor = parent_buffer[cursor]
                    depth += 1
                chains[(pm, bone_id)] = (hierarchy, chain)
                for slot in chain:
                    slot_addresses[(hierarchy, slot)] = (
                        local_ptr + TRSX_SIZE * slot
                    )
        if not slot_addresses:
            return None

        # -- Stage 3: the address list for every distinct TRS slot ----------
        slot_keys = list(slot_addresses)
        addresses = []
        for key in slot_keys:
            base = slot_addresses[key]
            addresses.extend(base + offset for offset in TRSX_WORD_OFFSETS)
        return chains, slot_keys, addresses

    def _plan_bone_reads_np(self, jobs, parents):
        """Stages 2-3 of _plan_bone_reads on cached chains (see _ChainBlock).

        Same contract as the pure-Python stages -- (chains, slot_keys,
        addresses), slot_keys aligned with the TRS words in `addresses` --
        plus a third element per chain entry, (layout, row), that lets
        _compose_bones walk every chain at once. Slots are grouped per
        hierarchy instead of first-seen order; batch_u64 sorts by address
        anyway, so the read itself is unchanged.
        """
        wanted = {}
        local_ptrs = {}
        for _, plan, _, hierarchy_ptrs in jobs:
            for hierarchy, index in plan.values():
                parent_buffer = parents.get(hierarchy)
                if parent_buffer is None or not 0 <= index < len(parent_buffer):
                    continue
                wanted.setdefault(hierarchy, set()).add(index)
                local_ptrs[hierarchy] = hierarchy_ptrs[hierarchy][0]
        if not wanted:
            return None
        cache = getattr(self, "_chain_block_cache", None)
        if cache is None or len(cache) > 256:
            cache = self._chain_block_cache = {}

        slot_keys = []
        address_parts = []
        placed = []          # (block, first_slot, first_row)
        first_row = {}
        rows = 0
        width = 0
        for hierarchy, indices in wanted.items():
            parent_buffer = parents[hierarchy]
            key = (hierarchy, frozenset(indices))
            block = cache.get(key)
            if block is None or block.parent_buffer is not parent_buffer:
                block = cache[key] = _ChainBlock(parent_buffer, indices)
            placed.append((block, len(slot_keys), rows))
            first_row[hierarchy] = (block, rows)
            slot_keys.extend((hierarchy, slot) for slot in block.slots)
            slot_bases = (
                _np.uint64(local_ptrs[hierarchy])
                + _np.uint64(TRSX_SIZE) * block.slots_np
            )
            address_parts.append(
                (slot_bases[:, None] + _TRSX_WORD_OFFSETS_NP[None, :]).ravel()
            )
            rows += block.matrix.shape[0]
            width = max(width, block.matrix.shape[1])

        layout = _np.full((rows, width), -1, dtype=_np.int64)
        for block, first_slot, row0 in placed:
            matrix = block.matrix
            layout[row0:row0 + matrix.shape[0], :matrix.shape[1]] = _np.where(
                matrix >= 0, matrix + first_slot, -1,
            )

        chains = {}
        for pm, plan, _, _ in jobs:
            for bone_id, (hierarchy, index) in plan.items():
                placed_block = first_row.get(hierarchy)
                if placed_block is None:
                    continue
                block, row0 = placed_block
                row = block.rows.get(index)
                if row is None:
                    continue
                chains[(pm, bone_id)] = (
                    hierarchy, block.chains[index], (layout, row0 + row),
                )
        addresses = _np.concatenate(address_parts).tolist()
        return chains, slot_keys, addresses

    def _compose_bones(self, jobs, chains, slot_keys, values):
        """Turn one batch of raw TRS words into world-space bone positions.

        Stages 3-decode and 4 of _sample_bones_multi, split out so the read
        itself can be merged into the tick's frame batch. See
        _plan_bone_reads.
        """
        stats = self._bone_debug
        world = None
        if _np is not None:
            world = self._world_positions_np(jobs, chains, slot_keys, values, stats)
        if world is None:
            world = self._world_positions_py(jobs, chains, slot_keys, values, stats)
        return self._finish_bone_positions(jobs, world, stats)

    def _world_positions_np(self, jobs, chains, slot_keys, values, stats):
        """Every queued bone's world position, all chains walked at once.

        Same maths and operation order as _world_positions_py (scale ->
        rotate -> translate per ancestor, float64 throughout) and the same
        rejection accounting; positions agree to ~1e-14 m (last-bit
        rounding), not bit for bit. It was ~9 ms of GIL time per tick at 40
        skeletons in pure Python -- the largest single piece of pos=.
        Returns None when the chains were not planned by
        _plan_bone_reads_np (the Python path handles those).
        """
        layout = None
        for entry in chains.values():
            if len(entry) < 3:
                return None
            layout = entry[2][0]
            break
        if layout is None:
            return None
        words = len(TRSX_WORD_OFFSETS)
        stats['slots_req'] = len(slot_keys)
        available = min(len(slot_keys), len(values) // words)
        stats['slots_ok'] += available
        keys = []
        rows = []
        for pm, plan, _, _ in jobs:
            for bone_id in plan:
                entry = chains.get((pm, bone_id))
                if entry is None:
                    stats['rej_chain'] += 1
                    continue
                keys.append((pm, bone_id))
                rows.append(entry[2][1])
        if not rows:
            return {}
        with _np.errstate(all="ignore"):
            raw = _np.array(values[:available * words], dtype=_np.uint64)
            f = raw.view(_np.float32).reshape(available, words * 2).astype(_np.float64)
            t = f[:, 0:3]
            q = f[:, 4:8]
            s = f[:, 8:11]
            norm_sq = q[:, 0] * q[:, 0] + q[:, 1] * q[:, 1] + q[:, 2] * q[:, 2] + q[:, 3] * q[:, 3]
            q_ok = _np.isfinite(q).all(axis=1) & (norm_sq >= 0.25) & (norm_sq <= 4.0)
            qn = q * (1.0 / _np.sqrt(norm_sq))[:, None]

            m = layout[_np.asarray(rows, dtype=_np.int64)]
            seed = m[:, 0]
            seed_ok = (seed >= 0) & (seed < available)
            pos = _np.zeros((len(rows), 3))
            pos[seed_ok] = t[seed[seed_ok]]
            alive = seed_ok.copy()
            quat_fail = _np.zeros(len(rows), dtype=bool)
            for level in range(1, m.shape[1]):
                col = m[:, level]
                step = alive & (col >= 0)
                if not step.any():
                    break
                missing = step & (col >= available)
                ai = _np.where(step & ~missing, col, 0)
                bad = step & ~missing & ~q_ok[ai]
                quat_fail |= bad
                alive &= ~(missing | bad)
                go = step & ~(missing | bad)
                g = ai[go]
                v = pos[go] * s[g]
                qq = qn[g]
                qx, qy, qz, qw = qq[:, 0], qq[:, 1], qq[:, 2], qq[:, 3]
                vx, vy, vz = v[:, 0], v[:, 1], v[:, 2]
                tx = 2.0 * (qy * vz - qz * vy)
                ty = 2.0 * (qz * vx - qx * vz)
                tz = 2.0 * (qx * vy - qy * vx)
                tt = t[g]
                out = _np.empty_like(v)
                out[:, 0] = (vx + qw * tx + (qy * tz - qz * ty)) + tt[:, 0]
                out[:, 1] = (vy + qw * ty + (qz * tx - qx * tz)) + tt[:, 1]
                out[:, 2] = (vz + qw * tz + (qx * ty - qy * tx)) + tt[:, 2]
                pos[go] = out
            finite = _np.isfinite(pos).all(axis=1)
        stats['rej_chain'] += int((~seed_ok).sum()) + int((alive & ~finite).sum())
        stats['rej_quat'] += int(quat_fail.sum())
        world = {}
        for key, good, position in zip(keys, (alive & finite).tolist(), pos.tolist()):
            if good:
                world[key] = tuple(position)
        return world

    def _world_positions_py(self, jobs, chains, slot_keys, values, stats):
        """Reference path for _compose_bones (and the no-numpy fallback)."""
        trs = {}
        stats['slots_req'] = len(slot_keys)
        words = len(TRSX_WORD_OFFSETS)
        for slot_number, key in enumerate(slot_keys):
            start = slot_number * words
            chunk = values[start:start + words]
            if len(chunk) < words:
                break
            stats['slots_ok'] += 1
            raw = struct.pack('<%dQ' % words, *chunk)
            # Normalise here, once per distinct slot, instead of inside the
            # composition loop below. A rig's spine and hip joints sit in
            # almost every bone's ancestor chain, so the old placement
            # re-normalised the same quaternion once per bone that walked
            # through it: ~4900 normalise+rotate steps per tick at 39 players
            # against ~860 distinct slots. Storing None keeps the existing
            # rejection semantics -- a chain containing an unusable rotation
            # still fails the bone.
            trs[key] = (
                struct.unpack_from('<3f', raw, 0x00),
                _normalize_quaternion(struct.unpack_from('<4f', raw, 0x10)),
                struct.unpack_from('<3f', raw, 0x20),
            )

        world = {}
        for pm, plan, _, _ in jobs:
            for bone_id in plan:
                entry = chains.get((pm, bone_id))
                if entry is None:
                    stats['rej_chain'] += 1
                    continue
                hierarchy, chain = entry[0], entry[1]
                seed = trs.get((hierarchy, chain[0]))
                if seed is None:
                    stats['rej_chain'] += 1
                    continue
                position = seed[0]
                broken = False
                for ancestor in chain[1:]:
                    node = trs.get((hierarchy, ancestor))
                    if node is None:
                        broken = True
                        break
                    translation, quaternion, scale = node
                    if quaternion is None:
                        stats['rej_quat'] += 1
                        broken = True
                        break
                    # scale → rotate → translate, matching the reference kernel
                    position = (
                        position[0] * scale[0],
                        position[1] * scale[1],
                        position[2] * scale[2],
                    )
                    position = _rotate_vector(quaternion, position)
                    position = (
                        position[0] + translation[0],
                        position[1] + translation[1],
                        position[2] + translation[2],
                    )
                if broken or not all(math.isfinite(c) for c in position):
                    if not broken:
                        stats['rej_chain'] += 1
                    continue
                world[(pm, bone_id)] = position
        return world

    def _finish_bone_positions(self, jobs, world, stats):
        """Box/anchor gates and per-rig outcome, shared by both compose paths."""
        anchor_radius = self._bone_anchor_radius()
        anchor_radius_sq = anchor_radius * anchor_radius
        results = {}
        for pm, plan, anchor, _ in jobs:
            bones = {}
            composed_here = 0
            anchor_rejected_here = 0
            # Per-pm, not the global stats['worst_anchor']: the decision below
            # is about THIS player's rig, and one broken rig would otherwise
            # condemn every other player sampled in the same batch.
            worst_here_sq = 0.0
            for bone_id in plan:
                position = world.get((pm, bone_id))
                if position is None:
                    continue
                if not (
                    abs(position[0]) < 6000.0
                    and -200.0 < position[1] < 2000.0
                    and abs(position[2]) < 6000.0
                ):
                    stats['rej_box'] += 1
                    continue
                stats['composed'] += 1
                composed_here += 1
                dx = position[0] - anchor[0]
                dy = position[1] - anchor[1]
                dz = position[2] - anchor[2]
                offset_sq = dx * dx + dy * dy + dz * dz
                if offset_sq > stats['worst_anchor']:
                    stats['worst_anchor'] = offset_sq
                if offset_sq > worst_here_sq:
                    worst_here_sq = offset_sq
                if offset_sq <= anchor_radius_sq:
                    bones[bone_id] = position
                else:
                    stats['rej_anchor'] += 1
                    anchor_rejected_here += 1
            # Recorded so the caller can tell "this rig is broken" (nothing
            # composed) from "this rig is fine, the anchor gate disagreed with
            # the networked position" — only the first deserves invalidation.
            self._bone_last_outcome[pm] = (
                composed_here, anchor_rejected_here, worst_here_sq,
            )
            if bones:
                results[pm] = bones
                # TEMP: flag any direct skeletal edge stretched past what a
                # real limb segment can span, and dump the raw slot index
                # for every bone so a bad idx (wrong Transform slot, not a
                # bad world position) jumps out directly. See conversation:
                # skeleton connects hand/foot/head/pelvis in ways that pass
                # every existing gate (rej_*, worst_anchor) but are still
                # anatomically wrong.
                suspect = []
                for a, b in legacy.SCI_BONE_LINKS:
                    pa = bones.get(a)
                    pb = bones.get(b)
                    if pa is None or pb is None:
                        continue
                    d = math.dist(pa, pb)
                    if d > 1.0:
                        suspect.append((a, b, round(d, 2)))
                if suspect:
                    idx_map = {
                        bid: plan[bid][1] for bid in bones if bid in plan
                    }
                    self._debug(
                        f"[BONE-LINK] pm=0x{pm:X} SUSPECT={suspect} "
                        f"idx={idx_map}",
                        interval=2.0,
                    )
        return results

    # ------------------------------------------------------------------
    # Main per-frame batch
    # ------------------------------------------------------------------
    def _read_player_frame_batch(self, pm_ptrs):
        """Read positions, vitals and skeleton in one coherent IOCTL batch."""
        if not pm_ptrs:
            self._resolve_bone_slots_batch({})
            return None, [], {}, {}, {}, {}, {}, {}

        t_phase = time.perf_counter()
        d_rig = 0.0
        d_batch = 0.0
        d_bones = 0.0
        # Wall time alone cannot tell "one slow read" from "six fast ones", and
        # at ~15 ms of fixed driver round-trip per call the call *count* is the
        # number that actually drives the tick. Count them per sub-phase so the
        # log says which one to attack.
        io0 = self.m.io_calls
        io_map = io_batch = io_bones = 0
        self._refresh_pm_to_bp_cache(pm_ptrs)
        d_map = (time.perf_counter() - t_phase) * 1000
        io_map = self.m.io_calls - io0
        active_set = set(pm_ptrs)
        cached_pm_to_bp = {
            pm: self._pm_bp_cache.get(pm, 0)
            for pm in pm_ptrs
            if legacy._valid_user_ptr(self._pm_bp_cache.get(pm, 0))
        }
        t_mark = time.perf_counter()
        self._resolve_bone_slots_batch(cached_pm_to_bp)
        d_rig += (time.perf_counter() - t_mark) * 1000

        origin = getattr(self, "local_pos", None)
        if origin is None and self._last_local_pm:
            origin = self._pm_pos_cache.get(self._last_local_pm)
        skeleton_candidates = []
        # Why a player did *not* become a skeleton candidate. `rig=N cand=M`
        # with M far below N is the overlay's most common visible failure --
        # players with a box and nothing else -- and the five reasons are not
        # distinguishable from the outside. Counted, never guessed.
        reject = {'origin': 0, 'local': 0, 'sleeping': 0, 'norig': 0,
                  'nopos': 0, 'far': 0}
        if origin is None:
            reject['origin'] = len(pm_ptrs)
        else:
            for pm in pm_ptrs:
                if pm == self._last_local_pm:
                    reject['local'] += 1
                    continue
                if self._sleeping_cache.get(pm, False):
                    reject['sleeping'] += 1
                    continue
                plan = self._bone_slot_cache.get(pm, {})
                if len(plan) < BONE_MIN_VALID:
                    reject['norig'] += 1
                    continue
                pos = self._pm_pos_cache.get(pm)
                if pos is None:
                    reject['nopos'] += 1
                    continue
                distance = self._dist3(pos, origin)
                if distance > legacy.SKELETON_MAX_DISTANCE:
                    reject['far'] += 1
                    continue
                skeleton_candidates.append((distance, pm))
        self._bone_reject = reject
        skeleton_candidates.sort()
        tracked_pms = [
            pm
            for _, pm in skeleton_candidates[:BONE_MAX_TRACKED_PLAYERS]
        ]
        sampled_pms = set()
        now_sample = time.perf_counter()
        if tracked_pms and now_sample >= self._next_bone_sample_at:
            count = min(BONE_MAX_PLAYERS_PER_BATCH, len(tracked_pms))
            start = self._bone_sample_cursor % len(tracked_pms)
            sampled_pms = {
                tracked_pms[(start + offset) % len(tracked_pms)]
                for offset in range(count)
            }
            self._bone_sample_cursor = (start + count) % len(tracked_pms)
            self._next_bone_sample_at = now_sample + BONE_SAMPLE_INTERVAL

        # Plan the skeleton reads *before* the frame batch is built, so
        # their addresses can ride in the same transaction. This is only
        # possible because the hierarchy pointers they need are cached
        # topology -- see _hierarchy_ptr_cache and _plan_bone_reads. A
        # hierarchy that has never been seen simply is not planned this
        # tick; the frame batch below reads its pointers, and the next
        # sample of that player picks it up.
        now_hier = time.perf_counter()
        # Bounded like the sibling parent-index cache: entries are keyed by
        # a game-side address, so a long session with a lot of player churn
        # would otherwise accumulate one per rig that ever existed.
        if len(self._hierarchy_ptr_cache) > 128:
            self._hierarchy_ptr_cache = {
                hierarchy: entry
                for hierarchy, entry in self._hierarchy_ptr_cache.items()
                if now_hier < entry[2]
            }
        plan_jobs = []
        planned_hier = {}
        for pm in sampled_pms:
            plan = self._bone_slot_cache.get(pm, {})
            if len(plan) < BONE_MIN_VALID:
                continue
            hierarchy_ptrs = {}
            complete = True
            for hierarchy, _ in plan.values():
                if hierarchy in hierarchy_ptrs:
                    continue
                cached = self._hierarchy_ptr_cache.get(hierarchy)
                if cached is None or now_hier >= cached[2]:
                    complete = False
                    break
                hierarchy_ptrs[hierarchy] = (cached[0], cached[1])
            if not complete:
                continue
            plan_jobs.append((pm, plan, None, hierarchy_ptrs))
            planned_hier.update(hierarchy_ptrs)
        planned = self._plan_bone_reads(plan_jobs) if plan_jobs else None
        planned_pms = {pm for pm, _, _, _ in plan_jobs}

        # Build the batch read for position, health, flags, rig jobs, the
        # hierarchy TRS/parent-array pointers and -- appended below -- every
        # TRS slot the skeletons need. One coherent IOCTL for the whole tick.
        descriptors = []
        addresses = []

        def add(pm, field, address):
            descriptors.append((pm, field))
            addresses.append(address)

        vel_probe = self._velocity_offsets()
        rig_fail_info = getattr(self, "_bone_resolve_fail_info", None) or {}
        for pm in pm_ptrs:
            add(pm, "local", pm + legacy.OFF.pm_is_local_player)
            add(pm, "server_0_lo", pm + legacy.OFF.position_pm)
            add(pm, "server_0_hi", pm + legacy.OFF.position_pm + 8)
            # Reading the engine's own velocity removes the structural lag in
            # extrapolating from two position samples: a backward difference is
            # an *average* over the last interval, so it is always half a tick
            # behind, and the 0.70 lerp smoothing on top of it adds more. That
            # lag is exactly what shows up as boxes and skeletons trailing a
            # moving player. See OFF.velocity_pm_a/_b for why two are probed.
            for tag, offset in vel_probe:
                add(pm, tag + "_lo", pm + offset)
                add(pm, tag + "_hi", pm + offset + 8)

            # NOTE: no transform-slot read here on purpose. See the comment on
            # _read_transform_positions_batch in legacy_runtime: the value at
            # `transformArray + 0x30*idx` is the root bone's *local* TRS
            # translation, not a world position, so it aliased across players
            # (the "everybody stands in the same spot" bug). Cl1kExternal's
            # BasePlayer::GetPosition reads PlayerModel+0x2F8 and nothing else,
            # so that is what we do — see "server_0" below.

            bp_hint = self._pm_bp_cache.get(pm)
            if legacy._valid_user_ptr(bp_hint):
                # BasePlayer.playerModel must still point back at THIS pm.
                # _pm_bp_cache is only ever pruned when a pm leaves the roster,
                # so nothing re-checked that a cached pair was still the same
                # pair -- and Unity pools PlayerModel objects, so when one
                # player despawns and another spawns the client can hand the
                # new player the same PlayerModel address. The pm never leaves
                # `active`, the mapping is never re-resolved, and every
                # bp-derived field (flags, health, name, held item) silently
                # keeps describing the PREVIOUS occupant: a player who really
                # is asleep somewhere else, rendered onto someone who is
                # walking around. Self-healing offset, not the OFF constant --
                # _map_baseplayers_to_pm updates it when the build moves.
                add(pm, "bp_backref", bp_hint + self._player_model_offset)
                add(pm, "player_flags", bp_hint + legacy.OFF.playerFlags)
                add(pm, "health_0", bp_hint + legacy.OFF.lifestate)
                add(pm, "health_1", bp_hint + legacy.OFF.lifestate + 8)
                add(pm, "health_2", bp_hint + legacy.OFF.lifestate + 16)
                # A rig that failed because Model was null (player dead or
                # still respawning) waits behind a backoff of up to 20 s. One
                # slot per such player watches for the Model to appear instead.
                failed = rig_fail_info.get(pm)
                if (failed is not None and failed[0] == "model"
                        and pm not in self._bone_resolve_jobs
                        and pm not in self._bone_slot_cache):
                    add(pm, "rig_probe", bp_hint + legacy.OFF.model)

            job = self._bone_resolve_jobs.get(pm)
            if job is not None:
                self._append_rig_job_reads(pm, job, add)

            if pm in sampled_pms:
                plan = self._bone_slot_cache.get(pm, {})
                if len(plan) >= BONE_MIN_VALID:
                    for hierarchy in {h for h, _ in plan.values()}:
                        add(pm, ("hier_lo", hierarchy), hierarchy + 0x18)
                        add(pm, ("hier_hi", hierarchy), hierarchy + 0x20)

        probe_pm = self._health_probe_pm
        if probe_pm is not None and probe_pm in active_set:
            probe_bp = self._pm_bp_cache.get(probe_pm)
            if legacy._valid_user_ptr(probe_bp):
                probe_base = probe_bp + legacy.OFF.lifestate - HEALTH_PROBE_BACK
                for word in range(HEALTH_PROBE_WORDS):
                    add(probe_pm, ("hraw", word), probe_base + word * 8)

        t_mark = time.perf_counter()
        values = self.m.batch_u64(addresses, attempts=2)
        d_batch = (time.perf_counter() - t_mark) * 1000
        io_batch = self.m.io_calls - io0 - io_map
        by_pm = {}
        for (pm, field), value in zip(descriptors, values):
            by_pm.setdefault(pm, {})[field] = value

        if not hasattr(self, "_sleeping_seen_at"):
            self._sleeping_seen_at = {}
        # Evict recycled pm->bp pairs before anything is seeded from the
        # caches they poisoned. Cheap: the address was already in this tick's
        # batch, so this costs no extra IOCTL.
        null_backref = getattr(self, "_null_backref_ticks", None)
        if null_backref is None:
            null_backref = self._null_backref_ticks = {}
        for pm in pm_ptrs:
            backref = by_pm.get(pm, {}).get("bp_backref")
            if backref is None or backref == pm:
                null_backref.pop(pm, None)
                continue
            # A valid pointer to a DIFFERENT PlayerModel is recycling. A zero
            # is either one missed batch entry -- evicting on that would thrash
            # the mapping and re-arm the ~600 ms walk -- or a BasePlayer the
            # game destroyed: a respawned player keeps the pm address but gets
            # a new BasePlayer, and the old one's Model reads null forever
            # (every [RIG-FAIL] in the 2026-09-15 log was stage 'model'). Only a
            # run of zeros is evidence.
            destroyed = not legacy._valid_user_ptr(backref)
            if destroyed:
                fields = by_pm.get(pm, {})
                # A failed IOCTL reads 0 everywhere, position included. Only a
                # zero backref next to a PlayerModel position that WAS read
                # is a destroyed BasePlayer -- otherwise one bad batch would
                # mark every player dead and hide the whole ESP.
                if not (fields.get("server_0_lo") or fields.get("server_0_hi")):
                    continue
                missed = null_backref.get(pm, 0) + 1
                null_backref[pm] = missed
                if missed < NULL_BACKREF_TICKS:
                    continue
            null_backref.pop(pm, None)
            stale_bp = self._pm_bp_cache.pop(pm, 0)
            if destroyed and not (by_pm.get(pm, {}).get("local", 0) & 0xFF):
                self._mark_pm_dead(pm, stale_bp)
            self._health_cache.pop(pm, None)
            self._sleeping_cache.pop(pm, None)
            self._sleeping_seen_at.pop(pm, None)
            self._bp_attempted.pop(pm, None)
            self._invalidate_bone_rig(pm)
            print(
                f"[PM2BP-DBG] pm=0x{pm:X} was mapped to bp=0x{stale_bp:X} "
                f"whose playerModel is now 0x{backref:X} -- recycled, evicted",
                flush=True,
            )
        if len(null_backref) > len(active_set):
            self._null_backref_ticks = {
                pm: n for pm, n in null_backref.items() if pm in active_set
            }

        for pm, failed in list(rig_fail_info.items()):
            probe = by_pm.get(pm, {}).get("rig_probe")
            if probe is not None and legacy._valid_user_ptr(probe):
                rig_fail_info.pop(pm, None)
                self._bone_resolve_fails.pop(pm, None)
                self._bone_resolve_retry_at.pop(pm, None)

        for pm in pm_ptrs:
            self._consume_rig_job(pm, by_pm.get(pm, {}))

        def decode_position(lo, hi):
            """Returns (pos, why) -- why is None on success.

            This is the LIVE path (model.py overrides legacy_runtime.py's
            _read_player_frame_batch without super() -- see
            rust-esp-live-code-lives-in-overrides). An earlier [PM-DROP]
            reject-reason fix landed in legacy_runtime.py's copy of this
            same function first, which is never actually called for the
            running RustGameModel -- every drop kept reporting the same
            "no candidate produced" default regardless of the real cause
            because that write never ran. Fixed here instead.
            """
            raw = struct.pack("<QQ", lo, hi)
            pos = struct.unpack_from("<fff", raw)
            if not all(math.isfinite(c) for c in pos):
                return None, "non-finite read (garbage or failed read)"
            if pos == (0.0, 0.0, 0.0):
                return None, "read returned zeros"
            if not (abs(pos[0]) < 6000 and abs(pos[2]) < 6000):
                return None, f"x/z out of map bounds ({pos[0]:.0f},{pos[2]:.0f})"
            if not (-200 < pos[1] < 2000):
                return None, f"y out of range ({pos[1]:.0f})"
            return pos, None

        def decode_velocity(lo, hi):
            raw = struct.pack("<QQ", lo, hi)
            vel = struct.unpack_from("<fff", raw)
            if all(math.isfinite(c) and abs(c) < 500.0 for c in vel):
                return vel
            return None

        pm_to_bp = {}
        transform_positions = {}
        server_positions = {}
        vel_by_pm = {}
        health_by_pm = {
            pm: health
            for pm, health in self._health_cache.items()
            if pm in active_set
        }
        # Seeded from the cache so a one-tick read failure does not make a
        # genuinely sleeping player flicker awake -- but bounded, because an
        # unbounded seed is what let a single stale True outlive the player it
        # described. After SLEEPING_STALE_TTL with no fresh flags read we stop
        # asserting anything and fall back to "awake", which is what the
        # renderer treats as the no-evidence case anyway.
        now_frame = time.perf_counter()
        seed_cutoff = now_frame - SLEEPING_STALE_TTL
        sleeping_by_pm = {
            pm: sleeping
            for pm, sleeping in self._sleeping_cache.items()
            if pm in active_set
            and self._sleeping_seen_at.get(pm, 0.0) >= seed_cutoff
        }
        bone_positions_by_pm = {}
        bone_jobs = []
        # Health accounting. "It shows a number but it is not functional" has
        # exactly two shapes and the displayed value cannot tell them apart:
        # the read is happening and the value genuinely never changes (bots
        # on a practice server respawn at full health), or the read is being
        # *rejected* every tick and health_by_pm is serving the last value
        # that passed -- which looks identical on screen and is frozen. This
        # counts which one is happening.
        health_read = 0
        health_fresh = 0
        health_damaged = 0
        health_reject_sample = None
        local_pm = next(
            (
                pm
                for pm in pm_ptrs
                if by_pm.get(pm, {}).get("local", 0) & 0xFF
            ),
            None,
        )

        for pm in pm_ptrs:
            fields = by_pm.get(pm, {})
            bp = self._pm_bp_cache.get(pm, 0)
            if legacy._valid_user_ptr(bp):
                pm_to_bp[pm] = bp

            if "player_flags" in fields:
                player_flags = fields["player_flags"] & 0xFFFFFFFF
                sleeping_by_pm[pm] = bool(
                    player_flags & legacy.PLAYER_FLAG_SLEEPING
                )
                self._sleeping_seen_at[pm] = now_frame
            if sleeping_by_pm.get(pm, False):
                self._bone_position_cache.pop(pm, None)

            # transform_positions stays empty: PlayerModel+0x2F8 is the single
            # source of truth for a player's world position (Cl1kExternal parity).
            transform_pos = None

            candidates = []
            reject_why = None
            for offset, prefix in ((legacy.OFF.position_pm, "server_0"),):
                pos, why = decode_position(
                    fields.get(prefix + "_lo", 0),
                    fields.get(prefix + "_hi", 0),
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
                candidates.append((offset, pos))
            why_map = getattr(self, "_pos_reject_why", None)
            if why_map is None:
                why_map = self._pos_reject_why = {}
            if candidates:
                why_map.pop(pm, None)
            elif reject_why is not None:
                why_map[pm] = reject_why
            selected = self._select_pm_position(pm, candidates)
            if selected is not None:
                server_positions[pm] = selected
                vel = self._track_velocity(
                    pm,
                    selected,
                    {
                        tag[-1]: decode_velocity(
                            fields.get(tag + "_lo", 0),
                            fields.get(tag + "_hi", 0),
                        )
                        for tag, _ in vel_probe
                    },
                    t_phase,
                )
                if vel is not None:
                    vel_by_pm[pm] = vel

            # Queue this player's skeleton; every eligible player is composed
            # together after the loop so the TRS reads collapse into one batch.
            plan = self._bone_slot_cache.get(pm, {})
            anchor = transform_pos if transform_pos is not None else selected
            if (
                pm in sampled_pms
                and len(plan) >= BONE_MIN_VALID
                and pm != local_pm
                and anchor is not None
                and not sleeping_by_pm.get(pm, False)
            ):
                fresh = {
                    hierarchy: (
                        fields.get(("hier_lo", hierarchy), 0),
                        fields.get(("hier_hi", hierarchy), 0),
                    )
                    for hierarchy, _ in plan.values()
                }
                # These reads are what keeps the cache honest, and they are
                # in the same transaction as the TRS words planned against
                # it. If a pointer moved between the two, the words we just
                # read describe a rig that no longer exists -- so drop the
                # sample rather than compose from it. Recording no outcome
                # at all (instead of an empty one) matters: an empty sample
                # counts towards the three-strike rig invalidation, and a
                # rig that merely moved is not a broken rig.
                stale = any(
                    hierarchy in planned_hier
                    and planned_hier[hierarchy] != ptrs
                    for hierarchy, ptrs in fresh.items()
                )
                for hierarchy, ptrs in fresh.items():
                    if legacy._valid_user_ptr(ptrs[0]) and legacy._valid_user_ptr(ptrs[1]):
                        self._hierarchy_ptr_cache[hierarchy] = (
                            ptrs[0], ptrs[1], now_hier + HIERARCHY_PTR_CACHE_TTL,
                        )
                if pm in planned_pms and not stale:
                    bone_jobs.append((pm, plan, anchor, fresh))
                elif stale:
                    self._bone_stale_rigs += 1
                    self._bone_stale_window = (
                        getattr(self, '_bone_stale_window', 0) + 1
                    )

            if all(
                key in fields
                for key in ("health_0", "health_1", "health_2")
            ):
                health_read += 1
                raw = struct.pack(
                    "<QQQ",
                    fields["health_0"],
                    fields["health_1"],
                    fields["health_2"],
                )
                life_state = struct.unpack_from("<I", raw, 0)[0]
                health = struct.unpack_from(
                    "<f",
                    raw,
                    legacy.OFF._health - legacy.OFF.lifestate,
                )[0]
                max_health = struct.unpack_from(
                    "<f",
                    raw,
                    legacy.OFF._maxHealth - legacy.OFF.lifestate,
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
                    health_fresh += 1
                    if health < max_health - 0.01:
                        health_damaged += 1
                elif health_reject_sample is None:
                    health_reject_sample = (pm, life_state, health, max_health)

        short = 0
        anchor_only = 0
        broken = 0
        if bone_jobs and planned is not None:
            t_mark = time.perf_counter()
            io_before_bones = self.m.io_calls
            chains, slot_keys, bone_addrs = planned
            bone_values = self.m.batch_u64(bone_addrs, attempts=1)
            sampled_bones = self._compose_bones(
                bone_jobs, chains, slot_keys, bone_values,
            )
            d_bones = (time.perf_counter() - t_mark) * 1000
            io_bones = self.m.io_calls - io_before_bones
            for pm, _, _, _ in bone_jobs:
                bones = sampled_bones.get(pm, {})
                outcome = self._record_bone_sample_outcome(pm, bones)
                if outcome == 'ok':
                    bone_positions_by_pm[pm] = bones
                    continue
                short += 1
                if outcome == 'anchor_only':
                    anchor_only += 1
                elif outcome == 'broken':
                    broken += 1

        self._log_bone_debug(
            pm_ptrs, local_pm, origin, skeleton_candidates,
            sampled_pms, bone_jobs, bone_positions_by_pm, short, anchor_only,
            broken,
        )

        if local_pm:
            self._last_local_pm = local_pm
        elif self._last_local_pm in active_set:
            local_pm = self._last_local_pm

        self._update_dead_marks(server_positions)

        enemies = [pm for pm in pm_ptrs if pm != local_pm]
        seen_recently = getattr(self, "_pm_bp_seen_at", ())
        self._pm_bp_cache = {
            pm: bp
            for pm, bp in self._pm_bp_cache.items()
            if pm in active_set or pm in seen_recently
        }
        active_pm_to_bp = {
            pm: self._pm_bp_cache.get(pm, 0)
            for pm in pm_ptrs
            if legacy._valid_user_ptr(self._pm_bp_cache.get(pm, 0))
        }
        t_mark = time.perf_counter()
        self._resolve_bone_slots_batch(active_pm_to_bp)
        d_rig += (time.perf_counter() - t_mark) * 1000
        self._health_cache = health_by_pm
        self._sleeping_cache = sleeping_by_pm
        self._log_health_debug(
            len(pm_ptrs), health_read, health_fresh, health_damaged,
            health_reject_sample,
        )
        self._probe_health_window(by_pm, server_positions, local_pm)
        # Consumed by _loop when it builds the player dicts; the frame-batch
        # return tuple is fixed-arity and shared with the base class, so this
        # rides on the instance instead of widening it.
        self._pm_vel_cache = vel_by_pm
        for pm in list(self._vel_probe_prev):
            if pm not in active_set:
                del self._vel_probe_prev[pm]

        d_total = (time.perf_counter() - t_phase) * 1000

        acc = self._pos_profile_sum
        acc['d_map'] += d_map
        acc['d_rig'] += d_rig
        acc['d_batch'] += d_batch
        acc['d_bones'] += d_bones
        acc['io_batch'] += io_batch
        acc['io_bones'] += io_bones
        acc['n'] += 1
        now_profile = time.perf_counter()
        if now_profile >= self._next_pos_profile_at:
            self._next_pos_profile_at = now_profile + 1.0
            n = acc['n'] or 1
            # `batch=` is the critical-path IOCTL (positions, health, flags)
            # and `bones=` is bone TRS IOCTL + composition. Splitting these
            # means a bone-batch timeout only drops skeletons, not the whole
            # ESP.
            print(
                f"[POS-PROFILE] n={acc['n']} avg "
                f"map={acc['d_map'] / n:.2f}ms rig={acc['d_rig'] / n:.2f}ms "
                f"batch={acc['d_batch'] / n:.2f}ms/{acc['io_batch'] / n:.2f}io "
                f"bones={acc['d_bones'] / n:.2f}ms/{acc['io_bones'] / n:.2f}io",
                flush=True,
            )
            for key in acc:
                acc[key] = 0 if key in ('io_batch', 'io_bones', 'n') else 0.0

        if d_total >= POS_PHASE_LOG_MS:
            # bp_src names which branch of _resolve_pm_to_bp paid: "cache" is
            # free, "base_networkable" means the BaseNetworkable buffer walk ran,
            # "offset_scan" means even that missed and the brute-force offset
            # scan ran on top of it.
            print(
                f"[POS-DBG] total={d_total:.1f}ms "
                f"(map={d_map:.1f}ms/{io_map}io, rig={d_rig:.1f}ms, "
                f"batch={d_batch:.1f}ms/{io_batch}io, "
                f"bones={d_bones:.1f}ms/{io_bones}io) pm={len(pm_ptrs)} "
                f"addrs={len(addresses)} bp_src={self._last_bp_source}"
                f"{self._format_bp_bg()}",
                flush=True,
            )
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

    def _mark_pm_dead(self, pm, destroyed_bp):
        """Hide a player whose BasePlayer was just destroyed (see DEAD_MARK_MAX).

        Also drops the position smoothing state, so that when the pm comes back
        at its respawn point the box starts there instead of gliding over from
        the corpse.
        """
        dead = getattr(self, "_pm_dead", None)
        if dead is None:
            dead = self._pm_dead = {}
        last_pos = getattr(self, "_pm_pos_cache", {}).get(pm)
        dead[pm] = [time.perf_counter(), destroyed_bp, last_pos]
        for cache_name in ("_pm_raw_pos_cache", "_pm_smooth_pos_cache",
                           "_pm_pos_time_cache", "_pm_jump_state"):
            cache = getattr(self, cache_name, None)
            if cache is not None:
                cache.pop(pm, None)
        print(
            f"[DEATH] pm=0x{pm:X} bp=0x{destroyed_bp:X} destroyed -> box and "
            f"skeleton hidden",
            flush=True,
        )

    def _update_dead_marks(self, positions):
        """Lift a death mark once the player is provably alive again."""
        dead = getattr(self, "_pm_dead", None)
        if not dead:
            return
        now = time.perf_counter()
        for pm, mark in list(dead.items()):
            since, destroyed_bp, last_pos = mark
            bp = self._pm_bp_cache.get(pm, 0)
            pos = positions.get(pm)
            why = None
            # A walk started before the death can still hand back the old,
            # destroyed bp; only a different one is a new life.
            if legacy._valid_user_ptr(bp) and bp != destroyed_bp:
                why = f"new bp=0x{bp:X}"
            elif pos is not None and last_pos is not None:
                jump = self._dist3(pos, last_pos)
                if jump > DEAD_RESPAWN_JUMP:
                    why = f"respawn jump {jump:.0f}m"
            if why is None and now - since > DEAD_MARK_MAX:
                why = "timeout"
            if why is None:
                if pos is not None:
                    mark[2] = pos
                continue
            del dead[pm]
            print(
                f"[DEATH] pm=0x{pm:X} alive again after {now - since:.1f}s ({why})",
                flush=True,
            )

    def _probe_health_window(self, by_pm, positions, local_pm):
        """Print the raw floats around lifestate for the nearest player.

        See HEALTH_PROBE_BACK for why this exists. One player, once a
        second: shoot whoever is closest and whichever number in the line
        drops is _health. The current guess (OFF._health = 0x2B4) appears
        in the line like any other, so it either moves and is vindicated or
        it does not and the neighbour that did is the answer.

        Runs after the batch, so what it prints is this tick's memory. The
        subject for the *next* tick is chosen at the end, because the
        addresses have to be in the batch before the read happens.
        """
        probe_pm = self._health_probe_pm
        fields = by_pm.get(probe_pm, {}) if probe_pm is not None else {}
        now = time.perf_counter()
        if fields and now >= self._next_health_probe_at:
            words = [fields.get(("hraw", w)) for w in range(HEALTH_PROBE_WORDS)]
            if all(w is not None for w in words):
                self._next_health_probe_at = now + HEALTH_PROBE_INTERVAL
                raw = struct.pack("<%dQ" % HEALTH_PROBE_WORDS, *words)
                base = legacy.OFF.lifestate - HEALTH_PROBE_BACK
                cells = []
                for byte in range(0, len(raw), 4):
                    (as_float,) = struct.unpack_from("<f", raw, byte)
                    (as_int,) = struct.unpack_from("<I", raw, byte)
                    offset = base + byte
                    mark = "*" if offset == legacy.OFF._health else (
                        "^" if offset == legacy.OFF._maxHealth else ""
                    )
                    if as_int == 0:
                        text = "0"
                    elif math.isfinite(as_float) and 0.01 <= abs(as_float) < 1e6:
                        text = f"{as_float:.1f}"
                    elif as_int < (1 << 24):
                        text = f"i{as_int}"
                    else:
                        text = "-"
                    cells.append(f"{mark}{offset:03x}={text}")
                print(
                    f"[HEALTH-RAW] pm=0x{probe_pm:x} " + " ".join(cells),
                    flush=True,
                )

        # Pick next tick's subject: the nearest player to the local one, so
        # it is whoever the user is most likely to be shooting.
        origin = positions.get(local_pm) if local_pm is not None else None
        if origin is None:
            origin = getattr(self, "local_pos", None)
        if origin is None:
            self._health_probe_pm = None
            return
        nearest = None
        nearest_d2 = None
        for pm, pos in positions.items():
            if pm == local_pm:
                continue
            dx = pos[0] - origin[0]
            dy = pos[1] - origin[1]
            dz = pos[2] - origin[2]
            d2 = dx * dx + dy * dy + dz * dz
            if nearest_d2 is None or d2 < nearest_d2:
                nearest, nearest_d2 = pm, d2
        self._health_probe_pm = nearest

    def _log_health_debug(self, players, read, fresh, damaged, rejected):
        """Say whether health is being read, accepted, and ever changing.

        `read` counts players whose BasePlayer pointer was known well enough
        to put the three health words in the batch; `fresh` counts those
        whose values passed the sanity filter this tick. read > fresh means
        the on-screen number is a *cached* one and will sit still no matter
        what happens in game. `damaged` counts players actually below full
        health -- if that is 0 while fresh is high, the read is working and
        nothing on this server is losing health.
        """
        if not hasattr(self, '_next_health_debug_at'):
            self._next_health_debug_at = 0.0
        now = time.perf_counter()
        if now < self._next_health_debug_at:
            return
        self._next_health_debug_at = now + 1.0
        detail = ""
        if rejected is not None:
            pm, life, hp, mx = rejected
            detail = (
                f" rejected pm=0x{pm:X} life={life} hp={hp:.1f} max={mx:.1f}"
            )
        print(
            f"[HEALTH-DBG] players={players} read={read} fresh={fresh} "
            f"cached={read - fresh} damaged={damaged}{detail}",
            flush=True,
        )

    def _log_bone_debug(self, pm_ptrs, local_pm, origin, candidates,
                        sampled, jobs, produced, short, anchor_only=0,
                        broken=0):
        """One line per second naming the exact gate the skeletons die at.

        The bone pipeline has seven sequential gates and a failure at any one of
        them looks identical from the outside (`sk=0`), so each is counted:

          rig     rigs fully resolved      (Model -> boneTransforms -> access)
          job     rig resolves still in flight, by stage
          origin  local player position known — without it nothing is a candidate
          cand    players inside SKELETON_MAX_DISTANCE with a resolved rig
          queued  candidates that also passed the alive/awake/anchor checks
          hier    TransformHierarchy parent-index arrays actually read
          slots   distinct TRS slots requested vs. decoded from the batch
          out     players that produced >= BONE_MIN_VALID bones

        The rej_* counters and worst= (largest bone-to-anchor distance seen,
        against BONE_ANCHOR_RADIUS) separate "composition produced garbage" from
        "composition was fine but the anchor check threw it away".
        """
        now = time.perf_counter()
        if now < self._next_bone_debug_at:
            return
        self._next_bone_debug_at = now + 1.0
        # stale= was a lifetime total, so a live capture showing stale=2943
        # could not distinguish 2943 events spread over an hour from 2943 in
        # the last second -- and every one of them is a tick where that player
        # got no bone sample, i.e. a frozen pose. Report the delta since the
        # previous line alongside the total.
        stale_window = getattr(self, '_bone_stale_window', 0)
        self._bone_stale_window = 0

        stages = {}
        for job in self._bone_resolve_jobs.values():
            stage = job.get("stage", "?")
            stages[stage] = stages.get(stage, 0) + 1
        rigs = sum(
            1 for plan in self._bone_slot_cache.values()
            if len(plan) >= BONE_MIN_VALID
        )
        stats = self._bone_debug
        worst = math.sqrt(stats.get('worst_anchor', 0.0))
        bone_counts = [len(b) for b in produced.values()]
        limit = self._bone_anchor_radius()
        fail_stages = getattr(self, "_rig_fail_stages", None)
        if fail_stages:
            fails = getattr(self, "_bone_resolve_fails", None) or {}
            retry_at = getattr(self, "_bone_resolve_retry_at", None) or {}
            waits = sorted(
                (retry_at[pm] - now for pm, n in fails.items()
                 if n >= 3 and pm in retry_at),
                reverse=True,
            )
            print(
                f"[RIG-FAIL] last 1s by stage {fail_stages} | cold={len(waits)} "
                f"max_fails={max(fails.values()) if fails else 0} "
                f"longest_wait={waits[0] if waits else 0.0:.1f}s",
                flush=True,
            )
            self._rig_fail_stages = {}
        print(
            "[BONE-DBG] pm=%d local=%s origin=%s rig=%d job=%d%s "
            "cand=%d%s sampled=%d queued=%d stale=%d(+%d) | hier=%d/%d pread=%d slots=%d/%d "
            "composed=%d out=%d short=%d anchor_only=%d broken=%d avg=%.1f | "
            "rej chain=%d quat=%d box=%d anchor=%d worst=%.2fm (limit %.2fm)"
            % (
                len(pm_ptrs), "y" if local_pm else "n", "y" if origin else "n",
                rigs, len(self._bone_resolve_jobs),
                (" " + repr(stages)) if stages else "",
                len(candidates), self._format_bone_rejects(),
                len(sampled), len(jobs),
                getattr(self, '_bone_stale_rigs', 0), stale_window,
                stats.get('hier_parents', 0), stats.get('hier_needed', 0),
                stats.get('parent_reads', 0),
                stats.get('slots_ok', 0), stats.get('slots_req', 0),
                stats.get('composed', 0), len(produced), short, anchor_only,
                broken,
                (sum(bone_counts) / len(bone_counts)) if bone_counts else 0.0,
                stats.get('rej_chain', 0), stats.get('rej_quat', 0),
                stats.get('rej_box', 0), stats.get('rej_anchor', 0),
                worst, limit,
            ),
            flush=True,
        )

    def _take_snapshot(self):
        """Freeze positions and bones from one coherent worker generation."""
        players = []
        for player in self.players:
            copied = dict(player)
            pos = copied.get("pos")
            if pos is not None:
                copied["pos"] = tuple(pos)
            bones = copied.get("bones")
            if bones:
                copied["bones"] = {
                    bone_id: tuple(bone_pos)
                    for bone_id, bone_pos in bones.items()
                }
            players.append(copied)

        vp = tuple(self.vp_matrix) if self.vp_matrix is not None else None
        world_ents = list(self.world_entities)
        snap = (time.perf_counter(), players, vp, world_ents)
        rig_ready = sum(
            len(plan) >= BONE_MIN_VALID
            for plan in self._bone_slot_cache.values()
        )
        rig_jobs = len(self._bone_resolve_jobs)
        with self._lock:
            self._snap_prev = self._snap_cur
            self._snap_cur = snap
            base_diag = self.diag.split(" | rig=", 1)[0]
            self.diag = f"{base_diag} | rig={rig_ready} job={rig_jobs}"


Mem = legacy.Mem
CameraSampler = legacy.CameraSampler
find_modules = legacy.find_modules
print_module_diagnostics = legacy.print_module_diagnostics
