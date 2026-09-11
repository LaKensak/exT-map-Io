"""1:1 Python port of the "imgui aternos" C++ Dear ImGui menu template.

Source of truth, all under ``imgui aternos/``: ``imgui_edited.cpp`` (widgets),
``imgui_settings.h`` (colours/sizes), ``examples/example_win32_directx11/
main.cpp`` (fonts, window layout, notifications, info bar), and the two
core tweaks in ``imgui_widgets.cpp`` (scrollbar grab) / ``imgui_draw.cpp``
(rounded AddRectFilledMultiColor). Every rect, colour, font size and lerp
speed is copied from there. Where imgui_bundle 1.92 forces a different
mechanism, the maths is reproduced so the pixels match:

* ``SliderBehavior`` takes raw pointers in the bindings -> reimplemented
  (same grab_padding/grab_sz/click-offset/round-to-format logic as 1.90).
* ``GetContentRegionMax`` was removed in 1.92 -> ``_region_max()``.
* ``AddRectFilledMultiColor`` has no rounding upstream -> ``rect_filled_multi``
  does exactly what the template's patched version does (white rounded rect,
  then per-vertex bilinear recolour).
* The core scrollbar grab was restyled in C++ -> the native grab is made
  transparent and the template's pill is drawn at the identical rect.
* ``AddShadowCircle`` (ImGui shadows branch) does not exist upstream -> the
  only approximation, used by the saved-colour swatches of the picker.
"""
import ctypes
import math
import os
import re
import time

from imgui_bundle import imgui

_it = imgui.internal
_ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "aternos")
_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32


def _rgb(r, g, b, a=255):
    return imgui.ImVec4(r / 255.0, g / 255.0, b / 255.0, a / 255.0)


def _v4(x=0.0, y=0.0, z=0.0, w=0.0):
    return imgui.ImVec4(x, y, z, w)


class C:
    """imgui_settings.h ``namespace c``. ``accent`` is what main.cpp actually
    writes every frame (124,103,255), not the header's 112,110,215."""

    accent = _rgb(124, 103, 255)

    bg_filling = _rgb(12, 12, 12)
    bg_stroke = _rgb(24, 26, 36)
    bg_size = (900.0, 515.0)
    bg_rounding = 6.0

    mark = _rgb(255, 255, 255)
    el_stroke = _rgb(28, 26, 37)
    el_background = _rgb(15, 15, 17)
    el_background_widget = _rgb(21, 23, 26)
    text_active = _rgb(255, 255, 255)
    text_hov = _rgb(81, 92, 109)
    text = _rgb(43, 51, 63)
    el_rounding = 4.0

    tab_active = _rgb(22, 22, 30)
    tab_border = _rgb(14, 14, 15)


class _Font:
    __slots__ = ("font", "size")

    def __init__(self, font, size):
        self.font = font
        self.size = size


class F:
    """main.cpp's six fonts. Note the template's own naming: ``lexend_bold``
    is loaded from the *regular* TTF at 17 px, and the default font (first
    one added) is ``lexend_general_bold`` = the bold TTF at 18 px."""

    lexend_general_bold = None
    lexend_bold = None
    lexend_regular = None
    icomoon = None
    icomoon_widget = None
    icomoon_widget2 = None


def load_fonts():
    """Register the template fonts. Call once, right after create_context().

    ImGui's own default font is added first so it stays font #0 for the ESP
    text; the menu pushes Lexend itself (see ``begin_menu``). ImGui 1.92
    fonts are size-dynamic, so one ImFont per file serves every size.
    """
    if F.lexend_general_bold is not None:
        return
    fonts = imgui.get_io().fonts
    fonts.add_font_default()

    def add(name, size):
        return fonts.add_font_from_file_ttf(os.path.join(_ASSET_DIR, name), size)

    bold = add("lexend_bold.ttf", 18.0)
    regular = add("lexend_regular.ttf", 17.0)
    icons = add("icomoon.ttf", 20.0)
    icons_widget = add("icomoon_widget.ttf", 15.0)
    F.lexend_general_bold = _Font(bold, 18.0)
    F.lexend_bold = _Font(regular, 17.0)
    F.lexend_regular = _Font(regular, 14.0)
    F.icomoon = _Font(icons, 20.0)
    F.icomoon_widget = _Font(icons_widget, 15.0)
    F.icomoon_widget2 = _Font(icons, 16.0)


# ─── colour / maths helpers (ImGui semantics) ───────────────────────────────

def _sat(v):
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def _f2u8(v):
    return int(_sat(v) * 255.0 + 0.5)


def _pack(r, g, b, a):
    return (_f2u8(a) << 24) | (_f2u8(b) << 16) | (_f2u8(g) << 8) | _f2u8(r)


def _unpack(u):
    return (
        (u & 0xFF) / 255.0,
        ((u >> 8) & 0xFF) / 255.0,
        ((u >> 16) & 0xFF) / 255.0,
        ((u >> 24) & 0xFF) / 255.0,
    )


def col32(v, alpha_mul=1.0):
    """GetColorU32(const ImVec4&, float): multiplies in style.Alpha."""
    return _pack(v.x, v.y, v.z, v.w * imgui.get_style().alpha * alpha_mul)


def u32_alpha(col):
    """GetColorU32(ImU32): only the style.Alpha multiply."""
    alpha = imgui.get_style().alpha
    if alpha >= 1.0:
        return col
    a = int(((col >> 24) & 0xFF) * alpha)
    return (col & 0x00FFFFFF) | (a << 24)


def _vec(u):
    return imgui.ImVec4(*_unpack(u))


def _lerp(a, b, t):
    return a + (b - a) * t


def _lerp4(a, b, t):
    return imgui.ImVec4(
        a.x + (b.x - a.x) * t,
        a.y + (b.y - a.y) * t,
        a.z + (b.z - a.z) * t,
        a.w + (b.w - a.w) * t,
    )


def _dt():
    return imgui.get_io().delta_time


class _State(dict):
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__


_ANIM = {}

# Responsive menu height -- see Menu.draw / begin_child.
_CHILD_HEIGHTS = {}  # begin_child str_id -> height its content needs (last frame)
_menu_height = C.bg_size[1]


def _anim(kind, id_, **defaults):
    """The C++ ``static std::map<ImGuiID, xxx_state> anim`` of each widget:
    value-initialised (zero colours, zero floats) on first lookup."""
    bucket = _ANIM.setdefault(kind, {})
    st = bucket.get(id_)
    if st is None:
        st = _State(defaults)
        bucket[id_] = st
    return st


def _region_max():
    """GetContentRegionMax() (removed in 1.92): ContentRegionRect.Max - Pos."""
    win = _it.get_current_window()
    r = win.content_region_rect
    return r.max.x - win.pos.x, r.max.y - win.pos.y


def _widget_width():
    """``GetContentRegionMax().x - style.WindowPadding.x`` used by every widget."""
    return _region_max()[0] - imgui.get_style().window_padding.x


def _display(text):
    i = text.find("##")
    return text if i < 0 else text[:i]


def render_text_color(fnt, p_min, p_max, col, text, align):
    """edited::RenderTextColor -> RenderTextClipped with the font pushed.

    The C++ pushes ``col`` (already style-alpha'd) as ImGuiCol_Text and
    RenderTextClipped re-reads it through GetColorU32(ImGuiCol_Text), which
    multiplies style.Alpha a second time -- kept, it is visible in the fade.
    """
    text = _display(text)
    if not text:
        return
    imgui.push_font(fnt.font, fnt.size)
    size = imgui.calc_text_size(text)
    x, y = p_min[0], p_min[1]
    if align[0] > 0.0:
        x = max(x, x + (p_max[0] - x - size.x) * align[0])
    if align[1] > 0.0:
        y = max(y, y + (p_max[1] - y - size.y) * align[1])
    r, g, b, a = _unpack(col)
    col = _pack(r, g, b, a * imgui.get_style().alpha)
    dl = imgui.get_window_draw_list()
    if x + size.x >= p_max[0] or y + size.y >= p_max[1]:
        dl.add_text(
            fnt.font, fnt.size, imgui.ImVec2(x, y), col, text, None, 0.0,
            imgui.ImVec4(p_min[0], p_min[1], p_max[0], p_max[1]),
        )
    else:
        dl.add_text(fnt.font, fnt.size, imgui.ImVec2(x, y), col, text)
    imgui.pop_font()


_RC_TL = imgui.ImDrawFlags_.round_corners_top_left.value
_RC_TR = imgui.ImDrawFlags_.round_corners_top_right.value
_RC_BL = imgui.ImDrawFlags_.round_corners_bottom_left.value
_RC_BR = imgui.ImDrawFlags_.round_corners_bottom_right.value
_RC_NONE = imgui.ImDrawFlags_.round_corners_none.value
_RC_TOP = _RC_TL | _RC_TR
_RC_BOTTOM = _RC_BL | _RC_BR
_RC_LEFT = _RC_TL | _RC_BL
_RC_RIGHT = _RC_TR | _RC_BR
_RC_ALL = _RC_TOP | _RC_BOTTOM
_RC_MASK = _RC_ALL | _RC_NONE


def rect_filled_multi(dl, p_min, p_max, ul, ur, br, bl, rounding=0.0, flags=0):
    """The template's patched ImDrawList::AddRectFilledMultiColor."""
    if ((ul | ur | br | bl) & 0xFF000000) == 0:
        return
    if (flags & _RC_MASK) == 0:
        flags |= _RC_ALL
    w = abs(p_max[0] - p_min[0])
    h = abs(p_max[1] - p_min[1])
    rounding = min(rounding, w * (0.5 if (flags & _RC_TOP) == _RC_TOP or (flags & _RC_BOTTOM) == _RC_BOTTOM else 1.0) - 1.0)
    rounding = min(rounding, h * (0.5 if (flags & _RC_LEFT) == _RC_LEFT or (flags & _RC_RIGHT) == _RC_RIGHT else 1.0) - 1.0)
    if rounding > 0.0:
        buf = dl.vtx_buffer
        start = buf.size()
        dl.add_rect_filled(p_min, p_max, 0xFFFFFFFF, rounding, flags)
        end = buf.size()
        c_ul, c_ur, c_br, c_bl = _unpack(ul), _unpack(ur), _unpack(br), _unpack(bl)
        dx = p_max[0] - p_min[0]
        dy = p_max[1] - p_min[1]
        for i in range(start, end):
            v = buf[i]
            X = _sat((v.pos.x - p_min[0]) / dx) if dx else 0.0
            Y = _sat((v.pos.y - p_min[1]) / dy) if dy else 0.0
            top = [c_ul[k] + (c_ur[k] - c_ul[k]) * X for k in range(4)]
            bot = [c_bl[k] + (c_br[k] - c_bl[k]) * X for k in range(4)]
            vr, vg, vb, va = _unpack(v.col)
            v.col = _pack(
                (top[0] + (bot[0] - top[0]) * Y) * vr,
                (top[1] + (bot[1] - top[1]) * Y) * vg,
                (top[2] + (bot[2] - top[2]) * Y) * vb,
                (top[3] + (bot[3] - top[3]) * Y) * va,
            )
        return
    dl.add_rect_filled_multi_color(p_min, p_max, ul, ur, br, bl)


def _circle_segments(radius):
    """ImDrawList::_CalcCircleAutoSegmentCount (table index = ceil(radius))."""
    r = int(radius + 0.999999)
    if r <= 0:
        return 48
    max_err = imgui.get_style().circle_tessellation_max_error
    n = int(math.ceil(math.pi / math.acos(1.0 - min(max_err, r) / r)))
    n = (n + 1) // 2 * 2
    return max(4, min(512, n))


def _shadow_circle(dl, center, radius, col, thickness):
    """Stand-in for the shadows-branch AddShadowCircle (not in upstream
    ImGui): a soft falloff ring of ``thickness`` px around the circle."""
    r, g, b, a = _unpack(col)
    steps = max(1, int(thickness))
    for s in range(1, steps + 1):
        k = 1.0 - s / (steps + 1.0)
        dl.add_circle(center, radius + s - 0.5, _pack(r, g, b, a * k * k * 0.35), 30, 1.0)


def _rect(x1, y1, x2, y2):
    return _it.ImRect(x1, y1, x2, y2)


# ─── template style scope ───────────────────────────────────────────────────

_DARK = None
_STYLE_VARS = None


def _dark_palette():
    """The template never styles colours globally: it runs on CreateContext's
    StyleColorsDark defaults. The overlay's global theme differs, so the menu
    re-pushes the untouched dark palette while it draws."""
    global _DARK
    if _DARK is None:
        st = imgui.Style()
        imgui.style_colors_dark(st)
        _DARK = [st.color_(i) for i in range(imgui.Col_.count.value)]
    return _DARK


def _style_vars():
    global _STYLE_VARS
    if _STYLE_VARS is None:
        sv = imgui.StyleVar_
        _STYLE_VARS = (
            # main.cpp, every frame:
            (sv.window_padding, imgui.ImVec2(0.0, 0.0)),
            (sv.item_spacing, imgui.ImVec2(10.0, 10.0)),
            (sv.window_border_size, 0.0),
            (sv.scrollbar_size, 8.0),
            # ImGuiStyle() defaults the overlay's global theme overrides:
            (sv.window_rounding, 0.0),
            (sv.child_rounding, 0.0),
            (sv.popup_rounding, 0.0),
            (sv.frame_padding, imgui.ImVec2(4.0, 3.0)),
            (sv.frame_rounding, 0.0),
            (sv.item_inner_spacing, imgui.ImVec2(4.0, 4.0)),
            (sv.indent_spacing, 21.0),
            (sv.grab_min_size, 12.0),
            (sv.grab_rounding, 0.0),
        )
    return _STYLE_VARS


_HIDDEN_GRAB = (
    imgui.Col_.scrollbar_grab,
    imgui.Col_.scrollbar_grab_hovered,
    imgui.Col_.scrollbar_grab_active,
)


def push_template_style():
    pal = _dark_palette()
    for i, c in enumerate(pal):
        imgui.push_style_color(i, c)
    for idx in _HIDDEN_GRAB:
        imgui.push_style_color(idx, _v4())
    for idx, val in _style_vars():
        imgui.push_style_var(idx, val)
    imgui.push_font(F.lexend_general_bold.font, F.lexend_general_bold.size)


def pop_template_style():
    imgui.pop_font()
    imgui.pop_style_var(len(_style_vars()))
    imgui.pop_style_color(len(_dark_palette()) + len(_HIDDEN_GRAB))


# ─── widgets (imgui_edited.cpp) ─────────────────────────────────────────────

def tab(selected, icon, label, description, size):
    """edited::Tab."""
    win = _it.get_current_window()
    if win.skip_items:
        return False
    id_ = imgui.get_id(label)
    pos = imgui.get_cursor_screen_pos()
    x, y = pos.x, pos.y
    bb = _rect(x, y, x + size[0], y + size[1])
    _it.item_size(imgui.ImVec2(size[0], size[1]), 0.0)
    if not _it.item_add(bb, id_):
        return False
    pressed, hovered, _held = _it.button_behavior(bb, id_, False, False)

    st = _anim("tab", id_, text=_v4(), icon=_v4(), shadow=_v4(), bg_alpha=0.0)
    t = _dt() * 6.0
    st.icon = _lerp4(st.icon, C.accent if selected else (C.text_hov if hovered else C.text), t)
    st.text = _lerp4(st.text, C.text_active if selected else (C.text_hov if hovered else C.text), t)
    st.bg_alpha = _lerp(st.bg_alpha, 1.0 if selected else 0.0, t)
    st.shadow = _lerp4(st.shadow, C.tab_active if selected else C.tab_border, t)

    dl = imgui.get_window_draw_list()
    mn, mx = (x, y), (x + size[0], y + size[1])
    dl.add_rect_filled(mn, mx, col32(C.tab_active, st.bg_alpha), C.el_rounding)
    render_text_color(F.lexend_regular, (x + size[1], y), mx, col32(C.text), description, (0.0, 0.7))
    rect_filled_multi(
        dl, (x + size[0] / 2, y), mx,
        col32(st.shadow, 0.0), col32(st.shadow, 1.0), col32(st.shadow, 1.0), col32(st.shadow, 0.0),
        C.el_rounding,
    )
    render_text_color(F.lexend_bold, (x + size[1], y), mx, col32(st.text), label, (0.0, 0.3))
    render_text_color(F.icomoon, mn, (x + size[1], y + size[1]), col32(st.icon), icon, (0.5, 0.5))
    return pressed


def _draw_scrollbar_grab():
    """The template's ScrollbarEx render tweak (imgui_widgets.cpp:1003-1007):
    a background_widget pill, 6 px left of the bar, 10 px inset top/bottom.
    Geometry follows 1.90's ScrollbarEx exactly."""
    win = _it.get_current_window()
    if not win.scrollbar_y:
        return
    frame = _it.get_window_scrollbar_rect(win, _it.Axis.y)
    fw = frame.max.x - frame.min.x
    fh = frame.max.y - frame.min.y
    ex = min(max(float(int((fw - 2.0) * 0.5)), 0.0), 3.0)
    ey = min(max(float(int((fh - 2.0) * 0.5)), 0.0), 3.0)
    b_min_x, b_max_x = frame.min.x + ex, frame.max.x - ex
    b_min_y, b_max_y = frame.min.y + ey, frame.max.y - ey
    style = imgui.get_style()
    scrollbar_size_v = b_max_y - b_min_y
    size_avail_v = int(win.inner_rect.max.y - win.inner_rect.min.y)
    size_contents_v = int(win.content_size.y + win.window_padding.y * 2.0)
    win_size_v = max(max(size_contents_v, size_avail_v), 1)
    grab_h_pixels = min(max(scrollbar_size_v * (size_avail_v / win_size_v), style.grab_min_size), scrollbar_size_v)
    scroll_max = max(1, size_contents_v - size_avail_v)
    scroll_ratio = _sat(int(win.scroll.y) / scroll_max)
    grab_v_norm = scroll_ratio * (scrollbar_size_v - grab_h_pixels) / scrollbar_size_v
    win.draw_list.add_rect_filled(
        (b_min_x - 6.0, _lerp(b_min_y + 10.0, b_max_y, grab_v_norm)),
        (b_max_x - 6.0, _lerp(b_min_y - 10.0, b_max_y, grab_v_norm) + grab_h_pixels),
        col32(C.el_background_widget), 30.0,
    )


def begin_child(str_id, size):
    """edited::BeginChild: padding/spacing 10, AlwaysUseWindowPadding, no border."""
    sv = imgui.StyleVar_
    imgui.push_style_var(sv.window_padding, imgui.ImVec2(10.0, 10.0))
    imgui.push_style_var(sv.item_spacing, imgui.ImVec2(10.0, 10.0))
    ret = imgui.begin_child(str_id, imgui.ImVec2(size[0], size[1]), imgui.ChildFlags_.always_use_window_padding.value)
    win = _it.get_current_window()
    # Height the column's content actually needs (last frame's layout), so
    # Menu.draw can grow the window to fit instead of cutting it off.
    _CHILD_HEIGHTS[str_id] = win.content_size.y + win.window_padding.y * 2.0
    _draw_scrollbar_grab()
    return ret


def end_child():
    imgui.pop_style_var(2)
    imgui.end_child()


def section(text):
    """``ImGui::TextColored(ImColor(GetColorU32(c::elements::text)), ...)``."""
    imgui.text_colored(_vec(col32(C.text)), text)


def _row_background(dl, x, y, w, right_solid, fade_cut):
    """The row chrome every widget shares: background, a solid block on the
    right, and a left-to-right fade that hides the description under it."""
    mx = (x + w, y + 50.0)
    dl.add_rect_filled((x, y), mx, col32(C.el_background), C.el_rounding)
    return mx


def checkbox(label, description, v):
    """edited::Checkbox. Returns (toggled, value)."""
    win = _it.get_current_window()
    if win.skip_items:
        return False, v
    id_ = imgui.get_id(label)
    w = _widget_width()
    sq = 20.0
    pos = imgui.get_cursor_screen_pos()
    x, y = pos.x, pos.y
    rect = _rect(x, y, x + w, y + 50.0)
    _it.item_size(rect, 0.0)
    if not _it.item_add(rect, id_):
        return False, v
    _it.button_behavior(rect, id_, False, False)
    toggled = False
    if imgui.is_item_clicked():
        v = not v
        toggled = True
        _it.mark_item_edited(id_)

    st = _anim("check", id_, background=_v4(), alpha=0.0, mark_pos=0.0)
    dt = _dt()
    st.background = _lerp4(st.background, C.accent if v else C.el_background_widget, dt * 6.0)
    st.alpha = _lerp(st.alpha, 1.0 if v else 0.0, dt * 6.0)
    st.mark_pos = min(max(st.mark_pos + 35.0 * dt * (-3.0 if v else 3.0), 0.0), 35.0)

    dl = imgui.get_window_draw_list()
    mx = (x + w, y + 50.0)
    dl.add_rect_filled((x, y), mx, col32(C.el_background), C.el_rounding)
    render_text_color(F.lexend_regular, (x + 10.0, y), mx, col32(C.text), description, (0.0, 0.8))
    dl.add_rect_filled((x + w - 40.0, y), mx, col32(C.el_background), C.el_rounding)
    b0, b1 = col32(C.el_background, 0.0), col32(C.el_background, 1.0)
    rect_filled_multi(dl, (x, y), (mx[0] - 20.0, mx[1]), b0, b1, b1, b0, C.el_rounding)
    box_min, box_max = (mx[0] - 37.0, mx[1] - 37.0), (mx[0] - 13.0, mx[1] - 13.0)
    dl.add_rect(box_min, box_max, col32(st.background), 2.0)
    dl.add_rect_filled(box_min, box_max, col32(st.background, 0.5), 2.0)
    imgui.push_clip_rect(box_min, box_max, True)
    _it.render_check_mark(
        dl,
        imgui.ImVec2(mx[0] - (35.0 - (sq / 2) / 2), mx[1] - (35.0 - (sq / 2 + st.mark_pos) / 2)),
        col32(C.mark, st.alpha), sq / 2,
    )
    imgui.pop_clip_rect()
    render_text_color(F.lexend_bold, (x + 10.0, y), mx, col32(C.text_active), label, (0.0, 0.2))
    return toggled, v


_FMT_START = re.compile(r"%(?!%)")
_LEADING_FLOAT = re.compile(r"\s*[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?")


def _format_value(fmt, value, is_float):
    try:
        return fmt % (value if is_float else int(value))
    except (TypeError, ValueError):
        return str(value)


def _round_to_format(fmt, value):
    """RoundScalarWithFormatT: print with the format, read the number back."""
    m = _FMT_START.search(fmt)
    if not m:
        return value
    s = _format_value(fmt[m.start():], value, True)
    n = _LEADING_FLOAT.match(s)
    return float(n.group(0)) if n else value


def slider(label, description, value, v_min, v_max, fmt, is_float):
    """edited::SliderScalar (+ ImGui 1.90 SliderBehaviorT, horizontal/linear).
    Returns (changed, value)."""
    win = _it.get_current_window()
    if win.skip_items:
        return False, value
    id_ = imgui.get_id(label)
    w = _widget_width()
    pos = imgui.get_cursor_screen_pos()
    x, y = pos.x, pos.y
    fx0, fy0, fx1, fy1 = x + 180.0, y + 29.0, x + w - 10.0, y + 39.0
    frame_bb = _rect(fx0, fy0, fx1, fy1)
    _it.item_size(_rect(x, y, x + w, y + 50.0), 0.0)
    if not _it.item_add(frame_bb, id_, frame_bb):
        return False, value
    _pressed, _hovered, held = _it.button_behavior(frame_bb, id_, False, False)

    st = _anim("slider", id_, slow=0.0, grab_off=0.0, was_held=False)

    dl = imgui.get_window_draw_list()
    mx = (x + w, y + 50.0)
    dl.add_rect_filled((x, y), mx, col32(C.el_background), C.el_rounding)
    render_text_color(F.lexend_regular, (x + 10.0, y), mx, col32(C.text), description, (0.0, 0.8))
    dl.add_rect_filled((x + w - 140.0, y), mx, col32(C.el_background), C.el_rounding)
    b0, b1 = col32(C.el_background, 0.0), col32(C.el_background, 1.0)
    rect_filled_multi(dl, (x, y), (mx[0] - 120.0, mx[1]), b0, b1, b1, b0, C.el_rounding)
    dl.add_rect_filled((fx0, fy0), (fx1, fy1), col32(C.el_background_widget), 2.0)

    # SliderBehavior(ImRect(frame_bb.Min, frame_bb.Max + ImVec2(6, 0)), ...)
    bx0, bx1, by1 = fx0, fx1 + 6.0, fy1
    style = imgui.get_style()
    grab_padding = 2.0
    slider_sz = (bx1 - bx0) - grab_padding * 2.0
    grab_sz = style.grab_min_size
    if not is_float and v_max >= v_min:
        grab_sz = max(slider_sz / (v_max - v_min + 1), style.grab_min_size)
    grab_sz = min(grab_sz, slider_sz)
    usable_sz = slider_sz - grab_sz
    usable_min = bx0 + grab_padding + grab_sz * 0.5
    usable_max = bx1 - grab_padding - grab_sz * 0.5

    def ratio(v):
        if v_min == v_max:
            return 0.0
        v = min(max(v, min(v_min, v_max)), max(v_min, v_max))
        return (v - v_min) / (v_max - v_min)

    changed = False
    if held:
        mouse_x = imgui.get_io().mouse_pos.x
        if not st.was_held:
            gp = _lerp(usable_min, usable_max, ratio(value))
            around = gp - grab_sz * 0.5 - 1.0 <= mouse_x <= gp + grab_sz * 0.5 + 1.0
            st.grab_off = (mouse_x - gp) if (around and is_float) else 0.0
        t = _sat((mouse_x - st.grab_off - usable_min) / usable_sz) if usable_sz > 0.0 else 0.0
        if t <= 0.0:
            new = v_min
        elif t >= 1.0:
            new = v_max
        elif is_float:
            new = _lerp(v_min, v_max, t)
        else:
            new = int(v_min) + int((v_max - v_min) * t + (-0.5 if v_min > v_max else 0.5))
        if is_float:
            new = _round_to_format(fmt, new)
        if new != value:
            value = new
            changed = True
            _it.mark_item_edited(id_)
    st.was_held = held

    grab_min_x = _lerp(usable_min, usable_max, ratio(value)) - grab_sz * 0.5
    grab_max_y = by1 - grab_padding
    if slider_sz < 1.0:
        grab_min_x, grab_max_y = bx0, fy0
    st.slow = _lerp(st.slow, grab_min_x - (fx0 + 5.0), _dt() * 25.0)
    dl.add_rect_filled((fx0, fy0), (st.slow + (fx0 + 9.0), grab_max_y), col32(C.accent), 2.0)

    render_text_color(F.lexend_bold, (x + 10.0, y), mx, col32(C.text_active), label, (0.0, 0.2))
    render_text_color(
        F.lexend_bold, (x, y + 7.0), (mx[0] - 15.0, mx[1] - 10.0), col32(C.text_active),
        _format_value(fmt, value, is_float), (1.0, 0.0),
    )
    return changed, value


def slider_float(label, description, value, v_min, v_max, fmt="%.3f"):
    return slider(label, description, float(value), float(v_min), float(v_max), fmt, True)


def slider_int(label, description, value, v_min, v_max, fmt="%d"):
    return slider(label, description, int(value), int(v_min), int(v_max), fmt, False)


_WIN_FLAGS = imgui.WindowFlags_


def begin_combo(label, description, preview, val, multi=False):
    """edited::BeginCombo. The dropdown is a plain window (not a popup) whose
    height lerps to ``val * 33 + 17``; it only closes on a click outside it
    or on the header, exactly like the template."""
    win = _it.get_current_window()
    if win.skip_items:
        return False
    id_ = imgui.get_id(label)
    w = _widget_width()
    pos = imgui.get_cursor_screen_pos()
    x, y = pos.x, pos.y
    bb = _rect(x, y, x + w, y + 50.0)
    rx0, ry0, rx1, ry1 = x + 180.0, y + 10.0, x + w - 10.0, y + 40.0
    _it.item_size(bb, 0.0)
    if not _it.item_add(bb, id_, bb):
        return False
    _pressed, hovered, _held = _it.button_behavior(bb, id_, False, False)

    st = _anim("combo", id_, background=_v4(), text=_v4(), combo_size=0.0,
               opened=False, hovered=False, arrow_roll=0.0)
    clicked = imgui.get_io().mouse_clicked[0]
    if (hovered and clicked) or (st.opened and clicked and not st.hovered):
        st.opened = not st.opened
    dt = _dt()
    st.arrow_roll = _lerp(st.arrow_roll, -1.0 if st.opened else 1.0, dt * 6.0)
    st.text = _lerp4(st.text, C.text_active if st.opened else (C.text_hov if hovered else C.text), dt * 6.0)
    st.combo_size = _lerp(st.combo_size, (val * 33 + 17) if st.opened else 0.0, dt * 12.0)

    dl = imgui.get_window_draw_list()
    mx = (x + w, y + 50.0)
    dl.add_rect_filled((x, y), mx, col32(C.el_background), C.el_rounding)
    render_text_color(F.lexend_regular, (x + 10.0, y), mx, col32(C.text), description, (0.0, 0.8))
    dl.add_rect_filled((x + w - 140.0, y), mx, col32(C.el_background), C.el_rounding)
    b0, b1 = col32(C.el_background, 0.0), col32(C.el_background, 1.0)
    rect_filled_multi(dl, (x, y), (mx[0] - 120.0, mx[1]), b0, b1, b1, b0, C.el_rounding)
    dl.add_rect_filled((rx0, ry0), (rx1, ry1), col32(C.el_background_widget), C.el_rounding)
    render_text_color(F.lexend_bold, (rx0 + 10.0, ry0), (rx1, ry1), col32(C.text_active), preview or "", (0.0, 0.5))
    dl.add_rect_filled((rx1 - 30.0, ry1 - 30.0), (rx1, ry1), col32(C.el_background_widget), 2.0)
    w0, w1 = col32(C.el_background_widget, 0.0), col32(C.el_background_widget, 1.0)
    rect_filled_multi(dl, (rx0, ry0), (rx1 - 30.0, ry1), w0, w1, w1, w0, 2.0)
    render_text_color(F.icomoon_widget, (rx0, ry0), (rx1 - 7.0, ry1), col32(C.accent), "z", (1.0, 0.5))
    render_text_color(F.lexend_bold, (x + 10.0, y), mx, col32(C.text_active), label, (0.0, 0.2))
    dl.add_rect_filled((rx1 - 50.0, ry1 - 50.0), (rx1, ry1), col32(st.background), C.el_rounding)

    if not imgui.is_rect_visible(imgui.ImVec2(rx0, ry0), imgui.ImVec2(rx1, ry1 + 2.0)):
        st.opened = False
        st.combo_size = 0.0
    if not st.opened and st.combo_size < 2.0:
        return False

    imgui.set_next_window_pos(imgui.ImVec2(rx0, ry1 + 5.0))
    imgui.set_next_window_size(imgui.ImVec2(rx1 - rx0, st.combo_size))
    flags = (
        _WIN_FLAGS.always_auto_resize | _WIN_FLAGS.no_title_bar | _WIN_FLAGS.no_resize
        | _WIN_FLAGS.no_saved_settings | _WIN_FLAGS.no_move | _WIN_FLAGS.no_scrollbar
        | _WIN_FLAGS.no_focus_on_appearing | _WIN_FLAGS.no_scroll_with_mouse
    )
    sv = imgui.StyleVar_
    imgui.push_style_color(imgui.Col_.window_bg, C.el_background)
    imgui.push_style_color(imgui.Col_.border, C.el_background_widget)
    imgui.push_style_var(sv.window_rounding, C.el_rounding)
    imgui.push_style_var(sv.window_padding, imgui.ImVec2(15.0, 15.0))
    imgui.push_style_var(sv.window_border_size, 1.0)
    imgui.begin(label, None, flags)
    imgui.pop_style_var(3)
    imgui.pop_style_color(2)
    st.hovered = imgui.is_window_hovered()
    if multi and st.hovered and clicked:
        st.opened = False
    return True


def end_combo():
    imgui.end()


def selectable(label, selected, size=(0.0, 0.0)):
    """edited::Selectable (the parts the combos use: no columns, no nav)."""
    win = _it.get_current_window()
    if win.skip_items:
        return False
    id_ = imgui.get_id(label)
    label_size = imgui.calc_text_size(label, None, True)
    sx = size[0] if size[0] != 0.0 else label_size.x
    sy = size[1] if size[1] != 0.0 else label_size.y
    pos = imgui.get_cursor_screen_pos()
    px, py = pos.x, pos.y + win.dc.curr_line_text_base_offset
    _it.item_size(imgui.ImVec2(sx, sy), 0.0)
    min_x, max_x = px, win.work_rect.max.x
    if size[0] == 0.0:
        sx = max(label_size.x, max_x - min_x)
    text_min = (px, py)
    text_max = (min_x + sx, py + sy)
    style = imgui.get_style()
    spx, spy = style.item_spacing.x, style.item_spacing.y
    pad_l, pad_u = float(int(spx * 0.5)), float(int(spy * 0.5))
    bx0, by0 = min_x - pad_l, py - pad_u
    bx1, by1 = text_max[0] + (spx - pad_l), text_max[1] + (spy - pad_u)
    bb = _rect(bx0, by0, bx1, by1)
    if not _it.item_add(bb, id_):
        return False
    pressed, _hovered, _held = _it.button_behavior(bb, id_, False, False)
    if pressed:
        _it.mark_item_edited(id_)

    st = _anim("select", id_, text=_v4(), opticaly=0.0)
    t = _dt() * 6.0
    st.text = _lerp4(st.text, C.accent if selected else C.text, t)
    st.opticaly = _lerp(st.opticaly, 1.0 if selected else 0.0, t)
    dl = imgui.get_window_draw_list()
    dl.add_rect_filled((bx0, by0), (bx1, by1 + 1.0), col32(C.el_background_widget, st.opticaly), 2.0)
    render_text_color(F.lexend_bold, (text_min[0] + 1.0, text_min[1] + 1.0), text_max, col32(st.text), label, (0.0, 0.5))
    return pressed


def combo(label, description, current, items):
    """edited::Combo. Returns (changed, current)."""
    preview = items[current] if 0 <= current < len(items) else ""
    if not begin_combo(label, description, preview, len(items)):
        return False, current
    changed = False
    imgui.push_style_var(imgui.StyleVar_.item_spacing, imgui.ImVec2(15.0, 15.0))
    for i, text in enumerate(items):
        imgui.push_id(i)
        is_sel = i == current
        if selectable(text, is_sel) and current != i:
            changed = True
            current = i
        if is_sel:
            imgui.set_item_default_focus()
        imgui.pop_id()
    imgui.pop_style_var()
    end_combo()
    return changed, current


def multi_combo(label, description, values, labels):
    """edited::MultiCombo. ``values`` is a list of bools, edited in place.
    Returns True if anything toggled."""
    picked = [l for l, v in zip(labels, values) if v]
    preview = ", ".join(picked) if picked else "None"
    changed = False
    if begin_combo(label, description, preview, len(labels)):
        sv = imgui.StyleVar_
        for i, text in enumerate(labels):
            imgui.push_style_var(sv.item_spacing, imgui.ImVec2(15.0, 15.0))
            imgui.push_style_var(sv.window_padding, imgui.ImVec2(15.0, 15.0))
            if selectable(text, values[i]):
                values[i] = not values[i]
                changed = True
            imgui.pop_style_var(2)
        imgui.end()
    return changed


# Keybind: edited::keys[] -- the display name per virtual-key code.
_KEYS = (
    "-", "M1", "M2", "CN", "M3", "M4", "M5", "-", "BACK", "TAB", "-", "-", "CLR", "ENTER", "-", "-",
    "SHIFT", "CTRL", "Menu", "Pause", "CAPS", "KAN", "-", "JUN", "FIN", "KAN", "-", "ESC", "CON", "NCO", "ACC", "MAD",
    "SPACE", "PGU", "PGD", "END", "HOME", "LEFT", "UP", "RIGHT", "DOWN", "SEL", "PRI", "EXE", "PRI", "INS", "DEL", "HEL",
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "-", "-", "-", "-", "-", "-",
    "-", "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O",
    "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z", "WIN", "WIN", "APP", "-", "SLE",
    "NUM0", "NUM1", "NUM2", "NUM3", "NUM4", "NUM5", "NUM6", "NUM7", "NUM8", "NUM9", "MUL", "ADD", "SEP", "MIN", "DEL", "DIV",
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12", "F13", "F14", "F15", "F16",
    "F17", "F18", "F19", "F20", "F21", "F22", "F23", "F24", "-", "-", "-", "-", "-", "-", "-", "-",
    "NUM", "SCR", "EQU", "MAS", "TOY", "OYA", "OYA", "-", "-", "-", "-", "-", "-", "-", "-", "-",
    "SHIFT", "SHIFT", "CTRL", "CTRL", "ALT", "ALT",
)
_KB_MOUSE = (0x01, 0x02, 0x04, 0x05, 0x06)
_KB_KEYS = tuple(range(0x08, 0xA6))
_kb_baseline = set()


def _keys_down():
    return {vk for vk in _KB_MOUSE + _KB_KEYS if _user32.GetAsyncKeyState(vk) & 0x8000}


def key_name(key):
    return _KEYS[key] if 0 < key < len(_KEYS) else "None"


def keybind(label, description, key):
    """edited::Keybind. Returns (changed, key).

    The C++ memsets io.MouseDown/KeysDown on activation so the click that
    activated it can't bind M1, then binds the next key that goes down; this
    does the same with a press-edge on GetAsyncKeyState (VK codes, like the
    C++'s KeysDown[0x08..0xA5] and MouseDown[0..4] -> 1,2,4,5,6)."""
    global _kb_baseline
    win = _it.get_current_window()
    if win.skip_items:
        return False, key
    io = imgui.get_io()
    id_ = imgui.get_id(label)
    width = _widget_width()
    pos = imgui.get_cursor_screen_pos()
    x, y = pos.x, pos.y
    rect = _rect(x, y, x + width, y + 50.0)
    cx0, cy0, cx1, cy1 = x + width - 80.0, y + 10.0, x + width - 10.0, y + 40.0
    _it.item_size(rect)
    if not _it.item_add(rect, id_):
        return False, key

    active = _it.get_active_id() == id_
    if key != 0 and not active:
        buf = _KEYS[key] if key < len(_KEYS) else "-"
    elif active:
        buf = "..."
    else:
        buf = "None"
    hovered = _it.item_hoverable(rect, id_, 0)

    dl = imgui.get_window_draw_list()
    mx = (x + width, y + 50.0)
    dl.add_rect_filled((cx0, cy0), (cx1, cy1), col32(C.el_background), C.el_rounding)
    render_text_color(F.lexend_regular, (x + 10.0, y), mx, col32(C.text), description, (0.0, 0.8))
    dl.add_rect_filled((x + width - 80.0, y), mx, col32(C.el_background), C.el_rounding)
    b0, b1 = col32(C.el_background, 0.0), col32(C.el_background, 1.0)
    rect_filled_multi(dl, (x, y), (mx[0] - 80.0, mx[1]), b0, b1, b1, b0, C.el_rounding)
    dl.add_rect_filled((cx0, cy0), (cx1, cy1), col32(C.el_background_widget), C.el_rounding)
    render_text_color(F.lexend_bold, (x + 10.0, y), mx, col32(C.text_active), label, (0.0, 0.2))
    render_text_color(F.lexend_bold, (cx0, cy0), (cx1, cy1), col32(C.text_active), buf, (0.5, 0.5))

    changed = False
    clicked = io.mouse_clicked[0]
    if hovered and clicked:
        if not active:
            key = 0
            _kb_baseline = _keys_down()
        _it.set_active_id(id_, win)
        _it.focus_window(win)
    elif clicked and active:
        _it.clear_active_id()

    if _it.get_active_id() == id_:
        down = _keys_down()
        fresh = down - _kb_baseline
        _kb_baseline &= down
        k = key
        for vk in _KB_MOUSE:
            if vk in fresh:
                k = vk
                changed = True
                _it.clear_active_id()
        if not changed:
            for vk in _KB_KEYS:
                if vk in fresh:
                    k = vk
                    changed = True
                    _it.clear_active_id()
        key = 0 if 0x1B in fresh else k
    return changed, key


def action_row(label, description, button_text):
    """A keybind-styled row whose right box is a button. Not a template
    widget -- the template has no plain button -- so it reuses Keybind's
    exact chrome for the menu's one action ("Reset colours")."""
    win = _it.get_current_window()
    if win.skip_items:
        return False
    id_ = imgui.get_id(label)
    width = _widget_width()
    pos = imgui.get_cursor_screen_pos()
    x, y = pos.x, pos.y
    rect = _rect(x, y, x + width, y + 50.0)
    cx0, cy0, cx1, cy1 = x + width - 80.0, y + 10.0, x + width - 10.0, y + 40.0
    _it.item_size(rect)
    if not _it.item_add(rect, id_):
        return False
    pressed, hovered, _held = _it.button_behavior(_rect(cx0, cy0, cx1, cy1), id_, False, False)
    dl = imgui.get_window_draw_list()
    mx = (x + width, y + 50.0)
    render_text_color(F.lexend_regular, (x + 10.0, y), mx, col32(C.text), description, (0.0, 0.8))
    dl.add_rect_filled((x + width - 80.0, y), mx, col32(C.el_background), C.el_rounding)
    b0, b1 = col32(C.el_background, 0.0), col32(C.el_background, 1.0)
    rect_filled_multi(dl, (x, y), (mx[0] - 80.0, mx[1]), b0, b1, b1, b0, C.el_rounding)
    dl.add_rect_filled((cx0, cy0), (cx1, cy1), col32(C.el_background_widget), C.el_rounding)
    render_text_color(F.lexend_bold, (x + 10.0, y), mx, col32(C.text_active), label, (0.0, 0.2))
    render_text_color(
        F.lexend_bold, (cx0, cy0), (cx1, cy1),
        col32(C.accent if hovered else C.text_active), button_text, (0.5, 0.5),
    )
    return pressed


# ─── colour edit + custom picker ────────────────────────────────────────────

class _PickerGlobals:
    search_col = False          # static bool search_col
    tick = 0.0                  # static DWORD dwTickStart
    add_status = True           # static bool add_status
    saved = []                  # static std::vector color_x/y/z/a
    # g.ColorEditSaved*: the template never resets ColorEditCurrentID, so the
    # saved-hue memory is effectively shared by every picker -- same here.
    saved_hue = 0.0
    saved_sat = 0.0
    saved_color = None


def _rgb_to_hsv(r, g, b):
    """ImGui::ColorConvertRGBtoHSV."""
    K = 0.0
    if g < b:
        g, b = b, g
        K = -1.0
    if r < g:
        r, g = g, r
        K = -2.0 / 6.0 - K
    chroma = r - (g if g < b else b)
    h = abs(K + (g - b) / (6.0 * chroma + 1e-20))
    s = chroma / (r + 1e-20)
    return h, s, r


def _hsv_to_rgb(h, s, v):
    """ImGui::ColorConvertHSVtoRGB."""
    if s == 0.0:
        return v, v, v
    h = math.fmod(h, 1.0) / (60.0 / 360.0)
    i = int(h)
    f = h - i
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    return ((v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v))[i] if i < 5 else (v, p, q)


def _restore_hs(col, h, s, v):
    g = _PickerGlobals
    if g.saved_color != _pack(col[0], col[1], col[2], 0.0):
        return h, s
    if s == 0.0 or (h == 0.0 and g.saved_hue == 1.0):
        h = g.saved_hue
    if v == 0.0:
        s = g.saved_sat
    return h, s


def _restore_h(col, h):
    g = _PickerGlobals
    if g.saved_color != _pack(col[0], col[1], col[2], 0.0):
        return h
    return g.saved_hue


def icon_box(icon, size, color_bg, color_icon, color_border):
    """edited::icon_box."""
    win = _it.get_current_window()
    if win.skip_items:
        return False
    pos = imgui.get_cursor_screen_pos()
    x, y = pos.x, pos.y
    rect = _rect(x, y, x + size[0], y + size[1])
    id_ = imgui.get_id(icon)
    _it.item_size(rect, 0.0)
    if not _it.item_add(rect, id_):
        return False
    pressed, _hovered, _held = _it.button_behavior(rect, id_, False, False)
    dl = imgui.get_window_draw_list()
    dl.add_rect_filled((x, y), (x + size[0], y + size[1]), u32_alpha(color_bg), C.el_rounding)
    dl.add_rect((x, y), (x + size[0], y + size[1]), u32_alpha(color_border), C.el_rounding)
    render_text_color(F.icomoon_widget, (x, y), (x + size[0], y + size[1]), u32_alpha(color_icon), icon, (0.5, 0.5))
    return pressed


def _swatch(name, size, color_bg):
    """edited::color_button (the saved-colour circles)."""
    win = _it.get_current_window()
    if win.skip_items:
        return False
    pos = imgui.get_cursor_screen_pos()
    x, y = pos.x, pos.y
    rect = _rect(x, y, x + size[0], y + size[1])
    id_ = imgui.get_id(name)
    _it.item_size(rect, 0.0)
    if not _it.item_add(rect, id_):
        return False
    pressed, _hovered, _held = _it.button_behavior(rect, id_, False, False)
    dl = imgui.get_window_draw_list()
    center = imgui.ImVec2(x + size[0] / 2, y + size[1] / 2)
    dl.add_circle_filled(center, size[0] / 2, u32_alpha(color_bg), 30)
    _shadow_circle(dl, center, size[0] / 2, u32_alpha(color_bg), 18.0)
    dl.add_circle(center, size[0] / 3, col32(C.el_background, 0.3), 30, 3.0)
    return pressed


def _color_row(desc_id, col4):
    """edited::ColorButton: the 50 px row with the swatch on the right."""
    win = _it.get_current_window()
    if win.skip_items:
        return False, None
    id_ = imgui.get_id(desc_id)
    pos = imgui.get_cursor_screen_pos()
    x, y = pos.x, pos.y
    width = _widget_width()
    rect = _rect(x, y, x + width, y + 50.0)
    k0x, k0y, k1x, k1y = x + width - 35.0, y + 15.0, x + width - 15.0, y + 35.0
    _it.item_size(rect)
    if not _it.item_add(rect, id_):
        return False, None
    pressed, _hovered, _held = _it.button_behavior(_rect(k0x, k0y, k1x, k1y), id_, False, False)
    dl = imgui.get_window_draw_list()
    dl.add_rect_filled((x, y), (x + width, y + 50.0), col32(C.el_background), C.el_rounding)
    c32 = col32(imgui.ImVec4(*col4))
    dl.add_rect_filled((k0x, k0y), (k1x, k1y), c32, C.el_rounding)
    _it.render_color_rect_with_alpha_checkerboard(
        dl, imgui.ImVec2(k0x, k0y), imgui.ImVec2(k1x, k1y), c32, 20.0 / 2.99, imgui.ImVec2(0.0, 0.0), C.el_rounding,
    )
    return pressed, (rect, id_)


def _color_picker(label, col):
    """edited::ColorPicker4 with the flags ColorEdit4 forwards
    (AlphaBar | NoLabel | AlphaPreviewHalf, hue-bar, RGB input)."""
    win = _it.get_current_window()
    if win.skip_items:
        return False
    dl = imgui.get_window_draw_list()
    io = imgui.get_io()
    style = imgui.get_style()
    width = imgui.calc_item_width()
    imgui.push_id(label)
    imgui.begin_group()

    picker = imgui.get_cursor_screen_pos()
    px, py = picker.x, picker.y
    bars_width = 15.0
    sv = max(bars_width, width - 2 * bars_width)
    bar0_x = px + sv + 10.0
    bar1_x = bar0_x + bars_width + 10.0
    backup = list(col[:4])

    H, S, V = _rgb_to_hsv(col[0], col[1], col[2])
    H, S = _restore_hs(col, H, S, V)
    R, G, B = col[0], col[1], col[2]

    changed = changed_h = changed_sv = False
    imgui.invisible_button("sv", imgui.ImVec2(sv, sv))
    if imgui.is_item_active():
        S = _sat((io.mouse_pos.x - px) / (sv - 1))
        V = 1.0 - _sat((io.mouse_pos.y - py) / (sv - 1))
        H = _restore_h(col, H)
        changed = changed_sv = True
    imgui.set_cursor_screen_pos(imgui.ImVec2(bar0_x, py))
    imgui.invisible_button("hue", imgui.ImVec2(bars_width, sv))
    if imgui.is_item_active():
        H = _sat((io.mouse_pos.y - py) / (sv - 1))
        changed = changed_h = True
    imgui.set_cursor_screen_pos(imgui.ImVec2(bar1_x, py))
    imgui.invisible_button("alpha", imgui.ImVec2(bars_width, sv))
    if imgui.is_item_active():
        col[3] = 1.0 - _sat((io.mouse_pos.y - py) / (sv - 1))
        changed = True

    if changed_h or changed_sv:
        col[0], col[1], col[2] = _hsv_to_rgb(H, S, V)
        g = _PickerGlobals
        g.saved_hue, g.saved_sat = H, S
        g.saved_color = _pack(col[0], col[1], col[2], 0.0)
    if changed:
        R, G, B = col[0], col[1], col[2]
        H, S, V = _rgb_to_hsv(R, G, B)
        H, S = _restore_hs(col, H, S, V)

    a8 = _f2u8(style.alpha)
    black = a8 << 24
    white = (a8 << 24) | 0xFFFFFF
    hues = [(a8 << 24) | c for c in (0x0000FF, 0x00FFFF, 0x00FF00, 0xFFFF00, 0xFF0000, 0xFF00FF, 0x0000FF)]
    hr, hg, hb = _hsv_to_rgb(H, 1.0, 1.0)
    hue32 = _pack(hr, hg, hb, style.alpha)
    user32 = _pack(R, G, B, style.alpha)

    st = _anim("picker", imgui.get_id(label), hue_bar=0.0, alpha_bar=0.0, circle=0.0, cmx=0.0, cmy=0.0)
    dt = _dt()

    rect_filled_multi(dl, (px, py), (px + sv, py + sv), white, hue32, hue32, white, 5.0)
    rect_filled_multi(dl, (px - 1, py - 1), (px + sv + 1, py + sv + 1), 0, 0, black, black, 5.0)

    svx = min(max(math.floor(px + _sat(S) * sv + 0.5), px + 2), px + sv - 2)
    svy = min(max(math.floor(py + _sat(1 - V) * sv + 0.5), py + 2), py + sv - 2)

    for i in range(6):
        flags = _RC_TOP if i == 0 else (_RC_BOTTOM if i == 5 else _RC_NONE)
        rect_filled_multi(
            dl,
            (bar0_x, py + i * (sv / 6) - (1 if i == 5 else 0)),
            (bar0_x + bars_width, py + (i + 1) * (sv / 6) + (1 if i == 0 else 0)),
            hues[i], hues[i], hues[i + 1], hues[i + 1], 10.0, flags,
        )
    line0 = math.floor(py + H * sv + 0.5)
    line0 = min(max(line0, py + 3.0), py + (sv - 13))
    st.hue_bar = _lerp(st.hue_bar, line0 + 5, dt * 24.0)
    dl.add_circle_filled(imgui.ImVec2(bar0_x + 7.5, st.hue_bar), 4.5, black, 100)

    rad = 10.0 if changed_sv else 6.0
    st.cmx = _lerp(st.cmx, svx, dt * 10.0)
    st.cmy = _lerp(st.cmy, svy, dt * 10.0)
    st.circle = _lerp(st.circle, 6.0 if changed_sv else 4.0, dt * 24.0)
    dl.add_circle(imgui.ImVec2(st.cmx, st.cmy), st.circle, white, _circle_segments(rad), 2.0)

    alpha = _sat(col[3])
    user_na = user32 & 0x00FFFFFF
    rect_filled_multi(dl, (bar1_x, py), (bar1_x + bars_width, py + sv), user32, user32, user_na, user_na, 100.0)
    line1 = math.floor(py + (1.0 - alpha) * sv + 0.5)
    line1 = min(max(line1, py + 3.0), py + (sv - 13))
    st.alpha_bar = _lerp(st.alpha_bar, line1 + 5, dt * 24.0)
    dl.add_circle_filled(imgui.ImVec2(bar1_x + 7.5, st.alpha_bar), 4.5, black, 100)

    imgui.end_group()
    if changed and list(col[:4]) == backup:
        changed = False
    imgui.pop_id()
    return changed


def _sample_screen_pixel():
    pt = ctypes.wintypes.POINT() if hasattr(ctypes, "wintypes") else None
    if pt is None:
        import ctypes.wintypes  # noqa: F401  (lazy: only the eyedropper needs it)
        pt = ctypes.wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    hdc = _user32.GetDC(None)
    try:
        c = _gdi32.GetPixel(hdc, pt.x, pt.y)
    finally:
        _user32.ReleaseDC(None, hdc)
    return (c & 0xFF) / 255.0, ((c >> 8) & 0xFF) / 255.0, ((c >> 16) & 0xFF) / 255.0


def color_edit4(label, description, col):
    """edited::ColorEdit4 with main.cpp's picker_flags (NoSidePreview |
    AlphaBar | NoInputs | AlphaPreview). ``col`` is a 4-float list, edited in
    place. Returns True if it changed.

    One behavioural deviation: the C++ HEX field parses into ``i[]`` and
    never writes it back to ``col``, so typing a hex code does nothing there.
    Here it applies -- same look, a field that works."""
    g = _PickerGlobals
    io = imgui.get_io()
    style = imgui.get_style()
    imgui.begin_group()
    imgui.push_id(label)
    pos = imgui.get_cursor_screen_pos()
    changed = False

    ints = [int(v * 255.0 + (0.5 if v >= 0 else -0.5)) for v in col[:4]]
    st = _anim("edit", imgui.get_id(label), text=_v4(), alpha_search=0.0, hovered=False, active=False)

    _pressed, last = _color_row("##ColorButton", col)
    if last is not None:
        row_rect, row_id = last
        clicked = io.mouse_clicked[0]
        if (_it.item_hoverable(row_rect, row_id, 0) and clicked) or (
            st.active and not g.search_col and clicked and not st.hovered
        ):
            st.active = not st.active
    st.alpha_search = _lerp(st.alpha_search, 1.0 if g.search_col else 0.0, _dt() * 6.0)

    sv = imgui.StyleVar_
    imgui.push_style_color(imgui.Col_.window_bg, _vec(col32(C.el_background)))
    imgui.push_style_color(imgui.Col_.border, _vec(col32(C.el_background_widget)))
    imgui.push_style_var(sv.window_rounding, C.el_rounding)
    imgui.push_style_var(sv.window_padding, imgui.ImVec2(15.0, 15.0))
    imgui.push_style_var(sv.window_border_size, 1.0)

    if st.active and last is not None:
        row_rect = last[0]
        imgui.set_next_window_pos(imgui.ImVec2(row_rect.max.x - 45.0, row_rect.min.y - 5.0))
        imgui.begin(
            "picker", None,
            _WIN_FLAGS.no_decoration | _WIN_FLAGS.no_move | _WIN_FLAGS.always_auto_resize,
        )
        st.hovered = imgui.is_window_hovered()

        if g.search_col:
            now = time.perf_counter()
            if now - g.tick > 0.150:
                col[0], col[1], col[2] = _sample_screen_pixel()
                g.tick = now
                changed = True
                if _user32.GetAsyncKeyState(0x01) & 0x8000:
                    g.search_col = False

        c = [min(max(v, 0), 255) for v in ints]
        buf = "#%02X%02X%02X%02X" % tuple(c)

        dl = imgui.get_window_draw_list()
        cur = imgui.get_cursor_screen_pos()
        dl.add_text(F.lexend_bold.font, 17.0, imgui.ImVec2(cur.x, cur.y - 2.0), col32(C.text_active),
                    _display(label) or "Color picker")
        imgui.push_font(F.icomoon_widget2.font, F.icomoon_widget2.size)
        dl.add_text(
            imgui.ImVec2(cur.x + _region_max()[0] - (imgui.calc_text_size("l").x + 15.0), cur.y),
            col32(C.text_hov), "l",
        )
        imgui.pop_font()
        imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() + 30.0)

        imgui.set_next_item_width(18.0 * 11.5)
        changed |= _color_picker("##picker", col)

        if icon_box("u", (35.0, 35.0), col32(C.el_background_widget), col32(C.accent), col32(C.accent, st.alpha_search)):
            g.search_col = True
        imgui.same_line(0.0, 15.0)
        changed |= _hex_input(buf, col)
        imgui.same_line(0.0, 3.0)

        if icon_box("g" if g.add_status else "k", (35.0, 35.0), col32(C.el_background_widget), col32(C.accent), col32(C.accent, 0.0)):
            if g.add_status:
                g.saved.append(tuple(col[:4]))
            elif g.saved:
                g.saved.pop()
        if imgui.is_item_hovered() and io.mouse_clicked[1]:
            g.add_status = not g.add_status

        for i, sc in enumerate(g.saved):
            if _swatch(str(i), (17.0, 17.0), _pack(*sc)):
                col[0], col[1], col[2], col[3] = sc
                changed = True
            if (i + 1) % 7 != 0:
                imgui.same_line(0.0, 18.0)
        imgui.end()

    imgui.pop_style_color(2)
    imgui.pop_style_var(3)

    st.text = _lerp4(st.text, C.text_active if st.active else C.text, _dt() * 6.0)
    rmx = _region_max()[0]
    render_text_color(F.lexend_regular, (pos.x + 10.0, pos.y), (pos.x + rmx, pos.y + 50.0), col32(C.text), description, (0.0, 0.8))
    render_text_color(F.lexend_bold, (pos.x + 10.0, pos.y), (pos.x + rmx, pos.y + 50.0), col32(C.text_active), label, (0.0, 0.2))

    imgui.pop_id()
    imgui.end_group()
    return changed


_HEX_FLAGS = imgui.InputTextFlags_.chars_hexadecimal.value | imgui.InputTextFlags_.chars_uppercase.value


def _hex_input(buf, col):
    """The template's restyled InputTextEx("v", "HEX COLOR", ..., (128, 35)):
    background/border boxes, Lexend text, right-hand fade and pen icon."""
    hint = "HEX COLOR"
    w, h = 128.0, 35.0
    dl = imgui.get_window_draw_list()
    pos = imgui.get_cursor_screen_pos()
    fx0, fy0, fx1, fy1 = pos.x, pos.y, pos.x + w, pos.y + h
    dl.add_rect_filled((fx0, fy0), (fx1, fy1), col32(C.el_background), C.el_rounding)
    dl.add_rect((fx0, fy0), (fx1, fy1), col32(C.el_background_widget), C.el_rounding)

    imgui.push_font(F.lexend_bold.font, F.lexend_bold.size)
    hint_h = imgui.calc_text_size(hint).y
    sv = imgui.StyleVar_
    imgui.push_style_var(sv.frame_padding, imgui.ImVec2((h - hint_h) / 2, (h - F.lexend_bold.size) / 2))
    imgui.push_style_var(sv.frame_border_size, 0.0)
    imgui.push_style_color(imgui.Col_.frame_bg, _v4())
    imgui.push_style_color(imgui.Col_.frame_bg_hovered, _v4())
    imgui.push_style_color(imgui.Col_.frame_bg_active, _v4())
    imgui.push_style_color(imgui.Col_.text, _vec(col32(C.text_active)))
    imgui.push_style_color(imgui.Col_.text_disabled, _vec(col32(C.text)))
    imgui.push_style_color(imgui.Col_.text_selected_bg, _vec(col32(C.accent, 0.1)))
    imgui.set_next_item_width(w)
    edited, text = imgui.input_text_with_hint("##v", hint, buf, _HEX_FLAGS)
    imgui.pop_style_color(6)
    imgui.pop_style_var(2)
    imgui.pop_font()

    w0, w1 = col32(C.el_background, 0.0), col32(C.el_background, 1.0)
    rect_filled_multi(dl, (fx0 + 1, fy0 + 1), (fx1 - (h - hint_h - 1), fy1 - 1), w0, w1, w1, w0, C.el_rounding)
    imgui.push_font(F.icomoon_widget2.font, F.icomoon_widget2.size)
    iw = imgui.calc_text_size("v").x
    dl.add_text(imgui.ImVec2(fx1 - (h + iw) / 2, fy1 - (h + iw) / 2), col32(C.accent), "v")
    imgui.pop_font()

    if not edited:
        return False
    digits = text.lstrip("# ").strip()
    try:
        vals = [int(digits[i:i + 2], 16) for i in range(0, min(len(digits), 8), 2) if len(digits[i:i + 2]) == 2]
    except ValueError:
        return False
    if len(vals) < 3:
        return False
    col[0], col[1], col[2] = vals[0] / 255.0, vals[1] / 255.0, vals[2] / 255.0
    if len(vals) >= 4:
        col[3] = vals[3] / 255.0
    return True


# ─── main.cpp: notifications, info bar, menu frame ──────────────────────────

def _circle_progress(progress, max_value, radius, offset):
    """main.cpp CricleProgress (drawn on the foreground list)."""
    position = progress / max_value * 6.28
    fg = imgui.get_foreground_draw_list()
    cur = imgui.get_cursor_screen_pos()
    center = imgui.ImVec2(cur.x + offset[0], cur.y + offset[1])
    fg.path_clear()
    fg.path_arc_to(center, radius, 0.0, 2.0 * math.pi, 120)
    fg.path_stroke(col32(C.el_background_widget), 0, 3.0)
    fg.path_clear()
    fg.path_arc_to(center, radius, math.pi * 1.5, math.pi * 1.5 + position, 120)
    fg.path_stroke(col32(C.accent), 0, 3.0)


class Notifications:
    """main.cpp NotificationSystem."""

    def __init__(self):
        self._items = []
        self._next_id = 0
        self.design = 0  # notify_select: 0 = Circle, 1 = Line

    def add(self, message, duration_ms):
        now = time.monotonic()
        self._items.append((self._next_id, message, now, now + duration_ms / 1000.0))
        self._next_id += 1

    def draw(self):
        now = time.monotonic()

        def remaining(n):
            dur = n[3] - n[2]
            return (dur - (now - n[2])) / dur

        self._items.sort(key=remaining)
        slot = 0
        for n in self._items:
            if now < n[3]:
                dur = n[3] - n[2]
                self._show(slot, n[1], (dur - (now - n[2])) / dur * 100.0)
                slot += 1
        self._items = [n for n in self._items if now < n[3]]

    def _show(self, position, message, percentage):
        due = 100.0 - percentage
        alpha = 1.0 if percentage > 10.0 else percentage / 10.0
        imgui.push_style_var(imgui.StyleVar_.window_padding, imgui.ImVec2(15.0, 10.0))
        imgui.set_next_window_pos(imgui.ImVec2(due if due < 15.0 else 15.0, 15.0 + position * 90.0))
        imgui.begin(
            "##NOTIFY%d" % position, None,
            _WIN_FLAGS.no_decoration | _WIN_FLAGS.always_auto_resize
            | _WIN_FLAGS.no_move | _WIN_FLAGS.no_bring_to_front_on_focus,
        )
        wp = imgui.get_window_pos()
        rx, ry = _region_max()
        bg = imgui.get_background_draw_list()
        rect_filled_multi(
            bg, (wp.x, wp.y), (wp.x + rx, wp.y + ry),
            col32(C.bg_filling, alpha), col32(C.accent, 0.01), col32(C.accent, 0.01), col32(C.bg_filling, alpha),
            C.el_rounding,
        )
        bg.add_rect_filled((wp.x, wp.y), (wp.x + rx, wp.y + ry), col32(C.bg_filling, 0.4), C.el_rounding)
        bg.add_rect((wp.x, wp.y), (wp.x + rx, wp.y + ry), col32(C.bg_stroke, alpha), C.el_rounding)
        if self.design == 0:
            _circle_progress(percentage, 100.0, 7.0, (rx - 40.0, 11.0))
        if self.design == 1:
            bg.add_rect_filled(
                (wp.x, wp.y + ry - 3.0), (wp.x + rx * (due / 100.0), wp.y + ry),
                col32(C.accent, alpha), C.el_rounding,
            )
        imgui.push_font(F.lexend_bold.font, F.lexend_bold.size)
        imgui.text_colored(_vec(col32(C.accent, alpha)), "[Notification]")
        imgui.text_colored(_vec(col32(C.text_active, alpha)), message)
        imgui.dummy(imgui.ImVec2(imgui.calc_text_size(message).x + 15.0, 5.0))
        imgui.pop_font()
        imgui.end()
        imgui.pop_style_var()


def info_bar(values):
    """main.cpp's "info-bar": six fields [name, a, b, c, d, time]. The width
    formula is the template's own (it double-counts some fields -- kept)."""
    imgui.push_font(F.lexend_bold.font, F.lexend_bold.size)
    name, dev, role, ping, fps, wtime = values
    cs = imgui.calc_text_size
    bar = cs("|").x
    ibar = (cs(name).x + bar + cs(dev).x + bar + cs(role).x + bar + cs(ping).x + cs(fps).x
            + cs(dev).x + bar + cs(wtime).x * 2 + bar * 3)
    io = imgui.get_io()
    imgui.set_next_window_pos(imgui.ImVec2(io.display_size.x - (ibar + 15.0), 15.0))
    imgui.set_next_window_size(imgui.ImVec2(ibar, 45.0))
    imgui.begin(
        "info-bar", None,
        _WIN_FLAGS.always_auto_resize | _WIN_FLAGS.no_background | _WIN_FLAGS.no_decoration,
    )
    wp = imgui.get_window_pos()
    rx, ry = _region_max()
    bg = imgui.get_background_draw_list()
    bg.add_rect_filled((wp.x, wp.y), (wp.x + rx, wp.y + ry), col32(C.bg_filling), C.el_rounding)
    bg.add_rect((wp.x, wp.y), (wp.x + rx, wp.y + ry), col32(C.bg_stroke), C.el_rounding)
    spacing = imgui.get_style().item_spacing
    imgui.set_cursor_pos(imgui.ImVec2(spacing.x, (45.0 - cs(dev).y) / 2))
    imgui.begin_group()
    for i, text in enumerate(values):
        imgui.text_colored(_vec(col32(C.accent if i < 1 else C.text)), text)
        imgui.same_line()
        imgui.text_colored(_vec(col32(C.text)), "|")
        imgui.same_line()
    imgui.end_group()
    imgui.end()
    imgui.pop_font()


class Menu:
    """main.cpp's window: 900x515, a 200 px tab strip, and the tab content
    sliding in (cursor y = 100 - alpha*100) while fading (style.Alpha)."""

    def __init__(self, tabs):
        # tabs: [(icon, label, description, notification), ...]
        self.tabs = tabs
        self.page = 0
        self.active_tab = 0
        self.tab_alpha = 0.0
        self.tab_add = 0.0
        self.notifications = Notifications()
        self.height = C.bg_size[1]
        self._pos_y = 60.0  # ImGui's default position for a new window

    def draw(self, draw_tab, info_values=None):
        """``draw_tab(index)`` renders the two content columns."""
        push_template_style()
        try:
            self.notifications.draw()
            # Responsive height (not in the C++ template, whose 515 px panel
            # cuts long tabs off): grow to fit the taller column, never below
            # the template's 515, never past the bottom of the screen -- there
            # the columns' own scrolling takes over.
            global _menu_height
            io = imgui.get_io()
            wanted = max(C.bg_size[1], max(_CHILD_HEIGHTS.values(), default=0.0))
            room = io.display_size.y - self._pos_y - 10.0
            wanted = min(wanted, max(room, 300.0))
            self.height = _lerp(self.height, wanted, min(1.0, _dt() * 12.0))
            if abs(self.height - wanted) < 0.5:
                self.height = wanted
            _menu_height = self.height
            size = (C.bg_size[0], self.height)
            imgui.set_next_window_size(imgui.ImVec2(*size))
            imgui.begin("IMGUI!", None, _WIN_FLAGS.no_decoration | _WIN_FLAGS.no_bring_to_front_on_focus)
            wp = imgui.get_window_pos()
            self._pos_y = wp.y
            x, y = wp.x, wp.y
            bg = imgui.get_background_draw_list()
            bg.add_rect_filled((x, y), (x + size[0], y + size[1]), col32(C.bg_filling), C.bg_rounding)
            bg.add_rect_filled((x, y), (x + 200.0, y + size[1]), col32(C.tab_border), C.bg_rounding, _RC_LEFT)
            bg.add_line((x + 200.0, y), (x + 200.0, y + size[1]), col32(C.bg_stroke), 1.0)
            bg.add_rect((x, y), (x + size[0], y + size[1]), col32(C.bg_stroke), C.bg_rounding)

            imgui.set_cursor_pos(imgui.ImVec2(10.0, 10.0))
            imgui.begin_group()
            for i, (icon, label, desc, note) in enumerate(self.tabs):
                if tab(self.page == i, icon, label, desc, (180.0, 50.0)):
                    self.notifications.add(note, 1000)
                    self.page = i
            imgui.end_group()

            self.tab_alpha = _lerp(self.tab_alpha, 1.0 if self.page == self.active_tab else 0.0, 15.0 * _dt())
            if self.tab_alpha < 0.01 and self.tab_add < 0.01:
                self.active_tab = self.page

            imgui.set_cursor_pos(imgui.ImVec2(200.0, 100.0 - (self.tab_alpha * 100.0)))
            imgui.push_style_var(imgui.StyleVar_.alpha, self.tab_alpha * imgui.get_style().alpha)
            try:
                draw_tab(self.active_tab)
            finally:
                imgui.pop_style_var()
            imgui.end()

            if info_values is not None:
                info_bar(info_values)
        finally:
            pop_template_style()

    @staticmethod
    def column_size():
        return ((C.bg_size[0] - 200.0) / 2, _menu_height)
