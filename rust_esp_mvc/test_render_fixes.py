"""Tests for the teleport gate, the engine-velocity probe and the palette.

These cover the three fixes made for the "0 bug" render pass:

  * `_gate_position_jump` — a player who walks into the train tunnels used to
    render on the far side of the map until they came back out;
  * `_track_velocity` / `get_snapshot` — boxes and skeletons trailing a moving
    player because extrapolation differenced two position samples;
  * `Settings` colours — the user-editable ESP palette.

and the four that followed, for the micro-freezes during fast movement:

  * `raise_timer_resolution` — the 15.6 ms Windows timer tick was quantising
    every IOCTL that reached `Mem._wait`'s sleep backoff;
  * `_bone_anchor_radius` / `_record_bone_sample_outcome` — a fixed anchor gate
    turned fast movement into a rig-invalidation runaway;
  * `_take_heavy_slot` — the three heavy scans could land on the same tick;
  * `_track_velocity` — the probe differenced across tick periods instead of
    across genuine position changes, inflating measured speed several-fold.
"""

import ast
import builtins
import contextlib
import importlib
import inspect
import io
import math
import pathlib
import random
import struct
import textwrap
import threading
import time
import unittest

from tools.rust_esp_mvc import legacy_runtime as legacy
from tools.rust_esp_mvc.legacy_runtime import OFF, RustGame
from tools.rust_esp_mvc import view_base
from tools.rust_esp_mvc import model as model_mod
from tools.rust_esp_mvc import view as view_mod
from tools.rust_esp_mvc import controller as controller_mod
from tools.rust_esp_mvc.model import (
    BONE_MIN_VALID,
    TRSX_SIZE,
    RustGameModel,
)


def _bare_game():
    """A RustGame with only the state the gate and the probe touch."""
    game = object.__new__(RustGame)
    game._pm_bp_cache = {}
    game._pm_pos_time_cache = {}
    game._pm_pos_bp_cache = {}
    game._pm_jump_state = {}
    game._pos_jump_stats = {'held': 0, 'accepted': 0, 'reset': 0}
    game._next_pos_jump_log_at = time.perf_counter() + 3600.0
    game._vel_choice = None
    game._vel_probe = {'a': 0.0, 'b': 0.0, 'n': 0}
    game._vel_probe_prev = {}
    game._pm_vel_cache = {}
    return game


class PositionJumpGateTests(unittest.TestCase):
    def setUp(self):
        self.game = _bare_game()
        self.game._pm_bp_cache[1] = 0x7FF000000000

    def _gate(self, pos, prev):
        return self.game._gate_position_jump(1, pos, prev)

    def test_first_sample_is_always_accepted(self):
        self.assertEqual(self._gate((10.0, 0.0, 10.0), None), (10.0, 0.0, 10.0))

    def test_normal_walking_passes_straight_through(self):
        self._gate((0.0, 0.0, 0.0), None)
        time.sleep(0.02)
        moved = (0.12, 0.0, 0.0)
        self.assertEqual(self._gate(moved, (0.0, 0.0, 0.0)), moved)
        self.assertEqual(self.game._pos_jump_stats['held'], 0)

    def test_map_wide_jump_is_held_at_the_last_good_position(self):
        prev = (0.0, 0.0, 0.0)
        self._gate(prev, None)
        far = (900.0, 0.0, 900.0)
        self.assertEqual(self._gate(far, prev), prev)
        self.assertEqual(self.game._pos_jump_stats['held'], 1)

    def test_a_repeated_jump_is_believed_after_confirmation(self):
        prev = (0.0, 0.0, 0.0)
        self._gate(prev, None)
        far = (900.0, 0.0, 900.0)
        for _ in range(legacy.POS_JUMP_CONFIRM - 1):
            self.assertEqual(self._gate(far, prev), prev)
        self.assertEqual(self._gate(far, prev), far)
        self.assertEqual(self.game._pos_jump_stats['accepted'], 1)

    def test_a_wandering_jump_never_confirms(self):
        prev = (0.0, 0.0, 0.0)
        self._gate(prev, None)
        # Each sample lands somewhere else entirely: a recycled slot, not a
        # teleport. The confirmation counter must keep resetting.
        for step in range(legacy.POS_JUMP_CONFIRM + 2):
            far = (900.0 + step * 100.0, 0.0, 900.0)
            self.assertEqual(self._gate(far, prev), prev)
        self.assertEqual(self.game._pos_jump_stats['accepted'], 0)

    def test_the_hold_expires(self):
        prev = (0.0, 0.0, 0.0)
        self._gate(prev, None)
        far = (900.0, 0.0, 900.0)
        self._gate(far, prev)
        state = self.game._pm_jump_state[1]
        state['since'] -= legacy.POS_JUMP_HOLD_MAX + 0.1
        # Deliberately somewhere new, so only the expiry can let it through.
        self.assertEqual(self._gate((-800.0, 0.0, 0.0), prev), (-800.0, 0.0, 0.0))

    def test_a_recycled_slot_resets_instead_of_holding(self):
        prev = (0.0, 0.0, 0.0)
        self._gate(prev, None)
        self.game._pm_bp_cache[1] = 0x7FF000009999  # different BasePlayer
        far = (900.0, 0.0, 900.0)
        self.assertEqual(self._gate(far, prev), far)
        self.assertEqual(self.game._pos_jump_stats['reset'], 1)
        self.assertEqual(self.game._pos_jump_stats['held'], 0)


class VelocityProbeTests(unittest.TestCase):
    def setUp(self):
        self.game = _bare_game()

    def test_probe_requests_both_slots_then_only_the_winner(self):
        self.assertEqual(
            [tag for tag, _ in self.game._velocity_offsets()],
            ["vel_a", "vel_b"],
        )
        self.game._vel_choice = 'b'
        self.assertEqual(
            self.game._velocity_offsets(),
            (("vel_b", OFF.velocity_pm_b),),
        )
        self.game._vel_choice = ''
        self.assertEqual(self.game._velocity_offsets(), ())

    def _drive(self, samples_for):
        """Walk a player in +x at 5 m/s and feed the probe `samples_for(t)`."""
        pos = [0.0, 0.0, 0.0]
        now = 1000.0
        out = None
        for _ in range(RustGame.VEL_PROBE_SAMPLES + 2):
            now += 0.05
            pos[0] += 0.25  # 5 m/s
            out = self.game._track_velocity(1, tuple(pos), samples_for(), now)
        return out

    def test_the_slot_that_tracks_motion_wins(self):
        truth = (5.0, 0.0, 0.0)
        noise = (0.0, 0.0, -37.0)
        self._drive(lambda: {'a': noise, 'b': truth})
        self.assertEqual(self.game._vel_choice, 'b')

    def test_neither_slot_matching_disables_the_probe(self):
        self._drive(lambda: {'a': (0.0, 0.0, -37.0), 'b': (99.0, 0.0, 0.0)})
        self.assertEqual(self.game._vel_choice, '')

    def test_an_unreadable_slot_loses(self):
        self._drive(lambda: {'a': None, 'b': (5.0, 0.0, 0.0)})
        self.assertEqual(self.game._vel_choice, 'b')

    def test_a_standing_player_never_scores(self):
        now = 1000.0
        for _ in range(RustGame.VEL_PROBE_SAMPLES + 2):
            now += 0.05
            self.game._track_velocity(
                1, (0.0, 0.0, 0.0), {'a': (0.0, 0.0, 0.0), 'b': (9.0, 0.0, 0.0)}, now
            )
        self.assertEqual(self.game._vel_probe['n'], 0)
        self.assertIsNone(self.game._vel_choice)


class PredictionTests(unittest.TestCase):
    """get_snapshot must prefer the engine velocity once one is published."""

    @staticmethod
    def _model(vel):
        game = object.__new__(RustGame)
        game._lock = threading.Lock()
        game._render_motion_cache = {}
        game.diag = ""
        game.tick_ms = 0.0
        now = time.perf_counter()
        # Two identical position samples: differencing alone yields no motion,
        # so any predicted displacement can only come from the engine vector.
        def frame(ts):
            return (ts, [{
                "pm": 1,
                "pos": (0.0, 0.0, 0.0),
                "bones": {53: (0.0, 1.7, 0.0)},
                "sleeping": False,
                "vel": vel,
            }], None, [])
        game._snap_prev = frame(now - 0.05)
        game._snap_cur = frame(now - 0.01)
        return game

    def test_the_engine_velocity_no_longer_moves_the_drawn_player(self):
        """It used to, and that was the bug. See INTERPOLATION_ONLY.

        Two identical position samples: under the old scheme the engine
        vector alone pushed the player forward, which is exactly the
        +0.11..+0.23 m of invented travel [PRED-ERR] measured in game. The
        velocity is still computed -- [PRED-ERR] and [RIG-LAG] need it --
        but nothing drawn depends on it any more.
        """
        player = self._model((6.0, 0.0, 0.0)).get_snapshot()[0][0]
        self.assertAlmostEqual(player["pos"][0], 0.0, places=6)
        self.assertLessEqual(player["prediction_ms"], 0.0)
        self.assertEqual(player["velocity"][0], 6.0 * 0.85)

    def test_without_it_two_identical_samples_do_not_move(self):
        player = self._model(None).get_snapshot()[0][0]
        self.assertAlmostEqual(player["pos"][0], 0.0, places=6)

    def test_bones_stay_welded_to_the_predicted_position(self):
        player = self._model((6.0, 0.0, 0.0)).get_snapshot()[0][0]
        offset = tuple(
            bone - origin
            for bone, origin in zip(player["bones"][53], player["pos"])
        )
        for actual, expected in zip(offset, (0.0, 1.7, 0.0)):
            self.assertAlmostEqual(actual, expected, places=5)


class PaletteTests(unittest.TestCase):
    def test_defaults_reproduce_the_old_hard_coded_words(self):
        settings = view_base.Settings()
        self.assertEqual(settings.col("box"), view_base.COL_ACCENT)
        self.assertEqual(settings.col("distance"), view_base.COL_WHITE)
        self.assertEqual(settings.col("name"), view_base.COL_NAME)
        self.assertEqual(settings.col("held_item"), view_base.COL_HELD_ITEM)
        for etype, expected in view_base.WORLD_ENTITY_COLORS.items():
            self.assertEqual(settings.col("we_" + etype), expected, etype)

    def test_every_menu_entry_maps_to_a_real_palette_key(self):
        settings = view_base.Settings()
        listed = {
            key for _, entries in view_base.PALETTE_GROUPS for key, _ in entries
        }
        self.assertEqual(listed, set(view_base.DEFAULT_PALETTE))
        for key in listed:
            self.assertIn(key, settings.colors)

    def test_editing_and_resetting_a_colour(self):
        settings = view_base.Settings()
        settings.set_col("skeleton", (1.0, 0.0, 0.0, 1.0))
        self.assertEqual(settings.col("skeleton"), 0xFF0000FF)  # ABGR
        settings.reset_colors()
        self.assertEqual(settings.col("skeleton"), view_base.COL_ACCENT)

    def test_alpha_survives_the_round_trip(self):
        settings = view_base.Settings()
        settings.set_col("tracer", (0.0, 0.0, 0.0, 0.5))
        self.assertEqual(settings.col("tracer") >> 24, 128)

    def test_unknown_key_falls_back_to_white(self):
        self.assertEqual(view_base.Settings().col("nope"), view_base.COL_WHITE)


class BoneBoxTests(unittest.TestCase):
    """calculate_bone_box's guards, exercised through a stub projection."""

    def setUp(self):
        self._real = view_base.world_to_screen

    def tearDown(self):
        view_base.world_to_screen = self._real

    @staticmethod
    def _project(mapping):
        def stub(world_pos, *_args, **_kwargs):
            return mapping.get(tuple(world_pos))
        return stub

    def test_box_wraps_the_projected_bones_with_padding(self):
        bones = {i: (float(i), 0.0, 0.0) for i in range(10)}
        screen = {
            (float(i), 0.0, 0.0): (100.0 + i * 2.0, 200.0 + i * 6.0)
            for i in range(10)
        }
        view_base.world_to_screen = self._project(screen)
        box = view_base.calculate_bone_box(bones, (1,), 1920.0, 1080.0)
        self.assertIsNotNone(box)
        left, top, right, bottom = box
        self.assertLess(left, 100.0)
        self.assertGreater(right, 118.0)
        self.assertLess(top, 200.0)
        self.assertGreater(bottom, 254.0)

    def test_too_few_visible_bones_falls_back(self):
        bones = {i: (float(i), 0.0, 0.0) for i in range(10)}
        screen = {(float(i), 0.0, 0.0): (10.0, 10.0 * i) for i in range(4)}
        view_base.world_to_screen = self._project(screen)
        self.assertIsNone(
            view_base.calculate_bone_box(bones, (1,), 1920.0, 1080.0)
        )

    def test_a_degenerate_projection_falls_back(self):
        bones = {i: (float(i), 0.0, 0.0) for i in range(10)}
        screen = {(float(i), 0.0, 0.0): (10.0, 10.0) for i in range(10)}
        view_base.world_to_screen = self._project(screen)
        self.assertIsNone(
            view_base.calculate_bone_box(bones, (1,), 1920.0, 1080.0)
        )

    def test_no_bones_falls_back(self):
        self.assertIsNone(view_base.calculate_bone_box({}, (1,), 1920.0, 1080.0))


def _bare_model(tick_ms=0.0):
    """A RustGameModel with only the state the bone bookkeeping touches."""
    game = object.__new__(RustGameModel)
    game.tick_ms = tick_ms
    game._bone_last_outcome = {}
    game._bone_sample_failures = {}
    game._bone_slot_cache = {}
    game._bone_rig_identity = {}
    game._bone_resolve_jobs = {}
    game._bone_position_cache = {}
    game._bone_resolve_retry_at = {}
    return game


class TimerResolutionTests(unittest.TestCase):
    """Fix A: the 15.6 ms timer tick that quantised every IOCTL wait."""

    def setUp(self):
        self._saved = legacy._TIMER_RESOLUTION_RAISED
        legacy._TIMER_RESOLUTION_RAISED = False

    def tearDown(self):
        legacy._TIMER_RESOLUTION_RAISED = self._saved

    def test_raising_is_idempotent(self):
        calls = []
        real = legacy._measure_sleep_granularity
        legacy._measure_sleep_granularity = lambda *a, **k: calls.append(1) or 1.0
        try:
            legacy.raise_timer_resolution()
            first = len(calls)
            legacy.raise_timer_resolution()
            legacy.raise_timer_resolution()
            self.assertEqual(len(calls), first)
        finally:
            legacy._measure_sleep_granularity = real
        self.assertTrue(legacy._TIMER_RESOLUTION_RAISED)

    def test_a_failing_win32_call_does_not_take_the_worker_down(self):
        import ctypes

        def boom(*a, **k):
            raise OSError("nope")

        real = ctypes.WinDLL
        ctypes.WinDLL = boom
        try:
            legacy.raise_timer_resolution()   # must not raise
        finally:
            ctypes.WinDLL = real

    def test_the_measurement_reports_real_wall_time(self):
        ms = legacy._measure_sleep_granularity(request=0.0002, samples=3)
        self.assertGreater(ms, 0.0)
        self.assertLess(ms, 200.0)


class HeavyScanBudgetTests(unittest.TestCase):
    """Fix C: world-entity, name and held-item scans colliding on one tick."""

    def setUp(self):
        self.game = object.__new__(RustGame)

    def test_one_scan_per_tick(self):
        self.game._heavy_scan_budget = 1
        self.assertTrue(self.game._take_heavy_slot())
        self.assertFalse(self.game._take_heavy_slot())
        self.assertFalse(self.game._take_heavy_slot())

    def test_the_budget_refills_each_tick(self):
        self.game._heavy_scan_budget = 1
        self.assertTrue(self.game._take_heavy_slot())
        self.game._heavy_scan_budget = 1          # what _loop does per tick
        self.assertTrue(self.game._take_heavy_slot())

    def test_an_uninitialised_budget_still_allows_one(self):
        # _take_heavy_slot may be reached before _loop has run once.
        self.assertTrue(self.game._take_heavy_slot())

    def test_every_heavy_scan_is_off_the_tick(self):
        import inspect
        src = inspect.getsource(legacy.RustGame)
        # All three heavy scans now run on the slow lane (traps #42/#49/#52),
        # so none of them claims the tick's budget any more -- each guards its
        # claim with `on_worker` instead, because decrementing shared per-tick
        # state from another thread would corrupt the tick's own accounting.
        # The budget machinery stays: it is what any future on-tick scan must
        # use, and _take_heavy_slot is still exercised by its own tests.
        self.assertEqual(src.count("if not self._take_heavy_slot():"), 0)
        self.assertEqual(
            src.count("if not on_worker and not self._take_heavy_slot():"), 3
        )


class BoneAnchorRadiusTests(unittest.TestCase):
    """Fix B: the fixed anchor gate that caused the rig-invalidation runaway."""

    def test_a_fast_tick_keeps_the_original_radius(self):
        self.assertAlmostEqual(
            _bare_model(tick_ms=0.0)._bone_anchor_radius(),
            model_mod.BONE_ANCHOR_RADIUS,
        )

    def test_a_slow_tick_widens_the_gate(self):
        # 200 ms of staleness at 25 m/s is 5 m of legitimate divergence.
        self.assertAlmostEqual(
            _bare_model(tick_ms=200.0)._bone_anchor_radius(),
            model_mod.BONE_ANCHOR_RADIUS + 5.0,
        )

    def test_the_slack_is_capped(self):
        self.assertAlmostEqual(
            _bare_model(tick_ms=9000.0)._bone_anchor_radius(),
            model_mod.BONE_ANCHOR_RADIUS + model_mod.BONE_ANCHOR_SLACK_MAX,
        )

    def test_a_negative_tick_never_narrows_the_gate(self):
        self.assertAlmostEqual(
            _bare_model(tick_ms=-50.0)._bone_anchor_radius(),
            model_mod.BONE_ANCHOR_RADIUS,
        )

    def test_the_radius_is_monotonic_in_tick_time(self):
        radii = [_bare_model(tick_ms=t)._bone_anchor_radius()
                 for t in (0.0, 10.0, 50.0, 120.0, 400.0, 1000.0)]
        self.assertEqual(radii, sorted(radii))


class BoneSampleOutcomeTests(unittest.TestCase):
    """Fix B: anchor rejections must not be counted as rig failures."""

    def setUp(self):
        self.game = _bare_model()
        self.full = {i: (0.0, 0.0, 0.0) for i in range(model_mod.BONE_MIN_VALID)}

    def _score(self, bones):
        return self.game._record_bone_sample_outcome(1, bones)

    def test_a_full_skeleton_clears_the_strike_count(self):
        self.game._bone_sample_failures[1] = 2
        self.assertEqual(self._score(self.full), 'ok')
        self.assertNotIn(1, self.game._bone_sample_failures)

    def test_a_broken_rig_still_strikes_out(self):
        self.game._bone_slot_cache[1] = {'x': 1}
        for _ in range(3):
            self.assertEqual(self._score({}), 'short')
        # three strikes -> the rig is thrown away and re-resolved
        self.assertNotIn(1, self.game._bone_slot_cache)
        self.assertIn(1, self.game._bone_resolve_retry_at)

    def test_an_anchor_rejection_is_not_a_strike(self):
        # The rig composed a full skeleton; the anchor gate threw it out.
        self.game._bone_slot_cache[1] = {'x': 1}
        self.game._bone_last_outcome[1] = (model_mod.BONE_MIN_VALID, 4)
        for _ in range(20):
            self.assertEqual(self._score({}), 'anchor_only')
        self.assertEqual(self.game._bone_sample_failures.get(1, 0), 0)
        self.assertIn(1, self.game._bone_slot_cache)   # never invalidated

    def test_a_rig_that_composed_too_little_is_a_real_failure(self):
        # Composed fewer bones than the minimum: that is the rig, not the gate.
        self.game._bone_last_outcome[1] = (model_mod.BONE_MIN_VALID - 1, 4)
        self.assertEqual(self._score({}), 'short')
        self.assertEqual(self.game._bone_sample_failures[1], 1)

    def test_composed_bones_with_nothing_anchor_rejected_is_a_real_failure(self):
        self.game._bone_last_outcome[1] = (model_mod.BONE_MIN_VALID, 0)
        self.assertEqual(self._score({}), 'short')
        self.assertEqual(self.game._bone_sample_failures[1], 1)

    def test_anchor_rejections_do_not_hide_a_rig_that_later_breaks(self):
        self.game._bone_slot_cache[1] = {'x': 1}
        self.game._bone_last_outcome[1] = (model_mod.BONE_MIN_VALID, 4)
        self.assertEqual(self._score({}), 'anchor_only')
        # the rig stops composing at all -> strikes resume from zero and land
        self.game._bone_last_outcome[1] = (0, 0)
        for _ in range(3):
            self.assertEqual(self._score({}), 'short')
        self.assertNotIn(1, self.game._bone_slot_cache)


class VelocityProbeSamplingTests(unittest.TestCase):
    """Fix D: the probe must difference genuine motion, not tick periods."""

    def setUp(self):
        self.game = _bare_game()

    def test_a_worker_faster_than_the_network_does_not_inflate_speed(self):
        # This is the in-game failure: the networked position updates at ~15 Hz
        # while the worker ticks at ~200 Hz, so most ticks see the *same*
        # position. Differencing across a tick period instead of across the
        # real move made the measured speed several times too high, and
        # neither slot could ever match it.
        truth = (5.0, 0.0, 0.0)          # 5 m/s along +x
        net_dt = 1.0 / 15.0
        tick_dt = 1.0 / 200.0
        now = 100.0
        pos = (0.0, 0.0, 0.0)
        next_net = now + net_dt
        for _ in range(4000):
            if now >= next_net:
                pos = tuple(c + v * net_dt for c, v in zip(pos, truth))
                next_net += net_dt
            self.game._track_velocity(
                1, pos, {'a': (99.0, 0.0, 0.0), 'b': truth}, now
            )
            if self.game._vel_choice is not None:
                break
            now += tick_dt
        self.assertEqual(self.game._vel_choice, 'b')

    def test_an_unchanged_position_never_advances_the_probe(self):
        for i in range(50):
            self.game._track_velocity(
                1, (7.0, 0.0, 0.0),
                {'a': (5.0, 0.0, 0.0), 'b': (5.0, 0.0, 0.0)},
                100.0 + i * 0.01,
            )
        self.assertEqual(self.game._vel_probe['n'], 0)
        self.assertIsNone(self.game._vel_choice)



class BoneReanchorTtlTests(unittest.TestCase):
    """The window a sampled skeleton survives must track the tick rate.

    A bare 0.25 s constant silently became "no skeletons at all" once ticks
    took longer than that -- which is exactly what `sk=0 boxbone=0` in the log
    meant, with `out=3` skeletons produced on the very same tick.
    """

    @staticmethod
    def _ttl(tick_ms):
        return min(
            max(tick_ms / 1000.0 * legacy.BONE_REANCHOR_TICKS,
                legacy.BONE_REANCHOR_TTL_MIN),
            legacy.BONE_REANCHOR_TTL_MAX,
        )

    def test_a_fast_tick_keeps_the_original_window(self):
        self.assertAlmostEqual(self._ttl(10.0), legacy.BONE_REANCHOR_TTL_MIN)

    def test_a_slow_tick_outlives_itself(self):
        # The whole point: a skeleton sampled on tick N must still be there on
        # tick N+1, so the window must exceed one tick at every tick length.
        for tick_ms in (50.0, 108.0, 200.0, 334.0, 470.0):
            with self.subTest(tick_ms=tick_ms):
                self.assertGreater(self._ttl(tick_ms), tick_ms / 1000.0)

    def test_the_window_is_capped(self):
        self.assertAlmostEqual(self._ttl(10000.0), legacy.BONE_REANCHOR_TTL_MAX)

    def test_the_old_constant_would_have_dropped_every_slow_tick(self):
        # Guards the regression itself: 0.25 s could not survive these ticks,
        # all of which appear in the user's log.
        for tick_ms in (334.0, 498.0, 573.0, 623.0, 772.0):
            self.assertLess(0.25, tick_ms / 1000.0)
            self.assertGreater(self._ttl(tick_ms), tick_ms / 1000.0)


class FeatureGateTests(unittest.TestCase):
    """A feature the user switched off must cost zero reads."""

    def setUp(self):
        self.game = object.__new__(RustGame)
        self.game._heavy_scan_budget = 1
        self.game.world_entities = [object(), object()]
        self.game._next_world_scan_at = 0.0
        self.game._held_item_cache = {7: 'rifle'}
        self.game._next_held_item_scan_at = 0.0
        self.game._player_name_cache = {7: 'someone'}
        self.game._next_player_name_scan_at = 0.0
        self.game.wanted = {'world_entities': True, 'names': True,
                            'held_item': True}

    def test_the_slow_lane_runs_every_off_tick_scan(self):
        # The gap this catches: removing a scan from the tick without wiring
        # it into the slow lane leaves it running nowhere at all, and nothing
        # errors -- the feature just silently stops updating. That is exactly
        # what would happen to the player list now the tick no longer refreshes
        # it, so _refresh_topology is named here with the other three.
        import inspect
        src = inspect.getsource(RustGameModel._run_slow_lane)
        # Match on the method name plus `on_worker=True`, not on an exact call
        # string: the previous version asserted the literal text and broke the
        # moment the calls became lambdas, even though the wiring was intact.
        for name in ("_read_held_items_batch",
                     "_read_player_names_batch",
                     "_scan_world_entities",
                     "_refresh_topology"):
            self.assertIn(name, src, f"{name} is not wired into the slow lane")
        self.assertEqual(src.count("on_worker=True"), 3)

    def test_the_slow_lane_runs_one_scan_per_pass(self):
        # Bursting all three together is what inflated camera staleness to
        # 25-42ms (trap #55). Same total work, lower peak.
        import inspect
        src = inspect.getsource(RustGameModel._run_slow_lane)
        self.assertIn("self._slow_lane_cursor", src)

    def test_every_scan_is_reached_in_turn(self):
        game = object.__new__(RustGameModel)
        game._slow_lane_pm_to_bp = {1: 2}
        game._slow_lane_cursor = 0
        game._slow_lane_last = (0, 0.0)
        game.m = type("M", (), {"io_calls": 0})()
        seen = []
        game._read_held_items_batch = lambda *a, **k: seen.append("held")
        game._read_player_names_batch = lambda *a, **k: seen.append("names")
        game._scan_world_entities = lambda *a, **k: seen.append("we")
        game._refresh_topology = lambda *a, **k: seen.append("topo")
        for _ in range(8):
            game._run_slow_lane()
        self.assertEqual(seen, ["held", "names", "we", "topo"] * 2)


    def test_disabled_world_entities_scan_nothing_and_clear(self):
        self.game.wanted['world_entities'] = False
        self.game._scan_world_entities()
        self.assertEqual(self.game.world_entities, [])
        # and it must not have burned the tick's heavy slot
        self.assertEqual(self.game._heavy_scan_budget, 1)

    def test_disabled_names_return_the_cache_untouched(self):
        self.game.wanted['names'] = False
        self.assertEqual(self.game._read_player_names_batch({7: 1}),
                         {7: 'someone'})
        self.assertEqual(self.game._heavy_scan_budget, 1)

    def test_disabled_held_items_return_the_cache_untouched(self):
        self.game.wanted['held_item'] = False
        self.assertEqual(self.game._read_held_items_batch({7: 1}),
                         {7: 'rifle'})
        self.assertEqual(self.game._heavy_scan_budget, 1)

    def test_a_missing_flag_defaults_to_enabled(self):
        # An older/partial `wanted` dict must not silently disable a feature.
        # Park the interval in the future so the scan stops one step *past*
        # the gate: reaching that point without clearing proves it was let
        # through rather than gated off.
        self.game.wanted = {}
        self.game._next_world_scan_at = time.perf_counter() + 3600.0
        self.game._scan_world_entities()
        self.assertNotEqual(self.game.world_entities, [])

    def test_the_controller_pushes_every_gated_feature(self):
        import inspect
        from tools.rust_esp_mvc import controller
        src = inspect.getsource(controller)
        for key in ('world_entities', 'names', 'held_item'):
            self.assertIn(f"'{key}':", src)


class OpenGLSafetyFlagTests(unittest.TestCase):
    """PyOpenGL's per-call error check has to be off before GL is imported.

    Measured with py-spy over a ~20 s run on 2026-08-27: `glCheckError`
    (OpenGL/error.py) was the single largest own-time entry in the whole
    program at 3.62 s -- ahead of `swap_buffers` (2.95 s) and `batch_u64`
    (1.94 s). PyOpenGL calls glGetError() after every GL call, and ImGui's
    backend issues tens per frame at 144 Hz; each is a synchronous driver
    round-trip.

    The ordering is the fragile part: the flags are read at PyOpenGL import
    time, and *both* view_base and legacy_runtime import OpenGL.GL at module
    level. Setting them in either one is a coin flip on which imports first.
    """

    def test_error_checking_is_off_once_the_package_is_imported(self):
        try:
            import OpenGL
        except ImportError:
            self.skipTest("PyOpenGL is not installed")
        self.assertFalse(OpenGL.ERROR_CHECKING)
        self.assertFalse(OpenGL.ERROR_LOGGING)
        self.assertFalse(OpenGL.ARRAY_SIZE_CHECKING)
        # The flag alone proves nothing -- PyOpenGL reads it while building
        # each wrapper, so what matters is whether the built wrapper carries
        # a checker. It holds an _ErrorChecker when checking is on and None
        # when it is off, which is the difference glCheckError lives in.
        if view_base.gl is not None:
            self.assertIsNone(view_base.gl.glBindTexture.error_checker)

    def test_the_flags_live_in_the_package_init(self):
        """Not in a submodule, where import order would decide the winner."""
        init = pathlib.Path(view_base.__file__).parent / "__init__.py"
        src = init.read_text(encoding="utf-8")
        self.assertIn("ERROR_CHECKING = False", src)
        for name in ("legacy_runtime", "view_base", "view", "model",
                     "controller"):
            path = pathlib.Path(view_base.__file__).parent / (name + ".py")
            body = path.read_text(encoding="utf-8")
            self.assertNotIn(
                "ERROR_CHECKING", body,
                f"{name}.py sets a PyOpenGL flag too late to matter",
            )

    def test_the_risky_pair_is_left_alone(self):
        """STORE_POINTERS off without ERROR_ON_COPY on is a crash, not a win.

        PyOpenGL keeps a reference to arrays it converts; dropping that
        without also forbidding copies hands the driver a pointer into a
        freed temporary.
        """
        init = pathlib.Path(view_base.__file__).parent / "__init__.py"
        src = init.read_text(encoding="utf-8")
        self.assertNotIn("STORE_POINTERS =", src)
        self.assertNotIn("ERROR_ON_COPY =", src)

    def test_there_is_a_way_back_for_a_debugging_session(self):
        init = pathlib.Path(view_base.__file__).parent / "__init__.py"
        src = init.read_text(encoding="utf-8")
        self.assertIn("ESP_GL_DEBUG", src)

    def test_a_missing_c_accelerator_is_not_silent(self):
        """PyOpenGL falls back to pure Python without saying so.

        `OpenGL.USE_ACCELERATE` defaults to True, so an absent
        PyOpenGL_accelerate looks exactly like a present one from the
        flag -- and costs about 3.4 s per 20 s profile (wrapperCall 2.04,
        zeros 0.55, dataPointer 0.25, calculate_pyArgs 0.22, converters
        0.19, latebind 0.16). It went unnoticed until a flamegraph.
        """
        init = pathlib.Path(view_base.__file__).parent / "__init__.py"
        src = init.read_text(encoding="utf-8")
        self.assertIn("ACCELERATE_AVAILABLE", src)
        self.assertIn("pip install PyOpenGL_accelerate", src)

    def test_the_accelerator_is_actually_present_here(self):
        """Informational: skips rather than fails on a machine without it."""
        try:
            from OpenGL import acceleratesupport
        except ImportError:
            self.skipTest("PyOpenGL is not installed")
        if not acceleratesupport.ACCELERATE_AVAILABLE:
            self.skipTest(
                "PyOpenGL_accelerate is missing -- the overlay still runs, "
                "it just pays the pure-Python wrapper on every GL call"
            )
        from OpenGL.arrays import numpymodule
        self.assertIn("OpenGL_accelerate", numpymodule.NumpyHandler.__module__)


class TopologyOffTheTickTests(unittest.TestCase):
    """The tick must never spend a round-trip on who is in the game.

    The ListComponent buffer pointer and the PlayerModel pointer list change
    when someone joins or leaves, not when someone moves. Both were already
    cached at 1 Hz, but the refresh landed *on a tick*: measured in game
    2026-08-27, [REFRESH-HITRATE] chain=20/21 pm_list=20/21, and that one
    tick in twenty-one read `chain=43.6ms, pm_list=47.0ms` -- 90-140 ms
    against 41-48 ms for the other twenty. Since the render lag now *is* the
    tick interval, that single tick set the worst case for the whole second.
    """

    def test_the_tick_asks_for_topology_without_refreshing_it(self):
        src = inspect.getsource(legacy.RustGame._tick)
        self.assertIn("refresh_ok=False", src)

    def test_a_valid_cache_is_never_refreshed_for_the_tick(self):
        game = object.__new__(RustGame)
        game._cached_lc_buf_arr = 0x7FF000001000
        game._cached_lc_count = 42
        game._lc_chain_cache_at = time.perf_counter()
        game._lc_cache_ttl = 5.0
        # deliberately overdue: the refresh window has long since passed
        game._next_lc_refresh_at = 0.0
        game._lc_fast_path = False
        game.m = None          # any read at all would raise
        game.ga = 0

        buf, count, cached, error = game._get_lc_buffer(refresh_ok=False)

        self.assertEqual(buf, 0x7FF000001000)
        self.assertEqual(count, 42)
        self.assertTrue(cached)
        self.assertEqual(error, "")
        self.assertTrue(game._lc_fast_path)

    def test_an_invalid_cache_still_falls_through_to_a_real_read(self):
        """Bootstrap. With nothing to answer from, refusing is worse."""
        game = object.__new__(RustGame)
        game._cached_lc_buf_arr = 0
        game._cached_lc_count = 0
        game._lc_chain_cache_at = 0.0
        game._lc_cache_ttl = 5.0
        game._lc_refresh_interval = 1.0
        game._next_lc_refresh_at = 0.0
        game._lc_fast_path = True
        game._cached_lc_pm_list = 0
        game._cached_lc_klass = 0
        game._cached_lc_sf = 0
        reads = []
        game.m = type("M", (), {
            "u64": lambda self, addr: reads.append(addr) or 0,
        })()
        game.ga = 0x140000000

        buf, count, cached, error = game._get_lc_buffer(refresh_ok=False)

        self.assertTrue(reads, "bootstrap must still be allowed to read")
        self.assertEqual(buf, 0)
        self.assertNotEqual(error, "")

    def test_the_tick_falls_back_when_the_slow_lane_is_starved(self):
        """A wedged lane must degrade into a slow tick, not a frozen list."""
        src = inspect.getsource(legacy.RustGame._tick)
        self.assertIn("TOPOLOGY_SAFETY_TTL", src)
        self.assertIn("_pm_ptr_cache_updated_at", src)
        self.assertGreater(legacy.TOPOLOGY_SAFETY_TTL, 1.0)

    def test_the_tick_no_longer_owns_a_refresh_schedule(self):
        """The attribute is gone, not merely unused.

        Leaving `_next_pm_list_refresh_at` behind would leave two places
        that look like they schedule the same refresh, one of them dead.
        """
        src = inspect.getsource(legacy)
        self.assertNotIn("_next_pm_list_refresh_at", src)


class BoneRejectReasonTests(unittest.TestCase):
    """`rig=N cand=M` with M far below N needs a reason, not a guess."""

    def setUp(self):
        self.game = _bare_model()

    def test_no_reasons_renders_empty(self):
        self.game._bone_reject = {'sleeping': 0, 'far': 0}
        self.assertEqual(self.game._format_bone_rejects(), "")

    def test_only_non_zero_reasons_are_shown(self):
        self.game._bone_reject = {'local': 1, 'sleeping': 15, 'far': 0}
        out = self.game._format_bone_rejects()
        self.assertIn("sleeping=15", out)
        self.assertIn("local=1", out)
        self.assertNotIn("far", out)

    def test_an_unset_breakdown_is_safe(self):
        game = object.__new__(RustGameModel)
        self.assertEqual(game._format_bone_rejects(), "")



class AsyncBpResolutionTests(unittest.TestCase):
    """pm->bp resolution must never block the tick (trap #27)."""

    def setUp(self):
        self.game = object.__new__(RustGameModel)
        self.game._pm_bp_cache = {}
        self.game._bp_worker = None
        self.game._bp_worker_lock = threading.Lock()
        self.game._bp_result = None
        self.game._bp_runs = 0
        self.game._bp_last_ms = 0.0
        self.game._bp_mapping_backoff = model_mod.BP_MAPPING_RETRY_INTERVAL
        self.game._next_bp_mapping_at = 0.0
        self.game._next_bp_fresh_at = 0.0
        self.game._bp_attempted = {}
        self.resolved = threading.Event()
        self.calls = []

    def _install_resolver(self, mapping, delay=0.0):
        def fake(pm_ptrs):
            self.calls.append(list(pm_ptrs))
            if delay:
                time.sleep(delay)
            self.resolved.set()
            return dict(mapping)
        self.game._resolve_pm_to_bp = fake

    def _drain(self, timeout=2.0):
        worker = self.game._bp_worker
        if worker is not None:
            worker.join(timeout)

    def test_the_tick_does_not_wait_for_the_walk(self):
        self._install_resolver({1: 0x7FF000000000}, delay=0.30)
        started = time.perf_counter()
        self.game._refresh_pm_to_bp_cache([1])
        elapsed = time.perf_counter() - started
        # The real walk cost 515 ms; the tick must return essentially at once.
        self.assertLess(elapsed, 0.10)
        self.assertTrue(self.resolved.wait(2.0))
        self._drain()

    def test_the_mapping_lands_on_a_later_tick(self):
        self._install_resolver({1: 0x7FF000000000})
        self.game._refresh_pm_to_bp_cache([1])
        self._drain()
        self.assertEqual(self.game._pm_bp_cache, {})   # not yet collected
        self.game._refresh_pm_to_bp_cache([1])         # next tick collects it
        self.assertEqual(self.game._pm_bp_cache, {1: 0x7FF000000000})

    def test_only_one_walk_runs_at_a_time(self):
        self._install_resolver({1: 0x7FF000000000}, delay=0.20)
        for _ in range(25):
            self.game._refresh_pm_to_bp_cache([1])
        self._drain()
        self.assertEqual(len(self.calls), 1)

    def test_a_stale_player_never_enters_the_cache(self):
        # The walk started while pm=1 was live; by the time it finished the
        # player had gone. Its pointer must not be resurrected.
        self._install_resolver({1: 0x7FF000000000})
        self.game._refresh_pm_to_bp_cache([1])
        self._drain()
        self.game._refresh_pm_to_bp_cache([2])
        self.assertNotIn(1, self.game._pm_bp_cache)

    def test_an_invalid_pointer_is_rejected(self):
        self._install_resolver({1: 0x5})
        self.game._refresh_pm_to_bp_cache([1])
        self._drain()
        self.game._refresh_pm_to_bp_cache([1])
        self.assertEqual(self.game._pm_bp_cache, {})

    def test_a_fruitless_walk_backs_off(self):
        self._install_resolver({})
        self.game._refresh_pm_to_bp_cache([1])
        self._drain()
        before = self.game._bp_mapping_backoff
        self.game._refresh_pm_to_bp_cache([1])   # collects the empty result
        self.assertGreater(self.game._bp_mapping_backoff, before)
        self.assertGreater(self.game._next_bp_mapping_at, time.perf_counter())

    def test_a_crashing_walk_is_reported_and_does_not_wedge_the_resolver(self):
        def boom(pm_ptrs):
            raise RuntimeError("driver went away")
        self.game._resolve_pm_to_bp = boom
        self.game._refresh_pm_to_bp_cache([1])
        self._drain()
        # It published an empty result rather than leaving the tick waiting
        # forever for a mapping that will never arrive.
        self.assertEqual(self.game._bp_runs, 1)
        self.game._refresh_pm_to_bp_cache([1])
        self.assertGreater(self.game._bp_mapping_backoff,
                           model_mod.BP_MAPPING_RETRY_INTERVAL)

    def test_a_fully_mapped_roster_never_starts_a_walk(self):
        self._install_resolver({1: 0x7FF000000000})
        self.game._pm_bp_cache = {1: 0x7FF000000000}
        self.game._refresh_pm_to_bp_cache([1])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.game._bp_mapping_backoff,
                         model_mod.BP_MAPPING_RETRY_INTERVAL)

    def test_the_background_cost_stays_visible_in_the_log(self):
        self.assertEqual(self.game._format_bp_bg(), "")
        self._install_resolver({1: 0x7FF000000000})
        self.game._refresh_pm_to_bp_cache([1])
        self._drain()
        self.assertIn("bg=1x", self.game._format_bp_bg())


class EntityLockTests(unittest.TestCase):
    """The tick and the background resolver share the BaseNetworkable caches."""

    def test_the_world_scan_skips_rather_than_blocks(self):
        game = object.__new__(RustGame)
        game.wanted = {'world_entities': True}
        game._heavy_scan_budget = 1
        game._next_world_scan_at = 0.0
        game.world_entities = []
        game._entity_lock = threading.RLock()
        game._scan_world_entities_locked = lambda now: 'ran'
        held = threading.Event()
        release = threading.Event()

        def holder():
            with game._entity_lock:
                held.set()
                release.wait(2.0)

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        self.assertTrue(held.wait(2.0))
        started = time.perf_counter()
        game._scan_world_entities()          # must not wait on the holder
        elapsed = time.perf_counter() - started
        release.set()
        t.join(2.0)
        self.assertLess(elapsed, 0.10)
        # and it must not have consumed the retry interval either
        self.assertEqual(game._next_world_scan_at, 0.0)

    def test_the_scan_runs_when_the_lock_is_free(self):
        game = object.__new__(RustGame)
        game.wanted = {'world_entities': True}
        game._heavy_scan_budget = 1
        game._next_world_scan_at = 0.0
        game.world_entities = []
        game._entity_lock = threading.RLock()
        game._scan_world_entities_locked = lambda now: 'ran'
        self.assertEqual(game._scan_world_entities(), 'ran')
        self.assertGreater(game._next_world_scan_at, 0.0)



class UnboundLocalGuardTests(unittest.TestCase):
    """No method may read a local it never binds.

    `_scan_world_entities` was split into a locked inner method and `now` was
    left behind in the wrapper. The worker catches every exception per tick, so
    the result was not a crash but `[DBG] ERR NameError: name 'now' is not
    defined` on every tick with the rest of that tick silently skipped -- the
    worst possible failure mode. A mechanical refactor can reintroduce this at
    any time, so it is checked rather than remembered.
    """

    HOT_LOCALS = ('now', 'pos', 'vals', 'ptrs', 'raw', 'buffer', 'capacity')

    def test_no_method_reads_an_unbound_hot_local(self):
        import ast
        import builtins
        import pathlib
        offenders = []
        pkg = pathlib.Path(legacy.__file__).parent
        for path in sorted(pkg.glob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for fn in [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                stores = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
                if fn.args.vararg:
                    stores.add(fn.args.vararg.arg)
                if fn.args.kwarg:
                    stores.add(fn.args.kwarg.arg)
                loads = []
                for node in ast.walk(fn):
                    if isinstance(node, ast.Name):
                        if isinstance(node.ctx, ast.Store):
                            stores.add(node.id)
                        elif isinstance(node.ctx, ast.Load):
                            loads.append((node.id, node.lineno))
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        # a nested def binds its own params; count the name too
                        stores.add(node.name)
                        stores.update(a.arg for a in node.args.args)
                for name, lineno in loads:
                    if name in self.HOT_LOCALS and name not in stores \
                            and not hasattr(builtins, name) \
                            and name not in vars(legacy):
                        offenders.append(f"{path.name}:{lineno} {fn.name} -> {name}")
        self.assertEqual(offenders, [], "unbound local(s): " + "; ".join(offenders))


class ParentIndexBatchTests(unittest.TestCase):
    """Parent-index buffers must cost one IOCTL total, not one per rig."""

    class _CountingMem:
        def __init__(self, word):
            self.word = word
            self.batches = []
            self.reads = 0

        def batch_u64(self, addrs, attempts=3, **kwargs):
            self.batches.append(list(addrs))
            return [self.word] * len(addrs)

        def read(self, addr, size):
            self.reads += 1
            return b'\xff' * size

    def _game(self, word=0):
        game = object.__new__(RustGameModel)
        game.m = self._CountingMem(word)
        game._parent_index_cache = {}
        game._bone_debug = {}
        return game

    def test_many_hierarchies_cost_one_call(self):
        game = self._game()
        needed = {0x7FF000000000 + i * 0x1000: (0x7FF100000000 + i * 0x1000, 40)
                  for i in range(16)}
        out = game._read_parent_index_buffers(needed)
        self.assertEqual(len(game.m.batches), 1)
        self.assertEqual(game.m.reads, 0)          # no per-rig read() survives
        self.assertEqual(len(out), 16)
        self.assertEqual(game._bone_debug['parent_reads'], 16)

    def test_a_cached_hierarchy_costs_nothing(self):
        game = self._game()
        needed = {0x7FF000000000: (0x7FF100000000, 40)}
        game._read_parent_index_buffers(needed)
        game.m.batches.clear()
        out = game._read_parent_index_buffers(needed)
        self.assertEqual(game.m.batches, [])
        self.assertEqual(game._bone_debug['parent_reads'], 0)
        self.assertEqual(len(out), 1)

    def test_only_the_misses_are_fetched(self):
        game = self._game()
        a = {0x7FF000000000: (0x7FF100000000, 40)}
        game._read_parent_index_buffers(a)
        both = dict(a)
        both[0x7FF000002000] = (0x7FF100002000, 40)
        game.m.batches.clear()
        out = game._read_parent_index_buffers(both)
        self.assertEqual(len(game.m.batches), 1)
        self.assertEqual(game._bone_debug['parent_reads'], 1)
        self.assertEqual(len(out), 2)

    def test_a_growing_capacity_refetches(self):
        game = self._game()
        game._read_parent_index_buffers({0x7FF000000000: (0x7FF100000000, 40)})
        game.m.batches.clear()
        # deeper bone index than last time: the cached buffer is too short
        out = game._read_parent_index_buffers({0x7FF000000000: (0x7FF100000000, 90)})
        self.assertEqual(len(game.m.batches), 1)
        self.assertEqual(len(out[0x7FF000000000]), 90)

    def test_an_odd_capacity_still_yields_every_slot(self):
        # capacity*4 is not a multiple of 8, so the last u64 word is a partial
        # read that must be trimmed, not dropped.
        game = self._game()
        out = game._read_parent_index_buffers(
            {0x7FF000000000: (0x7FF100000000, 41)}
        )
        self.assertEqual(len(out[0x7FF000000000]), 41)

    def test_an_absurd_capacity_is_refused(self):
        game = self._game()
        out = game._read_parent_index_buffers(
            {0x7FF000000000: (0x7FF100000000, model_mod.TRSX_MAX_CAPACITY + 1)}
        )
        self.assertEqual(out, {})
        self.assertEqual(game.m.batches, [])

    def test_the_addresses_walk_the_buffer_in_eight_byte_words(self):
        game = self._game()
        game._read_parent_index_buffers({0x7FF000000000: (0x7FF100000000, 40)})
        addrs = game.m.batches[0]
        self.assertEqual(addrs[0], 0x7FF100000000)
        self.assertEqual(addrs[1] - addrs[0], 8)
        self.assertEqual(len(addrs), (40 * 4 + 7) // 8)



class BpBackoffTests(unittest.TestCase):
    """The retry backoff may ration retries, never first attempts.

    A squad spawning in used to inherit a backoff already doubled up to
    BP_MAPPING_MAX_BACKOFF (30 s), so brand-new players sat with no bp --
    therefore no rig job, therefore no skeleton -- for up to half a minute.
    That is `cand=2(local=1 norig=47)` with `job=0` in the 2026-08-25 log.
    """

    def setUp(self):
        self.game = object.__new__(RustGameModel)
        self.game._pm_bp_cache = {}
        self.game._bp_worker = None
        self.game._bp_worker_lock = threading.Lock()
        self.game._bp_result = None
        self.game._bp_runs = 0
        self.game._bp_last_ms = 0.0
        self.game._bp_attempted = {}
        self.game._bp_mapping_backoff = model_mod.BP_MAPPING_RETRY_INTERVAL
        self.game._next_bp_mapping_at = 0.0
        self.game._next_bp_fresh_at = 0.0
        self.started = []
        # Called with None for a slow-lane-only pass (nothing to map).
        self.game._start_bp_resolution = (
            lambda pms: self.started.append(list(pms) if pms else None)
        )

    def _finish(self, mapping):
        """Simulate a background resolution completing with `mapping`."""
        self.game._bp_result = dict(mapping)

    def test_a_brand_new_player_is_tried_immediately(self):
        # Worst case: the backoff is already maxed out from earlier failures.
        self.game._bp_mapping_backoff = model_mod.BP_MAPPING_MAX_BACKOFF
        self.game._next_bp_mapping_at = time.perf_counter() + 3600.0
        self.game._refresh_pm_to_bp_cache([42])
        self.assertEqual([x for x in self.started if x], [[42]])
        self.assertEqual(self.game._bp_mapping_backoff,
                         model_mod.BP_MAPPING_RETRY_INTERVAL)

    def test_a_squad_spawning_in_does_not_inherit_the_backoff(self):
        self.game._bp_attempted = {1: time.perf_counter()}
        self.game._pm_bp_cache = {}
        self.game._bp_mapping_backoff = model_mod.BP_MAPPING_MAX_BACKOFF
        self.game._next_bp_mapping_at = time.perf_counter() + 3600.0
        roster = list(range(1, 40))
        self.game._refresh_pm_to_bp_cache(roster)
        self.assertEqual(len([x for x in self.started if x]), 1)

    def test_the_same_unresolvable_player_still_backs_off(self):
        self.game._refresh_pm_to_bp_cache([7])       # first attempt
        self.assertEqual([x for x in self.started if x], [[7]])
        self.assertIn(7, self.game._bp_attempted)
        before = self.game._bp_mapping_backoff
        self._finish({})                              # resolved nothing
        self.game._refresh_pm_to_bp_cache([7])
        self.assertGreater(self.game._bp_mapping_backoff, before)
        self.assertEqual(len([x for x in self.started if x]), 1)        # and did not retry now

    def test_the_backoff_keeps_doubling_on_repeated_failure(self):
        self.game._refresh_pm_to_bp_cache([7])
        seen = []
        for _ in range(4):
            self._finish({})
            self.game._next_bp_mapping_at = 0.0
            self.game._refresh_pm_to_bp_cache([7])
            seen.append(self.game._bp_mapping_backoff)
        self.assertEqual(seen, sorted(seen))
        self.assertLessEqual(seen[-1], model_mod.BP_MAPPING_MAX_BACKOFF)

    def test_a_reconnecting_player_counts_as_new_again(self):
        self.game._refresh_pm_to_bp_cache([7])
        self._finish({})
        self.game._refresh_pm_to_bp_cache([7])
        self.game._bp_mapping_backoff = model_mod.BP_MAPPING_MAX_BACKOFF
        self.game._next_bp_mapping_at = time.perf_counter() + 3600.0
        self.started.clear()

        # Blinking out of the roster for a few ticks must NOT forget them:
        # `LC count` bounces every tick, and forgetting on absence is what
        # re-armed the freshness bypass continuously (trap #44).
        self.game._refresh_pm_to_bp_cache([])
        self.assertIn(7, self.game._bp_attempted)
        self.game._next_bp_fresh_at = 0.0
        self.game._refresh_pm_to_bp_cache([7])
        self.assertEqual([x for x in self.started if x], [])

        # Gone longer than the memory, though, is a real reconnect.
        self.game._bp_attempted[7] = (
            time.perf_counter() - model_mod.BP_ATTEMPT_MEMORY - 1.0
        )
        self.game._refresh_pm_to_bp_cache([])        # prunes the stale entry
        self.assertEqual(dict(self.game._bp_attempted), {})
        self.game._next_bp_fresh_at = 0.0            # past the walk floor
        self.game._refresh_pm_to_bp_cache([7])       # and came back
        self.assertEqual([x for x in self.started if x], [[7]])

    def test_roster_flicker_does_not_re_arm_the_bypass(self):
        """The regression: `bg=26x` at ~650 ms a walk, thread busy ~83%."""
        self.game._refresh_pm_to_bp_cache([7])
        self._finish({})
        self.started.clear()
        # 7 flickers out and back 30 times, as the real LC list does.
        for _ in range(30):
            self.game._refresh_pm_to_bp_cache([])
            self.game._refresh_pm_to_bp_cache([7])
            self._finish({})

        # Guarantee 1: flicker never made 7 look brand new. (The backoff
        # itself is *not* asserted here -- a tick where nothing is missing
        # legitimately resets it, so it does not accumulate across flicker.
        # The floor below is what actually caps the cost.)
        self.assertIn(7, self.game._bp_attempted)

        # Guarantee 2: the floor capped the walks. The loop runs in well under
        # one interval, so at most the first spawn got through -- against 30
        # before the fix, at ~620 ms of driver time each.
        walks = len([x for x in self.started if x])
        self.assertLessEqual(walks, 1, f"{walks} walks from pure flicker")

    def test_success_resets_the_backoff(self):
        self.game._bp_mapping_backoff = model_mod.BP_MAPPING_MAX_BACKOFF
        self.game._refresh_pm_to_bp_cache([7])
        self._finish({7: 0x7FF000000000})
        self.game._refresh_pm_to_bp_cache([7])
        self.assertEqual(self.game._pm_bp_cache, {7: 0x7FF000000000})
        self.assertEqual(self.game._bp_mapping_backoff,
                         model_mod.BP_MAPPING_RETRY_INTERVAL)

    def test_a_partial_success_still_chases_the_rest(self):
        # 1 resolved, 2 is new and unmapped: 2 must not wait out 1's *backoff*
        # (which can reach 30 s). It does wait out the walk floor, which is
        # sub-second and exists so the ~620 ms BaseNetworkable walk cannot run
        # back to back forever -- see BP_MAPPING_MIN_INTERVAL and trap #43.
        self.game._refresh_pm_to_bp_cache([1])
        self._finish({1: 0x7FF000000000})
        self.started.clear()
        self.game._refresh_pm_to_bp_cache([1, 2])
        self.assertEqual([x for x in self.started if x], [])   # floor holds it

        self.game._next_bp_fresh_at = 0.0                      # floor elapsed
        self.game._refresh_pm_to_bp_cache([1, 2])
        self.assertEqual([x for x in self.started if x], [[1, 2]])
        # and it went now, not after the 30 s backoff
        self.assertEqual(self.game._bp_mapping_backoff,
                         model_mod.BP_MAPPING_RETRY_INTERVAL)

    def test_constant_roster_churn_cannot_run_the_walk_continuously(self):
        """The regression this floor exists for.

        A busy server's player list churns every tick, so there is *always* a
        never-tried pm. Trap #30 let such a pm skip the backoff, which turned
        into "walk the whole BaseNetworkable buffer continuously" -- the log
        read `bg=11x/622ms/busy` with `bp_src` stuck on `base_networkable`.
        """
        # 40 ticks, a brand-new unmappable player on every one of them.
        for tick in range(40):
            self.game._refresh_pm_to_bp_cache([1000 + tick])
            self._finish({})
        walks = len([x for x in self.started if x])
        # Without the floor this is 40. With it, the elapsed wall time of the
        # loop is far below one interval, so at most the first one goes.
        self.assertLessEqual(walks, 2, f"{walks} walks for 40 churn ticks")
        self.assertGreaterEqual(walks, 1, "a new player must still be tried")

    def test_the_walk_floor_is_sub_second(self):
        # The floor delays a new player's health/name/held-item. It must stay
        # short enough that the delay is not what the user notices.
        self.assertLessEqual(model_mod.BP_MAPPING_MIN_INTERVAL, 1.0)
        self.assertLess(model_mod.BP_MAPPING_MIN_INTERVAL,
                        model_mod.BP_MAPPING_RETRY_INTERVAL)



class ItemDefNameCacheTests(unittest.TestCase):
    """An ItemDefinition's shortName is immutable, so resolve it once.

    The held-item scan is a chain of dependent round-trips at a ~14 ms driver
    floor, and the last two (shortName pointer, then the string) were being
    paid on every pass for item types already seen.
    """

    def setUp(self):
        self.game = object.__new__(RustGame)
        self.game._itemdef_name_cache = {}

    def test_the_cache_starts_empty_and_survives_lookups(self):
        cache = self.game._itemdef_name_cache
        self.assertEqual(cache, {})
        cache[0x1000] = "rifle.ak"
        self.assertEqual(cache.get(0x1000), "rifle.ak")

    def test_a_display_name_override_still_applies(self):
        # The cache stores the raw shortName; the pretty name is applied on the
        # way out, so overrides keep working for cached entries.
        raw = next(iter(legacy.ITEM_DISPLAY_NAMES), None)
        if raw is None:
            self.skipTest("no display-name overrides defined")
        self.assertNotEqual(
            legacy.ITEM_DISPLAY_NAMES.get(raw, raw), None
        )

    def test_the_scan_interval_is_slower_than_the_scan_itself(self):
        # The 0.5 s interval was shorter than the tick the scan produced, so a
        # heavy scan landed on nearly every tick. It must stay comfortably
        # above the worst observed scan cost (~0.24 s).
        self.assertGreaterEqual(legacy.HELD_ITEM_SCAN_INTERVAL, 1.0)

    def test_an_empty_name_is_never_cached(self):
        import inspect
        src = inspect.getsource(legacy.RustGame._read_held_items_batch)
        # A failed string read looks like an empty name; caching it would pin
        # the wrong label for the whole session, since this cache has no TTL.
        self.assertIn("if short_name:", src)


class TempDiagnosticsTests(unittest.TestCase):
    """Diagnostics that have served their purpose must not keep costing IOCTLs."""

    def test_the_brute_force_entity_scanners_are_off(self):
        self.assertFalse(legacy.ENABLE_WE_OFFSET_SCAN)

    def test_the_bone_name_dump_is_off_but_kept(self):
        # 94 lines in every session's console is noise in every log; the tool
        # itself must stay, it is what settles bone IDs on a new build.
        self.assertFalse(model_mod.ENABLE_BONE_NAME_DUMP)
        self.assertTrue(hasattr(RustGameModel, '_debug_dump_bone_names'))

    def test_they_are_kept_rather_than_deleted(self):
        # They are the tool to reach for if entity positions break on a new
        # build, so the flag must still guard real code.
        self.assertTrue(hasattr(legacy.RustGame, '_debug_scan_entity_offsets'))
        self.assertTrue(
            hasattr(legacy.RustGame, '_debug_scan_dropped_item_collider')
        )

    def test_the_flag_actually_gates_both_calls(self):
        import inspect
        src = inspect.getsource(legacy.RustGame)
        for name in ('_debug_scan_entity_offsets',
                     '_debug_scan_dropped_item_collider'):
            call = f"self.{name}(matched, ent_names)"
            self.assertIn(call, src)
        gate = src.split("if ENABLE_WE_OFFSET_SCAN:")[1][:200]
        self.assertIn("_debug_scan_entity_offsets", gate)
        self.assertIn("_debug_scan_dropped_item_collider", gate)



class BoneTrackingBudgetTests(unittest.TestCase):
    """The tracked-player cap was set for a cost model that no longer holds.

    The driver charges ~14 ms per call whatever the payload, and every tracked
    player's slots ride in ONE batch_u64 -- so capping at 16 bought nothing and
    cost two thirds of the visible players their skeleton (`cand=39
    sampled=16`, `drawn=24 sk=16`).
    """

    # ~22 slots per player, 6 u64 per TRS slot.
    ADDRESSES_PER_PLAYER = 22 * 6
    BATCH_SPLIT = 16000          # batch_u64 splits above this

    def test_the_cap_covers_a_realistic_roster(self):
        self.assertGreaterEqual(model_mod.BONE_MAX_TRACKED_PLAYERS, 40)

    def test_a_full_tracked_set_still_fits_one_batch(self):
        addrs = model_mod.BONE_MAX_TRACKED_PLAYERS * self.ADDRESSES_PER_PLAYER
        self.assertLess(addrs, self.BATCH_SPLIT)

    def test_a_full_tracked_set_fits_the_shared_buffer(self):
        addrs = model_mod.BONE_MAX_TRACKED_PLAYERS * self.ADDRESSES_PER_PLAYER
        self.assertLess(addrs * 8, legacy.COMM_DATA_SIZE)

    def test_the_batch_size_still_equals_the_tracked_set(self):
        # The round-robin only makes sense if these differ; they must not,
        # or players outside the window silently lose their skeleton again.
        self.assertEqual(model_mod.BONE_MAX_PLAYERS_PER_BATCH,
                         model_mod.BONE_MAX_TRACKED_PLAYERS)


class DispAttributionTests(unittest.TestCase):
    """`disp=148ms` must name which scan paid for it."""

    def setUp(self):
        self.game = object.__new__(RustGame)

    def test_a_quiet_tick_prints_nothing(self):
        self.game._last_disp_io = (0, 0, 0)
        self.assertEqual(self.game._format_disp_io(), "")

    def test_an_unset_attribution_is_safe(self):
        self.assertEqual(object.__new__(RustGame)._format_disp_io(), "")

    def test_only_the_scan_that_paid_is_named(self):
        self.game._last_disp_io = (0, 11, 0)
        out = self.game._format_disp_io()
        self.assertIn("names=11io", out)
        self.assertNotIn("held", out)
        self.assertNotIn("we=", out)

    def test_several_scans_are_all_named(self):
        self.game._last_disp_io = (6, 11, 3)
        out = self.game._format_disp_io()
        for part in ("held=6io", "names=11io", "we=3io"):
            self.assertIn(part, out)

    def test_the_attribution_is_written_every_tick(self):
        import inspect
        src = inspect.getsource(legacy.RustGame._tick)
        # Must be unconditional: a stale attribution on a quiet tick would
        # point at a scan that did not run.
        self.assertIn("self._last_disp_io = (_io_held, _io_names, _io_we)", src)
        self.assertNotIn("if _io_held or _io_names or _io_we:", src)



class ThreadLocalIoCountersTests(unittest.TestCase):
    """IOCTL accounting must not attribute another thread's work to this one.

    Three threads issue reads on one Mem: the worker tick, CameraSampler and
    the background pm->bp resolver. With a shared counter, [POS-DBG] reported
    `bones=6io` on a tick where the bone phase issues exactly one batch and
    `pread=0` proved the parent cache had read nothing -- the extra five were
    the resolver's. Every phase number derived from a shared counter is
    inflated by an unknown amount, which is worse than having no number.
    """

    def _mem(self):
        mem = object.__new__(legacy.Mem)
        mem._io_tls = threading.local()
        return mem

    def test_a_fresh_thread_starts_at_zero(self):
        mem = self._mem()
        mem.io_calls = 7
        seen = []
        t = threading.Thread(target=lambda: seen.append(mem.io_calls))
        t.start()
        t.join(2.0)
        self.assertEqual(seen, [0])
        self.assertEqual(mem.io_calls, 7)

    def test_another_thread_cannot_inflate_this_one(self):
        mem = self._mem()
        mem.io_calls = 0

        def background():
            for _ in range(500):
                mem.io_calls += 1

        t = threading.Thread(target=background)
        t.start()
        for _ in range(10):
            mem.io_calls += 1
        t.join(2.0)
        self.assertEqual(mem.io_calls, 10)

    def test_resetting_one_thread_leaves_the_other_alone(self):
        mem = self._mem()
        mem.io_calls = 4
        done = threading.Event()
        other = {}

        def background():
            mem.io_calls = 99
            mem.io_calls = 0          # what _tick does each pass
            other['after'] = mem.io_calls
            done.set()

        threading.Thread(target=background).start()
        self.assertTrue(done.wait(2.0))
        self.assertEqual(other['after'], 0)
        self.assertEqual(mem.io_calls, 4)

    def test_the_wait_total_is_thread_local_too(self):
        mem = self._mem()
        mem.io_wait_s = 1.5
        seen = []
        t = threading.Thread(target=lambda: seen.append(mem.io_wait_s))
        t.start()
        t.join(2.0)
        self.assertEqual(seen, [0.0])
        self.assertEqual(mem.io_wait_s, 1.5)

    def test_both_counters_read_before_any_write(self):
        # _wait's finally-block does `self.io_calls += 1`, which reads first.
        mem = self._mem()
        self.assertEqual(mem.io_calls, 0)
        self.assertEqual(mem.io_wait_s, 0.0)
        mem.io_calls += 1
        mem.io_wait_s += 0.25
        self.assertEqual(mem.io_calls, 1)
        self.assertAlmostEqual(mem.io_wait_s, 0.25)



class _HandleTable:
    """A fake IL2CPP handle table backing both resolvers identically."""

    PAGE = 0x2000

    def __init__(self, type_flag=2):
        self.mem = {}
        self.type_flag = type_flag
        self.batches = 0
        self.calls = 0

    def add(self, table_base, slot_index, target, bitmap_set=True,
            table_size=64, type_flag=None):
        flag = self.type_flag if type_flag is None else type_flag
        bitmap_ptr = table_base + 0x1000
        self.mem[table_base + 16] = bitmap_ptr
        self.mem[table_base + 28] = table_size
        self.mem[table_base + 32] = flag
        word_addr = bitmap_ptr + 4 * (slot_index >> 5)
        word = self.mem.get(word_addr, 0)
        if bitmap_set:
            word |= 1 << (slot_index & 0x1F)
        self.mem[word_addr] = word
        stored = target if flag > 1 else (~target & 0xFFFFFFFF)
        self.mem[table_base + 8 * (slot_index + 5)] = stored
        return table_base + 40 + slot_index * 8

    # -- Mem surface ------------------------------------------------------
    def batch_u64(self, addrs, attempts=3, **kwargs):
        self.batches += 1
        self.calls += len(addrs)
        return [self.mem.get(a, 0) for a in addrs]


class BatchedHandleResolutionTests(unittest.TestCase):
    """`resolve_tagged_handles` must match `resolve_tagged_handle` exactly.

    The batched form exists because the single form was called inside a
    per-player loop (two IOCTLs each -> `held=41io`, `disp=1190ms`). It is only
    safe if it is a pure re-shaping of the same algorithm, so every test below
    compares the two directly rather than asserting an expected value.
    """

    BASE = 0x7FF000000000

    def _table(self, count, **kw):
        t = _HandleTable(**kw)
        handles = []
        targets = []
        # The inverted encoding (type_flag <= 1) only round-trips the low 32
        # bits, so a target for that path has to be 32-bit representable --
        # that is a property of the format, not of the fake.
        wide = kw.get('type_flag', 2) > 1
        for i in range(count):
            target = (0x7FE000000000 + i * 0x100) if wide else (0x00A00000 + i * 0x100)
            handles.append(t.add(self.BASE + i * t.PAGE, 3 + i, target))
            targets.append(target)
        return t, handles, targets

    def test_it_agrees_with_the_single_resolver(self):
        t, handles, targets = self._table(6)
        batched = legacy.resolve_tagged_handles(t, handles)
        for handle, target in zip(handles, targets):
            single = legacy.resolve_tagged_handle(t, handle)
            self.assertEqual(single, target)
            self.assertEqual(batched.get(handle, 0), single)

    def test_it_agrees_on_the_inverted_encoding(self):
        # type_flag <= 1 stores the pointer inverted over the low 32 bits only.
        t, handles, targets = self._table(4, type_flag=1)
        batched = legacy.resolve_tagged_handles(t, handles)
        for handle, target in zip(handles, targets):
            single = legacy.resolve_tagged_handle(t, handle)
            self.assertEqual(single, target)
            self.assertEqual(batched.get(handle, 0), single)

    def test_twenty_handles_cost_two_calls_not_forty(self):
        t, handles, _ = self._table(20)
        t.batches = 0
        legacy.resolve_tagged_handles(t, handles)
        self.assertEqual(t.batches, 2)

        t.batches = 0
        for handle in handles:
            legacy.resolve_tagged_handle(t, handle)
        self.assertEqual(t.batches, 40)      # what it used to cost

    def test_a_clear_bitmap_bit_is_rejected_by_both(self):
        t = _HandleTable()
        handle = t.add(self.BASE, 5, 0x7FE000000000, bitmap_set=False)
        self.assertEqual(legacy.resolve_tagged_handle(t, handle), 0)
        self.assertNotIn(handle, legacy.resolve_tagged_handles(t, [handle]))

    def test_a_slot_past_the_table_is_rejected_by_both(self):
        t = _HandleTable()
        handle = t.add(self.BASE, 40, 0x7FE000000000, table_size=8)
        self.assertEqual(legacy.resolve_tagged_handle(t, handle), 0)
        self.assertNotIn(handle, legacy.resolve_tagged_handles(t, [handle]))

    def test_a_bad_type_flag_is_rejected_by_both(self):
        t = _HandleTable()
        handle = t.add(self.BASE, 5, 0x7FE000000000, type_flag=4)
        self.assertEqual(legacy.resolve_tagged_handle(t, handle), 0)
        self.assertNotIn(handle, legacy.resolve_tagged_handles(t, [handle]))

    def test_a_misaligned_handle_is_rejected_by_both(self):
        t = _HandleTable()
        good = t.add(self.BASE, 5, 0x7FE000000000)
        bad = good + 3                       # not 8-byte aligned
        self.assertEqual(legacy.resolve_tagged_handle(t, bad), 0)
        self.assertNotIn(bad, legacy.resolve_tagged_handles(t, [bad]))

    def test_junk_handles_are_dropped_without_a_read(self):
        t = _HandleTable()
        self.assertEqual(legacy.resolve_tagged_handles(t, [0, 5, None, -1]), {})
        self.assertEqual(t.batches, 0)

    def test_one_bad_handle_does_not_lose_the_good_ones(self):
        # The per-player loop tolerated a single failure; the batch must too.
        t, handles, targets = self._table(4)
        broken = t.add(self.BASE + 90 * t.PAGE, 6, 0x7FE0FFFF0000,
                       bitmap_set=False)
        out = legacy.resolve_tagged_handles(t, handles + [broken])
        self.assertNotIn(broken, out)
        for handle, target in zip(handles, targets):
            self.assertEqual(out[handle], target)

    def test_duplicate_handles_are_read_once(self):
        t, handles, targets = self._table(3)
        t.calls = 0
        out = legacy.resolve_tagged_handles(t, handles + handles + handles)
        # 3 metadata addresses + 2 slot addresses per distinct handle
        self.assertEqual(t.calls, 3 * 3 + 3 * 2)
        self.assertEqual(len(out), 3)

    def test_an_empty_input_reads_nothing(self):
        t = _HandleTable()
        self.assertEqual(legacy.resolve_tagged_handles(t, []), {})
        self.assertEqual(t.batches, 0)

    def test_the_held_scan_no_longer_resolves_in_a_loop(self):
        import inspect
        src = inspect.getsource(legacy.RustGame._read_held_items_batch)
        self.assertIn("resolve_tagged_handles(", src)
        self.assertNotIn("resolve_tagged_handle(m,", src)



class _RigMem:
    """Backs _sample_bones_multi with a synthetic TRS array + parent table."""

    LOCAL = 0x7FF000000000
    PARENT = 0x7FF000100000

    def __init__(self, parents, slots):
        # parents: [parent_index, ...]; slots: {index: (t, quat, s)}
        self.parents = parents
        self.slots = slots
        self.words = {}
        for index, (t, q, sc) in slots.items():
            raw = (struct.pack('<3f', *t) + b'\x00' * 4
                   + struct.pack('<4f', *q)
                   + struct.pack('<3f', *sc) + b'\x00' * 4)
            base = self.LOCAL + model_mod.TRSX_SIZE * index
            for off in range(0, len(raw) - 7, 8):
                self.words[base + off] = struct.unpack_from('<Q', raw, off)[0]
        packed = struct.pack('<%di' % len(parents), *parents)
        packed += b'\x00' * (-len(packed) % 8)
        for off in range(0, len(packed), 8):
            self.words[self.PARENT + off] = struct.unpack_from('<Q', packed, off)[0]

    def batch_u64(self, addrs, attempts=3, **kwargs):
        return [self.words.get(a, 0) for a in addrs]

    def read(self, addr, size):
        raise AssertionError("per-item read() must not be used here")


class QuaternionMemoisationTests(unittest.TestCase):
    """Normalising once per slot must not change a single bone position.

    A rig's spine and hip joints sit in almost every bone's ancestor chain, so
    normalising inside the composition loop re-did the same quaternion once per
    bone that walked through it (~4900 steps per tick at 39 players against
    ~860 distinct slots). Moving it to the slot build is only safe if the
    output is bit-for-bit the same.
    """

    def _model(self, mem, tick_ms=0.0):
        game = object.__new__(RustGameModel)
        game.m = mem
        game.tick_ms = tick_ms
        game._bone_debug = {}
        game._bone_last_outcome = {}
        game._parent_index_cache = {}
        return game

    def _rig(self):
        # 0 -> 1 -> 2 chain plus a sibling 3 under 1, so slot 1 is shared.
        half = math.sqrt(0.5)
        parents = [-1, 0, 1, 1]
        slots = {
            0: ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
            1: ((0.0, 1.0, 0.0), (0.0, 0.0, half, half), (1.0, 1.0, 1.0)),
            2: ((0.5, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
            3: ((0.0, 0.3, 0.2), (0.0, 0.0, 0.0, 1.0), (2.0, 2.0, 2.0)),
        }
        return parents, slots

    def _jobs(self, anchor=(0.0, 0.0, 0.0)):
        plan = {10: (0xAA, 2), 11: (0xAA, 3), 12: (0xAA, 1)}
        hierarchy_ptrs = {0xAA: (_RigMem.LOCAL, _RigMem.PARENT)}
        return [(1, plan, anchor, hierarchy_ptrs)]

    def test_composed_positions_match_hand_computed_values(self):
        """The real invariant: moving the normalise must not move a bone.

        Hand-computed for the rig in _rig(), applying the documented
        scale -> rotate -> translate per ancestor. Slot 1 carries a +90 deg
        rotation about Z, so (x, y) -> (-y, x).

          bone 10 = slot 2, chain [2, 1, 0]
            seed (0.5, 0, 0) -> rot90 -> (0, 0.5, 0) -> +(0,1,0) = (0, 1.5, 0)
          bone 11 = slot 3, chain [3, 1, 0]
            seed (0, 0.3, 0.2) -> rot90 -> (-0.3, 0, 0.2) -> +(0,1,0)
                                                          = (-0.3, 1, 0.2)
          bone 12 = slot 1, chain [1, 0]
            seed (0, 1, 0) through identity slot 0        = (0, 1, 0)

        Note bone 11 does NOT get slot 3's own scale of 2: the seed is the
        slot's own translation and only *ancestor* transforms are applied.
        """
        parents, slots = self._rig()
        model = self._model(_RigMem(parents, slots))
        out = model._sample_bones_multi(self._jobs())
        self.assertIn(1, out, "the harness produced no bones at all")
        bones = out[1]
        expected = {
            10: (0.0, 1.5, 0.0),
            11: (-0.3, 1.0, 0.2),
            12: (0.0, 1.0, 0.0),
        }
        self.assertEqual(set(bones), set(expected))
        for bone_id, want in expected.items():
            for got, exp in zip(bones[bone_id], want):
                self.assertAlmostEqual(got, exp, places=5,
                                       msg=f"bone {bone_id}")

    def test_each_distinct_slot_is_normalised_once(self):
        parents, slots = self._rig()
        real = model_mod._normalize_quaternion
        calls = []

        def counting(q):
            calls.append(tuple(q))
            return real(q)

        model_mod._normalize_quaternion = counting
        try:
            model = self._model(_RigMem(parents, slots))
            model._sample_bones_multi(self._jobs())
        finally:
            model_mod._normalize_quaternion = real

        # Exactly one normalise per distinct slot fetched -- not one per bone
        # that walks through it. Slot 1 is an ancestor of both bone 10 and
        # bone 11, so the old placement did 5 (3 seeds + slot 1 twice + slot 0
        # twice); this must stay at 4.
        #
        # Count calls, never distinct quaternion *values*: separate slots
        # legitimately hold the same rotation (three of this rig's four are
        # identity), so deduplicating by value would hide a real regression.
        self.assertEqual(len(calls), len(slots),
                         f"expected one normalise per slot, got {calls}")

    def test_an_unusable_rotation_still_rejects_the_bone(self):
        parents, slots = self._rig()
        slots[1] = ((0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
        model = self._model(_RigMem(parents, slots))
        out = model._sample_bones_multi(self._jobs())
        # bone 12 seeds on slot 1 itself and never walks through it, so only
        # the bones whose chain crosses the broken joint are dropped.
        self.assertNotIn(10, out.get(1, {}))
        self.assertNotIn(11, out.get(1, {}))
        self.assertGreater(model._bone_debug.get('rej_quat', 0), 0)

    def test_the_composition_loop_no_longer_normalises(self):
        import inspect
        src = inspect.getsource(RustGameModel._compose_bones)
        body = src.split("Stage 4")[1]
        self.assertNotIn("_normalize_quaternion", body)

    def test_the_tick_reads_the_skeleton_in_the_frame_batch(self):
        """One IOCTL per tick, not two.

        At ~15.5 ms of fixed driver latency per call (measured in-game
        2026-08-27, `slow=` 100%), the tick's call count *is* its cost --
        the whole of _compose_bones is 2.5 ms of Python against that. The
        TRS read can only join the frame batch because the hierarchy
        pointers it needs are cached topology, so this holds both halves:
        the planning runs against the cache, and the addresses are appended
        to the frame batch rather than issued separately.
        """
        import inspect
        src = inspect.getsource(RustGameModel._read_player_frame_batch)
        self.assertIn("_plan_bone_reads", src)
        self.assertIn("_hierarchy_ptr_cache", src)
        self.assertIn("addresses.extend(planned[2])", src)
        # ...and the tick must not fall back to the second transaction.
        self.assertNotIn("_sample_bones_multi", src)

    def test_a_rig_that_moved_is_dropped_not_composed(self):
        """The cached pointer is planned against; the fresh one is truth.

        Planning a tick ahead is only safe because the same batch re-reads
        the pointers it planned against. If they moved, the TRS words in
        that batch describe a rig that no longer exists.
        """
        import inspect
        src = inspect.getsource(RustGameModel._read_player_frame_batch)
        self.assertIn("stale", src)
        self.assertIn("planned_hier[hierarchy] != ptrs", src)



class StaticSoundnessTests(unittest.TestCase):
    """Nothing in the package may read a name that does not exist.

    Written after breaking the overlay twice in one session, both times the
    same way: an edit spliced out a range of a file and took neighbouring
    top-level definitions with it. Both times the whole unit suite stayed
    green, because a module-level constant that no test imports is invisible
    to tests -- and both times the failure surfaced as a NameError seconds
    into a real run.

    A unit test cannot cover every constant. This does not try: it asks the
    files themselves whether every name they *read* is a name they define,
    import, or receive as an argument. That is the exact class of damage,
    and it costs milliseconds.
    """

    PACKAGE = pathlib.Path(__file__).parent

    # Names Python provides to every module.
    MODULE_GLOBALS = {
        "__file__", "__name__", "__doc__", "__package__", "__spec__",
        "__loader__", "__builtins__", "__debug__",
    }

    def _undefined(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = set(dir(builtins)) | self.MODULE_GLOBALS
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(
                node.ctx, (ast.Store, ast.Del)
            ):
                defined.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    defined.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                defined.update(node.names)
        seen = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                seen.setdefault(node.id, node.lineno)
        return sorted(
            (line, name) for name, line in seen.items() if name not in defined
        )

    def test_no_module_reads_a_name_it_never_defines(self):
        modules = [
            "legacy_runtime.py", "model.py", "view.py", "view_base.py",
            "controller.py", "bench_driver.py", "bench_event_latency.py",
        ]
        problems = []
        for name in modules:
            path = self.PACKAGE / name
            if not path.exists():
                continue
            for line, undefined_name in self._undefined(path):
                problems.append(f"{name}:{line}: undefined name {undefined_name!r}")
        self.assertEqual(problems, [])

    def test_no_class_calls_a_method_it_does_not_have(self):
        """The other half of the same damage.

        A deleted module-level constant shows up as an undefined *name*; a
        deleted method shows up as an attribute, which the name check above
        cannot see. Both went missing from the same edit. This resolves
        every `self._foo(...)` call site against the real class, inheritance
        included, so `model.py` overriding half of `RustGame` is handled by
        asking Python rather than by parsing.
        """
        problems = []
        for module_name in ("legacy_runtime", "model", "view_base", "view",
                            "controller"):
            module = importlib.import_module(
                f"tools.rust_esp_mvc.{module_name}"
            )
            tree = ast.parse(
                pathlib.Path(module.__file__).read_text(encoding="utf-8")
            )
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                cls = getattr(module, node.name, None)
                if cls is None:
                    continue
                have = set(dir(cls))
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id == "self"
                        and sub.func.attr not in have
                    ):
                        problems.append(
                            f"{module_name}.py:{sub.lineno}: "
                            f"{node.name}.{sub.func.attr}() is called but "
                            f"not defined"
                        )
        self.assertEqual(sorted(set(problems)), [])

    def test_the_overlay_constants_the_window_needs_are_present(self):
        """The four that actually went missing, named so the loss is loud.

        _set_click_through and poll() are called before the first frame is
        ever drawn, so losing any of these is not a subtle degradation -- it
        is a crash during startup, every time.
        """
        for name in ("GWL_EXSTYLE", "WS_EX_LAYERED", "WS_EX_TRANSPARENT",
                     "VK_MENU_TOGGLE"):
            self.assertTrue(
                hasattr(view_base, name), f"view_base.{name} is missing"
            )
        self.assertEqual(view_base.GWL_EXSTYLE, -20)
        self.assertEqual(view_base.WS_EX_LAYERED, 0x00080000)
        self.assertEqual(view_base.WS_EX_TRANSPARENT, 0x00000020)


class SkeletonThicknessTests(unittest.TestCase):
    """A distant skeleton must not turn into a head.

    The previous scheme scaled line and head size on a *distance* curve with
    a fixed floor, and the floor is what broke it: at 65-114 m a player
    projects to roughly 12-21 px tall while the head circle stayed at its
    3.3 px radius. That is a 6.6 px blob on a 21 px body -- 31% of the
    player's height, rising past 50% further out. On screen, a field of
    distant players read as a row of heads.

    Sizing from the skeleton's own projected height fixes it by
    construction: screen height already carries distance, field of view and
    resolution together, so one ratio is correct at every range.
    """

    # Projected height of a 1.8 m player at 1080p, ~70 degrees vertical FOV.
    SPANS = {2: 700.0, 8: 175.0, 22: 63.0, 47: 30.0, 65: 21.0, 114: 12.0}

    def test_the_head_is_the_same_fraction_of_the_body_at_every_range(self):
        fractions = []
        for span in self.SPANS.values():
            _, head, _ = view_base._skeleton_thickness(span)
            fractions.append(2.0 * head / span)
        # Only the clamped extremes may differ; the unclamped middle must be
        # one constant fraction.
        middle = [
            2.0 * view_base._skeleton_thickness(self.SPANS[d])[1] / self.SPANS[d]
            for d in (22, 47, 65)
        ]
        for got in middle:
            self.assertAlmostEqual(got, middle[0], places=6)
        # ...and that fraction has to be head-sized, not blob-sized. A real
        # head is about 13% of a standing human's height.
        self.assertLess(middle[0], 0.20)
        self.assertGreater(middle[0], 0.08)

    def test_the_far_case_from_the_screenshot_is_no_longer_a_blob(self):
        """65 m, the distance the complaint was made at."""
        line, head, outline = view_base._skeleton_thickness(self.SPANS[65])
        self.assertLess(2.0 * head, 4.0)      # was 6.6 px
        self.assertLess(outline, 2.0)         # was 3.0 px of pure halo
        self.assertGreaterEqual(head, view_base.SKELETON_HEAD_MIN_PX)

    def test_a_point_blank_skeleton_is_capped(self):
        """Scaling with screen height has to be bounded at the near end.

        A player two metres away projects ~700 px tall; the same ratio
        unclamped would draw a 44 px head.
        """
        line, head, _ = view_base._skeleton_thickness(self.SPANS[2])
        self.assertEqual(head, view_base.SKELETON_HEAD_MAX_PX)
        self.assertEqual(line, view_base.SKELETON_LINE_MAX_PX)

    def test_the_halo_never_outgrows_the_line_it_outlines(self):
        """The old outline was a *separate* 3.0 px against a 1.25 px line.

        At distance the halo was the only thing with any width left, which
        is what turned a far skeleton into a solid dark smudge.
        """
        for span in self.SPANS.values():
            line, _, outline = view_base._skeleton_thickness(span)
            self.assertGreater(outline, line)
            self.assertLess(outline - line, 1.5)

    def test_an_unprojectable_skeleton_falls_back_to_the_floor(self):
        line, head, _ = view_base._skeleton_thickness(0.0)
        self.assertEqual(line, view_base.SKELETON_LINE_MIN_PX)
        self.assertEqual(head, view_base.SKELETON_HEAD_MIN_PX)

    def test_thickness_grows_with_the_skeleton(self):
        spans = sorted(self.SPANS.values())
        heads = [view_base._skeleton_thickness(sp)[1] for sp in spans]
        self.assertEqual(heads, sorted(heads))


class _CountingDrawList:
    """Records what was drawn instead of drawing it."""

    def __init__(self):
        self.lines = []
        self.circles = []

    def add_line(self, first, second, color, width):
        self.lines.append((first, second, color, width))

    def add_circle_filled(self, center, radius, color, segments):
        self.circles.append((center, radius, color, segments))


class FarSkeletonCollapseTests(unittest.TestCase):
    """Past a point, a skeleton has to stop being a skeleton.

    Sizing from screen height fixed the head (see SkeletonThicknessTests),
    but the 2026-08-27 recording -- made *with* that fix -- still showed a
    solid magenta smear at 60-85 m. The reason is not width, it is count:
    twenty links inside a ~10 px-wide silhouette touch each other whatever
    they are drawn at. So below SKELETON_DETAIL_MIN_SPAN_PX only the spine
    is drawn.
    """

    def _bones(self, span):
        """A rig where every linked joint exists and the height is `span`.

        Hips at the bottom, head at the top, everything else halfway, so
        the projected extent is exactly `span` and no link is long enough
        to trip the 500 px sanity cut.
        """
        ids = {a for a, _ in view_base.SKELETON_LINKS}
        ids |= {b for _, b in view_base.SKELETON_LINKS}
        bones = {}
        for bone_id in ids:
            if bone_id == 0:
                y = span
            elif bone_id == 53:
                y = 0.0
            else:
                y = span / 2.0
            bones[bone_id] = (float(bone_id), y, 0.0)
        return bones

    def _draw(self, span):
        draw_list = _CountingDrawList()
        real = view_base.world_to_screen
        view_base.world_to_screen = lambda world, *a, **k: (world[0], world[1])
        try:
            view_base._draw_player_skeleton(
                draw_list, self._bones(span), (1,) * 16, 1920, 1080
            )
        finally:
            view_base.world_to_screen = real
        # every link draws its halo and then its line
        return len(draw_list.lines) // 2, draw_list

    def test_a_near_skeleton_still_draws_every_link(self):
        drawn, _ = self._draw(200.0)
        self.assertEqual(drawn, len(view_base.SKELETON_LINKS))

    def test_a_far_skeleton_draws_only_the_spine(self):
        drawn, _ = self._draw(20.0)
        self.assertEqual(drawn, len(view_base.SKELETON_SPINE_LINKS))
        self.assertLess(drawn, len(view_base.SKELETON_LINKS) / 2)

    def test_the_head_marker_survives_the_collapse(self):
        """Whatever else goes, the player must stay findable."""
        _, draw_list = self._draw(20.0)
        self.assertEqual(len(draw_list.circles), 2)  # halo + fill

    def test_the_spine_is_a_real_subset_that_reaches_from_hips_to_head(self):
        links = set(view_base.SKELETON_LINKS)
        for link in view_base.SKELETON_SPINE_LINKS:
            self.assertIn(link, links)
        joints = {a for a, _ in view_base.SKELETON_SPINE_LINKS}
        joints |= {b for _, b in view_base.SKELETON_SPINE_LINKS}
        self.assertIn(0, joints)    # hips
        self.assertIn(53, joints)   # head

    def test_the_switch_happens_at_the_documented_span(self):
        below, _ = self._draw(view_base.SKELETON_DETAIL_MIN_SPAN_PX - 0.5)
        at, _ = self._draw(view_base.SKELETON_DETAIL_MIN_SPAN_PX)
        self.assertEqual(below, len(view_base.SKELETON_SPINE_LINKS))
        self.assertEqual(at, len(view_base.SKELETON_LINKS))


class HealthProbeTests(unittest.TestCase):
    """The [HEALTH-RAW] dump runs on the tick thread, so it must not raise.

    Two overlay crashes this session were AttributeErrors on the render or
    tick path that no test covered, so a new diagnostic gets its unhappy
    paths pinned before it ever runs in-game: no local player, no
    positions, a subject that vanished between ticks, a short read.
    """

    def _model(self, bp_cache=None):
        model = object.__new__(RustGameModel)
        model._health_probe_pm = None
        model._health_probe_who = "?"
        model._health_probe_last = {}
        model._next_health_probe_at = 0.0
        model._pm_bp_cache = dict(bp_cache or {})
        return model

    def _words(self, floats):
        """Pack floats into the u64 words the batch would have returned."""
        raw = struct.pack("<%df" % len(floats), *floats)
        words = struct.unpack("<%dQ" % (len(raw) // 8), raw)
        return {("hraw", i): w for i, w in enumerate(words)}

    def test_no_local_player_is_not_a_crash(self):
        model = self._model()
        model._probe_health_window({}, {}, None)
        self.assertIsNone(model._health_probe_pm)

    def test_the_local_player_is_the_preferred_subject(self):
        """Only a subject whose health can be made to change proves anything.

        The first run watched a bot for 40 seconds and nothing moved, which
        was uninformative: [HEALTH-DBG] damaged=0 across ~48 players says
        nothing on that server is ever damaged at tick time. The local
        player can take a fall and the game's own HUD says what the number
        should be.
        """
        model = self._model({1: 0x7FF000000000})
        positions = {1: (0.0, 0.0, 0.0), 3: (4.0, 0.0, 0.0)}
        model._probe_health_window({}, positions, 1)
        self.assertEqual(model._health_probe_pm, 1)
        self.assertEqual(model._health_probe_who, "local")

    def test_the_nearest_player_is_the_fallback(self):
        """Before pm->bp resolves for the local player there is no window."""
        model = self._model()
        positions = {
            1: (0.0, 0.0, 0.0),      # the local player, no bp yet
            2: (30.0, 0.0, 0.0),
            3: (4.0, 0.0, 0.0),
            4: (100.0, 0.0, 0.0),
        }
        model._probe_health_window({}, positions, 1)
        self.assertEqual(model._health_probe_pm, 3)
        self.assertEqual(model._health_probe_who, "near")

    def test_the_local_player_is_never_the_fallback_subject(self):
        model = self._model()
        model._probe_health_window({}, {1: (0.0, 0.0, 0.0)}, 1)
        self.assertIsNone(model._health_probe_pm)

    def test_a_subject_that_vanished_prints_nothing_and_survives(self):
        model = self._model()
        model._health_probe_pm = 9
        model._probe_health_window({}, {1: (0.0, 0.0, 0.0)}, 1)
        self.assertIsNone(model._health_probe_pm)

    def test_a_short_read_does_not_consume_the_interval(self):
        """A partial batch must not silence the probe until the next beat."""
        model = self._model()
        model._health_probe_pm = 7
        fields = self._words([0.0] * 4)   # far fewer words than asked for
        model._probe_health_window({7: fields}, {}, None)
        self.assertEqual(model._next_health_probe_at, 0.0)

    def test_a_field_that_moves_is_printed_the_tick_it_moves(self):
        """The whole point. A 1 Hz timer could not see a dip this short.

        Health is restored in well under a second on a practice server, so
        two full runs came back with every float identical -- which proves
        nothing about the offset. The window is read every tick anyway.
        """
        model = self._model()
        model._health_probe_pm = 7
        full = [100.0] * (model_mod.HEALTH_PROBE_WORDS * 2)
        hurt = list(full)
        hurt[(legacy.OFF._health - legacy.OFF.lifestate
              + model_mod.HEALTH_PROBE_BACK) // 4] = 62.0

        def probe(words):
            # the subject is re-chosen every tick; with no positions the
            # chooser clears it, so restore it the way a real tick would
            model._health_probe_pm = 7
            model._probe_health_window({7: self._words(words)}, {}, None)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            probe(full)
            model._next_health_probe_at = time.perf_counter() + 3600.0
            # nothing moved: silent, even though the heartbeat is far away
            probe(full)
            quiet = buffer.getvalue()
            probe(hurt)
        printed = buffer.getvalue()

        self.assertEqual(quiet.count("[HEALTH-RAW]"), 1)
        self.assertEqual(printed.count("[HEALTH-RAW]"), 2)
        self.assertIn("CHANGED %03x" % legacy.OFF._health, printed)
        self.assertIn("62.0", printed)

    def test_the_dump_names_the_offset_currently_believed_to_be_health(self):
        model = self._model()
        model._health_probe_pm = 7
        floats = [1.0] * (model_mod.HEALTH_PROBE_WORDS * 2)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            model._probe_health_window({7: self._words(floats)}, {}, None)
        line = buffer.getvalue()
        self.assertIn("[HEALTH-RAW]", line)
        self.assertIn("pm=0x7", line)
        # the current guess is marked so it can be judged against its
        # neighbours rather than trusted
        self.assertIn("*%03x=" % legacy.OFF._health, line)
        self.assertIn("^%03x=" % legacy.OFF._maxHealth, line)

    def test_the_window_actually_contains_the_offset_it_is_probing(self):
        first = legacy.OFF.lifestate - model_mod.HEALTH_PROBE_BACK
        last = first + model_mod.HEALTH_PROBE_WORDS * 8
        self.assertLessEqual(first, legacy.OFF._health)
        self.assertLess(legacy.OFF._health, last)
        self.assertLess(legacy.OFF._maxHealth, last)


class InterpolationOnlyTests(unittest.TestCase):
    """Nothing drawn may be past the newest sample. Ever.

    Measured in game 2026-08-27: the extrapolator ran +0.11..+0.23 m ahead
    of the player in every one-second window, entirely along the direction
    of travel, with single samples reaching +1.57 m and -1.83 m on direction
    changes -- while [RIG-LAG] showed the game draws the model exactly where
    the transform we anchor to says, so there was no latency to hide in the
    first place. These pin the replacement: one lerp, one instant, no
    invention.
    """

    POSE_PREV = {53: (0.0, 1.70, 0.0), 47: (0.20, 1.10, 0.0)}
    POSE_CUR = {53: (0.6, 1.74, 0.0), 47: (0.74, 1.16, 0.0)}

    def _model(self, cur_pos=(0.6, 0.0, 0.0), cur_pose=None, dt=0.05,
               age=None):
        game = object.__new__(RustGame)
        game._lock = threading.Lock()
        game._render_motion_cache = {}
        game.diag = ""
        game.tick_ms = 0.0
        now = time.perf_counter()
        cur_ts = now - (dt * 0.4 if age is None else age)
        game._snap_prev = (cur_ts - dt, [{
            "pm": 1, "pos": (0.0, 0.0, 0.0), "bones": dict(self.POSE_PREV),
            "sleeping": False, "vel": (12.0, 0.0, 0.0),
        }], None, [])
        game._snap_cur = (cur_ts, [{
            "pm": 1, "pos": cur_pos,
            "bones": dict(self.POSE_CUR if cur_pose is None else cur_pose),
            "sleeping": False, "vel": (12.0, 0.0, 0.0),
        }], None, [])
        return game

    def _alpha_of(self, player, cur_x=0.6):
        """Recover the lerp weight from the drawn body position.

        Deriving it from the output rather than from the clock is what makes
        the bone assertions exact: whatever fraction the body moved, the
        bones must have moved the same one.
        """
        return player["pos"][0] / cur_x

    def test_the_drawn_body_lies_between_the_two_samples(self):
        player = self._model().get_snapshot()[0][0]
        self.assertGreaterEqual(player["pos"][0], 0.0)
        self.assertLessEqual(player["pos"][0], 0.6)

    def test_the_drawn_skeleton_is_a_lerp_of_the_two_measured_ones(self):
        player = self._model().get_snapshot()[0][0]
        a = self._alpha_of(player)
        for bone_id, was in self.POSE_PREV.items():
            now_pos = self.POSE_CUR[bone_id]
            expected = tuple(
                w * (1.0 - a) + n * a for w, n in zip(was, now_pos)
            )
            for got, want in zip(player["bones"][bone_id], expected):
                self.assertAlmostEqual(got, want, places=5)

    def test_a_late_frame_never_runs_past_the_newest_sample(self):
        """The overlay renders far faster than the worker ticks.

        When a tick runs long, `now - cur_ts` grows past the interval and
        alpha saturates. Saturating must mean "sit on the newest sample",
        never "keep going".
        """
        player = self._model(age=0.30).get_snapshot()[0][0]
        self.assertAlmostEqual(player["pos"][0], 0.6, places=5)
        for bone_id, expected in self.POSE_CUR.items():
            for got, want in zip(player["bones"][bone_id], expected):
                self.assertAlmostEqual(got, want, places=5)

    def test_an_early_frame_never_runs_behind_the_older_sample(self):
        player = self._model(age=0.0).get_snapshot()[0][0]
        self.assertGreaterEqual(player["pos"][0], 0.0)

    def test_the_reported_lag_is_negative_and_is_one_interval(self):
        """`prediction_ms` is now a lag, so its sign has to say so."""
        dt = 0.05
        player = self._model(dt=dt).get_snapshot()[0][0]
        self.assertLess(player["prediction_ms"], 0.0)
        self.assertAlmostEqual(player["prediction_ms"], -dt * 1000.0, places=3)

    def test_a_teleport_snaps_instead_of_sliding_across_the_gap(self):
        """Above POS_INTERP_MAX_STEP, lerping is worse than not lerping.

        The position gate upstream has already decided to believe the jump.
        Interpolating one would drag a body over hundreds of metres of open
        ground for a whole tick.
        """
        far = legacy.POS_INTERP_MAX_STEP + 50.0
        player = self._model(cur_pos=(far, 0.0, 0.0)).get_snapshot()[0][0]
        self.assertAlmostEqual(player["pos"][0], far, places=5)
        self.assertAlmostEqual(player["bone_shift"][0], 0.0, places=5)

    def test_a_skeleton_that_refuses_the_blend_still_never_leads(self):
        """The raw fallback keeps the newest pose, at the drawn position.

        It fires when a whole pose changed too much to blend -- roughly 0-7%
        of skeleton-frames. It must not become a back door for the thing
        that was just removed: the shift it applies points *backwards*, from
        the newest sample to the rendered instant.
        """
        wild = {53: (0.6, 1.74, 0.0), 47: (3.5, 1.16, 0.0)}
        player = self._model(cur_pose=wild).get_snapshot()[0][0]
        shift = player["bone_shift"]
        self.assertLessEqual(shift[0], 1e-9)
        for bone_id, sampled in wild.items():
            for got, want in zip(player["bones"][bone_id], sampled):
                self.assertLessEqual(got, want + 1e-9)

    def test_a_standing_player_is_drawn_exactly_where_it_was_sampled(self):
        player = self._model(
            cur_pos=(0.0, 0.0, 0.0), cur_pose=self.POSE_PREV
        ).get_snapshot()[0][0]
        for component in player["bone_shift"]:
            self.assertAlmostEqual(component, 0.0, places=6)
        for bone_id, expected in self.POSE_PREV.items():
            for got, want in zip(player["bones"][bone_id], expected):
                self.assertAlmostEqual(got, want, places=6)

    def test_no_code_path_still_multiplies_velocity_by_a_horizon(self):
        """A static check, because this bug can come back as one line.

        The prediction pass must not contain a `velocity * <time>` term for
        the body any more. It is allowed to compute velocity -- [PRED-ERR]
        and [RIG-LAG] read it -- but not to travel on it.
        """
        src = inspect.getsource(RustGame._predict_from_snapshots)
        self.assertNotIn("velocity[0] * prediction_time", src)
        self.assertNotIn("MAX_PLAYER_PREDICTION", src)
        self.assertIn("interpolated_pos", src)


class PredictionErrorTests(unittest.TestCase):
    """The two numbers that separate the two ways the anchor can be wrong.

    The 2026-08-27 recording showed whole skeletons sitting ahead of moving
    bodies while static ones sat exactly on theirs, and [BONE-STRETCH]
    never fired -- so the rig's shape is right and its anchor is not.
    Either the predictor overshoots the transform it aims at ([PRED-ERR])
    or the game draws the model behind that transform ([RIG-LAG]). Those
    have opposite fixes, so the arithmetic that tells them apart is worth
    pinning down.
    """

    def _game(self):
        game = object.__new__(legacy.RustGame)
        game._reset_prediction_error_window()
        game._next_pred_err_log_at = time.perf_counter() + 3600.0
        return game

    def test_predicting_past_the_player_reads_as_positive_overshoot(self):
        game = self._game()
        prior = {
            "pos": (0.0, 0.0, 0.0),
            "velocity": (10.0, 0.0, 0.0),
            "snapshot_ts": 0.0,
            "lead": 0.0,
        }
        # 0.1 s at 10 m/s predicts x=1.0; the player only reached 0.5.
        game._track_prediction_error(prior, (0.5, 0.0, 0.0), 0.1,
                                     (10.0, 0.0, 0.0), None)
        self.assertEqual(game._pred_err_n, 1)
        self.assertAlmostEqual(game._pred_err_overshoot_sum, 0.5, places=6)

    def test_falling_short_reads_as_negative_overshoot(self):
        game = self._game()
        prior = {
            "pos": (0.0, 0.0, 0.0),
            "velocity": (10.0, 0.0, 0.0),
            "snapshot_ts": 0.0,
            "lead": 0.0,
        }
        game._track_prediction_error(prior, (1.4, 0.0, 0.0), 0.1,
                                     (10.0, 0.0, 0.0), None)
        self.assertAlmostEqual(game._pred_err_overshoot_sum, -0.4, places=6)

    def test_the_lead_that_was_applied_is_the_lead_that_is_scored(self):
        """A residual computed without the lead would exonerate the lead."""
        game = self._game()
        prior = {
            "pos": (0.0, 0.0, 0.0),
            "velocity": (10.0, 0.0, 0.0),
            "snapshot_ts": 0.0,
            "lead": 0.05,
        }
        game._track_prediction_error(prior, (0.5, 0.0, 0.0), 0.1,
                                     (10.0, 0.0, 0.0), None)
        # (0.1 + 0.05) * 10 = 1.5 predicted against 0.5 reached
        self.assertAlmostEqual(game._pred_err_overshoot_sum, 1.0, places=6)

    def test_a_rig_trailing_the_anchor_reads_as_positive_lag(self):
        game = self._game()
        game._track_prediction_error(
            None, (10.0, 0.0, 0.0), 1.0, (5.0, 0.0, 0.0),
            {0: (9.7, 1.0, 0.0)},
        )
        self.assertEqual(game._rig_lag_n, 1)
        self.assertAlmostEqual(game._rig_lag_sum, 0.3, places=6)

    def test_a_rig_ahead_of_the_anchor_reads_as_negative_lag(self):
        game = self._game()
        game._track_prediction_error(
            None, (10.0, 0.0, 0.0), 1.0, (5.0, 0.0, 0.0),
            {0: (10.4, 1.0, 0.0)},
        )
        self.assertAlmostEqual(game._rig_lag_sum, -0.4, places=6)

    def test_a_standing_player_only_feeds_the_baseline(self):
        """At rest the direction of travel is noise, so nothing else is safe.

        The at-rest bucket is not decoration: the hips have a constant
        offset from the entity origin, and the moving number means nothing
        until that constant is subtracted from it.
        """
        game = self._game()
        game._track_prediction_error(
            None, (10.0, 0.0, 0.0), 1.0, (0.0, 0.0, 0.0),
            {0: (10.04, 1.0, 0.0)},
        )
        self.assertEqual(game._rig_lag_n, 0)
        self.assertEqual(game._pred_err_n, 0)
        self.assertEqual(game._rig_rest_n, 1)
        self.assertAlmostEqual(game._rig_rest_sum, 0.04, places=6)

    def test_a_walking_player_is_counted_by_neither_bucket(self):
        game = self._game()
        game._track_prediction_error(
            None, (10.0, 0.0, 0.0), 1.0, (1.0, 0.0, 0.0),
            {0: (10.04, 1.0, 0.0)},
        )
        self.assertEqual(game._rig_lag_n, 0)
        self.assertEqual(game._rig_rest_n, 0)

    def test_vertical_motion_never_enters_the_measurement(self):
        """Crouch and jump are separately clamped; mixing them in is noise."""
        game = self._game()
        game._track_prediction_error(
            None, (10.0, 0.0, 0.0), 1.0, (5.0, 4.0, 0.0),
            {0: (9.7, 3.0, 0.0)},
        )
        self.assertAlmostEqual(game._rig_lag_sum, 0.3, places=6)
        self.assertAlmostEqual(game._rig_lag_speed_sum, 5.0, places=6)

    def test_a_stale_prior_is_not_scored(self):
        game = self._game()
        prior = {
            "pos": (0.0, 0.0, 0.0),
            "velocity": (10.0, 0.0, 0.0),
            "snapshot_ts": 0.0,
            "lead": 0.0,
        }
        game._track_prediction_error(prior, (0.5, 0.0, 0.0), 5.0,
                                     (10.0, 0.0, 0.0), None)
        self.assertEqual(game._pred_err_n, 0)


class RealConstructionTests(unittest.TestCase):
    """Drive the render path on an object that actually ran __init__.

    Every other test in this file builds its subject with `object.__new__`,
    which is fast and focused but has a blind spot that bit for real on
    2026-08-27: the instrumentation helpers are "self-healing", i.e. they
    lazily create their counters behind a `hasattr` guard. On an
    `object.__new__` object that guard always fires, so the lazy path is the
    only one ever tested. On a real object __init__ had already set *some*
    of those names, the guard did not fire, and the body reached for one
    nothing had created -- an AttributeError on the render thread, in-game,
    at the first skeleton.

    So this exercises the combination no other test did: real construction,
    real snapshots, the whole prediction pass.
    """

    class _NullMem:
        io_calls = 0
        io_wait_s = 0.0
        yield_wait_s = 0.0
        lock_wait_s = 0.0

        def batch_u64(self, addrs, attempts=1, skip_camera_yield=False):
            return [0] * len(addrs)

    def _model(self):
        return RustGameModel(self._NullMem(), 0x10000000)

    def _publish(self, model, prev_bones, cur_bones, dt=0.06):
        now = time.perf_counter()
        model._snap_prev = (now - dt * 2, [{
            "pm": 1, "pos": (0.0, 0.0, 0.0), "bones": dict(prev_bones),
            "sleeping": False, "vel": None,
        }], None)
        model._snap_cur = (now - dt, [{
            "pm": 1, "pos": (0.0, 0.0, 0.0), "bones": dict(cur_bones),
            "sleeping": False, "vel": None,
        }], None)

    def test_the_prediction_pass_runs_on_a_real_model(self):
        model = self._model()
        self._publish(
            model,
            prev_bones={29: (0.0, 1.4, 0.0), 30: (0.0, 1.0, 0.0)},
            cur_bones={29: (0.1, 1.4, 0.0), 30: (0.05, 1.0, 0.0)},
        )
        players = model.get_snapshot()[0]
        self.assertEqual(len(players[0]["bones"]), 2)

    def test_the_raw_fallback_runs_on_a_real_model(self):
        """The branch that actually crashed: a skeleton forced back to raw.

        It only runs when a bone fails the blend guard, which is why a happy
        path would not have caught it.
        """
        model = self._model()
        self._publish(
            model,
            prev_bones={29: (0.0, 1.4, 0.0), 30: (0.0, 1.0, 0.0)},
            cur_bones={29: (0.1, 1.4, 0.0), 30: (9.0, 1.0, 0.0)},
        )
        players = model.get_snapshot()[0]
        self.assertAlmostEqual(players[0]["bones"][30][0], 9.0, places=4)

    def test_every_lazily_created_counter_is_also_set_by_init(self):
        """The guard and __init__ must not be able to drift apart again.

        A self-healing helper whose guard names one attribute while its body
        writes another is invisible until a real object reaches it. Rather
        than trusting the two comments to stay in sync, check that a real
        model already owns everything the lazy path would have created.
        """
        model = self._model()
        lazily_created = (
            "_bone_phase_raw",
            "_bone_phase_total_skeletons",
            "_bone_phase_worst",
            "_last_bone_phase_log_at",
            "_pose_frames_total",
            "_pose_frames_saturated",
            "_pred_err_n",
            "_rig_lag_n",
            "_rig_rest_n",
            "_next_pred_err_log_at",
        )
        missing = [
            name for name in lazily_created if not hasattr(model, name)
        ]
        self.assertEqual(missing, [])


class MergedFrameBatchTests(unittest.TestCase):
    """The tick's one transaction, driven end to end against a fake driver.

    _read_player_frame_batch had no behavioural coverage at all before this
    -- only source inspection -- which is a poor trade for the hottest and
    most delicate method in the program. It now plans the skeleton reads
    against a cache *before* the batch is issued and slices their results
    back out afterwards, and neither the planning, the slicing nor the
    staleness check can be checked by reading the source.

    The fake driver serves a synthetic address space, so what is asserted is
    the real thing: how many round trips a tick costs, and whether the bones
    that come out are the ones that were written in.
    """

    PM = 0x100000
    HIER = 0x200000
    LOCAL_PTR = 0x300000
    PARENT_PTR = 0x400000
    POS = (100.0, 20.0, 300.0)

    class _FakeMem:
        def __init__(self, words):
            self.words = words
            self.batches = 0
            self.io_calls = 0
            self.io_wait_s = 0.0
            self.yield_wait_s = 0.0
            self.lock_wait_s = 0.0

        def batch_u64(self, addrs, attempts=1, skip_camera_yield=False):
            self.batches += 1
            self.io_calls += 1
            return [self.words.get(a, 0) for a in addrs]

    def _words(self, local_ptr=None):
        """A synthetic address space holding one player and one rig."""
        local_ptr = self.LOCAL_PTR if local_ptr is None else local_ptr
        words = {}

        def put_vec3(addr, vec):
            raw = struct.pack("<fff", *vec) + b"\x00" * 12
            lo, hi = struct.unpack_from("<QQ", raw)
            words[addr] = lo
            words[addr + 8] = hi

        put_vec3(self.PM + legacy.OFF.position_pm, self.POS)
        words[self.HIER + 0x18] = local_ptr
        words[self.HIER + 0x20] = self.PARENT_PTR

        # Every bone is its own root, so a chain is one slot deep and the
        # composed world position is just that slot's translation.
        capacity = BONE_MIN_VALID
        raw = struct.pack("<%di" % capacity, *([-1] * capacity))
        raw += b"\x00" * ((-len(raw)) % 8)
        for i in range(len(raw) // 8):
            words[self.PARENT_PTR + i * 8] = struct.unpack_from("<Q", raw, i * 8)[0]

        for slot in range(capacity):
            base = local_ptr + TRSX_SIZE * slot
            blob = (
                struct.pack("<fff", self.POS[0], self.POS[1] + slot * 0.05,
                            self.POS[2])
                + b"\x00" * 4
                + struct.pack("<ffff", 0.0, 0.0, 0.0, 1.0)      # identity quat
                + struct.pack("<fff", 1.0, 1.0, 1.0)
                + b"\x00" * 4
            )
            for i in range(TRSX_SIZE // 8):
                words[base + i * 8] = struct.unpack_from("<Q", blob, i * 8)[0]
        return words

    def _model(self, words):
        model = RustGameModel(self._FakeMem(words), 0x10000000)
        model.tick_ms = 60.0
        model.local_pos = self.POS
        model._last_local_pm = 0
        model._pm_pos_cache = {self.PM: self.POS}
        model._bone_slot_cache = {
            self.PM: {
                bone_id: (self.HIER, bone_id)
                for bone_id in range(BONE_MIN_VALID)
            }
        }
        model._next_pos_profile_at = time.perf_counter() + 3600.0
        model._next_bone_debug_at = time.perf_counter() + 3600.0
        # Not under test here, and both would reach for the driver.
        model._refresh_pm_to_bp_cache = lambda pm_ptrs: None
        model._resolve_bone_slots_batch = lambda mapping: None
        model._consume_rig_job = lambda pm, fields: None
        return model

    def test_a_steady_tick_costs_exactly_one_round_trip(self):
        """The whole point of the merge, measured rather than asserted.

        At ~15.5 ms of fixed driver latency (in-game, `slow=` 100%), the
        tick's cost *is* its round-trip count. Two ticks are needed to reach
        the steady state: the first learns the hierarchy pointers, the
        second reads the rig's parent-index array. Neither is per-frame
        work -- both are topology, cached for as long as the rig lives.
        """
        model = self._model(self._words())
        model._read_player_frame_batch([self.PM])   # learns the hierarchy
        model._read_player_frame_batch([self.PM])   # reads parent indices
        model.m.batches = 0
        for _ in range(5):
            model._read_player_frame_batch([self.PM])
        self.assertEqual(model.m.batches, 5)

    def test_the_parent_index_read_is_a_one_off_per_rig(self):
        """...and the second tick's extra call must not come back.

        Parent indices are rig topology with a 3 s cache. If this ever
        starts costing a call per tick it doubles the tick's driver cost
        again, silently -- [BONE-DBG] `pread=` is the same number in-game.
        """
        model = self._model(self._words())
        model._read_player_frame_batch([self.PM])
        model.m.batches = 0
        model._read_player_frame_batch([self.PM])
        self.assertEqual(model.m.batches, 2)        # frame batch + parents
        model.m.batches = 0
        model._read_player_frame_batch([self.PM])
        self.assertEqual(model.m.batches, 1)        # parents now cached

    def test_the_cold_tick_learns_the_rig_and_the_next_one_draws_it(self):
        """A hierarchy nobody has seen cannot be planned against.

        It costs one sample, not a wrong bone: the frame batch reads the
        pointers, and the next tick has them.
        """
        model = self._model(self._words())
        _, _, _, _, _, _, _, bones = model._read_player_frame_batch([self.PM])
        self.assertEqual(bones, {})
        self.assertIn(self.HIER, model._hierarchy_ptr_cache)

        _, _, _, _, _, _, _, bones = model._read_player_frame_batch([self.PM])
        self.assertEqual(len(bones.get(self.PM, {})), BONE_MIN_VALID)

    def test_the_bones_that_come_back_are_the_ones_written_in(self):
        """The slice has to line up, or every bone is quietly wrong.

        Reading the skeleton inside the frame batch means its results arrive
        offset by however many addresses the frame half used. An off-by-one
        there would still produce a plausible-looking skeleton.
        """
        model = self._model(self._words())
        model._read_player_frame_batch([self.PM])
        _, _, _, _, _, _, _, bones = model._read_player_frame_batch([self.PM])
        got = bones[self.PM]
        for bone_id in range(BONE_MIN_VALID):
            want = (self.POS[0], self.POS[1] + bone_id * 0.05, self.POS[2])
            for axis in range(3):
                self.assertAlmostEqual(got[bone_id][axis], want[axis], places=4)

    def test_a_rig_that_moved_between_plan_and_read_is_dropped(self):
        """The cached pointer is a prediction; the batch re-reads the truth.

        If they disagree, the TRS words in that same batch describe a rig
        that no longer exists -- composing from them would put bones at
        addresses belonging to something else entirely.
        """
        model = self._model(self._words())
        model._read_player_frame_batch([self.PM])
        # The rig moves: same hierarchy, new TRS array.
        model.m.words = self._words(local_ptr=0x500000)
        _, _, _, _, _, _, _, bones = model._read_player_frame_batch([self.PM])
        self.assertEqual(bones, {})
        self.assertEqual(model._bone_stale_rigs, 1)
        # ...and the cache has learned the new pointer, so the next tick works.
        _, _, _, _, _, _, _, bones = model._read_player_frame_batch([self.PM])
        self.assertEqual(len(bones.get(self.PM, {})), BONE_MIN_VALID)


class BoneShiftTests(unittest.TestCase):
    """The render-side prediction that moves the skeleton must be undoable.

    The bones are the pose the game is rendering -- the thing actually on
    screen -- while the shift applied to them comes from the *networked*
    position and its velocity. When the box sits ahead of a running player
    that shift is the first suspect, so it is recorded and reversible rather
    than baked in.
    """

    def _model(self, vel, bones, dt=0.05):
        model = object.__new__(RustGameModel)
        model._lock = threading.Lock()
        model._render_motion_cache = {}
        model.diag = ""
        model.tick_ms = 60.0
        now = time.perf_counter()
        player = {
            "pm": 1,
            "pos": (0.0, 0.0, 0.0),
            "bones": dict(bones),
            "sleeping": False,
            "vel": vel,
        }
        model._snap_prev = (now - dt * 2, [dict(player)], None)
        model._snap_cur = (now - dt, [player], None)
        return model

    def test_the_shift_is_recorded_alongside_the_bones(self):
        model = self._model((5.0, 0.0, 0.0), {47: (0.0, 1.7, 0.0)})
        player = model.get_snapshot()[0][0]
        self.assertIn("bone_shift", player)
        shift = player["bone_shift"]
        self.assertEqual(len(shift), 3)

    def test_undoing_the_shift_returns_the_sampled_pose(self):
        raw = {47: (0.0, 1.7, 0.0), 48: (0.3, 1.2, 0.1)}
        model = self._model((5.0, 0.0, 0.0), raw)
        player = model.get_snapshot()[0][0]
        shift = player["bone_shift"]
        for bone_id, sampled in raw.items():
            undone = tuple(
                got - off for got, off in zip(player["bones"][bone_id], shift)
            )
            for a, b in zip(undone, sampled):
                self.assertAlmostEqual(a, b, places=5)

    def test_a_standing_player_is_not_shifted(self):
        model = self._model((0.0, 0.0, 0.0), {47: (0.0, 1.7, 0.0)})
        player = model.get_snapshot()[0][0]
        for component in player["bone_shift"]:
            self.assertAlmostEqual(component, 0.0, places=5)

    def test_the_shift_never_reaches_past_the_samples_it_sits_between(self):
        """Under interpolation the shift is bounded by the data, not a cap.

        It is the distance from the newest sample back to the rendered
        instant, so it can never exceed the gap between the two samples --
        no constant needs to be trusted for that, it falls out of the lerp.
        """
        speed = 8.0
        raw = {47: (0.0, 1.7, 0.0)}
        model = object.__new__(RustGameModel)
        model._lock = threading.Lock()
        model._render_motion_cache = {}
        model.diag = ""
        model.tick_ms = 60.0
        now = time.perf_counter()
        dt = 0.05
        step = speed * dt
        model._snap_prev = (now - dt * 2, [{
            "pm": 1, "pos": (0.0, 0.0, 0.0), "bones": dict(raw),
            "sleeping": False, "vel": (speed, 0.0, 0.0),
        }], None)
        model._snap_cur = (now - dt, [{
            "pm": 1, "pos": (step, 0.0, 0.0), "bones": dict(raw),
            "sleeping": False, "vel": (speed, 0.0, 0.0),
        }], None)
        player = model.get_snapshot()[0][0]
        magnitude = math.sqrt(sum(c * c for c in player["bone_shift"]))
        self.assertLessEqual(magnitude, step + 1e-6)
        # and the drawn body is somewhere between the two samples, never past
        self.assertGreaterEqual(player["pos"][0], -1e-6)
        self.assertLessEqual(player["pos"][0], step + 1e-6)

    def test_there_is_no_extrapolation_toggle_left_to_turn_on(self):
        """The setting is gone, not defaulted off.

        It used to hide the extrapolation from the *bones only*, by
        subtracting the shift back off inside the view. That left the
        skeleton anchored to one instant and the box and position to
        another, which is a second bug wearing the first one's clothes.
        With the model interpolating, there is nothing to undo and nothing
        to switch.
        """
        self.assertNotIn("extrapolate_skeleton", vars(view_base.Settings()))

    def test_the_view_draws_the_bones_it_was_given(self):
        from tools.rust_esp_mvc import view as view_mod
        render_src = inspect.getsource(view_mod.OverlayView.render)
        self.assertNotIn("extrapolate_skeleton", render_src)
        self.assertNotIn("bone[0] - shift[0]", render_src)
        self.assertIn('player.get("bone_shift")', render_src)

    def test_the_applied_shift_is_reported(self):
        import inspect
        from tools.rust_esp_mvc import view as view_mod
        render_src = inspect.getsource(view_mod.OverlayView.render)
        self.assertIn("pred=", render_src)


class KlassNameDecodeTests(unittest.TestCase):
    """A failed klass-name read must yield "", not a buffer full of NULs.

    `raw.find(NUL)` returns 0 when the name pointer read back as zero, and the
    old `if terminator > 0` skipped the slice for exactly that case -- so the
    whole null-filled buffer became the klass name and was cached under that
    klass forever. It appeared verbatim in [WE-NAMES] as a 64-character
    garbage entry alongside 'BasePlayer' and 'TreeManager'.
    """

    @staticmethod
    def _decode(raw):
        terminator = raw.find(b'\x00')
        if terminator >= 0:
            raw = raw[:terminator]
        try:
            return raw.decode('ascii')
        except UnicodeDecodeError:
            return ''

    def test_an_all_null_buffer_decodes_to_empty(self):
        self.assertEqual(self._decode(b'\x00' * 64), "")

    def test_a_normal_name_survives(self):
        self.assertEqual(
            self._decode(b'BasePlayer' + b'\x00' * 54), "BasePlayer"
        )

    def test_a_name_filling_the_whole_buffer_survives(self):
        self.assertEqual(self._decode(b'A' * 64), 'A' * 64)

    def test_the_source_uses_the_inclusive_bound(self):
        import inspect
        src = inspect.getsource(legacy.RustGame)
        self.assertIn("if terminator >= 0:", src)
        self.assertNotIn("if terminator > 0:", src)



class RigRetryBackoffTests(unittest.TestCase):
    """A rig that can never resolve must stop hammering the admission queue.

    Some entries in the PlayerModel list have no usable skeleton -- a corpse,
    a bot on another model, a player still streaming in. `_fail_rig_job`
    retried every 0.35 s forever with no counter, so those pms sat in a
    permanent loop and consumed the six admission slots per tick that players
    who *could* resolve needed. That is the steady `norig=5..7` with `job=0`.
    """

    def _model(self):
        game = object.__new__(RustGameModel)
        game._bone_resolve_jobs = {}
        game._bone_resolve_retry_at = {}
        game._bone_resolve_fails = {}
        game._bone_slot_cache = {}
        game._bone_rig_identity = {}
        game._bone_position_cache = {}
        game._bone_sample_failures = {}
        game._bone_reject = {}
        return game

    def _delay(self, game, pm):
        return game._bone_resolve_retry_at[pm] - time.perf_counter()

    def test_the_first_failure_keeps_the_original_delay(self):
        game = self._model()
        game._fail_rig_job(7)
        self.assertAlmostEqual(self._delay(game, 7),
                               model_mod.BONE_RIG_RETRY_BASE, places=1)

    def test_repeated_failures_back_off(self):
        game = self._model()
        seen = []
        for _ in range(5):
            game._fail_rig_job(7)
            seen.append(round(self._delay(game, 7), 2))
        self.assertEqual(seen, sorted(seen))
        self.assertGreater(seen[-1], seen[0])

    def test_the_backoff_is_capped(self):
        game = self._model()
        for _ in range(40):
            game._fail_rig_job(7)
        self.assertLessEqual(self._delay(game, 7),
                             model_mod.BONE_RIG_RETRY_MAX + 0.1)

    def test_each_player_backs_off_independently(self):
        game = self._model()
        for _ in range(6):
            game._fail_rig_job(7)
        game._fail_rig_job(8)
        self.assertGreater(self._delay(game, 7), self._delay(game, 8))

    def test_an_explicit_delay_still_wins(self):
        # _invalidate_bone_rig asks for an immediate retry; the backoff must
        # not override a caller that named a delay.
        game = self._model()
        for _ in range(6):
            game._fail_rig_job(7)
        game._fail_rig_job(7, delay=0.0)
        self.assertLessEqual(self._delay(game, 7), 0.05)

    def test_success_clears_the_counter(self):
        import inspect
        src = inspect.getsource(RustGameModel._consume_rig_job)
        self.assertIn("self._bone_resolve_fails.pop(pm, None)", src)

    def test_a_cold_rig_is_visible_in_the_log(self):
        game = self._model()
        self.assertEqual(game._format_bone_rejects(), "")
        for _ in range(3):
            game._fail_rig_job(7)
        self.assertIn("cold=1", game._format_bone_rejects())

    def test_one_failure_is_not_reported_as_cold(self):
        # A player mid-resolution must not be labelled unresolvable.
        game = self._model()
        game._fail_rig_job(7)
        self.assertNotIn("cold", game._format_bone_rejects())

    def test_the_counter_is_pruned_with_the_roster(self):
        import inspect
        src = inspect.getsource(RustGameModel._resolve_bone_slots_batch)
        self.assertIn("_bone_resolve_fails", src)



class LimbExtrapolationTests(unittest.TestCase):
    """Bones must stay welded to the body AND keep animating between samples.

    Translating the whole skeleton by the body delta froze the pose between
    worker ticks: at a ~60ms tick and a 144Hz overlay that is nine frames of an
    unchanging skeleton sliding along, then a jump. Differencing each bone
    against its own previous sample fixes the animation but unwelds the
    skeleton, because a bone's raw delta is unsmoothed while the body's
    velocity is not. The shipped model splits the two: body delta for every
    bone, plus only the limb's motion *in the body's frame* on top.
    """

    def _model(self, prev_bones, cur_bones, prev_pos, cur_pos, dt=0.06):
        model = object.__new__(RustGameModel)
        model._lock = threading.Lock()
        model._render_motion_cache = {}
        model.diag = ""
        model.tick_ms = 60.0
        now = time.perf_counter()
        model._snap_prev = (now - dt * 2, [{
            "pm": 1, "pos": prev_pos, "bones": dict(prev_bones),
            "sleeping": False, "vel": None,
        }], None)
        model._snap_cur = (now - dt, [{
            "pm": 1, "pos": cur_pos, "bones": dict(cur_bones),
            "sleeping": False, "vel": None,
        }], None)
        return model

    def test_a_limb_moving_with_the_body_keeps_its_offset(self):
        # Whole player translates; no limb motion in the body frame. The
        # skeleton must stay exactly welded, as it did before.
        model = self._model(
            prev_bones={47: (0.0, 1.7, 0.0)},
            cur_bones={47: (0.3, 1.7, 0.0)},
            prev_pos=(0.0, 0.0, 0.0),
            cur_pos=(0.3, 0.0, 0.0),
        )
        player = model.get_snapshot()[0][0]
        offset = tuple(
            b - p for b, p in zip(player["bones"][47], player["pos"])
        )
        for got, want in zip(offset, (0.0, 1.7, 0.0)):
            self.assertAlmostEqual(got, want, places=5)

    def test_a_swinging_limb_lands_between_its_two_samples(self):
        """The pose is interpolated, never invented.

        Extrapolating made the residual grow across every rendered frame and
        snap back to zero when a new snapshot landed -- a visible hitch six
        times a second at a 60ms tick. Interpolation means every frame shows a
        pose bounded by two genuinely measured ones, so there is nothing to
        snap back from.
        """
        model = self._model(
            prev_bones={29: (0.0, 1.4, 0.0)},
            cur_bones={29: (0.4, 1.4, 0.0)},
            prev_pos=(0.0, 0.0, 0.0),
            cur_pos=(0.0, 0.0, 0.0),
        )
        x = model.get_snapshot()[0][0]["bones"][29][0]
        self.assertGreaterEqual(x, 0.0)
        self.assertLessEqual(x, 0.4)

    def test_the_pose_advances_as_the_snapshot_ages(self):
        # The whole point of fluidity: two different render times inside one
        # worker tick must produce two different poses.
        def pose_at(age):
            model = self._model(
                prev_bones={29: (0.0, 1.4, 0.0)},
                cur_bones={29: (0.4, 1.4, 0.0)},
                prev_pos=(0.0, 0.0, 0.0),
                cur_pos=(0.0, 0.0, 0.0),
            )
            now = time.perf_counter()
            dt = 0.06
            model._snap_prev = (now - dt - age, model._snap_prev[1], None)
            model._snap_cur = (now - age, model._snap_cur[1], None)
            return model.get_snapshot()[0][0]["bones"][29][0]

        early = pose_at(0.005)
        late = pose_at(0.05)
        self.assertGreater(late, early)

    def test_one_bad_bone_snaps_the_whole_skeleton_not_just_itself(self):
        """The "les os sortent du corps" bug, reproduced.

        [BONE-PHASE-MIX] measured it in-game on 2026-08-27: 1-2% of
        skeleton-frames came out with some bones interpolated and the rest
        raw -- worst case one bone blended against twenty held back by the
        guard. A blended bone sits at prev + (cur-prev)*alpha and a raw one
        at cur, so the skeleton is stitched from two different instants and
        the odd limb visibly detaches. Whatever the guard decides, it has to
        decide it for every bone at once.
        """
        model = self._model(
            # bone 29 moves a normal limb distance and would blend happily;
            # bone 30 jumped 9m, which the guard refuses.
            prev_bones={29: (0.0, 1.4, 0.0), 30: (0.0, 1.0, 0.0)},
            cur_bones={29: (0.4, 1.4, 0.0), 30: (9.0, 1.0, 0.0)},
            prev_pos=(0.0, 0.0, 0.0),
            cur_pos=(0.0, 0.0, 0.0),
        )
        bones = model.get_snapshot()[0][0]["bones"]
        # Both must be raw -- 29 gives up its interpolation so the pose stays
        # one single instant.
        self.assertAlmostEqual(bones[30][0], 9.0, places=5)
        self.assertAlmostEqual(bones[29][0], 0.4, places=5)

    def test_a_clean_skeleton_still_interpolates_every_bone(self):
        """...and the fix must not cost the smoothness when nothing is wrong."""
        model = self._model(
            prev_bones={29: (0.0, 1.4, 0.0), 30: (0.0, 1.0, 0.0)},
            cur_bones={29: (0.4, 1.4, 0.0), 30: (0.2, 1.0, 0.0)},
            prev_pos=(0.0, 0.0, 0.0),
            cur_pos=(0.0, 0.0, 0.0),
        )
        # Age the snapshot deliberately: the default helper puts `now` a full
        # dt_snap past cur, i.e. alpha == 1, where an interpolated pose and a
        # snapped one are the same number and the test would prove nothing.
        now = time.perf_counter()
        model._snap_prev = (now - 0.075, model._snap_prev[1], None)
        model._snap_cur = (now - 0.015, model._snap_cur[1], None)
        bones = model.get_snapshot()[0][0]["bones"]
        for bone_id, full in ((29, 0.4), (30, 0.2)):
            self.assertGreater(bones[bone_id][0], 0.0)
            self.assertLess(bones[bone_id][0], full)

    def test_a_bone_missing_from_the_previous_pose_snaps_the_skeleton(self):
        """A newly resolved bone has no previous offset to blend from.

        Interpolating its neighbours while it arrives raw is the same
        stitched-from-two-instants pose, so it counts as dissent too.
        """
        model = self._model(
            prev_bones={29: (0.0, 1.4, 0.0)},
            cur_bones={29: (0.4, 1.4, 0.0), 30: (0.2, 1.0, 0.0)},
            prev_pos=(0.0, 0.0, 0.0),
            cur_pos=(0.0, 0.0, 0.0),
        )
        bones = model.get_snapshot()[0][0]["bones"]
        self.assertAlmostEqual(bones[29][0], 0.4, places=5)

    def test_a_rig_swap_is_not_blended_across(self):
        # A pose that jumped metres between snapshots is a different skeleton,
        # not a movement; blending would smear it across the gap.
        model = self._model(
            prev_bones={29: (0.0, 1.4, 0.0)},
            cur_bones={29: (9.0, 1.4, 0.0)},
            prev_pos=(0.0, 0.0, 0.0),
            cur_pos=(0.0, 0.0, 0.0),
        )
        x = model.get_snapshot()[0][0]["bones"][29][0]
        self.assertAlmostEqual(x, 9.0, places=5)

    def test_an_absurd_limb_speed_falls_back_to_the_body(self):
        # A noisy sample must not fling a bone off; it gets the body delta,
        # which is always safe.
        model = self._model(
            prev_bones={29: (0.0, 1.4, 0.0)},
            cur_bones={29: (50.0, 1.4, 0.0)},
            prev_pos=(0.0, 0.0, 0.0),
            cur_pos=(0.0, 0.0, 0.0),
        )
        player = model.get_snapshot()[0][0]
        self.assertAlmostEqual(player["bones"][29][0], 50.0, places=5)

    def test_a_bone_the_previous_sample_lacked_still_gets_the_body_delta(self):
        model = self._model(
            prev_bones={},
            cur_bones={47: (0.3, 1.7, 0.0)},
            prev_pos=(0.0, 0.0, 0.0),
            cur_pos=(0.3, 0.0, 0.0),
        )
        player = model.get_snapshot()[0][0]
        offset = tuple(
            b - p for b, p in zip(player["bones"][47], player["pos"])
        )
        for got, want in zip(offset, (0.0, 1.7, 0.0)):
            self.assertAlmostEqual(got, want, places=5)

    def test_a_standing_player_is_not_moved_at_all(self):
        model = self._model(
            prev_bones={47: (0.0, 1.7, 0.0)},
            cur_bones={47: (0.0, 1.7, 0.0)},
            prev_pos=(0.0, 0.0, 0.0),
            cur_pos=(0.0, 0.0, 0.0),
        )
        player = model.get_snapshot()[0][0]
        for got, want in zip(player["bones"][47], (0.0, 1.7, 0.0)):
            self.assertAlmostEqual(got, want, places=5)

    def test_a_limb_residual_is_never_amplified(self):
        """The trembling regression.

        `prediction_time` (<=0.12s) over `dt_snap` (~0.06s) scaled the limb
        residual by up to 2.0, doubling every bit of sample noise on a value
        that has no smoothing at all. The body's velocity is smoothed and
        engine-backed and keeps its full prediction; the raw residual must
        never claim a limb kept moving longer than it was actually watched.
        """
        swing = 0.4
        model = self._model(
            prev_bones={29: (0.0, 1.4, 0.0)},
            cur_bones={29: (swing, 1.4, 0.0)},
            prev_pos=(0.0, 0.0, 0.0),
            cur_pos=(0.0, 0.0, 0.0),
        )
        player = model.get_snapshot()[0][0]
        # Under interpolation the pose can never pass its newest sample at
        # all, which is strictly stronger than the old "never two intervals".
        self.assertLessEqual(player["bones"][29][0], swing + 1e-6)

    def test_the_cap_is_one_observed_interval(self):
        self.assertLessEqual(legacy.BONE_LIMB_SCALE_MAX, 1.0)

    def test_the_body_still_gets_the_full_prediction(self):
        # The cap must apply to the limb residual only. A player translating
        # with no limb motion must still be predicted the full amount.
        model = self._model(
            prev_bones={47: (0.0, 1.7, 0.0)},
            cur_bones={47: (0.3, 1.7, 0.0)},
            prev_pos=(0.0, 0.0, 0.0),
            cur_pos=(0.3, 0.0, 0.0),
        )
        player = model.get_snapshot()[0][0]
        offset = tuple(
            b - p for b, p in zip(player["bones"][47], player["pos"])
        )
        for got, want in zip(offset, (0.0, 1.7, 0.0)):
            self.assertAlmostEqual(got, want, places=5)

    def test_the_limb_residual_excludes_the_body_motion(self):
        import inspect
        src = inspect.getsource(RustGameModel._predict_from_snapshots)
        # The residual must be bone delta MINUS body delta, or the body's
        # motion gets counted twice and the skeleton runs ahead.
        self.assertIn("(cur_pos[0] - prev_pos[0])", src)



def _sleep_zero_calls(func):
    """Every literal `time.sleep(0)` in func's body.

    Matched on the parsed tree rather than on the source text, so a
    docstring or a comment explaining *why* the call is gone does not read
    as the call still being there.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        target = node.func
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            name = f"{target.value.id}.{target.attr}"
        else:
            name = getattr(target, "id", "")
        if name not in ("time.sleep", "sleep"):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and first.value == 0:
            found.append(name)
    return found


class RenderLagTests(unittest.TestCase):
    """"The skeleton is just behind the body."

    Reported visually three sessions running, against an overlay whose
    [PRED-ERR] was already +/-0.005 m -- because that number scores the
    *interpolation*, not the age of what is being interpolated. The drawn
    body is behind live by two terms:

      sample  the newest snapshot is already stale when it arrives
      buffer  interpolation deliberately renders one snapshot interval
              further back, so two measured samples always bracket the
              drawn instant

    Only the second is a choice. The first was partly an accounting error:
    _take_snapshot stamped the snapshot when the tick *finished*, ~10 ms of
    decode and bone composition after the driver handed the words back, so
    every snapshot claimed to be fresher than it was and the interpolation
    compensated by rendering 10 ms further into the past.

    Measured on the in-game tick (31 ms, 10 ms of it post-read) at 9 m/s:
    0.369 m behind, down to 0.279 m once the stamp tells the truth.
    """

    TICK = 0.031
    DECODE = 0.010
    SPEED = 9.0

    # ---- the stamp ---------------------------------------------------
    def _model(self):
        model = object.__new__(RustGameModel)
        model._lock = threading.Lock()
        model.players = []
        model.vp_matrix = None
        model.world_entities = []
        model._bone_slot_cache = {}
        model._bone_resolve_jobs = {}
        model._snap_cur = None
        model._snap_prev = None
        model.diag = ""
        return model

    def test_the_snapshot_is_stamped_when_the_words_were_read(self):
        model = self._model()
        read_at = time.perf_counter() - self.DECODE
        model._frame_data_ts = read_at
        model._take_snapshot()
        self.assertEqual(model._snap_cur[0], read_at)

    def test_the_stamp_is_consumed_so_a_readless_tick_falls_back(self):
        """A snapshot taken on a path that did no frame read must not
        inherit the previous tick's timestamp -- that collapses dt_snap to
        zero and the interpolation bails out entirely."""
        model = self._model()
        read_at = time.perf_counter()
        model._frame_data_ts = read_at
        model._take_snapshot()
        model._take_snapshot()
        self.assertIsNone(model._frame_data_ts)
        self.assertNotEqual(model._snap_cur[0], model._snap_prev[0])
        self.assertGreater(model._snap_cur[0], model._snap_prev[0])

    def test_an_impossible_stamp_is_not_trusted(self):
        model = self._model()
        for bad in (
            time.perf_counter() - model_mod.SNAPSHOT_STAMP_MAX_AGE - 1.0,
            time.perf_counter() + 5.0,          # the future
        ):
            model._frame_data_ts = bad
            model._take_snapshot()
            self.assertNotEqual(model._snap_cur[0], bad)

    def test_the_read_instant_is_the_batch_completion(self):
        src = inspect.getsource(RustGameModel._read_player_frame_batch)
        self.assertIn("self._frame_data_ts = t_batch_done", src)

    # ---- what it buys ------------------------------------------------
    def _game(self):
        game = object.__new__(RustGame)
        game._lock = threading.Lock()
        game._render_motion_cache = {}
        game.diag = ""
        game.tick_ms = 0.0
        return game

    def _mean_lag_metres(self, stamp_offset, lead=0.0):
        """Average metres the drawn body sits behind the real one, swept
        across one tick period of render times."""
        previous = legacy.POS_INTERP_LEAD_FRACTION
        legacy.POS_INTERP_LEAD_FRACTION = lead
        try:
            game = self._game()
            read_prev = 100.0
            read_cur = read_prev + self.TICK
            snap = lambda ts, x: (
                ts,
                [{"pm": 1, "pos": (x, 0.0, 0.0), "vel": (self.SPEED, 0.0, 0.0)}],
                None,
                [],
            )
            game._snap_prev = snap(read_prev + stamp_offset, 0.0)
            game._snap_cur = snap(
                read_cur + stamp_offset, self.SPEED * self.TICK
            )
            total = 0.0
            steps = 40
            with contextlib.redirect_stdout(io.StringIO()):
                for i in range(steps):
                    now = read_cur + stamp_offset + (i / steps) * self.TICK
                    players, _, _, _, _ = game._predict_from_snapshots(now)
                    total += self.SPEED * (now - read_prev) - players[0]["pos"][0]
            return total / steps
        finally:
            legacy.POS_INTERP_LEAD_FRACTION = previous

    def test_an_honest_stamp_brings_the_body_forward(self):
        stale = self._mean_lag_metres(self.DECODE)
        honest = self._mean_lag_metres(0.0)
        self.assertAlmostEqual(stale - honest, self.SPEED * self.DECODE,
                               places=3)
        self.assertLess(honest, stale)

    def test_the_remaining_lag_is_exactly_one_snapshot_interval(self):
        """Not a bug to be shaved: it is what interpolation is. Which is
        why the only way to spend it is POS_INTERP_LEAD_FRACTION, and why
        that costs freeze."""
        honest = self._mean_lag_metres(0.0)
        self.assertAlmostEqual(honest, self.SPEED * self.TICK, places=3)

    def test_a_lead_fraction_never_renders_past_the_newest_sample(self):
        """The rule is interpolation only. A lead moves the drawn instant
        inside the measured segment; it must never leave it."""
        game = self._game()
        cur_x = self.SPEED * self.TICK
        snap = lambda ts, x: (
            ts,
            [{"pm": 1, "pos": (x, 0.0, 0.0), "vel": (self.SPEED, 0.0, 0.0)}],
            None,
            [],
        )
        previous = legacy.POS_INTERP_LEAD_FRACTION
        try:
            for lead in (0.0, 0.5, 0.9, 3.0):
                legacy.POS_INTERP_LEAD_FRACTION = lead
                game._snap_prev = snap(100.0, 0.0)
                game._snap_cur = snap(100.0 + self.TICK, cur_x)
                game._render_motion_cache = {}
                with contextlib.redirect_stdout(io.StringIO()):
                    for i in range(40):
                        now = 100.0 + self.TICK + (i / 40.0) * self.TICK * 3
                        players, _, _, _, _ = game._predict_from_snapshots(now)
                        self.assertLessEqual(
                            players[0]["pos"][0], cur_x + 1e-9,
                            "lead %.2f ran past the newest sample" % lead,
                        )
        finally:
            legacy.POS_INTERP_LEAD_FRACTION = previous

    def test_the_default_lead_is_zero(self):
        """Freshness is bought with pose freeze. [POSE-FREEZE] prints what
        each candidate would cost on the run in front of you; the number is
        picked from that, not guessed here."""
        self.assertEqual(legacy.POS_INTERP_LEAD_FRACTION, 0.0)

    def test_the_freeze_counterfactual_prices_each_candidate(self):
        game = self._game()
        # First call self-heals the counters and, because the log interval
        # has "elapsed", prints and resets them. Prime past that.
        with contextlib.redirect_stdout(io.StringIO()):
            game._track_pose_freeze(0.6, 100.0, raw_ratio=0.6)
        game._last_pose_freeze_log_at = 100.0
        # raw_ratio 0.6: already past 1.0 once a 0.5 lead is added, not with
        # 0.25. That is the whole point of printing it per candidate.
        for _ in range(10):
            game._track_pose_freeze(0.6, 100.0, raw_ratio=0.6)
        self.assertEqual(game._pose_freeze_counterfactual[0.25], 0)
        self.assertEqual(game._pose_freeze_counterfactual[0.5], 10)
        self.assertEqual(game._pose_freeze_counterfactual[0.75], 10)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            game._track_pose_freeze(0.6, 200.0, raw_ratio=0.6)
        line = buf.getvalue()
        self.assertIn("at lead", line)
        self.assertIn("0.50=", line)

    def test_the_lag_is_averaged_not_sampled_at_the_print(self):
        """It first shipped printing the milliseconds from whatever the last
        call happened to set, while the metres were a mean -- so the line
        read `sample=70.6 buffer=0.0` then `sample=3.5 buffer=26.9`, the
        same total at a different phase of the tick."""
        game = self._game()
        # First call self-heals the counters and, the log interval having
        # "elapsed", prints and resets them. Prime past that.
        with contextlib.redirect_stdout(io.StringIO()):
            game._render_lag_total_ms = 0.0
            game._track_render_lag((self.SPEED, 0.0, 0.0), 100.0)
        game._last_render_lag_log_at = 100.0
        for total in (10.0, 50.0):
            game._render_lag_total_ms = total
            game._track_render_lag((self.SPEED, 0.0, 0.0), 100.0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            game._render_lag_total_ms = 50.0
            game._track_render_lag((self.SPEED, 0.0, 0.0), 200.0)
        line = buf.getvalue()
        self.assertIn("[RENDER-LAG]", line)
        # mean of 10, 50, 50 -- not the 50 of the last call
        self.assertIn("36.7ms", line)

    def test_the_two_terms_are_complementary_so_only_the_total_is_printed(self):
        """`sample` and `buffer` sum to one snapshot interval by
        construction: alpha is measured from the same `now - cur_ts` that
        `sample` is. Printing them apart implied they could be traded
        against each other. They cannot."""
        game = self._game()
        snap = lambda ts, x: (
            ts,
            [{"pm": 1, "pos": (x, 0.0, 0.0), "vel": (self.SPEED, 0.0, 0.0)}],
            None,
            [],
        )
        game._snap_prev = snap(100.0, 0.0)
        game._snap_cur = snap(100.0 + self.TICK, self.SPEED * self.TICK)
        with contextlib.redirect_stdout(io.StringIO()):
            for i in range(20):
                now = 100.0 + self.TICK + (i / 20.0) * self.TICK
                game._predict_from_snapshots(now)
                self.assertAlmostEqual(
                    game._render_lag_total_ms, self.TICK * 1000.0, places=3,
                    msg="the total must be one snapshot interval, always",
                )

    def test_a_standing_player_is_not_counted(self):
        """Lag in metres is meaningless for someone who is not moving, and
        averaging them in would bury the players it matters for."""
        game = self._game()
        game._render_lag_sample_ms = 8.0
        game._render_lag_buffer_ms = 31.0
        game._last_render_lag_log_at = 100.0
        game._track_render_lag((0.0, 0.0, 0.0), 100.0)
        self.assertEqual(game._render_lag_n, 0)
        game._track_render_lag((legacy.RENDER_LAG_MIN_SPEED + 1.0, 0.0, 0.0),
                               100.0)
        self.assertEqual(game._render_lag_n, 1)


class BoneForwardPassTests(unittest.TestCase):
    """Each rig slot is placed once, instead of once per bone below it.

    Profiled in-game 2026-08-28, the bone pipeline was the single largest
    cost on the tick: [POS-PROFILE] `bones=12.4ms/0.00io`, i.e. pure Python
    on a ~50 ms tick, with _compose_bones second and _plan_bone_reads fifth
    in own-time across the whole program.

    The cause was structural. [BONE-DBG] reported `slots=660/660` carrying
    `composed=630` bones -- 21 bones sharing a 22-node subtree per player --
    so both the parent walk and the composition re-did a rig's hips and
    spine once for every bone hanging off them. Measured on that exact
    shape: 3810 walk steps against 646 distinct slots.

    The replacement gives every slot its own local-to-world transform,
    built once from its parent's, and reads the bone's world position
    straight off it. These tests exist because that is only worth doing if
    it moves no bone.
    """

    LOCAL = 0x7FF000000000
    PARENT = 0x7FF000100000
    HIER = 0xAA

    class _Mem:
        def __init__(self, words):
            self.words = words
            self.io_calls = 0

        def batch_u64(self, addrs, attempts=3, **kwargs):
            self.io_calls += 1
            return [self.words.get(a, 0) for a in addrs]

        def read(self, addr, size):
            raise AssertionError("per-item read() must not be used here")

    def _mem(self, parents, slots):
        words = {}
        for index, (t, q, sc) in slots.items():
            raw = (struct.pack("<3f", *t) + b"\x00" * 4
                   + struct.pack("<4f", *q)
                   + struct.pack("<3f", *sc) + b"\x00" * 4)
            base = self.LOCAL + model_mod.TRSX_SIZE * index
            for off in range(0, len(raw) - 7, 8):
                words[base + off] = struct.unpack_from("<Q", raw, off)[0]
        packed = struct.pack("<%di" % len(parents), *parents)
        packed += b"\x00" * (-len(packed) % 8)
        for off in range(0, len(packed), 8):
            words[self.PARENT + off] = struct.unpack_from("<Q", packed, off)[0]
        return self._Mem(words)

    def _model(self, mem, radius=1e9):
        model = object.__new__(RustGameModel)
        model.m = mem
        model._bone_debug = {}
        model._bone_last_outcome = {}
        model._parent_index_cache = {}
        model._debug = lambda *a, **k: None
        model._bone_anchor_radius = lambda: radius
        return model

    def _jobs(self, bone_ids, pms=(1,), anchor=(0.0, 0.0, 0.0)):
        plan = {1000 + i: (self.HIER, i) for i in bone_ids}
        ptrs = {self.HIER: (self.LOCAL, self.PARENT)}
        return [(pm, plan, anchor, ptrs) for pm in pms]

    # -- the reference: the algorithm this replaced, kept verbatim ---------
    @staticmethod
    def _reference(jobs, parents, slots):
        """The pre-2026-08-28 per-bone chain walk, unchanged."""
        out = {}
        for pm, plan, _, _ in jobs:
            bones = {}
            for bone_id, (_, index) in plan.items():
                if index >= len(parents):
                    continue
                chain = [index]
                cursor = parents[index]
                depth = 0
                while cursor >= 0 and depth < model_mod.PARENT_WALK_LIMIT:
                    if cursor >= len(parents):
                        break
                    chain.append(cursor)
                    cursor = parents[cursor]
                    depth += 1
                seed = slots.get(chain[0])
                if seed is None:
                    continue
                position = seed[0]
                broken = False
                for ancestor in chain[1:]:
                    node = slots.get(ancestor)
                    if node is None:
                        broken = True
                        break
                    translation, raw_q, scale = node
                    quaternion = model_mod._normalize_quaternion(raw_q)
                    if quaternion is None:
                        broken = True
                        break
                    position = (position[0] * scale[0],
                                position[1] * scale[1],
                                position[2] * scale[2])
                    position = model_mod._rotate_vector(quaternion, position)
                    position = (position[0] + translation[0],
                                position[1] + translation[1],
                                position[2] + translation[2])
                if broken or not all(math.isfinite(c) for c in position):
                    continue
                if not (abs(position[0]) < 6000.0
                        and -200.0 < position[1] < 2000.0
                        and abs(position[2]) < 6000.0):
                    continue
                bones[bone_id] = position
            out[pm] = bones
        return out

    @staticmethod
    def _random_rig(rng, n_slots, break_quat=False, uniform_scale=False):
        parents = [-1]
        for i in range(1, n_slots):
            parents.append(rng.randrange(0, i))      # parents precede children
        slots = {}
        for i in range(n_slots):
            ax, ay, az = (rng.uniform(-1.0, 1.0) for _ in range(3))
            norm = math.sqrt(ax * ax + ay * ay + az * az) or 1.0
            angle = rng.uniform(-math.pi, math.pi)
            k = math.sin(angle / 2.0) / norm
            quat = (ax * k, ay * k, az * k, math.cos(angle / 2.0))
            if break_quat and rng.random() < 0.08:
                quat = (0.0, 0.0, 0.0, 0.0)
            scale = ((1.0, 1.0, 1.0) if uniform_scale else
                     (rng.uniform(0.4, 1.8), rng.uniform(0.4, 1.8),
                      rng.uniform(0.4, 1.8)))
            slots[i] = (
                (rng.uniform(-0.4, 0.4), rng.uniform(-0.4, 0.4),
                 rng.uniform(-0.4, 0.4)),
                quat,
                scale,
            )
        return parents, slots

    def test_it_matches_the_chain_walk_on_random_rigs(self):
        """The only invariant that matters: no bone moves.

        Deep chains, shared ancestors, non-uniform scale and unusable
        quaternions -- the shapes the forward pass is meant to exploit and
        the ones most likely to break it.
        """
        worst = 0.0
        for seed in range(60):
            for n_slots, n_bones in ((4, 3), (12, 8), (40, 21), (90, 21)):
                for break_quat in (False, True):
                    rng = random.Random(seed)
                    parents, slots = self._random_rig(
                        rng, n_slots, break_quat=break_quat
                    )
                    picks = rng.sample(range(n_slots), min(n_bones, n_slots))
                    jobs = self._jobs(picks)
                    model = self._model(self._mem(parents, slots))
                    got = model._sample_bones_multi(jobs).get(1, {})
                    want = self._reference(jobs, parents, slots)[1]
                    self.assertEqual(
                        set(got), set(want),
                        "seed=%d slots=%d quat=%s" % (seed, n_slots, break_quat),
                    )
                    for bone_id, position in got.items():
                        worst = max(worst, math.dist(position, want[bone_id]))
        # float32 words re-associated by the matrix form; microns, on metres.
        self.assertLess(worst, 1e-4, "worst divergence %.3e m" % worst)

    def test_non_uniform_scale_is_why_this_is_a_matrix(self):
        """(t, q, s) is not closed under composition with unequal scale.

        A cheaper quaternion-plus-scalar accumulator would be exact for a
        rig whose scales are all uniform and silently wrong for one that is
        not, which is the kind of bug that shows up as a limb in the wrong
        place on one player model and nowhere else. This is the case that
        would catch it.
        """
        parents = [-1, 0, 1]
        slots = {
            0: ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
            # +90 deg about Z, then a scale that is 3x in x and 1x in y:
            # applying them in the wrong order moves the child by 2 units.
            1: ((0.0, 0.0, 0.0), (0.0, 0.0, math.sqrt(0.5), math.sqrt(0.5)),
                (3.0, 1.0, 1.0)),
            2: ((1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
        }
        jobs = self._jobs([2])
        model = self._model(self._mem(parents, slots))
        got = model._sample_bones_multi(jobs)[1][1002]
        # slot 2 seeds at (1,0,0); slot 1 scales x by 3 -> (3,0,0), then
        # rotates +90 about Z -> (0,3,0); slot 0 is identity.
        for value, expected in zip(got, (0.0, 3.0, 0.0)):
            self.assertAlmostEqual(value, expected, places=4)
        # ...and the chain walk this replaced agrees, to float32 noise.
        for value, expected in zip(
            got, self._reference(jobs, parents, slots)[1][1002]
        ):
            self.assertAlmostEqual(value, expected, places=5)

    def test_a_slot_is_walked_once_not_once_per_bone_below_it(self):
        """`walk=` and `slots=` in [BONE-DBG] must be the same number.

        On the in-game shape -- 30 players, 21 bones each over a 22-node
        subtree -- the old walk was 3810 steps against 646 distinct slots.
        """
        rng = random.Random(11)
        parents, slots = self._random_rig(rng, 40, uniform_scale=True)
        picks = rng.sample(range(40), 21)
        jobs = self._jobs(picks)
        model = self._model(self._mem(parents, slots))
        model._sample_bones_multi(jobs)
        stats = model._bone_debug
        self.assertEqual(stats["chain_steps"], stats["slots_req"])
        self.assertGreater(stats["slots_req"], 21)

    def test_a_shared_ancestor_is_not_read_twice(self):
        """Two players in one hierarchy still cost one set of slot reads."""
        rng = random.Random(12)
        parents, slots = self._random_rig(rng, 30, uniform_scale=True)
        picks = rng.sample(range(30), 12)
        mem = self._mem(parents, slots)
        model = self._model(mem)
        model._sample_bones_multi(self._jobs(picks, pms=(1,)))
        alone = model._bone_debug["slots_req"]
        model = self._model(mem)
        model._sample_bones_multi(self._jobs(picks, pms=(1, 2, 3, 4)))
        self.assertEqual(model._bone_debug["slots_req"], alone)

    def test_a_bone_on_a_broken_joint_survives_its_children(self):
        """A slot's own rotation is never applied to its own position.

        So an unusable quaternion drops everything hanging below that joint
        and not the bone sitting on it -- the same asymmetry the chain walk
        had, and for the same reason. Losing it would silently delete a
        bone that used to draw.
        """
        parents = [-1, 0, 1, 1]
        half = math.sqrt(0.5)
        slots = {
            0: ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
            1: ((0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 0.0), (1.0, 1.0, 1.0)),
            2: ((0.5, 0.0, 0.0), (0.0, 0.0, half, half), (1.0, 1.0, 1.0)),
            3: ((0.0, 0.3, 0.2), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
        }
        jobs = self._jobs([1, 2, 3])
        model = self._model(self._mem(parents, slots))
        bones = model._sample_bones_multi(jobs).get(1, {})
        self.assertIn(1001, bones, "the bone on the broken joint")
        self.assertNotIn(1002, bones)
        self.assertNotIn(1003, bones)
        self.assertGreaterEqual(model._bone_debug["rej_quat"], 2)
        self.assertEqual(bones[1001], (0.0, 1.0, 0.0))

    def test_the_slots_arrive_in_an_order_the_forward_pass_can_use(self):
        """One pass only works if a parent is always decoded before its child.

        Unity stores parents before children, so ascending slot index is a
        topological order -- but only if the planner hands them over sorted.
        """
        rng = random.Random(13)
        parents, slots = self._random_rig(rng, 50, uniform_scale=True)
        jobs = self._jobs(rng.sample(range(50), 20))
        model = self._model(self._mem(parents, slots))
        _, slot_keys, _ = model._plan_bone_reads(jobs)
        self.assertEqual(slot_keys, sorted(slot_keys))
        seen = set()
        for hierarchy, index in slot_keys:
            parent = parents[index]
            if parent >= 0 and (hierarchy, parent) in set(slot_keys):
                self.assertIn((hierarchy, parent), seen,
                              "slot %d decoded before its parent" % index)
            seen.add((hierarchy, index))

    def test_the_quaternion_range_test_still_rejects_nan_and_inf(self):
        """The isfinite() sweep was dropped as redundant, not as unneeded.

        A NaN makes every comparison False and an infinity fails the upper
        bound, so both still return None -- which is the only reason it was
        safe to delete two generator expressions from a path that runs once
        per slot.
        """
        nan = float("nan")
        inf = float("inf")
        for bad in (
            (nan, 0.0, 0.0, 1.0),
            (0.0, inf, 0.0, 0.0),
            (0.0, 0.0, -inf, 0.0),
            (nan, nan, nan, nan),
            (0.0, 0.0, 0.0, 0.0),        # zero norm
            (3.0, 0.0, 0.0, 0.0),        # norm 9, past the upper bound
        ):
            self.assertIsNone(model_mod._normalize_quaternion(bad), bad)
        ok = model_mod._normalize_quaternion((0.0, 0.0, 0.6, 0.8))
        self.assertAlmostEqual(sum(c * c for c in ok), 1.0, places=6)

    def test_the_decode_does_not_build_a_format_string_per_slot(self):
        src = inspect.getsource(RustGameModel._compose_bones)
        decode = src.split("Stage 4")[0]
        self.assertNotIn("'<%dQ'", decode)
        self.assertIn("_TRS_FIELDS", decode)
        self.assertEqual(model_mod._TRS_WORDS.size, model_mod.TRSX_SIZE)
        self.assertEqual(model_mod._TRS_FIELDS.size, model_mod.TRSX_SIZE)


class VsyncTests(unittest.TestCase):
    """swap_buffers() blocking on the vblank was the loop's only pacing.

    Turning vsync off uncaps the render loop: a frame is never held back
    waiting for the monitor, and the loop stops idling inside swap_buffers
    -- which is where it was releasing the GIL for the worker. Profiled
    2026-08-28 that idle was 2.83 s. The trade is real in both directions,
    so it stays a setting rather than a constant, and poll() is the single
    place that applies it.
    """

    class _Overlay:
        """Just enough OverlayView to exercise poll()'s vsync reconciliation."""

        set_vsync = view_base.OverlayView.set_vsync

        def __init__(self, vsync):
            self.settings = view_base.Settings()
            self.settings.vsync = vsync
            self.vsync = False
            self.menu_open = False
            self._menu_key_was_down = False
            self.renderer = type("_R", (), {"process_inputs": lambda self: None})()
            self._user32 = type(
                "_U", (), {"GetAsyncKeyState": lambda self, key: 0}
            )()

    def _poll_with_fake_glfw(self, overlay):
        """Run the real poll() against a glfw stub, collecting the calls."""
        calls = []
        stub = type(
            "_G", (), {
                "poll_events": staticmethod(lambda: None),
                "swap_interval": staticmethod(lambda n: calls.append(n)),
            },
        )
        real = view_base.glfw
        view_base.glfw = stub
        try:
            view_base.OverlayView.poll(overlay)
            view_base.OverlayView.poll(overlay)
        finally:
            view_base.glfw = real
        return calls

    def test_the_default_is_off(self):
        self.assertFalse(view_base.Settings().vsync)

    def test_poll_applies_the_setting_once_and_then_stops(self):
        """A glfw call every frame for a value that changes on a click is
        the kind of thing that ends up in a profile."""
        overlay = self._Overlay(vsync=True)
        calls = self._poll_with_fake_glfw(overlay)
        self.assertEqual(calls, [1], "applied once, not once per frame")
        self.assertTrue(overlay.vsync)

    def test_poll_does_nothing_when_the_setting_already_matches(self):
        overlay = self._Overlay(vsync=False)
        self.assertEqual(self._poll_with_fake_glfw(overlay), [])

    def test_the_menu_can_turn_it_back_on(self):
        """The whole point of it being a setting: the tick may pay for this,
        and that has to be reversible without a rebuild."""
        src = inspect.getsource(view_mod.OverlayView._draw_feature_tab)
        self.assertIn("s.vsync", src)
        self.assertIn("VSync", src)

    @staticmethod
    def _sleeps_in(func):
        """Actual sleep calls, not the word in a comment explaining why not."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = (target.attr if isinstance(target, ast.Attribute)
                    else getattr(target, "id", ""))
            if name == "sleep":
                found.append(ast.dump(target))
        return found

    def test_no_software_limiter_came_back_with_it(self):
        """With vsync on, a sleep-based cap fought the GPU's own pacing --
        two clocks drifting, which is the stutter that was reported live.
        With vsync off the loop is meant to be uncapped, not re-paced in
        Python."""
        for func in (view_mod.OverlayView.render,
                     view_base.OverlayView.render,
                     controller_mod.EspController.run):
            self.assertEqual(self._sleeps_in(func), [], func.__qualname__)

    def test_startup_says_which_mode_it_is_in(self):
        src = inspect.getsource(controller_mod.EspController.run)
        self.assertIn("vsync off", src)
        self.assertNotIn('f"[+] Rendu synchronisé à {self._view.refresh_hz} Hz"',
                         src)


class ViewportCacheTests(unittest.TestCase):
    """Five Win32 round-trips per frame to ask where a window that did not move is.

    Profiled 2026-08-28 at 0.83 s of own time, ~0.2 ms of a 6.9 ms frame at
    144 Hz, for a rect that changes on alt-tab and resolution changes and
    never between two frames.
    """

    def setUp(self):
        view_base._viewport_cache = None
        view_base._viewport_cache_until = 0.0
        self.addCleanup(setattr, view_base, "_viewport_cache", None)
        self.addCleanup(setattr, view_base, "_viewport_cache_until", 0.0)

    def _probe(self, results):
        """Swap the Win32 probe for a counter; restore it afterwards."""
        calls = []

        def probe():
            calls.append(1)
            return results[min(len(calls) - 1, len(results) - 1)]

        real = view_base._probe_game_viewport
        view_base._probe_game_viewport = probe
        self.addCleanup(setattr, view_base, "_probe_game_viewport", real)
        return calls

    def test_the_window_is_probed_once_per_ttl(self):
        calls = self._probe([(0.0, 0.0, 1920.0, 1080.0, 1.0, 1.0,
                              1920.0, 1080.0)])
        first = view_base.get_game_viewport()
        for _ in range(200):
            self.assertEqual(view_base.get_game_viewport(), first)
        self.assertEqual(len(calls), 1)

    def test_it_is_re_probed_once_the_ttl_lapses(self):
        """A resolution change must not be invisible for longer than the TTL."""
        self._probe([
            (0.0, 0.0, 1920.0, 1080.0, 1.0, 1.0, 1920.0, 1080.0),
            (0.0, 0.0, 2560.0, 1440.0, 1.0, 1.0, 2560.0, 1440.0),
        ])
        self.assertEqual(view_base.get_game_viewport()[2], 1920.0)
        view_base._viewport_cache_until = 0.0        # the TTL lapsing
        self.assertEqual(view_base.get_game_viewport()[2], 2560.0)

    def test_the_ttl_is_short_enough_to_be_invisible(self):
        self.assertLessEqual(view_base.VIEWPORT_CACHE_TTL, 1.0)
        self.assertGreater(view_base.VIEWPORT_CACHE_TTL, 0.0)


class CameraPredictionTests(unittest.TestCase):
    """The view matrix is carried forward; players are not.

    That asymmetry is the point and it is measured, not assumed. A remote
    player is a networked position that can reverse between two samples --
    [PRED-ERR] caught +1.5 m spikes doing exactly that, which is what "the
    bones leave the body" was. The camera is the local player's head:
    continuous rotation, no network, and every sample resets the error.

    Meanwhile [CAM-HZ] is 22-34 Hz against a 144 Hz overlay, so `vpage=`
    runs 1-44 ms and a stale matrix slides *everything* on screen at once
    while turning.

    These pin the bounds, because an unbounded version snaps back.
    """

    IDENTITYish = tuple(float(i) for i in range(16))

    def _sampler(self, latest, previous):
        sampler = object.__new__(legacy.CameraSampler)
        sampler._lock = threading.Lock()
        sampler._latest = latest
        sampler._previous = previous
        sampler._pred_frames = 0
        sampler._pred_used = 0
        sampler._pred_lead_sum = 0.0
        sampler._pred_lead_max = 0.0
        sampler._pred_skips = {}
        sampler._next_pred_log_at = float("inf")
        return sampler

    def _pair(self, gap=0.030, spin=0.5, moved=0.0):
        """Two samples `gap` apart, the matrix advancing by `spin` a step."""
        prev_vp = tuple(float(i) for i in range(16))
        vp = tuple(v + spin for v in prev_vp)
        prev_cam = (0.0, 0.0, 0.0)
        cam = (moved, 0.0, 0.0)
        t = 100.0
        return (vp, cam, t), (prev_vp, prev_cam, t - gap)

    def test_the_matrix_is_advanced_by_the_time_since_the_sample(self):
        latest, previous = self._pair(gap=0.030, spin=0.5)
        sampler = self._sampler(latest, previous)
        vp, _, sampled_at = sampler.get_predicted(100.015)   # half a gap on
        for i in range(16):
            self.assertAlmostEqual(vp[i], latest[0][i] + 0.25, places=5)
        self.assertEqual(sampled_at, 100.0, "vpage= must stay the raw age")

    def test_a_still_camera_is_left_exactly_alone(self):
        latest, previous = self._pair(spin=0.0)
        sampler = self._sampler(latest, previous)
        vp, cam, _ = sampler.get_predicted(100.02)
        self.assertEqual(vp, latest[0])
        self.assertEqual(cam, latest[1])

    def test_the_lead_is_capped(self):
        """A frame drawn long after the last sample must not keep going."""
        latest, previous = self._pair(gap=0.030, spin=1.0)
        sampler = self._sampler(latest, previous)
        vp, _, _ = sampler.get_predicted(100.0 + 5.0)
        step = vp[0] - latest[0][0]
        self.assertAlmostEqual(
            step, 1.0 * (legacy.CAM_PREDICT_MAX_LEAD / 0.030), places=5
        )

    def test_the_alpha_is_capped_when_samples_are_far_apart(self):
        """A slow sample rate must not become a wild extrapolation."""
        latest, previous = self._pair(gap=0.005, spin=1.0)
        sampler = self._sampler(latest, previous)
        vp, _, _ = sampler.get_predicted(100.0 + legacy.CAM_PREDICT_MAX_LEAD)
        step = vp[0] - latest[0][0]
        self.assertAlmostEqual(step, legacy.CAM_PREDICT_MAX_ALPHA, places=5)

    def test_a_teleport_is_never_projected(self):
        """Respawn, vehicle, death cam: the matrix delta is meaningless."""
        latest, previous = self._pair(
            spin=1.0, moved=legacy.CAM_PREDICT_MAX_STEP + 1.0
        )
        sampler = self._sampler(latest, previous)
        vp, cam, _ = sampler.get_predicted(100.02)
        self.assertEqual(vp, latest[0])
        self.assertEqual(cam, latest[1])
        self.assertEqual(sampler._pred_skips.get("teleport"), 1)

    def test_an_impossible_gap_is_not_trusted(self):
        for gap in (legacy.CAM_PREDICT_MIN_GAP / 2,
                    legacy.CAM_PREDICT_MAX_GAP * 2):
            latest, previous = self._pair(gap=gap, spin=1.0)
            sampler = self._sampler(latest, previous)
            vp, _, _ = sampler.get_predicted(100.02)
            self.assertEqual(vp, latest[0], f"gap={gap}")

    def test_the_first_sample_of_all_is_drawable(self):
        latest, _ = self._pair()
        sampler = self._sampler(latest, None)
        self.assertEqual(sampler.get_predicted(100.02), latest)
        self.assertIsNone(self._sampler(None, None).get_predicted(100.0))

    def test_a_frame_earlier_than_the_sample_is_not_run_backwards(self):
        latest, previous = self._pair(spin=1.0)
        sampler = self._sampler(latest, previous)
        vp, _, _ = sampler.get_predicted(99.99)
        self.assertEqual(vp, latest[0])

    def test_the_controller_draws_the_predicted_matrix(self):
        from tools.rust_esp_mvc import controller as controller_mod
        src = inspect.getsource(controller_mod.EspController.run)
        self.assertIn("get_predicted(now)", src)
        self.assertNotIn("get_latest()", src)


class TextShadowTests(unittest.TestCase):
    """Every label was drawn five times.

    Profiled 2026-08-27: `_draw_outlined_text` was 1.47 s of own time,
    fifth in the whole program. A four-way outline plus the fill is five
    passes over the glyphs, and each player carries a name, a distance and
    a held item.
    """

    def test_two_shadows_and_a_fill(self):
        drawn = []

        class _List:
            def add_text(self, pos, color, text):
                drawn.append((pos, color, text))

        view_base._draw_outlined_text(_List(), 10.0, 20.0, 0xFFFFFFFF, "x")
        self.assertEqual(len(drawn), 3)
        self.assertEqual(drawn[-1][0], (10.0, 20.0))
        self.assertEqual(drawn[-1][1], 0xFFFFFFFF)

    def test_the_shadows_are_diagonal_so_no_corner_is_bare(self):
        """A single drop shadow leaves one corner with no contrast at all
        against sky, which is where distance labels usually sit."""
        xs = {dx for dx, _ in view_base.TEXT_SHADOW_OFFSETS}
        ys = {dy for _, dy in view_base.TEXT_SHADOW_OFFSETS}
        self.assertEqual(xs, {-1.0, 1.0})
        self.assertEqual(ys, {-1.0, 1.0})

    def test_the_fill_is_drawn_last(self):
        """Otherwise the shadow lands on top of the glyph."""
        order = []

        class _List:
            def add_text(self, pos, color, text):
                order.append(color)

        view_base._draw_outlined_text(_List(), 0.0, 0.0, 0xFF00FF00, "x")
        self.assertEqual(order[:-1],
                         [view_base.TEXT_SHADOW_COLOR] * 2)
        self.assertEqual(order[-1], 0xFF00FF00)


class BatchPlanTests(unittest.TestCase):
    """batch_u64 must return exactly what it used to, ~7x faster.

    Profiled 2026-08-27: batch_u64 was 1.94 s of own time, third in the
    whole program, and 1.11 ms of its ~2.03 ms per call was
    `sorted(enumerate(addrs), key=lambda x: x[1])` -- n tuples and a Python
    lambda per comparison, on the worker thread, at 56% GIL.

    It did not need to run. Player positions are `pm + <fixed offset>` and
    bone slots are `local_ptr + 0x30 * slot`, so the list is usually
    identical tick to tick. Measured 2.04 ms -> 0.27 ms warm.

    Everything below is about the part that must NOT change: the words that
    come out, and in which order.
    """

    KEY = 0x0101010101010101

    def _mem(self):
        mem = object.__new__(legacy.Mem)
        mem._io_tls = threading.local()
        return mem

    @staticmethod
    def _reference(addrs, raw_by_sorted_position):
        """The original algorithm, spelled out, as the thing to match."""
        indexed = sorted(enumerate(addrs), key=lambda x: x[1])
        out = [0] * len(addrs)
        for i, (orig_idx, addr) in enumerate(indexed):
            out[orig_idx] = (
                raw_by_sorted_position[i]
                ^ (((addr ^ 0x5A) & 0xFF) * BatchPlanTests.KEY)
            )
        return out

    def _addrs(self, n=257, seed=3):
        rng = random.Random(seed)
        bases = [0x2B000000000 + rng.randrange(0, 1 << 28, 0x1000)
                 for _ in range(9)]
        return [rng.choice(bases) + rng.randrange(0, 0x800, 8)
                for _ in range(n)]

    def test_the_plan_reproduces_the_original_decode_exactly(self):
        mem = self._mem()
        addrs = self._addrs()
        rng = random.Random(11)
        raw = [rng.getrandbits(64) for _ in addrs]

        _, _, inverse, masks = mem._batch_plan(addrs, len(addrs))
        got = [raw[inverse[j]] ^ masks[j] for j in range(len(addrs))]

        self.assertEqual(got, self._reference(addrs, raw))

    def test_the_blob_is_little_endian_and_sorted(self):
        """The driver is handed sorted addresses; that has not changed."""
        mem = self._mem()
        addrs = self._addrs(n=64)
        _, blob, _, _ = mem._batch_plan(addrs, len(addrs))
        unpacked = list(struct.unpack("<%dQ" % len(addrs), blob))
        self.assertEqual(unpacked, sorted(addrs))

    def test_an_unchanged_list_reuses_the_plan(self):
        mem = self._mem()
        addrs = self._addrs()
        first = mem._batch_plan(addrs, len(addrs))
        again = mem._batch_plan(list(addrs), len(addrs))
        self.assertIs(first, again)

    def test_a_changed_list_of_the_same_length_is_not_reused(self):
        """The guard is equality, never length. A stale plan would decode
        every word of the batch against the wrong address."""
        mem = self._mem()
        addrs = self._addrs()
        first = mem._batch_plan(addrs, len(addrs))
        moved = list(addrs)
        moved[len(moved) // 2] += 8
        second = mem._batch_plan(moved, len(moved))
        self.assertIsNot(first, second)

        rng = random.Random(5)
        raw = [rng.getrandbits(64) for _ in moved]
        _, _, inverse, masks = second
        got = [raw[inverse[j]] ^ masks[j] for j in range(len(moved))]
        self.assertEqual(got, self._reference(moved, raw))

    def test_duplicate_addresses_still_round_trip(self):
        """Sorting is not a bijection on values, only on indices."""
        mem = self._mem()
        addrs = [0x2B000001000] * 5 + [0x2B000002000] * 5
        rng = random.Random(2)
        raw = [rng.getrandbits(64) for _ in addrs]
        _, _, inverse, masks = mem._batch_plan(addrs, len(addrs))
        got = [raw[inverse[j]] ^ masks[j] for j in range(len(addrs))]
        self.assertEqual(got, self._reference(addrs, raw))

    def test_the_cache_is_bounded(self):
        mem = self._mem()
        for n in range(1, legacy.BATCH_PLAN_CACHE_MAX + 4):
            mem._batch_plan(self._addrs(n=n, seed=n), n)
        self.assertLessEqual(
            len(mem._io_tls.batch_plan), legacy.BATCH_PLAN_CACHE_MAX
        )

    def test_each_thread_keeps_its_own_plan(self):
        """One shared dict would let three threads evict each other.

        The tick, the camera and the slow lane all call batch_u64 with
        different lists; sharing one entry per length means each call
        misses on the previous thread's list and recomputes -- correct,
        but the cache would never hit, which is the entire point of it.
        """
        mem = self._mem()
        mine = self._addrs(n=32, seed=1)
        theirs = self._addrs(n=32, seed=2)
        mem._batch_plan(mine, 32)
        seen = []

        def other():
            mem._batch_plan(theirs, 32)
            seen.append(dict(mem._io_tls.batch_plan))

        thread = threading.Thread(target=other)
        thread.start()
        thread.join()

        self.assertEqual(seen[0][32][0], theirs)
        # ...and this thread's plan is untouched by that
        self.assertEqual(mem._io_tls.batch_plan[32][0], mine)

    def test_the_masks_only_depend_on_the_low_byte(self):
        """_XOR_MASKS is a 256-entry table standing in for the arithmetic."""
        for value in (0x00, 0x5A, 0xFF, 0x7C):
            self.assertEqual(
                legacy._XOR_MASKS[value],
                ((value ^ 0x5A) & 0xFF) * self.KEY,
            )
        self.assertEqual(len(legacy._XOR_MASKS), 256)


class DriverWaitTests(unittest.TestCase):
    """The wait for the driver, not the driver, was the 15 ms.

    Measured 2026-08-27, all three from the same machine:

      * the driver answers a CMD_PING in 30us mean / 34us p99
        (bench_event_latency.py, PASS);
      * one ctypes shared-field read costs 0.030us, so the old
        `for _ in range(1500)` spin covered 45 *microseconds* of waiting;
      * what it fell through into was `time.sleep(0)`, which releases the GIL
        -- and with two CPU-bound Python threads running (the worker tick and
        the render loop, i.e. always) one such call cost p90=29.8ms,
        p99=63.0ms at CPython's default 5 ms switch interval, against
        p90=0.015ms at 0.5 ms.

    That is the whole of the flat "~15 ms per IOCTL" that [TICK-LATENCY],
    [CAM-HZ] and bench_driver.py all reported -- bimodal, ~0.01 ms or
    ~15.1 ms with nothing in between, and identical for a 100-address batch
    and a 1000-address one. These tests hold the two properties that fixed
    it: the spin is bounded by a clock rather than an iteration count, and
    nothing in the wait path yields the GIL.
    """

    class _FakeShared:
        def __init__(self, command):
            self.command = command

    class _DelayedShared:
        """A driver that answers exactly `delay` from now, holding no GIL.

        A stand-in thread cannot model this: it needs the interpreter to run
        in order to publish its answer, so what it really measures is thread
        scheduling. The real driver writes shared memory from the kernel and
        the answer is simply *there* at some wall-clock instant, which is
        what this reproduces -- and deterministically, where the thread
        version raced its own start-up latency against the delay it was
        supposed to be simulating.
        """

        def __init__(self, delay):
            self._answer_at = time.perf_counter() + delay

        @property
        def command(self):
            return 0 if time.perf_counter() >= self._answer_at else 5

    def _mem(self, command=0, budget=None, answers_in=None):
        mem = object.__new__(legacy.Mem)
        mem._io_tls = threading.local()
        mem.shared = (
            self._FakeShared(command) if answers_in is None
            else self._DelayedShared(answers_in)
        )
        mem.spin_budget = (
            legacy._WAIT_SPIN_START if budget is None else budget
        )
        return mem

    def test_an_already_finished_command_costs_nothing(self):
        mem = self._mem(command=0)
        started = time.perf_counter()
        self.assertTrue(mem._wait())
        self.assertLess(time.perf_counter() - started, 0.001)
        self.assertEqual(mem.io_slow_calls, 0)
        self.assertEqual(mem.io_calls, 1)

    def test_a_completion_inside_the_budget_never_reaches_the_sleep(self):
        """The case the old 45us spin could not cover.

        0.3 ms is longer than any iteration-count spin this file ever had and
        still far inside a real batch's round trip, so it is exactly the
        window where the old code paid a full scheduler quantum.
        """
        mem = self._mem(answers_in=0.0003)
        self.assertTrue(mem._wait())
        self.assertEqual(mem.io_slow_calls, 0)

    def test_a_completion_past_the_budget_is_counted_slow(self):
        """`slow=` is the actionable number, so it has to be truthful.

        The mean IOCTL time cannot distinguish one missed wakeup from ten
        mediocre round trips, and only the first is fixed by widening
        _WAIT_SPIN_MIN.
        """
        mem = self._mem(budget=0.0005, answers_in=0.0005 + 0.004)
        self.assertTrue(mem._wait())
        self.assertEqual(mem.io_slow_calls, 1)

    def test_a_polling_driver_stops_being_spun_for(self):
        """The 2026-08-27 in-game case: slow=100%, io pinned at 15.4ms.

        A driver whose answer is 15 ms away cannot be caught by any budget
        worth holding the GIL for, so continuing to spin 1.5 ms per IOCTL on
        three threads is pure waste. The budget has to walk itself down.
        """
        mem = self._mem(command=5, budget=legacy._WAIT_SPIN_MAX)
        for _ in range(40):
            mem._wait(ms=1)
        self.assertLess(mem.spin_budget, legacy._WAIT_SPIN_MAX / 10.0)
        self.assertGreaterEqual(mem.spin_budget, legacy._WAIT_SPIN_MIN)

    def test_a_fast_driver_wins_its_budget_back(self):
        """...and the floor must not lock out an event-based driver.

        bench_event_latency measured 30us round trips when the driver's wake
        event was in use. If that build is ever reloaded, the budget has to
        climb back on its own rather than needing a constant edited.
        """
        mem = self._mem(budget=legacy._WAIT_SPIN_MIN, answers_in=0.0002)
        started = mem.spin_budget
        for _ in range(5):
            mem.shared = self._DelayedShared(0.0002)
            mem._wait()
        self.assertGreater(mem.spin_budget, started)
        self.assertLessEqual(mem.spin_budget, legacy._WAIT_SPIN_MAX)

    def test_a_driver_that_never_answers_times_out(self):
        mem = self._mem(command=5)
        started = time.perf_counter()
        self.assertFalse(mem._wait(ms=30))
        elapsed = time.perf_counter() - started
        self.assertGreaterEqual(elapsed, 0.02)
        self.assertLess(elapsed, 0.5)

    def test_the_wait_path_never_yields_the_gil(self):
        self.assertEqual(_sleep_zero_calls(legacy.Mem._wait), [])
        self.assertIn("spin_budget", inspect.getsource(legacy.Mem._wait))

    def test_the_spin_is_bounded_by_a_clock_not_an_iteration_count(self):
        self.assertFalse(hasattr(legacy, "_WAIT_SPIN_ITERS"))
        self.assertGreater(legacy._WAIT_SPIN_MIN, 0.0)
        # A spin holds the GIL for its whole duration, so an unbounded-ish
        # budget would starve the render thread instead of the driver.
        self.assertLessEqual(legacy._WAIT_SPIN_MAX, 0.005)
        self.assertLess(legacy._WAIT_SPIN_MIN, legacy._WAIT_SPIN_MAX)

    def test_startup_lowers_the_gil_switch_interval(self):
        # 5 ms is CPython's default and the value that produced the 29.8ms
        # p90 above; anything at or above it defeats the whole fix.
        self.assertLess(legacy.GIL_SWITCH_INTERVAL, 0.005)
        src = inspect.getsource(legacy.raise_timer_resolution)
        self.assertIn("setswitchinterval", src)
        self.assertIn("GIL_SWITCH_INTERVAL", src)

    def test_post_command_measures_the_driver_and_not_the_scheduler(self):
        """bench_event_latency's number is only worth having if this is true."""
        self.assertEqual(_sleep_zero_calls(legacy.Mem.post_command), [])
        self.assertIn("spin_budget",
                      inspect.getsource(legacy.Mem.post_command))


class CameraPriorityTests(unittest.TestCase):
    """A stale view matrix makes the whole overlay slide against the world.

    Originally a bespoke _camera_waiting/_camera_idle Event pair (trap #54):
    every other reader only did `time.sleep(0)` -- a bare GIL yield that does
    not hold anything back -- and a Python Lock is not FIFO. With the slow
    lane issuing 17-36 back-to-back IOCTLs the camera lost the race
    repeatedly, and each lost race is more milliseconds of view-matrix
    staleness. Generalized 2026-08-26 into the tier system
    (TIER_CAMERA/TIER_TICK, _claim_tier/_yield_to_this_tier) after a second,
    near-identical pair was copy-pasted for the tick and a third need
    (pm->bp mapping) made the copy-paste pattern itself the problem; these
    tests now exercise that shared mechanism through the camera tier.
    """

    def _mem(self):
        mem = object.__new__(legacy.Mem)
        mem._io_tls = threading.local()
        mem._drv_lock = threading.Lock()
        mem._tier_idle = [
            threading.Event() for _ in range(legacy._PRIORITY_TIER_COUNT)
        ]
        for ev in mem._tier_idle:
            ev.set()
        return mem

    def test_an_idle_camera_costs_nothing(self):
        mem = self._mem()
        started = time.perf_counter()
        mem._yield_to_camera()
        self.assertLess(time.perf_counter() - started, 0.01)

    def test_a_pending_camera_read_holds_the_others_back(self):
        mem = self._mem()
        mem._tier_idle[legacy.TIER_CAMERA].clear()
        released = threading.Event()

        def camera():
            time.sleep(0.03)
            mem._tier_idle[legacy.TIER_CAMERA].set()
            released.set()

        threading.Thread(target=camera, daemon=True).start()
        started = time.perf_counter()
        mem._yield_to_camera()
        waited = time.perf_counter() - started
        self.assertTrue(released.wait(1.0))
        self.assertGreater(waited, 0.02)

    def test_a_stuck_flag_cannot_deadlock(self):
        # A lost or never-cleared flag must cost one timeout, never forever.
        mem = self._mem()
        mem._tier_idle[legacy.TIER_CAMERA].clear()
        started = time.perf_counter()
        mem._yield_to_camera(timeout=0.05)
        elapsed = time.perf_counter() - started
        self.assertGreaterEqual(elapsed, 0.04)
        self.assertLess(elapsed, 0.5)

    def test_both_bulk_readers_yield(self):
        import inspect
        src = inspect.getsource(legacy.Mem)
        self.assertEqual(src.count("self._yield_to_camera()"), 2)
        # The old bare-announcement flag (set/cleared, never actually waited
        # on by anything) must stay gone -- it looked like a priority
        # mechanism and was not one, which is worse than no mechanism at all.
        self.assertNotIn("_camera_waiting", src)
        self.assertNotIn("_tick_waiting", src)

    def test_the_camera_path_marks_itself_busy_and_idle_again(self):
        import inspect
        src = inspect.getsource(legacy.Mem._claim_tier)
        self.assertIn("idle.clear()", src)
        self.assertIn("idle.set()", src)
        # Cleared before the transaction, restored in a finally -- an
        # exception in a priority path must not leave lower tiers blocked
        # forever. This is now shared by both the camera and tick tiers, not
        # reimplemented per tier.
        tail = src.split("idle.clear()")[1]
        self.assertIn("finally", tail)
        self.assertIn("idle.set()", tail.split("finally")[1])

    def test_the_tick_tier_defers_to_the_camera_tier(self):
        # Regression for a real bug found during the 2026-08-26 review:
        # the hand-rolled batch_u64_priority() never deferred to the camera
        # at all, so the tick's priority reads could win a race the camera
        # was supposed to win outright. _claim_tier fixes this structurally
        # (every tier yields to every strictly higher one before claiming
        # its own), so batch_u64_priority's own source no longer needs its
        # own camera-yield call -- it goes through _claim_tier instead.
        import inspect
        src = inspect.getsource(legacy.Mem.batch_u64_priority)
        self.assertIn("self._claim_tier(TIER_TICK)", src)

    def test_the_view_matrix_age_is_reported(self):
        import inspect
        from tools.rust_esp_mvc import view as view_mod
        self.assertIn("vpage=", inspect.getsource(view_mod.OverlayView.render))



if __name__ == "__main__":
    unittest.main()
