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


class OverlayView(base.OverlayView):
    """Render boxes, distance, tracers, watermark and body-anchored skeletons."""

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
                            "box_sleeping" if is_sleeping else "box"
                        )
                        base._draw_corner_box(
                            draw_list, left, top, right, bottom, box_col
                        )

                    # ── Health bar (left of box) ──
                    if s.show_health:
                        hp = player.get("hp", -1.0)
                        max_hp = player.get("max_hp", -1.0)
                        base._draw_health_bar(
                            draw_list, left, top, bottom, hp, max_hp
                        )

                    # ── Player name (above box) ──
                    name = player.get("name", "")
                    if s.show_name and name:
                        name_col = s.col(
                            "name_sleeping" if is_sleeping else "name"
                        )
                        base._draw_centered_text(
                            draw_list, center_x, top - 16.0, name_col, name
                        )

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
                        s.col("skeleton"),
                        distance,
                    ):
                        skeletons += 1

                    # ── Info below box: distance + held item ──
                    info_y = (
                        bottom + 4.0
                        if bottom <= self.height - 40.0
                        else bottom - 32.0
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

                if is_holding_key:
                    # Draw Full Inventory Overlay (Wear, Main, Belt)
                    slot_size = 40.0
                    slot_gap = 4.0
                    num_cols = 6
                    grid_w = num_cols * slot_size + (num_cols - 1) * slot_gap
                    start_x = screen_cx - grid_w * 0.5
                    start_y = (gy + gh * sy) - 340.0

                    # Main Outer HUD Box
                    draw_list.add_rect_filled(
                        (start_x - 10.0, start_y - 24.0),
                        (start_x + grid_w + 10.0, start_y + 310.0),
                        0xDD080C10,
                        6.0,
                    )
                    draw_list.add_rect(
                        (start_x - 10.0, start_y - 24.0),
                        (start_x + grid_w + 10.0, start_y + 310.0),
                        0xFF00D4FF,
                        6.0,
                        0,
                        1.5,
                    )

                    # Target Inventory Title
                    base._draw_centered_text(
                        draw_list, screen_cx, start_y - 18.0,
                        0xFFFFFFFF, f"Target's Inventory ({t_name})"
                    )

                    cur_y = start_y + 6.0

                    def _draw_grid(items_list, rows, cols, title):
                        nonlocal cur_y
                        if title:
                            base._draw_centered_text(draw_list, screen_cx, cur_y, 0xFF88CCFF, title)
                            cur_y += 14.0
                        for r in range(rows):
                            for c in range(cols):
                                idx = r * cols + c
                                sx_pos = start_x + c * (slot_size + slot_gap)
                                sy_pos = cur_y + r * (slot_size + slot_gap)
                                rect_min = (sx_pos, sy_pos)
                                rect_max = (sx_pos + slot_size, sy_pos + slot_size)

                                slot_item = items_list[idx] if idx < len(items_list) else None
                                short_name, disp_name = "", ""
                                if isinstance(slot_item, (tuple, list)):
                                    short_name, disp_name = slot_item[0], slot_item[1]
                                elif isinstance(slot_item, str):
                                    short_name, disp_name = slot_item, slot_item

                                if title == "BELT" and not short_name and idx == 0 and held_name:
                                    short_name, disp_name = held_name, held_name

                                is_held = (disp_name and disp_name == held_name) or (short_name and short_name == held_name)

                                border_col = 0xFF00D4FF if is_held else (0xFF88CCFF if disp_name else 0x55334455)
                                draw_list.add_rect_filled(rect_min, rect_max, 0xCC0A1015, 3.0)
                                draw_list.add_rect(rect_min, rect_max, border_col, 3.0, 0, 1.8 if is_held else 1.0)

                                label = disp_name or short_name
                                tex_id = get_item_texture(short_name) if short_name else None
                                if tex_id:
                                    pad = 3.0
                                    draw_list.add_image(
                                        imgui.ImTextureRef(int(tex_id)),
                                        imgui.ImVec2(sx_pos + pad, sy_pos + pad),
                                        imgui.ImVec2(sx_pos + slot_size - pad, sy_pos + slot_size - pad),
                                        imgui.ImVec2(0.0, 0.0),
                                        imgui.ImVec2(1.0, 1.0),
                                        0xFFFFFFFF,
                                    )
                                elif label:
                                    text_w, text_h = base._text_size(label)
                                    short_label = label if len(label) <= 6 else label[:5] + ".."
                                    item_col = s.col("held_item") if is_held else 0xFFFFFFFF
                                    base._draw_centered_text(
                                        draw_list, sx_pos + slot_size * 0.5, sy_pos + (slot_size - text_h) * 0.15,
                                        item_col, short_label
                                    )
                        cur_y += rows * (slot_size + slot_gap) + 4.0

                    _draw_grid(wear, 1, 6, "CLOTHING / ARMOR")
                    _draw_grid(main_inv, 4, 6, "MAIN INVENTORY")
                    _draw_grid(belt, 1, 6, "BELT")
                else:
                    # Draw Compact Target Belt HUD with keybind prompt hint
                    slot_size = 46.0
                    slot_gap = 6.0
                    total_slots_w = 6 * slot_size + 5 * slot_gap
                    start_x = screen_cx - total_slots_w * 0.5
                    hud_y = (gy + gh * sy) - 150.0

                    # Hint prompt above belt
                    base._draw_centered_text(
                        draw_list, screen_cx, hud_y - 30.0,
                        0xFF00D4FF, f"Target: {t_name}  |  [Hold ',' for Full Inventory]"
                    )
                    base._draw_centered_text(
                        draw_list, screen_cx, hud_y - 15.0,
                        0xFFFFFFFF, "Target's Belt"
                    )

                    for slot_i in range(6):
                        sx_pos = start_x + slot_i * (slot_size + slot_gap)
                        sy_pos = hud_y
                        rect_min = (sx_pos, sy_pos)
                        rect_max = (sx_pos + slot_size, sy_pos + slot_size)

                        slot_item = belt[slot_i] if slot_i < len(belt) else None
                        short_name, disp_name = "", ""
                        if isinstance(slot_item, (tuple, list)):
                            short_name, disp_name = slot_item[0], slot_item[1]
                        elif isinstance(slot_item, str):
                            short_name, disp_name = slot_item, slot_item

                        if not short_name and slot_i == 0 and held_name:
                            short_name, disp_name = held_name, held_name

                        is_held = (disp_name and disp_name == held_name) or (short_name and short_name == held_name)

                        border_col = 0xFF00D4FF if is_held else (0xFF88CCFF if disp_name else 0x88445566)
                        draw_list.add_rect_filled(rect_min, rect_max, 0xAA0A1015, 4.0)
                        draw_list.add_rect(rect_min, rect_max, border_col, 4.0, 0, 2.0 if is_held else 1.25)

                        label = disp_name or short_name
                        tex_id = get_item_texture(short_name) if short_name else None
                        if tex_id:
                            pad = 4.0
                            draw_list.add_image(
                                imgui.ImTextureRef(int(tex_id)),
                                imgui.ImVec2(sx_pos + pad, sy_pos + pad),
                                imgui.ImVec2(sx_pos + slot_size - pad, sy_pos + slot_size - pad),
                                imgui.ImVec2(0.0, 0.0),
                                imgui.ImVec2(1.0, 1.0),
                                0xFFFFFFFF,
                            )
                        elif label:
                            text_w, text_h = base._text_size(label)
                            short_label = label if len(label) <= 7 else label[:6] + ".."
                            item_col = s.col("held_item") if is_held else 0xFFFFFFFF
                            base._draw_centered_text(
                                draw_list, sx_pos + slot_size * 0.5, sy_pos + (slot_size - text_h) * 0.5,
                                item_col, short_label
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

    def _draw_menu(self):
        """Escape-toggled feature menu. Drawn after the transparent ESP
        window's style is popped, so it gets ImGui's normal opaque styling."""
        s = self.settings
        imgui.set_next_window_size((320, 0), imgui.Cond_.first_use_ever)
        imgui.set_next_window_pos((60, 60), imgui.Cond_.first_use_ever)
        expanded, opened = imgui.begin("Rust ESP MVC — Menu (ESC)", True)
        if expanded and imgui.begin_tab_bar("##menu_tabs"):
            if imgui.begin_tab_item("Features")[0]:
                self._draw_feature_tab(s)
                imgui.end_tab_item()
            if imgui.begin_tab_item("Aim")[0]:
                self._draw_aim_tab(s)
                imgui.end_tab_item()
            if imgui.begin_tab_item("No Recoil")[0]:
                self._draw_norecoil_tab(s)
                imgui.end_tab_item()
            if imgui.begin_tab_item("No Sway")[0]:
                self._draw_nosway_tab(s)
                imgui.end_tab_item()
            if imgui.begin_tab_item("Chams")[0]:
                self._draw_chams_tab(s)
                imgui.end_tab_item()
            if imgui.begin_tab_item("Colors")[0]:
                self._draw_color_tab(s)
                imgui.end_tab_item()
            imgui.end_tab_bar()
        imgui.end()
        if not opened:
            self.menu_open = False
            self._set_click_through(True)

    @staticmethod
    def _draw_feature_tab(s):
        imgui.spacing()
        imgui.text_colored(base._rgba(*base._ACCENT), "Player ESP")
        imgui.separator()
        _, s.show_boxes = imgui.checkbox("Boxes", s.show_boxes)
        _, s.show_skeleton = imgui.checkbox("Skeleton", s.show_skeleton)
        _, s.show_tracers = imgui.checkbox("Tracers", s.show_tracers)
        _, s.show_health = imgui.checkbox("Health bar", s.show_health)
        _, s.show_name = imgui.checkbox("Names", s.show_name)
        _, s.show_distance = imgui.checkbox("Distance", s.show_distance)
        _, s.show_held_item = imgui.checkbox("Held item", s.show_held_item)
        # is_item_hovered() refers to the widget declared immediately above
        # it. Inserting a checkbox between another checkbox and its tooltip
        # silently reassigns that tooltip to the new widget -- which is what
        # happened when "Extrapolate skeleton" was added: both tooltips ended
        # up on it, the second overwriting the first, and "Box follows
        # skeleton" showed nothing at all.
        _, s.box_from_bones = imgui.checkbox(
            "Box follows skeleton", s.box_from_bones
        )
        if imgui.is_item_hovered():
            imgui.set_tooltip(
                "Build the box from the rendered bones instead of the\n"
                "networked position, so box and skeleton stay together\n"
                "while a player moves."
            )
        _, s.extrapolate_skeleton = imgui.checkbox(
            "Extrapolate skeleton", s.extrapolate_skeleton
        )
        if imgui.is_item_hovered():
            imgui.set_tooltip(
                "The skeleton's shape is always interpolated between two\n"
                "measured poses, so it is smooth either way. This controls\n"
                "only whether the body is pushed forward to hide the\n"
                "worker's latency. Turn OFF if the skeleton sits ahead of a\n"
                "running player; it will trail slightly instead."
            )

        imgui.spacing()
        imgui.text_colored(base._rgba(*base._ACCENT), "World ESP")
        imgui.separator()
        _, s.show_world_entities = imgui.checkbox(
            "Enabled", s.show_world_entities
        )
        for etype in ("Ore", "Hemp", "Item", "Bag", "Crate", "TC"):
            _, s.world_types[etype] = imgui.checkbox(
                etype, s.world_types[etype]
            )

        imgui.spacing()
        imgui.separator()
        _, s.show_watermark = imgui.checkbox("Watermark", s.show_watermark)

    @staticmethod
    def _draw_aim_tab(s):
        imgui.spacing()
        imgui.text_colored(base._rgba(*base._ACCENT), "Aim Assist (Write Angles)")
        imgui.separator()

        _, s.aim_enabled = imgui.checkbox("Enabled", s.aim_enabled)
        if imgui.is_item_hovered():
            imgui.set_tooltip(
                "Master toggle. When on, holding the aim key writes\n"
                "bodyAngles directly to PlayerInput — frame-perfect aim.\n"
                "No mouse injection, no sensitivity tuning needed."
            )

        # Aim key dropdown
        key_labels = ["Right click (RMB)",
                      "Left click (LMB)",
                      "Middle click (MMB)",
                      "Mouse 4 (X1)",
                      "Mouse 5 (X2)",
                      "Alt (left)",
                      "Shift (left)",
                      "Control (left)"]
        key_codes = [0x02, 0x01, 0x04, 0x05, 0x06, 0x12, 0xA0, 0xA2]
        try:
            cur = key_codes.index(s.aim_key)
        except ValueError:
            cur = 0
        changed, cur = imgui.combo("Aim key", cur, key_labels)
        if changed:
            s.aim_key = key_codes[cur]

        # Bone dropdown
        bone_labels = ["Head (53)", "Neck (52)", "Chest (23)"]
        bone_codes = [53, 52, 23]
        try:
            bcur = bone_codes.index(s.aim_head_bone_id)
        except ValueError:
            bcur = 0
        changed, bcur = imgui.combo("Target bone", bcur, bone_labels)
        if changed:
            s.aim_head_bone_id = bone_codes[bcur]

        imgui.spacing()
        imgui.text_colored(base._rgba(*base._ACCENT), "Targeting")
        imgui.separator()

        _, s.aim_fov_deg = imgui.slider_float(
            "FOV cone (deg)", s.aim_fov_deg, 1.0, 45.0
        )
        if imgui.is_item_hovered():
            imgui.set_tooltip(
                "World-space angular FOV. Only targets within this\n"
                "many degrees of the crosshair centre are considered.\n"
                "15° is tight, 30° is generous."
            )

        _, s.aim_deadzone_deg = imgui.slider_float(
            "Deadzone (deg)", s.aim_deadzone_deg, 0.01, 3.0
        )
        if imgui.is_item_hovered():
            imgui.set_tooltip(
                "Skip angle writes when the target is closer than\n"
                "this many degrees. Prevents sub-pixel jitter once\n"
                "the crosshair is already on target."
            )

        _, s.aim_projectile_lead = imgui.checkbox(
            "Projectile drop/lead (bow/crossbow/nailgun)", s.aim_projectile_lead
        )
        if imgui.is_item_hovered():
            imgui.set_tooltip(
                "Aims ahead of a moving target and above their current\n"
                "position to compensate for arrow/bolt/nail travel time\n"
                "and gravity drop. No effect on hitscan weapons.\n"
                "Speed, drag and gravityModifier are read off real\n"
                "projectiles in flight after your first shot with a\n"
                "weapon ([AIM-BALLISTIC] src=measured). Before that it\n"
                "falls back to aim_engine.PROJECTILE_TABLE."
            )

        if s.aim_projectile_lead:
            _, s.aim_gravity = imgui.slider_float(
                "Gravity (m/s2)", s.aim_gravity, 5.0, 20.0
            )
            if imgui.is_item_hovered():
                imgui.set_tooltip(
                    "Base gravity, multiplied by each projectile's own\n"
                    "gravityModifier (read live). This is the only value\n"
                    "in the ballistics chain that is assumed rather than\n"
                    "read: Unity's Physics.gravity is a native property\n"
                    "with no readable offset. 9.81 is Unity's default.\n"
                    "It scales drop linearly -- if arrows land short or\n"
                    "long by the same proportion at EVERY range, change\n"
                    "this, not the per-weapon speeds."
                )

            _, s.aim_lead_smooth_s = imgui.slider_float(
                "Lead smoothing (s)", s.aim_lead_smooth_s, 0.0, 1.5
            )
            if imgui.is_item_hovered():
                imgui.set_tooltip(
                    "Smoothing on the TARGET's velocity, not on your aim.\n"
                    "Keep this SHORT (0.10-0.20). A long value steadies a\n"
                    "strafing target but lags a real sprint by just as much,\n"
                    "so a player running straight gets led short for the\n"
                    "whole run -- the preshot case you actually want.\n"
                    "Jitter is handled separately by the coherence gate:\n"
                    "consistent motion gets the full lead, thrashing motion\n"
                    "collapses the lead toward the body. Watch coh= in the\n"
                    "[AIM-BALLISTIC] line -- ~1.0 running straight, ~0.2\n"
                    "when a target is juking."
                )

            _, s.aim_projectile_homing = imgui.checkbox(
                "Steer arrows in flight (homing)", s.aim_projectile_homing
            )
            if imgui.is_item_hovered():
                imgui.set_tooltip(
                    "Curves your own arrows onto the target while they fly.\n"
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
                _, s.aim_homing_turn_dps = imgui.slider_float(
                    "Homing turn cap (deg/s)", s.aim_homing_turn_dps, 10.0, 360.0
                )
                if imgui.is_item_hovered():
                    imgui.set_tooltip(
                        "How fast an arrow may change direction. Lower is\n"
                        "safer and subtler; higher bends the shot harder and\n"
                        "leaves the launch trajectory further behind.\n"
                        "60 deg/s corrects a near miss without the flight\n"
                        "path looking impossible."
                    )

        imgui.spacing()
        imgui.text_colored(base._rgba(*base._ACCENT), "Motion")
        imgui.separator()

        _, s.aim_smooth = imgui.slider_float(
            "Smooth", s.aim_smooth, 0.01, 1.0
        )
        if imgui.is_item_hovered():
            imgui.set_tooltip(
                "Interpolation factor per write.\n"
                "1.0 = instant snap (ragebot)\n"
                "0.5 = responsive glide\n"
                "0.3 = smooth humanized feel\n"
                "0.1 = very slow drift\n"
                "When Humanize is ON, this is the max speed — the\n"
                "engine auto-reduces near the target."
            )

        _, s.aim_interval_ms = imgui.slider_int(
            "Interval (ms)", s.aim_interval_ms, 1, 100
        )
        if imgui.is_item_hovered():
            imgui.set_tooltip(
                "Min time between two angle writes. Since we write\n"
                "directly to memory, this can be very low (5-10 ms).\n"
                "When Humanize is ON, ±30%% jitter is added automatically."
            )

        # ── Anti-detection ──
        imgui.spacing()
        imgui.text_colored(base._rgba(1.0, 0.3, 0.3, 1.0), "⛨ Anti-Detection")
        imgui.separator()

        _, s.aim_dual_write = imgui.checkbox(
            "Dual write (body + head)", s.aim_dual_write
        )
        if imgui.is_item_hovered():
            imgui.set_tooltip(
                "Write to BOTH PlayerInput.bodyAngles AND\n"
                "PlayerEyes.headAngles simultaneously.\n"
                "Eliminates the 1-frame desync that server-side\n"
                "integrity checks can flag.\n"
                "⚠ RECOMMENDED: always ON."
            )

            _, s.aim_max_speed_dps = imgui.slider_float(
                "Max speed (°/s)", s.aim_max_speed_dps, 50.0, 1000.0
            )
            if imgui.is_item_hovered():
                imgui.set_tooltip(
                    "Maximum angular velocity in degrees per second.\n"
                    "Human sustained tracking: 80-150°/s\n"
                    "Human fast flick: 300-500°/s\n"
                    "0 = unlimited (but don't — that's detectable)"
                )

            _, s.aim_jitter_deg = imgui.slider_float(
                "Jitter (deg)", s.aim_jitter_deg, 0.0, 3.0
            )
            if imgui.is_item_hovered():
                imgui.set_tooltip(
                    "Gaussian noise added to the target point.\n"
                    "Simulates natural mouse tremor / hand shake.\n"
                    "0.3-0.5° is subtle, 1.0° is very noisy.\n"
                    "0 = pixel-perfect (detectable)."
                )

            _, s.aim_skip_pct = imgui.slider_float(
                "Skip %%", s.aim_skip_pct, 0.0, 0.5
            )
            if imgui.is_item_hovered():
                imgui.set_tooltip(
                    "Fraction of ticks where we DON'T write.\n"
                    "Breaks the constant-rate write pattern that\n"
                    "behavioral analysis can detect.\n"
                    "0.05-0.10 is subtle, 0.20+ causes visible stutter."
                )

            _, s.aim_sticky_ms = imgui.slider_int(
                "Sticky (ms)", s.aim_sticky_ms, 0, 2000
            )
            if imgui.is_item_hovered():
                imgui.set_tooltip(
                    "Don't switch targets for at least this long.\n"
                    "Prevents inhuman instant target switching.\n"
                    "300-500ms is natural, 0 = instant switch."
                )

            imgui.unindent(16)

        imgui.spacing()
        _, s.aim_debug_prints = imgui.checkbox("Debug prints", s.aim_debug_prints)
        if imgui.is_item_hovered():
            imgui.set_tooltip(
                "Print [AIM-WRITE] to console on each fire.\n"
                "Shows mode (RAW/HUMAN), dual-write (+PE),\n"
                "angles, readback verification, and source.\n"
                "Turn ON while testing, OFF in gameplay."
            )

    @staticmethod
    def _draw_norecoil_tab(s):
        imgui.spacing()
        imgui.text_colored(base._rgba(*base._ACCENT), "No Recoil Engine")
        imgui.separator()

        _, s.norecoil_enabled = imgui.checkbox("Enabled##norecoil", s.norecoil_enabled)
        if imgui.is_item_hovered():
            imgui.set_tooltip(
                "Overrides recoil properties of your held weapon.\n"
                "Automatically applies and resets when you change weapons."
            )
        
        imgui.spacing()
        imgui.text_colored(base._rgba(*base._ACCENT), "Recoil Control %")
        imgui.separator()

        _, s.norecoil_x = imgui.slider_int("Yaw (Horizontal) %", s.norecoil_x, 0, 100)
        if imgui.is_item_hovered():
            imgui.set_tooltip("0% = No horizontal recoil\n100% = Vanilla recoil")

        _, s.norecoil_y = imgui.slider_int("Pitch (Vertical) %", s.norecoil_y, 0, 100)
        if imgui.is_item_hovered():
            imgui.set_tooltip("0% = No vertical recoil\n100% = Vanilla recoil")

    @staticmethod
    def _draw_nosway_tab(s):
        imgui.spacing()
        imgui.text_colored(base._rgba(*base._ACCENT), "No Sway / No Bloom")
        imgui.separator()

        _, s.nosway_enabled = imgui.checkbox("Enabled##nosway", s.nosway_enabled)
        if imgui.is_item_hovered():
            imgui.set_tooltip(
                "Zeroes all spread and sway fields on your held weapon.\n"
                "Bullets go perfectly straight with no bloom or visual sway.\n"
                "Resets automatically when disabled or when you switch weapons."
            )

        imgui.spacing()
        imgui.text_colored(base._rgba(*base._ACCENT), "What it does")
        imgui.separator()
        imgui.bullet_text("Removes visual aim sway")
        imgui.bullet_text("Removes ADS and hip-fire bullet spread")
        imgui.bullet_text("Removes per-shot spread penalty")
        imgui.bullet_text("Removes stance spread penalty")

    @staticmethod
    def _draw_chams_tab(s):
        imgui.spacing()
        imgui.text_colored(base._rgba(*base._ACCENT), "Material Override (Chams)")
        imgui.separator()

        _, s.chams_enabled = imgui.checkbox("Enabled", s.chams_enabled)
        if imgui.is_item_hovered():
            imgui.set_tooltip(
                "Writes a custom Material ID into the SkinnedMultiMesh renderer.\n"
                "Makes players glow through walls. WARNING: Often requires a game restart\n"
                "to fully revert back to normal materials."
            )

        # Material dropdown
        from rust_esp_mvc.legacy_runtime import OFF
        mat_names = list(OFF.MATERIAL_IDS.keys())
        mat_values = list(OFF.MATERIAL_IDS.values())
        
        try:
            cur_idx = mat_values.index(s.chams_material_id)
        except ValueError:
            cur_idx = 0

        changed, cur_idx = imgui.combo("Material", cur_idx, mat_names)
        if changed:
            s.chams_material_id = mat_values[cur_idx]

        imgui.spacing()
        imgui.text_colored(base._rgba(*base._ACCENT), "Targets")
        imgui.separator()
        
        _, s.chams_apply_enemies = imgui.checkbox("Enemy Players", s.chams_apply_enemies)
        _, s.chams_apply_local = imgui.checkbox("Local Player", s.chams_apply_local)
        imgui.begin_disabled()
        imgui.checkbox("Held Items (Weapons) — disabled, causes crashes", False)
        imgui.end_disabled()
        s.chams_apply_held = False  # force off

    @staticmethod
    def _draw_color_tab(s):
        imgui.spacing()
        flags = (
            imgui.ColorEditFlags_.alpha_bar
            | imgui.ColorEditFlags_.alpha_preview_half
        )
        for section, entries in base.PALETTE_GROUPS:
            imgui.text_colored(base._rgba(*base._ACCENT), section)
            imgui.separator()
            for key, label in entries:
                changed, rgba = imgui.color_edit4(
                    f"{label}##col_{key}", s.colors[key], flags
                )
                if changed:
                    s.set_col(key, rgba)
            imgui.spacing()
        if imgui.button("Reset to defaults"):
            s.reset_colors()
