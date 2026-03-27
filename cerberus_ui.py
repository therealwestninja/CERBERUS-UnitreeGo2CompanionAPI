"""
CERBERUS UI
===========
Dear PyGui-based operator interface.

Design language:
  • Utilitarian / industrial — not cute, not tactical.
    Flat dark panels, tight grids, monospace telemetry, clear status colours.
  • Never calls robot code.  All actions go through UIBridge.send_command().
  • Runs in the main thread.  Runtime lives on an asyncio background thread.

Colour palette:
  BG_BASE   #0d0f12   Panel backgrounds
  BG_PANEL  #14171c   Card backgrounds
  BG_BORDER #252930   Dividers
  TXT_DIM   #6c7280   Labels, secondary text
  TXT_MAIN  #c8cdd5   Primary text
  AMBER     #e8a030   Warnings, active indicators
  CYAN      #30c8c0   Connected / live
  RED       #e83030   ESTOP / danger
  GREEN     #30c860   OK / safe
  PURPLE    #9060e8   Plugin accent
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import dearpygui.dearpygui as dpg

from ui.ui_bridge import UIBridge, UIState, get_bridge

# ── Colour constants (RGBA tuples) ───────────────────────────────────────────

BG_BASE   = (13, 15, 18, 255)
BG_PANEL  = (20, 23, 28, 255)
BG_BORDER = (37, 41, 48, 255)
TXT_DIM   = (108, 114, 128, 255)
TXT_MAIN  = (200, 205, 213, 255)
AMBER     = (232, 160, 48, 255)
CYAN      = (48, 200, 192, 255)
RED       = (232, 48, 48, 255)
GREEN     = (48, 200, 96, 255)
PURPLE    = (144, 96, 232, 255)
WHITE     = (255, 255, 255, 255)

FONT_MONO = None   # set in _load_fonts if a mono font is bundled

WIN_W, WIN_H = 1100, 760

# DPG tag constants
TAG_ESTOP_BTN      = "btn_estop"
TAG_CLEAR_BTN      = "btn_clear_estop"
TAG_PLAY_BTN       = "btn_play"
TAG_PAUSE_BTN      = "btn_pause"
TAG_STOP_BTN       = "btn_stop"
TAG_FS_PATH        = "txt_fs_path"
TAG_FS_PROGRESS    = "pb_fs_progress"
TAG_FS_POSITION    = "txt_fs_position"
TAG_STATUS_ROBOT   = "txt_status_robot"
TAG_STATUS_WEARABLE = "txt_status_wearable"
TAG_STATUS_INTIFACE = "txt_status_intiface"
TAG_STATUS_HISMITH  = "txt_status_hismith"
TAG_HR_VALUE       = "txt_hr_value"
TAG_BATTERY        = "pb_battery"
TAG_BATTERY_LABEL  = "txt_battery"
TAG_IMU_ROLL       = "txt_imu_roll"
TAG_IMU_PITCH      = "txt_imu_pitch"
TAG_IMU_YAW        = "txt_imu_yaw"
TAG_VEL_VX         = "txt_vel_vx"
TAG_VEL_VY         = "txt_vel_vy"
TAG_VEL_VYAW       = "txt_vel_vyaw"
TAG_TICK_COUNT     = "txt_ticks"
TAG_OVERRUNS       = "txt_overruns"
TAG_QUEUE_DEPTH    = "txt_queue"
TAG_PLUGIN_TABLE   = "tbl_plugins"
TAG_ESTOP_BANNER   = "grp_estop_banner"
TAG_MAIN_WINDOW    = "wnd_main"
TAG_VIEWPORT       = "vp_main"


class CERBERUSApp:
    """
    Main application class.  Call run() from the main thread.
    """

    def __init__(self, bridge: UIBridge | None = None) -> None:
        self._bridge   = bridge or get_bridge()
        self._running  = False
        self._last_state: UIState | None = None
        self._fs_file_dialog_open = False

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self) -> None:
        dpg.create_context()
        self._apply_theme()
        self._build_ui()

        dpg.create_viewport(
            title  = "CERBERUS — Operator Interface",
            width  = WIN_W,
            height = WIN_H,
            min_width  = 900,
            min_height = 600,
        )
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window(TAG_MAIN_WINDOW, True)

        self._running = True
        while dpg.is_dearpygui_running():
            self._update()
            dpg.render_dearpygui_frame()

        dpg.destroy_context()

    # ── Theme ─────────────────────────────────────────────────────────────────

    def _apply_theme(self) -> None:
        with dpg.theme() as global_theme:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg,      BG_BASE,   category=dpg.mvThemeCat_Core)
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg,       BG_PANEL,  category=dpg.mvThemeCat_Core)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg,       BG_PANEL,  category=dpg.mvThemeCat_Core)
                dpg.add_theme_color(dpg.mvThemeCol_PopupBg,       BG_PANEL,  category=dpg.mvThemeCat_Core)
                dpg.add_theme_color(dpg.mvThemeCol_Border,        BG_BORDER, category=dpg.mvThemeCat_Core)
                dpg.add_theme_color(dpg.mvThemeCol_Text,          TXT_MAIN,  category=dpg.mvThemeCat_Core)
                dpg.add_theme_color(dpg.mvThemeCol_Button,        (40,45,55,255), category=dpg.mvThemeCat_Core)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (55,62,75,255), category=dpg.mvThemeCat_Core)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,  (70,80,95,255), category=dpg.mvThemeCat_Core)
                dpg.add_theme_color(dpg.mvThemeCol_Header,        (35,40,50,255), category=dpg.mvThemeCat_Core)
                dpg.add_theme_color(dpg.mvThemeCol_TableHeaderBg, (20,23,28,255), category=dpg.mvThemeCat_Core)
                dpg.add_theme_style(dpg.mvStyleVar_WindowRounding,   0, category=dpg.mvThemeCat_Core)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding,    3, category=dpg.mvThemeCat_Core)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing,      8, 6, category=dpg.mvThemeCat_Core)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding,    10, 10, category=dpg.mvThemeCat_Core)
        dpg.bind_theme(global_theme)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        with dpg.window(tag=TAG_MAIN_WINDOW, no_title_bar=True, no_move=True,
                        no_resize=True, no_scrollbar=True):
            self._build_header()
            self._build_estop_banner()
            with dpg.group(horizontal=True):
                self._build_left_column()
                dpg.add_spacer(width=8)
                self._build_right_column()
        self._build_file_dialog()

    def _build_header(self) -> None:
        with dpg.group(horizontal=True):
            dpg.add_text("CERBERUS", color=CYAN)
            dpg.add_text("  Operator Interface  v2.0", color=TXT_DIM)
            dpg.add_spacer(width=-1)
            dpg.add_text("", tag=TAG_TICK_COUNT, color=TXT_DIM)
        dpg.add_separator()
        dpg.add_spacer(height=4)

    def _build_estop_banner(self) -> None:
        with dpg.group(tag=TAG_ESTOP_BANNER, show=False):
            dpg.add_text("■  E-STOP ACTIVE  ■", color=RED)
            dpg.add_spacer(height=2)

    def _build_left_column(self) -> None:
        with dpg.child_window(width=340, height=-1, border=True):
            # ── Connection status ─────────────────────────────────────────
            self._section("CONNECTION")
            with dpg.table(header_row=False, borders_innerH=False,
                           borders_outerH=False, pad_outerX=True):
                dpg.add_table_column(width_fixed=True, init_width_or_weight=120)
                dpg.add_table_column()
                for label, tag in [
                    ("Go2 Robot",  TAG_STATUS_ROBOT),
                    ("Wearable",   TAG_STATUS_WEARABLE),
                    ("Intiface",   TAG_STATUS_INTIFACE),
                    ("Hismith",    TAG_STATUS_HISMITH),
                ]:
                    with dpg.table_row():
                        dpg.add_text(label, color=TXT_DIM)
                        dpg.add_text("—", tag=tag)

            dpg.add_spacer(height=8)

            # ── Robot telemetry ───────────────────────────────────────────
            self._section("TELEMETRY")
            dpg.add_text("Battery", color=TXT_DIM)
            dpg.add_progress_bar(tag=TAG_BATTERY, default_value=0.0,
                                 width=-1, height=14)
            dpg.add_text("", tag=TAG_BATTERY_LABEL, color=TXT_DIM)

            dpg.add_spacer(height=6)
            with dpg.table(header_row=False, borders_innerH=False,
                           borders_outerH=False, pad_outerX=True):
                dpg.add_table_column(width_fixed=True, init_width_or_weight=80)
                dpg.add_table_column()
                for label, tag in [
                    ("Roll",  TAG_IMU_ROLL),
                    ("Pitch", TAG_IMU_PITCH),
                    ("Yaw",   TAG_IMU_YAW),
                    ("Vx",    TAG_VEL_VX),
                    ("Vy",    TAG_VEL_VY),
                    ("Vyaw",  TAG_VEL_VYAW),
                ]:
                    with dpg.table_row():
                        dpg.add_text(label, color=TXT_DIM)
                        dpg.add_text("—", tag=tag, color=TXT_MAIN)

            dpg.add_spacer(height=8)

            # ── Bio ───────────────────────────────────────────────────────
            self._section("BIO")
            with dpg.group(horizontal=True):
                dpg.add_text("Heart Rate", color=TXT_DIM)
                dpg.add_text("—", tag=TAG_HR_VALUE, color=TXT_MAIN)
                dpg.add_text("bpm", color=TXT_DIM)

            dpg.add_spacer(height=8)

            # ── Plugins ───────────────────────────────────────────────────
            self._section("PLUGINS")
            with dpg.table(tag=TAG_PLUGIN_TABLE, header_row=True,
                           borders_innerH=True, borders_outerH=True):
                dpg.add_table_column(label="Plugin")
                dpg.add_table_column(label="State")

    def _build_right_column(self) -> None:
        with dpg.child_window(width=-1, height=-1, border=True):
            # ── FunScript player ──────────────────────────────────────────
            self._section("FUNSCRIPT TIMELINE")

            with dpg.group(horizontal=True):
                dpg.add_button(label="Open File…", callback=self._cb_open_file)
                dpg.add_text("", tag=TAG_FS_PATH, color=TXT_DIM)

            dpg.add_spacer(height=6)
            with dpg.group(horizontal=True):
                dpg.add_button(label=" ▶  PLAY  ",  tag=TAG_PLAY_BTN,  callback=self._cb_play,
                               width=90)
                dpg.add_button(label=" ⏸  PAUSE ", tag=TAG_PAUSE_BTN, callback=self._cb_pause,
                               width=90)
                dpg.add_button(label=" ⏹  STOP  ", tag=TAG_STOP_BTN,  callback=self._cb_stop,
                               width=90)

            dpg.add_spacer(height=8)
            dpg.add_progress_bar(tag=TAG_FS_PROGRESS, default_value=0.0,
                                 width=-1, height=20)
            with dpg.group(horizontal=True):
                dpg.add_text("", tag=TAG_FS_POSITION, color=TXT_DIM)

            dpg.add_spacer(height=16)
            dpg.add_separator()
            dpg.add_spacer(height=8)

            # ── E-STOP ─────────────────────────────────────────────────────
            self._section("SAFETY")
            with dpg.group(horizontal=True):
                with dpg.theme() as estop_theme, dpg.theme_component(dpg.mvButton):
                    dpg.add_theme_color(dpg.mvThemeCol_Button,        (200, 30, 30, 255),
                                        category=dpg.mvThemeCat_Core)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,  (230, 50, 50, 255),
                                        category=dpg.mvThemeCat_Core)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,   (255, 80, 80, 255),
                                        category=dpg.mvThemeCat_Core)
                    dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 4,
                                        category=dpg.mvThemeCat_Core)

                estop_btn = dpg.add_button(
                    label="  ■  EMERGENCY STOP  ",
                    tag=TAG_ESTOP_BTN,
                    callback=self._cb_estop,
                    width=220, height=44,
                )
                dpg.bind_item_theme(estop_btn, estop_theme)

                dpg.add_spacer(width=12)

                with dpg.theme() as clear_theme, dpg.theme_component(dpg.mvButton):
                    dpg.add_theme_color(dpg.mvThemeCol_Button,        (30, 120, 60, 255),
                                        category=dpg.mvThemeCat_Core)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,  (40, 150, 75, 255),
                                        category=dpg.mvThemeCat_Core)

                clear_btn = dpg.add_button(
                    label=" ✓ Clear E-Stop ",
                    tag=TAG_CLEAR_BTN,
                    callback=self._cb_clear_estop,
                    width=140, height=44,
                    enabled=False,
                )
                dpg.bind_item_theme(clear_btn, clear_theme)

            dpg.add_spacer(height=16)
            dpg.add_separator()
            dpg.add_spacer(height=8)

            # ── Runtime stats ──────────────────────────────────────────────
            self._section("RUNTIME")
            with dpg.table(header_row=False, borders_innerH=False,
                           borders_outerH=False):
                dpg.add_table_column(width_fixed=True, init_width_or_weight=120)
                dpg.add_table_column()
                for label, tag in [
                    ("Ticks",      TAG_TICK_COUNT),
                    ("Overruns",   TAG_OVERRUNS),
                    ("Bus depth",  TAG_QUEUE_DEPTH),
                ]:
                    with dpg.table_row():
                        dpg.add_text(label, color=TXT_DIM)
                        dpg.add_text("—", tag=tag, color=TXT_MAIN)

    def _build_file_dialog(self) -> None:
        with dpg.file_dialog(
            tag="fd_funscript",
            label="Load FunScript",
            width=600, height=400,
            show=False,
            callback=self._cb_file_selected,
            cancel_callback=lambda: None,
        ):
            dpg.add_file_extension(".funscript", color=CYAN)
            dpg.add_file_extension(".json",      color=TXT_DIM)

    # ── Per-frame update ───────────────────────────────────────────────────────

    def _update(self) -> None:
        state = self._bridge.get_state()
        if state is self._last_state:
            return
        self._last_state = state
        self._apply_state(state)

    def _apply_state(self, s: UIState) -> None:
        # E-stop banner
        dpg.configure_item(TAG_ESTOP_BANNER, show=s.estop)
        dpg.configure_item(TAG_ESTOP_BTN,   enabled=not s.estop)
        dpg.configure_item(TAG_CLEAR_BTN,   enabled=s.estop)

        # Connection indicators
        self._set_status(TAG_STATUS_ROBOT,    s.robot_connected)
        self._set_status(TAG_STATUS_WEARABLE, s.wearable_connected)
        self._set_status(TAG_STATUS_INTIFACE, s.intiface_connected)
        self._set_status(TAG_STATUS_HISMITH,  s.hismith_connected)

        # Battery
        pct = s.battery_pct / 100.0
        dpg.set_value(TAG_BATTERY, pct)
        color = GREEN if pct > 0.3 else (AMBER if pct > 0.15 else RED)
        dpg.configure_item(TAG_BATTERY, overlay=f"{s.battery_pct}%")
        dpg.set_value(TAG_BATTERY_LABEL, f"{s.battery_voltage:.1f}V")

        # IMU
        dpg.set_value(TAG_IMU_ROLL,  f"{s.imu_roll:+6.2f}°")
        dpg.set_value(TAG_IMU_PITCH, f"{s.imu_pitch:+6.2f}°")
        dpg.set_value(TAG_IMU_YAW,   f"{s.imu_yaw:+7.2f}°")
        dpg.set_value(TAG_VEL_VX,    f"{s.vx:+5.2f} m/s")
        dpg.set_value(TAG_VEL_VY,    f"{s.vy:+5.2f} m/s")
        dpg.set_value(TAG_VEL_VYAW,  f"{s.vyaw:+5.2f} r/s")

        # HR
        hr_color = RED if s.hr_alarm else (AMBER if s.heart_rate_bpm > 150 else TXT_MAIN)
        dpg.set_value(TAG_HR_VALUE, str(s.heart_rate_bpm) if s.heart_rate_bpm else "—")
        dpg.configure_item(TAG_HR_VALUE, color=hr_color)

        # FunScript
        if s.fs_loaded:
            name = Path(s.fs_path).name if s.fs_path else "—"
            dpg.set_value(TAG_FS_PATH, name)
        else:
            dpg.set_value(TAG_FS_PATH, "No file loaded")

        if s.fs_duration_ms > 0:
            prog = s.fs_position_ms / s.fs_duration_ms
            dpg.set_value(TAG_FS_PROGRESS, prog)
            cur_s  = s.fs_position_ms  / 1000
            tot_s  = s.fs_duration_ms  / 1000
            dpg.set_value(TAG_FS_POSITION,
                          f"{cur_s:.1f}s / {tot_s:.1f}s  ({s.fs_position_norm*100:.0f}%)")
        else:
            dpg.set_value(TAG_FS_PROGRESS, 0.0)
            dpg.set_value(TAG_FS_POSITION,  "—")

        dpg.configure_item(TAG_PLAY_BTN,  enabled=s.fs_loaded and not s.fs_playing and not s.estop)
        dpg.configure_item(TAG_PAUSE_BTN, enabled=s.fs_playing)
        dpg.configure_item(TAG_STOP_BTN,  enabled=s.fs_loaded)

        # Plugins table — rebuild if changed
        if s.plugin_states != getattr(self, "_last_plugins", {}):
            self._last_plugins = dict(s.plugin_states)
            dpg.delete_item(TAG_PLUGIN_TABLE, children_only=True, slot=1)
            for name, state_name in s.plugin_states.items():
                color = CYAN if state_name == "ACTIVE" else (
                    RED  if state_name == "ERROR"  else TXT_DIM
                )
                with dpg.table_row(parent=TAG_PLUGIN_TABLE):
                    dpg.add_text(name)
                    dpg.add_text(state_name, color=color)

        # Runtime stats (right column shares TAG_TICK_COUNT — pick one)
        dpg.set_value(TAG_TICK_COUNT,  str(s.tick_count))
        dpg.set_value(TAG_OVERRUNS,    str(s.tick_overruns))
        dpg.set_value(TAG_QUEUE_DEPTH, str(s.bus_queue_depth))

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def _cb_estop(self) -> None:
        self._bridge.send_command("estop")

    def _cb_clear_estop(self) -> None:
        self._bridge.send_command("clear_estop")

    def _cb_play(self) -> None:
        self._bridge.send_command("play")

    def _cb_pause(self) -> None:
        self._bridge.send_command("pause")

    def _cb_stop(self) -> None:
        self._bridge.send_command("stop")

    def _cb_open_file(self) -> None:
        dpg.show_item("fd_funscript")

    def _cb_file_selected(self, sender: Any, app_data: Any) -> None:
        path = app_data.get("file_path_name", "")
        if path:
            self._bridge.send_command("load_funscript", path=path)
            self._bridge.update_fs(loaded=True, path=path)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _section(title: str) -> None:
        dpg.add_text(title, color=TXT_DIM)
        dpg.add_separator()
        dpg.add_spacer(height=4)

    @staticmethod
    def _set_status(tag: str, connected: bool) -> None:
        if connected:
            dpg.set_value(tag, "● ONLINE")
            dpg.configure_item(tag, color=CYAN)
        else:
            dpg.set_value(tag, "○ offline")
            dpg.configure_item(tag, color=TXT_DIM)


# ── Standalone launcher ────────────────────────────────────────────────────────

def launch_ui(bridge: UIBridge | None = None) -> None:
    """Call from the main thread after the asyncio runtime is started."""
    app = CERBERUSApp(bridge)
    app.run()


def launch_ui_thread(bridge: UIBridge | None = None) -> threading.Thread:
    """Launch UI in a dedicated thread (when asyncio owns the main thread)."""
    t = threading.Thread(target=launch_ui, args=(bridge,), daemon=True, name="cerberus.ui")
    t.start()
    return t


if __name__ == "__main__":
    launch_ui()
