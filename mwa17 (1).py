import sys
import time
import math
import queue
import argparse
import csv
import os
import uuid
from pathlib import Path
import socket
import json
import subprocess
from datetime import datetime

import serial

from PySide6.QtCore import (
    Qt, QThread, Signal, QTimer, QSize, QSettings, QRectF, QObject,
    QVariantAnimation, QEasingCurve, QEvent
)
from PySide6.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont, QFontDatabase
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame,
    QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QGridLayout, QScrollArea, QStackedWidget,
    QCheckBox, QMessageBox, QSizePolicy, QSlider, QComboBox, QButtonGroup,
    QGraphicsDropShadowEffect
)

# >>> WATCHDOG integration (mwa_watchdog.py must sit next to this file)
from mwa_watchdog import WatchdogPage, create_shared_watchdog
WATCHDOG_BLOCKS_RF_ON = True      # refuse PA ON while a HIGH watchdog alarm cause is present
WATCHDOG_BUTTON_QSS = "padding: 4px 8px; font-size: 12px;"   # compact: header is full at 800 px
# <<< WATCHDOG

SERIAL_PORT = "/dev/serial0"
BAUD_RATE = 115200

TELEMETRY_TIMEOUT = 3.0
RF_GENERATOR_IP = "192.168.10.10"
RF_GENERATOR_PORT = 5025
GENERATOR_UI_MIN = -20
GENERATOR_UI_MAX = 12
GENERATOR_RESET_DBM = -5

# ── Coolant pump ────────────────────────────────────────────────────────────
# The pump is no longer driven by the STM32. With PUMP_SIMULATED = True the pump
# works like the applicator: the operator manually confirms that the coolant pump
# is running (SIMULATED — nothing switches or measures it). No PUMP_ON/PUMP_OFF
# commands are sent and the STM32 'pump_on' field is ignored. Every confirmation
# is logged. Set to False to go back to STM32 pump control.
PUMP_SIMULATED = True

# ── Automatic output power control (APC) ────────────────────────────────────
# The operator sets a target in watts; the software adjusts the generator level
# (dBm) until the MEASURED amplifier output equals the target, then holds it.
# !!! Review every value below against YOUR amplifier, applicator and power
# !!! sensor before connecting real hardware.
APC_ENABLED_DEFAULT = True
APC_REGULATE = "forward"          # "forward" = forward power (what the screen shows)
                                  # "net"     = forward minus reflected power
APC_TARGET_MIN_W = 1.0
APC_TARGET_MAX_W = 180.0          # measured PA: 183 W at +12 dBm input (readings.pdf).
                                  # LOWER THIS to your applicator's rated power.
APC_TARGET_DEFAULT_W = 5.0
APC_TOL_REL = 0.01                # hold band: ±1 % of target ...
APC_TOL_ABS_W = 0.03              # ... but never tighter than ±0.03 W
APC_KP = 0.8                      # fraction of the dB error corrected per step
APC_MAX_STEP_UP_DB = 2.0          # largest output (and input) increase per step, dB
APC_MAX_STEP_DOWN_DB = 6.0        # largest decrease per step (fast down), dB
APC_SETTLE_S = 0.3                # wait after generator readback before measuring
APC_READBACK_TIMEOUT_S = 3.0      # generator must confirm each level within this
APC_START_DBM = -15.0             # first RF level when no gain is learned yet;
                                  # must give an output the power sensor can read
APC_START_MARGIN_DB = 3.0         # without a calibration table, start this far BELOW
# ── Watts -> input dBm conversion ───────────────────────────────────────────
# The input level for a requested output is calculated from the amplifier's
# transfer curve (input dBm -> output W), stored in pa_calibration.json next to
# this program. Points are added automatically whenever the output is held
# steady at a target, or by the engineer with "Save point", or by hand from
# power-meter measurements (most accurate).
PA_CALIBRATION_FILE = "pa_calibration.json"
PA_NOMINAL_GAIN_DB = None         # e.g. 40.0 from the PA datasheet; used only until
                                  # the calibration table has its first point
APC_FF_MARGIN_DB = 0.3            # with a calibration, start this far below the
                                  # calculated input, then trim up to the target
APC_FF_MAX_JUMP_UP_DB = 3.0       # largest calculated jump up when the target is raised
APC_AUTO_LEARN = True             # add calibration points while holding steady
APC_LEARN_AFTER_S = 2.0           # ... after this long within the hold band
APC_MAX_GEN_DBM = 12.0            # never command above this (PA input limit)
APC_OVERPOWER_REL = 1.25          # stop RF if output > 125 % of target ...
APC_OVERPOWER_MIN_MARGIN_W = 0.5  # ... or target + 0.5 W, whichever is larger
APC_OVERPOWER_ABS_W = APC_TARGET_MAX_W * 1.10   # absolute ceiling, any target
APC_STALL_DB = 6.0                # stop RF if drive changes this much ...
APC_STALL_MIN_RISE_DB = 1.0       # ... and the output moves less than this
APC_MISMATCH_DB = 3.0             # stop RF if the measured output differs from the
                                  # calibration's expected output by more than this
APC_VSWR_HOLD = 1.8               # do not increase drive above this VSWR
                                  # (the existing VSWR > 2 fault still applies)

# ─────────────────────────────────────────────────────────────────────────────
# Display modes
#
# Blossom    Light. Soft blush paper with a lavender accent.
# Lavender   Dark. Dusk plum with a blush accent, for dimmed rooms.
# Sage       Light. Misty sage green with a mauve accent.
# High contrast  Black on white with heavier borders, for sunlight or low vision.
#
# Colour meanings follow IEC 60601-1 (indicator lights) and IEC 60601-1-8
# (alarm systems). Red is never used for anything but warnings and STOP,
# yellow only for cautions, green only for "ON / ready". Accent colours are
# deliberately kept away from those three hues.
# Colour always means the same thing, in every mode:
#   accent = something you can adjust or select
#   ok     = ON / START          warn   = check needed
#   danger = STOP / fault        readings are plain ink, never coloured
# ─────────────────────────────────────────────────────────────────────────────
THEMES = {
    "Blossom": {
        "bg": "#F7EEF1", "surface": "#FFFFFF", "surface2": "#F2E4EA",
        "line": "#E8D3DC", "ink": "#3B2A35", "ink2": "#7A6270", "ink3": "#B39DA9",
        "accent": "#8D6CC9", "accent_ink": "#FFFFFF", "accent_soft": "#EEE6FA",
        "ok": "#4F9A72", "ok_ink": "#FFFFFF", "ok_soft": "#E2F1E8",
        "warn": "#E0A12E", "warn_ink": "#2B1D05", "warn_soft": "#FBEFD9",
        "danger": "#CF3F4F", "danger_ink": "#FFFFFF", "danger_soft": "#FBE3E6",
        "shadow": "141,90,120,40", "bw": "1px",
    },
    "Lavender": {
        "bg": "#1F1B2B", "surface": "#29243A", "surface2": "#332D47",
        "line": "#463E61", "ink": "#F3EEF8", "ink2": "#BBB0CF", "ink3": "#807596",
        "accent": "#A9B4F5", "accent_ink": "#161A33", "accent_soft": "#34365A",
        "ok": "#86D3A5", "ok_ink": "#10281A", "ok_soft": "#233A31",
        "warn": "#F3C46E", "warn_ink": "#2B1D05", "warn_soft": "#3D3426",
        "danger": "#EE6D79", "danger_ink": "#2A0D12", "danger_soft": "#43252F",
        "shadow": "8,5,15,130", "bw": "1px",
    },
    "Sage": {
        "bg": "#EBF0EA", "surface": "#FBFDFB", "surface2": "#E2EAE2",
        "line": "#CCD9CD", "ink": "#26332B", "ink2": "#5D6F63", "ink3": "#98A89C",
        "accent": "#A0668F", "accent_ink": "#FFFFFF", "accent_soft": "#F2E6EE",
        "ok": "#3E8758", "ok_ink": "#FFFFFF", "ok_soft": "#DCEDE1",
        "warn": "#D9A02F", "warn_ink": "#2B1D05", "warn_soft": "#F6EBD3",
        "danger": "#C0414B", "danger_ink": "#FFFFFF", "danger_soft": "#F6DEE0",
        "shadow": "60,90,70,36", "bw": "1px",
    },
    "High contrast": {
        "bg": "#FFFFFF", "surface": "#FFFFFF", "surface2": "#EDEDED",
        "line": "#000000", "ink": "#000000", "ink2": "#1E1E1E", "ink3": "#555555",
        "accent": "#0038A8", "accent_ink": "#FFFFFF", "accent_soft": "#E6ECFA",
        "ok": "#006B2D", "ok_ink": "#FFFFFF", "ok_soft": "#DDF0E3",
        "warn": "#FFC400", "warn_ink": "#000000", "warn_soft": "#FFEFC2",
        "danger": "#B00020", "danger_ink": "#FFFFFF", "danger_soft": "#F9DDE1",
        "shadow": "0,0,0,0", "bw": "2px",
    },
}
THEME_ORDER = ["Blossom", "Lavender", "Sage", "High contrast"]
THEME_SHORT = {"Blossom": "Blossom", "Lavender": "Lavender", "Sage": "Sage", "High contrast": "Contrast"}
DEFAULT_THEME = "Blossom"
UI_FONT = "DejaVu Sans"           # replaced by Nunito when fonts/ is present
T = dict(THEMES[DEFAULT_THEME])   # active palette, mutated in place by set_theme()
T["font"] = UI_FONT

def load_fonts():
    """Use the rounded Nunito typeface if its files sit in ./fonts next to this
    script; otherwise keep DejaVu Sans, which ships with Raspberry Pi OS."""
    global UI_FONT
    folder = Path(__file__).resolve().parent / "fonts"
    loaded = False
    for name in ("Nunito-Regular.ttf", "Nunito-Bold.ttf", "Nunito-ExtraBold.ttf"):
        path = folder / name
        if path.exists() and QFontDatabase.addApplicationFont(str(path)) != -1:
            loaded = True
    UI_FONT = "Nunito" if loaded else "DejaVu Sans"
    T["font"] = UI_FONT
    return UI_FONT

# ── Screen profiles ─────────────────────────────────────────────────────────
# The layout is designed on an 800 x 480 canvas (the 7-inch panel). On a larger
# monitor the whole interface is scaled up by Qt, so text, touch targets and
# drawings stay sharp and in proportion.
DESIGN_W, DESIGN_H = 800, 480
_SCREEN_PROBE = (
    "import sys\n"
    "from PySide6.QtGui import QGuiApplication\n"
    "a = QGuiApplication(sys.argv[:1])\n"
    "g = a.primaryScreen().geometry()\n"
    "print(g.width(), g.height())\n"
)

def detect_screen_size():
    """Ask Qt for the primary screen size in a short child process, so the
    real application can be started with the right scale factor."""
    env = dict(os.environ)
    env.pop("QT_SCALE_FACTOR", None)
    try:
        out = subprocess.run([sys.executable, "-c", _SCREEN_PROBE], capture_output=True,
                             text=True, timeout=10, env=env)
        w, h = (int(v) for v in out.stdout.split()[:2])
        if w > 0 and h > 0:
            return w, h
    except Exception:
        pass
    return None

def choose_scale(display, windowed, forced_scale=None):
    if forced_scale:
        return max(0.5, min(4.0, float(forced_scale)))
    if display == "7inch":
        return 1.0
    size = detect_screen_size()
    if size is None:
        size = (1920, 1080) if display == "monitor" else (DESIGN_W, DESIGN_H)
    scale = min(size[0] / DESIGN_W, size[1] / DESIGN_H)
    if windowed:
        scale *= 0.88          # leave room for the taskbar and window frame
    return max(1.0, min(3.0, scale))

def tok(qss):
    """Replace @token names in a stylesheet with the active palette values."""
    for key in sorted(T, key=len, reverse=True):
        qss = qss.replace("@" + key, T[key])
    return qss

# Kept for compatibility with any external code that imports these names.
BACKGROUND = THEMES["Blossom"]["bg"]
CARD_COLOR = THEMES["Blossom"]["surface"]
TEXT_COLOR = THEMES["Blossom"]["ink"]
MUTED_COLOR = THEMES["Blossom"]["ink2"]
BLUE = THEMES["Blossom"]["accent"]
GREEN = THEMES["Blossom"]["ok"]
ORANGE = THEMES["Blossom"]["warn"]
RED = THEMES["Blossom"]["danger"]

def strip_checksum(line):
    """Remove and verify an optional NMEA '*XX' checksum (STM32 firmware 1.1+).
    Returns the line without it, or None if the checksum is wrong."""
    if len(line) > 3 and line[-3] == "*":
        body, cs = line[:-3], line[-2:]
        calc = 0
        for ch in body:
            calc ^= ord(ch)
        try:
            if int(cs, 16) != calc:
                return None
        except ValueError:
            return None
        return body
    return line


def demo_pa_line(data):
    """Demo mode: the same telemetry as a full PA line, for the watchdog."""
    fields = dict(data)
    fields.setdefault("on_locked", 0)
    fields.setdefault("so24_calibrated", 1)
    fields["rf_state"] = "ENABLED" if data.get("enable") == 0 and data.get("alarm_in") == 0 else "OFF"
    return "PA," + ",".join(f"{k}={v}" for k, v in fields.items())


def parse_telemetry(line):

    if not line.startswith("PA,"):
        return None

    result = {}

    for element in line.split(",")[1:]:
        if "=" not in element:
            continue

        key, value = element.split("=", 1)

        try:
            result[key] = int(value.strip())
        except ValueError:
            continue

    required = {
        "ms",
        "conn",
        "valid",
        "adc_valid",
        "enable",
        "alarm_in"
    }

    if not required.issubset(result):
        return None

    return result

def forward_dbm_to_watts(dbm, rf_active):
    if rf_active is False:
        return 0.0
    if rf_active is not True or dbm is None:
        return None
    try:
        value = float(dbm)
        if not math.isfinite(value) or not (-120.0 <= value <= 60.0):
            return None
        return 10.0 ** ((value - 30.0) / 10.0)
    except (TypeError, ValueError, OverflowError):
        return None

def watts_to_dbm(watts):
    return 10.0 * math.log10(max(float(watts), 1e-9)) + 30.0


class PaCalibration:
    """Amplifier transfer curve: generator input (dBm) -> amplifier output (W).

    level_for(watts) returns the input needed for a requested output, by
    interpolating between measured points in dB. Outside the measured range it
    extrapolates with a slope of 1 dB in -> 1 dB out. A compressing amplifier
    gives less than that, so the estimate errs LOW (safe), and the control loop
    trims up the rest."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.points = []        # [(input_dbm, output_dbm)], sorted, output increasing
        self.load()

    def load(self):
        self.points = []
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for item in data.get("points", []):
                gin, w = float(item["input_dbm"]), float(item["output_w"])
                if math.isfinite(gin) and math.isfinite(w) and w > 0:
                    self._insert(gin, watts_to_dbm(w))
        except (OSError, ValueError, KeyError, TypeError):
            self.points = []

    def save(self):
        if not self.path:
            return True
        data = {
            "description": "Amplifier transfer curve: generator input (dBm) -> output (W)",
            "updated": datetime.now().isoformat(timespec="seconds"),
            "points": [{"input_dbm": round(g, 2), "output_w": round(10 ** ((o - 30) / 10), 4)}
                       for g, o in self.points],
        }
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            return True
        except OSError:
            return False

    def _insert(self, gin, out_dbm):
        # Replace nearby points, then drop any that contradict the new one
        # (output must rise with input), so newer measurements win.
        pts = [(g, o) for g, o in self.points if abs(g - gin) >= 0.25]
        pts = [(g, o) for g, o in pts if (g < gin and o < out_dbm) or (g > gin and o > out_dbm)]
        pts.append((gin, out_dbm))
        self.points = sorted(pts)

    def add_point(self, input_dbm, output_w):
        if output_w is None or not output_w > 0:
            return False
        self._insert(float(input_dbm), watts_to_dbm(output_w))
        self.save()
        return True

    def clear(self):
        self.points = []
        self.save()

    def valid(self):
        return len(self.points) >= 1

    def level_for(self, watts):
        return self.level_for_dbm(watts_to_dbm(watts))

    def level_for_dbm(self, t):
        if not self.points:
            return None
        pts = self.points
        if t <= pts[0][1] or len(pts) == 1:
            g, o = pts[0] if t <= pts[0][1] else pts[-1]
            return g + (t - o)
        if t >= pts[-1][1]:
            g, o = pts[-1]
            return g + (t - o)
        for (g1, o1), (g2, o2) in zip(pts, pts[1:]):
            if o1 <= t <= o2:
                return g1 + (g2 - g1) * (t - o1) / (o2 - o1)
        return None

    def output_dbm_for(self, g):
        """Expected output (dBm) at generator input g, from the measured curve."""
        pts = self.points
        if not pts:
            return None
        if g <= pts[0][0] or len(pts) == 1:
            gp, op = pts[0] if g <= pts[0][0] else pts[-1]
            return op + (g - gp)
        if g >= pts[-1][0]:
            return pts[-1][1] + (g - pts[-1][0])
        for (g1, o1), (g2, o2) in zip(pts, pts[1:]):
            if g1 <= g <= g2:
                return o1 + (o2 - o1) * (g - g1) / (g2 - g1)
        return None

    def summary(self):
        if not self.points:
            return "No calibration yet"
        lo, hi = self.points[0], self.points[-1]
        return (f"{len(self.points)} point{'s' if len(self.points) > 1 else ''}: "
                f"{lo[0]:+.1f}…{hi[0]:+.1f} dBm in → "
                f"{10 ** ((lo[1] - 30) / 10):.2g}…{10 ** ((hi[1] - 30) / 10):.3g} W out")


class PowerController:
    """Closed-loop output power control: keeps the MEASURED amplifier output
    at a target in watts by adjusting the generator level in dBm.

    Plain Python (no Qt), so it can be bench-tested on its own
    (see apc_bench_test.py). Built-in safety behaviour:
      * one change at a time: after each new level it waits for the generator
        readback plus a settling time and a fresh measurement;
      * increases slowly (<= APC_MAX_STEP_UP_DB per step), decreases quickly;
      * never commands above APC_MAX_GEN_DBM;
      * does not increase drive while VSWR is high;
      * returns a fault (the caller stops RF) on over-power, when the output
        does not follow the drive (sensor or RF-path failure), or when the
        generator does not confirm a level.
    Accuracy is limited by the power sensor: "exact" means within the hold
    band of the MEASURED value, so the sensor must be calibrated."""

    def __init__(self, calibration=None):
        self.target_w = APC_TARGET_DEFAULT_W
        self.learned_gain_db = None
        self.cal = calibration
        self.new_point = None        # (input_dbm, output_w) just learned, for logging
        self.reset()

    def reset(self):
        self.phase = "idle"
        self.status = "Idle"
        self.awaiting = False
        self.cmd_level = None
        self.cmd_time = 0.0
        self.settle_until = None
        self.trip_ref_w = self.target_w
        self.ref_gen = None
        self.ref_meas_db = None
        self.ref_dir = 0
        self.seen_response = False   # output has followed the drive at least once
        self.sat_level = None        # drive level above which the PA is saturated
        self.last_measured_w = None
        self.ff_pending = False      # target changed: jump to the calculated input
        self.hold_since = None
        self.learned_for = None

    def tolerance_w(self):
        return max(APC_TOL_ABS_W, APC_TOL_REL * self.target_w)

    def set_target(self, watts):
        watts = round(max(APC_TARGET_MIN_W, min(APC_TARGET_MAX_W, float(watts))), 1)
        # When the target is lowered during treatment, the over-power limit stays
        # at the old target until the output has actually come down.
        self.trip_ref_w = watts if self.phase == "idle" else max(self.trip_ref_w, watts)
        if watts < self.target_w:
            self.sat_level = None
        if watts != self.target_w and self.phase != "idle":
            self.ff_pending = True
        self.target_w = watts
        self.ref_gen = None
        self.hold_since = None
        return watts

    def input_for(self, watts=None):
        """Generator input (dBm) calculated for an output in watts, and its source."""
        watts = self.target_w if watts is None else watts
        if self.cal is not None and self.cal.valid():
            level, source = self.cal.level_for(watts), "calibration"
        elif self.learned_gain_db is not None:
            level, source = watts_to_dbm(watts) - self.learned_gain_db, "learned gain"
        elif PA_NOMINAL_GAIN_DB is not None:
            level, source = watts_to_dbm(watts) - PA_NOMINAL_GAIN_DB, "datasheet gain"
        else:
            return None, None
        if level is None:
            return None, None
        return round(max(GENERATOR_UI_MIN, min(APC_MAX_GEN_DBM, level)), 2), source

    def start_level(self):
        level, source = self.input_for()
        if level is None:
            level = APC_START_DBM
        elif source == "calibration":
            level -= APC_FF_MARGIN_DB
        else:
            level -= APC_START_MARGIN_DB
        return round(max(GENERATOR_UI_MIN, min(APC_MAX_GEN_DBM, level)), 2)

    def input_step_for(self, meas_db, out_step_db):
        """Input change (dB) expected to move the output by out_step_db."""
        if self.cal is not None and len(self.cal.points) >= 2:
            a = self.cal.level_for_dbm(meas_db)
            b = self.cal.level_for_dbm(meas_db + out_step_db)
            if a is not None and b is not None:
                return b - a
        return out_step_db

    def arm(self, level, now):
        """Called when treatment starts at `level`; the first step waits for its readback."""
        self.reset()
        self.phase = "starting"
        self.status = "Starting"
        self._command(level, now)

    def _command(self, level, now):
        self.awaiting = True
        self.cmd_level = level
        self.cmd_time = now
        self.settle_until = None

    def on_level_readback(self, level, now):
        if (self.awaiting and self.cmd_level is not None and self.settle_until is None
                and abs(float(level) - self.cmd_level) <= 0.06):
            self.settle_until = now + APC_SETTLE_S

    def step(self, now, measured_w, gen_level, vswr=None):
        """One control step per fresh measurement.
        Returns (new_generator_level_or_None, fault_or_None); fault = (key, message)."""
        if self.phase == "idle":
            self.phase = "adjusting"
        if self.awaiting:
            if self.settle_until is None:
                if now - self.cmd_time > APC_READBACK_TIMEOUT_S:
                    return None, ("APC_NO_READBACK",
                                  "Generator did not confirm the automatic power level")
                self.status = "Waiting for generator"
                return None, None
            if now < self.settle_until:
                return None, None
            self.awaiting = False
        if gen_level is None:
            self.status = "Waiting for generator level"
            return None, None
        if measured_w is None or not math.isfinite(measured_w) or measured_w < 0:
            self.status = "Waiting for a valid power reading"
            return None, None

        self.last_measured_w = measured_w
        target = self.target_w
        trip = max(self.trip_ref_w * APC_OVERPOWER_REL, self.trip_ref_w + APC_OVERPOWER_MIN_MARGIN_W)
        if measured_w > trip or measured_w > APC_OVERPOWER_ABS_W:
            return None, ("APC_OVERPOWER",
                          f"OVER-POWER: output {measured_w:.2f} W, target {target:.1f} W")

        meas_db = watts_to_dbm(measured_w)
        if self.cal is not None and len(self.cal.points) >= 2:
            expected = self.cal.output_dbm_for(gen_level)
            if expected is not None and abs(meas_db - expected) > APC_MISMATCH_DB:
                return None, ("APC_MISMATCH",
                              f"Output {measured_w:.2f} W does not match calibration "
                              f"({10 ** ((expected - 30) / 10):.2f} W expected at {gen_level:+.2f} dBm) "
                              "— check power sensor, cables and amplifier")
        if self.ff_pending:
            # Target changed: go straight to the calculated input level.
            self.ff_pending = False
            level, source = self.input_for()
            if level is not None and not (level > gen_level and vswr is not None and vswr > APC_VSWR_HOLD):
                jump = max(-APC_MAX_STEP_DOWN_DB * 3, min(APC_FF_MAX_JUMP_UP_DB, level - gen_level))
                new = round(max(GENERATOR_UI_MIN, min(APC_MAX_GEN_DBM, gen_level + jump)), 2)
                if abs(new - gen_level) >= 0.01:
                    self.phase = "adjusting"
                    self.status = f"Set {new:+.2f} dBm for {target:.1f} W ({source})"
                    self.ref_gen = None
                    self._command(new, now)
                    return new, None
        if abs(measured_w - target) <= self.tolerance_w():
            self.phase = "holding"
            self.status = f"Holding {target:.1f} W  (±{self.tolerance_w():.2f} W)"
            self.trip_ref_w = target
            self.learned_gain_db = meas_db - gen_level
            self.seen_response = True
            self.ref_gen = None
            if self.hold_since is None:
                self.hold_since = now
            elif (APC_AUTO_LEARN and self.cal is not None and self.learned_for != target
                  and now - self.hold_since >= APC_LEARN_AFTER_S):
                if self.cal.add_point(gen_level, measured_w):
                    self.new_point = (gen_level, measured_w)
                self.learned_for = target
            return None, None
        self.hold_since = None

        # Decide how far the OUTPUT should move this step, then convert that to an
        # input change using the amplifier's measured curve (this PA gives ~1.5 dB
        # out per 1 dB in over most of its range, so a 1:1 guess would overshoot).
        out_step = APC_KP * (watts_to_dbm(target) - meas_db)
        out_step = (min(out_step, APC_MAX_STEP_UP_DB) if out_step > 0
                    else max(out_step, -APC_MAX_STEP_DOWN_DB))
        step = self.input_step_for(meas_db, out_step)
        step = min(step, APC_MAX_STEP_UP_DB) if step > 0 else max(step, -APC_MAX_STEP_DOWN_DB)
        step = round(step, 2)
        if step == 0:
            self.phase = "holding"
            self.status = f"Holding {target:.1f} W (generator resolution)"
            return None, None
        if step > 0 and vswr is not None and vswr > APC_VSWR_HOLD:
            self.phase = "limited"
            self.status = f"Not increasing: reflected power high (VSWR {vswr:.2f})"
            return None, None
        ceiling = APC_MAX_GEN_DBM if self.sat_level is None else min(APC_MAX_GEN_DBM, self.sat_level)
        new = round(max(GENERATOR_UI_MIN, min(ceiling, gen_level + step)), 2)
        if abs(new - gen_level) < 0.005:
            self.phase = "limited"
            if step > 0 and self.sat_level is not None:
                self.status = f"Target not reachable: amplifier saturated at {measured_w:.1f} W"
            else:
                self.status = ("Target not reachable: generator at its limit" if step > 0
                               else "Output above target at minimum generator level")
            return None, None

        # The output must follow the drive. If the drive has moved APC_STALL_DB in
        # one direction without the output moving APC_STALL_MIN_RISE_DB, the sensor
        # or RF path is not behaving: stop instead of driving further.
        direction = 1 if new > gen_level else -1
        if self.ref_gen is None or self.ref_dir != direction:
            self.ref_gen, self.ref_meas_db, self.ref_dir = gen_level, meas_db, direction
        elif (meas_db - self.ref_meas_db) * direction >= APC_STALL_MIN_RISE_DB:
            self.ref_gen, self.ref_meas_db = gen_level, meas_db
            self.seen_response = True
        elif abs(gen_level - self.ref_gen) >= APC_STALL_DB:
            if direction > 0 and self.seen_response:
                # Output followed the drive earlier, so the sensor works: the amplifier
                # is saturated. Back off to the last level that still raised the output.
                self.sat_level = self.ref_gen
                self.phase = "limited"
                self.status = f"Target not reachable: amplifier saturated at {measured_w:.1f} W"
                self._command(self.ref_gen, now)
                return self.ref_gen, None
            return None, ("APC_NO_RESPONSE",
                          "Output power is not following the generator — check RF path and power sensor")

        self.phase = "adjusting"
        self.status = f"Adjusting  {measured_w:.2f} W → {target:.1f} W"
        self._command(new, now)
        return new, None


class CsvSessionLogger:
    CLINICAL_FIELDS = [
        "timestamp", "session_id", "role", "operator", "patient_id", "language",
        "event", "timer_setting_sec", "remaining_sec", "so24_temp_c", "so24_valid",
        "power_w", "power_dbm", "rf_measurement_valid", "pa_command_state",
        "rf_generator_reported_on", "pump_command_state", "comment"
    ]
    ENGINEER_FIELDS = CLINICAL_FIELDS + [
        "stm32_ms", "conn", "valid", "adc_valid", "rf_valid", "fwd_mdBm",
        "refl_mdBm", "vswr", "current_mA", "voltage_mV", "temp_mC", "alarm",
        "fwd_mV", "refl_mV", "isense_mV", "tsense_mV", "alarm_out",
        "enable", "alarm_in", "reset", "so24_temp_mC", "so24_raw", "pump_on",
        "so24_calibrated", "generator_setpoint_dbm", "pump_rpm"
    ]

    def __init__(self, base_dir, session_id):
        root = Path(base_dir).expanduser()
        (root / "clinical").mkdir(parents=True, exist_ok=True)
        (root / "engineering").mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = f"MWA_{stamp}_{session_id}"
        self.clinical_path = root / "clinical" / (stem + "_clinical.csv")
        self.engineering_path = root / "engineering" / (stem + "_engineering.csv")
        self.clinical_file = None
        self.engineering_file = None
        try:
            self.clinical_file = self.clinical_path.open("x", newline="", encoding="utf-8")
            self.engineering_file = self.engineering_path.open("x", newline="", encoding="utf-8")
            self.clinical_writer = csv.DictWriter(self.clinical_file, fieldnames=self.CLINICAL_FIELDS, extrasaction="ignore")
            self.engineering_writer = csv.DictWriter(self.engineering_file, fieldnames=self.ENGINEER_FIELDS, extrasaction="ignore")
            self.clinical_writer.writeheader()
            self.engineering_writer.writeheader()
            self.clinical_file.flush()
            self.engineering_file.flush()
        except Exception:
            self.close()
            raise

    def write(self, row):
        self.clinical_writer.writerow(row)
        self.engineering_writer.writerow(row)
        self.clinical_file.flush()
        self.engineering_file.flush()

    def close(self):
        for handle in (self.clinical_file, self.engineering_file):
            if handle and not handle.closed:
                handle.close()

def calculate_vswr(forward_dbm, reflected_dbm):
    try:
        forward = float(forward_dbm)
        reflected = float(reflected_dbm)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(forward) and math.isfinite(reflected)):
        return None
    if not (-120.0 <= forward <= 60.0 and -120.0 <= reflected <= 60.0):
        return None
    if reflected >= forward:
        return None
    gamma = 10.0 ** ((reflected - forward) / 20.0)
    if not (0.0 <= gamma < 1.0):
        return None
    result = (1.0 + gamma) / (1.0 - gamma)
    return result if math.isfinite(result) else None

class SerialWorker(QThread):

    telemetry_received = Signal(dict)
    connection_changed = Signal(bool)
    acknowledgement = Signal(str)
    error_message = Signal(str)
    raw_line = Signal(bytes)       # >>> WATCHDOG: every received line, unprocessed

    def __init__(self, port, baud, demo=False):
        super().__init__()

        self.port = port
        self.baud = baud
        self.demo = demo

        self.running = True
        self.tx_queue = queue.Queue()

    def send_command(self, command):

        if command in ("DISABLE", "REQUEST_ON", "HEARTBEAT", "PUMP_ON", "PUMP_OFF"):
            self.tx_queue.put(command + "\n")
            return True
        return False

    def stop(self):
        self.running = False

    def run(self):
        if self.demo:
            self.run_demo()
            return

        while self.running:
            try:
                with serial.Serial(
                    self.port,
                    self.baud,
                    timeout=0.2,
                    write_timeout=0.5
                ) as uart:

                    self.connection_changed.emit(True)
                    rx_buffer = b""

                    while self.running:

                        try:
                            while True:
                                command = self.tx_queue.get_nowait()
                                uart.write(command.encode("ascii"))
                        except queue.Empty:
                            pass

                        # Buffer bytes until a full line arrives. (readline() with a
                        # timeout could return half a line, which was then lost.)
                        chunk = uart.read(uart.in_waiting or 1)
                        if not chunk:
                            continue
                        rx_buffer += chunk

                        while b"\n" in rx_buffer:
                            raw, rx_buffer = rx_buffer.split(b"\n", 1)
                            self.raw_line.emit(raw)      # >>> WATCHDOG checks every line itself

                            line = strip_checksum(raw.decode(
                                "ascii",
                                errors="replace"
                            ).strip())
                            if line is None:
                                continue                 # corrupted line

                            if line.startswith(("ACK,", "EVENT,")):
                                self.acknowledgement.emit(line)
                                continue

                            packet = parse_telemetry(line)

                            if packet is not None:
                                self.telemetry_received.emit(packet)

                        if len(rx_buffer) > 2048:        # no line end: discard garbage
                            rx_buffer = b""

            except (serial.SerialException, OSError) as exc:

                retained = []
                while True:
                    try:
                        queued = self.tx_queue.get_nowait()
                    except queue.Empty:
                        break
                    if queued.strip() not in ("PUMP_ON", "PUMP_OFF"):
                        retained.append(queued)
                for queued in retained:
                    self.tx_queue.put(queued)
                self.connection_changed.emit(False)
                self.error_message.emit(str(exc))

                for _ in range(10):
                    if not self.running:
                        break
                    time.sleep(0.2)

        self.connection_changed.emit(False)

    def _demo_ack(self, text):
        self.raw_line.emit(text.encode("ascii"))   # >>> WATCHDOG sees demo replies too
        self.acknowledgement.emit(text)

    def run_demo(self):
        self.connection_changed.emit(True)
        start = time.monotonic()
        self.demo_pump_on = 0

        while self.running:
            t = time.monotonic() - start

            data = {
                "ms": int(t * 1000),
                "conn": 1,
                "valid": 1,
                "adc_valid": 1,
                "fwd_mdBm": 0,
                "refl_mdBm": 0,
                "rf_valid": 0,
                "temp_mC": int(
                    32000 + 1500 * math.sin(t / 5)
                ),
                "so24_temp_mC": int(29500 + 2700 * math.sin(t / 8)),
                "so24_valid": 1,
                "so24_raw": 1915,
                "pump_on": self.demo_pump_on,
                "current_mA": 120,
                "voltage_mV": 32000,
                "alarm": 0,
                "fwd_mV": 100,
                "refl_mV": 50,
                "isense_mV": 25,
                "tsense_mV": 2100,
                "alarm_out": 0,
                "enable": 1,
                "alarm_in": 1,
                "reset": 0
            }

            self.telemetry_received.emit(data)
            self.raw_line.emit(demo_pa_line(data).encode("ascii"))   # >>> WATCHDOG

            try:
                while True:
                    cmd = self.tx_queue.get_nowait()
                    if cmd.strip() == "DISABLE":
                        self._demo_ack("ACK,DISABLE,DEMO_ONLY")
                    elif cmd.strip() == "REQUEST_ON":
                        self._demo_ack(
                            "ACK,REQUEST_ON,DENIED,INTERLOCK_NOT_VERIFIED"
                        )
                    elif cmd.strip() == "PUMP_ON":
                        self.demo_pump_on = 1
                        self._demo_ack("ACK,PUMP_ON,ON,DEMO_ONLY")
                    elif cmd.strip() == "PUMP_OFF":
                        self.demo_pump_on = 0
                        self._demo_ack("ACK,PUMP_OFF,OFF,DEMO_ONLY")
            except queue.Empty:
                pass

            self.msleep(500)

        self.connection_changed.emit(False)

class GeneratorWorker(QThread):
    state_changed = Signal(bool, str)
    level_changed = Signal(float)
    output_changed = Signal(bool)
    error_message = Signal(str)

    def __init__(self, ip=RF_GENERATOR_IP, port=RF_GENERATOR_PORT, demo=False):
        super().__init__()
        self.ip, self.port, self.demo = ip, port, demo
        self.running = True
        self.commands = queue.Queue()
        self.demo_level = float(GENERATOR_UI_MIN)
        self._last_output = False

    def set_power(self, level):
        if GENERATOR_UI_MIN <= level <= GENERATOR_UI_MAX:
            self.commands.put(("level", float(level)))
            return True
        return False

    def set_output(self, enabled):

        self.commands.put(("output", bool(enabled)))
        return True

    def stop(self):
        self.running = False

    @staticmethod
    def query(sock, cmd):
        sock.sendall((cmd + "\n").encode("ascii"))
        chunks = bytearray()
        while not chunks.endswith(b"\n"):
            part = sock.recv(4096)
            if not part:
                raise ConnectionError("SCPI connection closed")
            chunks.extend(part)
            if len(chunks) > 8192:
                raise ValueError("SCPI response too long")
        return chunks.decode("ascii", errors="replace").strip()

    @staticmethod
    def write(sock, cmd):
        sock.sendall((cmd + "\n").encode("ascii"))

    def run(self):
        if self.demo:
            self.state_changed.emit(True, "DEMO — no RF hardware")
            self.level_changed.emit(self.demo_level)
            self.output_changed.emit(False)
            while self.running:
                try:
                    action, value = self.commands.get(timeout=0.25)
                except queue.Empty:
                    continue
                if action == "level":
                    self.demo_level = value
                    self.level_changed.emit(value)
                elif action == "output":
                    self._last_output = bool(value)
                    self.output_changed.emit(self._last_output)
            self.state_changed.emit(False, "Disconnected")
            return

        while self.running:
            try:
                with socket.create_connection((self.ip, self.port), timeout=3) as sock:
                    sock.settimeout(3)
                    idn = self.query(sock, "*IDN?")
                    if "SMCV100B" not in idn.upper():
                        raise RuntimeError("Unexpected instrument: " + idn[:65])

                    self.state_changed.emit(True, "SMCV100B connected")
                    self.output_changed.emit(self.query(sock, "OUTP:STAT?").strip() in ("1", "+1"))
                    self.level_changed.emit(float(self.query(sock, "SOUR:POW:LEV:IMM:AMPL?")))
                    last_poll = time.monotonic()
                    while self.running:
                        try:
                            action, value = self.commands.get(timeout=0.15)
                        except queue.Empty:
                            action = None
                        if action == "level":

                            deferred = []
                            while True:
                                try:
                                    a, v = self.commands.get_nowait()
                                except queue.Empty:
                                    break
                                if a == "level":
                                    value = v
                                else:
                                    deferred.append((a, v))
                            for item in deferred:
                                self.commands.put(item)
                            if GENERATOR_UI_MIN <= value <= GENERATOR_UI_MAX:
                                self.write(sock, f"SOUR:POW:LEV:IMM:AMPL {value:.2f}")
                                actual = float(self.query(sock, "SOUR:POW:LEV:IMM:AMPL?"))
                                self.level_changed.emit(actual)
                                if abs(actual - value) > 0.05:
                                    self.error_message.emit("Generator rejected requested power level")
                        elif action == "output":

                            self.write(sock, "OUTP:STAT ON" if value else "OUTP:STAT OFF")
                            actual = self.query(sock, "OUTP:STAT?").strip()
                            if actual not in ("0", "+0", "1", "+1"):
                                raise RuntimeError("Unrecognized RF output response: " + actual)
                            state = actual in ("1", "+1")
                            self.output_changed.emit(state)
                            if state != value:
                                self.error_message.emit("RF output command not confirmed by generator")
                        if time.monotonic() - last_poll >= 1.5:
                            self.output_changed.emit(self.query(sock, "OUTP:STAT?").strip() in ("1", "+1"))
                            self.level_changed.emit(float(self.query(sock, "SOUR:POW:LEV:IMM:AMPL?")))
                            last_poll = time.monotonic()
            except (OSError, RuntimeError, ValueError) as exc:
                self.state_changed.emit(False, "Generator disconnected")
                self.error_message.emit(str(exc))

                while True:
                    try:
                        self.commands.get_nowait()
                    except queue.Empty:
                        break
                for _ in range(10):
                    if not self.running:
                        break
                    self.msleep(200)
        self.state_changed.emit(False, "Disconnected")

class PowerSwitch(QWidget):

    off_requested = Signal()
    on_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setFixedSize(180, 48)
        self.pending_on = False

    def set_pending(self, pending):
        self.pending_on = bool(pending)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(T["warn_soft"] if self.pending_on else T["surface2"]))
        painter.setPen(QPen(QColor(T["warn"] if self.pending_on else T["line"]), 2 if T["bw"] == "2px" else 1))
        painter.drawRoundedRect(8, 6, 164, 36, 18, 18)
        painter.setPen(Qt.NoPen)

        knob_x = 136 if self.pending_on else 14
        painter.setBrush(QColor(T["warn"] if self.pending_on else T["ink3"]))
        painter.drawEllipse(knob_x, 10, 28, 28)
        painter.setPen(QColor(T["ink"]))
        font = painter.font()
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(48, 29, "OFF")
        painter.drawText(111, 29, "ON")
        painter.end()

    def mouseReleaseEvent(self, event):

        if event.button() != Qt.LeftButton:
            return
        if self.pending_on:
            self.set_pending(False)
            self.off_requested.emit()
        else:
            self.on_requested.emit()
        event.accept()

def _mix(c1, c2, t):
    a, b = QColor(c1), QColor(c2)
    return QColor(round(a.red() + (b.red() - a.red()) * t),
                  round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t)).name()


class AlarmFlasher(QObject):
    """Visual alarm indication for a banner label, after IEC 60601-1-8.

    priority   colour   behaviour (standard range)            used here
    high       red      flashing 1.4-2.8 Hz, 20-60 % duty      1.4 Hz, 50 %
    medium     yellow   flashing 0.4-0.8 Hz, 20-60 % duty      0.4 Hz, 50 %
    low        yellow   constant                                constant
    info       neutral  constant (not an alarm)                 constant

    The slowest rates the standard allows are used, and each flash fades in and
    out over about 10 % of the cycle, so the alert is calm rather than harsh
    while staying a recognisable flash. Text never relies on colour alone:
    callers prefix the message with !!!, !! or ! as the standard suggests."""

    PERIOD_MS = {"high": 714, "medium": 2500}

    def __init__(self, label, radius=14, font_px=13):
        super().__init__(label)
        self.label = label
        self.radius = radius
        self.font_px = font_px
        self.priority = None
        self._last = None
        self.anim = QVariantAnimation(self)
        self.anim.setStartValue(0.0)
        self.anim.setKeyValueAt(0.10, 1.0)
        self.anim.setKeyValueAt(0.45, 1.0)
        self.anim.setKeyValueAt(0.55, 0.0)
        self.anim.setEndValue(0.0)
        self.anim.setLoopCount(-1)
        self.anim.valueChanged.connect(self._paint)

    def set_priority(self, priority):
        if priority == self.priority:
            return
        self.priority = priority
        self.anim.stop()
        self._last = None
        if priority is None:
            self.label.setStyleSheet("")
        elif priority in self.PERIOD_MS:
            self.anim.setDuration(self.PERIOD_MS[priority])
            self.anim.start()
        else:
            self._paint(1.0)

    def refresh(self):
        """Repaint with the current display-mode colours."""
        self._last = None
        if self.priority is not None and self.anim.state() != QVariantAnimation.Running:
            self._paint(1.0)

    def _paint(self, value):
        p = self.priority
        if p is None:
            return
        level = round(float(value) * 10) / 10.0
        key = (p, level, T.get("_name"))
        if key == self._last:
            return
        self._last = key
        if p == "high":
            on_fill, on_text, off_fill, edge = T["danger"], T["danger_ink"], T["danger_soft"], T["danger"]
        elif p == "medium":
            on_fill, on_text, off_fill, edge = T["warn"], T["warn_ink"], T["warn_soft"], T["warn"]
        elif p == "low":
            on_fill, on_text, off_fill, edge = T["warn_soft"], T["ink"], T["warn_soft"], T["warn"]
        else:
            on_fill, on_text, off_fill, edge = T["surface"], T["ink"], T["surface"], T["line"]
        fill = _mix(off_fill, on_fill, level)
        text = on_text if level >= 0.5 else T["ink"]
        self.label.setStyleSheet(
            f"background:{fill};color:{text};border:2px solid {edge};"
            f"border-radius:{self.radius}px;padding:6px 14px;font-size:{self.font_px}px;font-weight:bold;")


class ClickGlow(QWidget):
    """Colour feedback for a touch: the button takes on its action colour while
    pressed, then the colour fades out slowly (about 0.8 s) after release, so
    the operator can see which control registered the touch."""

    def __init__(self, button, colour_key):
        super().__init__(button)
        self.button = button
        self.colour_key = colour_key
        self.alpha = 0
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.resize(button.size())
        self.fade = QVariantAnimation(self)
        self.fade.setDuration(800)
        self.fade.setEasingCurve(QEasingCurve.OutCubic)
        self.fade.valueChanged.connect(self._set_alpha)
        button.installEventFilter(self)
        button.pressed.connect(self._on_press)
        button.released.connect(self._on_release)

    def eventFilter(self, obj, event):
        if obj is self.button and event.type() == QEvent.Resize:
            self.resize(self.button.size())
        return False

    def _set_alpha(self, value):
        self.alpha = int(value)
        self.update()

    def _on_press(self):
        self.fade.stop()
        self.raise_()
        self._set_alpha(120)

    def _on_release(self):
        self.fade.stop()
        self.fade.setStartValue(self.alpha or 120)
        self.fade.setEndValue(0)
        self.fade.start()

    def paintEvent(self, event):
        if self.alpha <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        colour = QColor(T.get(self.colour_key, T["accent"]))
        colour.setAlpha(self.alpha)
        p.setPen(Qt.NoPen)
        p.setBrush(colour)
        radius = min(self.height() / 2.0, 16.0)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), radius, radius)
        p.end()


class TimerDial(QWidget):
    """Treatment-time dial. One tick per 10 s step (the size of the +/- buttons),
    a longer tick and numeral every minute, 6 minutes = one full turn.
    Display only: it never changes treatment state."""

    MAX_SEC = 360

    def __init__(self, size=176):
        super().__init__()
        self.setFixedSize(size, size)
        self._text = "00:00"
        self._frac = 0.0
        self._mode = "set"
        self._caption = "Max 6 min"

    def set_state(self, text, frac, mode, caption):
        frac = max(0.0, min(1.0, float(frac)))
        if (text, round(frac, 4), mode, caption) != (self._text, round(self._frac, 4), self._mode, self._caption):
            self._text, self._frac, self._mode, self._caption = text, frac, mode, caption
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        c = w / 2.0
        r_dot = c - 6
        p.setPen(Qt.NoPen)
        lit = int(round(self._frac * 36))
        for i in range(36):
            major = i % 6 == 0
            ang = math.radians(-90 + i * 10)
            size = 3.6 if major else 2.0
            colour = T["ink2"] if major else T["line"]
            p.setBrush(QColor(colour))
            p.drawEllipse(QRectF(c + r_dot * math.cos(ang) - size, c + r_dot * math.sin(ang) - size,
                                 size * 2, size * 2))
        ring = QRectF(20, 20, w - 40, w - 40)
        pen = QPen(QColor(T["surface2"]), 12)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawArc(ring, 0, 360 * 16)
        if self._frac > 0:
            colour = {"active": T["ok"], "starting": T["warn"], "done": T["ink3"]}.get(self._mode, T["accent"])
            pen = QPen(QColor(colour), 12)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawArc(ring, 90 * 16, -int(self._frac * 360 * 16))
        font = QFont(T.get("font", "DejaVu Sans"))
        font.setPixelSize(int(w * 0.22))
        font.setWeight(QFont.ExtraBold)
        p.setFont(font)
        p.setPen(QColor(T["ink"]))
        p.drawText(QRectF(0, c - w * 0.17, w, w * 0.26), Qt.AlignCenter, self._text)
        font.setPixelSize(12)
        font.setWeight(QFont.Normal)
        p.setFont(font)
        p.setPen(QColor(T["ink2"]))
        p.drawText(QRectF(0, c + w * 0.10, w, 18), Qt.AlignCenter, self._caption)
        p.end()

class MetricCard(QFrame):

    def __init__(self, name, unit, source):
        super().__init__()

        self.setObjectName("MetricCard")
        self.setMinimumHeight(68)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(2)

        self.name = QLabel(name)
        self.name.setObjectName("MetricTitle")

        value_row = QHBoxLayout()
        value_row.setSpacing(6)

        self.value = QLabel("—")
        self.value.setObjectName("MetricValue")

        self.unit = QLabel(unit)
        self.unit.setObjectName("MetricUnit")

        value_row.addWidget(self.value)
        value_row.addWidget(self.unit)
        value_row.addStretch()

        self.source = QLabel(source)
        self.source.setObjectName("MetricSource")

        layout.addWidget(self.name)
        layout.addLayout(value_row)
        layout.addWidget(self.source)

    def set_reading(self, value):
        self.value.setText(str(value))

    def clear(self):
        self.value.setText("—")

class DiagnosticRow(QFrame):

    def __init__(self, name, pin, unit=""):
        super().__init__()

        self.setObjectName("DiagnosticRow")
        self.setMinimumHeight(43)

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 5, 12, 5)

        label = QLabel(name)
        label.setMinimumWidth(145)
        label.setObjectName("DiagName")

        pin_label = QLabel(pin)
        pin_label.setObjectName("DiagPin")

        self.value = QLabel("—" + unit)
        self.value.setAlignment(
            Qt.AlignRight | Qt.AlignVCenter
        )
        self.value.setMinimumWidth(115)
        self.value.setObjectName("DiagValue")

        row.addWidget(label, 2)
        row.addWidget(pin_label, 2)
        row.addWidget(self.value, 1)

    def set_value(self, value):
        self.value.setText(str(value))

class PowerAmplifierGUI(QMainWindow):

    def __init__(self, port, demo=False):
        super().__init__()

        self.demo = demo
        self.port = port

        self.serial_connected = False
        self.last_packet_time = 0.0
        self.status_message_until = 0.0
        self.pending_on_deadline = 0.0
        self.commanded_on = False
        self.exit_pending = False
        self.role = None
        self.operator = ""
        self.patient_id = ""
        self.language = "English"
        self.session_id = None
        self.logger = None
        self.last_telemetry = {}
        self.last_good_sensor = False
        self.logging_failed = False
        self.session_ended = False
        self.cycle_outcome = None
        self.timer_setting_sec = 0
        # Use the calibration file wherever it exists: next to this program,
        # in the folder it was started from, or in the home folder.
        script_dir = Path(__file__).resolve().parent
        candidates = [script_dir / PA_CALIBRATION_FILE, Path.cwd() / PA_CALIBRATION_FILE,
                      Path.home() / PA_CALIBRATION_FILE]
        cal_path = next((c for c in candidates if c.exists()), None)
        if cal_path is None:
            cal_path = candidates[0] if os.access(script_dir, os.W_OK) else candidates[2]
        self.pa_cal = PaCalibration(cal_path)
        self.apc = PowerController(self.pa_cal)
        self.apc_enabled = APC_ENABLED_DEFAULT
        self._preset_done = False
        self.pending_treatment_start = False
        self.current_stage = "INITIALIZE / SYSTEM CHECK"
        self._active_popup = None
        self._popup_keys = set()
        self.timer_deadline = None
        self.timer_elapsed = False
        self.timer_last_shown = -1
        self.fault_latched = False
        self.fault_reason = ""
        self.warning_key = None
        self.last_warning_logged = None
        self.shutdown_confirmed = False
        self.pump_sim_ack = False      # manual pump confirmation (PUMP_SIMULATED)
        self.pump_reported_on = False if PUMP_SIMULATED else None
        self.pump_pending = None
        self.pump_pending_deadline = 0.0
        # Doctor-only control flow; simulated applicator is a MANUAL acknowledgement,
        # never a physical or independently verified safety interlock.
        self.doctor_applicator_ack = False
        self.doctor_auto_start = False
        self.doctor_auto_deadline = 0.0

        self.setWindowTitle(
            "Microwave Ablation"
        )

        self.resize(800, 480)
        self.setMinimumSize(700, 420)

        self.theme_buttons = []
        self.theme_segments = {}
        self._banner_critical = False
        saved_theme = DEFAULT_THEME
        try:
            saved_theme = str(QSettings("SAMEER", "MWA").value("display_mode_v2", DEFAULT_THEME))
        except Exception:
            pass
        self.build_ui()
        self.banner_flasher = AlarmFlasher(self.warning_banner, radius=14, font_px=12)
        self.notice_flasher = AlarmFlasher(self.doctor_notice, radius=16, font_px=13)
        self.set_theme(saved_theme, save=False)
        self.install_click_feedback()

        self.worker = SerialWorker(
            self.port,
            BAUD_RATE,
            demo=self.demo
        )

        self.worker.telemetry_received.connect(
            self.update_telemetry
        )
        self.worker.connection_changed.connect(
            self.update_connection
        )
        self.worker.acknowledgement.connect(
            self.show_acknowledgement
        )
        self.worker.error_message.connect(
            self.show_error
        )

        # >>> WATCHDOG: shares this SerialWorker (only one program can open the port).
        # Created before worker.start() so it sees the first connection_changed.
        self.watchdog, self._watchdog_signal = create_shared_watchdog(self)
        self.worker.raw_line.connect(self.watchdog.feed_line, Qt.DirectConnection)
        self.worker.connection_changed.connect(self.watchdog.set_port_state, Qt.DirectConnection)
        self._watchdog_signal.force_off.connect(self.watchdog_force_off)
        self.watchdog.start()
        self.watchdog_page = WatchdogPage(wd=self.watchdog, back=self.close_watchdog)
        self.pages.addWidget(self.watchdog_page)
        self.watchdog_page.status_changed.connect(self.update_watchdog_button)
        # <<< WATCHDOG

        self.worker.start()
        self.generator_connected = False
        self.generator_rf_on = None
        self.generator_worker = GeneratorWorker(demo=self.demo)
        self.generator_worker.state_changed.connect(self.update_generator_state)
        self.generator_worker.level_changed.connect(self.update_generator_level)
        self.generator_worker.output_changed.connect(self.update_generator_output)
        self.generator_worker.error_message.connect(self.generator_error)
        self.generator_worker.start()

        self.timeout_timer = QTimer(self)
        self.timeout_timer.timeout.connect(
            self.check_timeout
        )
        self.timeout_timer.start(250)
        self.heartbeat_timer = QTimer(self)
        self.heartbeat_timer.timeout.connect(self.send_heartbeat)
        self.heartbeat_timer.start(1000)
        self.treatment_timer = QTimer(self)
        self.treatment_timer.timeout.connect(self.update_treatment_timer)
        self.treatment_timer.start(100)

    def build_ui(self):
        self.pages = QStackedWidget()
        self.setCentralWidget(self.pages)
        self.login_page = QWidget()
        self.main_page = QWidget()
        self.monitor_page = QWidget()
        self.diagnostics_page = QWidget()
        self.doctor_page = QWidget()
        for page in (self.login_page, self.main_page, self.monitor_page, self.diagnostics_page, self.doctor_page):
            self.pages.addWidget(page)
        self.build_login_page()
        self.build_main_page()
        self.build_monitor_page()
        self.build_diagnostics_page()
        self.build_doctor_page()
        self.pages.setCurrentIndex(0)

    CLICK_COLOURS = {
        "DoctorStart": "ok", "StartAction": "ok",
        "DoctorStop": "danger", "StopAction": "danger", "OffButton": "danger",
        "ExitButton": "danger", "DoctorExit": "danger",
    }

    def install_click_feedback(self):
        """Give every on-screen button the slow-fading colour response."""
        self._click_glows = []
        for button in self.findChildren(QPushButton):
            if button.objectName() in ("NavButton", "ExitButton", "ThemeButton"):
                # usability: small header controls still >= ~7 mm tall on the 7-inch panel
                button.setMinimumHeight(max(button.minimumHeight(), 36))
            key = self.CLICK_COLOURS.get(button.objectName(), "accent")
            self._click_glows.append(ClickGlow(button, key))

    def theme_button(self):
        """Small header control that steps through the display modes."""
        button = QPushButton("◐  " + T.get("_name", DEFAULT_THEME))
        button.setObjectName("ThemeButton")
        button.setToolTip("Change display mode")
        button.clicked.connect(self.cycle_theme)
        self.theme_buttons.append(button)
        return button

    def build_login_page(self):
        outer = QVBoxLayout(self.login_page)
        outer.setContentsMargins(28, 16, 28, 10)
        outer.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(10)
        brand = QLabel("ϟ")
        brand.setObjectName("BrandMark")
        brand.setAlignment(Qt.AlignCenter)
        brand.setFixedSize(36, 36)
        top.addWidget(brand)
        names = QVBoxLayout()
        names.setSpacing(0)
        heading = QLabel("Microwave Ablation")
        heading.setObjectName("PageTitle")
        org = QLabel("SAMEER Medical Tech, MeitY, Government of India")
        org.setObjectName("SubTitle")
        names.addWidget(heading)
        names.addWidget(org)
        top.addLayout(names, 1)
        self.login_exit_button = QPushButton("Exit")
        self.login_exit_button.setObjectName("ExitButton")
        self.login_exit_button.clicked.connect(self.request_exit)
        top.addWidget(self.login_exit_button)
        outer.addLayout(top)

        outer.addStretch(1)
        hour = datetime.now().hour
        greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"
        title = QLabel(greeting + ", who is using the system today?")
        title.setObjectName("HeroTitle")
        outer.addWidget(title)
        subheading = QLabel("Choose a language, then your role. No password is needed on this prototype.")
        subheading.setObjectName("SubTitle")
        subheading.setWordWrap(True)
        outer.addWidget(subheading)
        outer.addSpacing(6)

        role_row = QHBoxLayout()
        role_row.setSpacing(12)
        self.doctor_role_button = QPushButton("Doctor\nRun a treatment")
        self.engineer_role_button = QPushButton("Engineer\nTreatment, telemetry and diagnostics")
        self.doctor_role_button.setObjectName("RoleLoginButton")
        self.engineer_role_button.setObjectName("RoleLoginButtonAlt")
        self.doctor_role_button.setMinimumHeight(104)
        self.engineer_role_button.setMinimumHeight(104)
        role_row.addWidget(self.doctor_role_button, 5)
        role_row.addWidget(self.engineer_role_button, 6)
        self.doctor_role_button.setToolTip("Open Doctor page without a password")
        self.engineer_role_button.setToolTip("Open Engineer pages without a password")
        self.doctor_role_button.clicked.connect(lambda: self.login_as("doctor"))
        self.engineer_role_button.clicked.connect(lambda: self.login_as("engineer"))
        outer.addLayout(role_row)

        settings = QHBoxLayout()
        settings.setSpacing(10)
        language_label = QLabel("Language / भाषा")
        language_label.setObjectName("FieldLabel")
        settings.addWidget(language_label)
        self.language_combo = QComboBox()
        self.language_combo.addItems(["English", "Hindi / हिन्दी"])
        self.language_combo.setMinimumHeight(44)
        self.language_combo.setMinimumWidth(150)
        settings.addWidget(self.language_combo)
        settings.addSpacing(10)
        mode_label = QLabel("Display")
        mode_label.setObjectName("FieldLabel")
        settings.addWidget(mode_label)
        segment_box = QHBoxLayout()
        segment_box.setSpacing(4)
        group = QButtonGroup(self.login_page)
        group.setExclusive(True)
        for name in THEME_ORDER:
            seg = QPushButton(THEME_SHORT.get(name, name))
            seg.setObjectName("Segment")
            seg.setCheckable(True)
            seg.setMinimumHeight(42)
            seg.clicked.connect(lambda _checked=False, n=name: self.set_theme(n))
            group.addButton(seg)
            segment_box.addWidget(seg)
            self.theme_segments[name] = seg
        self.theme_group = group
        settings.addLayout(segment_box)
        settings.addStretch(1)
        outer.addLayout(settings)

        self.login_message = QLabel(
            "Doctor: treatment page only  |  Engineer: treatment, monitoring and diagnostics\n"
            "Both clinical and engineering CSV logs are saved automatically."
        )
        self.login_message.setObjectName("FooterMessage")
        self.login_message.setWordWrap(True)
        outer.addWidget(self.login_message)
        outer.addStretch(1)
        legal = QLabel("IEC 60601-1 / IEC 60601-1-2 design references (not certified)  |  © SAMEER, MeitY, Government of India")
        legal.setObjectName("StandardsFooter")
        outer.addWidget(legal)

    def login_as(self, role):
        if self.role is not None or self.exit_pending:
            return
        if role not in ("doctor", "engineer"):
            return
        session_id = uuid.uuid4().hex[:12]
        try:
            logger = CsvSessionLogger(
                os.environ.get("MWA_LOG_DIR", str(Path.home() / "MWA_Logs")),
                session_id,
            )
        except OSError as exc:
            self.login_message.setText("Unable to create clinical and engineering CSV files: " + str(exc))
            return

        self.session_id = session_id
        self.logger = logger
        self.role = role
        self.operator = "Doctor (unidentified)" if role == "doctor" else "Engineer (unidentified)"
        self.patient_id = ""
        self.language = "Hindi" if self.language_combo.currentIndex() == 1 else "English"
        self._popup_keys.clear()
        self.cycle_outcome = None
        self.timer_elapsed = False
        self.timer_setting_sec = 0
        self.timer_value.setText("00:00")
        self.doctor_applicator_ack = False
        self.doctor_auto_start = False
        self.doctor_auto_deadline = 0.0
        self.engineer_nav.setVisible(role == "engineer")
        self.engineer_generator_card.setVisible(role == "engineer")
        self.watchdog_button.setVisible(role == "engineer")     # >>> WATCHDOG
        self.mode_label.setText("DOCTOR MODE" if role == "doctor" else "ENGINEER")
        # >>> WATCHDOG: make room for the Watchdog button in the 800 px engineer header
        self.page_title_label.setStyleSheet("font-size: 16px;" if role == "engineer" else "")
        self.doctor_power_controls.setVisible(role == "doctor")
        self.apply_role_layout()
        self.apply_language()
        self.pages.setCurrentIndex(4 if role == "doctor" else 1)
        self.update_flow_stage()
        self.refresh_doctor_display()
        self.log_event("LOGIN", comment="Prototype role-only login; identity not verified")

    def apply_role_layout(self):
        doctor = self.role == "doctor"
        self.treatment_page_layout.setContentsMargins(
            13 if doctor else 12, 9 if doctor else 7,
            13 if doctor else 12, 9 if doctor else 7,
        )
        self.treatment_page_layout.setSpacing(10 if doctor else 7)
        self.treatment_page_content.setStyleSheet(tok("""
            QLabel#PageTitle { font-size: 24px; font-weight: bold; }
            QLabel#FocusValue { font-size: 31px; font-weight: bold; color: @ink; }
            QLabel#DoctorHeading { font-size: 16px; font-weight: bold; }
            QLabel#FlowStep { font-size: 11px; padding: 6px; font-weight: bold; }
            QLabel#WarningBanner { font-size: 13px; font-weight: bold; }
            QLabel#FooterMessage { font-size: 11px; }
            QLabel#StandardsFooter { font-size: 9px; }
            QPushButton { font-size: 14px; padding: 9px 11px; }
            QPushButton#GeneratorRFSwitch, QPushButton#PumpSwitch {
                font-size: 15px; font-weight: bold;
            }
        """) if doctor else "")
        for button in (self.timer_minus, self.timer_plus,
                       self.start_button, self.stop_button,
                       self.pump_switch, self.doctor_rf_switch):
            button.setMinimumHeight(47 if doctor else 40)
        for button in self.doctor_power_controls.findChildren(QPushButton):
            button.setMinimumHeight(39 if doctor else 33)
        self.timer_value.setMinimumWidth(116 if doctor else 0)
        self.ntc_value.setMinimumHeight(45 if doctor else 0)
        self.doctor_power_value.setMinimumHeight(45 if doctor else 0)
        self.treatment_timer_card.setMinimumHeight(108 if doctor else 0)
        self.doctor_temperature_card.setMinimumHeight(112 if doctor else 0)
        self.doctor_power_card.setMinimumHeight(112 if doctor else 0)

    def apply_language(self):
        hindi = self.language == "Hindi"
        self.page_title_label.setText("माइक्रोवेव एब्लेशन" if hindi else "Microwave Ablation")
        self.timer_heading.setText("उपचार समय (0–6 मिनट)" if hindi else "Operation Timer (0–6 min)")
        self.so24_heading.setText("तापमान (SO24)" if hindi else "Temperature (SO24)")
        self.power_heading.setText("पावर" if hindi else "Power")
        self.pump_heading.setText(("पंप (सिम्युलेटेड)" if hindi else "Pump (SIMULATED)") if PUMP_SIMULATED
                                  else ("पंप ON / OFF" if hindi else "Pump ON / OFF"))
        self.pa_heading.setText("PA ON / OFF" if hindi else "PA ON / OFF")
        self.rf_heading.setText("RF आउटपुट ON / OFF" if hindi else "RF Output ON / OFF")
        self.start_button.setText("शुरू करें" if hindi else "START")
        self.stop_button.setText("बंद करें" if hindi else "STOP")
        self.update_flow_stage()

    def navigation_button(self, title, destination):
        button = QPushButton(title)
        button.setObjectName("NavButton")
        def navigate():
            if self.role != "engineer" and destination in (2, 3):
                return
            self.pages.setCurrentIndex(destination)
        button.clicked.connect(navigate)
        button.setMinimumHeight(32)
        return button

    def build_main_page(self):
        scroller = QScrollArea()
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        content.setObjectName("TreatmentPageContent")
        self.treatment_page_content = content
        scroller.setWidget(content)
        page_layout = QVBoxLayout(self.main_page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addWidget(scroller)
        outer = QVBoxLayout(content)
        self.treatment_page_layout = outer
        outer.setContentsMargins(12, 7, 12, 7)
        outer.setSpacing(7)
        header = QHBoxLayout()
        self.page_title_label = QLabel("Microwave Ablation")
        self.page_title_label.setObjectName("PageTitle")
        self.mode_label = QLabel("LOGIN REQUIRED")
        self.mode_label.setObjectName("RFBadge")
        self.rf_badge = QLabel("PA UNKNOWN")
        self.rf_badge.setObjectName("RFBadge")
        self.exit_button = QPushButton("Exit")
        self.exit_button.setObjectName("ExitButton")
        self.exit_button.clicked.connect(self.request_exit)
        header.addWidget(self.page_title_label)
        header.addStretch()
        header.addWidget(self.theme_button())
        header.addWidget(self.mode_label)
        # >>> WATCHDOG: engineer-only button, right after the mode badge
        self.watchdog_button = QPushButton("Watchdog")
        self.watchdog_button.setObjectName("NavButton")
        self.watchdog_button.setToolTip("Open the system watchdog")
        self.watchdog_button.clicked.connect(self.open_watchdog)
        self.watchdog_button.setStyleSheet(WATCHDOG_BUTTON_QSS)
        self.watchdog_button.setVisible(False)
        header.addWidget(self.watchdog_button)
        # <<< WATCHDOG
        header.addWidget(self.rf_badge)
        self.logout_button = QPushButton("LOGOUT")
        self.logout_button.setObjectName("NavButton")
        self.logout_button.clicked.connect(self.logout_to_login)
        header.addWidget(self.logout_button)
        header.addWidget(self.exit_button)
        outer.addLayout(header)

        flow = QHBoxLayout()
        flow.setSpacing(4)
        self.flow_labels = []
        for caption in ("1  System check", "2  Standby", "3  Configuration", "4  Treatment", "5  Complete"):
            step = QLabel(caption)
            step.setObjectName("FlowStep")
            step.setAlignment(Qt.AlignCenter)
            step.setMinimumHeight(29)
            self.flow_labels.append(step)
            flow.addWidget(step, 1)
        outer.addLayout(flow)
        notice_row = QHBoxLayout()
        self.warning_banner = QLabel("Monitoring standby · pump flow/speed not verified")
        self.warning_banner.setObjectName("WarningBanner")
        self.warning_banner.setWordWrap(True)
        notice_row.addWidget(self.warning_banner, 1)
        self.reset_fault_button = QPushButton("RESET WARNING")
        self.reset_fault_button.setEnabled(False)
        self.reset_fault_button.clicked.connect(self.reset_fault)
        notice_row.addWidget(self.reset_fault_button)
        self.reset_cycle_button = QPushButton("RESET TREATMENT")
        self.reset_cycle_button.setObjectName("NavButton")
        self.reset_cycle_button.clicked.connect(self.reset_treatment_cycle)
        notice_row.addWidget(self.reset_cycle_button)
        outer.addLayout(notice_row)

        timer_card = QFrame()
        self.treatment_timer_card = timer_card
        timer_card.setObjectName("ControlCard")
        timer_box = QVBoxLayout(timer_card)
        timer_box.setContentsMargins(12, 7, 12, 7)
        self.timer_heading = QLabel("Operation Timer (0–6 min)")
        self.timer_heading.setObjectName("DoctorHeading")
        timer_box.addWidget(self.timer_heading)
        timer_row = QHBoxLayout()
        self.timer_value = QLabel("00:00")
        self.timer_value.setObjectName("FocusValue")
        self.timer_value.setAlignment(Qt.AlignCenter)
        timer_row.addWidget(self.timer_value)
        self.timer_minus = QPushButton("−10 s")
        self.timer_minus.setMinimumHeight(40)
        self.timer_minus.clicked.connect(lambda: self.change_timer_setting(-10))
        self.timer_plus = QPushButton("+10 s")
        self.timer_plus.setMinimumHeight(40)
        self.timer_plus.clicked.connect(lambda: self.change_timer_setting(+10))
        timer_row.addWidget(self.timer_minus)
        timer_row.addWidget(self.timer_plus)
        self.start_button = QPushButton("START")
        self.start_button.setObjectName("StartAction")
        self.start_button.clicked.connect(self.start_treatment_timer)
        timer_row.addWidget(self.start_button)
        self.stop_button = QPushButton("STOP")
        self.stop_button.setObjectName("StopAction")
        self.stop_button.clicked.connect(self.stop_treatment_timer)
        timer_row.addWidget(self.stop_button)
        timer_box.addLayout(timer_row)
        self.timer_note = QLabel("Set time first → enable PA → press START to request RF. STOP requests both OFF.")
        self.timer_note.setObjectName("FooterMessage")
        timer_box.addWidget(self.timer_note)
        outer.addWidget(timer_card)

        readings = QHBoxLayout()
        temp_card = QFrame()
        temp_card.setObjectName("ControlCard")
        temp_layout = QVBoxLayout(temp_card)
        temp_layout.setContentsMargins(12, 7, 12, 7)
        self.so24_heading = QLabel("Temperature (SO24)")
        self.so24_heading.setObjectName("DoctorHeading")
        self.ntc_value = QLabel("— °C")
        self.ntc_value.setObjectName("FocusValue")
        self.ntc_status = QLabel("Waiting for sensor")
        self.ntc_status.setObjectName("FooterMessage")
        temp_layout.addWidget(self.so24_heading)
        temp_layout.addWidget(self.ntc_value)
        temp_layout.addWidget(self.ntc_status)
        power_card = QFrame()
        power_card.setObjectName("ControlCard")
        power_layout = QVBoxLayout(power_card)
        power_layout.setContentsMargins(12, 7, 12, 7)
        self.power_heading = QLabel("Power")
        self.power_heading.setObjectName("DoctorHeading")
        self.doctor_power_value = QLabel("— W")
        self.doctor_power_value.setObjectName("FocusValue")
        self.power_note = QLabel("Measured PA forward power; 0 W when RF is confirmed OFF")
        self.power_note.setObjectName("FooterMessage")
        power_layout.addWidget(self.power_heading)
        power_layout.addWidget(self.doctor_power_value)
        power_layout.addWidget(self.power_note)
        self.doctor_power_controls = QWidget()
        self.doctor_power_controls.setObjectName("Transparent")
        power_controls = QHBoxLayout(self.doctor_power_controls)
        power_controls.setContentsMargins(0, 2, 0, 0)
        self.doctor_power_setpoint = QLabel("Generator: — dBm")
        self.doctor_power_setpoint.setObjectName("FooterMessage")
        power_controls.addWidget(self.doctor_power_setpoint)
        for change in (-3, -1, 1, 3):
            b = QPushButton(f"{change:+d}")
            b.setMinimumHeight(33)
            b.setToolTip("Generator input adjustment (dBm), not direct PA watts")
            b.clicked.connect(lambda _checked=False, d=change: self.change_generator_level(d))
            power_controls.addWidget(b)
        power_layout.addWidget(self.doctor_power_controls)
        self.doctor_temperature_card = temp_card
        self.doctor_power_card = power_card
        readings.addWidget(temp_card, 1)
        readings.addWidget(power_card, 1)
        outer.addLayout(readings)

        generator = QFrame()
        generator.setObjectName("ControlCard")
        gen_box = QVBoxLayout(generator)
        gen_box.setContentsMargins(14, 8, 14, 8)
        gen_box.setSpacing(6)
        gen_head = QHBoxLayout()
        gen_head.addWidget(QLabel("▣  R&S SMCV100B  –  RF Generator"))
        gen_head.addStretch()
        self.generator_connection = QLabel("●  LAN: Connecting...")
        gen_head.addWidget(self.generator_connection)
        gen_box.addLayout(gen_head)

        self.generator_output = QLabel("RF: UNKNOWN")
        self.generator_output.setObjectName("GeneratorRFStatus")
        gen_box.addWidget(self.generator_output)
        gen_row = QHBoxLayout()
        self.engineer_dbm_buttons = []
        self.generator_level = None
        for label, delta in (("−3", -3), ("−1", -1)):
            button = QPushButton(label)
            button.setMinimumHeight(42)
            button.clicked.connect(lambda _checked=False, change=delta: self.change_generator_level(change))
            gen_row.addWidget(button, 1)
            self.engineer_dbm_buttons.append(button)
        self.generator_value = QLabel("— dBm")
        self.generator_value.setAlignment(Qt.AlignCenter)
        self.generator_value.setObjectName("FocusValue")
        self.generator_value.setMinimumWidth(115)
        self.generator_value.setObjectName("GeneratorValue")
        gen_row.addWidget(self.generator_value, 3)
        for label, delta in (("+1", 1), ("+3", 3)):
            button = QPushButton(label)
            button.setMinimumHeight(42)
            button.clicked.connect(lambda _checked=False, change=delta: self.change_generator_level(change))
            gen_row.addWidget(button, 1)
            self.engineer_dbm_buttons.append(button)
        gen_box.addWidget(QLabel("RF Generator Power (dBm)"))
        gen_box.addLayout(gen_row)
        slider_row = QHBoxLayout()
        slider_row.addWidget(QLabel("−20"))
        self.generator_slider = QSlider(Qt.Horizontal)
        self.generator_slider.setRange(GENERATOR_UI_MIN, GENERATOR_UI_MAX)
        self.generator_slider.setSingleStep(1)
        self.generator_slider.setPageStep(3)
        self.generator_slider.setValue(GENERATOR_UI_MIN)
        self.generator_slider.setEnabled(False)
        self.generator_slider.sliderReleased.connect(self.commit_generator_slider)
        self.generator_slider.valueChanged.connect(self.preview_generator_slider)
        slider_row.addWidget(self.generator_slider, 1)
        slider_row.addWidget(QLabel("+12 dBm"))
        gen_box.addLayout(slider_row)
        self.generator_limit_label = QLabel(
            "Minimum: −20 dBm   |   Maximum: +12 dBm   |   Readback from generator"
        )
        self.generator_limit_label.setObjectName("FooterMessage")
        gen_box.addWidget(self.generator_limit_label)
        apc_row = QHBoxLayout()
        apc_row.setSpacing(6)
        self.engineer_apc_toggle = QPushButton("Auto W")
        self.engineer_apc_toggle.setObjectName("ApcToggle")
        self.engineer_apc_toggle.setCheckable(True)
        self.engineer_apc_toggle.setMinimumHeight(40)
        self.engineer_apc_toggle.clicked.connect(self.toggle_apc_mode)
        apc_row.addWidget(self.engineer_apc_toggle)
        self.engineer_target_buttons = []
        for label, delta in (("−1 W", -1.0), ("−0.1", -0.1)):
            b = QPushButton(label)
            b.setMinimumHeight(40)
            b.clicked.connect(lambda _checked=False, d=delta: self.change_power_target(d))
            apc_row.addWidget(b, 1)
            self.engineer_target_buttons.append(b)
        self.engineer_target_value = QLabel("— W")
        self.engineer_target_value.setObjectName("GeneratorValue")
        self.engineer_target_value.setAlignment(Qt.AlignCenter)
        apc_row.addWidget(self.engineer_target_value, 2)
        for label, delta in (("+0.1", 0.1), ("+1 W", 1.0)):
            b = QPushButton(label)
            b.setMinimumHeight(40)
            b.clicked.connect(lambda _checked=False, d=delta: self.change_power_target(d))
            apc_row.addWidget(b, 1)
            self.engineer_target_buttons.append(b)
        gen_box.addWidget(QLabel("Output power target (automatic)"))
        gen_box.addLayout(apc_row)
        self.engineer_apc_status = QLabel("")
        self.engineer_apc_status.setObjectName("FooterMessage")
        self.engineer_apc_status.setWordWrap(True)
        gen_box.addWidget(self.engineer_apc_status)
        cal_row = QHBoxLayout()
        cal_row.setSpacing(6)
        self.engineer_cal_label = QLabel("Calibration: —")
        self.engineer_cal_label.setObjectName("FooterMessage")
        self.engineer_cal_label.setWordWrap(True)
        cal_row.addWidget(self.engineer_cal_label, 1)
        self.engineer_cal_save = QPushButton("Save point")
        self.engineer_cal_save.setMinimumHeight(36)
        self.engineer_cal_save.setToolTip("Store the current input dBm and measured output W")
        self.engineer_cal_save.clicked.connect(self.save_calibration_point)
        cal_row.addWidget(self.engineer_cal_save)
        cal_clear = QPushButton("Clear")
        cal_clear.setMinimumHeight(36)
        cal_clear.clicked.connect(self.clear_calibration)
        cal_row.addWidget(cal_clear)
        gen_box.addLayout(cal_row)
        self.engineer_generator_card = generator
        outer.addWidget(generator)

        controls = QFrame()
        self.treatment_controls_card = controls
        controls.setObjectName("ControlCard")
        controls_row = QHBoxLayout(controls)
        controls_row.setContentsMargins(10, 7, 10, 7)
        controls_row.setSpacing(10)
        pump_col = QVBoxLayout()
        self.pump_heading = QLabel("Pump (SIMULATED)" if PUMP_SIMULATED else "Pump ON / OFF")
        self.pump_heading.setObjectName("DoctorHeading")
        self.pump_switch = QPushButton("OFF    ●    ON")
        self.pump_switch.setObjectName("PumpSwitch")
        self.pump_switch.setCheckable(True)
        self.pump_switch.setMinimumHeight(40)
        self.pump_switch.setEnabled(False)
        self.pump_switch.clicked.connect(self.toggle_pump)
        self.pump_value = QLabel("Unknown")
        self.pump_value.setObjectName("FooterMessage")
        pump_col.addWidget(self.pump_heading)
        pump_col.addWidget(self.pump_switch)
        pump_col.addWidget(self.pump_value)
        controls_row.addLayout(pump_col, 1)
        pa_col = QVBoxLayout()
        self.pa_heading = QLabel("PA ON / OFF")
        self.pa_heading.setObjectName("DoctorHeading")
        self.power_switch = PowerSwitch()
        self.power_switch.off_requested.connect(self.request_rf_off)
        self.power_switch.on_requested.connect(self.request_rf_on)
        pa_col.addWidget(self.pa_heading)
        pa_col.addWidget(self.power_switch)
        self.off_button = QPushButton("PA OFF")
        self.off_button.setObjectName("OffButton")
        self.off_button.clicked.connect(self.request_rf_off)
        pa_col.addWidget(self.off_button)
        controls_row.addLayout(pa_col, 1)
        rf_col = QVBoxLayout()
        self.rf_heading = QLabel("RF Output (START / OFF)")
        self.rf_heading.setObjectName("DoctorHeading")
        self.doctor_rf_switch = QPushButton("OFF    ●    ON")
        self.doctor_rf_switch.setObjectName("GeneratorRFSwitch")
        self.doctor_rf_switch.setCheckable(True)
        self.doctor_rf_switch.setMinimumHeight(40)
        self.doctor_rf_switch.setEnabled(False)
        self.doctor_rf_switch.clicked.connect(self.toggle_generator_rf)
        self.doctor_rf_status = QLabel("Unknown")
        self.doctor_rf_status.setObjectName("FooterMessage")
        rf_col.addWidget(self.rf_heading)
        rf_col.addWidget(self.doctor_rf_switch)
        rf_col.addWidget(self.doctor_rf_status)
        controls_row.addLayout(rf_col, 1)
        outer.addWidget(controls)
        self.control_status = QLabel("PA control awaiting STM32 authorization")
        self.control_status.setObjectName("FooterMessage")
        outer.addWidget(self.control_status)
        footer = QHBoxLayout()
        self.engineer_nav = self.navigation_button("Page 2  ›", 2)
        footer.addWidget(self.engineer_nav)
        footer.addStretch()
        self.i2c_status = QLabel("I²C: Unknown")
        self.adc_status = QLabel("ADC: Unknown")
        footer.addWidget(self.i2c_status)
        footer.addWidget(self.adc_status)
        outer.addLayout(footer)
        self.message = QLabel("Waiting for STM32 telemetry")
        self.message.setObjectName("FooterMessage")
        outer.addWidget(self.message)
        self.standards_footer = QLabel(
            "IEC 60601-1 / IEC 60601-1-2: design references, NOT certification  |  "
            "© SAMEER · MeitY · Government of India  |  Engineering prototype"
        )
        self.standards_footer.setObjectName("StandardsFooter")
        self.standards_footer.setAlignment(Qt.AlignCenter)
        self.standards_footer.setWordWrap(True)
        outer.addWidget(self.standards_footer)

    DOCTOR_QSS = """
        QWidget#DoctorPage { background: @bg; color: @ink; font-family: '@font'; }
        QLabel { background: transparent; }
        QLabel#DrMark { background: @accent; color: @accent_ink; border-radius: 15px; font-size: 16px; font-weight: 800; }
        QLabel#DrTitle { font-size: 18px; font-weight: 800; color: @ink; }
        QLabel#DoctorStage { background: @surface; color: @ink2; font-size: 12px; font-weight: bold;
            border: @bw solid @line; border-radius: 14px; padding: 5px 12px; }
        QLabel#DrWho { color: @ink2; font-size: 12px; }
        QLabel#DoctorBanner { background: @surface; color: @ink; border: 2px solid @line;
            border-radius: 16px; padding: 6px 14px; font-size: 13px; font-weight: bold; }
        QLabel#DrCaption { color: @ink2; font-size: 13px; font-weight: bold; }
        QFrame#DrPanel { background: @surface; border: none; border-radius: 22px; }
        QFrame#DrInset { background: @surface2; border: none; border-radius: 22px; }
        QFrame#DrRule { background: @line; border: none; min-height: 1px; max-height: 1px; }
        QLabel#DoctorWatts { color: @ink; font-size: 54px; font-weight: 800; }
        QLabel#DoctorTemp { color: @ink; font-size: 28px; font-weight: 800; }
        QLabel#DoctorSet { color: @ink; font-size: 26px; font-weight: 800; background: @surface;
            border: 2px solid @accent; border-radius: 16px; padding: 6px; }
        QLabel#DoctorNote { color: @ink2; font-size: 11px; }
        QLabel#DoctorLock { color: @ink2; font-size: 12px; padding: 0px 6px; }
        QLabel#DoctorFooter { color: @ink3; font-size: 9px; }
        QPushButton { background: @surface; color: @ink; border: @bw solid @line; border-radius: 14px;
            font-size: 13px; font-weight: bold; padding: 5px 12px; }
        QPushButton:pressed { background: @surface2; }
        QPushButton:disabled { background: transparent; color: @ink3; border: @bw dashed @line; }
        QPushButton#DrStep, QPushButton#DoctorAdjust { background: @accent_soft; color: @ink; border: none;
            border-radius: 16px; font-size: 16px; font-weight: 800; }
        QPushButton#DrStep:pressed, QPushButton#DoctorAdjust:pressed { background: @accent; color: @accent_ink; }
        QPushButton#DoctorAdjust:disabled, QPushButton#DrStep:disabled { background: transparent; color: @ink3; border: @bw dashed @line; }
        QPushButton#DrQuiet, QPushButton#ThemeButton { background: @surface; color: @ink2; border: @bw solid @line;
            border-radius: 14px; font-size: 11px; font-weight: bold; padding: 6px 12px; }
        QPushButton#DoctorArrow { background: @accent_soft; color: @ink; border: none; border-radius: 18px;
            font-size: 22px; font-weight: 800; padding: 0px; }
        QPushButton#DoctorArrow:pressed { background: @accent; color: @accent_ink; }
        QPushButton#DoctorArrow:disabled { background: transparent; color: @ink3; border: @bw dashed @line; }
        QLabel#DoctorAlertNote { color: @warn; font-size: 12px; font-weight: bold; }
        QPushButton#DoctorTarget { background: @accent_soft; color: @ink; border: none; border-radius: 16px;
            font-size: 13px; font-weight: 800; padding: 4px 2px; }
        QPushButton#DoctorTarget:pressed { background: @accent; color: @accent_ink; }
        QPushButton#DoctorTarget:disabled { background: transparent; color: @ink3; border: @bw dashed @line; }
        QPushButton#ApcToggle { background: @surface; color: @ink2; border: @bw solid @line; border-radius: 12px;
            font-size: 11px; padding: 3px 10px; }
        QPushButton#ApcToggle:checked { background: @accent; color: @accent_ink; border-color: @accent; }
        QPushButton#ApcToggle:disabled { background: transparent; color: @ink3; border: @bw dashed @line; }
        QPushButton#DrFooterBtn { background: @accent_soft; border: none; border-radius: 12px; padding: 3px 14px; font-size: 12px; }
        QPushButton#DoctorExit { background: @danger_soft; color: @danger; border: none; border-radius: 14px;
            font-size: 11px; padding: 6px 14px; }
        QPushButton#DoctorExit:pressed { background: @danger; color: @danger_ink; }
        QPushButton#DoctorStart { background: @ok; color: @ok_ink; border: none; border-radius: 20px; font-size: 21px; font-weight: 800; }
        QPushButton#DoctorStart:disabled { background: @ok_soft; color: @ink3; border: none; }
        QPushButton#DoctorStop { background: @danger; color: @danger_ink; border: none; border-radius: 20px; font-size: 21px; font-weight: 800; }
        QPushButton#DoctorStop:disabled { background: @danger_soft; color: @ink3; border: none; }
        QPushButton#DoctorPump, QPushButton#DoctorApp { background: @surface; color: @ink; border: 2px solid @line;
            border-radius: 20px; font-size: 13px; }
        QPushButton#DoctorApp { border: 2px solid @danger_soft; }
        QPushButton#DoctorPump:checked, QPushButton#DoctorApp:checked { background: @ok_soft; color: @ink; border: 2px solid @ok; }
        QPushButton#DoctorPump:disabled, QPushButton#DoctorApp:disabled { background: transparent; color: @ink3;
            border: 2px dashed @line; }
    """

    def build_doctor_page(self):
        """Dedicated 800x480 doctor screen. Same widgets and behaviour as before;
        only placement and styling changed."""
        root = QVBoxLayout(self.doctor_page)
        root.setContentsMargins(14, 8, 14, 5)
        root.setSpacing(6)
        self.doctor_page.setObjectName("DoctorPage")

        # Header: identity on the left, state next to it, session controls right.
        head = QHBoxLayout()
        head.setSpacing(8)
        mark = QLabel("ϟ")
        mark.setObjectName("DrMark")
        mark.setFixedSize(30, 30)
        self._doctor_mark = mark
        mark.setAlignment(Qt.AlignCenter)
        head.addWidget(mark)
        title = QLabel("Microwave Ablation")
        title.setObjectName("DrTitle")
        head.addWidget(title)
        head.addSpacing(4)
        self.doctor_stage_label = QLabel("Current State: System Check")
        self.doctor_stage_label.setObjectName("DoctorStage")
        self.doctor_stage_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        head.addWidget(self.doctor_stage_label, 1)
        head.addWidget(self.theme_button())
        who = QLabel("Doctor")
        who.setObjectName("DrWho")
        head.addWidget(who)
        logout = QPushButton("Log out")
        logout.setObjectName("DrQuiet")
        logout.clicked.connect(self.logout_to_login)
        head.addWidget(logout)
        exit_btn = QPushButton("Exit")
        exit_btn.setObjectName("DoctorExit")
        exit_btn.clicked.connect(self.request_exit)
        head.addWidget(exit_btn)
        for b in (logout, exit_btn, self.theme_buttons[-1]):
            b.setMinimumHeight(36)      # header controls: >= 7 mm on the 7-inch panel
        root.addLayout(head)

        # The one instruction the doctor needs right now.
        self.doctor_notice = QLabel("1. Turn pump ON")
        self.doctor_notice.setObjectName("DoctorBanner")
        self.doctor_notice.setMinimumHeight(34)
        self.doctor_notice.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        root.addWidget(self.doctor_notice)

        content = QHBoxLayout()
        content.setSpacing(12)

        # Left: treatment time dial sits directly on the page, no card.
        time_col = QVBoxLayout()
        time_col.setSpacing(6)
        t_title = QLabel("Treatment time")
        t_title.setObjectName("DrCaption")
        time_col.addWidget(t_title)
        self.doctor_timer_dial = TimerDial(164)
        time_col.addWidget(self.doctor_timer_dial, 0, Qt.AlignHCenter)
        self.doctor_timer_display = QLabel("00:00", self.doctor_page)   # text source for the dial
        self.doctor_timer_display.hide()
        steps = QHBoxLayout()
        steps.setSpacing(6)
        minus = QPushButton("− 10 s")
        plus = QPushButton("+ 10 s")
        for b in (minus, plus):
            b.setObjectName("DrStep")
            b.setMinimumHeight(42)
            steps.addWidget(b)
        minus.clicked.connect(lambda: self.change_timer_setting(-10))
        plus.clicked.connect(lambda: self.change_timer_setting(10))
        self.doctor_timer_minus = minus
        self.doctor_timer_plus = plus
        time_col.addLayout(steps)
        time_col.addStretch(1)
        time_wrap = QWidget()
        time_wrap.setObjectName("Transparent")
        time_wrap.setLayout(time_col)
        time_wrap.setFixedWidth(196)
        content.addWidget(time_wrap)

        # Centre: what the patient is receiving. Largest number on the screen.
        readings = QFrame()
        readings.setObjectName("DrPanel")
        self.doctor_readings_panel = readings
        rl = QVBoxLayout(readings)
        rl.setContentsMargins(20, 12, 20, 12)
        rl.setSpacing(2)
        delivered_h = QLabel("Delivered power")
        delivered_h.setObjectName("DrCaption")
        rl.addWidget(delivered_h)
        self.doctor_watts_display = QLabel("— W")
        self.doctor_watts_display.setObjectName("DoctorWatts")
        self.doctor_watts_display.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        rl.addWidget(self.doctor_watts_display, 1)
        measured = QLabel("Measured at the amplifier output")
        measured.setObjectName("DoctorNote")
        self.doctor_measured_note = measured
        rl.addWidget(measured)
        rl.addSpacing(8)
        rule = QFrame()
        rule.setObjectName("DrRule")
        rl.addWidget(rule)
        rl.addSpacing(6)
        tx = QLabel("Probe temperature (SO24)")
        tx.setObjectName("DrCaption")
        rl.addWidget(tx)
        self.doctor_temp_display = QLabel("— °C")
        self.doctor_temp_display.setObjectName("DoctorTemp")
        self.doctor_temp_display.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        rl.addWidget(self.doctor_temp_display)
        content.addWidget(readings, 6)

        # Right: the setting the doctor can change, on an inset surface.
        mid = QFrame()
        mid.setObjectName("DrInset")
        mid_l = QVBoxLayout(mid)
        mid_l.setContentsMargins(14, 12, 14, 12)
        mid_l.setSpacing(6)
        ph = QLabel("Output power")
        ph.setObjectName("DrCaption")
        mid_l.addWidget(ph)
        # The automatic/manual switch lives on the engineer page only.
        self.doctor_apc_toggle = QPushButton("Auto W", mid)
        self.doctor_apc_toggle.setCheckable(True)
        self.doctor_apc_toggle.hide()
        # Stepper: ▼  5.0 W  ▲   (hold an arrow to keep changing)
        stepper = QHBoxLayout()
        stepper.setSpacing(8)
        self.doctor_down = QPushButton("▼")
        self.doctor_up = QPushButton("▲")
        for button, delta, tip in ((self.doctor_down, -1, "Lower output power by 1 W"),
                                   (self.doctor_up, 1, "Raise output power by 1 W")):
            button.setObjectName("DoctorArrow")
            button.setFixedSize(58, 60)
            button.setAutoRepeat(True)
            button.setAutoRepeatDelay(450)
            button.setAutoRepeatInterval(140)
            button.setToolTip(tip)
            button.clicked.connect(lambda _checked=False, d=delta: self.change_power_target(d))
        self.doctor_set_display = QLabel("— W")
        self.doctor_set_display.setObjectName("DoctorSet")
        self.doctor_set_display.setAlignment(Qt.AlignCenter)
        self.doctor_set_display.setMinimumHeight(60)
        stepper.addWidget(self.doctor_down)
        stepper.addWidget(self.doctor_set_display, 1)
        stepper.addWidget(self.doctor_up)
        mid_l.addLayout(stepper)
        self.doctor_target_buttons = [self.doctor_down, self.doctor_up]
        # Slider for larger changes (before START; arrows only during treatment).
        self.doctor_target_row = QWidget()
        self.doctor_target_row.setObjectName("Transparent")
        slider_row = QHBoxLayout(self.doctor_target_row)
        slider_row.setContentsMargins(0, 2, 0, 0)
        slider_row.setSpacing(8)
        low = QLabel(f"{APC_TARGET_MIN_W:.0f} W")
        low.setObjectName("DoctorNote")
        high = QLabel(f"{APC_TARGET_MAX_W:.0f} W")
        high.setObjectName("DoctorNote")
        self.doctor_target_slider = QSlider(Qt.Horizontal)
        self.doctor_target_slider.setRange(int(round(APC_TARGET_MIN_W)), int(round(APC_TARGET_MAX_W)))
        self.doctor_target_slider.setSingleStep(1)
        self.doctor_target_slider.setPageStep(5)
        self.doctor_target_slider.setMinimumHeight(36)
        self.doctor_target_slider.valueChanged.connect(self.on_target_slider)
        self.doctor_target_slider.sliderReleased.connect(self.commit_target_slider)
        slider_row.addWidget(low)
        slider_row.addWidget(self.doctor_target_slider, 1)
        slider_row.addWidget(high)
        mid_l.addWidget(self.doctor_target_row)
        # Manual mode: generator steps in dBm (previous behaviour).
        self.doctor_dbm_row = QWidget()
        self.doctor_dbm_row.setObjectName("Transparent")
        adjust = QHBoxLayout(self.doctor_dbm_row)
        adjust.setContentsMargins(0, 0, 0, 0)
        adjust.setSpacing(5)
        self.doctor_adjust_buttons = []
        for delta in (-3, -1, 1, 3):
            b = QPushButton(f"{delta:+d}".replace("-", "−"))
            b.setObjectName("DoctorAdjust")
            b.setMinimumHeight(42)
            b.setToolTip("Generator setpoint step in dBm, not delivered watts")
            b.clicked.connect(lambda _checked=False, d=delta: self.change_generator_level(d))
            adjust.addWidget(b)
            self.doctor_adjust_buttons.append(b)
        mid_l.addWidget(self.doctor_dbm_row)
        self.doctor_apc_status = QLabel("")
        self.doctor_apc_status.setObjectName("DoctorAlertNote")
        self.doctor_apc_status.setWordWrap(True)
        mid_l.addWidget(self.doctor_apc_status)
        mid_l.addStretch(1)
        self.doctor_apc_hint = QLabel("")   # kept for compatibility; not shown
        self.doctor_apc_hint.hide()
        content.addWidget(mid, 5)
        root.addLayout(content, 1)

        self.doctor_lock = QLabel("Treatment locked — complete checks")
        self.doctor_lock.setObjectName("DoctorLock")
        self.doctor_lock.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        root.addWidget(self.doctor_lock)

        # Action bar, in the order the steps happen. STOP is always bottom-right.
        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.doctor_pump_button = QPushButton("Pump  OFF")
        self.doctor_pump_button.setObjectName("DoctorPump")
        self.doctor_pump_button.setCheckable(True)
        self.doctor_pump_button.clicked.connect(self.doctor_toggle_pump)
        controls.addWidget(self.doctor_pump_button, 3)
        self.doctor_app_button = QPushButton("Applicator  NOT CONNECTED\n(manual simulation)")
        self.doctor_app_button.setObjectName("DoctorApp")
        self.doctor_app_button.setCheckable(True)
        self.doctor_app_button.clicked.connect(self.doctor_toggle_applicator)
        controls.addWidget(self.doctor_app_button, 4)
        self.doctor_start_button = QPushButton("START")
        self.doctor_start_button.setObjectName("DoctorStart")
        self.doctor_start_button.clicked.connect(self.doctor_start_sequence)
        controls.addWidget(self.doctor_start_button, 3)
        self.doctor_stop_button = QPushButton("STOP")
        self.doctor_stop_button.setObjectName("DoctorStop")
        self.doctor_stop_button.clicked.connect(self.doctor_stop_sequence)
        controls.addWidget(self.doctor_stop_button, 4)
        for child in (self.doctor_pump_button, self.doctor_app_button, self.doctor_start_button, self.doctor_stop_button):
            child.setMinimumHeight(56)
        root.addLayout(controls)

        foot = QHBoxLayout()
        foot.setSpacing(5)
        self.doctor_reset_warning = QPushButton("Reset warning")
        self.doctor_reset_warning.clicked.connect(self.reset_fault)
        self.doctor_reset_cycle = QPushButton("Reset treatment")
        self.doctor_reset_cycle.clicked.connect(self.reset_treatment_cycle)
        for b in (self.doctor_reset_warning, self.doctor_reset_cycle):
            b.setObjectName("DrFooterBtn")
        foot.addWidget(self.doctor_reset_warning)
        foot.addWidget(self.doctor_reset_cycle)
        legal = QLabel("IEC 60601-1 / IEC 60601-1-2 design references (NOT certified)  |  © SAMEER, MeitY  |  Prototype")
        legal.setObjectName("DoctorFooter")
        legal.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        foot.addWidget(legal, 1)
        root.addLayout(foot)

    def doctor_toggle_pump(self, _checked=False):
        # Use existing STM32 PUMP_ON/PUMP_OFF commands + telemetry, never fake
        # feedback or alter the Engineer pump behavior.
        self.toggle_pump()
        self.refresh_doctor_display()

    def doctor_toggle_applicator(self, checked=False):
        if self.pump_reported_on is not True and checked:
            self.doctor_app_button.setChecked(False)
            self.set_status("First confirm the pump, then acknowledge the applicator")
            self.refresh_doctor_display()
            return
        if self.timer_deadline is not None or self.pending_treatment_start or self.doctor_auto_start:
            self.doctor_app_button.setChecked(self.doctor_applicator_ack)
            self.set_status("Applicator acknowledgement locked during treatment")
            return
        self.doctor_applicator_ack=bool(checked)
        self.log_event("MANUAL_APPLICATOR_ACK",comment="SIMULATED ONLY; no connection sensor" if checked else "Manual acknowledgement cleared")
        self.refresh_doctor_display()

    def doctor_stop_sequence(self):
        self.doctor_auto_start=False
        self.doctor_auto_deadline=0.0
        if self.timer_deadline is not None or self.pending_treatment_start:
            self.stop_treatment_timer()
        else:
            self.request_all_rf_off("DOCTOR_STOP")
        self.refresh_doctor_display()

    def doctor_start_sequence(self):
        # Strict sequence: reported pump command ON -> manually acknowledged
        # applicator -> set timer -> confirmation -> STM32 PA ACK + GPIO readback
        # -> generator RF readback -> timer starts (existing implementation).
        if self.role!="doctor" or self.exit_pending or self.doctor_auto_start:
            return
        if self.fault_latched or self.cycle_outcome or self.logging_failed or self.session_ended:
            self.notify_popup("DR_BLOCKED", "Start blocked", "Resolve the fault or reset the previous treatment first.")
            return
        if self.pump_reported_on is not True:
            self.notify_popup("DR_PUMP", "Pump required",
                "Confirm the coolant pump is running (SIMULATED — not controlled or monitored by the system)."
                if PUMP_SIMULATED else "Turn the pump ON and wait for STM32 pump command-state feedback.")
            return
        if not self.doctor_applicator_ack:
            self.notify_popup("DR_APPLICATOR", "Applicator check", "Manually acknowledge the applicator connection (SIMULATED; no sensor is installed).")
            return
        if not 0 < self.timer_setting_sec <= 360:
            self.notify_popup("DR_TIMER", "Set treatment time", "Set a treatment duration before starting.")
            return
        if not self.serial_connected or not self.generator_connected or self.generator_rf_on is not False:
            self.notify_popup("DR_COMM", "System unavailable", "STM32 and generator must be connected, with RF confirmed OFF.")
            return
        if self.generator_level is None:
            self.notify_popup("DR_LEVEL", "Generator level unavailable", "Wait for a valid power setpoint readback before starting.")
            return
        if not self.last_good_sensor or self.active_fault(self.last_telemetry)[0] is not None:
            self.notify_popup("DR_CHECK", "Safety check failed", "The existing sensor/monitor checks are not satisfied.")
            return
        if not self.outputs_off_reported():
            self.notify_popup("DR_OFF", "Outputs not verified OFF", "PA and generator RF must be reported OFF before automatic startup.")
            return
        reply=QMessageBox.question(
            self,"Confirm treatment start",
            "Start treatment?\n\nPump command: ON (flow not measured)\n"
            "Applicator: MANUALLY ACKNOWLEDGED (not sensor-verified)\n"
            f"Duration: {self.format_duration(self.timer_setting_sec)}\n"
            + (f"Output power: {self.apc.target_w:.1f} W, held automatically "
               f"(generator starts at {self.apc.start_level():+.2f} dBm and ramps up)\n\n"
               if self.apc_enabled else f"Generator setpoint: {self.generator_level:+.1f} dBm\n\n") +
            "On confirmation, request PA enable; only after STM32 confirmation, request RF ON and begin the timer after generator readback.\n\n"
            "ENGINEERING PROTOTYPE — do not use on patients.",
            QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if reply!=QMessageBox.Yes:
            return
        # Recheck live state after user confirmation.
        if (self.pump_reported_on is not True or not self.doctor_applicator_ack
                or not self.outputs_off_reported() or self.active_fault(self.last_telemetry)[0] is not None):
            self.set_status("Conditions changed — START cancelled")
            return
        self.doctor_auto_start=True
        self.doctor_auto_deadline=time.monotonic()+4.0
        self.log_event("DOCTOR_AUTO_START_REQUEST",comment="manual applicator acknowledgement — no sensor")
        self.request_rf_on()  # inherited hardware safety checks/STM32 REQUEST_ON
        if not self.commanded_on and not self.pending_on_deadline:
            self.doctor_auto_start=False
            self.doctor_auto_deadline=0.0
        self.refresh_doctor_display()

    def check_doctor_auto_start(self):
        if not self.doctor_auto_start or self.role!="doctor":
            return
        if (self.exit_pending or self.fault_latched or self.cycle_outcome
                or self.pump_reported_on is not True or not self.doctor_applicator_ack
                or not self.serial_connected or not self.generator_connected
                or self.generator_rf_on is not False
                or self.active_fault(self.last_telemetry)[0] is not None):
            self.cancel_doctor_auto_start("Pre-start condition lost")
            return
        p=self.last_telemetry
        if self.commanded_on and p.get("enable")==0 and p.get("alarm_in")==0:
            self.doctor_auto_start=False
            self.doctor_auto_deadline=0.0
            self.start_treatment_timer()  # existing RF readback -> timer path
            if not self.pending_treatment_start and self.timer_deadline is None:
                self.request_all_rf_off("START_REJECTED")
        elif time.monotonic()>self.doctor_auto_deadline:
            self.cancel_doctor_auto_start("STM32 PA-enable readback timed out")
        self.refresh_doctor_display()

    def cancel_doctor_auto_start(self, reason):
        if not self.doctor_auto_start:
            return
        self.doctor_auto_start=False
        self.doctor_auto_deadline=0.0
        self.log_event("DOCTOR_START_ABORTED",comment=reason)
        self.request_all_rf_off("DOCTOR_START_ABORT")
        self.set_status(reason)
        self.notify_popup("DOCTOR_START_ABORT", "Start cancelled", reason+". PA/RF OFF requested; verify hardware state.",serious=True)
        self.refresh_doctor_display()

    def refresh_doctor_display(self):
        if not hasattr(self,"doctor_stage_label") or self.role!="doctor":
            return
        self.doctor_stage_label.setText("Current State: "+self.current_stage.title())
        self.doctor_timer_display.setText(self.timer_value.text())
        self.doctor_temp_display.setText(self.ntc_value.text())
        self.doctor_watts_display.setText(self.doctor_power_value.text())
        level=self.generator_level
        if self.apc_enabled:
            self.doctor_set_display.setText(f"{self.apc.target_w:.1f} W")
        else:
            self.doctor_set_display.setText(f"{level:+.1f} dBm" if level is not None else "— dBm")
        self.refresh_power_control_ui()
        if PUMP_SIMULATED:
            known=True
            on=self.pump_sim_ack
        else:
            known=(self.serial_connected and self.last_packet_time>0 and
                   time.monotonic()-self.last_packet_time<=TELEMETRY_TIMEOUT and
                   self.pump_reported_on is not None)
            on=known and self.pump_reported_on is True
        if not on and self.doctor_applicator_ack and self.timer_deadline is None and not self.pending_treatment_start:
            self.doctor_applicator_ack=False  # force re-acknowledgement after pump changes OFF
        self.doctor_pump_button.blockSignals(True)
        self.doctor_pump_button.setChecked(on)
        self.doctor_pump_button.blockSignals(False)
        if PUMP_SIMULATED:
            self.doctor_pump_button.setText("Pump  CONFIRMED\n(SIMULATED)" if on
                                            else "Pump  NOT CONFIRMED\n(SIMULATED)")
        else:
            self.doctor_pump_button.setText("Pump  ON" if on else "Pump  OFF" if known else "Pump  UNKNOWN")
        self.doctor_pump_button.setEnabled(known and self.pump_pending is None and
            self.timer_deadline is None and not self.pending_treatment_start and not self.doctor_auto_start)
        self.doctor_app_button.setChecked(self.doctor_applicator_ack)
        self.doctor_app_button.setText("Applicator  ACKNOWLEDGED\n(SIMULATED)" if self.doctor_applicator_ack
                                        else "Applicator  NOT ACKNOWLEDGED\n(SIMULATED)")
        self.doctor_app_button.setEnabled(on and self.timer_deadline is None and
             not self.pending_treatment_start and not self.doctor_auto_start)
        self.doctor_timer_minus.setEnabled(self.timer_minus.isEnabled() and not self.doctor_auto_start)
        self.doctor_timer_plus.setEnabled(self.timer_plus.isEnabled() and not self.doctor_auto_start)
        for b in self.doctor_adjust_buttons:
            b.setEnabled(level is not None and self.generator_connected and not self.apc_enabled and
                not self.fault_latched and not self.logging_failed and not self.doctor_auto_start)
        self.doctor_reset_warning.setVisible(self.fault_latched)
        self.doctor_reset_cycle.setVisible(bool(self.cycle_outcome) and not self.fault_latched)
        active=(self.timer_deadline is not None or self.pending_treatment_start or self.doctor_auto_start)
        if self.fault_latched:
            msg="SAFETY FAULT: "+(self.fault_reason or "Check hardware")
        elif self.cycle_outcome:
            msg="Treatment "+self.cycle_outcome.lower()+" — verify PA and RF OFF; reset to begin again"
        elif active:
            msg="Treatment active — monitoring enabled" if self.timer_deadline else "Starting: awaiting confirmed PA / RF state"
        elif not on:
            msg=("Step 1 / 3  •  Confirm the coolant pump is running (SIMULATED)" if PUMP_SIMULATED
                 else "Step 1 / 3  •  Turn pump ON (coolant flow not measured)")
        elif not self.doctor_applicator_ack:
            msg="Step 2 / 3  •  Confirm applicator connection manually (SIMULATED)"
        elif self.timer_setting_sec==0:
            msg="Step 3 / 3  •  Set the treatment time"
        elif not self.last_good_sensor:
            msg="Sensor unavailable — treatment locked"
        elif not self.outputs_off_reported():
            msg="PA/RF OFF state not verified — treatment locked"
        else:
            msg="Checks complete for prototype — select START to confirm"
        # Always prefer critical warning text to an informational sequence prompt.
        # Priority (IEC 60601-1-8): high = fault, medium/low = active warning,
        # low = cycle ended and needs checking, info = next-step guidance.
        if self.fault_latched:
            priority, text = "high", msg
        elif self.warning_key and active:
            priority, text = (self.banner_flasher.priority or "medium"), self.warning_banner.text()
        elif self.cycle_outcome:
            priority, text = "low", msg
        else:
            priority, text = "info", msg
        marker = {"high": "!!!  ", "medium": "!!  ", "low": "!  "}.get(priority, "ℹ  ")
        self.doctor_notice.setText(marker + text)
        self.notice_flasher.set_priority(priority)
        ready=(not active and not self.cycle_outcome and not self.fault_latched
               and not self.logging_failed and on and self.doctor_applicator_ack
               and self.timer_setting_sec>0 and self.last_good_sensor and level is not None and
               self.outputs_off_reported() and self.active_fault(self.last_telemetry)[0] is None)
        self.doctor_start_button.setEnabled(ready)
        self.doctor_stop_button.setEnabled(active or self.commanded_on or self.generator_rf_on is True)
        self.doctor_lock.setText(
            ("START available — pump and applicator confirmed manually (SIMULATED); flow unverified"
             if PUMP_SIMULATED else "START available — manual applicator acknowledgement only; flow unverified") if ready
            else "PA/RF startup in progress — STOP available" if active
            else "Treatment locked — complete required checks" if not self.cycle_outcome
            else "Cycle ended — verify outputs OFF, then reset")
        # Dial is display-only; it mirrors the timer text and state above.
        if self.timer_deadline is not None:
            remaining = max(0.0, self.timer_deadline - time.monotonic())
            self.doctor_timer_dial.set_state(self.timer_value.text(),
                remaining / max(1, self.timer_setting_sec), "active", "Remaining")
        elif self.cycle_outcome:
            self.doctor_timer_dial.set_state(self.timer_value.text(), 0.0, "done", "Cycle ended")
        elif active:
            self.doctor_timer_dial.set_state(self.timer_value.text(),
                self.timer_setting_sec / TimerDial.MAX_SEC, "starting", "Starting")
        else:
            self.doctor_timer_dial.set_state(self.timer_value.text(),
                self.timer_setting_sec / TimerDial.MAX_SEC, "set", "Max 6 min")

    def build_monitor_page(self):
        outer = QVBoxLayout(self.monitor_page)
        outer.setContentsMargins(12, 9, 12, 9)
        outer.setSpacing(8)
        heading = QHBoxLayout()
        monitor_title = QLabel("Amplifier telemetry")
        monitor_title.setObjectName("PageTitle")
        heading.addWidget(monitor_title)
        heading.addStretch()
        heading.addWidget(self.theme_button())
        btn = QPushButton("Exit")
        btn.setObjectName("ExitButton")
        btn.clicked.connect(self.request_exit)
        logout = QPushButton("LOGOUT")
        logout.setObjectName("NavButton")
        logout.clicked.connect(self.logout_to_login)
        heading.addWidget(logout)
        heading.addWidget(btn)
        outer.addLayout(heading)
        definitions = [
            ("forward", "Forward Power", "dBm", "I²C Reg 245"),
            ("reflected", "Reflected Power", "dBm", "I²C Reg 247"),
            ("vswr", "VSWR", ":1", "Calculated from P_f / P_r"),
            ("voltage", "Supply Voltage", "V", "I²C Reg 251"),
            ("current", "Supply Current", "A", "I²C Reg 249"),
            ("temperature", "Temperature", "°C", "I²C Reg 253"),
            ("alarm", "Alarm", "", "I²C Reg 119"),
            ("i2c", "I²C Status", "", "Amplifier ↔ STM32"),
            ("uart", "UART Status", "", "STM32 ↔ Raspberry Pi"),
        ]
        grid = QGridLayout()
        grid.setSpacing(8)
        self.cards = {}
        for index, (key, title, unit, source) in enumerate(definitions):
            card = MetricCard(title, unit, source)
            self.cards[key] = card
            grid.addWidget(card, index // 3, index % 3)
        for i in range(3):
            grid.setColumnStretch(i, 1)
            grid.setRowStretch(i, 1)
        outer.addLayout(grid, 1)
        nav = QHBoxLayout()
        nav.addWidget(self.navigation_button("‹  Home", 1))
        nav.addStretch()
        nav.addWidget(self.navigation_button("Additional Data  ›", 3))
        outer.addLayout(nav)

    @staticmethod
    def format_duration(seconds):
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def update_flow_stage(self):
        if not hasattr(self, "flow_labels"):
            return
        if self.fault_latched:
            index, name = 4, "FAULT / STOP REQUESTED"
        elif self.cycle_outcome == "STOPPED":
            index, name = 4, "TREATMENT STOPPED"
        elif self.cycle_outcome == "COMPLETE":
            index, name = 4, "ABLATION COMPLETE"
        elif self.timer_deadline is not None:
            index, name = 3, "TREATMENT ACTIVE"
        elif self.pending_treatment_start:
            index, name = 2, "STARTING RF — AWAITING READBACK"
        elif self.role == "doctor" and self.doctor_auto_start:
            index, name = 2, "STARTING PA — AWAITING READBACK"
        elif self.timer_setting_sec > 0:
            index, name = 2, "CONFIGURATION & SETUP"
        elif self.serial_connected and self.generator_connected:
            index, name = 1, "STANDBY"
        else:
            index, name = 0, "INITIALIZE / SYSTEM CHECK"
        self.current_stage = name
        if self.role == "doctor":
            self.refresh_doctor_display()
        for i, step in enumerate(self.flow_labels):
            if i == index:
                step.setStyleSheet(tok("background:@accent;color:@accent_ink;border:none;border-radius:13px;font-weight:bold;"))
            elif i < index:
                step.setStyleSheet(tok("background:@ok_soft;color:@ok;border:none;border-radius:13px;font-weight:bold;"))
            else:
                step.setStyleSheet(tok("background:@surface;color:@ink3;border:none;border-radius:13px;"))

    def notify_popup(self, key, title, message, serious=False):

        if key in self._popup_keys:
            return
        self._popup_keys.add(key)
        dialog = QMessageBox(self)
        dialog.setWindowTitle(title)
        dialog.setIcon(QMessageBox.Critical if serious else QMessageBox.Warning)
        dialog.setText(message)
        dialog.setInformativeText("Acknowledge this notification. RF OFF requests must be independently verified.")
        dialog.setStandardButtons(QMessageBox.Ok)
        dialog.button(QMessageBox.Ok).setText("ACKNOWLEDGE")
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        self._active_popup = dialog
        dialog.open()

    def change_timer_setting(self, delta_seconds):
        if (self.timer_deadline is not None or self.pending_treatment_start
                or self.fault_latched or self.session_ended or self.cycle_outcome):
            self.set_status("Select RESET TREATMENT before configuring another cycle")
            return
        self.timer_setting_sec = max(0, min(360, self.timer_setting_sec + int(delta_seconds)))
        self.timer_value.setText(self.format_duration(self.timer_setting_sec))
        self._popup_keys.discard("TIMER_NOT_SET")
        self.update_flow_stage()
        self.log_event("TIMER_SETTING", comment=str(self.timer_setting_sec))
        self.refresh_doctor_display()

    def start_treatment_timer(self):
        if (self.role is None or self.session_ended or self.logging_failed
                or self.fault_latched or self.cycle_outcome):
            self.set_status("Reset the previous treatment or clear the fault before START")
            return
        if self.timer_deadline is not None or self.pending_treatment_start:
            return
        if not (0 < self.timer_setting_sec <= 360):
            self.set_status("TIMER NOT SET — choose treatment duration first")
            self.notify_popup("TIMER_NOT_SET", "Timer not set", "Set a treatment duration (10-second increments) before enabling PA or RF.")
            return
        if not self.serial_connected or not self.generator_connected or self.generator_rf_on is not False:
            self.notify_popup("PRECHECK_COMMS", "System check required", "Check STM32 UART and generator LAN. Generator RF must be confirmed OFF before START.")
            return
        if self.pump_reported_on is not True or not self.last_good_sensor:
            self.notify_popup("PRECHECK_COOLING", "Cooling check required", ("Pump not confirmed (SIMULATED) or SO24 temperature invalid. Coolant flow requires physical verification." if PUMP_SIMULATED else "Pump command and SO24 temperature are not verified. Motor operation and coolant flow also require physical verification."))
            return
        p = self.last_telemetry
        if not (self.commanded_on and p.get("enable") == 0 and p.get("alarm_in") == 0):
            self.notify_popup("PA_NOT_ENABLED", "PA not ready", "Enable the PA first, then wait for STM32 telemetry to report its enabled command state.")
            return
        if self.active_fault(p)[0] is not None:
            self.notify_popup("PRECHECK_FAULT", "Monitoring fault", "Resolve the monitoring fault before attempting START.", serious=True)
            return

        if self.apc_enabled:
            # Automatic power: RF starts at a level below the target, then ramps up.
            start_level = self.apc.start_level()
            if not self.generator_worker.set_power(start_level):
                self.notify_popup("APC_START", "Power control unavailable",
                                  "The starting generator level could not be requested.", serious=True)
                return
            self.apc.arm(start_level, time.monotonic())
            self.log_event("APC_START", comment=f"target {self.apc.target_w:.1f} W, start {start_level:+.2f} dBm")
        self.pending_treatment_start = True
        self.timer_minus.setEnabled(False)
        self.timer_plus.setEnabled(False)
        self.start_button.setEnabled(False)
        self.update_flow_stage()
        self.generator_worker.set_output(True)
        self.log_event("START_RF_ON_REQUEST")
        self.set_status("START: RF ON requested; waiting for generator readback")
        QTimer.singleShot(4500, self.check_pending_start)

    def check_pending_start(self):
        if self.pending_treatment_start:
            self.pending_treatment_start = False
            self.cycle_outcome = "STOPPED"
            self.timer_minus.setEnabled(False)
            self.timer_plus.setEnabled(False)
            self.start_button.setEnabled(False)
            self.timer_value.setText("00:00")
            self.log_event("TREATMENT_STOPPED", comment="RF ON readback timed out")
            self.request_all_rf_off("RF_ON_READBACK_TIMEOUT")
            self.update_flow_stage()
            self.notify_popup("RF_START_TIMEOUT", "TREATMENT STOPPED", "Generator RF ON readback did not arrive. PA/RF OFF requested; verify shutdown before resetting.", serious=True)

    def stop_treatment_timer(self):
        if self.role == "doctor" and self.doctor_auto_start:
            self.cancel_doctor_auto_start("Operator pressed STOP")
            return
        if self.timer_deadline is None and not self.pending_treatment_start:
            return
        self.timer_deadline = None
        self.pending_treatment_start = False
        self.timer_elapsed = False
        self.cycle_outcome = "STOPPED"
        self.timer_minus.setEnabled(False)
        self.timer_plus.setEnabled(False)
        self.start_button.setEnabled(False)
        self.timer_value.setText("00:00")
        self.log_event("TREATMENT_STOPPED", comment="Operator pressed STOP")
        self.request_all_rf_off("MANUAL_STOP")
        self.warning_banner.setText("TREATMENT STOPPED — PA/RF OFF requested; verify OFF state")
        self.update_flow_stage()
        self.notify_popup("CYCLE_STOPPED", "TREATMENT STOPPED",
                          "Operation stopped by operator. RF generator and PA OFF requested. "
                          "Verify hardware outputs are OFF before resetting.", serious=True)

    def update_treatment_timer(self):
        if self.timer_deadline is None:
            return
        remaining = max(0, math.ceil(self.timer_deadline - time.monotonic()))
        if remaining != self.timer_last_shown:
            self.timer_last_shown = remaining
            self.timer_value.setText(self.format_duration(remaining))
            self.refresh_doctor_display()
        if remaining == 0:

            if (not self.serial_connected or not self.generator_connected
                    or self.last_packet_time <= 0
                    or time.monotonic() - self.last_packet_time > TELEMETRY_TIMEOUT
                    or self.generator_rf_on is not True
                    or self.active_fault(self.last_telemetry)[0] is not None):
                self.emergency_stop("END_CHECK_FAILED", "Treatment end check failed or monitoring unverified")
                return
            self.timer_deadline = None
            self.timer_elapsed = True
            self.cycle_outcome = "COMPLETE"
            self.timer_minus.setEnabled(False)
            self.timer_plus.setEnabled(False)
            self.start_button.setEnabled(False)
            self.log_event("TIMER_EXPIRED")
            self.request_all_rf_off("TIMER_EXPIRED")
            self.warning_banner.setText("TIME COMPLETE — PA OFF / RF OFF requested; verify outputs")
            self.update_flow_stage()
            self.notify_popup("TIMER_EXPIRED", "ABLATION COMPLETE",
                              "Preset time elapsed without a GUI-detected stop. PA and generator "
                              "RF OFF have been requested, NOT independently verified. "
                              "Confirm shutdown, then use RESET TREATMENT for another cycle.")
            self.set_status("TIMER EXPIRED: RF generator OFF + PA DISABLE requested", 10)

    def show_warning(self, key, message, critical=False):
        if key != self.warning_key:
            self.warning_key = key
            self.log_event("WARNING" if not critical else "SAFETY_FAULT", comment=message)
        self.warning_banner.setText(message)
        if key not in ("NO_PUMP_SPEED_FEEDBACK",):
            self.notify_popup("WARN_" + key, "Treatment warning" if not critical else "TREATMENT STOPPED", message, serious=critical)
        self._style_banner(critical, key)

    def _style_banner(self, critical, key=None):
        self._banner_critical = bool(critical)
        if critical:
            priority = "high"
        elif key == "NO_PUMP_SPEED_FEEDBACK":
            priority = "low"       # advisory, no popup: constant, not flashing
        else:
            priority = "medium"
        self.banner_flasher.set_priority(priority)

    def emergency_stop(self, reason, message):
        if self.fault_latched:
            return

        self.doctor_auto_start = False
        self.doctor_auto_deadline = 0.0
        self.fault_latched = True
        self.fault_reason = reason
        self.timer_deadline = None
        self.pending_treatment_start = False
        self.timer_elapsed = True
        self.cycle_outcome = "STOPPED"
        self.timer_value.setText("00:00")
        self.timer_minus.setEnabled(False)
        self.timer_plus.setEnabled(False)
        self.start_button.setEnabled(False)
        self.reset_fault_button.setEnabled(True)
        self.update_flow_stage()
        self.show_warning(reason, message + " — RF/PA OFF REQUESTED", critical=True)
        self.request_all_rf_off(reason)
        self.log_event("TREATMENT_STOP_FAULT", comment=message)
        self.set_status(message + "; RF OFF requested, verify hardware state", 15)

    def pump_is_on(self, p):
        if PUMP_SIMULATED:
            return self.pump_sim_ack
        return p.get("pump_on") == 1

    def active_fault(self, p):
        if p.get("so24_valid") != 1 or not isinstance(p.get("so24_temp_mC"), int):
            return "SO24_INVALID", "SO24 temperature sensor invalid"
        ntc = p["so24_temp_mC"] / 1000
        if ntc > 60.0:
            return "SO24_OVER_60", "TEMPERATURE OVEREXCEED — SO24 above 60°C"
        if not self.pump_is_on(p):
            if PUMP_SIMULATED:
                return "PUMP_NOT_CONFIRMED", "Pump confirmation (SIMULATED) cleared during treatment"
            return "PUMP_COMMAND_OFF", "Pump command OFF/unavailable during treatment"
        if p.get("conn") != 1 or p.get("valid") != 1:
            return "PA_MONITOR_INVALID", "PA monitoring invalid during treatment"
        pa_temp = p.get("temp_mC")
        if not isinstance(pa_temp, int):
            return "PA_TEMP_INVALID", "PA temperature reading unavailable"
        if self.generator_rf_on is True and p.get("enable") == 0 and p.get("alarm_in") == 0:
            if p.get("rf_valid") != 1:
                return "RF_METRIC_INVALID", "RF monitoring invalid during treatment"
            f, r = p.get("fwd_mdBm"), p.get("refl_mdBm")
            if not isinstance(f, int) or not isinstance(r, int):
                return "RF_METRIC_INVALID", "RF power measurements unavailable"
            pf = f / 1000
            pr = r / 1000

            if pf >= 0.0:
                v = calculate_vswr(pf, pr)
                if v is None:
                    return "VSWR_INVALID", "VSWR measurement invalid during RF output"
                if v > 2.0:
                    return "VSWR_OVER_2", "REFLECTED POWER OVERLOADED — VSWR above 2"
        return None, None

    def reset_fault(self):
        if not self.fault_latched or self.timer_deadline is not None:
            return
        if not self.serial_connected or not self.generator_connected:
            self.set_status("Cannot reset fault: hardware communication unavailable")
            return
        if not self.last_telemetry or self.active_fault(self.last_telemetry)[0] is not None:
            self.set_status("Cannot reset: fault condition is unresolved")
            return
        if self.generator_rf_on is not False or self.last_telemetry.get("enable") != 1:
            self.set_status("Cannot reset: RF and PA OFF not confirmed")
            return
        self.fault_latched = False
        self.fault_reason = ""
        self.timer_elapsed = False
        self.timer_minus.setEnabled(False if self.cycle_outcome else True)
        self.timer_plus.setEnabled(False if self.cycle_outcome else True)
        self.start_button.setEnabled(False if self.cycle_outcome else True)
        self.reset_fault_button.setEnabled(False)
        self.timer_value.setText("00:00" if self.cycle_outcome else self.format_duration(self.timer_setting_sec))
        self.warning_key = None
        self.warning_banner.setText("Fault reset locally — verify independent interlocks")
        self.banner_flasher.set_priority(None)
        self.log_event("FAULT_RESET")
        self._popup_keys.clear()
        self.update_flow_stage()

    def outputs_off_reported(self):
        p = self.last_telemetry
        fresh = (self.last_packet_time > 0 and
                 time.monotonic() - self.last_packet_time <= TELEMETRY_TIMEOUT)
        return bool(self.serial_connected and self.generator_connected and fresh
                    and self.generator_rf_on is False
                    and p.get("enable") == 1 and p.get("alarm_in") == 1
                    and not self.pending_treatment_start and self.timer_deadline is None)

    def reset_treatment_cycle(self):
        if self.role is None or self.exit_pending:
            return
        if self.timer_deadline is not None or self.pending_treatment_start:
            self.notify_popup("RESET_ACTIVE", "Stop operation first",
                              "STOP treatment and wait for RF/PA OFF before resetting.", True)
            return
        if not self.outputs_off_reported():
            self.notify_popup("RESET_NOT_SAFE", "Reset blocked",
                              "Cannot reset until STM32 telemetry reports PA DISABLED and "
                              "generator readback reports RF OFF. Verify shutdown independently.", True)
            return
        if self.fault_latched:
            self.notify_popup("RESET_FAULT", "Fault must be cleared",
                              "Clear the physical fault and use RESET WARNING first. "
                              "Resetting the treatment cannot bypass a fault latch.", True)
            return
        if self.logging_failed:
            self.notify_popup("RESET_LOGGING", "Logging unavailable",
                              "Logging failure must be resolved before restarting.", True)
            return
        previous = self.cycle_outcome or "CANCELLED_BEFORE_START"
        self.log_event("TREATMENT_CYCLE_RESET", comment=previous)

        if self.generator_connected:
            self.generator_worker.set_power(GENERATOR_RESET_DBM)
            self.log_event("GENERATOR_SETPOINT_RESET_REQUEST", comment=f"{GENERATOR_RESET_DBM} dBm")
            self.preset_input_for_target()
        self.timer_deadline = None
        self.pending_treatment_start = False
        self.timer_setting_sec = 0
        self.timer_elapsed = False
        self.timer_last_shown = -1
        self.cycle_outcome = None
        self.timer_value.setText("00:00")
        self.timer_minus.setEnabled(True)
        self.timer_plus.setEnabled(True)
        self.start_button.setEnabled(True)
        self.warning_key = None
        self.warning_banner.setText("Ready for new cycle · Set treatment time · Verify cooling")
        self.banner_flasher.set_priority(None)
        self._popup_keys.clear()
        self.doctor_power_value.setText("0.00 W")
        self.update_flow_stage()
        self.refresh_doctor_display()
        self.set_status("Treatment settings reset — ready to configure a new cycle")

    def logout_to_login(self):
        if self.role is None or self.exit_pending:
            return
        if self.timer_deadline is not None or self.pending_treatment_start:
            self.notify_popup("LOGOUT_ACTIVE", "Treatment active",
                              "STOP treatment and confirm PA/RF shutdown before logout.", True)
            return
        if self.fault_latched:
            self.notify_popup("LOGOUT_FAULT", "Fault is latched",
                              "Resolve the hardware fault and clear RESET WARNING before logging out.", True)
            return
        if not self.outputs_off_reported():
            self.notify_popup("LOGOUT_NOT_SAFE", "Logout blocked",
                              "Generator RF OFF and PA DISABLED are not both reported. "
                              "Verify shutdown before logging out.", True)
            return
        self.log_event("LOGOUT", comment="Role session ended, outputs reported off")
        if self.logger is not None:
            self.logger.close()
            self.logger = None
        self.session_id = None
        self.role = None
        self.operator = ""
        self.patient_id = ""
        self.cycle_outcome = None
        self.timer_setting_sec = 0
        self.timer_elapsed = False
        self.fault_latched = False
        self.fault_reason = ""
        self.reset_fault_button.setEnabled(False)
        self.timer_value.setText("00:00")
        self.timer_minus.setEnabled(True)
        self.timer_plus.setEnabled(True)
        self.start_button.setEnabled(True)
        self.warning_key = None
        self.warning_banner.setText("Monitoring standby · pump flow/speed not verified")
        self.banner_flasher.set_priority(None)
        self._popup_keys.clear()
        self.doctor_applicator_ack = False
        self.doctor_auto_start = False
        self.doctor_auto_deadline = 0.0
        self.pages.setCurrentIndex(0)
        self.update_flow_stage()

    def request_all_rf_off(self, reason):
        self.request_rf_off()
        if self.generator_connected:
            self.generator_worker.set_output(False)
        self.log_event("RF_OFF_REQUEST", reason)

    def get_forward_watts(self, packet=None):
        p = packet if packet is not None else self.last_telemetry

        if not p or p.get("conn") != 1 or p.get("valid") != 1:
            return None
        if (self.generator_rf_on is False or
                (p.get("enable") == 1 and p.get("alarm_in") == 1)):
            return 0.0
        if (self.generator_rf_on is not True or p.get("enable") != 0 or
                p.get("alarm_in") != 0 or p.get("rf_valid") != 1):
            return None
        raw = p.get("fwd_mdBm")
        return forward_dbm_to_watts(raw / 1000.0 if isinstance(raw, int) else None, True)

    def log_event(self, event, comment="", packet=None):
        if self.logger is None or self.logging_failed:
            return
        p = packet if packet is not None else self.last_telemetry
        remaining = max(0, math.ceil(self.timer_deadline - time.monotonic())) if self.timer_deadline else 0
        power = self.get_forward_watts(p)
        raw = p.get("fwd_mdBm")
        forward_dbm = raw / 1000.0 if isinstance(raw, int) and p.get("rf_valid") == 1 else ""
        row = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "session_id": self.session_id, "role": self.role,
            "operator": self.operator, "patient_id": self.patient_id,
            "language": self.language, "event": event,
            "stm32_ms": p.get("ms", ""),
            "timer_setting_sec": self.timer_setting_sec, "remaining_sec": remaining,
            "so24_temp_c": round(p["so24_temp_mC"] / 1000, 3)
                if p.get("so24_valid") == 1 and isinstance(p.get("so24_temp_mC"), int) else "",
            "so24_valid": p.get("so24_valid", ""),
            "power_w": round(power, 6) if power is not None else "",
            "power_dbm": forward_dbm,
            "rf_measurement_valid": p.get("rf_valid", ""),
            "pa_command_state": ("DISABLED" if p.get("enable") == 1 and p.get("alarm_in") == 1 else
                                 "ENABLED" if p.get("enable") == 0 and p.get("alarm_in") == 0 else "UNKNOWN"),
            "rf_generator_reported_on": self.generator_rf_on if self.generator_rf_on is not None else "",
            "pump_command_state": (("SIM_CONFIRMED" if self.pump_sim_ack else "SIM_NOT_CONFIRMED")
                                   if PUMP_SIMULATED else p.get("pump_on", "")),
            "comment": comment, "vswr": "",
            "generator_setpoint_dbm": self.generator_level if self.generator_level is not None else "",
        }
        for field in CsvSessionLogger.ENGINEER_FIELDS:
            if field not in row:
                row[field] = p.get(field, "")
        if p.get("rf_valid") == 1:
            f, r = p.get("fwd_mdBm"), p.get("refl_mdBm")
            if isinstance(f, int) and isinstance(r, int):
                v = calculate_vswr(f / 1000.0, r / 1000.0)
                row["vswr"] = round(v, 4) if v is not None else ""
        try:
            self.logger.write(row)
        except OSError as exc:
            self.logging_failed = True
            self.request_rf_off()
            if self.generator_connected:
                self.generator_worker.set_output(False)
            QMessageBox.critical(self, "CSV logging failed", "RF shutdown requested. Logging error:\n" + str(exc))

    def toggle_generator_rf(self, _checked=False):

        self.doctor_rf_switch.blockSignals(True)
        self.doctor_rf_switch.setChecked(self.generator_rf_on is True)
        self.doctor_rf_switch.blockSignals(False)
        if self.generator_rf_on is True or self.pending_treatment_start or self.timer_deadline is not None:
            self.stop_treatment_timer()
            self.request_all_rf_off("RF_BOTTOM_OFF")
        else:
            self.notify_popup("START_REQUIRED", "Use START", "Set time, enable the PA, then press START to request generator RF ON. The bottom switch can turn RF OFF.")

    def preview_generator_slider(self, value):
        if self.generator_slider.isSliderDown():
            self.generator_value.setText(f"{value:+d} dBm · preview")

    def commit_generator_slider(self):
        self.set_generator_level(self.generator_slider.value())

    def change_generator_level(self, change):
        if self.generator_level is None:
            self.set_status("Generator level not yet verified")
            return
        self.set_generator_level(max(GENERATOR_UI_MIN, min(GENERATOR_UI_MAX,
            round(self.generator_level) + change)))

    def set_generator_level(self, level):
        if self.apc_enabled:
            self.set_status("Automatic power is on: change the target in watts, or switch to manual dBm")
            self.restore_generator_slider()
            return
        if self.fault_latched or self.session_ended or self.logging_failed:
            self.set_status("Power setting blocked: safety fault or session unavailable")
            return
        if self.demo:
            pass
        if not GENERATOR_UI_MIN <= level <= GENERATOR_UI_MAX:
            self.set_status("Requested setting outside slider range")
            self.restore_generator_slider()
            return

        if (not self.demo and getattr(self, "generator_rf_on", None) is not False
                and level > -30 and (self.generator_level is None or level > self.generator_level)):
            answer = QMessageBox.warning(
                self, "Confirm RF generator power",
                f"Request {level:+.0f} dBm?\n\nThe generator RF output may be ON. "
                "Verify your PA input limit, RF load and protection hardware first.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if answer != QMessageBox.Yes:
                self.restore_generator_slider()
                return
        if not self.generator_worker.set_power(level):
            self.set_status("Generator unavailable or request blocked")
            self.restore_generator_slider()
            return
        self.generator_value.setText(f"{level:+.0f} dBm · pending")

    # ── Automatic output power control ─────────────────────────────────────
    def apc_controlling(self):
        return self.apc_enabled and (self.timer_deadline is not None or self.pending_treatment_start
                                     or self.doctor_auto_start)

    def toggle_apc_mode(self, _checked=False):
        if self.timer_deadline is not None or self.pending_treatment_start or self.doctor_auto_start:
            self.set_status("Power mode can only be changed before START")
        else:
            self.apc_enabled = not self.apc_enabled
            self.apc.reset()
            if self.apc_enabled:
                self.preset_input_for_target()
            self.log_event("APC_MODE", comment="automatic W" if self.apc_enabled else "manual dBm")
            self.set_status("Automatic power: set the target in watts" if self.apc_enabled
                            else "Manual power: generator level in dBm")
        self.refresh_power_control_ui()
        self.refresh_doctor_display()

    def change_power_target(self, delta_w):
        if not self.apc_enabled or self.fault_latched or self.session_ended or self.logging_failed:
            return
        old = self.apc.target_w
        new = self.apc.set_target(old + delta_w)
        self.preset_input_for_target()
        reason = self.input_block_reason()
        if reason:
            self.set_status(reason, 6.0)
        if new != old:
            self.log_event("APC_TARGET", comment=f"{old:.1f} W -> {new:.1f} W")
            self.set_status(f"Output power target {new:.1f} W")
        elif new in (APC_TARGET_MIN_W, APC_TARGET_MAX_W):
            self.set_status(f"Target limit: {APC_TARGET_MIN_W:.0f}–{APC_TARGET_MAX_W:.0f} W")
        self.refresh_power_control_ui()
        self.refresh_doctor_display()

    def input_block_reason(self):
        """Why the synthesizer input is NOT being changed right now (None = it is)."""
        if not self.apc_enabled:
            return "Automatic power is OFF — switch on 'Auto W' on the engineer page"
        if self.fault_latched:
            return "Safety fault latched — press Reset warning, then set the power again"
        if self.session_ended or self.logging_failed:
            return "Session/logging unavailable — power changes blocked"
        if not self.generator_connected:
            return "Synthesizer not connected (LAN) — input not changed"
        if self.apc_controlling():
            if self.timer_deadline is None:
                return None                      # starting: handled at RF ON
            p = self.last_telemetry or {}
            if self.generator_rf_on is not True:
                return "Waiting for synthesizer RF ON readback"
            if p.get("conn") != 1 or p.get("valid") != 1:
                return "Waiting for valid STM32 telemetry"
            if p.get("rf_valid") != 1:
                return "STM32 reports no valid RF power reading (rf_valid≠1) — input held"
            if p.get("enable") != 0 or p.get("alarm_in") != 0:
                return "PA not reported enabled by STM32 — input held"
            return None
        level, _source = self.apc.input_for()
        if level is None:
            return (f"No calibration file found ({self.pa_cal.path}) — "
                    "the input will be found at START")
        if self.generator_rf_on is None:
            return "Synthesizer RF ON/OFF not confirmed yet — input not changed"
        if self.generator_rf_on is True:
            return "Synthesizer RF is ON outside a treatment — input not changed"
        return None

    def preset_input_for_target(self):
        """Before START, with generator RF confirmed OFF, set the generator to the
        input calculated for the target, so the dBm shown is the dBm that will be used."""
        if (not self.apc_enabled or self.apc_controlling() or self.generator_rf_on is not False
                or not self.generator_connected or self.fault_latched):
            return
        level, source = self.apc.input_for()
        if level is not None and (self.generator_level is None or abs(self.generator_level - level) >= 0.01):
            if self.generator_worker.set_power(level):
                self.log_event("APC_PRESET", comment=f"{self.apc.target_w:.1f} W -> {level:+.2f} dBm ({source})")

    def save_calibration_point(self, _checked=False):
        p = self.last_telemetry
        measured = self.regulated_watts(p) if p else None
        if (self.generator_rf_on is not True or self.generator_level is None or measured is None
                or measured <= 0 or p.get("rf_valid") != 1):
            self.set_status("Save point needs RF ON with a valid power reading")
            return
        self.pa_cal.add_point(self.generator_level, measured)
        self.log_event("PA_CAL_POINT", packet=p, comment=f"{self.generator_level:+.2f} dBm -> {measured:.3f} W")
        self.set_status(f"Calibration point saved: {self.generator_level:+.2f} dBm → {measured:.2f} W")
        self.refresh_power_control_ui()

    def clear_calibration(self, _checked=False):
        if QMessageBox.question(self, "Clear calibration",
                                "Delete all amplifier calibration points?",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            self.pa_cal.clear()
            self.apc.learned_gain_db = None
            self.log_event("PA_CAL_CLEARED")
            self.refresh_power_control_ui()

    def set_power_target(self, watts):
        self.change_power_target(float(watts) - self.apc.target_w)

    def on_target_slider(self, value):
        if self.doctor_target_slider.isSliderDown():
            self.doctor_set_display.setText(f"{value:.0f} W")   # preview while dragging
        else:
            self.set_power_target(value)                      # tap on the track

    def commit_target_slider(self):
        self.set_power_target(self.doctor_target_slider.value())

    def regulated_watts(self, p):
        forward = self.get_forward_watts(p)
        if forward is None or APC_REGULATE != "net":
            return forward
        raw = p.get("refl_mdBm")
        reflected = forward_dbm_to_watts(raw / 1000.0, True) if isinstance(raw, int) else None
        return None if reflected is None else max(0.0, forward - reflected)

    def run_power_control(self, p):
        """One controller step per telemetry packet, only while treatment RF is
        confirmed ON with a valid power measurement."""
        active = (self.apc_enabled and self.timer_deadline is not None and not self.fault_latched
                  and self.generator_rf_on is True and self.generator_level is not None
                  and p.get("conn") == 1 and p.get("valid") == 1 and p.get("rf_valid") == 1
                  and p.get("enable") == 0 and p.get("alarm_in") == 0)
        if not active:
            if not self.pending_treatment_start and self.apc.phase != "idle":
                self.apc.reset()
            return
        f, r = p.get("fwd_mdBm"), p.get("refl_mdBm")
        vswr = calculate_vswr(f / 1000.0, r / 1000.0) if isinstance(f, int) and isinstance(r, int) else None
        measured = self.regulated_watts(p)
        level, fault = self.apc.step(time.monotonic(), measured, self.generator_level, vswr)
        if self.apc.new_point is not None:
            g, w_out = self.apc.new_point
            self.apc.new_point = None
            self.log_event("PA_CAL_POINT_AUTO", packet=p, comment=f"{g:+.2f} dBm -> {w_out:.3f} W")
        if fault is not None:
            self.emergency_stop(fault[0], fault[1])
            return
        if level is not None:
            if self.generator_worker.set_power(level):
                self.log_event("APC_LEVEL", packet=p, comment=(
                    f"{self.generator_level:+.2f} -> {level:+.2f} dBm; measured "
                    f"{measured:.3f} W; target {self.apc.target_w:.1f} W"))
            else:
                self.emergency_stop("APC_COMMAND_REJECTED", "Automatic power level was rejected")

    def refresh_power_control_ui(self):
        if not hasattr(self, "doctor_apc_toggle"):
            return
        busy = self.timer_deadline is not None or self.pending_treatment_start or self.doctor_auto_start
        usable = not (self.fault_latched or self.session_ended or self.logging_failed)
        target = self.apc.target_w
        if self.apc_enabled:
            level, source = self.apc.input_for()
            if self.apc_controlling() and self.apc.phase != "idle":
                gen = f"{self.generator_level:+.2f} dBm" if self.generator_level is not None else "— dBm"
                status_line = f"{self.apc.status}  |  Input {gen}"
            elif level is not None:
                status_line = f"Input {level:+.2f} dBm → output {target:.1f} W  ({source})"
            else:
                status_line = f"Input for {target:.1f} W is found at START (no calibration yet)"
            hint = ("Input dBm is calculated from the amplifier calibration, "
                    "then trimmed so the measured output equals the target.")
        else:
            status_line = "Manual: the generator is set directly in dBm."
            hint = "Delivered watts are measured, not a calibrated setpoint."
        for toggle in (self.doctor_apc_toggle, self.engineer_apc_toggle):
            toggle.blockSignals(True)
            toggle.setChecked(self.apc_enabled)
            toggle.blockSignals(False)
            toggle.setEnabled(not busy)
        self.doctor_target_row.setVisible(self.apc_enabled)
        self.doctor_down.setVisible(self.apc_enabled)
        self.doctor_up.setVisible(self.apc_enabled)
        self.doctor_dbm_row.setVisible(not self.apc_enabled)
        for b in self.engineer_target_buttons:
            b.setEnabled(self.apc_enabled and usable)
        self.doctor_down.setEnabled(self.apc_enabled and usable and target > APC_TARGET_MIN_W)
        self.doctor_up.setEnabled(self.apc_enabled and usable and target < APC_TARGET_MAX_W)
        # Slider for quick setting before START; during treatment only the arrows,
        # so a single drag cannot make a large unintended change.
        self.doctor_target_slider.setEnabled(self.apc_enabled and usable and not busy)
        if not self.doctor_target_slider.isSliderDown():
            self.doctor_target_slider.blockSignals(True)
            self.doctor_target_slider.setValue(int(round(target)))
            self.doctor_target_slider.blockSignals(False)
        problem = self.apc_enabled and self.apc_controlling() and self.apc.phase == "limited"
        reason = self.input_block_reason() if self.apc_enabled else None
        if problem:
            self.doctor_apc_status.setText(self.apc.status)
        elif reason:
            self.doctor_apc_status.setText(reason)
        else:
            self.doctor_apc_status.setText("" if self.apc_enabled else "Manual: generator set in dBm")
        self.engineer_target_value.setText(f"{target:.1f} W" if self.apc_enabled else "manual")
        self.engineer_apc_status.setText(status_line)
        self.engineer_cal_label.setText("Calibration: " + self.pa_cal.summary() + f"  ({self.pa_cal.path})")
        self.engineer_cal_save.setEnabled(self.generator_rf_on is True)
        if self.apc_enabled:
            self.doctor_measured_note.setText(f"Measured at the amplifier output  ·  target {target:.1f} W")
        else:
            self.doctor_measured_note.setText("Measured at the amplifier output")
        self.generator_slider.setEnabled(self.generator_connected and not self.apc_enabled)
        for b in self.engineer_dbm_buttons:
            b.setEnabled(not self.apc_enabled)

    def restore_generator_slider(self):
        if self.generator_level is not None:
            self.generator_slider.blockSignals(True)
            self.generator_slider.setValue(round(self.generator_level))
            self.generator_slider.blockSignals(False)
            self.generator_value.setText(f"{self.generator_level:+.1f} dBm")

    def update_generator_state(self, connected, message):
        self.generator_connected = connected
        if not connected and self.doctor_auto_start:
            self.cancel_doctor_auto_start("RF generator connection lost")
        self.update_flow_stage()
        self.generator_connection.setText(("●  " if connected else "○  ") + "LAN: " + message)
        self.generator_slider.setEnabled(connected and not self.apc_enabled)
        self.doctor_rf_switch.setEnabled(connected and self.generator_rf_on is not None)
        self.doctor_rf_switch.setEnabled(connected and self.generator_rf_on is not None)
        if not connected:
            self.doctor_rf_switch.setEnabled(False)
            self.doctor_rf_switch.setEnabled(False)
            self.doctor_rf_switch.setChecked(False)
            self.doctor_rf_switch.setText("OFF    ●    ON")
            self.doctor_rf_status.setText("Unknown")
            self.doctor_rf_switch.setChecked(False)
            self.doctor_rf_switch.setText("OFF   ●   ON")
            self.generator_rf_on = None
            self.generator_level = None
            self.generator_value.setText("— dBm")
            self.generator_output.setText("Generator RF: Unverified")

    def update_generator_level(self, value):
        self.generator_level = value
        self.apc.on_level_readback(value, time.monotonic())
        self.generator_value.setText(f"{value:+.2f} dBm")
        self.doctor_power_setpoint.setText(f"Generator: {value:+.1f} dBm")
        self.generator_slider.blockSignals(True)

        self.generator_slider.setValue(max(GENERATOR_UI_MIN, min(GENERATOR_UI_MAX, round(value))))
        self.generator_slider.blockSignals(False)
        self.refresh_doctor_display()

    def update_generator_output(self, on):
        self.generator_rf_on = bool(on)
        self.doctor_rf_switch.blockSignals(True)
        self.doctor_rf_switch.setChecked(bool(on))
        self.doctor_rf_switch.blockSignals(False)
        self.doctor_rf_switch.setEnabled(self.generator_connected)
        self.doctor_rf_status.setText("ON · generator readback" if on else "OFF · generator readback")
        self.generator_output.setText("RF: ON (readback)" if on else "RF: OFF (readback)")
        self.generator_output.setStyleSheet(tok("color:@ok;" if on else "color:@danger;"))
        if self.pending_treatment_start and on:
            self.pending_treatment_start = False
            self.timer_deadline = time.monotonic() + self.timer_setting_sec
            self.timer_last_shown = -1
            self.timer_elapsed = False
            self.update_flow_stage()
            self.log_event("TREATMENT_STARTED_CONFIRMED_RF_READBACK")
            self.set_status("Treatment active: generator RF ON readback received; monitoring active")
        elif self.timer_deadline is not None and not on:

            self.emergency_stop("RF_LOST", "Generator RF output switched OFF during treatment")
        if self.last_telemetry:
            power = self.get_forward_watts()
            self.doctor_power_value.setText(f"{power:.2f} W" if power is not None else "— W")
        if not on:
            self.preset_input_for_target()
        self.refresh_doctor_display()

    def generator_error(self, error):
        self.set_status("Generator: " + error[:95], 6.0)

    def build_diagnostics_page(self):
        outer = QVBoxLayout(
            self.diagnostics_page
        )

        outer.setContentsMargins(
            15, 10, 15, 10
        )
        outer.setSpacing(8)

        heading_row = QHBoxLayout()

        heading_box = QVBoxLayout()
        heading_box.setSpacing(1)

        title = QLabel("Diagnostics")
        title.setObjectName("PageTitle")

        subtitle = QLabel(
            "Pin states, analog channels and links"
        )
        subtitle.setObjectName("SubTitle")

        heading_box.addWidget(title)
        heading_box.addWidget(subtitle)

        back_button = QPushButton(
            "‹  Page 2"
        )
        back_button.setObjectName("NavButton")
        back_button.clicked.connect(
            lambda: self.pages.setCurrentIndex(2)
        )

        heading_row.addLayout(heading_box)
        heading_row.addStretch()
        heading_row.addWidget(back_button)
        details_exit_button = QPushButton("Exit")
        details_exit_button.setObjectName("ExitButton")
        details_exit_button.clicked.connect(self.request_exit)
        details_logout_button = QPushButton("LOGOUT")
        details_logout_button.setObjectName("NavButton")
        details_logout_button.clicked.connect(self.logout_to_login)
        heading_row.addWidget(details_logout_button)
        heading_row.addWidget(details_exit_button)

        outer.addLayout(heading_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        content = QWidget()
        scroll.setWidget(content)

        self.diag_layout = QVBoxLayout(content)
        self.diag_layout.setSpacing(6)
        self.diag_layout.setContentsMargins(
            0, 0, 8, 0
        )

        self.diag_rows = {}

        self.add_section("GPIO / Signal Status")

        gpio = [
            ("enable", "PA_ENABLE",
             "J2-5 / STM32 PC6"),
            ("reset", "RST_ALARM",
             "J2-3 / STM32 PC7"),
            ("alarm_out", "ALARM_OUT",
             "J2-11 / STM32 PC8"),
            ("alarm_in", "ALARM_IN",
             "J2-7 / STM32 PC9"),
            ("trigger", "TRIG_IN",
             "J2-1 / STM32 PB10"),
            ("scl", "I²C SCL",
             "J2-13 / STM32 PB8"),
            ("sda", "I²C SDA",
             "J2-15 / STM32 PB9"),
        ]

        for key, label, pin in gpio:
            self.add_diag_row(key, label, pin)

        self.add_section("Analog Measurements")

        analog = [
            ("fwd_adc", "Forward Detector",
             "J2-4 / STM32 PA0"),
            ("refl_adc", "Reflected Detector",
             "J2-2 / STM32 PA3"),
            ("current_adc", "Current Sense",
             "J2-6 / STM32 PC0"),
            ("temp_adc", "Temperature Sense",
             "J2-8 / STM32 PF11"),
        ]

        for key, label, pin in analog:
            self.add_diag_row(key, label, pin)

        self.add_section("Communication")

        communication = [
            ("i2c", "I²C Link", "PB8 / PB9"),
            ("adc", "ADC Sampling", "ADC1 + DMA"),
            ("uart", "UART Link", "UART4"),
            ("last_update", "Last Update", "STM32"),
            ("uptime", "STM32 Uptime", "CM7"),
        ]

        for key, label, pin in communication:
            self.add_diag_row(key, label, pin)

        self.add_section("Additional System Data")

        extras = [
            ("alarm_code", "Internal Alarm Code",
             "I²C Reg 119"),
            ("fwd_digital", "Digital Forward Power",
             "I²C Reg 245"),
            ("refl_digital", "Digital Reflected Power",
             "I²C Reg 247"),
            ("supply", "Supply Voltage",
             "I²C Reg 251"),
            ("supply_current", "Supply Current",
             "I²C Reg 249"),
            ("pa_temperature", "PA Temperature",
             "I²C Reg 253")
        ]

        for key, label, pin in extras:
            self.add_diag_row(key, label, pin)

        self.diag_layout.addStretch()

        outer.addWidget(scroll)
        outer.addWidget(self.navigation_button("‹  Back to Page 2", 2))
        legal = QLabel("© SAMEER · MeitY · Government of India | Engineering prototype · IEC design references, not certified")
        legal.setObjectName("StandardsFooter")
        legal.setAlignment(Qt.AlignCenter)
        outer.addWidget(legal)

    def add_section(self, name):
        label = QLabel(name)
        label.setObjectName("SectionTitle")

        self.diag_layout.addSpacing(5)
        self.diag_layout.addWidget(label)

    def add_diag_row(self, key, name, pin):
        row = DiagnosticRow(name, pin)
        self.diag_rows[key] = row

        self.diag_layout.addWidget(row)

    GLOBAL_QSS = """
        QMainWindow, QWidget { background: @bg; color: @ink; font-family: "@font"; font-size: 12px; }
        QLabel, QWidget#Transparent { background: transparent; }

        QLabel#PageTitle { font-size: 20px; font-weight: 800; color: @ink; }
        QLabel#HeroTitle { font-size: 23px; font-weight: 800; color: @ink; }
        QLabel#SubTitle { font-size: 12px; color: @ink2; }
        QLabel#FieldLabel { font-size: 12px; color: @ink2; font-weight: bold; }
        QLabel#BrandMark { background: @accent; color: @accent_ink; border-radius: 18px; font-size: 19px; font-weight: 800; }
        QLabel#SectionTitle { font-size: 14px; font-weight: 800; color: @accent; padding: 10px 2px 4px 2px; }

        QLabel#RFBadge { background: @accent_soft; color: @ink; border: none; border-radius: 13px;
            padding: 5px 12px; font-weight: bold; }

        QFrame#ControlCard { background: @surface; border: none; border-radius: 18px; }
        QFrame#MetricCard { background: @surface; border: none; border-radius: 18px; }
        QLabel#MetricTitle { color: @ink2; font-size: 12px; font-weight: bold; }
        QLabel#MetricValue { color: @ink; font-size: 27px; font-weight: 800; }
        QLabel#MetricUnit { color: @accent; font-size: 13px; font-weight: bold; }
        QLabel#MetricSource { color: @ink2; font-size: 11px; }

        QFrame#DiagnosticRow { background: @surface; border: none; border-radius: 14px; }
        QLabel#DiagName { font-weight: bold; font-size: 13px; color: @ink; }
        QLabel#DiagPin { font-size: 12px; color: @ink2; }
        QLabel#DiagValue { font-weight: 800; font-size: 13px; color: @ink; }

        QPushButton { background: @surface; border: @bw solid @line; border-radius: 14px; color: @ink;
            padding: 8px 14px; font-weight: bold; }
        QPushButton:pressed { background: @surface2; }
        QPushButton:disabled { background: transparent; color: @ink3; border: @bw dashed @line; }

        QPushButton#RoleLoginButton { background: @accent; color: @accent_ink; border: none; border-radius: 24px;
            font-size: 19px; font-weight: 800; text-align: left; padding: 16px 24px; }
        QPushButton#RoleLoginButtonAlt { background: @surface; color: @ink; border: none; border-radius: 24px;
            font-size: 19px; font-weight: 800; text-align: left; padding: 16px 24px; }
        QPushButton#RoleLoginButton:pressed, QPushButton#RoleLoginButtonAlt:pressed { background: @surface2; color: @ink; }

        QPushButton#ExitButton { background: @danger_soft; color: @danger; border: none; border-radius: 14px;
            font-size: 11px; padding: 7px 16px; }
        QPushButton#ExitButton:pressed { background: @danger; color: @danger_ink; }
        QPushButton#OffButton { background: @danger; color: @danger_ink; border: none; border-radius: 16px; font-size: 14px; font-weight: 800; }
        QPushButton#OffButton:pressed { background: @ink; }
        QPushButton#NavButton { background: @surface; color: @ink; border: @bw solid @line; border-radius: 14px;
            font-size: 11px; padding: 7px 14px; }
        QPushButton#NavButton:pressed { background: @surface2; }
        QPushButton#ThemeButton { background: @surface; color: @ink2; border: @bw solid @line; border-radius: 14px;
            font-size: 11px; font-weight: bold; padding: 6px 12px; }
        QPushButton#ApcToggle { background: @surface; color: @ink2; border: @bw solid @line; border-radius: 14px;
            font-size: 12px; padding: 6px 12px; }
        QPushButton#ApcToggle:checked { background: @accent; color: @accent_ink; border-color: @accent; }
        QPushButton#ApcToggle:disabled { background: transparent; color: @ink3; border: @bw dashed @line; }
        QPushButton#Segment { background: @surface; color: @ink2; border: @bw solid @line; border-radius: 20px;
            padding: 8px 10px; min-width: 76px; font-weight: bold; }
        QPushButton#Segment:checked { background: @accent; color: @accent_ink; border-color: @accent; font-weight: 800; }

        QLabel#FooterMessage { color: @ink2; font-size: 11px; }
        QLabel#StandardsFooter { font-size: 9px; color: @ink3; padding: 2px; }
        QLabel#FlowStep { font-size: 10px; padding: 4px; color: @ink3; background: @surface; border: none; border-radius: 13px; }
        QLabel#WarningBanner { background: @surface; color: @ink; border: 2px solid @line; border-radius: 14px; padding: 6px 14px; font-weight: bold; }
        QPushButton#StartAction { background: @ok; border: none; color: @ok_ink; border-radius: 16px; font-weight: 800; }
        QPushButton#StopAction { background: @danger; border: none; color: @danger_ink; border-radius: 16px; font-weight: 800; }
        QPushButton#StartAction:disabled { background: @ok_soft; color: @ink3; border: none; }
        QPushButton#StopAction:disabled { background: @danger_soft; color: @ink3; border: none; }
        QLabel#DoctorHeading { color: @ink2; font-weight: bold; }
        QLabel#FocusValue { color: @ink; font-size: 23px; font-weight: 800; }
        QLabel#GeneratorValue { background: @surface; border: 2px solid @accent; border-radius: 16px; padding: 5px;
            color: @ink; font-size: 22px; font-weight: 800; }

        QMessageBox { background: @surface; }
        QMessageBox QLabel { font-size: 14px; color: @ink; }
        QMessageBox QPushButton { min-width: 125px; min-height: 38px; background: @accent; color: @accent_ink;
            border: none; border-radius: 16px; }

        QPushButton#PumpSwitch, QPushButton#GeneratorRFSwitch { background: @surface2; color: @ink2;
            border: none; border-radius: 20px; font-size: 13px; font-weight: bold; padding: 6px 10px; }
        QPushButton#PumpSwitch:checked, QPushButton#GeneratorRFSwitch:checked { background: @ok; color: @ok_ink; }
        QPushButton#PumpSwitch:disabled, QPushButton#GeneratorRFSwitch:disabled { background: transparent; color: @ink3;
            border: @bw dashed @line; }
        QLabel#PumpReported { color: @ink2; font-size: 12px; font-weight: bold; }
        QLabel#GeneratorRFStatus { font-size: 11px; font-weight: bold; }

        QComboBox { background: @surface; color: @ink; border: @bw solid @line; border-radius: 16px; padding: 6px 14px; font-size: 14px; }
        QComboBox::drop-down { border: none; width: 30px; }
        QComboBox QAbstractItemView { background: @surface; color: @ink; border: @bw solid @line;
            selection-background-color: @accent_soft; selection-color: @ink; outline: 0; }

        QSlider::groove:horizontal { height: 10px; border-radius: 5px; background: @surface2; }
        QSlider::sub-page:horizontal { background: @accent; border-radius: 5px; }
        QSlider::handle:horizontal { background: @surface; border: 4px solid @accent; width: 20px; margin: -9px 0; border-radius: 14px; }
        QSlider::sub-page:horizontal:disabled { background: @line; }
        QSlider::handle:horizontal:disabled { border-color: @line; }

        QScrollArea { background: transparent; border: none; }
        QScrollBar:vertical { background: transparent; width: 8px; }
        QScrollBar::handle:vertical { background: @line; border-radius: 4px; min-height: 40px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
        QToolTip { background: @surface; color: @ink; border: @bw solid @line; padding: 4px; }
    """

    def apply_styles(self):
        self.setStyleSheet(tok(self.GLOBAL_QSS))

    SHADOWED = ("ControlCard", "MetricCard", "DrPanel", "RoleLoginButton", "RoleLoginButtonAlt")

    def apply_shadows(self):
        """Soft lift under the main surfaces. Colour follows the display mode."""
        r, g, b, a = (int(v) for v in T["shadow"].split(","))
        if not hasattr(self, "_shadows"):
            self._shadows = []
            for widget in self.findChildren(QWidget):
                if widget.objectName() in self.SHADOWED:
                    effect = QGraphicsDropShadowEffect(widget)
                    effect.setBlurRadius(26)
                    effect.setOffset(0, 5)
                    widget.setGraphicsEffect(effect)
                    self._shadows.append(effect)
        for effect in self._shadows:
            effect.setColor(QColor(r, g, b, a))
            effect.setEnabled(a > 0)

    def cycle_theme(self, _checked=False):
        current = T.get("_name", DEFAULT_THEME)
        index = THEME_ORDER.index(current) if current in THEME_ORDER else 0
        self.set_theme(THEME_ORDER[(index + 1) % len(THEME_ORDER)])

    def set_theme(self, name, save=True):
        """Switch display mode. Purely visual: no treatment, telemetry or
        output state is read for anything other than choosing colours."""
        if name not in THEMES:
            name = DEFAULT_THEME
        T.clear()
        T.update(THEMES[name])
        T["_name"] = name
        T["font"] = UI_FONT
        if save:
            try:
                QSettings("SAMEER", "MWA").setValue("display_mode_v2", name)
            except Exception:
                pass
        self.apply_styles()
        self.doctor_page.setStyleSheet(tok(self.DOCTOR_QSS))
        self.apply_shadows()
        self.apply_role_layout()
        for button in self.theme_buttons:
            button.setText("◐  " + THEME_SHORT.get(name, name))
        for seg_name, seg in self.theme_segments.items():
            seg.setChecked(seg_name == name)
        if hasattr(self, "generator_connected"):
            self.update_flow_stage()
        if hasattr(self, "banner_flasher"):
            self.banner_flasher.refresh()
            self.notice_flasher.refresh()
        if self.generator_output.styleSheet():
            self.generator_output.setStyleSheet(tok("color:@ok;" if getattr(self, "generator_rf_on", None) else "color:@danger;"))
        self.power_switch.update()
        self.doctor_timer_dial.update()
        if self.role == "doctor":
            self.refresh_doctor_display()

    def request_exit(self):
        if self.exit_pending:
            return
        self.exit_pending = True
        self.doctor_auto_start = False
        self.doctor_auto_deadline = 0.0
        self.timer_deadline = None
        self.log_event("EXIT_REQUEST")
        self.exit_button.setEnabled(False)
        self.login_exit_button.setEnabled(False)
        self.request_rf_off()
        self.set_status("Exiting: RF OFF requested; hardware state must be verified", 3.0)

        if self.generator_connected:
            self.generator_worker.set_output(False)

        QTimer.singleShot(1100, self.close)

    def set_status(self, message, seconds=4.0):
        self.status_message_until = time.monotonic() + seconds
        self.message.setText(message)

    def send_heartbeat(self):
        if self.commanded_on and self.serial_connected and not self.demo:
            self.worker.send_command("HEARTBEAT")

    def request_rf_on(self):

        # >>> WATCHDOG: no PA ON while a HIGH watchdog alarm cause is present
        if WATCHDOG_BLOCKS_RF_ON:
            blockers = self.watchdog.rf_on_blockers()
            if blockers:
                self.power_switch.set_pending(False)
                text = ", ".join(d.text for d in blockers)
                self.set_status("PA ON blocked by watchdog: " + text)
                self._popup_keys.discard("WATCHDOG_BLOCK")
                self.notify_popup("WATCHDOG_BLOCK", "Watchdog alarm",
                                  "PA cannot be enabled: " + text + ".", True)
                self.log_event("PA_ON_BLOCKED_BY_WATCHDOG", comment=text)
                return
        # <<< WATCHDOG
        if self.timer_setting_sec == 0:
            self.power_switch.set_pending(False)
            self.set_status("TIMER NOT SET — cannot enable PA")
            self.notify_popup("TIMER_NOT_SET", "Timer not set", "Set treatment time above 00:00 using +10 s, then PA can be requested.")
            return
        if (self.timer_deadline is not None or self.pending_treatment_start
                or self.timer_elapsed or self.cycle_outcome
                or self.logging_failed or self.fault_latched):
            self.power_switch.set_pending(False)
            self.set_status("PA ON blocked in current treatment state")
            return
        if self.pump_reported_on is not True or not self.last_good_sensor:
            self.power_switch.set_pending(False)
            self.notify_popup("PA_COOLING_NOT_VERIFIED", "Cooling not verified", ("Pump confirmation (SIMULATED) and SO24 reading required; coolant flow must be verified by the operator." if PUMP_SIMULATED else "Pump command and SO24 reading must be valid; independent flow verification remains required."))
            return
        if not self.demo and not self.serial_connected:
            self.power_switch.set_pending(False)
            self.set_status("Cannot request ON: UART disconnected")
            return
        if not self.worker.send_command("REQUEST_ON"):
            self.power_switch.set_pending(False)
            self.set_status("ON request not supported")
            return
        self.power_switch.set_pending(True)
        self.pending_on_deadline = time.monotonic() + 3.0
        self.log_event("PA_ON_REQUEST")
        self.control_status.setText("PA enable requested — waiting for STM32 ACK")
        self.set_status("REQUEST_ON sent — awaiting STM32 decision")

    def request_rf_off(self):

        if self.timer_deadline is not None or self.pending_treatment_start:
            self.timer_deadline = None
            self.pending_treatment_start = False
            self.timer_elapsed = False
            if not self.fault_latched and self.cycle_outcome is None:
                self.cycle_outcome = "STOPPED"
                self.log_event("TREATMENT_STOPPED", comment="PA OFF requested during active cycle")
                self.notify_popup("CYCLE_STOPPED", "TREATMENT STOPPED",
                                  "PA OFF requested during the operation. RF/PA shutdown must be verified.", True)
            self.timer_minus.setEnabled(False)
            self.timer_plus.setEnabled(False)
            self.start_button.setEnabled(False)
            self.timer_value.setText("00:00")
            if self.generator_connected:
                self.generator_worker.set_output(False)
            self.update_flow_stage()
        self.commanded_on = False
        self.power_switch.set_pending(False)
        self.pending_on_deadline = 0.0
        self.control_status.setText("OFF requested — awaiting confirmation")
        if not self.demo and not self.serial_connected:
            self.set_status("UART disconnected: local OFF cannot be confirmed")
            return
        if self.worker.send_command("DISABLE"):
            self.log_event("PA_OFF_REQUEST")
            self.set_status("DISABLE sent — awaiting STM32 ACK")
        else:
            self.set_status("DISABLE command could not be queued")

    def refresh_pump_control(self):
        if PUMP_SIMULATED:
            busy = (self.timer_deadline is not None or self.pending_treatment_start
                    or getattr(self, "doctor_auto_start", False))
            self.pump_switch.blockSignals(True)
            self.pump_switch.setChecked(self.pump_sim_ack)
            self.pump_switch.blockSignals(False)
            self.pump_switch.setEnabled(not busy)
            self.pump_value.setText("CONFIRMED · SIMULATED" if self.pump_sim_ack
                                    else "NOT CONFIRMED · SIMULATED")
            return
        fresh = (self.serial_connected and self.last_packet_time > 0
                 and time.monotonic() - self.last_packet_time <= TELEMETRY_TIMEOUT)
        known = fresh and self.pump_reported_on is not None
        self.pump_switch.blockSignals(True)
        self.pump_switch.setChecked(bool(self.pump_reported_on) if known else False)
        self.pump_switch.blockSignals(False)
        self.pump_switch.setEnabled(bool(known and self.pump_pending is None))
        if self.pump_pending is not None:
            self.pump_value.setText("Switching…")
        elif not known:
            self.pump_value.setText("State unknown")
        else:
            self.pump_value.setText("ON · reported" if self.pump_reported_on else "OFF · reported")

    def toggle_pump(self, _checked=False):
        if PUMP_SIMULATED:
            if (self.timer_deadline is not None or self.pending_treatment_start
                    or self.doctor_auto_start):
                self.set_status("Pump confirmation locked during treatment")
            else:
                self.pump_sim_ack = not self.pump_sim_ack
                self.pump_reported_on = self.pump_sim_ack
                self.log_event("MANUAL_PUMP_ACK", comment=(
                    "SIMULATED ONLY; pump not controlled or monitored by STM32"
                    if self.pump_sim_ack else "Manual pump confirmation cleared"))
                self.set_status("Pump confirmed running (SIMULATED)" if self.pump_sim_ack
                                else "Pump confirmation cleared")
            self.refresh_pump_control()
            self.update_flow_stage()
            self.refresh_doctor_display()
            return

        self.refresh_pump_control()
        if not self.serial_connected or self.pump_reported_on is None:
            self.set_status("Pump control unavailable: no live STM32 state")
            return
        if self.pump_pending is not None:
            return
        requested = not self.pump_reported_on
        command = "PUMP_ON" if requested else "PUMP_OFF"
        if not self.worker.send_command(command):
            self.set_status("Pump command not supported")
            return
        self.log_event("PUMP_ON_REQUEST" if requested else "PUMP_OFF_REQUEST")
        self.pump_pending = requested
        self.pump_pending_deadline = time.monotonic() + 3.5
        self.refresh_pump_control()
        self.set_status(command + " sent — waiting for STM32 telemetry")
        self.refresh_doctor_display()

    def show_acknowledgement(self, message):
        if message.startswith("ACK,PUMP_"):

            if any(token in message.upper() for token in ("DENIED", "REJECT", "ERROR", "LOCKED")):
                self.pump_pending = None
                self.pump_pending_deadline = 0.0
            self.refresh_pump_control()
            self.set_status("STM32: " + message, 6.0)
            return
        if message.startswith("ACK,REQUEST_ON,ENABLED"):
            self.commanded_on = True
            self.power_switch.set_pending(True)
            self.pending_on_deadline = 0.0
            self.control_status.setText("Amplifier enable commanded — RF power unverified")
            self.set_status("STM32 accepted ON; monitor RF output separately", 6.0)
            self.check_doctor_auto_start()
        elif message.startswith("ACK,REQUEST_ON,DENIED"):
            self.notify_popup("PA_DENIED", "PA request denied", "STM32 rejected PA enable. Inspect interlocks and monitoring.")
            self.commanded_on = False
            self.power_switch.set_pending(False)
            self.pending_on_deadline = 0.0
            self.control_status.setText("ON denied — RF remains disabled")
            self.set_status("STM32 denied ON: interlocks not verified", 6.0)
            self.cancel_doctor_auto_start("STM32 denied PA enable")
        elif message.startswith("EVENT,RF_OFF,"):
            self.commanded_on = False
            self.power_switch.set_pending(False)
            self.pending_on_deadline = 0.0
            self.control_status.setText("STM32 reports RF OFF event")
            self.set_status("STM32: " + message, 6.0)
        elif message.startswith("ACK,DISABLE,"):
            self.commanded_on = False
            self.power_switch.set_pending(False)
            self.pending_on_deadline = 0.0
            self.control_status.setText("OFF requested — hardware not verified")
            self.set_status("STM32: " + message, 6.0)
        else:

            self.power_switch.set_pending(False)
            self.pending_on_deadline = 0.0
            self.set_status("STM32: " + message, 6.0)

    def update_connection(self, connected):
        self.serial_connected = connected
        self.update_flow_stage()
        if not connected:
            self.pump_reported_on = self.pump_sim_ack if PUMP_SIMULATED else None
            self.pump_pending = None
            self.pump_pending_deadline = 0.0
        self.refresh_pump_control()
        self.refresh_doctor_display()
        if connected:
            self.cards["uart"].set_reading("Connected")
        else:
            self.cards["uart"].set_reading("Disconnected")
            self.cards["i2c"].set_reading("Unknown")

        if not connected:
            self.commanded_on = False
            self.power_switch.set_pending(False)
            self.pending_on_deadline = 0.0
            self.i2c_status.setText(
                "UART: Disconnected"
            )
            self.invalidate_telemetry()
            self.cards["uart"].set_reading("Disconnected")
            self.cards["i2c"].set_reading("Unknown")

    def show_error(self, message):
        self.commanded_on = False
        self.power_switch.set_pending(False)
        self.pending_on_deadline = 0.0
        self.cards["uart"].set_reading("Error")
        self.set_status("Serial error: " + message[:75], 6.0)

    def update_telemetry(self, p):
        self.last_packet_time = time.monotonic()
        self.last_telemetry = dict(p)
        self.update_flow_stage()
        self.last_good_sensor = p.get("so24_valid") == 1
        watts = self.get_forward_watts(p)
        self.doctor_power_value.setText(f"{watts:.2f} W" if watts is not None else "— W")
        self.log_event("TELEMETRY", packet=p)
        if self.timer_deadline is not None:
            fault_key, fault_message = self.active_fault(p)
            if fault_key is not None:
                self.emergency_stop(fault_key, fault_message)
            elif isinstance(p.get("temp_mC"), int) and p["temp_mC"] >= 55000:
                self.show_warning("PA_TEMP_55", "WARNING: PA temperature reached 55°C")
            elif isinstance(p.get("so24_temp_mC"), int) and p["so24_temp_mC"] >= 52000:
                self.show_warning("SO24_TEMP_52", "WARNING: SO24 temperature reached 52°C")
            elif "pump_rpm" in p and (not isinstance(p["pump_rpm"], int) or p["pump_rpm"] <= 0):
                self.show_warning("PUMP_SPEED", "INVALID PUMP SPEED — inspect pump")
            elif "pump_rpm" not in p and self.warning_key != "NO_PUMP_SPEED_FEEDBACK":
                self.show_warning("NO_PUMP_SPEED_FEEDBACK", "Pump SIMULATED — coolant flow NOT monitored"
                                  if PUMP_SIMULATED else "Pump speed / coolant flow NOT monitored")
            elif self.warning_key not in (None, "NO_PUMP_SPEED_FEEDBACK"):
                self.warning_key = None
                self.warning_banner.setText("Treatment active — no current measured warning; verify coolant flow")
                self.banner_flasher.set_priority(None)

        so24_mC = p.get("so24_temp_mC")
        if (p.get("so24_valid") == 1 and isinstance(so24_mC, int)
                and -40000 <= so24_mC <= 150000):
            self.ntc_value.setText(f"{so24_mC / 1000.0:.2f} °C")
            self.ntc_status.setText("PF6 / ADC3 · Sensor valid")
        else:
            self.ntc_value.setText("— °C")
            self.ntc_status.setText("SO24 reading unavailable / invalid")

        pump = p.get("pump_on")
        if PUMP_SIMULATED:
            self.pump_reported_on = self.pump_sim_ack
            self.pump_pending = None
            self.pump_pending_deadline = 0.0
        elif pump in (0, 1):
            self.pump_reported_on = bool(pump)
            if self.pump_pending is not None and self.pump_reported_on == self.pump_pending:
                self.pump_pending = None
                self.pump_pending_deadline = 0.0
        else:
            self.pump_reported_on = None
        self.refresh_pump_control()
        self.check_doctor_auto_start()
        self.run_power_control(p)
        self.refresh_doctor_display()

        digital_valid = (
            p.get("conn") == 1
            and p.get("valid") == 1
        )

        analog_valid = (
            p.get("adc_valid") == 1
        )

        def scaled(key):
            if key not in p:
                return None
            return p[key] / 1000.0

        def measurement(key, digits=2):
            val = scaled(key)
            if val is None:
                return "—"
            return f"{val:.{digits}f}"

        mapping = {
            "forward": "fwd_mdBm",
            "reflected": "refl_mdBm",
            "temperature": "temp_mC",
            "current": "current_mA",
            "voltage": "voltage_mV"
        }

        def power_plausible(key):

            if p.get("rf_valid") == 0:
                return False
            val = scaled(key)
            return val is not None and -120.0 <= val <= 60.0

        for card_name, data_key in mapping.items():
            plausible = (card_name not in ("forward", "reflected")
                         or power_plausible(data_key))
            if digital_valid and plausible:
                self.cards[card_name].set_reading(measurement(data_key))
            else:
                self.cards[card_name].clear()

        if digital_valid:
            alarm = p.get("alarm")
            if alarm is not None:
                self.cards["alarm"].set_reading(
                    f"0x{alarm:04X}"
                )
            else:
                self.cards["alarm"].clear()
        else:
            self.cards["alarm"].clear()

        self.i2c_status.setText(
            "I²C: Connected"
            if p.get("conn") == 1
            else "I²C: Disconnected"
        )

        self.cards["i2c"].set_reading(
            "Connected" if p.get("conn") == 1 else "Disconnected"
        )
        self.cards["uart"].set_reading("Receiving")

        if digital_valid and p.get("rf_valid") == 1 and p.get("enable") == 0 and p.get("alarm_in") == 0:
            pf = scaled("fwd_mdBm")
            pr = scaled("refl_mdBm")
            vswr = calculate_vswr(pf, pr)
        else:
            vswr = None
        self.cards["vswr"].set_reading(
            f"{vswr:.3f}" if vswr is not None else "N/A"
        )

        self.adc_status.setText(
            "ADC: Active"
            if analog_valid
            else "ADC: Not verified"
        )

        gpio_map = {
            "enable": "enable",
            "reset": "reset",
            "alarm_in": "alarm_in"
        }

        for gui_key, packet_key in gpio_map.items():
            level = p.get(packet_key)
            self.diag_rows[gui_key].set_value(
                "HIGH" if level == 1
                else "LOW" if level == 0
                else "—"
            )

        alarm_out = p.get("alarm_out")
        self.diag_rows["alarm_out"].set_value(
            "ALARM" if alarm_out == 1
            else "LOW" if alarm_out == 0
            else "—"
        )

        self.diag_rows["trigger"].set_value(
            "Not monitored"
        )

        bus_status = (
            "Link ACK" if p.get("conn") == 1
            else "Unavailable"
        )
        self.diag_rows["scl"].set_value(
            bus_status
        )
        self.diag_rows["sda"].set_value(
            bus_status
        )

        analog_map = {
            "fwd_adc": "fwd_mV",
            "refl_adc": "refl_mV",
            "current_adc": "isense_mV",
            "temp_adc": "tsense_mV"
        }

        for gui_key, packet_key in analog_map.items():
            if analog_valid:
                self.diag_rows[gui_key].set_value(
                    measurement(packet_key, 3) + " V"
                )
            else:
                self.diag_rows[gui_key].set_value("—")

        self.diag_rows["i2c"].set_value(
            "Connected"
            if p.get("conn") == 1
            else "Unavailable"
        )

        self.diag_rows["adc"].set_value(
            "Samples received"
            if analog_valid
            else "Unavailable"
        )

        self.diag_rows["uart"].set_value(
            "Receiving telemetry"
        )

        self.diag_rows["last_update"].set_value(
            datetime.now().strftime("%H:%M:%S")
        )

        ms = p.get("ms")

        self.diag_rows["uptime"].set_value(
            f"{ms / 1000:.1f} s"
            if ms is not None else "—"
        )

        if digital_valid:
            alarm = p.get("alarm")

            self.diag_rows["alarm_code"].set_value(
                f"0x{alarm:04X}"
                if alarm is not None else "—"
            )

            extra = {
                "fwd_digital": ("fwd_mdBm", "dBm"),
                "refl_digital": ("refl_mdBm", "dBm"),
                "supply": ("voltage_mV", "V"),
                "supply_current": ("current_mA", "A"),
                "pa_temperature": ("temp_mC", "°C")
            }

            for gui_key, (packet_key, unit) in extra.items():
                val = scaled(packet_key)
                if gui_key in ("fwd_digital", "refl_digital"):
                    if not power_plausible(packet_key):
                        val = None
                self.diag_rows[gui_key].set_value(
                    f"{val:.2f} {unit}" if val is not None else "—"
                )
        else:
            for key in (
                "alarm_code", "fwd_digital", "refl_digital",
                "supply", "supply_current", "pa_temperature"
            ):
                self.diag_rows[key].set_value("—")

        if (
            p.get("enable") == 1
            and p.get("alarm_in") == 1
        ):
            self.rf_badge.setText(
                "DISABLE REQ"
            )
            self.control_status.setText("Disabled command state")
            if self.commanded_on:
                self.commanded_on = False
                self.power_switch.set_pending(False)
        elif p.get("enable") == 0 and p.get("alarm_in") == 0:
            self.rf_badge.setText("ENABLE CMD")
            self.control_status.setText("Amplifier enable commanded; RF unverified")
        else:
            self.rf_badge.setText("STATE CHECK")
            self.control_status.setText("Unexpected control-pin state")

        if time.monotonic() >= self.status_message_until and not self.fault_latched:
            self.message.setText(
                "Live telemetry received"
                + (" [DEMO]" if self.demo else "")
            )

    def invalidate_telemetry(self):
        self.ntc_value.setText("— °C")
        self.ntc_status.setText("Waiting for valid SO24 telemetry")
        self.doctor_power_value.setText("— W")
        self.last_good_sensor = False
        self.last_telemetry = {}
        self.pump_reported_on = self.pump_sim_ack if PUMP_SIMULATED else None
        self.pump_pending = None
        self.pump_pending_deadline = 0.0
        self.refresh_pump_control()
        for card in self.cards.values():
            card.clear()
        self.cards["vswr"].set_reading("N/A")

        for row in self.diag_rows.values():
            row.set_value("—")

        self.commanded_on = False
        self.power_switch.set_pending(False)
        self.pending_on_deadline = 0.0
        self.rf_badge.setText(
            "UNKNOWN"
        )
        self.control_status.setText(
            "RF state unverified — ON locked"
        )
        self.adc_status.setText(
            "ADC: Not verified"
        )

    def check_timeout(self):
        if self.pump_pending_deadline and time.monotonic() >= self.pump_pending_deadline:
            self.pump_pending = None
            self.pump_pending_deadline = 0.0
            self.refresh_pump_control()
            self.set_status("Pump command not confirmed — check STM32 telemetry")
        if (self.pending_on_deadline and
                time.monotonic() >= self.pending_on_deadline):
            self.power_switch.set_pending(False)
            self.pending_on_deadline = 0.0
            self.control_status.setText("ON request timed out — not authorized")
            self.set_status("No STM32 ON response: treated as denied")
        if (self.timer_deadline is not None and
                (self.last_packet_time == 0 or
                 time.monotonic() - self.last_packet_time > TELEMETRY_TIMEOUT)):
            self.emergency_stop("TELEMETRY_TIMEOUT", "STM32 telemetry lost")
        if (
            self.last_packet_time == 0
            or time.monotonic() - self.last_packet_time
               > TELEMETRY_TIMEOUT
        ):
            self.invalidate_telemetry()
            self.cards["uart"].set_reading(
                "Stale" if self.serial_connected else "Disconnected"
            )
            self.cards["i2c"].set_reading("Unknown")
            self.cards["vswr"].set_reading("N/A")
            self.i2c_status.setText(
                "I²C: Unverified"
            )
            if time.monotonic() >= self.status_message_until:
                self.message.setText("No recent STM32 telemetry")
        if self.role == "doctor":
            self.check_doctor_auto_start()
            self.refresh_doctor_display()

    # >>> WATCHDOG integration methods
    def open_watchdog(self):
        if self.role == "engineer":
            self.pages.setCurrentWidget(self.watchdog_page)

    def close_watchdog(self):
        self.pages.setCurrentWidget(self.main_page)

    def update_watchdog_button(self, count, highest):
        """Button turns red (high) or yellow (medium) while watchdog alarms are active."""
        b = self.watchdog_button
        b.setText("Watchdog")     # colour shows priority; the count is on the Watchdog page
        b.setToolTip(f"Watchdog: {count} active alarm(s)" if count else "Watchdog: no active alarms")
        if highest >= 3:
            b.setStyleSheet(WATCHDOG_BUTTON_QSS + f"background:{T['danger']}; color:{T['danger_ink']}; font-weight:bold;")
        elif highest == 2:
            b.setStyleSheet(WATCHDOG_BUTTON_QSS + f"background:{T['warn']}; color:{T['warn_ink']}; font-weight:bold;")
        else:
            b.setStyleSheet(WATCHDOG_BUTTON_QSS)

    def watchdog_force_off(self, alarm_id, text):
        """HIGH watchdog alarm while PA is requested or on: stop like a hardware fault.
        The watchdog has already sent DISABLE itself; this updates the treatment state."""
        self.log_event("WATCHDOG_RF_OFF", comment=f"{alarm_id}: {text}")
        if self.fault_latched:
            self.request_all_rf_off("WATCHDOG_" + alarm_id)
        else:
            self.emergency_stop("WATCHDOG_" + alarm_id, "Watchdog: " + text)
    # <<< WATCHDOG

    def closeEvent(self, event):
        self.timer_deadline = None
        if self.logger is not None:
            self.log_event("SESSION_CLOSED")
        if self.serial_connected and not self.demo:
            self.worker.send_command("DISABLE")
        if self.generator_connected:
            self.generator_worker.set_output(False)
        self.watchdog_page.stop()          # >>> WATCHDOG
        self.watchdog.shutdown()           # >>> WATCHDOG: log closed, DISABLE queued

        self.generator_worker.stop()
        self.generator_worker.wait(1500)
        self.worker.stop()
        self.worker.wait(1500)
        if self.logger is not None:
            self.logger.close()
            self.logger = None
        super().closeEvent(event)

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--port",
        default=SERIAL_PORT
    )

    parser.add_argument(
        "--demo",
        action="store_true"
    )

    parser.add_argument(
        "--windowed",
        action="store_true"
    )

    parser.add_argument(
        "--display",
        choices=("auto", "7inch", "monitor"),
        default="auto",
        help="7inch = 800x480 panel, monitor = HP/desktop monitor, auto = detect"
    )

    parser.add_argument(
        "--scale",
        type=float,
        default=None,
        help="Force a UI scale factor, e.g. 1.6 for 1366x768 or 2.25 for 1920x1080"
    )

    args = parser.parse_args()

    # Must be set before QApplication is created.
    scale = choose_scale(args.display, args.windowed, args.scale)
    os.environ["QT_SCALE_FACTOR"] = f"{scale:.3f}"
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    app = QApplication(sys.argv[:1])
    load_fonts()

    window = PowerAmplifierGUI(
        port=args.port,
        demo=args.demo
    )

    if args.windowed:
        window.resize(DESIGN_W, DESIGN_H)
        window.show()
    else:
        window.showFullScreen()

    sys.exit(app.exec())

if __name__ == "__main__":
    main()
