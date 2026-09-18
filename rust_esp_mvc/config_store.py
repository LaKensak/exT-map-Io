"""Config slots for the overlay's Settings (toggles, sliders, keys, colours).

The overlay never takes keyboard focus (WS_EX_NOACTIVATE, see view_base), so
a config cannot be named by typing: there are SLOT_COUNT fixed slots,
configs/config_N.json next to this file. _state.json remembers the slot in
use and whether to auto-save to it on exit.

Loading is defensive by design -- a file written by an older or newer build
must never crash the overlay or poison a setting: only keys the current
Settings() actually has are applied, and only when the stored value has the
same type as that setting's default. Everything else is counted and ignored.
"""

import json
import math
import os
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent / "configs"
SLOT_COUNT = 5
_STATE_FILE = "_state.json"
_BAD = object()


def slot_path(slot):
    return CONFIG_DIR / f"config_{slot + 1}.json"


def slot_labels():
    return [
        f"Config {i + 1}" + ("" if slot_path(i).exists() else " (empty)")
        for i in range(SLOT_COUNT)
    ]


def _defaults(settings):
    # A fresh instance is the source of truth for which keys exist and what
    # type each one has; it also keeps derived state (_packed) out of files.
    return vars(type(settings)())


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _coerce(default, value):
    if isinstance(default, bool):
        return value if isinstance(value, bool) else _BAD
    if isinstance(default, int):
        if isinstance(value, bool):
            return _BAD
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return _BAD
    if isinstance(default, float):
        if _is_number(value) and math.isfinite(value):
            return float(value)
        return _BAD
    if isinstance(default, str):
        return value if isinstance(value, str) else _BAD
    return _BAD


def _rgba(value):
    if (
        isinstance(value, (list, tuple))
        and len(value) == 4
        and all(_is_number(c) and math.isfinite(c) for c in value)
    ):
        return tuple(min(1.0, max(0.0, float(c))) for c in value)
    return None


def to_dict(settings):
    data = {}
    for key, default in _defaults(settings).items():
        if key.startswith("_"):
            continue
        value = getattr(settings, key, default)
        if key == "colors":
            data[key] = {k: [float(c) for c in v] for k, v in value.items()}
        elif isinstance(value, dict):
            data[key] = dict(value)
        else:
            data[key] = value
    return data


def apply_dict(settings, data):
    """Apply a to_dict() mapping. Returns (applied, skipped) counts."""
    applied = skipped = 0
    for key, default in _defaults(settings).items():
        if key.startswith("_") or key not in data:
            continue
        value = data[key]
        if key == "colors":
            if not isinstance(value, dict):
                skipped += 1
                continue
            for name, rgba in value.items():
                rgba = _rgba(rgba)
                if name in default and rgba is not None:
                    settings.set_col(name, rgba)
                    applied += 1
                else:
                    skipped += 1
            continue
        if isinstance(default, dict):
            if not isinstance(value, dict):
                skipped += 1
                continue
            merged = dict(getattr(settings, key, default))
            for name, item in value.items():
                coerced = _coerce(default[name], item) if name in default else _BAD
                if coerced is _BAD:
                    skipped += 1
                else:
                    merged[name] = coerced
                    applied += 1
            setattr(settings, key, merged)
            continue
        coerced = _coerce(default, value)
        if coerced is _BAD:
            skipped += 1
        else:
            setattr(settings, key, coerced)
            applied += 1
    return applied, skipped


def _write_json(path, payload):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)  # never leaves a half-written config behind


def save(settings, slot, menu=None):
    payload = {"version": 1, "settings": to_dict(settings)}
    if menu:
        payload["menu"] = menu
    path = slot_path(slot)
    _write_json(path, payload)
    return path


def load(settings, slot):
    """Returns (applied, skipped, menu_prefs). Raises FileNotFoundError for an
    empty slot and ValueError for a file that is not a config."""
    data = json.loads(slot_path(slot).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("settings"), dict):
        raise ValueError("not a config file")
    applied, skipped = apply_dict(settings, data["settings"])
    menu = data.get("menu")
    return applied, skipped, menu if isinstance(menu, dict) else {}


def read_state():
    try:
        data = json.loads((CONFIG_DIR / _STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    slot = data.get("slot", 0)
    if not isinstance(slot, int) or isinstance(slot, bool) or not 0 <= slot < SLOT_COUNT:
        slot = 0
    autosave = data.get("autosave", True)
    return {"slot": slot, "autosave": autosave if isinstance(autosave, bool) else True}


def write_state(state):
    _write_json(CONFIG_DIR / _STATE_FILE, {"slot": state["slot"], "autosave": state["autosave"]})
