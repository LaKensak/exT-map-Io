import ctypes
import math
import os
import time

from imgui_bundle import imgui, imgui_ctx

try:
    import PIL.Image
    import OpenGL.GL as gl
except ImportError:
    PIL = None
    gl = None

from . import aternos_ui as ui
from . import view_base as base


OverlayDependencyError = base.OverlayDependencyError

_TEXTURE_CACHE = {}

_ITEMS_DIR_CANDIDATES = [
    r"F:\SteamLibrary\steamapps\common\Rust\Bundles\items",
    r"C:\Program Files (x86)\Steam\steamapps\common\Rust\Bundles\items",
    r"D:\SteamLibrary\steamapps\common\Rust\Bundles\items",
    r"E:\SteamLibrary\steamapps\common\Rust\Bundles\items",
    r"C:\Rust\Bundles\items",
    r"Rust\Bundles\items",
]

_ITEMS_DIR = None
for _d in _ITEMS_DIR_CANDIDATES:
    if os.path.exists(_d):
        _ITEMS_DIR = _d
        break


def get_item_texture(short_name):
    if not short_name or not _ITEMS_DIR or not PIL or not gl:
        return None
    key = str(short_name).strip().lower()
    if key in _TEXTURE_CACHE:
        return _TEXTURE_CACHE[key]

    candidates = [
        f"{key}.png",
        f"{key.replace(' ', '.')}.png",
        f"{key.replace(' ', '_')}.png",
        f"{key.replace('.', '_')}.png",
    ]
    img_path = None
    for cand in candidates:
        full_path = os.path.join(_ITEMS_DIR, cand)
        if os.path.exists(full_path):
            img_path = full_path
            break

    if not img_path:
        _TEXTURE_CACHE[key] = None
        return None

    try:
        img = PIL.Image.open(img_path).convert("RGBA")
        img = img.transpose(PIL.Image.FLIP_TOP_BOTTOM)
        img_data = img.tobytes("raw", "RGBA", 0, -1)
        w, h = img.size

        tex_id = gl.glGenTextures(1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexImage2D(
            gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, w, h, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, img_data
        )
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        _TEXTURE_CACHE[key] = tex_id
        return tex_id
    except Exception as exc:
        print(f"[TEXTURE-ERR] Failed loading {img_path}: {exc}", flush=True)
        _TEXTURE_CACHE[key] = None
        return None


def _tip(text):
    """Tooltip for the widget just drawn (aternos widgets register an item)."""
    if imgui.is_item_hovered():
        imgui.set_tooltip(text)


def _two_columns(left, right):
    """main.cpp's tab body: two edited::BeginChild columns side by side."""
    size = ui.Menu.column_size()
    ui.begin_child("##Container0", size)
    left()
    ui.end_child()
    imgui.same_line(0.0, 0.0)
    ui.begin_child("##Container1", size)
    right()
    ui.end_child()


# Target belt HUD, styled after Rust's own hotbar (measured off an in-game
# screenshot: ~72 px square slots, 4 px gaps, no border, translucent grey-beige
# fill, blue selected slot, green condition bar on the left edge, stack count
# bottom-right) -- drawn at ~55% of the game's size so it stays out of the way.
BELT_SLOT_PX = 40.0
BELT_SLOT_GAP_PX = 3.0
BELT_LABEL_FONT_PX = 13.0
NAME_FONT_PX = 11.0
_SLOT_BG = base._pack_abgr((0.78, 0.78, 0.72, 0.22))
_SLOT_SELECTED = base._pack_abgr((0.17, 0.40, 0.59, 0.88))
_SLOT_CONDITION = base._pack_abgr((0.44, 0.54, 0.26, 1.0))
_SLOT_COND_W = 3.0


def _draw_game_slot(draw_list, x, y, size, short_name, label, amount, cond, selected):
    """One belt slot in Rust's hotbar style."""
    draw_list.add_rect_filled((x, y), (x + size, y + size), _SLOT_SELECTED if selected else _SLOT_BG)
    if cond is not None:
        fill = size * max(0.0, min(1.0, cond))
        draw_list.add_rect_filled((x, y + size - fill), (x + _SLOT_COND_W, y + size), _SLOT_CONDITION)
    tex_id = get_item_texture(short_name) if short_name else None
    pad = 3.0
    if tex_id:
        draw_list.add_image(
            imgui.ImTextureRef(int(tex_id)),
            imgui.ImVec2(x + pad + 1.0, y + pad),
            imgui.ImVec2(x + size - pad + 1.0, y + size - pad),
            imgui.ImVec2(0.0, 0.0),
            imgui.ImVec2(1.0, 1.0),
            0xFFFFFFFF,
        )
    elif label:
        imgui.push_font(None, BELT_LABEL_FONT_PX)
        short_label = label if len(label) <= 6 else label[:5] + ".."
        _, th = base._text_size(short_label)
        base._draw_centered_text(draw_list, x + size * 0.5, y + (size - th) * 0.5, 0xFFFFFFFF, short_label)
        imgui.pop_font()
    if amount is not None and amount > 1:
        imgui.push_font(None, BELT_LABEL_FONT_PX)
        text = str(amount)
        tw, th = base._text_size(text)
        base._draw_outlined_text(draw_list, x + size - tw - 2.0, y + size - th - 1.0, 0xFFFFFFFF, text)
        imgui.pop_font()


INV_HEADER_PX = 13.0
INV_NAME_PX = 15.0


def _slot_fields(items, idx):
    """(short_name, label, amount, cond) for one slot of a belt/wear/main list."""
    item = items[idx] if idx < len(items) else None
    if isinstance(item, (tuple, list)):
        amount, cond = (item[2], item[3]) if len(item) >= 4 else (None, None)
        return item[0], item[1] or item[0], amount, cond
    if isinstance(item, str):
        return item, item, None, None
    return "", "", None, None


def _draw_header(draw_list, x, y, text, px, col=0xE6FFFFFF):
    """Bold section header like the game's inventory screen (drop shadow
    instead of the game's blurred backdrop, so it reads over the world)."""
    fnt = ui.F.lexend_general_bold
    if fnt is None:
        base._draw_outlined_text(draw_list, x, y, col, text)
        return
    draw_list.add_text(fnt.font, px, imgui.ImVec2(x + 1.0, y + 1.0), 0x99000000, text)
    draw_list.add_text(fnt.font, px, imgui.ImVec2(x, y), col, text)


class OverlayView(base.OverlayView):
    """Render boxes, distance, tracers, watermark and body-anchored skeletons."""

    def __init__(self):
        super().__init__()
        # The renderer honours RendererHasTextures (ImGui 1.92), so fonts can
        # be added after it exists. load_fonts keeps ImGui's default font as
        # font #0 for the ESP text; only the menu uses Lexend/icomoon.
        ui.load_fonts()

    def render(
        self,
        players,
        vp,
        local_pos,
        camera_pos,
        camera_age_ms,
        diag,
        tick_ms,
        world_entities=None,
    ):
        s = self.settings
        now = time.perf_counter()
        self._fps_frames += 1
        elapsed = now - self._last_fps_at
        if elapsed >= 1.0:
            self._fps = self._fps_frames / elapsed
            self._fps_frames = 0
            self._last_fps_at = now

        base.gl.glClear(base.gl.GL_COLOR_BUFFER_BIT)
        imgui.new_frame()
        imgui.set_next_window_pos((0, 0))
        imgui.set_next_window_size((self.width, self.height))
        imgui.push_style_color(imgui.Col_.window_bg, (0, 0, 0, 0))

        drawn = 0
        behind = 0
        skeletons = 0
        world_drawn = 0
        boxes_from_bones = 0
        # How far the render-side prediction is actually moving the skeleton.
        # If the box sits ahead of a running player, compare this against the
        # offset on screen before changing anything else.
        pred_sum = 0.0
        pred_n = 0
        gx, gy, gw, gh, sx, sy, _, _ = base.get_game_viewport()

        screen_cx = gx + gw * 0.5 * sx
        screen_cy = gy + gh * 0.5 * sy
        best_target = None

        with imgui_ctx.begin("##esp", None, base.ESP_WINDOW_FLAGS):
            draw_list = imgui.get_window_draw_list()

            # ── Top-Left Watermark Header (cl1kexternal style) ──
            if s.show_watermark:
                pl_count = len(players) if players else 0
                wm_text = f"Rust ESP MVC  |  {self._fps:.0f} FPS  |  {pl_count} Players"
                base._draw_outlined_text(
                    draw_list, 14.0, 12.0, s.col("watermark"), wm_text
                )
                text_w, _ = base._text_size(wm_text)
                draw_list.add_line(
                    (14.0, 30.0),
                    (14.0 + text_w, 30.0),
                    s.col("accent"),
                    2.0,
                )

            # ── Target Selection FOV Circle ──
            if getattr(s, "show_target_fov", False):
                fov_r = getattr(s, "target_fov_radius", 180.0)
                draw_list.add_circle(
                    (screen_cx, screen_cy),
                    fov_r,
                    0xAAFFCC00 if best_target else 0x5500D4FF,
                    64,
                    1.5,
                )

            if vp and players:
                t_players0 = time.perf_counter()
                screen_center_bottom = (gx + gw * 0.5 * sx, gy + gh * sy)
                fov_limit = getattr(s, "target_fov_radius", 180.0)
                best_target_dist_sq = fov_limit * fov_limit

                for player in players:
                    pos = player["pos"]
                    # Computed once here (was recomputed further down inside
                    # `if s.show_distance:`) so the skeleton-thickness scale
                    # has it too, regardless of whether the distance text is
                    # switched on.
                    distance = self._distance(
                        pos, local_pos, camera_pos, player.get("dist", -1.0)
                    )
                    bones = player.get("bones")
                    if bones and not s.extrapolate_skeleton:
                        shift = player.get("bone_shift")
                        if shift:
                            bones = {
                                bone_id: (
                                    bone[0] - shift[0],
                                    bone[1] - shift[1],
                                    bone[2] - shift[2],
                                )
                                for bone_id, bone in bones.items()
                            }
                    shift = player.get("bone_shift")
                    if shift is not None:
                        pred_sum += math.sqrt(
                            shift[0] * shift[0]
                            + shift[1] * shift[1]
                            + shift[2] * shift[2]
                        )
                        pred_n += 1

                    is_sleeping = player.get("sleeping", False)
                    # Scientist/zombie/bandit/scarecrow/pet/shopkeeper -- see
                    # legacy_runtime.NpcClassifier. Skipped entirely (not
                    # just recoloured) when the setting is off, same as a
                    # teammate filter would skip a teammate.
                    is_npc = player.get("is_npc", False)
                    if is_npc and not s.show_npcs:
                        continue
                    raw_box = None
                    # Prefer the skeleton's own screen bounds: the bones are the
                    # pose actually being rendered, while `pos` is the networked
                    # position the client is still interpolating towards, so a
                    # world-position box trails a moving player and drifts off
                    # its own skeleton. See base.calculate_bone_box.
                    if s.box_from_bones and bones and not is_sleeping:
                        raw_box = base.calculate_bone_box(
                            bones, vp, gw, gh, gx, gy, sx, sy, min_bones=3
                        )
                        if raw_box is not None:
                            boxes_from_bones += 1
                    if raw_box is None:
                        if is_sleeping:
                            raw_box = base.calculate_sleeping_box(
                                pos, vp, gw, gh, gx, gy, sx, sy
                            )
                        else:
                            raw_box = base.calculate_player_box(
                                pos, vp, gw, gh, gx, gy, sx, sy
                            )

                    if raw_box is None:
                        behind += 1
                        continue

                    # Draw the raw re-projected box every frame — no
                    # screen-space smoothing. A LERP here can't tell "camera
                    # rotated" from "player moved", so it dragged the box
                    # behind the (unsmoothed) tracer during fast pitch/yaw/
                    # roll, i.e. it re-introduced motion the camera's own VP
                    # update should already cancel out. The box now tracks
                    # the world position 1:1, same as the tracer, so it only
                    # visibly moves when the player actually does.
                    left, top, right, bottom = raw_box
                    drawn += 1
                    center_x = (left + right) * 0.5
                    center_y = (top + bottom) * 0.5

                    # Track crosshair target by 2D box center
                    if not is_sleeping:
                        cdx = center_x - screen_cx
                        cdy = center_y - screen_cy
                        cdist_sq = cdx * cdx + cdy * cdy
                        if cdist_sq < best_target_dist_sq:
                            best_target_dist_sq = cdist_sq
                            best_target = player

                    # ── Tracers (screen bottom to feet) ──
                    if s.show_tracers:
                        # Anchor the tracer on the box we just drew rather than
                        # re-projecting `pos`: with a bone-derived box those are
                        # two different sources, and the tracer would visibly
                        # miss the box while the player moves.
                        feet_screen = (center_x, bottom)
                        tracer_col = s.col(
                            "tracer_npc" if is_npc else
                            "tracer_sleeping" if is_sleeping else "tracer"
                        )
                        draw_list.add_line(
                            screen_center_bottom,
                            feet_screen,
                            tracer_col,
                            1.0,
                        )

                    # ── Corner Bounding Box ──
                    if s.show_boxes:
                        box_col = s.col(
                            "box_npc" if is_npc else
                            "box_sleeping" if is_sleeping else "box"
                        )
                        base._draw_corner_box(
                            draw_list, left, top, right, bottom, box_col
                        )

                    # ── Player name (above box) ──
                    name = player.get("name", "")
                    if is_npc:
                        name = f"{name} [NPC]" if name else "[NPC]"
                    if s.show_name and name:
                        name_col = s.col(
                            "name_npc" if is_npc else
                            "name_sleeping" if is_sleeping else "name"
                        )
                        imgui.push_font(None, NAME_FONT_PX)
                        base._draw_centered_text(
                            draw_list, center_x, top - NAME_FONT_PX - 3.0, name_col, name
                        )
                        imgui.pop_font()

                    # ── Body Skeleton ──
                    if s.show_skeleton and not is_sleeping and base._draw_player_skeleton(
                        draw_list,
                        bones,
                        vp,
                        gw,
                        gh,
                        gx,
                        gy,
                        sx,
                        sy,
                        s.col("skeleton_npc" if is_npc else "skeleton"),
                        distance,
                    ):
                        skeletons += 1

                    # ── Info below box: health bar, distance, held item ──
                    hp = player.get("hp", -1.0)
                    max_hp = player.get("max_hp", -1.0)
                    has_bar = s.show_health and max_hp > 0.0 and hp >= 0.0
                    bar_space = (
                        base.HEALTH_BAR_BELOW_HEIGHT + base.HEALTH_BAR_BELOW_GAP
                        if has_bar else 0.0
                    )
                    info_y = (
                        bottom + 4.0
                        if bottom <= self.height - 40.0 - bar_space
                        else bottom - 32.0 - bar_space
                    )
                    if has_bar:
                        info_y += base._draw_health_bar_below(
                            draw_list, center_x, info_y,
                            base._health_bar_below_width(left, right),
                            hp, max_hp,
                        )
                    if s.show_distance:
                        if distance >= 0.0:
                            base._draw_centered_text(
                                draw_list, center_x, info_y,
                                s.col("distance"), f"{distance:.0f}m",
                            )
                            info_y += 14.0

                    held_item = player.get("held_item", "")
                    if s.show_held_item and held_item:
                        base._draw_centered_text(
                            draw_list, center_x, info_y,
                            s.col("held_item"), held_item,
                        )

                players_ms = (time.perf_counter() - t_players0) * 1000.0
                self._players_render_ms_sum += players_ms
                self._players_render_ms_max = max(
                    self._players_render_ms_max, players_ms
                )
                self._players_render_n += 1

            # ── Target Inventory HUD (Wear, Main, Belt) ──
            if vp and getattr(s, "show_target_belt", True) and best_target:
                target_player = best_target
                t_name = target_player.get("name") or "Player"
                belt = target_player.get("belt", [])
                wear = target_player.get("wear", [])
                main_inv = target_player.get("main", [])
                held_name = target_player.get("held_item", "")

                key_code = getattr(s, "full_inventory_key_code", 0xBC)
                # Check comma key (0xBC) exclusively
                is_holding_key = bool(ctypes.windll.user32.GetAsyncKeyState(key_code) & 0x8000)

                # Rust's own hotbar fills the bottom ~90 px of a 1080p frame and its
                # UI scales with height, so both views keep clear of it by the same
                # scaled margin. The full view grows upward from the compact belt's
                # row: opening it never moves the belt, and it never reaches the game's.
                ui_scale = (gh * sy) / 1080.0
                belt_y = (gy + gh * sy) - 150.0 * ui_scale

                if is_holding_key:
                    # Full target inventory in the game's own inventory-screen
                    # style: no frame, bold uppercase headers, translucent
                    # square slots (same as the belt), 7 clothing slots, a 6x4
                    # main grid and the belt set apart underneath.
                    size = BELT_SLOT_PX
                    gap = BELT_SLOT_GAP_PX
                    cols = 6
                    # Each block is centred on its own width (7 clothing slots
                    # are wider than the 6-column grid), so the grid and belt
                    # line up with the compact belt instead of sitting 21 px
                    # left of it.
                    wear_left = screen_cx - (7 * size + 6 * gap) * 0.5
                    grid_left = screen_cx - (cols * size + (cols - 1) * gap) * 0.5
                    above_belt = (
                        INV_NAME_PX + 6.0 + INV_HEADER_PX + 4.0 + size + 10.0
                        + INV_HEADER_PX + 4.0 + 4 * size + 3 * gap + 10.0
                    )
                    y = belt_y - above_belt

                    _draw_header(draw_list, wear_left, y, t_name, INV_NAME_PX)
                    y += INV_NAME_PX + 6.0
                    _draw_header(draw_list, wear_left, y, "CLOTHING", INV_HEADER_PX)
                    y += INV_HEADER_PX + 4.0
                    for i in range(7):
                        short_name, label, amount, cond = _slot_fields(wear, i)
                        _draw_game_slot(
                            draw_list, wear_left + i * (size + gap), y, size,
                            short_name, label, amount, cond, False,
                        )
                    y += size + 10.0
                    _draw_header(draw_list, grid_left, y, "INVENTORY", INV_HEADER_PX)
                    y += INV_HEADER_PX + 4.0
                    for r in range(4):
                        for c in range(cols):
                            short_name, label, amount, cond = _slot_fields(main_inv, r * cols + c)
                            _draw_game_slot(
                                draw_list, grid_left + c * (size + gap), y + r * (size + gap), size,
                                short_name, label, amount, cond, False,
                            )
                    y += 4 * size + 3 * gap + 10.0
                    for i in range(cols):
                        short_name, label, amount, cond = _slot_fields(belt, i)
                        if not short_name and i == 0 and held_name:
                            short_name, label = held_name, held_name
                        is_held = bool(held_name) and held_name in (short_name, label)
                        _draw_game_slot(
                            draw_list, grid_left + i * (size + gap), y, size,
                            short_name, label, amount, cond, is_held,
                        )
                else:
                    # Compact target belt in Rust's own hotbar style -- see
                    # _draw_game_slot / BELT_SLOT_PX above.
                    slot_size = BELT_SLOT_PX
                    slot_gap = BELT_SLOT_GAP_PX
                    total_slots_w = 6 * slot_size + 5 * slot_gap
                    start_x = screen_cx - total_slots_w * 0.5
                    hud_y = belt_y

                    imgui.push_font(None, BELT_LABEL_FONT_PX)
                    base._draw_centered_text(
                        draw_list, screen_cx, hud_y - 26.0,
                        0xFF00D4FF, f"{t_name}"
                    )

                    imgui.pop_font()

                    for slot_i in range(6):
                        slot_item = belt[slot_i] if slot_i < len(belt) else None
                        short_name, disp_name, amount, cond = "", "", None, None
                        if isinstance(slot_item, (tuple, list)):
                            short_name, disp_name = slot_item[0], slot_item[1]
                            if len(slot_item) >= 4:
                                amount, cond = slot_item[2], slot_item[3]
                        elif isinstance(slot_item, str):
                            short_name, disp_name = slot_item, slot_item

                        if not short_name and slot_i == 0 and held_name:
                            short_name, disp_name = held_name, held_name

                        is_held = bool(
                            (disp_name and disp_name == held_name)
                            or (short_name and short_name == held_name)
                        )
                        _draw_game_slot(
                            draw_list, start_x + slot_i * (slot_size + slot_gap), hud_y,
                            slot_size, short_name, disp_name or short_name, amount, cond, is_held,
                        )
                self._players_render_n += 1

            # ── World entities (ores, hemp, bags, crates, TC) ──
            we_total = len(world_entities) if world_entities else 0
            we_toggled_off = 0
            we_no_pos = 0
            we_far = 0
            we_offscreen = 0
            if vp and world_entities and s.show_world_entities:
                t_we0 = time.perf_counter()
                ref = local_pos or camera_pos
                for ent in world_entities:
                    etype = ent.get("type", "")
                    if not s.world_types.get(etype, True):
                        we_toggled_off += 1
                        continue
                    epos = ent.get("pos")
                    if epos is None:
                        we_no_pos += 1
                        continue

                    max_dist = base.WORLD_ENTITY_MAX_DIST.get(etype, 200.0)
                    if ref is not None:
                        dx = epos[0] - ref[0]
                        dy = epos[1] - ref[1]
                        dz = epos[2] - ref[2]
                        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                        if dist > max_dist:
                            we_far += 1
                            continue
                    else:
                        dist = -1.0

                    screen = base.world_to_screen(
                        epos, vp, gw, gh, gx, gy, sx, sy
                    )
                    if screen is None:
                        we_offscreen += 1
                        continue

                    color = s.col("we_" + etype)
                    display = ent.get("name") or etype
                    label = (
                        f"{display} [{dist:.0f}m]" if dist >= 0.0 else display
                    )
                    base._draw_outlined_text(
                        draw_list, screen[0], screen[1], color, label
                    )
                    world_drawn += 1

                we_ms = (time.perf_counter() - t_we0) * 1000.0
                self._we_render_ms_sum += we_ms
                self._we_render_ms_max = max(self._we_render_ms_max, we_ms)
                self._we_render_n += 1

            if now - self._last_console_at >= 1.0:
                self._last_console_at = now
                vp_text = "None"
                if vp:
                    vp_text = (
                        f"[{vp[0]:.3f},{vp[5]:.3f},"
                        f"{vp[10]:.3f},{vp[15]:.3f}]"
                    )
                print(
                    f"[RENDER] drawn={drawn} sk={skeletons} "
                    f"boxbone={boxes_from_bones} "
                    f"vpage={camera_age_ms:.0f}ms "
                    f"pred={(pred_sum / pred_n) if pred_n else 0.0:.2f}m "
                    f"behind={behind} "
                    f"pl={len(players) if players else 0} "
                    f"we={world_drawn} vp={vp_text}",
                    flush=True,
                )
                print(
                    f"[WE-RENDER] vp={'y' if vp else 'n'} "
                    f"show_world={s.show_world_entities} total={we_total} "
                    f"off={we_toggled_off} no_pos={we_no_pos} far={we_far} "
                    f"offscreen={we_offscreen} drawn={world_drawn} "
                    f"local={'y' if local_pos else 'n'} cam={'y' if camera_pos else 'n'}",
                    flush=True,
                )
                # Per-frame render cost, split player-loop vs world-entity-loop,
                # averaged/maxed over the last second of frames (up to 144 of
                # them). world_entities is already read off-tick (see
                # _run_slow_lane in model.py) -- this measures the separate
                # cost of iterating + projecting that cached list every frame.
                p_n = self._players_render_n or 1
                w_n = self._we_render_n or 1
                print(
                    f"[RENDER-PROFILE] players={self._players_render_ms_sum / p_n:.2f}ms"
                    f"(max={self._players_render_ms_max:.2f}ms n={self._players_render_n}) "
                    f"we={self._we_render_ms_sum / w_n:.2f}ms"
                    f"(max={self._we_render_ms_max:.2f}ms n={self._we_render_n} "
                    f"count={we_total})",
                    flush=True,
                )
                self._players_render_ms_sum = 0.0
                self._players_render_ms_max = 0.0
                self._players_render_n = 0
                self._we_render_ms_sum = 0.0
                self._we_render_ms_max = 0.0
                self._we_render_n = 0

        imgui.pop_style_color()

        if self.menu_open:
            self._draw_menu()

        imgui.render()
        self.renderer.render(imgui.get_draw_data())
        # swap_buffers() already blocks until the next vblank (vsync). See
        # the matching note in view_base.py's render() — no extra sleep here.
        base.glfw.swap_buffers(self.window)

    # ── Menu: 1:1 port of the "imgui aternos" C++ template (aternos_ui.py) ──
    _MENU_TABS = (
        ("b", "Visuals", "Player & world ESP", "Visuals: player and world ESP."),
        ("c", "Aim", "Aim assist & targets", "Aim: assist, targeting, anti-detection."),
        ("o", "No Recoil", "Recoil compensation", "No Recoil: per-axis recoil scale."),
        ("f", "No Sway", "Sway & bloom removal", "No Sway: spread and sway removal."),
        ("j", "Chams", "Material override", "Chams: material override."),
        ("h", "Colors", "ESP palette", "Colors: every colour the ESP draws."),
        ("e", "Settings", "Menu customization", "Settings: accent and notifications."),
    )

    def _draw_menu(self):
        """INSERT-toggled menu: the aternos template (see aternos_ui.py)."""
        s = self.settings
        menu = getattr(self, "_aternos_menu", None)
        if menu is None:
            menu = self._aternos_menu = ui.Menu(self._MENU_TABS)
        tabs = (
            self._draw_feature_tab,
            self._draw_aim_tab,
            self._draw_norecoil_tab,
            self._draw_nosway_tab,
            self._draw_chams_tab,
            self._draw_color_tab,
            lambda st: self._draw_menu_settings_tab(st, menu),
        )
        menu.draw(lambda i: tabs[i](s))

    @staticmethod
    def _draw_feature_tab(s):
        def left():
            ui.section("Player ESP")
            _, s.show_boxes = ui.checkbox("Boxes", "Corner box around each player", s.show_boxes)
            _, s.show_skeleton = ui.checkbox("Skeleton", "Bones of the rendered pose", s.show_skeleton)
            _, s.show_tracers = ui.checkbox("Tracers", "Line from the screen bottom to the feet", s.show_tracers)
            _, s.show_health = ui.checkbox("Health bar", "Small bar under the player", s.show_health)
            _, s.show_name = ui.checkbox("Names", "Player name above the box", s.show_name)
            _, s.show_distance = ui.checkbox("Distance", "Range in metres under the player", s.show_distance)
            _, s.show_held_item = ui.checkbox("Held item", "What they have in hand", s.show_held_item)
            _, s.show_npcs = ui.checkbox("Show NPCs", "Scientists, zombies, bandits... in amber", s.show_npcs)
            _tip(
                "Scientists, zombies/murderers, bandit camp NPCs,\n"
                "scarecrows, pets, shopkeepers. Drawn in a distinct amber\n"
                "(see the 'NPCs' colour group) instead of the player cyan,\n"
                "so they no longer look like a real player with a name\n"
                "but no bones/hp. Off hides them entirely, same as a\n"
                "teammate would be hidden by a team filter."
            )

        def right():
            ui.section("Rendering")
            _, s.box_from_bones = ui.checkbox(
                "Box follows skeleton", "Box built from the rendered bones", s.box_from_bones
            )
            _tip(
                "Build the box from the rendered bones instead of the\n"
                "networked position, so box and skeleton stay together\n"
                "while a player moves."
            )
            _, s.extrapolate_skeleton = ui.checkbox(
                "Extrapolate skeleton", "Push the body forward to hide latency", s.extrapolate_skeleton
            )
            _tip(
                "The skeleton's shape is always interpolated between two\n"
                "measured poses, so it is smooth either way. This controls\n"
                "only whether the body is pushed forward to hide the\n"
                "worker's latency. Turn OFF if the skeleton sits ahead of a\n"
                "running player; it will trail slightly instead."
            )
            ui.section("World ESP")
            _, s.show_world_entities = ui.checkbox(
                "Enabled##world", "Ores, hemp, items, bags, crates, TC", s.show_world_entities
            )
            types = ["Ore", "Hemp", "Item", "Bag", "Crate", "TC"]
            values = [s.world_types[t] for t in types]
            if ui.multi_combo("Types", "Which world entities to draw", values, types):
                for t, v in zip(types, values):
                    s.world_types[t] = v
            ui.section("Interface")
            _, s.show_watermark = ui.checkbox("Watermark", "Overlay status line", s.show_watermark)

        _two_columns(left, right)

    @staticmethod
    def _draw_aim_tab(s):
        def left():
            ui.section("General")
            _, s.aim_enabled = ui.checkbox("Enabled##aim", "Write angles while the key is held", s.aim_enabled)
            _tip(
                "Master toggle. When on, holding the aim key writes\n"
                "bodyAngles directly to PlayerInput -- frame-perfect aim.\n"
                "No mouse injection, no sensitivity tuning needed."
            )
            _, s.aim_key = ui.keybind("Aim key", "Hold to aim", s.aim_key)
            bone_labels = ["Head", "Neck", "Chest"]
            bone_codes = [53, 52, 23]
            try:
                bcur = bone_codes.index(s.aim_head_bone_id)
            except ValueError:
                bcur = 0
            changed, bcur = ui.combo("Target bone", "Which bone to aim at", bcur, bone_labels)
            if changed:
                s.aim_head_bone_id = bone_codes[bcur]

            ui.section("Targeting")
            _, s.aim_fov_deg = ui.slider_float("FOV cone", "Angular range around the crosshair", s.aim_fov_deg, 1.0, 45.0, "%.1f°")
            _tip(
                "World-space angular FOV. Only targets within this\n"
                "many degrees of the crosshair centre are considered.\n"
                "15° is tight, 30° is generous."
            )
            _, s.aim_distance_bias = ui.slider_float("Prefer nearer", "Penalty per 25 m of range", s.aim_distance_bias, 0.0, 3.0, "%.2f")
            _tip(
                "Ranking used to be the angle from your crosshair and\n"
                "nothing else -- which is why it locked someone else: a\n"
                "player 200 m out but dead centre beat the one at 20 m and\n"
                "three degrees off, even though the near one is obviously\n"
                "who you are shooting at.\n"
                "Degrees of penalty per 25 m of range.\n"
                "Higher = stronger preference for close players.\n"
                "0 = the old pure-angle behaviour."
            )
            _, s.aim_max_distance_m = ui.slider_float("Max range", "Ignore anyone past this (0 = no limit)", s.aim_max_distance_m, 0.0, 500.0, "%.0f m")
            _, s.aim_team_check = ui.checkbox("Skip teammates", "Never aim at your own team", s.aim_team_check)
            _tip(
                "Reads BasePlayer.currentTeam and drops players on your\n"
                "own team from the target list. They are still DRAWN by\n"
                "the ESP -- you want to see your team, just not aim at it.\n"
                "\n"
                "Fails OPEN: if the field cannot be read a player counts\n"
                "as an enemy, because silently switching the aimbot off is\n"
                "much harder to notice than the odd teammate slipping in.\n"
                "Check the [AIM-TEAM] debug line against a real teammate\n"
                "the first time -- two dumps disagree on this offset."
            )
            _, s.aim_exclude_npcs = ui.checkbox("Skip NPCs", "Never lock a scientist or zombie", s.aim_exclude_npcs)
            _tip(
                "Scientists, zombies/murderers, bandit camp NPCs,\n"
                "scarecrows, pets, shopkeepers -- all of them derive from\n"
                "NPCPlayer : BasePlayer, which is why they used to slip in\n"
                "as half-broken players (a name, but bones/hp only\n"
                "sometimes). They are still DRAWN by the ESP in amber (see\n"
                "the 'NPCs' colour group) -- this only keeps the aimbot\n"
                "from locking onto one."
            )
            _, s.aim_lock_key = ui.keybind("Lock target", "Hold to freeze the current target", s.aim_lock_key)
            _tip(
                "Hold to freeze the current target -- nothing else in the\n"
                "cone can steal it, whatever the ranking says.\n"
                "The scoring above is a heuristic and a heuristic picks\n"
                "wrong sometimes; this is the manual override for when it\n"
                "matters and you already know who you want.\n"
                "Click the box then press a key; ESC clears it."
            )

        def right():
            ui.section("Accuracy")
            _, s.aim_deadzone_deg = ui.slider_float("Deadzone", "No write when this close to target", s.aim_deadzone_deg, 0.01, 3.0, "%.2f°")
            _tip(
                "Skip angle writes when the target is closer than\n"
                "this many degrees. Prevents sub-pixel jitter once\n"
                "the crosshair is already on target."
            )
            # The old "Projectile drop/lead" open-loop aim-point adjustment (and
            # its "Gravity (m/s2)" tunable) is gone -- homing below does all of
            # the ballistics compensation, continuously, off the arrow's own
            # live position. Gravity is still used internally by homing.
            _, s.aim_projectile_homing = ui.checkbox(
                "Homing arrows", "Steer bow/crossbow/nailgun shots", s.aim_projectile_homing
            )
            _tip(
                "Curves your own arrows onto the target while they fly.\n"
                "Speed, drag and gravityModifier are read off real\n"
                "projectiles in flight after your first shot with a\n"
                "weapon ([AIM-BALLISTIC] src=measured). Before that it\n"
                "falls back to aim_engine.PROJECTILE_TABLE. Bow/crossbow/\n"
                "nailgun only -- no effect on hitscan weapons.\n"
                "\n"
                "This is NOT the same risk as everything else here. The\n"
                "rest reads memory; this writes state the SERVER\n"
                "re-simulates. Rust verifies periodic projectile\n"
                "positions against the velocity it was told at launch,\n"
                "so a projectile that leaves that trajectory is a LOGGED\n"
                "violation -- not just a shot that fails to register.\n"
                "\n"
                "The turn cap below is the entire safety margin. Small\n"
                "corrections near the target stay inside the tolerance\n"
                "the server allows for sway and movement; hard turns do\n"
                "not. Off by default on purpose."
            )
            if s.aim_projectile_homing:
                _, s.aim_homing_turn_dps = ui.slider_float(
                    "Homing turn cap", "Max arrow turn rate", s.aim_homing_turn_dps, 10.0, 360.0, "%.0f°/s"
                )
                _tip(
                    "How fast an arrow may change direction. Lower is\n"
                    "safer and subtler; higher bends the shot harder and\n"
                    "leaves the launch trajectory further behind.\n"
                    "60 deg/s corrects a near miss without the flight\n"
                    "path looking impossible."
                )

            ui.section("Motion")
            _, s.aim_smooth = ui.slider_float("Smooth", "1 = snap, lower = glide", s.aim_smooth, 0.01, 1.0, "%.2f")
            _tip(
                "Interpolation factor per write.\n"
                "1.0 = instant snap (ragebot)\n"
                "0.5 = responsive glide\n"
                "0.3 = smooth humanized feel\n"
                "0.1 = very slow drift\n"
                "When Humanize is ON, this is the max speed -- the\n"
                "engine auto-reduces near the target."
            )
            _, s.aim_interval_ms = ui.slider_int("Interval", "Min time between two writes", s.aim_interval_ms, 1, 100, "%d ms")
            _tip(
                "Min time between two angle writes. Since we write\n"
                "directly to memory, this can be very low (5-10 ms).\n"
                "When Humanize is ON, ±30% jitter is added automatically."
            )

            ui.section("Anti-Detection")
            _, s.aim_dual_write = ui.checkbox("Dual write", "Body + head angles together", s.aim_dual_write)
            _tip(
                "Write to BOTH PlayerInput.bodyAngles AND\n"
                "PlayerEyes.headAngles simultaneously.\n"
                "Eliminates the 1-frame desync that server-side\n"
                "integrity checks can flag. Recommended: always ON."
            )
            # aim_engine gates max speed / jitter / skip on aim_humanize
            # (aim_engine.py tick): the old menu showed those sliders but had
            # no way to turn Humanize on, so they never applied.
            _, s.aim_humanize = ui.checkbox("Humanize", "Speed cap, tremor, skipped writes", s.aim_humanize)
            if s.aim_humanize:
                _, s.aim_max_speed_dps = ui.slider_float(
                    "Max speed", "Angular speed cap", s.aim_max_speed_dps, 50.0, 1000.0, "%.0f°/s"
                )
                _tip(
                    "Maximum angular velocity in degrees per second.\n"
                    "Human sustained tracking: 80-150°/s\n"
                    "Human fast flick: 300-500°/s"
                )
                _, s.aim_jitter_deg = ui.slider_float("Jitter", "Hand tremor noise", s.aim_jitter_deg, 0.0, 3.0, "%.2f°")
                _tip(
                    "Gaussian noise added to the target point.\n"
                    "Simulates natural mouse tremor / hand shake.\n"
                    "0.3-0.5° is subtle, 1.0° is very noisy."
                )
                _, s.aim_skip_pct = ui.slider_float("Skip", "Fraction of writes skipped", s.aim_skip_pct, 0.0, 0.5, "%.2f")
                _tip(
                    "Fraction of ticks where we DON'T write.\n"
                    "Breaks the constant-rate write pattern that\n"
                    "behavioral analysis can detect.\n"
                    "0.05-0.10 is subtle, 0.20+ causes visible stutter."
                )
            _, s.aim_sticky_ms = ui.slider_int("Sticky", "Min time before switching target", s.aim_sticky_ms, 0, 2000, "%d ms")
            _tip(
                "Don't switch targets for at least this long.\n"
                "Prevents inhuman instant target switching.\n"
                "300-500ms is natural, 0 = instant switch."
            )

            ui.section("Debug")
            _, s.aim_debug_prints = ui.checkbox("Debug prints", "[AIM-WRITE] on every fire", s.aim_debug_prints)
            _tip(
                "Print [AIM-WRITE] to console on each fire.\n"
                "Shows mode (RAW/HUMAN), dual-write (+PE),\n"
                "angles, readback verification, and source.\n"
                "Turn ON while testing, OFF in gameplay."
            )

        _two_columns(left, right)

    @staticmethod
    def _draw_norecoil_tab(s):
        def left():
            ui.section("No Recoil Engine")
            _, s.norecoil_enabled = ui.checkbox("Enabled##norecoil", "Override the held weapon's recoil", s.norecoil_enabled)
            _tip(
                "Overrides recoil properties of your held weapon.\n"
                "Automatically applies and resets when you change weapons."
            )
            ui.section("Recoil Control")
            _, s.norecoil_x = ui.slider_int("Yaw", "Horizontal recoil kept", s.norecoil_x, 0, 100, "%d%%")
            _tip("0% = No horizontal recoil\n100% = Vanilla recoil")
            _, s.norecoil_y = ui.slider_int("Pitch", "Vertical recoil kept", s.norecoil_y, 0, 100, "%d%%")
            _tip("0% = No vertical recoil\n100% = Vanilla recoil")

        _two_columns(left, lambda: None)

    @staticmethod
    def _draw_nosway_tab(s):
        def left():
            ui.section("No Sway / No Bloom")
            _, s.nosway_enabled = ui.checkbox("Enabled##nosway", "Zero spread and sway fields", s.nosway_enabled)
            _tip(
                "Zeroes all spread and sway fields on your held weapon.\n"
                "Bullets go perfectly straight with no bloom or visual sway.\n"
                "Resets automatically when disabled or when you switch weapons."
            )

        def right():
            ui.section("What it does")
            ui.section("- Removes visual aim sway")
            ui.section("- Removes ADS and hip-fire spread")
            ui.section("- Removes per-shot spread penalty")
            ui.section("- Removes stance spread penalty")

        _two_columns(left, right)

    @staticmethod
    def _draw_chams_tab(s):
        from rust_esp_mvc.legacy_runtime import OFF

        def left():
            ui.section("Material Override")
            _, s.chams_enabled = ui.checkbox("Enabled##chams", "Custom material on player renderers", s.chams_enabled)
            _tip(
                "Writes a custom Material ID into the SkinnedMultiMesh renderer.\n"
                "Makes players glow through walls. WARNING: Often requires a game restart\n"
                "to fully revert back to normal materials."
            )
            mat_names = list(OFF.MATERIAL_IDS.keys())
            mat_values = list(OFF.MATERIAL_IDS.values())
            try:
                cur = mat_values.index(s.chams_material_id)
            except ValueError:
                cur = 0
            changed, cur = ui.combo("Material", "Material written on players", cur, mat_names)
            if changed:
                s.chams_material_id = mat_values[cur]

        def right():
            ui.section("Targets")
            _, s.chams_apply_enemies = ui.checkbox("Enemy Players", "Apply to everyone else", s.chams_apply_enemies)
            _, s.chams_apply_local = ui.checkbox("Local Player", "Apply to yourself (can bug arms)", s.chams_apply_local)
            imgui.begin_disabled()
            ui.checkbox("Held Items", "Disabled -- causes crashes", False)
            imgui.end_disabled()
            s.chams_apply_held = False  # force off

        _two_columns(left, right)

    @staticmethod
    def _draw_color_tab(s):
        groups = dict(base.PALETTE_GROUPS)

        def edit_group(section):
            ui.section(section)
            for key, label in groups[section]:
                imgui.push_id(key)
                col = list(s.colors[key])
                if ui.color_edit4(label, f"{section} {label.lower()} colour", col):
                    s.set_col(key, col)
                imgui.pop_id()

        def left():
            edit_group("Players")
            edit_group("Sleepers")

        def right():
            edit_group("NPCs")
            edit_group("World")
            edit_group("Interface")

        _two_columns(left, right)

    @staticmethod
    def _draw_menu_settings_tab(s, menu):
        def left():
            ui.section("Menu")
            accent = [ui.C.accent.x, ui.C.accent.y, ui.C.accent.z, ui.C.accent.w]
            if ui.color_edit4("Accented Color", "Setting the main color of the menu.", accent):
                ui.C.accent = imgui.ImVec4(*accent)
            _, menu.notifications.design = ui.combo(
                "Notify design", "Setting up notification styling", menu.notifications.design, ["Circle", "Line"]
            )
            if ui.action_row("Reset colors", "Restore the default ESP palette", "RESET"):
                s.reset_colors()
                menu.notifications.add("ESP colours reset to defaults.", 2500)

        _two_columns(left, lambda: None)

# MARKER_TEST
