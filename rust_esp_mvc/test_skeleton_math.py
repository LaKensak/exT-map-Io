"""Pure tests for Unity Transform hierarchy composition (UC bulk-buffer method)."""

import math
import struct
import threading
import time
import unittest

from tools.rust_esp_mvc import legacy_runtime as legacy
from tools.rust_esp_mvc.legacy_runtime import OFF, RustGame
from tools.rust_esp_mvc.model import (
    RustGameModel,
    _compose_position_from_buffers,
    _normalize_quaternion,
    _parse_parent_indices,
    _parse_trs_buffer,
    _rotate_vector,
    TRSX_SIZE,
)


def _build_trs_raw(entries):
    """Build a raw TRS buffer from a list of (t, q, s) tuples."""
    raw = b''
    for t, q, s in entries:
        raw += struct.pack('<3f', *t)       # Vec3 t  (12 bytes)
        raw += b'\x00' * 4                  # pad     (4 bytes)
        raw += struct.pack('<4f', *q)       # Vec4 q  (16 bytes)
        raw += struct.pack('<3f', *s)       # Vec3 s  (12 bytes)
        raw += b'\x00' * 4                  # pad     (4 bytes)
    return raw


def _build_parent_raw(indices):
    """Build a raw parent indices buffer from a list of int32."""
    return struct.pack(f'<{len(indices)}i', *indices)


class BulkBufferBoneTests(unittest.TestCase):
    def test_identity_root(self):
        """Bone at root (parent = -1): world pos = local translation."""
        trs_entries = [
            ((1.0, 2.0, 3.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
        ]
        trs_raw = _build_trs_raw(trs_entries)
        parent_raw = _build_parent_raw([-1])
        trs_buf = _parse_trs_buffer(trs_raw, 1)
        parent_buf = _parse_parent_indices(parent_raw, 1)

        result = _compose_position_from_buffers(trs_buf, parent_buf, 0, 1)
        self.assertIsNotNone(result)
        for actual, expected in zip(result, (1.0, 2.0, 3.0)):
            self.assertAlmostEqual(actual, expected, places=5)

    def test_parent_rotation_scale_and_translation(self):
        """Chain: bone 0 → parent 1 → parent 2 (root, parent=-1)."""
        half_sqrt = math.sqrt(0.5)
        trs_entries = [
            # Index 0: leaf bone
            ((1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
            # Index 1: parent with 90° Z rotation and scale(2,1,1)
            ((0.0, 2.0, 0.0), (0.0, 0.0, half_sqrt, half_sqrt), (2.0, 1.0, 1.0)),
            # Index 2: grandparent (root)
            ((10.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 3.0, 1.0)),
        ]
        trs_raw = _build_trs_raw(trs_entries)
        parent_raw = _build_parent_raw([1, 2, -1])  # 0→1, 1→2, 2→root

        trs_buf = _parse_trs_buffer(trs_raw, 3)
        parent_buf = _parse_parent_indices(parent_raw, 3)

        result = _compose_position_from_buffers(trs_buf, parent_buf, 0, 3)
        self.assertIsNotNone(result)
        # Order is S→R→T per parent, i.e. Unity's Transform.TransformPoint:
        #     result = parent.t + parent.q * (parent.s * result)
        #
        # This test previously asserted R→S→T and expected (10, 9, 0). That was
        # wrong. Cl1kExternal's ReadBonePosition (sdk/classes.h) computes
        # `tmp7 = _mm_mul_ps(v3, result)` — v3 is the scale row, and it is applied
        # to `result` *before* tmp7 is fed through the quaternion-rotate
        # expression, which then has v1 (translation) added last.
        #
        # The two orders agree whenever scale is uniform, which is why the bug
        # hid for so long: only the joints carrying non-uniform scale drifted.
        #
        # Leaf (1,0,0) → scale(2,1,1) → (2,0,0) → rot 90°Z → (0,2,0) → +t(0,2,0) → (0,4,0)
        # → scale(1,3,1) → (0,12,0) → rot identity → (0,12,0) → +t(10,0,0) → (10,12,0)
        for actual, expected in zip(result, (10.0, 12.0, 0.0)):
            self.assertAlmostEqual(actual, expected, places=4)


    def test_xyzw_quaternion_layout(self):
        half_sqrt = math.sqrt(0.5)
        q = _normalize_quaternion((0.0, 0.0, half_sqrt, half_sqrt))
        result = _rotate_vector(q, (1.0, 0.0, 0.0))
        for actual, expected in zip(result, (0.0, 1.0, 0.0)):
            self.assertAlmostEqual(actual, expected, places=5)

    def test_no_parent_fallback(self):
        """Bone whose parent index is out of range should still return."""
        trs_entries = [
            ((5.0, 6.0, 7.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
        ]
        trs_raw = _build_trs_raw(trs_entries)
        parent_raw = _build_parent_raw([99])  # parent index out of range
        trs_buf = _parse_trs_buffer(trs_raw, 1)
        parent_buf = _parse_parent_indices(parent_raw, 1)

        # Parent index 99 >= capacity 1, so loop breaks immediately
        result = _compose_position_from_buffers(trs_buf, parent_buf, 0, 1)
        self.assertIsNotNone(result)
        for actual, expected in zip(result, (5.0, 6.0, 7.0)):
            self.assertAlmostEqual(actual, expected, places=5)

    def test_invalid_quaternion_is_rejected(self):
        self.assertIsNone(_normalize_quaternion((0.0, 0.0, 0.0, 0.0)))
        self.assertIsNone(_normalize_quaternion((math.nan, 0.0, 0.0, 1.0)))

    def test_prediction_keeps_bones_attached_to_player(self):
        model = object.__new__(RustGameModel)
        model._lock = threading.Lock()
        model._render_motion_cache = {}
        model.diag = ""
        model.tick_ms = 0.0
        now = time.perf_counter()
        model._snap_prev = (
            now - 0.06,
            [{
                "pm": 1,
                "pos": (0.0, 0.0, 0.0),
                "bones": {47: (0.0, 1.7, 0.0)},
                "sleeping": False,
            }],
            None,
        )
        model._snap_cur = (
            now - 0.01,
            [{
                "pm": 1,
                "pos": (0.1, 0.0, 0.0),
                "bones": {47: (0.1, 1.7, 0.0)},
                "sleeping": False,
            }],
            None,
        )
        players = model.get_snapshot()[0]
        player = players[0]
        relative = tuple(
            bone - origin
            for bone, origin in zip(player["bones"][47], player["pos"])
        )
        for actual, expected in zip(relative, (0.0, 1.7, 0.0)):
            self.assertAlmostEqual(actual, expected, places=5)

    def test_trs_buffer_parsing(self):
        """Verify trsX struct parsing (48 bytes per entry)."""
        entries = [
            ((1.0, 2.0, 3.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
            ((4.0, 5.0, 6.0), (0.0, 0.0, 0.707, 0.707), (2.0, 2.0, 2.0)),
        ]
        raw = _build_trs_raw(entries)
        self.assertEqual(len(raw), 2 * TRSX_SIZE)
        parsed = _parse_trs_buffer(raw, 2)
        self.assertEqual(len(parsed), 2)
        for actual, expected in zip(parsed[0][0], (1.0, 2.0, 3.0)):
            self.assertAlmostEqual(actual, expected, places=5)
        for actual, expected in zip(parsed[1][0], (4.0, 5.0, 6.0)):
            self.assertAlmostEqual(actual, expected, places=5)

    def test_deep_chain_safety_limit(self):
        """Ensure parent walk doesn't infinite-loop on circular parent refs."""
        n = 10
        trs_entries = [
            ((float(i), 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0))
            for i in range(n)
        ]
        # Circular: 0→1→2→...→9→0
        parents = list(range(1, n)) + [0]
        trs_raw = _build_trs_raw(trs_entries)
        parent_raw = _build_parent_raw(parents)
        trs_buf = _parse_trs_buffer(trs_raw, n)
        parent_buf = _parse_parent_indices(parent_raw, n)

        # Should not hang; PARENT_WALK_LIMIT=200 will break the loop
        result = _compose_position_from_buffers(trs_buf, parent_buf, 0, n)
        self.assertIsNotNone(result)  # Result may be garbage but must not hang


_ARR = 0x20000000
_ENTITY_COUNT = 6000
_ENTITIES = [0x30000000 + i * 0x1000 for i in range(_ENTITY_COUNT)]
_PLAYERS = set(_ENTITIES[:12])


class _FakeMem:
    """Counts driver transactions so the tests can assert on IO, not wall time."""

    def __init__(self):
        self.reads = 0
        self.read_bytes = 0
        self.batch_addrs = 0
        self.scattered = 0

    def read(self, addr, size):
        self.reads += 1
        self.read_bytes += size
        if addr == _ARR + 0x20 and size == _ENTITY_COUNT * 8:
            return struct.pack(f'<{_ENTITY_COUNT}Q', *_ENTITIES)
        return b'\x00' * size

    def i32_retry(self, a, attempts=5):
        return _ENTITY_COUNT

    def batch_u64(self, addrs, attempts=3):
        self.batch_addrs += len(addrs)
        return [
            OFF.k_player_prefab_id
            if (a - OFF.prefab_id) in _PLAYERS
            else 0x11111111
            for a in addrs
        ]

    def read_ptr_array(self, addrs, attempts=3, fallback_limit=512):
        self.scattered += 1
        return self.batch_u64(addrs, attempts)


def _fake_game():
    """A RustGame with only the entity-buffer state populated, no driver."""
    game = RustGame.__new__(RustGame)
    game.m = _FakeMem()
    game.ga = 0x10000000
    game._cached_arr = _ARR
    game._cached_list_dict = 0x21000000
    game._entity_chain_at = float('inf')
    game._entity_ptr_cache = []
    game._entity_ptr_cache_count = 0
    game._entity_ptr_cache_at = 0.0
    game._entity_prefab_cache = {}
    game._entity_prefab_cursor = 0
    # Shared with the background pm->bp resolver; RustGame.__init__ always
    # creates it, so a stub that skips __init__ has to supply it too.
    game._entity_lock = threading.RLock()
    return game


class EntityBufferCostTests(unittest.TestCase):
    """Guards the BaseNetworkable read path against re-regressing to per-element
    IO. Each of these was a measured [TICK-LATENCY] spike, not a hypothetical:
    the scattered walk and the uncached prefab batch together made a tick that
    merely needed to map one new player cost 400-800ms (pos=)."""

    def test_array_payload_read_is_contiguous(self):
        """Il2CppArray elements are adjacent, so one CMD_READ, not N batched."""
        game = _fake_game()
        ptrs, _ = game._read_il2cpp_array_ptrs(
            _ARR, _ENTITY_COUNT, max_count=20000
        )
        self.assertEqual(ptrs, _ENTITIES)
        self.assertEqual(game.m.reads, 1)
        self.assertEqual(game.m.read_bytes, _ENTITY_COUNT * 8)
        self.assertEqual(game.m.scattered, 0)

    def test_scattered_path_still_covers_a_failed_read(self):
        """A dead page must fall back, not silently report an empty buffer."""
        game = _fake_game()
        game.m.read = lambda addr, size: b'\x00' * size
        ptrs, _ = game._read_il2cpp_array_ptrs(
            _ARR, _ENTITY_COUNT, max_count=20000
        )
        self.assertEqual(game.m.scattered, 1)
        self.assertTrue(ptrs)

    def test_buffer_walk_is_shared_between_consumers(self):
        """_scan_world_entities and _entity_baseplayers read the same array."""
        game = _fake_game()
        first = game._entity_buffer(20000)
        reads_after_first = game.m.reads
        second = game._entity_buffer(20000)
        self.assertEqual(first[2], _ENTITIES)
        self.assertEqual(second[2], _ENTITIES)
        self.assertEqual(game.m.reads, reads_after_first)

    def test_prefab_ids_are_cached_across_walks(self):
        """Only new entities plus the rotating window pay on later calls."""
        game = _fake_game()
        found = game._entity_baseplayers(20000)
        first_batch = game.m.batch_addrs
        self.assertEqual(set(found), _PLAYERS)
        # One batch for prefab_id (_ENTITY_COUNT addrs) plus one for the
        # klass-ptr NPC check, which only runs on entities the prefab check
        # didn't already clear as human (<= _ENTITY_COUNT more) -- see
        # _entity_npc_klass_cache in _entity_baseplayers_locked.
        self.assertGreaterEqual(first_batch, _ENTITY_COUNT)
        self.assertLessEqual(first_batch, 2 * _ENTITY_COUNT)

        game._entity_ptr_cache_at = 0.0   # force a real walk, not a TTL hit
        found_again = game._entity_baseplayers(20000)
        second_batch = game.m.batch_addrs - first_batch
        self.assertEqual(set(found_again), _PLAYERS)
        # Both the prefab cache and the NPC-klass cache revalidate off the
        # same rotating window, so a later walk pays for at most two such
        # windows, not just one (+ a small margin: the NPC check additionally
        # excludes whichever window entries the prefab pass just confirmed
        # human, so its own candidate count isn't bit-for-bit the same size).
        self.assertLessEqual(
            second_batch, 2 * legacy.ENTITY_PREFAB_REVALIDATE_PER_SCAN + len(_PLAYERS)
        )

    def test_failed_prefab_read_is_not_cached(self):
        """Caching a zero would hide a real player until the window came back."""
        game = _fake_game()
        game.m.batch_u64 = lambda addrs, attempts=3: [0] * len(addrs)
        game._entity_baseplayers(20000)
        self.assertEqual(game._entity_prefab_cache, {})


if __name__ == "__main__":
    unittest.main()
