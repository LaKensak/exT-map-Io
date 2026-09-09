# Architecture — Rust ESP MVC

How this thing is built and how it reads memory. Read this before changing
anything; read [`OFFSET_RECOVERY.md`](OFFSET_RECOVERY.md) when it *stops
working* after a game patch.

---

## 1. The 30-second version

The Rust client is a Unity IL2CPP game. We never inject into it. A kernel
driver reads its memory for us; a Python process asks the driver for bytes,
turns them into game objects, and draws an ImGui overlay on top.

```
RustClient.exe            kernel driver              Python (this package)
  (game memory)  <-------  reads phys/virt  <-------  shared-memory IOCTL
                                                             |
                                                     model  -> parse
                                                     view   -> ImGui overlay
```

Three threads:

| Thread | Rate | Job |
|---|---|---|
| worker (`RustGame._loop`) | `WORKER_HZ = 120` target | walk chains, build player/entity lists |
| camera sampler | `CAMERA_SAMPLE_HZ = 240` (controller.py; the legacy_runtime constant of the same name is unused) | read the view-projection matrix only |
| main / render | monitor refresh | draw the overlay from a snapshot |

The camera lives on its own thread because the view matrix must be fresh at
*draw* time — a stale matrix makes every box lag behind the world, even if the
positions are perfect.

---

## 2. Files

| File | Layer | Contains |
|---|---|---|
| `legacy_runtime.py` | base | `Mem` (driver I/O), `OFF` (all offsets), decrypts, chain walking, the worker tick |
| `model.py` | model | `RustGameModel` — overrides the bone/player-batch parts of `RustGame` |
| `view.py` / `view_base.py` | view | ImGui overlay, boxes, skeletons, labels |
| `controller.py` | controller | startup, thread wiring, the render loop |
| `dump.txt` | data | same-build offsets + decrypt routines (**source of truth**) |

`model.py` subclasses `legacy_runtime.RustGame` and overrides selected methods.
When editing, check whether the method you are touching is overridden — the
legacy copy may be dead code (e.g. the old dict-shaped `_bone_slot_cache`
handling in `legacy_runtime._resolve_bone_slots_batch` is unreachable).

---

## 3. Reading memory

### 3.1 The transport

`Mem` talks to the driver through a named shared-memory section
(`SECTION_NAME`) mapped as a `COMM_SHARED` struct: a command word, a status
word, address/size fields, and a `0x40000`-byte (256 KB) data buffer.

Protocol per call:

1. fill `cr3`, `address`, `size`
2. write `command` (non-zero)
3. spin until the driver writes `command = 0` (`Mem._wait`)
4. read `status`, then `data`

`data` is XOR-obfuscated with a per-address key: `key = (addr ^ 0x5A) & 0xFF`,
undone via the precomputed `XOR_TABLES`. This is why you cannot just memcpy the
buffer — always go through `Mem`.

Commands in use: `1` find process, `2` read, `4` get PEB, `5` batch read u64.

### 3.2 The two read primitives

```python
m.read(addr, size)     # one contiguous block, <= 256 KB (auto-chunks above)
m.batch_u64(addrs)     # N scattered 8-byte reads in ONE IOCTL
```

**`batch_u64` is the workhorse and the single most important performance
tool in the codebase.** One IOCTL costs a driver round-trip; the number of
round-trips per tick — not bytes moved — is what sets the frame time. It
accepts up to 16000 addresses and internally sorts them by page (helping the
driver's virtual→physical cache) before restoring your original ordering, so
**results come back positionally aligned with your input list**.

Convenience wrappers: `u64`, `u64_retry`, `i32_retry`, `u32`, `f32`, `vec3f`,
`read_ptr_array`, `read_retry`, `read_priority` (camera thread, jumps the lock).

### 3.3 The rule for writing fast code here

> Never read in a loop. Collect every address you will need, issue one
> `batch_u64`, then interpret.

The established pattern is *descriptor + address* lists built in parallel:

```python
descriptors, addresses = [], []
def add(pm, field, address):
    descriptors.append((pm, field)); addresses.append(address)

for pm in pm_ptrs:
    add(pm, "server_lo", pm + OFF.position_pm)
    add(pm, "server_hi", pm + OFF.position_pm + 8)

values = self.m.batch_u64(addresses, attempts=1)
by_pm = {}
for (pm, field), value in zip(descriptors, values):
    by_pm.setdefault(pm, {})[field] = value
```

A Vec3 is two u64 reads (`lo`, `hi`) repacked with `struct`, because the
transport is u64-granular:

```python
raw = struct.pack("<QQ", lo, hi)
x, y, z = struct.unpack_from("<fff", raw)
```

Multi-stage pointer chains become *stages*, one batch per depth level, carrying
an index list forward so results stay attributable — see
`_scan_world_entities` and `_read_transform_positions_batch`.

---

## 4. IL2CPP concepts you need

### 4.1 Classes and statics

Every managed object starts with a pointer to its `Il2CppClass` at offset 0.

```
object + 0x00      -> klass
klass  + 0x10      -> const char* class name     (OFF.klass_name)
klass  + 0xB8      -> static fields blob         (OFF.klass_static_fields)
```

Reading the class *name* is how we identify entities — see §6. Class names are
immutable, so `_read_klass_names` caches them permanently.

Klass pointers themselves are found via RVAs into `GameAssembly.dll`
(`OFF.BaseNetworkable_c` etc.). **RVAs change every game build.**

### 4.2 Arrays and lists

```
Il2CppArray + 0x18 -> max_length      (OFF.buffer_count)
Il2CppArray + 0x20 -> element 0       (OFF.array_payload)

BufferList  + 0x10 -> backing array   (OFF.buffer_array)
BufferList  + 0x18 -> live count      (OFF.ListComponent_size)
```

Note `max_length` (capacity) and the BufferList's live count are different
numbers; iterate the live count, validate against capacity.

### 4.3 HiddenValue — the obfuscation layer

Rust hides hot pointers behind `HiddenValue<T>`: the field holds an *encrypted
handle*, not a pointer.

```
wrapper + 0x18 -> encrypted u64        (OFF.hv_slot)
```

Decoding is two steps:

1. **Decrypt** — a per-field routine of rotate/xor/add ops over the two 32-bit
   halves, 2 iterations. `_hv_decrypt(value, ops)` implements the generic form;
   `decrypt_bn0`, `decrypt_bn1`, `decrypt_local_player`,
   `decrypt_player_inventory`, `decrypt_player_eyes`, `decrypt_cl_active_item`
   are the instances. **Each is unique per field and rotates every build** —
   re-derive from `dump.txt`.

2. **Resolve the handle** — `resolve_tagged_handle()`. The decrypted value
   points *inside* a 0x2000-byte GC handle-table page. We read the page header
   and walk it ourselves rather than calling into the game:

   ```
   table_base = handle & ~0x1FFF
   slot_index = (handle - table_base - 40) / 8
   table_base + 16 -> bitmap ptr    (slot allocated?)
   table_base + 28 -> slot count
   table_base + 32 -> type flag
   table_base + 8*(slot_index + 5) -> the value
   type_flag <= 1  =>  value is inverted over the LOW 32 BITS only
   ```

   That low-32 detail is a real trap: inverting all 64 bits yields garbage.

---

## 5. The worker tick

`RustGame._tick()`, roughly in order:

1. **Camera / VP** — handled by the sampler thread, not here.
2. **`_get_lc_buffer()`** — `ListComponent<PlayerModel>` singleton → buffer +
   count. Cached with a TTL.
3. **`_read_player_model_list()`** — the `PlayerModel*` array.
4. **`_resolve_bone_slots_batch()`** — advance rig-resolution state machines.
5. **`_read_player_frame_batch()`** *(model.py)* — **one** `batch_u64` for
   the entire tick: local-player flag, `PlayerModel.position`, BasePlayer
   link, health/flags, in-flight rig-job reads, bone hierarchy pointers,
   **and every TRS slot the skeletons need**.
6. **`_compose_bones()`** — pure maths on the words that already came back.
7. **Path A/B/C position selection**, health filtering, player list build.
8. **Throttled scans** — held items (0.5 s), names (1 s), world
   entities (2 s). At most *one* of the three runs per tick; see below.
9. **`_take_snapshot()`** — publish for the render thread.

### One transaction per tick

The skeleton read needs `local_ptr` (`hierarchy + 0x18`), which the frame
batch reads — so for a long time it had to be a *second* IOCTL, issued after
the first came back. On a driver whose answer is ~15.5 ms away regardless of
how much was asked for, that doubled the tick's cost for no extra data.

But that pointer is topology, not pose: a rig keeps the same TRS array for as
long as the hierarchy lives, exactly like the parent-index array next to it.
So `_plan_bone_reads` runs *before* the batch against
`_hierarchy_ptr_cache`, its addresses are appended to the frame batch, and
`_compose_bones` slices the results back out. One round trip, same data.

Planning a tick ahead is only safe because the same batch still re-reads the
pointers it planned against. If one moved in between, the TRS words describe
a rig that no longer exists — so that player's sample is dropped, not
composed, and no outcome is recorded against it (an empty sample would count
towards the three-strike rig invalidation, and a rig that merely moved is not
a broken rig). `[BONE-DBG] stale=` counts those; it should sit at 0.

Two ticks are needed to reach the steady state — the first learns a new
hierarchy's pointers, the second reads its parent indices — after which a
tick is one call. `[POS-PROFILE] bones=` is now pure composition time
(~2.5 ms at 39 skeletons) against `0.00io`.

### Rig resolution is a state machine, not a call

Bone rigs resolve *progressively*, one stage per tick, so no single tick pays
the whole chain: `model → array → elements → native → access`. Reads for the
current stage are appended into the same frame batch (`_append_rig_job_reads`)
and consumed next tick (`_consume_rig_job`). `[BONE-DBG]` prints the histogram
of which stage jobs are stuck in.

### The pose is blended per skeleton, never per bone

`get_snapshot()` interpolates each bone's offset between the two latest
snapshots, gated by `BONE_POSE_BLEND_MAX_SQ` — a bone whose offset jumped
metres is a rig swap, not a movement, and blending towards it would smear.

That gate used to be applied per bone, and `[BONE-PHASE-MIX]` measured what
that costs: 1-2 % of skeleton-frames came out *mixed*, worst case one bone
interpolated against twenty held raw. A blended bone sits at
`prev + (cur-prev)·alpha` and a raw one at `cur`, so a mixed skeleton is
stitched from two different instants — and the bone that disagreed is the one
seen flying out of a sprinting player's body.

So the decision is now made once for the whole skeleton: if any bone fails
the gate (or is missing from the previous pose), every bone goes raw.
Consistency beats smoothness — a whole skeleton arriving one snapshot early
is still a real pose; a detached limb never is. `[BONE-PHASE]` reports how
often that fallback fires and how narrowly: `narrowest=1/21 bones` would mean
one noisy bone is costing twenty good ones their interpolation, which is the
argument for widening `BONE_POSE_BLEND_MAX_SQ`; `21/21` means the pose
genuinely changed.

### Skeleton thickness comes from screen height, not distance

`_skeleton_thickness(span_px)` derives the line width, head radius and halo
from the skeleton's own projected vertical extent. Screen height already
carries distance, field of view and resolution together, so one ratio is
correct at every range: the head is a constant ~12.5 % of the player's
height, which is what a head is.

The previous scheme scaled on a distance curve with a fixed floor, and the
floor is what broke it. At 65-114 m a player projects to 12-21 px tall while
the head circle stayed at its 3.3 px radius — a 6.6 px blob on a 21 px body,
31 % of the player's height and rising past 50 % further out. A field of
distant players read as a row of heads. The dark halo made it worse by being
its own constant (3.0 px against a 1.25 px line), so at range the outline was
the only thing left with any width; it is now *added* to the line instead.

Width was only half of it. A recording made **with** that fix still showed a
solid magenta silhouette at 60-85 m, because the problem out there is not how
thick the lines are but how many there are: twenty links inside a ~10 px-wide
body touch each other at any width. So below `SKELETON_DETAIL_MIN_SPAN_PX`
(40 px, about 45 m at 1080p) `_draw_player_skeleton` draws
`SKELETON_SPINE_LINKS` only — hips to head, one stroke, plus the head marker.
The switch is deliberately hard rather than faded, so it reads as information
about range instead of as a glitch.

### Snapshots and prediction

`_take_snapshot()` publishes `(timestamp, players, vp, world_entities)` under a
lock, keeping the previous one. `get_snapshot()` extrapolates player motion from
it, so the overlay stays smooth at monitor refresh even though the worker runs
slower. Bones are re-anchored by the position delta rather than re-read.

Velocity comes from the engine when possible — `_track_velocity` probes the two
candidate `PlayerModel` slots against real motion and latches the winner
(`[VEL-PROBE]`, OFFSET_RECOVERY.md trap #19). Differencing the two snapshots is
the fallback, and it is measurably worse: a backward difference is the *average*
over the last interval, so it is half a tick stale before smoothing adds more,
which is what made boxes and skeletons trail a moving player.

**The player is interpolated, never extrapolated.** Position, box and every
bone are one lerp between the two newest snapshots, on one alpha. Nothing
drawn is ever projected past the newest sample, so nothing drawn can point
somewhere the player did not go.

That is a reversal. The body used to be extrapolated forward on its velocity
while only the pose was interpolated, and `[PRED-ERR]` / `[RIG-LAG]` were
added to find out which half of that was wrong. Measured in game on
2026-08-27, over ~40 s at 5-8 m/s:

| | measured |
|---|---|
| `[PRED-ERR]` overshoot | **+0.11 … +0.23 m** every window (+17 … +30 ms of travel) |
| `[PRED-ERR]` perpendicular | 0.001 … 0.013 m — the error is *purely* along the direction of travel |
| `[PRED-ERR]` worst sample | **+1.57 m / −1.83 m** |
| `[RIG-LAG]` moving | −0.02 … +0.07 m against an at-rest baseline of 0.020 m — **zero** |

`[RIG-LAG]` being zero is the finding. The bone transforms *are* the rendered
rig, so the game draws the player exactly where the transform we anchor to
says; there was no render latency to hide. Every one of those +0.15 m was
invented travel, and the metre-plus spikes are what a direction change costs
— they are the bones the user reported leaving the body.

So the extrapolation was deleted rather than tuned, and with it the
`extrapolate_skeleton` toggle, which had only ever hidden the shift on the
*bones* by subtracting it back inside the view — leaving the skeleton
anchored to one instant and the box and position to another.

The price is a **constant** lag of one snapshot interval — ~45 ms, ~0.25 m at
sprint — and the lever on that is tick time, not prediction. A constant lag
in a known direction beats a smaller average error that occasionally points
the wrong way.

Two consequences worth knowing:

* `[POSE-FREEZE]` no longer reports a defect. A saturated alpha now means
  "sit exactly on the newest sample", which is the *least*-lagged state
  there is, not a frozen one.
* `prediction_ms` is negative and equal to the snapshot interval; `[RENDER]
  pred=` is the render lag in metres, not a prediction distance.
* `[PRED-ERR]` is kept, and now scores a counterfactual: what the old
  extrapolation would have done this frame. It is the reason the code looks
  like this, and it is what would catch someone quietly putting the lead
  back. `[RIG-LAG]` stays as a live invariant — if it ever drifts away from
  its at-rest baseline, the anchor and the render have come apart.

Positions are gated before they are believed at all: `_gate_position_jump`
holds a sample that implies impossible speed until repeated samples agree
(`[POS-JUMP]`, trap #18). That is what stops a player entering the train tunnels
from snapping across the map.

### Boxes follow the skeleton

`PlayerModel.position` is the *networked* position; the bones are the pose the
client is actually rendering, and during movement the two are metres apart. So
`calculate_bone_box()` builds the box from the projected bones whenever enough
of them are on screen, putting box, skeleton and tracer on one source that
cannot drift apart — and giving a box that follows crouching and prone for free.
`calculate_player_box()`/`calculate_sleeping_box()` remain the fallback for
sleepers and for players with no resolved rig. Toggle: "Box follows skeleton".

The same divergence is why the bone *anchor* gate is not a constant. Bones are
rejected when they land too far from the networked position, and the tolerance
that is correct for a standing player rejects a sprinting one — which used to
trip the three-strike rig invalidation and start a re-resolve storm that the
user felt as a micro-freeze. `_bone_anchor_radius()` scales the gate with the
last tick's duration, and `_record_bone_sample_outcome()` refuses to count an
anchor rejection as a rig failure (trap #21).

### The skeleton is behind the body by two numbers, not one

Reported visually three sessions running, against an overlay whose
`[PRED-ERR]` read +/-0.005 m the whole time — because that scores the
*interpolation*, not the age of what is being interpolated. The drawn body
sits behind live by two terms, and they are different in kind:

| term | what it is |
|---|---|
| `sample` | the newest snapshot is already stale when it arrives — driver poll, plus the tick that decoded it |
| `buffer` | interpolation deliberately renders one snapshot interval further back, so two measured samples always bracket the drawn instant |

`[RENDER-LAG]` prints the total, converted to metres at the player's own
speed. It first printed the two apart — and measured in-game 2026-08-28 that
split turned out to be **degenerate**: alpha is derived from the same
`now - cur_ts` that `sample` is, so the two are complementary and always sum
to one snapshot interval. The line read `sample=70.6 buffer=0.0` on one print
and `sample=3.5 buffer=26.9` on the next: the same total, read at a different
phase of the tick.

That degeneracy is the useful result. **The lag *is* the tick period**, so
every millisecond off the tick is a millisecond off the lag with nothing
traded away — the only lever here that is free.

**The first half was partly an accounting error.** `_take_snapshot` stamped
the snapshot with `time.perf_counter()` at the moment the tick *finished* —
roughly 10 ms of decode and bone composition after the driver handed the
words back. So every snapshot claimed to be 10 ms fresher than it was, and
the interpolation, doing its arithmetic against that stamp, faithfully
rendered 10 ms further into the past to compensate. The snapshot now carries
the batch's completion time instead (`_frame_data_ts`).

Completion rather than the midpoint of the IOCTL, because the driver polls:
the wait is spent *before* the read, not around it. If that is wrong the
error is bounded by one poll and lands on the safe side — alpha saturates
sooner, which holds the pose at the newest measured sample. It can never
push the render past one.

Measured on the in-game tick (31 ms, 10 ms of it post-read) at 9 m/s:

```
end-of-tick stamp    0.369 m behind   (41 ms)
read-instant stamp   0.279 m behind   (31 ms)
```

**The second half is not a bug to be shaved — it is what interpolation is.**
0.279 m is exactly one snapshot interval, and the only way to spend it is
`POS_INTERP_LEAD_FRACTION`, which slides the drawn instant towards the newest
sample. That is *not* extrapolation: alpha stays clamped to `[0, 1]` and the
position stays a lerp between two genuinely measured samples, never past the
newer one — a test sweeps leads up to 3.0 and asserts it.

What it costs is freeze. When the next snapshot is late, alpha saturates and
the pose holds instead of continuing to slide, so the *mean* lag drops while
the *worst case* does not move at all:

| lead | mean | worst | frames frozen (measured in-game) |
|---|---|---|---|
| 0.00 | 0.279 m | 0.279 m | 5-15 % |
| 0.25 | 0.217 m | 0.272 m | **30 %** |
| 0.50 | 0.173 m | 0.272 m | **57 %** |
| 0.75 | 0.146 m | 0.272 m | **85 %** |

The freeze column is why it stays at 0. Measured in-game 2026-08-28 and
stable across every print: the exchange rate is **1 % of frames frozen per
1 % of the interval recovered**, and it has to be — the render thread sweeps
alpha uniformly from 0 to 1 across the tick, so the share of frames past the
clamp is just the lead. There is no sweet spot in that curve to find. Half
the frames frozen to move the skeleton 6 cm is not a trade worth taking.

`[POSE-FREEZE]` keeps printing the price (`| at lead 0.25=..% 0.50=..%`) the
way `[PRED-ERR]` keeps scoring the extrapolation that was removed: so the
answer stays visible rather than remembered.

So the lever is the tick. Measured 2026-08-28 with the honest stamp,
`[RENDER-LAG]` reads **30-32 ms = 0.10-0.18 m at 3-5 m/s** — exactly one
tick period, which is the theory closing. The tick breaks down as:

| | |
|---|---|
| ~16 ms | the driver's IOCTL — out of scope |
| ~10 ms | `yield=`, the GIL released while that call is outstanding |
| ~4 ms | Python: batch decode, bone composition, the rest |

Only the last row is ours to spend, and it is already small. Which is the
honest end of this thread: the remaining lag is the driver's poll, and the
overlay is within a few milliseconds of what that poll allows.

### Every rig slot is placed once, not once per bone below it

Profiled in-game 2026-08-28, after the driver call count was already down to
one per tick, the bone pipeline was the largest single cost left on the worker:
`[POS-PROFILE] bones=12.4ms/0.00io` — twelve milliseconds of pure Python on a
~50 ms tick, with no IO in it at all. `_compose_bones` was second in own-time
across the whole program and `_plan_bone_reads` fifth.

The cause was structural rather than local. `[BONE-DBG]` reported
`slots=660/660` carrying `composed=630` bones: 30 players, 21 bones each,
sharing a 22-node subtree per player. Every bone walked its own ancestor list,
so a rig's hips and spine were scaled-rotated-translated once for every bone
hanging off them. Measured on that exact shape: **3810 walk steps against 646
distinct slots.**

So each slot now carries its own local-to-world transform, built once from its
parent's:

```
T_n = T_p + M_p . t_n            <- the slot's world position
M_n = M_p . (R_n . diag(s_n))    <- what its children compose onto
```

`M` is a 3×3 matrix and not another quaternion because non-uniform scale sits
between two rotations, and `(t, q, s)` is not closed under that composition. A
quaternion-plus-scalar accumulator would be exact on a rig whose scales happen
to be uniform and quietly wrong on one that is not — a limb misplaced on one
player model and nowhere else. `BoneForwardPassTests` carries the case that
separates them.

Two properties are worth stating because both are load-bearing:

- **A slot's world position needs only its parent's transform and its own
  translation.** So a bone sitting *on* an unusable joint still resolves while
  everything hanging *below* it does not — the same asymmetry the chain walk
  had, for the same reason, and a test pins it.
- **Slots arrive sorted.** Unity stores parents before children, so ascending
  index is a topological order and one forward pass suffices. `_plan_bone_reads`
  sorts them, which also hands `_batch_plan` a nearly ordered address list.

The walk lost the same redundancy: it now stops at the first slot already
recorded, because that slot's ancestors are recorded too. `[BONE-DBG]` prints
`walk=` next to `slots=`, and **the two should be equal** — if `walk=` ever
climbs above `slots=`, the early-out has stopped firing.

Measured on the in-game shape in isolation: 3.04 ms → 1.51 ms, and the new
figure includes the struct decode that the old measurement excluded. 3200
random rigs — deep chains, non-uniform scale, unusable quaternions — move no
bone by more than 2 µm, which is float32 re-association noise on metres.

The decode shed its own overhead on the way past: `struct.pack('<%dQ' % words)`
built a format string per slot and three `unpack_from` calls read it back, so
~660 string formats and ~2640 struct calls a tick for 48 bytes each. Two
precompiled `struct.Struct` objects now do it in two calls. And
`_normalize_quaternion` dropped its opening `isfinite()` sweep as redundant,
not as unneeded: a NaN or an infinity makes `norm_sq` NaN or infinite, and both
fail the range test below it — NaN because every comparison against it is
False.

### The game window is not asked where it is 144 times a second

`get_game_viewport` costs five Win32 round-trips — `FindWindowW`, two
`GetSystemMetrics`, a `GetClientRect`, a `ClientToScreen` — and the render loop
called it once per frame, for a rect that changes on alt-tab and resolution
changes and never between two frames. Profiled at 0.83 s of own time, ~0.2 ms
of a 6.9 ms frame at 144 Hz.

`VIEWPORT_CACHE_TTL` is half a second: short enough that a resolution change is
invisible rather than sticky, long enough that the probe runs twice a second
instead of a hundred and forty. Resolving it once at startup would be cheaper
and wrong.

### The camera is extrapolated; players are not

That asymmetry is deliberate, and both halves are measured.

A remote player is a networked position that can reverse direction between
two samples with no warning. `[PRED-ERR]` caught exactly that: +1.5 m single
samples on a direction change, which on screen is a skeleton leaving its
body. So players are interpolated and never projected past the newest
sample (see *Snapshots and prediction*).

The camera is the local player's head. Its rotation is continuous, driven by
a mouse whose angular velocity cannot step, and its error does not
accumulate because every sample resets it. And the cost of *not* projecting
it is not subtle: `[CAM-HZ]` measures 22-34 Hz against a 144 Hz overlay, so
`[RENDER] vpage=` runs 1-44 ms, and a stale view matrix slides **every**
projected object on screen at once while the camera turns.

`CameraSampler.get_predicted(now)` advances the 16 matrix entries linearly
from the previous sample to the frame being drawn. For the small rotations
between two samples that is a good approximation, and the perspective divide
absorbs much of what it is not. Bounded three ways, because an unbounded
version snaps back:

| bound | stops |
|---|---|
| `CAM_PREDICT_MAX_LEAD` (40 ms) | a frame drawn long after the last sample from running away |
| `CAM_PREDICT_MAX_ALPHA` (1.5) | a slow sample rate turning into a wild extrapolation |
| `CAM_PREDICT_MAX_STEP` (5 m) | a teleport — respawn, vehicle, death cam — being projected |

Every rejection returns the raw sample, never nothing, and `sampled_at` is
passed through unchanged so `vpage=` keeps reporting the *real* staleness
rather than the compensated one. `[CAM-PRED]` says how often the projection
was used, the average and worst lead it covered, and why it was skipped.

This is the one thing the UnknownCheats thread on smooth external ESP
converges on. Its answer — "read the view matrix in the render loop" — is
unavailable at 15 ms an IOCTL; carrying the matrix forward is the same idea
paid for in CPU instead of driver round-trips.

### VSync is off, and that is a trade, not a win

`glfw.swap_interval(0)`. The overlay presents as soon as a frame is queued
instead of holding it for up to a full refresh period, so nothing drawn is
ever older than it has to be — which is the same argument as `[CAM-PRED]`,
applied to the other end of the pipeline.

It is not free, and the cost is not on the GPU. `swap_buffers` blocking on the
vblank was **the only thing pacing the render loop**, and it was also where
that thread spent its idle time with the GIL released — 2.83 s of a profile
where the worker needed every millisecond it could get. Uncapped, the loop
stops idling and starts competing.

So it is a `Settings` field with a menu checkbox, not a constant, and the
verdict is measurable rather than arguable. Read three numbers together:

| tag | reading |
|---|---|
| `[SNAP-PROFILE]` | frames/s — what the change bought |
| `[TICK-LATENCY]` | worker tick — what it cost |
| `[CAM-HZ]` | camera sample rate — the other thread the render loop can starve |

If the tick pays more than the frame time gains, turn it back on in the menu.

`poll()` is the only place that applies it: `swap_interval` needs the current
GL context and only the render thread has one, so a menu checkbox cannot call
it directly. One comparison a frame keeps the setting a plain boolean.

**Do not put a sleep-based limiter back.** With vsync on, one existed and had
to be removed: `time.sleep()` is not vblank-accurate, so the two clocks drifted
against each other and produced the stutter that was reported live. With vsync
off there is only one clock and the loop is meant to be uncapped. If it needs a
ceiling, vsync *is* the ceiling — a test parses both `render()` bodies and the
controller loop for actual `sleep` calls, and does not settle for a substring
search, because the comment explaining the rule contains the word.

### Labels are drawn three times, not five

`_draw_outlined_text` was a four-way outline plus the fill: five passes over
the glyphs per label, and each player carries a name, a distance and a held
item. Profiled 2026-08-27 it was 1.47 s of own time, fifth in the program.

`TEXT_SHADOW_OFFSETS` keeps the two diagonals, so a shadow still meets a
glyph edge whichever way the background is brighter. Dropping to a single
drop shadow saves more but leaves one corner with no contrast at all against
sky, which is where distance labels usually sit.

### PyOpenGL checks every call, so it is turned off

Profiled with py-spy over a ~20 s run, 2026-08-27:

| own time | where |
|---|---|
| **3.62 s** | `glCheckError` (OpenGL/error.py) |
| 2.95 s | `swap_buffers` (glfw) — vsync idle, not a cost (vsync has since been turned off; see above) |
| 1.94 s | `batch_u64` |
| 1.58 s | `_predict_from_snapshots` |
| 1.47 s | `_draw_outlined_text` |

`glCheckError` was the largest own-time entry in the whole program. PyOpenGL
calls `glGetError()` after **every** GL call by default; ImGui's programmable
backend issues tens per frame, and at 144 Hz each check is a synchronous
driver round-trip that stalls the pipeline.

The flags are set in the package `__init__.py`, not in a module — PyOpenGL
reads them while building each wrapper, and both `view_base` and
`legacy_runtime` import `OpenGL.GL` at module level, so setting them in
either one is a coin flip on which imports first. A package `__init__` runs
before any submodule whatever the entry point does.

Third in that table, `batch_u64`, was fixed the same day and for a related
reason: 1.11 ms of its ~2.03 ms per call was
`sorted(enumerate(addrs), key=lambda x: x[1])`, on the worker thread, at 56%
GIL. It did not need to run — a player's position address is
`pm + <fixed offset>` and a bone's is `local_ptr + 0x30 * slot`, so the list
the tick rebuilds is usually identical to last tick's. `_batch_plan` caches
the sorted blob, the inverse permutation and the per-word XOR masks per
thread, guarded by full list equality: **2.04 ms → 0.27 ms** warm, cold no
slower than before. `batch_u64_priority` shares it, so the driver's
obfuscation constant now appears exactly once in the file.

`ESP_GL_DEBUG=1` puts the checks back for a session where a silent GL error
would matter. `STORE_POINTERS` and `ERROR_ON_COPY` are deliberately left at
their defaults: disabling the first is only safe alongside the second, and
getting that pair wrong hands the driver a pointer into a freed temporary.

Verify it took: `gl.glBindTexture.error_checker` is `None` when checking is
off and an `_ErrorChecker` when it is on — the flag alone proves nothing.

### Topology never runs on the tick

The `ListComponent` buffer pointer and the `PlayerModel` pointer list change
when someone joins or leaves, not when someone moves. Both were already cached
at 1 Hz, but the refresh still landed **on a tick**, and each costs a full
round-trip.

Measured in game 2026-08-27: `[REFRESH-HITRATE] chain=20/21 pm_list=20/21`, so
one tick in twenty-one paid both — `chain=43.6ms, pm_list=47.0ms`, taking that
tick to 90-140 ms against 41-48 ms for the other twenty. Since the render lag
is now exactly the tick interval (see *Snapshots and prediction*), that single
tick set the worst case for the whole second.

Both refreshes now run as a fourth entry in the slow lane's round-robin
(`_refresh_topology`), alongside held items, names and world entities. The tick
reads the caches those calls publish and never refreshes them itself, with two
exceptions that both exist so a wedged background thread degrades into a slow
tick rather than a frozen player list:

* `_get_lc_buffer(refresh_ok=False)` answers from cache when the cache is
  merely *stale*, but still falls through to the full klass → static-fields →
  wrapper walk when it is *invalid* — that is the startup path.
* the pointer list falls back to an inline read once
  `_pm_ptr_cache_updated_at` is older than `TOPOLOGY_SAFETY_TTL`.

What to look for in a log: `chain=` and `pm_list=` should be 0.0 ms on every
tick, `[REFRESH-HITRATE]` should read 21/21 on both, and the 90-140 ms ticks
should be gone.

### One heavy scan per tick

The three throttled scans have independent intervals, so they periodically
align and fire together — 47-63 IOCTLs on a single tick, which showed up as
`disp=713ms` spikes. `_loop` hands out one `_heavy_scan_budget` per tick and
each scan claims it with `_take_heavy_slot()` *before* pushing its own
`_next_*_scan_at` forward, so a deferred scan retries on the very next tick
rather than waiting out another interval (trap #22).

### The clock underneath all of this

Every IOCTL used to cost a flat ~15 ms, whatever the batch size. Three
measurements taken on 2026-08-27 say where that came from, and it was never
the driver:

| Measured | Value | How |
|---|---|---|
| driver round trip | **30 µs** mean, 34 µs p99 | `bench_event_latency.py`, CMD_PING, PASS |
| one ctypes shared-field read | **0.030 µs** | so the old `for _ in range(1500)` spin covered **45 µs** of waiting |
| one `time.sleep(0)`, 2 CPU-bound Python threads | p90 **29.8 ms**, p99 63 ms | at CPython's default 5 ms switch interval |
| the same `time.sleep(0)` | p90 **0.015 ms** | at a 0.5 ms switch interval |

Those numbers had an obvious reading and it was wrong, so the sequence is
worth keeping. `Mem._wait`'s middle phase was `time.sleep(0)`, which
releases the GIL; getting it back is bounded by `sys.getswitchinterval()`,
5 ms by default, and this process always has two other threads burning CPU.
That made a very good story: an IOCTL finishing in 30 µs, *observed* 15 ms
later because the thread waiting for it could not run to notice.

**It was not what was happening.** The rewrite went in with a fixed 1.5 ms
spin, plus a `slow=` counter for exactly this question — how many waits fell
out of the spin. In-game, `slow=` came back at **100%**: 2 of 2 IOCTLs every
tick, 32 of 34 camera samples every window, with `io` still pinned at
15.4 ms. If the client had been losing a fast answer, a 1.5 ms spin would
have caught nearly all of them. It caught none. **The answer genuinely is
not there for ~15 ms**, which is the driver's polling cadence on the other
side of the shared memory — this build has the event-based wake path
removed. No client-side wait can shorten it. (When the event *was* in use,
`bench_event_latency.py` measured 30 µs mean / 34 µs p99. The reason
enabling it "changed nothing" at the time is that `time.sleep(0)` was still
throwing the 30 µs answer away.)

What survived from the rewrite, and why:

* **No `time.sleep(0)` anywhere in the wait path.** Its cost is unbounded
  by the switch interval, and it can only ever hurt.
* **A self-tuning spin budget** (`_WAIT_SPIN_MIN`/`_MAX`/`_PROBE_LIMIT`).
  Against a polling driver it decays to ~50 µs, so three threads stop
  burning a core proving the answer is 15 ms away; against an event-based
  driver it climbs back on its own and catches the 30 µs answer. Neither
  case needs a constant edited. `[TICK-LATENCY]` prints it as `spin=`.
* **`sys.setswitchinterval(GIL_SWITCH_INTERVAL)`**, set alongside the
  Windows timer tick in `raise_timer_resolution()` before the first IOCTL.
  `[TIMER]` prints both before/after (trap #20). This did show up:
  `[CAM-HZ] lock=` went from a jittery 16-30 ms to a flat 13.5 ms.

**The real constraint this leaves.** At ~15.5 ms per IOCTL, serialized on
one lock, the whole program has a budget of **~64 IOCTLs per second**.
Measured demand: camera ~33/s, worker tick ~16 ticks/s × 2-3, slow lane
~10-25/s — call it 85-90/s. The bus is oversubscribed by a third, and that
is what `yield=22-25 ms` on every tick actually is. So on this driver build
every design decision here is about **IOCTL count and nothing else**: not
Python (bone composition is 2.5 ms/tick, `[SNAP-PROFILE]` is 4-5% of the
render thread), not bytes moved, not lock policy.

Read `slow=` first in any latency line, before the mean. The cost is
bimodal, so a mean can never tell one missed wakeup from ten mediocre round
trips — and `slow=` says which world you are in: near zero means the spin is
catching completions and the transport is healthy; near 100% means the
answer is not there yet and nothing on this side of the shared memory will
change that.

### What the tick is allowed to wait for

Only the *pose* has to be fresh. Slow-changing per-entity relationships --
which `BasePlayer` a `PlayerModel` belongs to, what class an entity is -- are
stable for as long as the entity lives, so the tick must never block on
resolving one. `_refresh_pm_to_bp_cache` fires the `BaseNetworkable` walk onto
a background thread and collects it on a later tick; it used to run inline and
cost every player half a second whenever a single new player appeared (trap
#27). Its cost stays in the log as `bg=Nx/Xms`.

That thread and the worker tick share the `BaseNetworkable` caches, so those
are guarded by `_entity_lock`. `_scan_world_entities` acquires it
non-blocking and skips its round if the resolver holds it -- waiting would put
the slow walk straight back onto the tick.

### Caching philosophy

Cache by *what changes*, not by "it was slow":

| Data | Lifetime | Why |
|---|---|---|
| klass → class name | forever | a class never renames |
| entity → klass | 60 s TTL | GC can reuse an address for a new object |
| rig slots (hierarchy, index) | until the rig changes | topology, not pose |
| parent-index arrays | 3 s | topology, not pose |
| hierarchy TRS/parent pointers | 3 s | topology — and it is what lets the skeleton read join the frame batch |
| TRS matrices | **never** | that *is* the animation |

---

## 6. Identifying entities

Match on the **class name string**, not on a klass pointer from a hardcoded
RVA:

```
entity + 0x00 -> klass
klass  + 0x10 -> const char*   -> "OreResourceEntity"
```

`_WORLD_CLASS_RULES` substring-matches those names (mirroring the reference's
`isOreClass` / `isDroppedItemClass`). Order matters — `DroppedItemContainer`
also contains `droppeditem`, so specific rules come first.

The same name-reading powers `_try_accept_entity_buffer`, which *proves* we
found the right entity buffer by requiring ≥2 readable class names among the
first few elements. That validation is what lets the chain survive patches.

---

## 7. Positions

**Players** — `PlayerModel + 0x2F8`. Nothing else.

**Bones** — resolve `(hierarchy, slot index)` per bone, then compose up the
parent chain, **scale → rotate → translate** per ancestor:

```
result = trs[index].t
i      = parentIndices[index]
while i >= 0:
    result = parent.t + parent.q * (parent.s * result)
    i      = parentIndices[i]
```

`_sample_bones_multi` computes the exact set of slots needed across *all*
players (bones + their distinct ancestors, deduplicated — shared spine joints
collapse into one entry) and fetches them in a single batch.

**World entities** — `entity + 0x220 → transform + 0x78`, falling back to
`entity + 0x1E0 → +0x18 → +0x14`.

> Never read a raw TRS slot and call it a world position. That value is local
> to the parent and aliases unrelated players onto identical coordinates.

---

## 8. Debug tags

All unconditional (they do **not** go through `DEBUG_PLAYERS`, which defaults
off and would swallow them):

| Tag | Says |
|---|---|
| `[TIMER]` | once at startup: timer resolution, and what a short sleep really costs before/after. If the cost does not drop, every timing below is quantised to 15.6 ms |
| `[TICK-LATENCY]` | per-phase timing + IOCTL count/time share |
| `[BONE-DBG]` | every gate the skeleton pipeline must pass, once/sec. `anchor_only=` counts full skeletons the anchor gate threw out — those are *not* rig failures; `worst=Xm (limit Ym)` shows the gate's current width |
| `[POS-JUMP]` | a position implying impossible speed was held at the last believable one |
| `[VEL-PROBE]` | once: which `PlayerModel` velocity slot won, or that extrapolation fell back to differencing |
| `[WE-DBG]` | entity count, distinct klasses, how many are drawable |
| `[PM2BP-DBG]` | fires when the playerModel offset auto-heals |
| `[RENDER]` | drawn / skeletons / behind-camera / players / world entities, and `boxbone=` — how many boxes came from the skeleton rather than the networked position |
| `[BONE-PHASE]` | how often a whole skeleton fell back to a raw (unblended) pose, and how few dissenting bones forced it |
| `[SNAP-PROFILE]` | what one render frame pays for extrapolation, and what share of the render thread that is |
| `[HEALTH-DBG]` | `read`/`fresh`/`cached`/`damaged` — whether the health number on screen is a live read or the last one that passed the filter |
| `[BONE-STRETCH]` | a limb longer than a limb, measured on the pose that is actually **drawn**, and whether that frame was blended or raw |
| `[CAM-HZ]` | achieved camera sample rate, worst gap, and the io/lock split per sample — `slow=` first |
| `[PRED-ERR]` | how far last tick's prediction missed this tick's sample, along the direction of travel. Positive = the skeleton runs ahead of the body |
| `[RIG-LAG]` | how far the *drawn* rig sits behind the position we anchor it to, plus the at-rest baseline that has to be subtracted from it |
| `[RENDER-LAG]` | how far behind live the drawn body is, averaged, in ms and in metres at its own speed — equal to one tick period by construction |
| `[POSE-FREEZE]` `at lead` | what each `POS_INTERP_LEAD_FRACTION` candidate would have cost in frozen frames on this run |
| `[BONE-DBG]` `walk=` | parent-walk steps against `slots=`; equal means each slot is visited once, higher means the early-out stopped firing |
| `[CAM-PRED]` | how often the view matrix was carried forward to the drawn frame, the average and worst lead that covered, and why it was skipped (`teleport`, `gap`, `no-prev`, `fresh`) |
| `[HEALTH-RAW]` | the raw floats around `OFF.lifestate`, printed **the tick any of them changes** (plus a 5 s heartbeat) — the local player when its bp is known (`local`), otherwise the nearest (`near`). Take any damage and `CHANGED` names the offset that moved |

Decision tree for reading them: [`OFFSET_RECOVERY.md` §3](OFFSET_RECOVERY.md).

---

## 9. Adding a feature

1. Find the offsets in `dump.txt`, add them to `OFF` with a source comment.
2. If the field is a `HiddenValue`, port its decrypt from `dump.txt` and route
   through `resolve_tagged_handle`.
3. Add reads into an **existing** batch if the data is needed every tick;
   otherwise write a throttled scan (`_next_*_at` + interval) — see
   `_read_held_items_batch`.
4. Cache by what changes (§5).
5. Publish through the snapshot, draw in `view.py`.
6. Unit-test any pure math (`test_skeleton_math.py`); everything touching the
   driver can only be verified in-game — instrument the gate, ask for one run.
