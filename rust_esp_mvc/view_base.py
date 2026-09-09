"""View layer: transparent GLFW/ImGui overlay and HUD drawing only."""

import ctypes
import math
import time

GUI_IMPORT_ERROR = None
try:
    import glfw
    import OpenGL.GL as gl
    from imgui_bundle import imgui, imgui_ctx
    from imgui_bundle.python_backends.glfw_backend import GlfwRenderer
except ImportError as exc:
    GUI_IMPORT_ERROR = exc
    glfw = None
    gl = None
    imgui = None
    imgui_ctx = None
    GlfwRenderer = None


COL_ACCENT = 0xFFFFD400  # RGB #00D4FF packed as ImGui ABGR
COL_WHITE = 0xFFFFFFFF
# World entity ESP colors (ImGui ABGR)
COL_ORE    = 0xFF00D4FF   # Gold/Yellow
COL_HEMP   = 0xFF44FF44   # Bright green
COL_ITEM   = 0xFFFFFFFF   # White
COL_BAG    = 0xFFFF66CC   # Purple-pink
COL_CRATE  = 0xFF0099FF   # Orange
COL_TC     = 0xFF4444FF   # Red
WORLD_ENTITY_COLORS = {
    'Ore': COL_ORE, 'Hemp': COL_HEMP, 'Item': COL_ITEM,
    'Bag': COL_BAG, 'Crate': COL_CRATE, 'TC': COL_TC,
}
WORLD_ENTITY_MAX_DIST = {
    'Ore': 350.0, 'Hemp': 100.0, 'Item': 150.0,
    'Bag': 200.0, 'Crate': 200.0, 'TC': 400.0,
}
COL_HELD_ITEM = 0xFF88CCFF  # Light orange/peach (ABGR)
COL_NAME = 0xFFFFDD44      # Light cyan/sky blue (ABGR)
COL_HEALTH_BG = 0xC8000000 # Dark semi-transparent background
ESP_WINDOW_FLAGS = 0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x20 | 0x80
# Confirmed 2026-08-21 via a live Model.boneNames dump — see the matching
# note on SCI_BONE_IDS/SCI_BONE_LINKS in legacy_runtime.py (OFFSET_RECOVERY.md
# trap #11). Must stay in sync with those.
SKELETON_LINKS = (
    (0, 20), (20, 21), (21, 22), (22, 23), (23, 52), (52, 53),
    (23, 24), (24, 25), (25, 26), (26, 29),
    (23, 60), (60, 61), (61, 62), (62, 65),
    (0, 1), (1, 3), (3, 4),
    (0, 14), (14, 16), (16, 17),
)


class OverlayDependencyError(RuntimeError):
    pass


GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
# The overlay's menu key. GetAsyncKeyState only peeks at global key state --
# it cannot consume the press -- so whatever is chosen here also reaches the
# game. INSERT is a key Rust does not bind; ESCAPE (0x1B, what this used to
# be) opened Rust's own pause menu at the same time.
VK_MENU_TOGGLE = 0x2D  # VK_INSERT


# Skeleton line and head size are derived from the skeleton's own height on
# screen, not from a distance curve. The distance curve was the previous
# attempt and it produced the thing the user actually complained about: at
# 65-114 m a player projects to ~12-21 px tall while the head circle stayed
# at its 3.3 px floor -- a 6.6 px blob on a 21 px body, i.e. 31% of the
# player's height was head. "De loin on voit juste une grosse tete."
#
# Screen height already contains distance *and* field of view *and*
# resolution, so scaling from it is right at every range by construction. A
# real head is about 1/7.5 of a standing human, so its radius is ~1/15 of
# the projected height; a limb is roughly 1/45 as thick as the body is tall.
SKELETON_HEAD_SPAN_RATIO = 1.0 / 16.0
SKELETON_LINE_SPAN_RATIO = 1.0 / 45.0
# Floors, so a very distant skeleton stays visible as a mark rather than
# vanishing, and caps, so a player two metres away does not fill the screen
# with a 44 px head.
SKELETON_LINE_MIN_PX = 0.6
SKELETON_LINE_MAX_PX = 2.1
SKELETON_HEAD_MIN_PX = 0.7
SKELETON_HEAD_MAX_PX = 4.6
# The dark halo is *added* to the line rather than being its own scaled
# value. A multiplicative outline (it used to be 3.0 px against a 1.25 px
# line) is what turned a distant skeleton into a solid dark smudge: at that
# size the halo is the only thing left.
SKELETON_OUTLINE_EXTRA_PX = 0.9
# Below this projected height a skeleton stops being a skeleton.
#
# Measured off the 2026-08-27 recording, which was made *after* the sizing
# rewrite above: at 60 m a player spans ~30 px and the rig's twenty links all
# land inside a ~10 px-wide silhouette, so every line touches its neighbours
# and the result is a solid magenta smear. Thinning the lines did not fix
# that and cannot -- at that size there is no room for twenty of them, whatever
# their width. So past the threshold only the spine is drawn: hips to head,
# one stroke, which still marks the player and shows which way they lean
# without filling in the body. 40 px is a 1.8 m player at ~45 m on a 1080p
# ~65-degree vertical view.
#
# The switch is deliberately hard rather than faded: it happens at a fixed
# range, so it reads as information about distance rather than as a glitch.
SKELETON_DETAIL_MIN_SPAN_PX = 40.0
SKELETON_SPINE_LINKS = (
    (0, 20), (20, 21), (21, 22), (22, 23), (23, 52), (52, 53),
)


def _skeleton_thickness(span_px):
    """Line width, head radius and halo for a skeleton `span_px` tall.

    `span_px` is the projected vertical extent of the skeleton on screen.
    Returns (line_px, head_px, outline_px). A span of 0 (a single projected
    bone, or a skeleton edge-on) falls back to the floors, which is the
    smallest thing that is still visible -- never the largest.
    """
    line = min(
        SKELETON_LINE_MAX_PX,
        max(SKELETON_LINE_MIN_PX, span_px * SKELETON_LINE_SPAN_RATIO),
    )
    head = min(
        SKELETON_HEAD_MAX_PX,
        max(SKELETON_HEAD_MIN_PX, span_px * SKELETON_HEAD_SPAN_RATIO),
    )
    return line, head, line + SKELETON_OUTLINE_EXTRA_PX


def _pack_abgr(rgba):
    """Pack an (r, g, b, a) float tuple into the u32 ABGR word ImGui draws with."""
    r, g, b, a = rgba
    return (
        (int(max(0.0, min(1.0, a)) * 255.0 + 0.5) << 24)
        | (int(max(0.0, min(1.0, b)) * 255.0 + 0.5) << 16)
        | (int(max(0.0, min(1.0, g)) * 255.0 + 0.5) << 8)
        | int(max(0.0, min(1.0, r)) * 255.0 + 0.5)
    )


# Every colour the ESP draws with, as editable (r, g, b, a) floats. These are
# the previous hard-coded constants converted back out of their ABGR words, with
# one deliberate change: the tracer used to be 0xC000D4FF, which unpacks to
# *orange* — the intent was clearly the cyan accent, but the RGB triplet was
# written the wrong way round for ABGR. It now matches the box and skeleton.
DEFAULT_PALETTE = {
    "box":            (0.000, 0.831, 1.000, 1.00),
    "box_sleeping":   (0.533, 0.533, 0.533, 0.667),
    "skeleton":       (0.000, 0.831, 1.000, 1.00),
    "tracer":         (0.000, 0.831, 1.000, 0.75),
    "tracer_sleeping": (0.667, 0.667, 0.667, 0.533),
    "name":           (0.267, 0.867, 1.000, 1.00),
    "name_sleeping":  (0.533, 0.533, 0.533, 0.667),
    "distance":       (1.000, 1.000, 1.000, 1.00),
    "held_item":      (1.000, 0.800, 0.533, 1.00),
    "watermark":      (1.000, 1.000, 1.000, 1.00),
    "accent":         (0.000, 0.831, 1.000, 1.00),
    "we_Ore":         (1.000, 0.831, 0.000, 1.00),
    "we_Hemp":        (0.267, 1.000, 0.267, 1.00),
    "we_Item":        (1.000, 1.000, 1.000, 1.00),
    "we_Bag":         (0.800, 0.400, 1.000, 1.00),
    "we_Crate":       (1.000, 0.600, 0.000, 1.00),
    "we_TC":          (1.000, 0.267, 0.267, 1.00),
}

# Grouped for the menu: (section label, ((palette key, display label), ...)).
PALETTE_GROUPS = (
    ("Players", (
        ("box", "Box"),
        ("skeleton", "Skeleton"),
        ("tracer", "Tracer"),
        ("name", "Name"),
        ("distance", "Distance"),
        ("held_item", "Held item"),
    )),
    ("Sleepers", (
        ("box_sleeping", "Box"),
        ("tracer_sleeping", "Tracer"),
        ("name_sleeping", "Name"),
    )),
    ("World", (
        ("we_Ore", "Ore"),
        ("we_Hemp", "Hemp"),
        ("we_Item", "Item"),
        ("we_Bag", "Bag"),
        ("we_Crate", "Crate"),
        ("we_TC", "TC"),
    )),
    ("Interface", (
        ("watermark", "Watermark"),
        ("accent", "Accent"),
    )),
)


class Settings:
    """User-toggleable ESP feature flags and colours, controlled by the menu."""

    def __init__(self):
        self.show_boxes = True
        self.show_skeleton = True
        self.show_tracers = False
        self.show_health = True
        self.show_name = True
        self.show_distance = True
        self.show_held_item = True
        self.show_target_belt = True
        self.show_target_fov = False
        self.target_fov_radius = 180.0
        self.full_inventory_key_code = 0xBC  # VK_OEM_COMMA (',')
        self.show_watermark = False
        self.show_world_entities = False
        # Derive the box from the projected skeleton when one is available.
        # See calculate_bone_box for why this removes the box/skeleton drift.
        self.box_from_bones = True
        # Range used by the model to admit a player rig into the one-batch
        # skeleton sampler. Boxes and names are unaffected by this value.
        self.skeleton_render_distance = 1000.0
        # The skeleton is the rendered pose; the prediction that shifts it is
        # derived from the networked position. Off = draw bones exactly where
        # they were sampled. See [RENDER] pred=.
        self.extrapolate_skeleton = False
        self.world_types = {
            "Ore": True,
            "Hemp": True,
            "Item": True,
            "Bag": True,
            "Crate": True,
            "TC": True,
        }
        self.colors = dict(DEFAULT_PALETTE)
        self._packed = {
            key: _pack_abgr(rgba) for key, rgba in self.colors.items()
        }
        # ── Aim assist (write-angles mode) ──
        # Writes bodyAngles directly to the local PlayerInput in memory.
        # No mouse injection — one 8-byte write, frame-perfect aim.
        self.aim_enabled = False       # master toggle
        self.aim_key = 0x02            # 0x02 RMB, 0x12 Alt, 0x04 MMB, ...
        self.aim_head_bone_id = 53     # 53 = head, 52 = neck, 23 = chest
        self.aim_interval_ms = 10      # min ms between two writes (can be very low)
        self.aim_debug_prints = False  # print [AIM-WRITE] each fire
        # World-space FOV cone (degrees). Only targets within this angular
        # distance from the crosshair centre are considered.
        self.aim_fov_deg = 15.0
        # Smoothing factor. 1.0 = instant snap (teleport to target), lower
        # values interpolate between current and target angles each frame.
        # 0.3 is a smooth glide, 0.5 is responsive, 1.0 is pixel-perfect lock.
        self.aim_smooth = 0.35
        # Deadzone in degrees. Skip writes when the angular distance to the
        # target is smaller than this, to avoid sub-pixel hunting jitter.
        self.aim_deadzone_deg = 0.15
        # Compensate for gravity drop + target lead on projectile weapons
        # (bow/crossbow/nailgun) instead of a straight-line aim point.
        # Hitscan weapons are unaffected either way. See
        # aim_engine.PROJECTILE_TABLE for the per-weapon speed/gravity used.
        self.aim_projectile_lead = True
        # Base gravity for the ballistics solve, in m/s^2. Multiplied by the
        # projectile's own gravityModifier (read live). Unity's default is
        # 9.81 and Physics.gravity is a native extern property with no
        # readable offset, so this is the one assumed value in the chain --
        # exposed here because it scales the whole drop linearly.
        self.aim_gravity = 9.81
        # Smoothing time constant (seconds) for the target velocity used to
        # lead the shot. The raw per-tick estimate swings by 2+ m/s and flips
        # direction; a bow's ~0.75 s flight turns that into metres of lead
        # jitter. 0 = raw (jittery), higher = steadier but slower to react to
        # a genuine change of direction.
        #
        # Kept SHORT on purpose. A long tau steadies a strafing target but
        # lags a genuine sprint by the same amount -- at 0.60 s a player
        # running in a straight line was led short for the whole run, which
        # is exactly the preshot case. The jitter is handled by the coherence
        # gate in VelocitySmoother instead, which costs no response time.
        self.aim_lead_smooth_s = 0.15
        # Steer our own arrows/bolts/nails onto the target while they fly.
        # OFF by default and deliberately so: unlike everything else here it
        # writes state the SERVER re-simulates, and a projectile that leaves
        # the trajectory it was launched on is a logged violation, not just a
        # shot that fails to register. The turn cap is the whole safety
        # margin -- a gentle correction stays inside the tolerance the server
        # allows for sway and movement, a hard turn does not.
        self.aim_projectile_homing = False
        self.aim_homing_turn_dps = 60.0

        # ── Anti-detection / secure writes ──
        # Dual-write: write bodyAngles + headAngles simultaneously.
        # Eliminates the 1-frame desync between PlayerInput and PlayerEyes
        # that server-side integrity checks can flag.
        self.aim_dual_write = True
        # Humanization (optional — turn OFF for ragebot)
        self.aim_humanize = False
        # Max angular speed in degrees/second (0 = unlimited).
        # Human flick speed peaks ~300-500°/s, sustained tracking ~80-150°/s.
        self.aim_max_speed_dps = 300.0
        # Gaussian noise radius in degrees added to the target point.
        # Simulates mouse tremor / imprecision. 0 = pixel-perfect.
        self.aim_jitter_deg = 0.4
        # Random write skip (0.0–1.0). Fraction of ticks where we DON'T
        # write even though we could — breaks the constant-rate pattern.
        self.aim_skip_pct = 0.08
        # Target stickiness in ms. Don't switch targets for at least this
        # long. Prevents inhuman instant target switching.
        self.aim_sticky_ms = 350

        # ── Chams (Material Override) ──
        self.chams_enabled = False
        self.chams_material_id = 116322  # default: GreenGlow
        self.chams_apply_enemies = True  # Apply to enemy players
        self.chams_apply_local = False   # Apply to local player (often bugs out arms)
        self.chams_apply_held = False    # Apply to held items/weapons

        # ── No Recoil ──
        self.norecoil_enabled = False
        self.norecoil_x = 0    # 0 = full no recoil (yaw), 100 = vanilla
        self.norecoil_y = 0    # 0 = full no recoil (pitch), 100 = vanilla

        # ── No Sway / No Bloom ──
        self.nosway_enabled = False


    def col(self, key):
        """Packed ABGR word for a palette key — cheap enough to call per draw."""
        return self._packed.get(key, COL_WHITE)

    def set_col(self, key, rgba):
        rgba = tuple(float(c) for c in rgba)
        if self.colors.get(key) == rgba:
            return
        self.colors[key] = rgba
        self._packed[key] = _pack_abgr(rgba)

    def reset_colors(self):
        for key, rgba in DEFAULT_PALETTE.items():
            self.set_col(key, rgba)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

def get_game_viewport():
    """Detect game window/client rect and compute stretch scaling for any resolution (16:9, 4:3, 16:10, windowed)."""
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, "Rust")
    screen_w = int(user32.GetSystemMetrics(0))
    screen_h = int(user32.GetSystemMetrics(1))
    if screen_w <= 0 or screen_h <= 0:
        screen_w, screen_h = 1920, 1080

    if hwnd:
        rect = RECT()
        pt = POINT(0, 0)
        if user32.GetClientRect(hwnd, ctypes.byref(rect)) and user32.ClientToScreen(hwnd, ctypes.byref(pt)):
            gw = rect.right - rect.left
            gh = rect.bottom - rect.top
            if gw > 100 and gh > 100:
                if pt.x <= 0 and pt.y <= 0 and (gw != screen_w or gh != screen_h):
                    scale_x = float(screen_w) / float(gw)
                    scale_y = float(screen_h) / float(gh)
                    return 0.0, 0.0, float(gw), float(gh), scale_x, scale_y, float(screen_w), float(screen_h)
                else:
                    return float(pt.x), float(pt.y), float(gw), float(gh), 1.0, 1.0, float(screen_w), float(screen_h)

    return 0.0, 0.0, float(screen_w), float(screen_h), 1.0, 1.0, float(screen_w), float(screen_h)


def world_to_screen(pos, vp, game_w, game_h, game_x=0.0, game_y=0.0, scale_x=1.0, scale_y=1.0):
    x = pos[0] * vp[0] + pos[1] * vp[4] + pos[2] * vp[8] + vp[12]
    y = pos[0] * vp[1] + pos[1] * vp[5] + pos[2] * vp[9] + vp[13]
    w = pos[0] * vp[3] + pos[1] * vp[7] + pos[2] * vp[11] + vp[15]
    if w < 0.001:
        return None
    proj_x = (game_w * 0.5) * (1.0 + x / w)
    proj_y = (game_h * 0.5) * (1.0 - y / w)
    if (
        proj_x < -300.0
        or proj_x > game_w + 300.0
        or proj_y < -300.0
        or proj_y > game_h + 300.0
    ):
        return None
    screen_x = game_x + proj_x * scale_x
    screen_y = game_y + proj_y * scale_y
    return screen_x, screen_y


def compute_aim_angles(cam_pos, cam_right, cam_up, cam_forward, target_world):
    """Compute yaw / pitch deltas (degrees) to rotate the camera toward
    `target_world` from its current pose.

    Inputs are all 3-tuples in world coordinates. The three basis vectors
    must be unit-length and mutually orthogonal (they come from a Unity
    view-matrix inverse, which guarantees that).

    Returns (yaw_deg, pitch_deg, distance) :
      - yaw_deg   : positive = target to the RIGHT of camera forward
                    (=> camera must yaw right => mouse dx positive)
      - pitch_deg : positive = target ABOVE camera forward
                    (=> camera must pitch up => mouse dy NEGATIVE, since
                     mouse-Y-down = pitch-down in Rust's standard config)
      - distance  : world distance camera -> target (metres)

    Returns None if target coincides with the camera (undefined direction).

    Why atan2 and not asin for pitch : both work when |along_up| < 1, but
    atan2 stays numerically stable at the poles (straight up / down) where
    asin(1.0) can blow up on rounding, and it gives a signed result that
    matches yaw's convention without extra branching.
    """
    dx = target_world[0] - cam_pos[0]
    dy = target_world[1] - cam_pos[1]
    dz = target_world[2] - cam_pos[2]
    dist = math.sqrt(dx * dx + dy * dy + dz * dz)
    if dist < 1e-6:
        return None
    inv = 1.0 / dist
    ux, uy, uz = dx * inv, dy * inv, dz * inv

    along_right = ux * cam_right[0] + uy * cam_right[1] + uz * cam_right[2]
    along_up = ux * cam_up[0] + uy * cam_up[1] + uz * cam_up[2]
    along_forward = ux * cam_forward[0] + uy * cam_forward[1] + uz * cam_forward[2]

    yaw_rad = math.atan2(along_right, along_forward)
    horiz = math.sqrt(along_right * along_right + along_forward * along_forward)
    pitch_rad = math.atan2(along_up, horiz)

    return (math.degrees(yaw_rad), math.degrees(pitch_rad), dist)


def select_aim_target(players, vp, viewport, fov_px, head_bone_id=53):
    """Pick the player whose head bone is closest to the crosshair.

    Returns (best_player, (head_screen_x, head_screen_y), delta_px) or None.
    - `viewport` : tuple (gx, gy, gw, gh, sx, sy, sw, sh) from get_game_viewport()
    - `fov_px`   : max distance (in pixels) from crosshair to consider a target
    - `head_bone_id` : SCI_BONE_IDS entry — 53 = head, 23 = neck (chest fallback)

    Sleeping players are skipped. If a player has no head bone (occluded rig)
    we fall back to their 3D `pos` shifted 1.6 m up (approximate head height).
    """
    if not players or not vp or not viewport:
        return None
    gx, gy, gw, gh, sx, sy, _, _ = viewport
    cx = gx + gw * 0.5 * sx
    cy = gy + gh * 0.5 * sy
    fov_sq = fov_px * fov_px

    best = None
    best_dsq = fov_sq
    best_screen = None

    for p in players:
        if p.get("sleeping"):
            continue
        head_world = None
        bones = p.get("bones")
        if bones and head_bone_id in bones:
            head_world = bones[head_bone_id]
        else:
            pos = p.get("pos")
            if pos is None:
                continue
            head_world = (pos[0], pos[1] + 1.6, pos[2])

        screen = world_to_screen(head_world, vp, gw, gh, gx, gy, sx, sy)
        if screen is None:
            continue
        dx = screen[0] - cx
        dy = screen[1] - cy
        dsq = dx * dx + dy * dy
        if dsq < best_dsq:
            best_dsq = dsq
            best = p
            best_screen = screen

    if best is None:
        return None
    return best, best_screen, (best_screen[0] - cx, best_screen[1] - cy)


def calculate_player_box(world_pos, vp, game_w, game_h, game_x=0.0, game_y=0.0, scale_x=1.0, scale_y=1.0, player_height=1.75):
    feet = world_to_screen(world_pos, vp, game_w, game_h, game_x, game_y, scale_x, scale_y)
    head_world = (
        world_pos[0],
        world_pos[1] + player_height,
        world_pos[2],
    )
    head = world_to_screen(head_world, vp, game_w, game_h, game_x, game_y, scale_x, scale_y)
    if feet is None and head is None:
        return None

    if feet is not None and head is not None:
        box_height = abs(feet[1] - head[1])
        center_x = (feet[0] + head[0]) * 0.5
        top = min(feet[1], head[1])
    elif feet is not None:
        center_x, feet_y = feet
        box_height = 30.0 * scale_y
        top = feet_y - box_height
    else:
        center_x, top = head
        box_height = 30.0 * scale_y

    box_height = max(14.0 * scale_y, min(box_height, 400.0 * scale_y))
    box_width = max(8.0 * scale_x, min(box_height * 0.45, 200.0 * scale_x))
    half_width = box_width * 0.5
    return (
        center_x - half_width,
        top,
        center_x + half_width,
        top + box_height,
    )


def calculate_sleeping_box(world_pos, vp, game_w, game_h, game_x=0.0, game_y=0.0, scale_x=1.0, scale_y=1.0, player_height=1.75):
    standing_box = calculate_player_box(
        world_pos,
        vp,
        game_w,
        game_h,
        game_x,
        game_y,
        scale_x,
        scale_y,
        player_height,
    )
    feet = world_to_screen(world_pos, vp, game_w, game_h, game_x, game_y, scale_x, scale_y)
    if standing_box is None or feet is None:
        return standing_box

    standing_height = max(14.0 * scale_y, standing_box[3] - standing_box[1])
    body_width = max(12.0 * scale_x, min(standing_height * 0.95, 320.0 * scale_x))
    body_height = max(6.0 * scale_y, min(standing_height * 0.28, 90.0 * scale_y))
    center_x, ground_y = feet
    bottom = ground_y + body_height * 0.12
    return (
        center_x - body_width * 0.5,
        bottom - body_height,
        center_x + body_width * 0.5,
        bottom,
    )


def calculate_bone_box(
    bones, vp, game_w, game_h, game_x=0.0, game_y=0.0,
    scale_x=1.0, scale_y=1.0, min_bones=3,
):
    """Screen-space bounding box of a player's projected skeleton.

    The world-position box and the skeleton come from two different sources
    that disagree while a player is moving: the box is built from
    ``PlayerModel+0x2F8``, the *networked* position, while the bones come from
    the Unity transform hierarchy, i.e. the pose actually being rendered. The
    client interpolates the visible model towards the networked position, so
    during movement the two are metres apart — which is exactly the "box and
    bones lag behind the player" symptom. Deriving the box from the bones puts
    both on the same source, so they can no longer drift apart and both sit on
    the model as drawn.

    It also fixes the box shape for free: a projected-height box assumes a
    standing player, while this one follows crouching, prone and vehicles.

    Returns ``(left, top, right, bottom)``, or ``None`` when too few bones
    project on screen to trust the result — the caller should fall back to
    calculate_player_box then.
    """
    if not bones:
        return None
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    count = 0
    for world_pos in bones.values():
        screen = world_to_screen(
            world_pos, vp, game_w, game_h, game_x, game_y, scale_x, scale_y
        )
        if screen is None:
            continue
        x, y = screen
        if x < min_x:
            min_x = x
        if x > max_x:
            max_x = x
        if y < min_y:
            min_y = y
        if y > max_y:
            max_y = y
        count += 1

    if count < min_bones:
        return None

    width = max_x - min_x
    height = max_y - min_y
    if height < 3.0 or height > game_h * 4.0 or width > game_w * 2.0:
        return None

    # The rig stops at the bone centres: pad out to roughly where the mesh is.
    pad_x = max(width * 0.30, 3.0 * scale_x)
    pad_top = max(height * 0.10, 4.0 * scale_y)
    pad_bottom = max(height * 0.03, 2.0 * scale_y)
    return (
        min_x - pad_x,
        min_y - pad_top,
        max_x + pad_x,
        max_y + pad_bottom,
    )


def _text_size(text):
    size = imgui.calc_text_size(str(text))
    try:
        return float(size.x), float(size.y)
    except AttributeError:
        return float(size[0]), float(size[1])


def _draw_outlined_text(draw_list, x, y, color, text):
    text = str(text)
    for dx, dy in ((-1.0, 0.0), (1.0, 0.0), (0.0, -1.0), (0.0, 1.0)):
        draw_list.add_text((x + dx, y + dy), 0xE8000000, text)
    draw_list.add_text((x, y), color, text)


def _draw_centered_text(draw_list, center_x, y, color, text):
    text_width, _ = _text_size(text)
    _draw_outlined_text(
        draw_list,
        center_x - text_width * 0.5,
        y,
        color,
        text,
    )


def _health_color(ratio):
    """Return an ABGR color interpolated green→yellow→red for a 0..1 health ratio."""
    ratio = max(0.0, min(1.0, ratio))
    if ratio > 0.5:
        # green → yellow (R increases, G stays)
        t = (ratio - 0.5) * 2.0
        r = int(255 * (1.0 - t))
        g = 255
    else:
        # yellow → red (G decreases, R stays)
        t = ratio * 2.0
        r = 255
        g = int(255 * t)
    return 0xFF000000 | (g << 8) | r  # ABGR: A=FF, B=00, G=g, R=r


def _draw_health_bar(draw_list, left, top, bottom, hp, max_hp):
    """Draw a thin vertical health bar just to the left of a player box."""
    if max_hp <= 0.0 or hp < 0.0:
        return
    bar_width = 3.0
    bar_gap = 3.0
    bar_height = max(4.0, bottom - top)
    bar_left = left - bar_gap - bar_width
    bar_top = top
    # Background
    draw_list.add_rect_filled(
        (bar_left - 1.0, bar_top - 1.0),
        (bar_left + bar_width + 1.0, bar_top + bar_height + 1.0),
        COL_HEALTH_BG,
    )
    # Fill from bottom up
    ratio = min(1.0, hp / max_hp)
    fill_height = bar_height * ratio
    fill_top = bar_top + bar_height - fill_height
    color = _health_color(ratio)
    draw_list.add_rect_filled(
        (bar_left, fill_top),
        (bar_left + bar_width, bar_top + bar_height),
        color,
    )


def _draw_corner_box(draw_list, left, top, right, bottom, color):
    width = max(0.0, right - left)
    height = max(0.0, bottom - top)
    corner_w = max(2.0, min(18.0, width * 0.26))
    corner_h = max(2.0, min(18.0, height * 0.26))
    segments = (
        ((left, top), (left + corner_w, top)),
        ((left, top), (left, top + corner_h)),
        ((right, top), (right - corner_w, top)),
        ((right, top), (right, top + corner_h)),
        ((left, bottom), (left + corner_w, bottom)),
        ((left, bottom), (left, bottom - corner_h)),
        ((right, bottom), (right - corner_w, bottom)),
        ((right, bottom), (right, bottom - corner_h)),
    )
    for first, second in segments:
        draw_list.add_line(first, second, 0xD8000000, 3.0)
    for first, second in segments:
        draw_list.add_line(first, second, color, 1.25)


def _draw_player_skeleton(draw_list, bones, vp, game_w, game_h, game_x=0.0, game_y=0.0, scale_x=1.0, scale_y=1.0, color=COL_ACCENT, distance=None):
    """Draw one already body-anchored skeleton using the current camera.

    `distance` is accepted and ignored: thickness now comes from the
    skeleton's own projected height, which already carries distance, field
    of view and resolution. Kept in the signature because callers pass it
    positionally, and because it is still what the *fade* would key on if
    one is ever added.
    """
    if not bones:
        return False
    screen_bones = {}
    top = bottom = None
    for bone_id, world_pos in bones.items():
        screen_pos = world_to_screen(world_pos, vp, game_w, game_h, game_x, game_y, scale_x, scale_y)
        if screen_pos is not None:
            screen_bones[bone_id] = screen_pos
            y = screen_pos[1]
            if top is None or y < top:
                top = y
            if bottom is None or y > bottom:
                bottom = y

    span_px = (bottom - top) if (top is not None and bottom is not None) else 0.0
    line_px, head_inner_px, outline_px = _skeleton_thickness(span_px)
    head_outer_px = head_inner_px + SKELETON_OUTLINE_EXTRA_PX
    # The data path already has all 21 joints at this point. Do not replace
    # the body with a spine-only LOD at range: it makes a healthy skeleton
    # look partially missing even though BONE-DBG reports every joint valid.
    links = SKELETON_LINKS

    drawn = False
    for first_id, second_id in links:
        first = screen_bones.get(first_id)
        second = screen_bones.get(second_id)
        if first is None or second is None:
            continue
        dx = first[0] - second[0]
        dy = first[1] - second[1]
        if dx * dx + dy * dy > 250000.0:
            continue
        draw_list.add_line(first, second, 0xB8000000, outline_px)
        draw_list.add_line(first, second, color, line_px)
        drawn = True

    head = screen_bones.get(53)
    if head is not None:
        draw_list.add_circle_filled(head, head_outer_px, 0xC8000000, 12)
        draw_list.add_circle_filled(head, head_inner_px, color, 12)
        drawn = True
    return drawn


def _screen_size():
    _, _, _, _, _, _, screen_w, screen_h = get_game_viewport()
    return int(screen_w), int(screen_h)


def _monitor_refresh_rate(default=144):
    try:
        monitor = glfw.get_primary_monitor()
        mode = glfw.get_video_mode(monitor) if monitor else None
        refresh = int(getattr(mode, "refresh_rate", 0)) if mode else 0
        if 30 <= refresh <= 500:
            return refresh
    except Exception:
        pass
    return default


# Base palette adapted from "Moonlight" (deathsu/madam-herta,
# github.com/Madam-Herta/Moonlight) — a dark navy Dear ImGui theme with
# rounded frames. Its yellow-green accent (CheckMark/SliderGrab/etc.) is
# swapped for this project's own cyan (#00D4FF, see COL_ACCENT) so the menu
# matches the boxes/skeleton drawn on top of it; layout metrics (padding,
# rounding, spacing) are retuned smaller so a checkbox-heavy settings list
# stays compact instead of Moonlight's wide button-oriented spacing.
_ACCENT = (0.0, 0.831, 1.0)


def _rgba(r, g, b, a=1.0):
    return imgui.ImVec4(r, g, b, a)


def _apply_style():
    style = imgui.get_style()

    style.alpha = 1.0
    style.disabled_alpha = 0.6
    style.window_padding = imgui.ImVec2(14.0, 14.0)
    style.window_rounding = 8.0
    style.window_border_size = 1.0
    style.window_min_size = imgui.ImVec2(20.0, 20.0)
    style.window_title_align = imgui.ImVec2(0.5, 0.5)
    style.child_rounding = 6.0
    style.child_border_size = 1.0
    style.popup_rounding = 6.0
    style.popup_border_size = 1.0
    style.frame_padding = imgui.ImVec2(8.0, 5.0)
    style.frame_rounding = 6.0
    style.frame_border_size = 0.0
    style.item_spacing = imgui.ImVec2(10.0, 10.0)
    style.item_inner_spacing = imgui.ImVec2(8.0, 6.0)
    style.indent_spacing = 18.0
    style.scrollbar_size = 12.0
    style.scrollbar_rounding = 9.0
    style.grab_min_size = 10.0
    style.grab_rounding = 6.0
    style.tab_rounding = 6.0
    style.button_text_align = imgui.ImVec2(0.5, 0.5)

    c = _ACCENT
    colors = {
        imgui.Col_.text: _rgba(1.0, 1.0, 1.0, 1.0),
        imgui.Col_.text_disabled: _rgba(0.42, 0.46, 0.54, 1.0),
        imgui.Col_.window_bg: _rgba(0.063, 0.071, 0.086, 1.0),
        imgui.Col_.child_bg: _rgba(0.078, 0.086, 0.102, 1.0),
        imgui.Col_.popup_bg: _rgba(0.063, 0.071, 0.086, 0.98),
        imgui.Col_.border: _rgba(0.14, 0.16, 0.19, 1.0),
        imgui.Col_.border_shadow: _rgba(0.0, 0.0, 0.0, 0.0),
        imgui.Col_.frame_bg: _rgba(0.11, 0.13, 0.16, 1.0),
        imgui.Col_.frame_bg_hovered: _rgba(*c, 0.20),
        imgui.Col_.frame_bg_active: _rgba(*c, 0.32),
        imgui.Col_.title_bg: _rgba(0.043, 0.05, 0.063, 1.0),
        imgui.Col_.title_bg_active: _rgba(0.0, 0.10, 0.13, 1.0),
        imgui.Col_.title_bg_collapsed: _rgba(0.043, 0.05, 0.063, 1.0),
        imgui.Col_.menu_bar_bg: _rgba(0.078, 0.086, 0.102, 1.0),
        imgui.Col_.scrollbar_bg: _rgba(0.043, 0.05, 0.063, 1.0),
        imgui.Col_.scrollbar_grab: _rgba(0.16, 0.18, 0.22, 1.0),
        imgui.Col_.scrollbar_grab_hovered: _rgba(*c, 0.45),
        imgui.Col_.scrollbar_grab_active: _rgba(*c, 0.65),
        imgui.Col_.check_mark: _rgba(*c, 1.0),
        imgui.Col_.slider_grab: _rgba(*c, 0.85),
        imgui.Col_.slider_grab_active: _rgba(*c, 1.0),
        imgui.Col_.button: _rgba(0.13, 0.15, 0.19, 1.0),
        imgui.Col_.button_hovered: _rgba(*c, 0.35),
        imgui.Col_.button_active: _rgba(*c, 0.55),
        imgui.Col_.header: _rgba(*c, 0.18),
        imgui.Col_.header_hovered: _rgba(*c, 0.32),
        imgui.Col_.header_active: _rgba(*c, 0.45),
        imgui.Col_.separator: _rgba(0.16, 0.18, 0.22, 1.0),
        imgui.Col_.separator_hovered: _rgba(*c, 0.6),
        imgui.Col_.separator_active: _rgba(*c, 1.0),
        imgui.Col_.resize_grip: _rgba(*c, 0.15),
        imgui.Col_.resize_grip_hovered: _rgba(*c, 0.6),
        imgui.Col_.resize_grip_active: _rgba(*c, 0.9),
        imgui.Col_.tab: _rgba(0.078, 0.086, 0.102, 1.0),
        imgui.Col_.tab_hovered: _rgba(*c, 0.4),
        imgui.Col_.tab_selected: _rgba(*c, 0.35),
        imgui.Col_.tab_dimmed: _rgba(0.078, 0.086, 0.102, 1.0),
        imgui.Col_.tab_dimmed_selected: _rgba(*c, 0.22),
        imgui.Col_.plot_lines: _rgba(0.52, 0.60, 0.70, 1.0),
        imgui.Col_.plot_lines_hovered: _rgba(*c, 1.0),
        imgui.Col_.plot_histogram: _rgba(*c, 0.75),
        imgui.Col_.plot_histogram_hovered: _rgba(*c, 1.0),
        imgui.Col_.table_header_bg: _rgba(0.043, 0.05, 0.063, 1.0),
        imgui.Col_.table_border_strong: _rgba(0.043, 0.05, 0.063, 1.0),
        imgui.Col_.table_border_light: _rgba(0.0, 0.0, 0.0, 1.0),
        imgui.Col_.table_row_bg: _rgba(0.11, 0.13, 0.16, 1.0),
        imgui.Col_.table_row_bg_alt: _rgba(0.098, 0.106, 0.122, 1.0),
        imgui.Col_.text_selected_bg: _rgba(*c, 0.35),
        imgui.Col_.drag_drop_target: _rgba(*c, 1.0),
        imgui.Col_.nav_cursor: _rgba(*c, 1.0),
        imgui.Col_.nav_windowing_highlight: _rgba(*c, 1.0),
        imgui.Col_.nav_windowing_dim_bg: _rgba(0.05, 0.05, 0.07, 0.5),
        imgui.Col_.modal_window_dim_bg: _rgba(0.0, 0.0, 0.0, 0.55),
    }
    for idx, color in colors.items():
        style.set_color_(idx, color)


class OverlayView:
    """Own the overlay window and render immutable model snapshots."""

    def __init__(self):
        if GUI_IMPORT_ERROR is not None:
            raise OverlayDependencyError(str(GUI_IMPORT_ERROR))
        if not glfw.init():
            raise OverlayDependencyError("glfw.init() a échoué")

        self.width, self.height = _screen_size()
        glfw.window_hint(glfw.DECORATED, 0)
        glfw.window_hint(glfw.TRANSPARENT_FRAMEBUFFER, 1)
        glfw.window_hint(glfw.FLOATING, 1)
        self.window = glfw.create_window(
            self.width,
            self.height,
            "Rust ESP",
            None,
            None,
        )
        if not self.window:
            glfw.terminate()
            raise OverlayDependencyError("création de la fenêtre impossible")

        glfw.set_window_pos(self.window, 0, 0)
        glfw.make_context_current(self.window)
        self.refresh_hz = _monitor_refresh_rate()
        glfw.swap_interval(1)

        self._user32 = ctypes.windll.user32
        self._hwnd = glfw.get_win32_window(self.window)
        self._set_click_through(True)

        imgui.create_context()
        _apply_style()
        self.renderer = GlfwRenderer(self.window)
        self._last_fps_at = time.perf_counter()
        self._last_console_at = self._last_fps_at
        self._fps_frames = 0
        self._fps = 0.0

        # Per-frame render-loop cost, split by phase, accumulated between the
        # throttled [RENDER-PROFILE] prints. Answers "is it the player loop
        # or the world-entity loop" without guessing -- the world-entity
        # driver reads already happen off-tick (see _run_slow_lane), so any
        # cost here is pure per-frame iterate+project of the cached list,
        # paid at display refresh rate (up to 144 Hz) rather than tick rate.
        self._players_render_ms_sum = 0.0
        self._players_render_ms_max = 0.0
        self._players_render_n = 0
        self._we_render_ms_sum = 0.0
        self._we_render_ms_max = 0.0
        self._we_render_n = 0

        self.settings = Settings()
        self.menu_open = False
        self._menu_key_was_down = False

    def _set_click_through(self, click_through):
        """Toggle whether mouse clicks pass through to the game underneath.

        The overlay is always WS_EX_LAYERED (needed for the transparent
        framebuffer); WS_EX_TRANSPARENT is what makes it click-through.
        Dropped while the menu is open so it can receive mouse input.
        """
        style = self._user32.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
        style |= WS_EX_LAYERED
        if click_through:
            style |= WS_EX_TRANSPARENT
        else:
            style &= ~WS_EX_TRANSPARENT
        self._user32.SetWindowLongW(self._hwnd, GWL_EXSTYLE, style)

    def should_close(self):
        return glfw.window_should_close(self.window)

    def poll(self):
        glfw.poll_events()
        self.renderer.process_inputs()
        # GetAsyncKeyState is a *global* key query — it works no matter which
        # window has focus. glfw.get_key() only reports keys the overlay
        # window itself received, which needs the game to NOT have focus
        # (i.e. clicking the overlay first) since the overlay is click-through
        # and never gets focus on its own. This is what let the menu open
        # without touching the game window at all.
        menu_key_down = bool(self._user32.GetAsyncKeyState(VK_MENU_TOGGLE) & 0x8000)
        if menu_key_down and not self._menu_key_was_down:
            self.menu_open = not self.menu_open
            self._set_click_through(not self.menu_open)
        self._menu_key_was_down = menu_key_down

    @staticmethod
    def _distance(pos, local_pos, camera_pos, fallback):
        origin = local_pos if local_pos is not None else camera_pos
        if origin is None:
            return fallback
        dx = pos[0] - origin[0]
        dy = pos[1] - origin[1]
        dz = pos[2] - origin[2]
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    def render(
        self,
        players,
        vp,
        local_pos,
        camera_pos,
        camera_age_ms,
        diag,
        tick_ms,
    ):
        now = time.perf_counter()
        self._fps_frames += 1
        elapsed = now - self._last_fps_at
        if elapsed >= 1.0:
            self._fps = self._fps_frames / elapsed
            self._fps_frames = 0
            self._last_fps_at = now

        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        imgui.new_frame()
        imgui.set_next_window_pos((0, 0))
        imgui.set_next_window_size((self.width, self.height))
        imgui.push_style_color(imgui.Col_.window_bg, (0, 0, 0, 0))

        drawn = 0
        behind = 0
        with imgui_ctx.begin("##esp", None, ESP_WINDOW_FLAGS):
            draw_list = imgui.get_window_draw_list()
            draw_list.add_text(
                (10, 10),
                COL_WHITE,
                (
                    f"Rust ESP | {self._fps:.0f}fps | "
                    f"tick={tick_ms:.0f}ms | {diag}"
                ),
            )

            if vp and players:
                for player in players:
                    pos = player["pos"]
                    if player.get("sleeping", False):
                        box = calculate_sleeping_box(
                            pos,
                            vp,
                            self.width,
                            self.height,
                        )
                    else:
                        box = calculate_player_box(
                            pos,
                            vp,
                            self.width,
                            self.height,
                        )
                    if box is None:
                        behind += 1
                        continue

                    drawn += 1
                    left, top, right, bottom = box
                    _draw_corner_box(
                        draw_list,
                        left,
                        top,
                        right,
                        bottom,
                        COL_ACCENT,
                    )

                    distance = self._distance(
                        pos,
                        local_pos,
                        camera_pos,
                        player.get("dist", -1.0),
                    )
                    if distance >= 0.0:
                        text_y = (
                            bottom + 4.0
                            if bottom <= self.height - 20.0
                            else bottom - 16.0
                        )
                        _draw_centered_text(
                            draw_list,
                            (left + right) * 0.5,
                            text_y,
                            COL_WHITE,
                            f"{distance:.0f}m",
                        )

            draw_list.add_text(
                (10, 28),
                COL_WHITE,
                (
                    f"drawn={drawn} behind={behind} "
                    f"pl={len(players) if players else 0} "
                    f"vp={'yes' if vp else 'NO'} cam={camera_age_ms:.0f}ms"
                ),
            )

            if now - self._last_console_at >= 1.0:
                self._last_console_at = now
                vp_text = "None"
                if vp:
                    vp_text = (
                        f"[{vp[0]:.3f},{vp[5]:.3f},"
                        f"{vp[10]:.3f},{vp[15]:.3f}]"
                    )
                print(
                    f"[RENDER] drawn={drawn} behind={behind} "
                    f"pl={len(players) if players else 0} vp={vp_text}",
                    flush=True,
                )

        imgui.pop_style_color()
        imgui.render()
        self.renderer.render(imgui.get_draw_data())
        # swap_buffers() already blocks until the next vblank (swap_interval
        # = 1 above). A second, software sleep-based limiter on top of that
        # fights the GPU's own pacing — the two clocks drift against each
        # other (time.sleep() isn't vblank-accurate), which is what produced
        # the stutter/judder ("vsync effect") reported live. Let vsync alone
        # pace the loop.
        glfw.swap_buffers(self.window)

    def close(self):
        if getattr(self, "renderer", None) is not None:
            try:
                self.renderer.shutdown()
            except Exception:
                pass
        glfw.terminate()
