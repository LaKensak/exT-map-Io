# Recovering the ESP after a Rust update

Playbook for when the ESP goes blank (`sk=0`, `we=0`, `drawn=0`) after a game
patch. Written from the session that fixed exactly that, so the order below is
the order that actually worked — not the order it was attempted in.

---

## 0. The one rule

**Port the reference's *method*, don't copy its *numbers*.**

`F:\CPP\Nouveau dossier\cl1kexternal_[unknowncheats.me]_` is a working external
cheat and the structural reference for this project. Its key design property:
it never trusts a hardcoded field offset for anything that drifts. Its
`objects_basenetworkable` (`sdk/classes.h`) *scans* a window of candidate slots
and *validates* each one by reading IL2CPP class-name strings out of the first
few entries (`tryAccept`, needs >=2 readable names).

That is why it survives patches. Copy that. Its literal constants target an
older build and several are wrong here (see [Known traps](#4-known-traps)).

### Source-of-truth priority for actual values

| Rank | Source | Use for |
|---|---|---|
| 1 | `tools/rust_esp_mvc/dump.txt` | Everything, **when it's for the current build**. Project-local, includes decrypt routines and chain-walker notes. Stale as of build 24840484 — still reflects an older build. |
| 2 | `tools/rust_esp_mvc/offsets_decrypts_export.h` | Local copy of the Desktop dumper output ("rust dumper made by martin"), refreshed to build 24840484 on 2026-08-21. Rougher than ranks 3/4 (many fields still `0`/unresolved, "test these" candidate pairs), but its three `*_decrypt` routines matched ranks 3/4 byte-for-byte, and it's the only source that resolved `ListComponent_PlayerModel_c` (`0x115953F8`). |
| 3 | `tools/rust_esp_mvc/offsets_decrypts_export_24840484.h` | "NeoRed SDK v6" dumper output for build 24840484 — same category as rank 2, different naming scheme (`base_player`, `klass_rvas`, `decryption::*`). Self-tags every field `[AUTO]`/`[AUTO_PROBED]`/`[PROBED_HIGH]`/`[PENDING_*]` — treat those confidence tags as real signal, not noise. |
| 4 | `tools/rust_esp_mvc/il2cpp_dump_24840484.h` | "Validated on discord" IL2CPP class dump for build 24840484 — same build as rank-3 above, but `TypeDefinitionIndex`-anchored with plain field names and no uncertainty tags. Where dumps disagreed, this one usually won — see [Known traps](#4-known-traps) #8. All three cross-checked into `OFF` in `legacy_runtime.py` on 2026-08-20/21. |
| 5 | `cl1kexternal` `sdk/offsets.h` | **Structure only.** Different build — wrong values. |

---

## 1. What drifts and what doesn't

**Drifts nearly every patch** (game code — always re-dump):
`BasePlayer.*` (`playerModel`, `playerFlags`, `clActiveItem`, …),
`BaseEntity.model`, `PlayerModel.position`, klass RVAs, and **all HiddenValue
decrypt routines**.

**Stable across patches** (Unity engine layout — suspect last):
`Transform.m_CachedPtr 0x10`, `TransformAccess 0x28` (+`8` = index),
`TransformHierarchy` `0x18` localTRS / `0x20` parentIndices, TRS stride `0x30`,
Il2CppArray header (`max_length 0x18`, data `0x20`), `klass.static_fields 0xB8`,
`klass.name 0x10`.

---

## 2. The chains

### BaseNetworkable entity chain — `_resolve_entity_chain()`

Feeds **both** world items *and* player discovery. If this breaks, everything
breaks at once. This was the root cause in the session that produced this doc.

```
ga + BaseNetworkable_c (0x1164cc90)   -> bn_klass
bn_klass + 0xB8                        -> static fields
sf + 0x8                               -> clientEntities HiddenValue wrapper
wrapper + 0x18                         -> encrypted handle
  decrypt_bn0  = ROL(27) | XOR(0xD12F9B81) | ROL(5)   [dump.txt: client_entities]
  resolve_tagged_handle                -> EntityRealm
EntityRealm + 0x10                     -> parent wrapper
parent + 0x18                          -> encrypted handle
  decrypt_bn1  = ROL(15) | XOR(0xE2181B93) | ROL(8)   [dump.txt: entity_list]
  resolve_tagged_handle                -> ListDictionary
SCAN ListDictionary[0x08..0x40 step 8] -> BufferList      <-- validated, not guessed
BufferList + 0x10                      -> Il2CppArray
BufferList + 0x18                      -> live count
Array + 0x20                           -> element 0
```

The scan is `_probe_entity_container()`; validation is
`_try_accept_entity_buffer()` (the `tryAccept` port). On this build the
BufferList sits at `+0x20` — matching `dump.txt`'s `ENTITIES = 0x20`. **Do not
re-hardcode it**; the scan is what makes this survive the next patch.

### Bone chain — `_resolve_bone_slots_batch` + `_sample_bones_multi`

```
bp + 0x1B8 (OFF.model)        -> Model
Model + 0x50                  -> boneTransforms (Il2CppArray)
array + 0x20 + boneId*8       -> Transform
Transform + 0x10              -> native (m_CachedPtr)
native + 0x28                 -> TransformHierarchy
native + 0x28 + 8             -> slot index (u32)
hierarchy + 0x18              -> local TRS array (stride 0x30)
hierarchy + 0x20              -> parent indices (int32)
```

World position = walk parent chain composing **scale -> rotate -> translate**:

```
result = trs[index].t
i      = parentIndices[index]
while i >= 0:
    result = parent.t + parent.q * (parent.s * result)
    i      = parentIndices[i]
```

### Player position

`PlayerModel + 0x2F8`, nothing else. Same as the reference's
`BasePlayer::GetPosition`. Do **not** reintroduce a root-bone transform-slot
read — that value is a *local* translation and aliases players onto identical
coordinates. `0x310` and `0x31C` are the neighbouring velocity vectors (trap
#19) and are never positions. Every sample passes `_gate_position_jump` before
it is believed (trap #18).

### PlayerModel -> BasePlayer

`bp + 0x348`, with an automatic brute-force fallback
(`_map_baseplayers_to_pm`, scans `0x18..0x900`) that prints
`[PM2BP-DBG] BasePlayer.playerModel offset changed to 0x...` when it finds a
new one. **After a patch, check the console for that line first** — it may have
already self-healed and told you the new offset.

---

## 3. Diagnostic decision tree

Run in-game and read one second of console. Each gate is counted separately so
the failing stage is identified directly instead of guessed.

| Symptom | Meaning | Action |
|---|---|---|
| `[WE-DBG] entity chain resolve FAILED` | Chain dead at HiddenValue/decrypt stage | Re-dump `decrypt_bn0`/`decrypt_bn1` and `BaseNetworkable_c` from `dump.txt` |
| `[WE-DBG] N entities, M klasses, 0 drawable` | Chain fine, name matching off | Check `_WORLD_CLASS_RULES` substrings against real names |
| `[BONE-DBG] rig=0 job=0` | `pm_to_bp` empty — no BasePlayer found | Look for `[PM2BP-DBG]`; if silent, the entity chain is feeding garbage |
| `[BONE-DBG] job>0 rig=0` | Rig resolve stalling | Stage histogram in the line names the failing hop |
| `[BONE-DBG] rig>0 cand=0` | `local_pos` missing or distance gate | Check `origin=y/n` in the same line |
| `[BONE-DBG] queued>0 out=0` | Composition or anchor rejecting | Read `rej chain/quat/box/anchor` + `worst=Xm` |
| Boxes render but `sk=0` | Position path (0x2F8) fine, rig path broken | Bone chain only |

---

## 4. Known traps

Each of these was tried and measured. Don't repeat them.

1. **Entity array payload is `0x20`, not `0x28`.** `cl1kexternal` names a
   `Array_DataBase = 0x28` for its BufferList variant. Using it here nearly
   quartered the valid-pointer yield: **2515/3472 -> 161/3625**. The PlayerModel
   list carries a matching "LOCKED to 0x20" note for the same reason. Only
   change it with before/after yield numbers proving an improvement.

2. **Do not gate HiddenValue reads on `_hasValue` (0x14).** A forum note says to
   check it first; doing so killed the chain outright
   (`entity chain resolve FAILED`). The reference reads the handle at `0x18`
   unconditionally and never looks at `0x14`.

3. **Bone IDs use the NEW SCI list** (`head = 47`, `r_hip = 8`, `r_hand = 59`).
   `cl1kexternal`'s `kIds` is the OLD list (`head = 53`, `r_hip = 14`,
   `r_hand = 65`). Copying its bone table breaks working IDs.

4. **Compose order is scale -> rotate -> translate.** The reference computes
   `tmp7 = _mm_mul_ps(v3, result)` — scale *before* the quaternion rotate,
   translation added last. Rotate-then-scale only agrees under uniform scale, so
   the bug hides on most joints. Verified numerically against a literal port of
   the SSE kernel: max error 2e-14 over 2000 random trials.

5. **`cl1kexternal`'s `BaseEntity::Model = 0x1A8` is wrong here.** This build is
   `0x1B8`, confirmed by both same-build dumps.

6. **The NeoRed SDK v6 dump (build 24840484) re-proposes the trap-1 array
   payload.** Its `base_networkable.il2cpp_array_data_base` is `0x28`, tagged
   `[PROBED_HIGH]` (auto-probe, not a validated read) — the exact value trap 1
   already measured as a regression. Left `array_payload` at `0x20` in `OFF`.
   Only change it with a fresh before/after yield count.

7. **The NeoRed SDK v6 dump also re-proposes the trap-3 OLD bone list.** Its
   `model.*_bone_idx` table (`head_bone_idx = 0x35` = 53, `r_hip_bone_idx =
   0xE` = 14, `r_hand_bone_idx = 0x41` = 65, tagged `[AUTO_PROBED]`) matches
   `cl1kexternal`'s OLD list byte-for-byte, not this project's working NEW SCI
   list (`head = 47`, `r_hip = 8`, `r_hand = 59`, still in `SCI_BONE_IDS` in
   `legacy_runtime.py`). Left `SCI_BONE_IDS` untouched. If the working bone
   list ever stops matching after a patch, re-derive it in-game (existing
   working boxes going wrong first) rather than trusting this table.

8. **Two dumps for the *same* build can still disagree — check the confidence
   tags, don't average them.** The NeoRed v6 dump's `local_player_canonical`
   (klass `0x1161ABD8`, `Entity` at `0x10`) is self-tagged `[AUTO_PROBED]` /
   "CANONICAL" (heuristic wrapper detection). The validated-discord dump's
   plain `LocalPlayer_Static_Offsets` (klass `0x11587700`, `Entity` at `0x8`)
   has no uncertainty tag and is `TypeDefinitionIndex`-anchored. Went with the
   latter in `OFF.LocalPlayer_c` / `OFF.LocalPlayer_Entity`. Same pattern hit
   `item.held_entity` (NeoRed `0xE0`, self-tagged "highest of N>=2" heuristic,
   vs. the validated dump's plain `Item_Offsets.heldEntity = 0x88`, which also
   matches a long-standing fallback candidate already in this project's own
   old `offsets_decrypts_export.h`) — went with `0x88`.

9. **`cam_projMatrix`/`cam_viewMatrix` are joined at the hip — check the code,
   not just the dump.** `read_view_proj_matrix()` doesn't read these as two
   independent offsets: it reads one contiguous `0x1B0`-byte block starting at
   `cam_projMatrix` and pulls the view matrix out of that same block at a
   **hardcoded `+0x170`**. That only works if `cam_viewMatrix - cam_projMatrix
   == 0x170` — true for `0x18C`/`0x2FC`, broken for NeoRed v6's proposed
   `0x748` (negative gap). Applying it collapsed every on-screen player
   position onto a near-vertical line (reported live with screenshots,
   2026-08-21) instead of erroring loudly, because the corrupted matrices
   still produced *some* number, just a degenerate one. These are native
   Unity engine struct offsets (not IL2CPP game code) — per §1 they shouldn't
   drift per patch, so treat any per-build dump proposing a new value here
   with real suspicion, and grep the offset's actual read site before trusting
   it. Reverted to `0x18C`/`0x2FC`.

10. **The `player_model` (`PM+0x98`) bone-array fallback can outrank the
    working `model.boneTransforms` source and scramble the skeleton.**
    `_resolve_bone_slots_batch()` in `legacy_runtime.py` picked whichever of
    the two candidate arrays resolved *more* `_valid_user_ptr` bone slots,
    with no check that they point at the right array — pointer validity only
    means "readable memory", not "the right Transform". `OFF.pm_rootBone
    (0x98)` is an unverified "NeoRed pm_rootBone" guess (see the `[DIAG-F]`
    dump around line 1203) kept only as a compatible-layout fallback for
    older SCI builds. On 2026-08-21 it apparently resolved more (wrong)
    pointers than the confirmed `model.boneTransforms (Model+0x50)` path for
    at least one in-game player, producing a fully coherent-looking but
    spatially scrambled skeleton (reported live with a screenshot — lines
    fanning out from the torso/hip hubs instead of tracing limbs, on both a
    nearby and a distant player). Boxes/tracers were unaffected because
    those use the separate `PlayerModel + 0x2F8` position path, not the bone
    chain — a purely-garbled-but-still-drawn skeleton with correct boxes
    means suspect bone *source selection*, not the entity/position chain.
    Fixed by making `model` win outright whenever it resolves >= 12 bones
    (the same threshold `_sample_bones_multi` already requires to accept a
    skeleton), only trying `player_model` when `model` fails entirely. Don't
    revert to a "most pointers wins" heuristic between the two sources.

    **Correction:** this diagnosis was on the wrong file. `legacy_runtime.py`'s
    `RustGame._resolve_bone_slots_batch`/`_sample_bones_multi` are dead code —
    `model.py`'s `RustGameModel` (what `controller.py` / `tools/st_try.py`
    actually run) overrides both with its own job-based implementation, and
    that one already only tries `player_model` when `model` fails outright
    (no "most pointers" bug there). The fix above is harmless but didn't touch
    the live path. The real cause was IDs, not source selection — see #11.

11. **Half of `SCI_BONE_IDS` was flat wrong — verify bone IDs against
    `Model.boneNames`, don't trust a "confirmed working" note.** The project
    memory (and this doc, implicitly) treated the "NEW SCI list" — right leg
    `8,10,11`, spine `14,15,16,17`, left arm `18,19,20,23`, neck/head `46,47`,
    right arm `54,55,56,59` — as settled after a 2026-08-21 live check. It was
    never actually right: `OFF.boneNames` (`Model+0x58`, an `Il2CppString*[]`
    parallel to `boneTransforms`, present in `dump.txt` and both 24840484
    dumps but never wired up) reads the *real* per-index name, and a live dump
    of all 94 indices on 2026-08-21 showed `8/10/11` are genital-censor mesh
    bones, `14-17` are actually the *right leg* (`r_hip,r_hip_twist_02,
    r_knee,r_foot`), `18-23` are the right foot/toe plus `spine1/spine4`,
    `46/47` are left-hand fingers (`l_ring3`,`l_thumb1`), and `54/55/56/59`
    are face bones (`eyetransform`,`jaw`,`l_eye`,`r_eyelid`). Only pelvis
    (`0`) and the left leg (`1,3,4`) were ever correct. The user-reported
    symptom ("hand connects to foot, foot connects to head, foot connects to
    pelvis") was several joints each rendering a random unrelated bone, not a
    subtle math bug — every existing sanity gate (`rej_chain/quat/box/anchor`,
    `worst=`) passed because each wrong bone still composed to a *finite,
    nearby* position, just not the anatomically correct one. A **direct**
    per-link check (drawn skeleton edge distance > ~1m is anatomically
    impossible) caught the first symptom (`(17,54)` measuring 1.52m instead
    of ~15cm); reading the names directly gave the fix with zero guessing.
    The confirmed-correct list, live-verified 2026-08-21: pelvis `0`; left
    leg `l_hip=1,l_knee=3,l_foot=4`; right leg `r_hip=14,r_knee=16,r_foot=17`;
    spine `spine1=20,spine2=21,spine3=22,spine4=23`; left arm
    `l_clavicle=24,l_upperarm=25,l_forearm=26,l_hand=29`; neck/head
    `neck=52,head=53`; right arm `r_clavicle=60,r_upperarm=61,r_forearm=62,
    r_hand=65`. These exactly match the *old* pre-SCI list this doc's trap #3
    and #7 called wrong (`head=53,r_hip=14,r_hand=65`) — that rejection was
    itself the error; whatever "confirmed working live" check produced the
    NEW list either tested a different build or wasn't a rigorous per-joint
    check. `SCI_BONE_IDS`/`SCI_BONE_LINKS` (`legacy_runtime.py`) and
    `SKELETON_LINKS` + the head-bone lookup (`view_base.py`, duplicated in
    dead code in `legacy_runtime.py`) are now updated to this list — keep all
    three in sync. If a future patch scrambles the skeleton again, re-run the
    `Model.boneNames` dump (`RustGameModel._debug_dump_bone_names`, currently
    still wired to fire once per process under `DEBUG_PLAYERS=True`) before
    touching any ID by hand.

12. **World-entity (item/ore/crate/…) positions used an unconfirmed primary
    offset that silently beat the confirmed fallback.** `_scan_world_entities`
    read a "primary" world position via `entity + OFF.be_transform (0x220)`
    -> Transform -> `+0x78`, only falling back to the PositionLerp chain
    (`+0xC8` -> `+0x10` -> `+0x10`, fully confirmed — see the validated-discord
    dump's complete `BaseEntity_Offsets` namespace, which lists `bounds`,
    `model`, `flags`, `triggers`, `positionLerp` and **nothing named
    transform**) when the primary read failed the sanity box in
    `_batch_vec3`. `0x220` had no support in any of the three build-24840484
    dumps and was flagged as such directly in its own code comment
    ("unconfirmed... left as-is") — but because it usually produced a
    *finite, in-world-bounds* number rather than erroring, it passed the
    sanity check and never fell back, giving items/ore/crates a
    plausible-but-wrong position. Live symptom (reported 2026-08-21): world
    items detected and classified correctly (`matched=209`) but not showing
    up on screen / showing at the wrong distance. Removed the primary path
    entirely — Stage 3 of `_scan_world_entities` now goes straight to the
    confirmed PositionLerp chain for every matched entity, which also saves
    a batch read on the common case instead of costing an extra one on
    retry. `OFF.be_transform`/`OFF.be_transform_world_pos` are gone from
    `OFF`; only `OFF.be_position_lerp` remains for this chain.

    **Correction (same day):** PositionLerp-only was still wrong — see #13.

13. **PositionLerp is Rust's *network smoothing* state, not a general
    position source — it goes null once an entity settles.** After #12,
    `[WE-POS]` diagnostics live in a real Rust world (not a minigame lobby)
    showed `matched=49` but only `pos_resolved=4`, and those 4 were ~3000
    units from the player (`x≈2400` vs. the player's `x≈-800..-1040`) — not
    just far away, but suspiciously clustered near `y≈0-1.5, z≈0-1.5`, i.e.
    implausible for real dropped-item ground positions. Only 19/49 entities
    even had a valid PositionLerp pointer to begin with. Reading
    cl1kexternal's actual `BaseEntity::GetWorldPosition`
    (`sdk/classes.h:722`) showed why: it tries a direct Transform field
    first (this build doesn't have one, per trap #12), **then falls back to
    `Model.boneTransforms[0]`'s Transform — composing the full parent-chain
    world position exactly like player bones** — and only reaches for
    PositionLerp as the *last* resort. This project had PositionLerp as the
    *only* path, i.e. was using the reference's weakest fallback as primary.
    Rust apparently deallocates/nulls PositionLerp once an entity stops
    needing client-side network interpolation (a settled, static item has
    nothing left to smooth), which is why most items had no valid pointer at
    all, and the few that did gave stale/wrong data.
    Fix: added `RustGame._read_entity_root_positions()`, which walks
    `entity -> Model (OFF.model) -> rootBone (OFF.rootBone_Model) -> native
    -> TransformAccess/idx -> full parent-chain compose` — the same TRS-walk
    math already used for player bones, batched per distinct hierarchy so
    entities sharing one only pay for its buffers once. Made Stage 3's
    secondary path; PositionLerp is the fallback for whatever it can't
    resolve, matching the reference's actual priority order. `OFF.model` and
    `OFF.rootBone_Model` were already confirmed (used for player bones/eyes)
    — no new offsets, just the missing middle tier.

    **Correction (same day):** this still only covered rigged/structure
    entities — see #14 for the field that actually covers everything.

14. **`BaseEntity.bounds` — a plain value field, not a pointer chain — is
    the real universal world position, and it was sitting right next to
    `model` the whole time.** Live-tested #13's `Model.rootBone` tier
    (`root_resolved`) against a real world scan: it resolved **8 of 191**
    matched entities, and all 8 were `TC` (`BuildingPrivlidge`, an actual
    rigged/animated structure). Every `DroppedItem`/`LootContainer`/
    `OreResourceEntity`/`CollectibleEntity` — i.e. every simple static prop —
    has no populated `Model` at all, so neither tier 2 nor tier 3
    (PositionLerp, already known unreliable per #13) ever resolved them.
    Grepping the *full* deobfuscated class dump (`dump.cs`, not the curated
    header — same build, offsets cross-check exactly: `bounds=0x18C,
    model=0x1B8, flags=0x1C0`) for `class BaseEntity` showed:
    `public Bounds bounds; // 0x18C`. `Bounds` is Unity's standard struct
    (`Vector3 center` then `Vector3 extents`), always world-space, always
    computed by the engine's own collision/culling system regardless of
    Model or PositionLerp state — and it needs **zero pointer indirection**,
    just `entity + 0x18C`. Made this Stage 3's *primary* path
    (`OFF.be_bounds_center`); `Model.rootBone` and PositionLerp are now only
    tried for whatever it can't resolve (should be rare — this is a value
    field on the object itself, not a lazily-populated component). If a
    future patch ever breaks this, dump `class BaseEntity` from `dump.cs`
    again before guessing — it's the ground truth this whole trap chain (12,
    13, 14) should have started from.

    **Correction (same day):** `bounds` still needed something to trigger
    its computation, and turned out empty for `DroppedItem` specifically —
    see #15 for the source that works unconditionally.

15. **The entity's own native Unity Transform — reachable from the managed
    entity object itself, zero extra components needed — is the actually-
    universal source, and it was found by pasting a fresh third-party SDK
    dump rather than more guessing.** After 12-14, `DroppedItem` still had
    no usable position: `bounds` read as zero (never triggered/computed for
    a simple prop), `Model.rootBone` resolved to a shared prefab-local
    constant (identical across every instance — a mesh pivot offset, not a
    world position; same story for `LootContainer`, which explained why
    "some crates don't show"), and two rounds of brute-force memory scanning
    (flat + one level of pointer indirection, live-tested with real target
    positions as ground truth) found nothing but more shared constants. The
    fix came from a user-supplied `rust-dumper` SDK header with a
    `unity_object`/`unity_component`/`unity_game_object`/`unity_transform`
    section giving the *native engine* GameObject/Component chain — data
    this project never had before (everything up to this point came from
    IL2CPP *managed*-object dumps, which don't cover native engine
    internals). Its other values cross-checked exactly against offsets this
    project had already independently confirmed live (`0x28` == the already-
    confirmed `transform_access`/`unity_transform_native.access_struct_off`;
    `0x18`/`0x20` == `td_pos_base`/`td_parent_indices`; camera `viewMatrix`/
    `projectionMatrix` == `cam_viewMatrix`/`cam_projMatrix`), which is why it
    was trusted enough to implement directly rather than scan-verify first.
    The chain, added as `RustGame._read_entity_native_transform_positions()`
    and made Stage 3's new primary tier (bounds/rootBone/PositionLerp are now
    only fallbacks):
    ```
    entity (itself a UnityEngine.Object) + OFF.m_CachedPtr (0x10) -> native Component
    native Component + 0x20                                       -> native GameObject
    GameObject + 0x20                                              -> Components[] array
    Components[0] (stride 0x10, ptr at +0x8; Transform is always slot 0) -> native Transform
    Transform + 0x28 (== transform_access)                        -> TransformData
    TransformData + 0x90                                          -> cached WORLD position
    ```
    No parent-chain walk needed — TransformData caches the already-composed
    world position directly, unlike the per-slot local TRS array used for
    bones. This is a genuinely universal source: every GameObject always has
    exactly one Transform, always at component slot 0, regardless of
    Model/bounds/PositionLerp population state, so it should work for every
    BaseEntity subtype, not just items.

16. **`clActiveItem` (held-item lookup) isn't a HiddenValue wrapper — it's the
    encrypted qword itself, unlike every other wrapped field on this
    project.** `_read_held_items_batch` always read `bp + OFF.cl_active_item`
    as a *pointer*, then read `pointer + OFF.hv_slot (0x18)` to get the
    actual encrypted value to decrypt — the standard pattern that's correct
    for every other HiddenValue field here (`player_eyes`,
    `player_inventory`, the entity-chain fields). Live symptom: `wrapper_valid=0`
    for every player, every time — the raw value at `bp+0x580` is an
    *encrypted* qword, which essentially never happens to also look like a
    valid heap pointer, so it failed the `_valid_user_ptr` check 100% of the
    time. A fresh rust-dumper SDK header's own usage note for this exact
    field spelled out the fix directly: "takes the ENCRYPTED VALUE, not a
    pointer — no dereference here. The routine's prologue reads +0x18 off
    its argument, but this call site already holds the qword:
    `clActiveItem(read<uint64>(player + clActiveItem))`" — i.e. skip the
    wrapper hop entirely and decrypt the value read straight from
    `bp + OFF.cl_active_item`. Fixed by merging stages 1+2 into one direct
    read → decrypt → `resolve_tagged_handle` call. The decrypt constants
    themselves (`ROL(28) | ADD(0x4163CC9A) | XOR(0x3C9AE79B)`) were already
    correct — this project's own `decrypt_cl_active_item` already matched the
    SDK's routine exactly (both sourced from NeoRed SDK v6) — so the bug was
    purely structural, not a wrong constant.

    **Correction (same day):** `resolve_tagged_handle(decrypt(...))` still
    resolved to 0 for every player even after the structural fix — see #17
    for why, and the actual working chain.

17. **`clActiveItem` decrypts to an item UID, not a pointer or IL2CPP
    handle — it has to be matched against `Item.uid` in the belt, not
    resolved directly.** After trap #16's fix, live values (2026-08-22)
    decrypted to numbers like `0x502817`, `0x4ff42c` — all clustered in a
    ~5-million range, far too small for a real 64-bit heap address (and too
    small to survive `resolve_tagged_handle`'s handle-table slot-alignment
    check, which is why it kept resolving to 0). The SDK's own field
    annotation for `Item.uid` said as much directly: `type-nav ItemId --
    matches a decrypted clActiveItem` — i.e. `Item.uid` is a *plain* field
    of the same `ItemId` type, and the two are meant to be compared, not
    chained. Fixed by walking the belt instead: `BasePlayer.inventory`
    (`0x308`) is a *real* HiddenValue wrapper (unlike clActiveItem) whose
    decrypt routine (`decrypt_player_inventory`, already implemented and
    already matching the SDK's routine exactly — that one is SDK-tagged
    `[VERIFIED]`, "reproduced the pointer the game's own accessor returns")
    resolves to `PlayerInventory*`; `+containerBelt (0x58)` gives the belt
    `ItemContainer*`; `+itemList (0x50)` gives a `List<Item>`, read via the
    standard IL2CPP `List<T>` layout (`_items` +0x10, `_size` +0x18 — the
    same layout already confirmed independently for
    `BasePlayer_visiblePlayerList` and the SDK's `renderers` list); each
    belt `Item.uid` (`0xD8`) is compared against the decrypted clActiveItem
    value, and the match is the held item. `OFF.inventory`,
    `OFF.container_belt`, `OFF.item_list`, and `decrypt_player_inventory`
    all already existed in this project (unused/dead) with the right values
    — this was assembling already-confirmed pieces, not discovering new
    offsets. Only `OFF.item_uid (0xD8)` was new.

18. **A guard that only compared candidate *offsets* let every teleport
    through.** Symptom: with the local player above ground, anyone who walked
    into the train tunnels / sewers snapped to the far side of the map and
    stayed there until they came back out, at which point it self-corrected.
    `_select_pm_position`'s 120 m / 180 m distance guards look like teleport
    protection but are not: they only ever discriminated *between* several
    candidate position offsets, and there has been exactly one candidate
    (`PlayerModel+0x2F8`) since the transform-slot path was retired — so the
    final `candidates[0]` fallback accepted the jump unconditionally, every
    time. The fix is `_gate_position_jump`: gate on physics instead
    (`POS_JUMP_*` in legacy_runtime.py). A sample that implies >60 m/s over
    >25 m is held at the last believable position until several consecutive
    samples agree on the new spot (a real teleport confirms in ~3 worker ticks,
    ~25 ms) or the 1.5 s ceiling expires. It also resets rather than holds when
    the `bp` behind a `pm` changes, since a recycled slot is a different player,
    not a teleport — the same identity check that fixed stale names. Watch
    `[POS-JUMP]` in the console. **Don't repeat this:** a distance check is only
    a teleport gate if it can reject *all* candidates and still produce output.

19. **`PlayerModel` has four consecutive `Vector3`s and the dumps disagree on
    which is velocity.** dump.cs (line 839117+) lists `0x2F8` (position),
    `0x304`, `0x310`, `0x31C`, then a `Quaternion` at `0x328`; the field names
    are obfuscated in this build. dump.txt and
    `offsets_decrypts_export_24840484.h` both call `0x31C` `newVelocity`, while
    an in-code comment on the position path calls `0x310` velocity. Neither
    claim is settleable from a dump, and picking wrong silently degrades
    extrapolation instead of failing loudly. So both are read and scored at
    runtime against the position-differenced velocity (`_track_velocity`,
    `OFF.velocity_pm_a`/`_b`), only on ticks where the player demonstrably moved
    — a standing player makes every candidate look perfect, including one that
    is not a velocity at all. The winner is printed once as `[VEL-PROBE]` and
    the loser stops being read; if neither is within 2.5 m/s the extrapolator
    falls back to differencing. **Don't repeat this:** when two dumps disagree
    and the wrong answer degrades quietly, probe both live rather than picking.

20. **Every IOCTL cost a flat ~15 ms regardless of how much it read.**
    Symptom: `[TICK-LATENCY]` showed a 2-address batch costing the same as a
    66-address one, clustering hard at 15.0-15.3 ms with occasional halves at
    ~7.5 ms. That is not a driver cost curve, it is a clock: Windows' default
    timer tick is 15.6 ms, and `Mem._wait` falls back to `time.sleep()` once an
    IOCTL outlives its 3 ms yield-spin, so every such wait was rounded up to the
    next tick. `raise_timer_resolution()` (called from `Mem.__init__`) asks
    `NtSetTimerResolution` for 1 ms and `timeBeginPeriod(1)` for the process,
    then *measures* `sleep(0.2ms)` before and after and prints both, so the next
    run says plainly whether it took: `[TIMER] resolution=1.000ms |
    sleep(0.2ms) costs 15.63ms -> 1.00ms`. **Don't repeat this:** a latency that
    is flat across payload sizes and quantised to a suspicious constant is a
    scheduler artefact, not the thing you are measuring. Check the constant
    against the platform's timer tick before optimising the payload.

21. **A fixed bone-anchor radius turned fast movement into a rig-invalidation
    runaway.** Symptom, in the user's words: micro-freezes "quand une personne a
    une vélocité élevée ou qu'il fait trop de mouvement". The anchor is the
    *networked* position; the bones are the pose the client is still
    interpolating towards it. Those legitimately diverge while a player moves,
    and by more the longer the sample is stale. A fixed 4.5 m gate therefore
    starts rejecting perfectly good bones exactly during fast movement -> the
    sample comes back `short` -> three strikes -> `_invalidate_bone_rig` -> a
    full five-stage re-resolve -> a longer tick -> *more* staleness -> a wider
    divergence -> more rejections. The loop closes and the overlay stalls. Two
    changes break it: `_bone_anchor_radius()` widens the gate by what the last
    tick's duration can actually account for (25 m/s, capped at
    `BONE_ANCHOR_SLACK_MAX`), and `_record_bone_sample_outcome()` distinguishes
    `'anchor_only'` (the rig composed a full skeleton, the gate threw it out)
    from `'short'` (the rig itself failed) — only the latter counts as a strike.
    Watch `anchor_only=` and `worst=Xm (limit Ym)` in `[BONE-DBG]`. **Don't
    repeat this:** before a failure counter triggers an expensive repair, check
    that the counter can actually distinguish "the thing is broken" from "the
    thing is fine and the tolerance is too tight" — otherwise the repair is what
    keeps the failure alive.

22. **Three independent scan intervals aliased onto the same tick.** The
    world-entity (2 s), player-name (1 s) and held-item (0.5 s) scans each have
    their own timer, and periodically all three fire on one tick — 47-63 IOCTLs
    at once, which is the `disp=713ms` spike in the log. `_loop` now hands out
    one `_heavy_scan_budget` per tick and each scan claims it with
    `_take_heavy_slot()`. The claim must happen **before** the scan pushes its
    own `_next_*_scan_at` forward, or a deferred scan waits out another full
    interval instead of retrying on the next tick. **Don't repeat this:**
    independent periodic timers in one loop will eventually align; budget the
    shared resource rather than tuning the intervals to be coprime.

23. **The velocity probe measured its own tick rate, not the player's speed.**
    First in-game run of trap #19's probe printed `a(0x310)=4.44m/s
    b(0x31C)=3.68m/s -> none` — both candidates rejected, so the ruler was
    wrong, not the candidates. The networked position updates at ~15 Hz while
    the worker ticks several times faster, so most ticks observe the *same*
    position; differencing across a *tick period* rather than across two
    *distinct* positions inflated the measured speed several-fold and nothing
    could ever match it. `_track_velocity` now only records a reference sample
    when the position actually changed, and skips scoring entirely on unchanged
    ones. **Don't repeat this:** when validating a value against a derived
    measurement, confirm the measurement's own sample interval is the interval
    the source actually updates on.

24. **A constant staleness window turned into "no skeletons at all" once the
    tick got slower than it.** Symptom: `[BONE-DBG] ... out=3` on the same tick
    that `[RENDER]` said `sk=0 boxbone=0` — three skeletons produced and none
    drawn, with the user reporting bones frozen in place and players showing a
    box and nothing else. `_tick` re-anchors a sampled skeleton onto the fresher
    networked position and dropped the sample after a hard-coded `0.25` s. Ticks
    in that log ran 108-772 ms, so for most players the sample expired *before
    the next tick could refresh it*: `bones` came back `None`, which also took
    `calculate_bone_box` out (hence `boxbone=0` and a box that no longer follows
    the pose). The window is now `BONE_REANCHOR_TICKS` × the measured tick time,
    clamped to `[BONE_REANCHOR_TTL_MIN, BONE_REANCHOR_TTL_MAX]`, so a skeleton
    sampled on tick N always survives into tick N+1. **Don't repeat this:** any
    freshness window compared against a *loop's own* period must be expressed in
    that period, never as a constant — a constant is a silent cliff the moment
    the loop slows past it, and it fails by showing nothing rather than by
    erroring.

25. **The worker scanned features the user had switched off.** `[WE-RENDER]`
    read `show_world=False total=203 ... drawn=0`: 203 world entities had their
    full transform chain resolved every 2 s purely to be discarded at draw time,
    inside the same `disp=` phase that was costing 211-467 ms. `show_world_entities`
    (and `show_name`, `show_held_item`) lived only in the view's `Settings`, and
    the model had no way to know. The controller now pushes them into
    `RustGame.wanted` each frame, and each heavy scan returns immediately when
    its feature is off — *before* claiming the tick's heavy slot, so a disabled
    feature does not starve an enabled one. **Don't repeat this:** when the view
    owns the toggles and the model owns the reads, "off" means "not drawn", not
    "not read". Check what the worker does when every checkbox is unticked.

26. **Wall-clock per phase cannot tell one slow read from six fast ones.** At a
    ~15 ms fixed driver round-trip, the *number* of calls is what sets the tick
    time, and `bones=91.4ms` alone does not say whether that was one big batch
    or six small ones — the fix is completely different in each case.
    `[POS-DBG]` now prints `map=Xms/Nio, batch=Xms/Nio, bones=Xms/Nio`.
    **Don't repeat this:** when a fixed per-call cost dominates, instrument the
    call count next to every duration, or the profile cannot be acted on.

27. **Half a second of work for one player's extras froze the overlay for all
    of them.** `[POS-DBG] total=727.4ms (map=515.4ms ...) bp_src=base_networkable`:
    resolving a `PlayerModel` that is not already mapped walks the whole live
    `BaseNetworkable` buffer, and it ran *inline on the worker tick*. The three
    unmapped players out of 22 (`pm2bp=19`) therefore cost every other player
    their position update and their bones for the duration — the visible stall
    the user described, and the reason players appeared with a box and nothing
    else. The relationship is stable for as long as a player stays connected,
    so it is exactly the wrong thing to block on: `_refresh_pm_to_bp_cache` now
    fires it onto a background thread (`_start_bp_resolution`) and collects the
    result on a later tick. The tick uses whatever mapping exists right now; a
    newly-seen player gets health/name/held-item a fraction of a second late
    instead of freezing the overlay. Two things this needs and would be wrong
    without: the cost stays visible in the log (`bg=3x/515ms` in `[POS-DBG]`,
    because `map=` now only measures the cheap cached path and the walk would
    otherwise simply vanish), and the `BaseNetworkable` caches are guarded by
    `_entity_lock` — the tick and the resolver both *rebuild* those dicts by
    comprehension, so an unguarded overlap is a "dictionary changed size during
    iteration" crash, not a stale read. `_scan_world_entities` takes that lock
    with `blocking=False` and skips its round rather than waiting, since
    blocking would put the 500 ms walk straight back on the tick.
    **Don't repeat this:** before making a read faster, ask whether the tick
    needs to wait for it at all. Slow-changing, per-entity relationships
    (identity, ownership, class) belong off the critical path; only the pose
    has to be fresh.

28. **A refactor left a local behind and the worker swallowed it every tick.**
    Splitting `_scan_world_entities` into a wrapper plus a locked inner method
    moved `now = time.perf_counter()` into the wrapper while two uses stayed
    inside. `_loop` catches every exception per tick, so this was not a crash:
    it was `[DBG] ERR NameError: name 'now' is not defined` printed forever
    while the *entire rest of that tick* -- display build, snapshot, held
    items, names -- was silently skipped. The fix was to pass `now` in;
    `UnboundLocalGuardTests` now AST-checks every method in the package for a
    hot local that is read but never bound, because a mechanical refactor can
    reintroduce this at any time. **Don't repeat this:** a catch-all around a
    loop body converts programming errors into invisible partial work. When
    splitting a method, check the names that crossed the new boundary, and
    treat any per-tick `[DBG] ERR` as a stop-everything bug rather than noise.

29. **The parent-index read was one IOCTL per rig.** `bones=166.1ms/12io` with
    `hier=11/11` is 11 parent-buffer reads plus the single TRS batch. At a
    ~14 ms fixed driver round-trip the *call count* is the cost, so the shape
    of the read matters more than its size: `_read_parent_index_buffers` now
    collects every cache miss and fetches them all in one `batch_u64`, slicing
    the u64 words back into 4-byte parent indices. 12-20 calls become 2.
    `pread=` in `[BONE-DBG]` reports how many hierarchies actually missed, so
    the cache's behaviour stops being an assumption. **Don't repeat this:**
    a per-item loop containing a driver call is the single most expensive
    shape in this codebase. Look for `for ...: self.m.read(...)`.

30. **The retry backoff punished players it had never tried.**
    `_refresh_pm_to_bp_cache` doubles its retry interval up to
    `BP_MAPPING_MAX_BACKOFF` (30 s) whenever a resolution finishes with players
    still unmapped -- correct for a player that cannot be resolved, disastrous
    for a squad that just spawned in: they inherited the already-maxed backoff,
    so they had no bp, therefore no rig job, therefore no skeleton, for up to
    half a minute. That is `cand=2(local=1 norig=47)` with `job=0` in the log.
    The backoff now applies only to pms already in `_bp_attempted`; a pm nobody
    has tried resets it. `_bp_attempted` is pruned to the live roster, so a
    reconnecting player counts as new again. **Don't repeat this:** a backoff
    must key on *the thing that failed*, not on the operation. Ask what happens
    to a brand-new item arriving while the backoff is maxed.

31. **`bp_src=frame_batch@0x2E8` in the log was reporting a path that does not
    exist.** `model.py`'s `_read_player_frame_batch` does not read the
    `BasePlayer` pointer at all -- it takes it from `_pm_bp_cache`, which the
    background resolver fills -- but legacy `_tick` stamped
    `_last_bp_source = "frame_batch"` over the resolver's own label
    unconditionally. Anyone reading that line would have gone hunting for a
    drifted offset at `0x2E8`. Removed. **Don't repeat this:** a diagnostic
    that names a code path must be written by the path that ran, never by the
    caller assuming which one did.

32. **A scan interval shorter than the tick it produces is a feedback loop.**
    With held items at 0.5 s, names at 1 s and world entities at 2 s, the
    combined rate is 3.5 scans/second. That is fine at 60 ms ticks (~22% of
    ticks carry one) and self-sustaining at 250 ms ticks: 0.875 scans per tick,
    so a heavy scan landed on ~87% of them and `disp=` sat at 130-240 ms
    permanently. The scans were slowing the tick enough to guarantee their own
    intervals had already expired. Two changes break it. The held-item chain is
    ~6 *dependent* round-trips -- each stage's addresses come from the previous
    stage's results, so it cannot be collapsed into one batch, and at a ~14 ms
    floor it cannot go below ~85 ms; its interval moved to 1.5 s, where a
    weapon swap is still shown well within human reaction time. And the last
    two round-trips were removed outright: an `ItemDefinition`'s `shortName` is
    immutable and the definition is a per-item-type singleton, so
    `_itemdef_name_cache` resolves each item type once per session, with no TTL
    needed. An empty name is never cached -- that is what a failed string read
    looks like, and with no TTL it would pin the wrong label forever.
    **Don't repeat this:** a periodic scan's interval must be set against the
    tick time *it causes*, not the tick time you hope for. If cost × rate
    approaches 1 scan per tick, the throttle has stopped throttling.

33. **Diagnostics outlive the bug and keep charging for it.**
    `_debug_scan_entity_offsets` / `_debug_scan_dropped_item_collider` found the
    native transform chain (traps #12-#15). That chain is settled -- `[WE-POS]`
    reports `native_resolved == pos_resolved == matched` -- so every run since
    has paid their IOCTLs to print `ptrs_tried=49 candidates=[]`. They are now
    behind `ENABLE_WE_OFFSET_SCAN`, kept rather than deleted because they are
    the tool to reach for if entity positions break on a future build.
    **Don't repeat this:** a brute-force scanner is a debugging instrument, not
    a runtime feature. Gate it behind a flag the day it succeeds.

34. **A budget outlived the cost model it was written for.**
    `BONE_MAX_TRACKED_PLAYERS = 16` dates from when the bone sampler issued a
    read per player and the number of players was the cost. Every tracked
    player's slots now ride in a single `batch_u64`, and the driver charges
    ~14 ms *per call* regardless of payload -- so 16 players and 48 players are
    the same one IOCTL, differing only in address count (~132 per player: ~22
    slots x 6 u64 per TRS). The cap was therefore buying nothing while costing
    two thirds of the visible players their skeleton: the log read `cand=39
    sampled=16` alongside `drawn=24 sk=16`, which is the "players with a box
    and nothing else" complaint in its final form. Raised to 48, still
    comfortably inside one batch (`batch_u64` splits at 16000 addresses, i.e.
    ~121 players). **Don't repeat this:** when a cost model changes, the
    constants tuned against the old one become silent quality limits, not
    savings. After any batching change, re-derive every budget that was sized
    against per-item cost -- grep for MAX_*.

35. **`disp=` could not say which of its three scans paid.** Held items, names
    and world entities all live inside one timer, so a 148 ms spike named no
    culprit, and at a fixed per-call latency only the call count is actionable.
    `[TICK-LATENCY]` now appends `[held=Nio names=Nio we=Nio]`, written
    unconditionally each tick so a quiet tick reports nothing rather than
    keeping the previous spike's attribution on screen. **Don't repeat this:**
    a phase timer that aggregates several independent operations is not a
    measurement, it is a place for one of them to hide.

36. **The IOCTL counter was shared between three threads, so every per-phase
    number was inflated by an unknown amount.** `[POS-DBG]` reported
    `bones=6io` on a tick where the bone phase issues exactly one `batch_u64`
    and `pread=0` proved the parent-index cache had read nothing. The five
    extra calls were real, but they belonged to the background pm->bp resolver
    (`bg=4x/547ms/busy` on the same line): `Mem.io_calls` was a plain attribute
    and three threads increment it -- the worker tick, `CameraSampler`, and the
    resolver. It is now `threading.local()` behind `io_calls` / `io_wait_s`
    properties, so each thread accounts for its own work and `[TICK-LATENCY]`
    reports the tick's calls only. **Don't repeat this:** the moment work moves
    to a background thread, every shared counter measuring it becomes a lie.
    An instrument that reports another thread's work as yours is worse than no
    instrument, because it is trusted. Any phase attribution collected before
    this fix should be re-measured, not reasoned from.

37. **A one-shot diagnostic printed 94 lines in every session forever.**
    `_debug_dump_bone_names` is the tool that settled the bone IDs against live
    ground truth rather than a stale dump, and it must stay for the next build
    -- but it fired on every run, and 94 lines of console in every log anyone
    pastes is how real signals get missed. Now behind `ENABLE_BONE_NAME_DUMP`,
    same treatment as trap #33. **Don't repeat this:** log volume is a cost
    paid by whoever has to read it. A one-shot that answered its question gets
    a flag, not a permanent seat.

38. **The same trap as #29, one layer down: a driver call inside a per-player
    loop.** With honest per-thread accounting (trap #36) the log finally read
    `disp=1190.4ms [held=41io]`. `_read_held_items_batch` contains at most 12
    `batch_u64` calls of its own, so the other ~29 came from somewhere else:
    `resolve_tagged_handle` issues **two** `batch_u64` calls and was being
    called once per player, inside the loop that decrypts each inventory
    handle. `resolve_tagged_handles` (plural) is the batched form -- metadata
    for every handle in one call, slot/bitmap words for every survivor in a
    second -- so 20 players cost 2 IOCTLs instead of 40. The two functions
    implement the same algorithm and must stay in step; the tests assert that
    by comparing them directly on the same fake table rather than against
    hard-coded expectations. **Don't repeat this:** finding this pattern once
    does not remove it from the codebase. Grep for a driver call reachable from
    inside a `for` -- including through a helper, which is what hid this one.

39. **Half the tick was Python, not the driver.** Once the counters were
    honest, a steady tick read `io=2 calls 30.5ms (49% of tick)` on a 62 ms
    tick -- so ~30 ms was pure computation, and raising the tracked-player cap
    (trap #34) had quietly tripled it: `composed=819` bone compositions per
    tick. Each bone walks its ancestor chain applying scale -> rotate ->
    translate, and `_normalize_quaternion` was called *inside* that walk, so a
    rig's spine and hip joints -- present in nearly every chain -- were
    re-normalised once per bone that passed through them (~4900 normalise
    steps against ~860 distinct slots). Normalising once per slot at build time
    is behaviour-identical and removes most of them. **Don't repeat this:**
    when the I/O floor drops, the profile moves. `io% of tick` is the number
    that says whether to keep removing calls or start reading the loop.

40. **Two candidate truths about where a player is, and the overlay was mixing
    them.** User screenshot: the box and skeleton of a *running* bot at 28 m sit
    visibly to one side of the character. Both now come from the bones, and the
    bones are the pose the game is rendering -- they cannot be wrong about the
    character. What can be wrong is the shift applied on top: `get_snapshot`
    advances the bones by `predicted_pos - cur_pos`, a delta derived from the
    *networked* position and its velocity, capped at `MAX_PLAYER_PREDICTION`
    (0.12 s, ~0.66 m at a sprint). `[BONE-DBG] worst=` independently measures
    the rendered pose sitting up to 1.6 m from the networked position, so the
    two sources genuinely disagree and adding one to the other is not obviously
    right. Rather than pick from a still screenshot, the shift is now recorded
    on the player as `bone_shift`, reported as `pred=X.XXm` in `[RENDER]`, and
    reversible from the menu ("Extrapolate skeleton"). **Don't repeat this:**
    when two measurements of the same thing disagree and a screenshot cannot
    settle which is right, ship the A/B rather than the guess -- the person
    with the game open answers in two seconds what costs a round trip
    otherwise.

41. **The skeleton offset was never the extrapolation — the *position* is
    unstable.** Trap #40 shipped `pred=` and a toggle instead of a guess, and
    the answer came back immediately: `pred=` reads 0.02-0.74 m, typically
    0.1-0.3 m. Far too small to be the offset on screen, so that hypothesis is
    dead. What the same log shows instead is `[POS-JUMP] totals held=82
    accepted=28` — positions moving 26-43 m in ~60 ms, repeatedly, for many
    different players — with `reset` stuck at 12 the whole time. `reset` only
    increments when the `bp` behind a `pm` changes, so **this is not address
    reuse**: the same player, same `bp`, reads wildly different positions
    tick to tick.

    The knock-on is visible on the next line every time: `[BONE-DBG]
    worst=66.23m ... anchor=210 short=10 out=1`. The bones come from the
    rendered transform hierarchy and are correct; they get thrown out because
    they sit far from the *jumped* position. So the anchor gate punishes the
    good data for the bad data's error, and `[RENDER] drawn=0 sk=0 boxbone=0`
    follows. Position instability, not bone math, is what makes the overlay
    flicker and drift.

    `[POS-JUMP]` now prints `bp=`, `from=` and `to=` for both held and
    **accepted** jumps, because "held 30m in 61ms" cannot distinguish the three
    cases that need opposite fixes: a recycled `pm` (identity bug), a bad read
    landing inside the sanity range (position source at fault), or a real move
    (the gate is what is wrong). The accepted ones matter most — those are the
    ones that reach the screen.

    **Don't repeat this:** a validator that gates A against B assumes B is the
    more trustworthy of the two. When the overlay is unstable, check that
    assumption before tuning the validator — here the bones are ground truth
    for what is drawn and the networked position is the noisy one, which is the
    opposite of how the anchor gate is wired.

42. **Held items moved off the tick.** The chain is ~10 *dependent* driver
    round-trips, so it has a hard floor around 11 IOCTLs (trap #38 already
    removed the per-player loop) and was the last consistent source of
    118-300 ms tick spikes. A held weapon is decoration, not pose, so the
    existing background bp-resolver became a "slow lane" that also runs it:
    `_read_held_items_batch(on_worker=True)` skips the tick's heavy-slot claim,
    because decrementing shared per-tick state from another thread would
    corrupt the tick's own accounting. `_start_bp_resolution(None)` fires a
    slow-lane-only pass when every bp is already mapped -- without it held
    items would refresh only while some player was unmapped, i.e. almost never
    on a settled server. Confirmed live: `disp=0.1ms [slow=11io/471ms]`.
    **Don't repeat this:** moving work off the tick must not remove it from the
    profile. `slow=Nio/Xms` keeps the driver cost visible where the tick cost
    used to be.

43. **A backoff bypass became a runaway.** Trap #30 let a never-tried `pm` skip
    the retry backoff so a spawning squad would not sit for 30 s with no
    skeleton. Correct in isolation, wrong against a real roster: a busy
    server's player list churns every tick, so there is *always* a never-tried
    `pm`, and the bypass turned into "walk the whole BaseNetworkable buffer
    continuously". The 2026-08-25 log shows it plainly -- `bg=11x/622ms/busy`
    with `bp_src` permanently stuck on `base_networkable`, i.e. a ~620 ms
    driver walk running back to back forever, competing with the worker for
    the driver lock the whole time. `BP_MAPPING_MIN_INTERVAL` is a hard floor
    between two walks that the freshness bypass cannot cross, so trap #30's
    benefit survives (a new player waits sub-second, not 30 s) with the cost
    capped at one walk per interval. **Don't repeat this:** an exception that
    says "skip the rate limit when X is new" needs its own rate limit whenever
    X can be new continuously. Check the exception against churn, not against
    the single case that motivated it.

44. **Roster flicker kept re-arming the bypass, and the floor guarded the
    wrong thing.** Trap #43's `BP_MAPPING_MIN_INTERVAL` cut the runaway but not
    far enough: the next log still read `bg=26x` at ~650 ms a walk against a
    0.75 s floor -- the background thread walking the BaseNetworkable buffer
    ~83% of the time. Two causes, both about *which* code the guard covers.

    First, `_bp_attempted` was pruned against the live roster every tick
    (`&= active`). `LC count` bounces constantly (7, 9, 10, 13, 14, 15 ...) as
    PlayerModels flicker in and out, so a blinking `pm` was forgotten and
    counted as brand new the moment it came back -- re-arming the freshness
    bypass roughly once per floor interval. It is now a `pm -> last seen` dict
    pruned only after `BP_ATTEMPT_MEMORY`: a player gone that long is a real
    reconnect and *should* be new; one that blinked for three ticks is the
    same player.

    Second, the floor gated only the freshness branch. A tick where nothing
    was missing consumed the pending result without ever advancing the retry
    deadline, so the next tick with a missing `pm` walked immediately. The
    floor now guards the **spawn** itself: nothing starts a ~620 ms walk more
    often than the interval, whatever branch asked for it.

    **Don't repeat this:** a rate limit belongs on the expensive operation, not
    on one of the reasons for calling it. Every other path is a hole, and the
    hole is found by whatever the caller does most often -- here, ordinary
    roster churn. Note also what the regression test does *not* assert: the
    backoff does not accumulate across flicker, because a tick with nothing
    missing legitimately resets it. The floor is the guarantee; the backoff is
    not.

45. **`if terminator > 0` swallowed the empty string.** `_read_klass_names`
    trims a fixed 64-byte buffer at its first NUL. A klass whose name pointer
    read back as zero has that NUL at index 0, so `> 0` skipped the trim
    entirely and the *whole* null-filled buffer became the klass name -- cached
    under that klass for the rest of the session and printed verbatim in
    `[WE-NAMES]` as a 64-character garbage entry sitting between 'BasePlayer'
    and 'TreeManager'. `>= 0` fixes it; an empty name is the honest answer for
    a failed read and the caller already treats it as not-drawable.
    **Don't repeat this:** `find()` returns 0 for a match at the start, and
    `> 0` is the classic way to lose it. Any "trim at the terminator" needs
    `>= 0`, and the failure mode is silent garbage rather than an error.

46. **A retry with no failure counter is an infinite loop with extra steps.**
    `_fail_rig_job` rescheduled every failed rig resolution 0.35 s later,
    forever. Some entries in the `PlayerModel` list have no usable skeleton at
    all -- a corpse, a bot on a different model, a player still streaming in --
    so those pms never stopped retrying. That is the steady `norig=5..7` with
    `job=0` in every log: not players waiting for a slot, players stuck in a
    permanent 0.35 s loop. Worse, each retry consumed one of the six
    `BONE_MAX_NEW_JOBS_PER_TICK` admission slots, starving players who *could*
    have resolved -- which is why `sk` sat a few below `drawn` indefinitely.
    The delay now doubles per consecutive failure up to `BONE_RIG_RETRY_MAX`
    and resets the moment a rig succeeds; an explicit `delay=` still wins, so
    `_invalidate_bone_rig`'s deliberate immediate retry is unaffected.
    `cold=N` in `[BONE-DBG]` counts the pms that have failed three or more
    times, because a permanently unresolvable entity and a player about to
    resolve are otherwise indistinguishable in `norig=`.
    **Don't repeat this:** this is the third time in this codebase the same
    shape has bitten (see traps #30, #43, #44). Any retry needs to answer
    "what if this never succeeds?" -- and if the retry also consumes a shared
    budget, the answer is that it starves everything else.

47. **A rigid shift cannot animate a skeleton.** User report: "les bones ne
    sont pas collés en permanence aux joueurs quand ils bougent, il doit
    rester attaché a leurs body". `get_snapshot` translated every bone by the
    *body's* predicted delta, so the pose never changed between worker ticks:
    at a ~60 ms tick and a 144 Hz overlay that is nine frames of an unchanging
    skeleton sliding along, then a jump to the next sample. The body moves
    smoothly; the limbs do not follow it.

    The obvious fix -- difference each bone against its own previous sample --
    animates the limbs but breaks the *other* half of the request, and the
    existing tests caught it immediately: a bone's raw delta is unsmoothed
    while the body's velocity is smoothed and clamped, so the skeleton stops
    being welded to the predicted position. Three tests that assert exactly
    that invariant failed.

    Splitting the two keeps both. Every bone still gets `prediction_delta`, so
    the skeleton stays attached exactly as before; on top of that it gets only
    its motion *in the body's frame* -- `(bone_now - bone_prev) - (pos_now -
    pos_prev)` -- which is the arm swing and nothing else. Residuals above
    `BONE_EXTRAPOLATION_MAX_SPEED` fall back to the body delta, and a bone the
    previous snapshot lacked gets the body delta too.

    **Don't repeat this:** when a request has two halves ("must move" and
    "must stay attached"), the naive fix usually satisfies one by sacrificing
    the other. Subtracting the shared component and predicting only the
    residual keeps both, and it is the same shape as any frame-of-reference
    problem. Also: those three failing tests were the requirement, not an
    obstacle -- do not relax an invariant to make a new feature pass.

    Note also a misreading corrected here: `[BONE-DBG] worst=1.55-1.83m` is
    **not** pose/network divergence. It is the distance from the anchor to the
    furthest bone, i.e. roughly the height of a standing skeleton. Normal
    geometry, not error. Earlier notes treating it as drift were wrong.

48. **Predicting an unsmoothed value as far as a smoothed one amplifies its
    noise.** Trap #47 shipped per-limb extrapolation and the user reported
    trembling on the very next run. The residual was scaled by
    `prediction_time / dt_snap`: with `prediction_time` capped at 0.12 s and
    `dt_snap` around 0.06 s that is **2.0**, so every bit of sample noise in a
    raw two-sample bone difference was doubled.

    The body's velocity earns the full `MAX_PLAYER_PREDICTION` because it is
    smoothed and, when the probe latched, engine-backed. The limb residual has
    no smoothing whatsoever. `BONE_LIMB_SCALE_MAX = 1.0` caps it at one
    observed interval: never claim a limb kept moving longer than it was
    actually watched moving.

    **Don't repeat this:** a prediction horizon is only as long as the signal's
    quality allows. Two quantities in the same formula can deserve very
    different horizons, and reusing one factor for both is how a smoothing
    decision silently gets undone. Whenever a new term joins an existing
    extrapolation, ask what smoothing it has -- not just what units it is in.

    Also settled here, for the second time: `tools/tt/` (the Codex branch) has
    no fluidity advantage. Same `WORKER_HZ=120`, same `CAMERA_SAMPLE_HZ`,
    byte-identical prediction, and its own `view.py` carries a comment
    *rejecting* screen-space smoothing. Stop re-checking it.

49. **Names joined the slow lane; only the world-entity scan is left on the
    tick.** After trap #42 moved held items off, `disp=88-133ms [names=3io]`
    became the last recurring spike -- once per second, and only ~45 ms of it
    was driver time, the rest Python. A player's display name never changes
    while they are connected, so it is decoration by the same argument. Same
    `on_worker` guard, same slow lane. What remains on the tick is
    `_scan_world_entities`, which is the one scan whose result the user can
    switch off entirely.

    **Watching a video helped where the log could not.** Extracted frames
    (`ffmpeg -vf select=between(t,..)`) showed the skeleton correctly welded to
    a moving player at 10 m -- so traps #47/#48 hold, and the remaining
    "lag/freeze" is tick latency, not bone math. It also showed that what reads
    as visual chaos at 15+ players is the tracer fan, which is working exactly
    as designed and is one checkbox away. **Don't repeat this:** when a report
    is about *how it looks*, a few frames answer in seconds what a log cannot
    answer at all. `ffmpeg` is on PATH here.

50. **Extrapolation cannot be smooth; that is what it is.** The user still
    reported the skeleton as not fluid after traps #47/#48. The reason is
    structural, not a tuning value: an extrapolated residual grows across
    every rendered frame and then **snaps back to zero** the instant a new
    snapshot lands. At a ~60 ms worker tick against a 144 Hz overlay that is a
    visible hitch six times a second, and no clamp removes it.

    The pose is now **interpolated** between the two most recent snapshots, so
    every frame shows a shape bounded by two genuinely measured ones -- there
    is nothing to snap back from. The cost is that the shape trails the newest
    sample by up to one snapshot (~60 ms), far less than the networked
    position already lags reality.

    The body keeps its extrapolation, so the skeleton still sits on the player
    with no added latency. Only the *shape* is interpolated, and bone offsets
    are taken relative to each snapshot's own position before blending -- that
    is what keeps the two decisions independent. `BONE_POSE_BLEND_MAX_SQ`
    refuses to blend across a rig swap, which would smear the skeleton over
    the gap instead of snapping cleanly.

    **Don't repeat this:** "make it smoother" applied to an extrapolator is a
    category error. Smoothness comes from interpolating between known values;
    extrapolation buys latency at the cost of a discontinuity at every new
    sample. Decide which of the two the feature needs before tuning anything.

51. **`is_item_hovered()` binds to the widget above it, and inserting a
    checkbox steals the next tooltip.** Adding "Extrapolate skeleton" between
    "Box follows skeleton" and its tooltip left both tooltips attached to the
    new checkbox -- the second overwriting the first -- so the new one showed
    the wrong text and the old one showed nothing. Nothing errors; the menu
    just quietly lies. **Don't repeat this:** in an immediate-mode UI, adding a
    widget is an insertion into a positional sequence. Check what follows the
    insertion point, not just that the new widget renders.

52. **The freeze, measured instead of described.** The user kept reporting
    "it freezes sometimes". Frame-differencing their overlay capture settled
    it in one command: over one second of 60 fps video, **15 consecutive
    strictly identical frames -- 250 ms of the overlay completely frozen** --
    followed by a large jump. The log line for it was already there:
    `total=316.5ms ... disp=255.8ms [we=21io]`. The world-entity scan, 21
    IOCTLs on the tick.

    It is now on the slow lane with the other two, so **no heavy scan runs on
    the tick at all**. Note also that trap #50's interpolation makes a stall
    *look* worse than extrapolation did: once alpha saturates the pose stops
    dead, where extrapolation kept sliding (wrongly). That is the correct
    trade -- a frozen pose is honest, a sliding wrong one is not -- but it
    means tick stalls became visible rather than disguised.

    **How to measure it again:**
    `ffmpeg -i FILE -vf "select='between(t,5,6)',scale=640:-1" -vsync 0 d_%03d.png`
    then diff consecutive frames with PIL. A run of near-zero diffs is a
    freeze, and its length times the frame interval is its duration.
    **Don't repeat this:** "sometimes it stutters" is not actionable, and
    neither is a wall of tick timings. One second of video converts both into
    a number.

53. **Removing work from the tick without wiring it into its new home leaves
    it running nowhere.** Moving `_scan_world_entities` to the slow lane took
    two edits in two files; the first script asserted its way out before
    writing, so the tick-side removal landed and the slow-lane call did not.
    Nothing errored -- world entities simply stopped updating, silently, and
    the full test suite still passed. `test_the_slow_lane_runs_all_three_scans`
    now asserts every scan is actually called there. **Don't repeat this:**
    a move is a remove *and* an add. When a patch script can fail between the
    two halves, verify the destination, not just that the source is clean.

54. **A priority flag that nobody waits on is not a priority.**
    User report: turning the camera makes every overlay element slide, "comme
    un aspect vomito, c'est elastique". That is view-matrix staleness -- the
    VP used to project is read at a different instant than the frame drawn
    with it, so a camera rotation shifts everything projected.

    `Mem` already had `_camera_waiting`, set by the camera read. But every
    other reader only did `if self._camera_waiting.is_set(): time.sleep(0)` --
    a bare GIL yield that does not hold anything back -- and a Python `Lock`
    is not FIFO, so the camera lost the race repeatedly. With the slow lane
    now issuing 17-36 back-to-back IOCTLs (`slow=36io/1611ms` in the log), the
    view matrix could go stale for a long stretch, which is what turns a
    barely-perceptible softness into rubber-banding.

    `_camera_idle` inverts the flag into something waitable: set when the
    camera is *not* reading, cleared while it is, restored in a `finally`.
    Non-camera readers call `_yield_to_camera()` and actually block, bounded
    by a timeout so a lost flag costs one timeout rather than a deadlock.
    `vpage=` in `[RENDER]` reports the age so the result is measurable.

    **The floor is still the driver.** The VP is one 64-byte read, ~15 ms, so
    the sampler tops out near 66 Hz against a 144 Hz overlay -- 0-15 ms of
    staleness remains no matter what, and `CAMERA_SAMPLE_HZ = 240` has never
    been achievable. This fix removes the *bursts* where it was far worse; it
    cannot remove the floor.

    **Don't repeat this:** an `Event` named `_waiting` that only ever gets
    `is_set()` checked, next to a `sleep(0)`, is a comment pretending to be a
    mechanism. Priority means somebody waits.

55. **The elastic overlay, quantified: `vpage=` tracks the slow lane's
    bursts.** Trap #54 added `[RENDER] vpage=` and the first log with it read
    4, 7, 12, 17, 19, 19, 25, 26, 35, 42 ms. The correlation is unambiguous:
    **4-7 ms right after a slow-lane pass finished, 25-42 ms while one was
    running.** At 144 Hz a frame is 7 ms, so 25-42 ms is three to six frames
    of view-matrix lag -- exactly the slide the user described.

    The slow lane ran held items, names and world entities back to back, 22-36
    IOCTLs in one go, and the camera sampler queues behind every one of them.
    It now runs **one scan per pass**, round-robin. Same total work, a third
    of the peak.

    **The floor is architectural and will not go away.** The VP is one ~15 ms
    driver read, and `_yield_to_camera` only stops others from *starting* -- a
    call already in flight still has to finish. Worst case therefore stays
    around 30 ms and typical around 8-15 ms. Getting below a frame would need
    the camera on its own channel, or extrapolating the view matrix, which is
    a different and much riskier piece of work.

    **Don't repeat this:** total throughput was never the problem here; the
    *peak* was. When one consumer is latency-critical and shares a serialised
    resource, batching work together is exactly the wrong shape -- spread it.

---

## 5. Local verification without the game

Math and offset-drift logic are unit-testable — use these before asking for an
in-game run:

```
python3 -m unittest tools.rust_esp_mvc.test_skeleton_math
python3 -m unittest tools.rust_esp_mvc.test_render_fixes
```

`test_render_fixes` covers the teleport gate (trap #18), the velocity probe
(traps #19 and #23), the timer resolution (trap #20), the adaptive bone-anchor
radius and its outcome scoring (trap #21), the heavy-scan budget (trap #22), the
tick-relative skeleton window (trap #24), the feature gates (trap #25), the
candidate-rejection breakdown, the off-tick pm->bp resolution and its entity
lock (trap #27), the unbound-local AST guard (trap #28), the batched
parent-index read (trap #29), the first-attempt backoff (trap #30), the
immutable item-name cache and scan interval (trap #32), the gated brute-force
scanners (trap #33), the bone tracking budget against the batch limits
(trap #34), the disp attribution (trap #35), the thread-local IOCTL counters
(trap #36), the gated one-shot dumps (traps #33 and #37), the batched handle
resolver against the single-handle one (trap #38), the quaternion memoisation
against hand-computed bone positions (trap #39), the reversible skeleton
prediction (trap #40), the slow lane's on_worker guard (trap #42), the walk
floor against roster churn (traps #43 and #44), the klass-name terminator
(trap #45), the rig retry backoff (trap #46), the limb extrapolation against
the welded-to-body invariant (trap #47), the limb amplification cap
(trap #48), the on-tick scan budget after the slow-lane moves (trap #49), the
interpolated pose against its bounds (trap #50), the slow-lane wiring
(trap #53), the camera priority (trap #54), the slow lane's round-robin
(trap #55), the ESP colour palette, and
`calculate_bone_box`'s fallbacks. Trap #41 is diagnostic only — no test, it
needs an in-game log.

Everything else (chain walking, pointer yields, rig resolution) **can only be
validated in-game**. Instrument the failing gate and ask for one run; don't
change an offset and ask for a retest. If a pasted yield number *drops* after a
change, revert the change.
