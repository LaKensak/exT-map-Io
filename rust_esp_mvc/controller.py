"""Controller layer: application lifecycle and Model/View coordination."""

import ctypes
import sys
import time

from .model import (
    Mem,
    RustGameModel,
    find_modules,
    print_module_diagnostics,
    read_view_proj_matrix,
)
from .view import OverlayDependencyError, OverlayView
from .aim_engine import AimEngine
from .chams_engine import ChamsEngine
from .recoil_engine import RecoilEngine
from .sway_engine import SwayEngine
from . import view_base as base


class EspController:
    def __init__(self):
        self._winmm = None
        self._timer_period_set = False
        self._view = None
        self._aim_engine = None
        self._chams_engine = None
        self._user32 = ctypes.windll.user32
        # GetAsyncKeyState prototype : takes int VK, returns SHORT.
        self._user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self._user32.GetAsyncKeyState.restype = ctypes.c_short

    def _enable_precise_timer(self):
        try:
            self._winmm = ctypes.windll.winmm
            self._winmm.timeBeginPeriod(1)
            self._timer_period_set = True
            print("[+] Résolution scheduler Windows mise à 1 ms")
        except Exception:
            pass

    def _cleanup(self):
        if self._view is not None:
            self._view.close()
        if self._timer_period_set and self._winmm is not None:
            try:
                self._winmm.timeEndPeriod(1)
            except Exception:
                pass

    def run(self):
        self._enable_precise_timer()
        memory = Mem()
        if not memory.connect():
            print("[!] Driver non connecté — vérifie qu'il est chargé")
            self._cleanup()
            return
        # Only StealthDriver's event-based build waits on this; the
        # currently-loaded polling build ignores it entirely, so a failure
        # here does not mean the driver is broken -- it means the client
        # created its own orphan event object that nobody listens to (which
        # is harmless: _signal_command() still works, it just wastes a
        # SetEvent() syscall per command). Not worth aborting startup over.
        if not memory.init_event():
            print("[!] Event de commande du driver indisponible (normal avec "
                  "le driver en mode polling) -- on continue")
        if not memory.attach(timeout=120.0):
            print("[!] RustClient.exe non trouvé après 2 minutes")
            self._cleanup()
            return

        memory.find_base()
        print(f"[+] Base: 0x{memory.base:X}  PID: {memory.pid}")
        ga_base, up_base = find_modules(memory)
        print(f"[+] GameAssembly.dll: 0x{ga_base:X}")
        print(f"[+] UnityPlayer.dll:  0x{up_base:X}")
        if not ga_base:
            print_module_diagnostics(memory)
            print("[!] GameAssembly.dll introuvable — abandon")
            self._cleanup()
            return

        try:
            self._view = OverlayView()
        except OverlayDependencyError as exc:
            print("[!] imgui-bundle ou glfw indisponible.")
            print(f"    {exc}")
            self._cleanup()
            return

        model = RustGameModel(memory, ga_base)
        model.start_worker()

        # Init the aim engine — writes bodyAngles directly to PlayerInput
        self._recoil_engine = RecoilEngine(memory)
        # AimEngine needs the recoil engine's weapon resolution (belt walk ->
        # held Item -> shortname) to know when the equipped weapon is a
        # projectile (bow/crossbow/nailgun) and needs drop/lead compensation
        # instead of a straight-line aim point.
        self._aim_engine = AimEngine(memory, recoil_engine=self._recoil_engine)
        self._chams_engine = ChamsEngine(memory)
        self._sway_engine = SwayEngine(memory, self._recoil_engine)
        print("[+] Aim, Chams, Recoil, and Sway engines initialisés")

        print("[+] Rust ESP MVC prêt")
        print(
            f"[+] Rendu synchronisé à {self._view.refresh_hz} Hz"
        )

        last_vp = None
        last_camera_pos = None
        last_camera_at = 0.0
        # Camera basis vectors in world space -- fresh every frame, used
        # only for diagnostics now (aim uses absolute angles).
        last_cam_right = None
        last_cam_up = None
        last_cam_forward = None

        try:
            while not self._view.should_close():
                self._view.poll()
                # Tell the worker what is actually being drawn. A feature the
                # user switched off must cost zero reads: the world-entity
                # scan alone resolves 200+ transform chains per pass, which
                # used to run even with "World entities" unticked.
                vs = self._view.settings
                model.wanted = {
                    'world_entities': vs.show_world_entities,
                    'names': vs.show_name,
                    'held_item': vs.show_held_item,
                }
                model.skeleton_render_distance = vs.skeleton_render_distance
                players, world_entities, _, diag, tick_ms = model.get_snapshot(
                    render_at=time.perf_counter(),
                )
                with model._lock:
                    local_pos = getattr(model, "local_pos", None)

                # Sample the camera after snapshot preparation, immediately
                # before world-to-screen projection, to minimize VP age.
                camera_result = read_view_proj_matrix(memory, ga_base)
                camera_at = time.perf_counter()
                if camera_result is not None:
                    vp, camera_pos, cam_right, cam_up, cam_forward = camera_result
                    last_vp = tuple(vp)
                    last_camera_pos = tuple(camera_pos)
                    last_cam_right = cam_right
                    last_cam_up = cam_up
                    last_cam_forward = cam_forward
                    last_camera_at = camera_at

                now = time.perf_counter()
                camera_age_ms = (
                    (now - last_camera_at) * 1000.0
                    if last_camera_at > 0.0
                    else -1.0
                )

                # ── Aimbot (write-angles direct) ──
                if vs.aim_enabled and self._aim_engine is not None:
                    aim_key = getattr(vs, "aim_key", 0x02)
                    key_down = bool(
                        self._user32.GetAsyncKeyState(aim_key) & 0x8000
                    )
                    if key_down and players:
                        # Same guard the slow lane already uses: a raised
                        # exception here propagates all the way out of run()
                        # and ends the session mid-game. Print it and keep
                        # the overlay alive instead.
                        try:
                            self._aim_engine.tick(model, players, vs)
                        except Exception as exc:
                            print(f"[AIM-DBG] aim tick failed: {exc!r}", flush=True)


                # ── No Recoil ──
                if getattr(vs, 'norecoil_enabled', False) or (self._recoil_engine and self._recoil_engine._needs_reset):
                    if self._recoil_engine is not None:
                        self._recoil_engine.tick(model, vs)

                # ── No Sway / No Bloom ──
                if getattr(vs, 'nosway_enabled', False) or (self._sway_engine and self._sway_engine._needs_reset):
                    if self._sway_engine is not None:
                        self._sway_engine.tick(model, vs)

                # ── Chams (Material Override) ──
                if vs.chams_enabled and self._chams_engine is not None:
                    if players:
                        self._chams_engine.tick(model, players, vs)

                self._view.render(
                    players=players,
                    world_entities=world_entities,
                    vp=last_vp,
                    local_pos=local_pos,
                    camera_pos=last_camera_pos,
                    camera_age_ms=camera_age_ms,
                    diag=diag,
                    tick_ms=tick_ms,
                )
        except KeyboardInterrupt:
            pass
        finally:
            self._cleanup()


def main():
    # Several debug lines carry non-ASCII (arrows, delta, check marks). A
    # real console renders them fine, but the moment stdout is redirected
    # (`| Tee-Object`, `> log.txt`) Python falls back to the locale codec --
    # cp1252 here -- and printing one raises UnicodeEncodeError, which
    # propagates out of run() and ends the session. Degrade to '?' instead.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    EspController().run()
