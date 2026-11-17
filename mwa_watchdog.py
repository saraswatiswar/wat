#!/usr/bin/env python3
"""
mwa_watchdog.py  -  Microwave Ablation System Watchdog
(Raspberry Pi  <-UART4->  STM32H755 CM7 PA monitor firmware,
Mini-Circuits ZHL-2425-250X+ amplifier)

TWO WAYS TO RUN
1. Inside the treatment app (mwa17.py), as the "Watchdog" page of Engineer
   mode. This is the normal way. The treatment app owns /dev/serial0; the
   watchdog SHARES that connection (it receives every raw line from the app's
   SerialWorker and sends DISABLE through it). Only one program can open the
   serial port, so do not run the stand-alone watchdog at the same time.
2. Stand-alone on a 7-inch screen (bench testing only):
       python3 mwa_watchdog.py --port /dev/serial0 [--windowed]

In shared mode the watchdog:
  - never sends HEARTBEAT or REQUEST_ON (the treatment app does that);
  - treats RF as "requested" while the treatment app has commanded RF on;
  - on any HIGH priority alarm while RF is requested or on, sends DISABLE at
    once and asks the treatment app to stop (its emergency_stop);
  - blocks a new RF ON request while a HIGH alarm cause is present.

SAFETY NOTE: this program monitors, alarms, logs and requests RF off. It must
NOT be the only thing that stops RF. Per ISO 14971:2019 7.1 the protective
measures in the device (STM32 firmware + hardware interlocks) come first.

---------------------------------------------------------------------------
PROTOCOL  (matches the STM32 main.c firmware as supplied, 115200 8N1, ASCII)
---------------------------------------------------------------------------
PC -> STM32 (one command per line, '\n' terminated):
    HEARTBEAT      keep-alive. STM32 turns RF off if none for 3500 ms
                   (PA_LINK_TIMEOUT_MS). This program sends it every 500 ms
                   ONLY while RF is permitted, so a lost DISABLE still ends RF.
    REQUEST_ON     STM32 enables RF only if PA connected, data valid, no PA
                   alarm and no ADC error.
    DISABLE        STM32 turns RF off immediately (in the RX interrupt).
STM32 -> PC:
    PA,ms=..,conn=..,valid=..,fwd_mdBm=..,refl_mdBm=..,rf_valid=..,current_mA=..,
       voltage_mV=..,temp_mC=..,alarm=..,adc_valid=..,fwd_mV=..,refl_mV=..,
       isense_mV=..,tsense_mV=..,alarm_out=..,enable=..,alarm_in=..,reset=..,
       rf_state=ENABLED|OFF,on_locked=0,so24_temp_mC=..,so24_valid=..,
       so24_calibrated=..,so24_raw=..          (about every 500 ms)
    ACK,DISABLE,REQUESTED
    ACK,REQUEST_ON,ENABLED
    ACK,REQUEST_ON,DENIED,MONITOR_OR_ALARM
    EVENT,RF_OFF,WATCHDOG_OR_FAULT
If a line ends in *XX (NMEA XOR checksum) it is verified; the current firmware
sends no checksum, see FIRMWARE GAPS below.

---------------------------------------------------------------------------
STANDARDS MAPPING  (what this file implements; it does NOT by itself make the
device compliant - compliance needs the documented process, risk file and
test evidence listed under "STILL REQUIRED")
---------------------------------------------------------------------------
IEC 60601-1-8:2006 (alarm systems)
  6.1.2  every alarm has HIGH/MEDIUM/LOW priority (ALARM_DEFS). Priorities here
         are PROPOSALS; final values come from Table 1 via your risk file.
  6.3.2.2 Table 2  visual: HIGH red 2.0 Hz 50 % duty, MEDIUM yellow 0.625 Hz
         50 % duty, LOW cyan constant. Text names each alarm, "!!!/!!/!" marks.
  6.3.3.1 Tables 3+4  auditory: HIGH 10-pulse burst (td 150 ms, x 100 ms,
         5th-6th gap 0.6 s, interburst 5 s); MEDIUM 3-pulse burst (td 200 ms,
         y 200 ms, interburst 10 s); pulse f0 500 Hz + 4 harmonics up to
         2.5 kHz, rise 15 % of td. LOW: no sound (optional per note d).
         A burst is always completed once started (6.10) unless paused/reset.
  6.8    AUDIO PAUSED for AUDIO_PAUSE_S with countdown + crossed-bell
         indication; operator can end it early; sound returns afterwards.
  6.9/6.10 latching alarms need RESET; RESET refused while cause is present.
         Latching choice is fixed in code (not changeable by the operator).
  6.12   alarm log: every raise / clear / reset / audio pause, with time and
         priority, in logs/alarm_events.csv (kept on disk after power-down).
IEC 62304:2006
  4.3    treat this software as Class C until your risk file classifies it.
  8.1.2  SOUP: Python 3, pyserial, PySide6 (Qt 6), aplay (Linux) - record
         exact versions in your SOUP list.
  7.1.2  hardware/SOUP faults covered: port loss, corrupt data, STM32 reset,
         STM32 clock drift, unacknowledged commands, frozen screen, stalled
         monitor loop, log-disk failure, PC clock jump.
  5.8/8.1 SOFTWARE_VERSION is written to every log at session start.
IEC 81001-5-1:2021 5.4.3  serial input is untrusted: line length limit,
         printable ASCII only, exact key whitelist, range checks, no eval.
IEC 62366-1  plain-language alarm texts, one action per button, status by
         colour AND text (never colour alone).
ISO 14971:2019 7.1  host is the 3rd layer; RF off also happens by STM32
         heartbeat timeout and by hardware.

FIRMWARE GAPS found in the STM32 code (fix there, they are risk controls):
  F1  No checksum/CRC on any line, so a corrupted digit cannot be detected.
  F2  HEARTBEAT is not echoed (no sequence number), so the PC cannot prove the
      STM32 receives it. Suggest HEARTBEAT,<seq> and seq echo in PA line.
  F3  SO24 critical temperature only sounds the buzzer; it does NOT turn RF
      off in the STM32. Add PA_ForceDisabled() for mode 3 and mode 4.
  F4  No independent watchdog (IWDG). A hung main loop keeps the last RF state.
  F5  No HAL_UART_ErrorCallback: after a UART overrun, receive stops forever
      (RF then goes off after 3.5 s, but the link never recovers).
  F6  <math.h> is not included in main.c (isfinite/logf) unless pa_monitor.h
      includes it.
  F7  No firmware version in telemetry (needed for 62304 configuration ID).
  F8  Error_Handler before gpio_ready=1 cannot force RF off: GPIO reset state
      of PA_ENABLE / PA_ALARM_IN must be the OFF level in hardware.

STILL REQUIRED for compliance (outside this file): risk management file,
software requirements + traceability (REQ ids below), verification tests,
sound-pressure measurement of the alarm sound on the real speaker, usability
validation, instructions for use disclosing priorities, delays, audio-pause
duration and log behaviour (60601-1-8 6.1.2, 6.4, 6.8.5, 6.12).

Logs are written to the "logs" folder next to this file:
  session_<time>.csv    every 100 ms (keeps logging if the link is lost)
  alarm_events.csv      every alarm raised / cleared / reset / paused
"""
import argparse
import csv
import html
import importlib.util
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import Optional

import serial
import serial.tools.list_ports
from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog,
                               QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QMainWindow, QMenu, QPlainTextEdit, QPushButton, QScroller,
                               QStackedWidget, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

SOFTWARE_NAME = "Microwave ablation system watchdog"
SOFTWARE_VERSION = "2.3.0-dev"
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
AUDIO_PAUSE_S = 120          # disclose in the instructions for use (60601-1-8 6.8.5)


# ============================================================ CONFIG
@dataclass
class Config:
    port: str = ""
    baud: int = 115200
    hb_period_s: float = 0.5          # STM32 PA_LINK_TIMEOUT_MS is 3500 ms
    loop_period_s: float = 0.1        # evaluation + logging period
    link_timeout_s: float = 2.0       # STM32 sends ~every 0.5 s: 4 frames missed
    startup_grace_s: float = 3.0
    max_bad_frames: int = 3           # consecutive bad lines => BAD_FRAMES
    ack_timeout_s: float = 2.0        # REQUEST_ON / DISABLE must be ACKed in this time
    disable_repeat_s: float = 1.0     # resend DISABLE while RF still reported ON
    gui_timeout_s: float = 2.0        # screen not refreshed => RF not permitted
    loop_stall_s: float = 1.0         # evaluation loop late => RF not permitted
    max_line_bytes: int = 600
    stm_clock_tolerance: float = 0.05 # STM32 ms vs PC clock, over 20 s windows
    # SO24 probe temperature levels - same as the STM32 buzzer thresholds.
    probe_warn_c: float = 30.0
    probe_high_c: float = 40.0
    probe_critical_c: float = 60.0
    # PLACEHOLDERS - None means "not set": the check is off and a LOW alarm
    # "Safety limits not configured" stays on. Take values from the
    # ZHL-2425-250X+ datasheet and your risk analysis, not from this file.
    pa_temp_max_c: Optional[float] = None
    supply_min_v: Optional[float] = None
    supply_max_v: Optional[float] = None
    refl_check_min_fwd_dbm: Optional[float] = None   # only check above this forward power
    return_loss_min_db: Optional[float] = None       # fwd_dBm - refl_dBm must be >= this
    log_dir: str = "logs"

    def missing_limits(self):
        names = ("pa_temp_max_c", "supply_min_v", "supply_max_v",
                 "refl_check_min_fwd_dbm", "return_loss_min_db")
        return [n for n in names if getattr(self, n) is None]


# ============================================================ ALARM DEFINITIONS
class Prio(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3


@dataclass(frozen=True)
class AlarmDef:
    req: str        # requirement id for 62304 traceability
    id: str
    text: str
    prio: Prio
    latching: bool


# Priorities are PROPOSALS. Per IEC 60601-1-8 6.1.2 / Table 1 the final
# priority comes from your risk management file (ISO 14971).
ALARM_DEFS = [
    AlarmDef("ALM-01", "SERIAL_PORT_LOST",     "UART port missing or cable unplugged",      Prio.HIGH,   True),
    AlarmDef("ALM-02", "LINK_LOST",            "No data from STM32",                        Prio.HIGH,   True),
    AlarmDef("ALM-03", "BAD_FRAMES",           "Corrupted data from STM32",                 Prio.MEDIUM, True),
    AlarmDef("ALM-04", "CMD_NOT_ACKED",        "STM32 did not confirm a command",           Prio.HIGH,   True),
    AlarmDef("ALM-05", "STM_RESET",            "STM32 restarted unexpectedly",              Prio.HIGH,   True),
    AlarmDef("ALM-06", "STM_CLOCK_FAULT",      "STM32 clock rate out of tolerance",         Prio.MEDIUM, True),
    AlarmDef("ALM-07", "PA_NOT_CONNECTED",     "Amplifier not connected",                   Prio.HIGH,   True),
    AlarmDef("ALM-08", "PA_DATA_INVALID",      "Amplifier status data invalid",             Prio.HIGH,   True),
    AlarmDef("ALM-09", "PA_ALARM",             "Amplifier reports an alarm",                Prio.HIGH,   True),
    AlarmDef("ALM-10", "PA_OVER_TEMP",         "Amplifier over-temperature",                Prio.HIGH,   True),
    AlarmDef("ALM-11", "SUPPLY_FAULT",         "Amplifier supply voltage out of range",     Prio.HIGH,   True),
    AlarmDef("ALM-12", "ADC_FAULT",            "Measurement ADC fault",                     Prio.HIGH,   True),
    AlarmDef("ALM-13", "RF_MEAS_INVALID",      "RF power reading invalid while RF is on",   Prio.HIGH,   True),
    AlarmDef("ALM-14", "REFLECTED_HIGH",       "High reflected power",                      Prio.HIGH,   True),
    AlarmDef("ALM-15", "ENABLE_LINE_MISMATCH", "RF enable lines do not match RF state",     Prio.HIGH,   True),
    AlarmDef("ALM-16", "RF_ON_UNCOMMANDED",    "RF on without a request",                   Prio.HIGH,   True),
    AlarmDef("ALM-17", "RF_SWITCHED_OFF_BY_STM", "STM32 switched RF off (watchdog or fault)", Prio.MEDIUM, True),
    AlarmDef("ALM-18", "TEMP_SENSOR_FAULT",    "Temperature sensor fault or unplugged",     Prio.HIGH,   True),
    AlarmDef("ALM-19", "PROBE_TEMP_CRITICAL",  "Temperature critical",                      Prio.HIGH,   True),
    AlarmDef("ALM-20", "PROBE_TEMP_HIGH",      "Temperature high",                          Prio.MEDIUM, False),
    AlarmDef("ALM-21", "PROBE_TEMP_WARN",      "Temperature above warning level",           Prio.LOW,    False),
    AlarmDef("ALM-22", "PROBE_TEMP_EXTRAPOLATED", "Temperature outside calibrated range",   Prio.LOW,    False),
    AlarmDef("ALM-23", "GUI_NOT_RESPONDING",   "Monitor screen stopped responding",         Prio.HIGH,   True),
    AlarmDef("ALM-24", "CLOCK_JUMP",           "Computer clock jumped",                     Prio.MEDIUM, True),
    AlarmDef("ALM-25", "LOG_FAILURE",          "Data logging to disk failed",               Prio.MEDIUM, True),
    AlarmDef("ALM-26", "CONFIG_INCOMPLETE",    "Safety limits not configured",              Prio.LOW,    False),
]
ALARMS = {a.id: a for a in ALARM_DEFS}


# ============================================================ FRAMING / PARSING
PA_UINT_KEYS = ("ms", "conn", "valid", "rf_valid", "alarm", "adc_valid", "fwd_mV", "refl_mV",
                "isense_mV", "tsense_mV", "alarm_out", "enable", "alarm_in", "reset",
                "on_locked", "so24_valid", "so24_calibrated", "so24_raw")
PA_INT_KEYS = ("fwd_mdBm", "refl_mdBm", "current_mA", "voltage_mV", "temp_mC", "so24_temp_mC")
PA_BOOL_KEYS = ("conn", "valid", "rf_valid", "adc_valid", "alarm_out", "enable", "alarm_in",
                "reset", "on_locked", "so24_valid", "so24_calibrated")
PA_KEYS = set(PA_UINT_KEYS) | set(PA_INT_KEYS) | {"rf_state"}
PA_OPTIONAL_KEYS = {"pump_on"}      # sent by some firmware builds; accepted, not required
RF_STATES = ("ENABLED", "OFF")


def checksum(payload: str) -> str:
    cs = 0
    for ch in payload:
        cs ^= ord(ch)
    return f"{cs:02X}"


def clean_line(raw: bytes):
    """Untrusted input (IEC 81001-5-1 5.4.3 g). Return text or None."""
    try:
        s = raw.decode("ascii").rstrip("\r\n")
    except UnicodeDecodeError:
        return None
    if not s or any(not (32 <= ord(c) <= 126) for c in s):
        return None
    if len(s) > 3 and s[-3] == "*":               # optional *XX checksum
        payload, cs = s[:-3], s[-2:]
        if checksum(payload.lstrip("$")) != cs.upper():
            return None
        s = payload.lstrip("$")
    return s


def parse_pa(s: str):
    """Strict parse of one PA telemetry line. Returns dict or None."""
    parts = s.split(",")
    if parts[0] != "PA":
        return None
    out = {}
    for p in parts[1:]:
        k, sep, v = p.partition("=")
        if not sep or k not in (PA_KEYS | PA_OPTIONAL_KEYS) or k in out or len(v) > 12:
            return None
        if k == "rf_state":
            if v not in RF_STATES:
                return None
            out[k] = v
            continue
        try:
            n = int(v, 10)
        except ValueError:
            return None
        if (k in PA_UINT_KEYS or k in PA_OPTIONAL_KEYS) and n < 0:
            return None
        if (k in PA_BOOL_KEYS or k in PA_OPTIONAL_KEYS) and n not in (0, 1):
            return None
        out[k] = n
    if set(out) - PA_OPTIONAL_KEYS != PA_KEYS:
        return None
    return out


# ============================================================ ALARM MANAGER
class AlarmManager:
    """Latching / non-latching alarm handling, logs every transition (60601-1-8 6.12)."""

    def __init__(self, event_log_path, callbacks=None):
        self.lock = threading.Lock()
        self.state = {}  # id -> dict(active, cond)
        self.callbacks = callbacks or []
        new = not os.path.exists(event_log_path)
        self._f = open(event_log_path, "a", newline="", buffering=1)
        self._w = csv.writer(self._f)
        self._wlock = threading.Lock()
        if new:
            self._w.writerow(["wall_time", "event", "req", "alarm_id", "priority", "latching", "text"])

    def _row(self, row):
        with self._wlock:
            try:
                self._w.writerow([datetime.now().isoformat(timespec="milliseconds")] + row)
                self._f.flush()
                os.fsync(self._f.fileno())
            except (OSError, ValueError):
                pass

    def log_info(self, event, text):
        self._row([event, "", "", "", "", text])

    def _log(self, event, d: AlarmDef):
        self._row([event, d.req, d.id, d.prio.name, d.latching, d.text])
        for cb in self.callbacks:
            try:
                cb(event, d)
            except Exception as e:  # a GUI callback must never break the watchdog
                print(f"[alarm callback error] {e}", file=sys.stderr)

    def update(self, conditions: set):
        with self.lock:
            for d in ALARM_DEFS:
                st = self.state.setdefault(d.id, {"active": False, "cond": False})
                present = d.id in conditions
                if present and not st["active"]:
                    st["active"] = True
                    st["cond"] = True
                    self._log("RAISED", d)
                elif present:
                    st["cond"] = True
                elif st["active"] and st["cond"]:        # condition just went away
                    st["cond"] = False
                    if d.latching:
                        self._log("CONDITION_CLEARED_STILL_LATCHED", d)
                    else:
                        st["active"] = False
                        self._log("CLEARED", d)

    def reset(self):
        """Operator ALARM RESET. Only latched alarms whose cause is gone are cleared."""
        stuck = []
        with self.lock:
            for d in ALARM_DEFS:
                st = self.state.get(d.id)
                if st and st["active"] and d.latching:
                    if st["cond"]:
                        stuck.append(d.id)
                    else:
                        st["active"] = False
                        self._log("RESET_BY_OPERATOR", d)
        return stuck

    def active(self):
        with self.lock:
            return [ALARMS[i] for i, s in self.state.items() if s["active"]]

    def active_detail(self):
        """[(AlarmDef, cause_still_present)] for every active alarm."""
        with self.lock:
            return [(ALARMS[i], s["cond"]) for i, s in self.state.items() if s["active"]]

    def highest(self):
        return max((x.prio for x in self.active()), default=None)

    def close(self):
        with self._wlock:
            self._f.close()


# ============================================================ CSV DATA LOGGER
class DataLogger:
    COLUMNS = (["wall_time", "mono_s", "rf_requested", "permit", "hb_sent", "link_ok"]
               + sorted(PA_KEYS) + ["active_alarms"])

    def __init__(self, log_dir):
        os.makedirs(log_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(log_dir, f"session_{stamp}.csv")
        self._f = open(self.path, "w", newline="", buffering=1)
        self._w = csv.writer(self._f)
        self._w.writerow([f"# {SOFTWARE_NAME} {SOFTWARE_VERSION}"])
        self._w.writerow(self.COLUMNS)
        self._last_sync = time.monotonic()
        self.ok = True

    def write(self, row: dict):
        """Returns False if the disk write failed."""
        try:
            self._w.writerow([row.get(c, "") for c in self.COLUMNS])
            self._f.flush()
            if time.monotonic() - self._last_sync > 1.0:   # force to disk once a second
                os.fsync(self._f.fileno())
                self._last_sync = time.monotonic()
            self.ok = True
        except (OSError, ValueError):
            self.ok = False
        return self.ok

    def close(self):
        try:
            self._f.flush()
            os.fsync(self._f.fileno())
            self._f.close()
        except (OSError, ValueError):
            pass


# ============================================================ WATCHDOG
class Watchdog:
    """transport: None = open cfg.port itself (stand-alone).
    transport = callable(cmd) -> bool  => SHARED mode: lines come in through
    feed_line(), port state through set_port_state(), commands go out through
    transport. rf_requested: callable() -> bool, True while the host app has
    commanded RF on. on_force_off: callable(alarm_id, text), called once per
    HIGH-alarm episode while RF is requested or on (from the watchdog thread)."""

    def __init__(self, cfg: Config, alarm_callbacks=None, transport=None,
                 rf_requested=None, on_force_off=None):
        self.cfg = cfg
        self.transport = transport
        self.shared = transport is not None
        self.rf_requested = rf_requested
        self.on_force_off = on_force_off
        self._forced = False
        self._loop_thread = None
        os.makedirs(cfg.log_dir, exist_ok=True)
        self.alarms = AlarmManager(os.path.join(cfg.log_dir, "alarm_events.csv"), alarm_callbacks)
        self.logger = DataLogger(cfg.log_dir)

        self.lock = threading.Lock()        # shared state
        self.tx_lock = threading.Lock()     # serial writes
        self.eval_lock = threading.Lock()   # _evaluate is called from loop and reset
        self.stop_evt = threading.Event()
        self.ser = None
        self.port_ok = False
        self._rxbuf = b""

        now = time.monotonic()
        self.last_rx = None
        self.bad_frames = 0
        self.tel = None
        self.prev_ms = None
        self.clk_base = None
        self.stm_reset_seen = False
        self.stm_clock_bad = False
        self.stm_rf_off_seen = False
        self.ack_failed = False
        self.pending = {}                   # command -> time sent
        self.last_cmd = ("--", "")

        self.desired_run = False
        self.run_off_since = now
        self.hb_active = False
        self.hb_count = 0
        self.last_disable_tx = 0.0
        self.t_start = now
        self.loop_last = now
        self.gui_last = now
        self.gui_dead = False
        self._clock_ref = (time.time(), now)
        self.info = deque(maxlen=200)       # plain-language events for the GUI log

        self.alarms.log_info("SESSION_START", f"{SOFTWARE_NAME} {SOFTWARE_VERSION}, "
                             f"port {cfg.port} @ {cfg.baud}"
                             + (" (shared with treatment app)" if self.shared else ""))
        missing = cfg.missing_limits()
        if missing:
            self.alarms.log_info("CONFIG", "limits not set: " + ", ".join(missing))

    # ---------- helpers
    def note(self, text, level="info"):
        self.info.append((time.strftime("%H:%M:%S"), text, level))
        self.alarms.log_info("INFO", text)

    def _open_port(self):
        try:
            self.ser = serial.Serial(self.cfg.port, self.cfg.baud, timeout=0.05, write_timeout=0.2)
            self.ser.reset_input_buffer()
            self._rxbuf = b""
            with self.lock:
                self.port_ok = True
        except (serial.SerialException, OSError):
            self.ser = None
            with self.lock:
                self.port_ok = False

    def _port_failed(self):
        with self.lock:
            self.port_ok = False
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    # ---------- shared mode inputs (called from the host app's serial thread)
    def feed_line(self, raw):
        """One received line (bytes, without the trailing newline)."""
        self._handle_line(bytes(raw), time.monotonic())

    def set_port_state(self, ok):
        with self.lock:
            self.port_ok = bool(ok)

    def rf_on_blockers(self):
        """HIGH alarms whose cause is present now: RF must not be started."""
        return [d for d, cond in self.alarms.active_detail() if cond and d.prio == Prio.HIGH]

    def _send(self, cmd):
        if self.shared:
            try:
                return bool(self.transport(cmd))
            except Exception:
                return False
        with self.tx_lock:
            ser = self.ser
            if ser is None:
                return False
            try:
                ser.write((cmd + "\n").encode("ascii"))
                return True
            except (serial.SerialException, OSError):
                self._port_failed()
                return False

    def _send_disable(self, now):
        with self.lock:
            self.pending.setdefault("DISABLE", now)
        self.last_disable_tx = now
        return self._send("DISABLE")

    def _link_ok(self, now):                    # call with lock held
        return self.last_rx is not None and (now - self.last_rx) <= self.cfg.link_timeout_s

    # ---------- reader thread
    def _reader(self):
        while not self.stop_evt.is_set():
            if self.ser is None:
                self._open_port()
                if self.ser is None:
                    time.sleep(0.5)
                    continue
            try:
                chunk = self.ser.read(self.ser.in_waiting or 1)
            except (serial.SerialException, OSError, TypeError, AttributeError):
                self._port_failed()
                continue
            if not chunk:
                continue
            self._rxbuf += chunk
            while b"\n" in self._rxbuf:
                line, self._rxbuf = self._rxbuf.split(b"\n", 1)
                self._handle_line(line, time.monotonic())
            if len(self._rxbuf) > self.cfg.max_line_bytes:   # no newline: garbage
                self._rxbuf = b""
                with self.lock:
                    self.bad_frames += 1

    def _handle_line(self, raw, now):
        s = clean_line(raw)
        if s is None or len(raw) > self.cfg.max_line_bytes:
            with self.lock:
                self.bad_frames += 1
            return
        if s.startswith("PA,"):
            tel = parse_pa(s)
            with self.lock:
                if tel is None:
                    self.bad_frames += 1
                    return
                self.bad_frames = 0
                self.last_rx = now
                if self.prev_ms is not None and tel["ms"] < self.prev_ms:
                    self.stm_reset_seen = True        # latched until operator reset
                    self.clk_base = None
                    self.desired_run = False
                self.prev_ms = tel["ms"]
                if self.clk_base is None:
                    self.clk_base = (now, tel["ms"])
                elif now - self.clk_base[0] >= 20.0:
                    ratio = ((tel["ms"] - self.clk_base[1]) / 1000.0) / (now - self.clk_base[0])
                    if abs(ratio - 1.0) > self.cfg.stm_clock_tolerance:
                        self.stm_clock_bad = True
                    self.clk_base = (now, tel["ms"])
                self.tel = tel
            return
        msg = None
        with self.lock:
            if s == "ACK,DISABLE,REQUESTED":
                self.pending.pop("DISABLE", None)
                self.last_cmd = ("RF off", "confirmed")
            elif s == "ACK,REQUEST_ON,ENABLED":
                self.pending.pop("REQUEST_ON", None)
                self.last_cmd = ("RF on", "confirmed")
                msg = ("STM32 confirmed RF on", "ok")
            elif s.startswith("ACK,REQUEST_ON,DENIED"):
                self.pending.pop("REQUEST_ON", None)
                self.desired_run = False
                self.run_off_since = now
                self.last_cmd = ("RF on", "refused")
                msg = ("STM32 refused RF on: amplifier not ready or alarm", "warn")
            elif s == "EVENT,RF_OFF,WATCHDOG_OR_FAULT":
                self.stm_rf_off_seen = True
                self.desired_run = False
                self.run_off_since = now
                msg = ("STM32 switched RF off by itself (heartbeat timeout or fault)", "warn")
            elif s.startswith(("ACK,", "EVENT,")):
                pass                        # other replies (e.g. pump): valid, not ours
            else:
                self.bad_frames += 1
                return
            self.bad_frames = 0
        if msg:
            self.note(*msg)

    # ---------- heartbeat thread
    def _permit(self, now):
        hi = self.alarms.highest()
        return (self.desired_run and (hi is None or hi < Prio.HIGH) and self.port_ok
                and (now - self.loop_last) < self.cfg.loop_stall_s and not self.gui_dead)

    def _heartbeat(self):
        nxt = time.monotonic()
        while not self.stop_evt.is_set():
            nxt += self.cfg.hb_period_s
            now = time.monotonic()
            self.hb_active = self._permit(now)
            if self.hb_active and self._send("HEARTBEAT"):
                self.hb_count += 1
            time.sleep(max(0.0, nxt - time.monotonic()))

    # ---------- operator commands
    def start_rf(self):
        now = time.monotonic()
        with self.lock:
            link_ok = self._link_ok(now)
        if not link_ok:
            return False, "no data from STM32"
        blocking = [d for d in self.alarms.active() if d.prio >= Prio.MEDIUM]
        if blocking:
            return False, "clear these alarms first: " + ", ".join(d.text for d in blocking)
        self.desired_run = True
        self.hb_active = True
        self._send("HEARTBEAT")
        with self.lock:
            self.pending["REQUEST_ON"] = now
            self.last_cmd = ("RF on", "waiting")
        ok = self._send("REQUEST_ON")
        return ok, "RF on requested" if ok else "could not send to STM32"

    def stop_rf(self):
        now = time.monotonic()
        self.desired_run = False
        self.run_off_since = now
        self.hb_active = False
        with self.lock:
            self.pending.pop("REQUEST_ON", None)
            self.last_cmd = ("RF off", "waiting")
        self._send_disable(now)

    def gui_beat(self):
        self.gui_last = time.monotonic()

    def reset_alarms(self):
        now = time.monotonic()
        with self.lock:
            self.stm_reset_seen = False
            self.stm_rf_off_seen = False
            self.ack_failed = False
            self.stm_clock_bad = False
        if now - self.gui_last < self.cfg.gui_timeout_s:
            self.gui_dead = False
        cond, _, _ = self._evaluate(now)        # refresh causes before reset
        self.alarms.update(cond)
        return self.alarms.reset()

    # ---------- evaluation
    def _evaluate(self, now):
        with self.eval_lock:
            return self._evaluate_locked(now)

    def _evaluate_locked(self, now):
        c = self.cfg
        cond = set()
        with self.lock:
            port_ok, bad, tel = self.port_ok, self.bad_frames, self.tel
            link_ok = self._link_ok(now)
            for cmd, t in list(self.pending.items()):
                if now - t > c.ack_timeout_s:
                    del self.pending[cmd]
                    self.ack_failed = True
                    self.last_cmd = ("RF off" if cmd == "DISABLE" else "RF on", "no reply")
            flags = (self.stm_reset_seen, self.stm_clock_bad, self.ack_failed, self.stm_rf_off_seen)
        stm_reset, clock_bad, ack_failed, rf_off_seen = flags
        grace_over = (now - self.t_start) > c.startup_grace_s

        if not port_ok and grace_over:             # port is opened just after start
            cond.add("SERIAL_PORT_LOST")
        if not link_ok and grace_over:
            cond.add("LINK_LOST")
        if bad >= c.max_bad_frames:
            cond.add("BAD_FRAMES")
        if stm_reset:
            cond.add("STM_RESET")
        if clock_bad:
            cond.add("STM_CLOCK_FAULT")
        if ack_failed:
            cond.add("CMD_NOT_ACKED")
        if rf_off_seen:
            cond.add("RF_SWITCHED_OFF_BY_STM")

        if link_ok and tel:
            rf_on = tel["rf_state"] == "ENABLED"
            # PA_ENABLE and PA_ALARM_IN are active-low: 0/0 when ON, 1/1 when OFF.
            if (tel["enable"], tel["alarm_in"]) != ((0, 0) if rf_on else (1, 1)):
                cond.add("ENABLE_LINE_MISMATCH")
            if rf_on and not self.desired_run and (now - self.run_off_since) > c.ack_timeout_s:
                cond.add("RF_ON_UNCOMMANDED")
            if not tel["adc_valid"] and grace_over:
                cond.add("ADC_FAULT")
            if not tel["conn"]:
                cond.add("PA_NOT_CONNECTED")
            elif not tel["valid"]:
                cond.add("PA_DATA_INVALID")
            else:
                if tel["alarm"] != 0:
                    cond.add("PA_ALARM")
                if c.pa_temp_max_c is not None and tel["temp_mC"] / 1000 > c.pa_temp_max_c:
                    cond.add("PA_OVER_TEMP")
                if c.supply_min_v is not None and c.supply_max_v is not None and \
                   not (c.supply_min_v <= tel["voltage_mV"] / 1000 <= c.supply_max_v):
                    cond.add("SUPPLY_FAULT")
                if rf_on and not tel["rf_valid"]:
                    cond.add("RF_MEAS_INVALID")
                if (c.refl_check_min_fwd_dbm is not None and c.return_loss_min_db is not None
                        and tel["rf_valid"]):
                    fwd, refl = tel["fwd_mdBm"] / 1000, tel["refl_mdBm"] / 1000
                    if fwd >= c.refl_check_min_fwd_dbm and (fwd - refl) < c.return_loss_min_db:
                        cond.add("REFLECTED_HIGH")
            if not tel["so24_valid"]:
                cond.add("TEMP_SENSOR_FAULT")
            else:
                t = tel["so24_temp_mC"] / 1000
                if t >= c.probe_critical_c:
                    cond.add("PROBE_TEMP_CRITICAL")
                elif t >= c.probe_high_c:
                    cond.add("PROBE_TEMP_HIGH")
                elif t >= c.probe_warn_c:
                    cond.add("PROBE_TEMP_WARN")
                if not tel["so24_calibrated"]:
                    cond.add("PROBE_TEMP_EXTRAPOLATED")

        if now - self.gui_last > c.gui_timeout_s:
            self.gui_dead = True
        if self.gui_dead:
            cond.add("GUI_NOT_RESPONDING")
        wall0, mono0 = self._clock_ref        # detect NTP / manual clock changes
        if abs((time.time() - wall0) - (now - mono0)) > 1.0:
            cond.add("CLOCK_JUMP")
            self._clock_ref = (time.time(), now)
        if not self.logger.ok:
            cond.add("LOG_FAILURE")
        if c.missing_limits():
            cond.add("CONFIG_INCOMPLETE")
        return cond, link_ok, tel

    def _log_row(self, now, link_ok, tel):
        row = {
            "wall_time": datetime.now().isoformat(timespec="milliseconds"),
            "mono_s": f"{now - self.t_start:.3f}",
            "rf_requested": int(self.desired_run),
            "permit": int(self.hb_active),
            "hb_sent": self.hb_count,
            "link_ok": int(link_ok),
            "active_alarms": ";".join(a.id for a in self.alarms.active()),
        }
        if tel:                                   # keep logging even if link is lost
            row.update(tel)
        self.logger.write(row)

    # ---------- main loop
    def start(self):
        if not self.shared:
            threading.Thread(target=self._reader, daemon=True).start()
            threading.Thread(target=self._heartbeat, daemon=True).start()
        self._loop_thread = threading.Thread(target=self._loop, daemon=True)
        self._loop_thread.start()

    def _loop(self):
        nxt = time.monotonic()
        while not self.stop_evt.is_set():
            now = time.monotonic()
            self.loop_last = now
            cond, link_ok, tel = self._evaluate(now)
            self.alarms.update(cond)
            rf_on = bool(tel and link_ok and tel["rf_state"] == "ENABLED")
            hi = self.alarms.highest()
            if self.shared:
                requested = bool(self.rf_requested()) if self.rf_requested else False
                if requested != self.desired_run:
                    self.desired_run = requested
                    if not requested:
                        self.run_off_since = now
                self.hb_active = requested            # host app sends the heartbeats
                if hi == Prio.HIGH and (requested or rf_on):
                    if not self._forced:
                        self._forced = True
                        worst = sorted(((d, c) for d, c in self.alarms.active_detail()
                                        if d.prio == Prio.HIGH),
                                       key=lambda x: (not x[1], x[0].req))[0][0]   # cause present first
                        self.note(f"RF off requested by watchdog: {worst.text}", "alarm")
                        self._send_disable(now)
                        if self.on_force_off:
                            try:
                                self.on_force_off(worst.id, worst.text)
                            except Exception as e:
                                print(f"[watchdog] force-off callback: {e}", file=sys.stderr)
                elif hi != Prio.HIGH:
                    self._forced = False
            elif hi == Prio.HIGH and self.desired_run:
                self.desired_run = False             # latched safe state on any HIGH alarm
                self.run_off_since = now
                self.hb_active = False
                self.note("RF off requested automatically: high priority alarm", "alarm")
                self._send_disable(now)
            if rf_on and not self.desired_run and now - self.last_disable_tx >= self.cfg.disable_repeat_s:
                self._send_disable(now)              # keep asking until STM32 reports OFF
            self._log_row(now, link_ok, tel)
            nxt += self.cfg.loop_period_s
            time.sleep(max(0.0, nxt - time.monotonic()))

    def snapshot(self):
        """Thread-safe copy of everything the GUI needs."""
        now = time.monotonic()
        with self.lock:
            snap = {
                "port_ok": self.port_ok,
                "link_ok": self._link_ok(now),
                "rx_age_s": None if self.last_rx is None else now - self.last_rx,
                "bad_frames": self.bad_frames,
                "tel": dict(self.tel) if self.tel else None,
                "pending": dict(self.pending),
                "last_cmd": self.last_cmd,
            }
        snap.update({
            "run": self.desired_run,
            "permit": self.hb_active,
            "hb_count": self.hb_count,
            "loop_age": now - self.loop_last,
            "alarms": self.alarms.active_detail(),
        })
        return snap

    def shutdown(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self.desired_run = False
        self.hb_active = False
        self.stop_evt.set()
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=1.0)
        for _ in range(1 if self.shared else 3):  # tell STM32: RF off
            self._send("DISABLE")
            time.sleep(0.02)
        self.alarms.log_info("SESSION_END", "disconnected, DISABLE sent")
        try:
            if self.ser is not None:
                self.ser.close()
        except Exception:
            pass
        self.logger.close()
        self.alarms.close()


# ============================================================ GUI  (7-inch touchscreen, customer design)
# Layout follows the supplied design: header with menu button + title, alarm
# banner, a left page (Live Status or System Check) and a right column with
# Active Alarms and Event Log. Designed for 800 x 480; scales to larger screens.
# Alarm colours follow IEC 60601-1-8 Table 2 (red high, yellow medium, cyan low).
BG, HEADER, PANEL, TILE, TILE_LINE = "#0E1124", "#15365F", "#15365F", "#23569A", "#2E66B0"
TEXT, LABEL, MUTED, DISABLED = "#FFFFFF", "#C9DAF0", "#A9BCD6", "#6E86A8"
GREEN, RED, AMBER, GREY, BLUE, CYAN = "#4ADE80", "#EF4444", "#FACC15", "#94A3B8", "#7DD3FC", "#22D3EE"
ALARM_BG, WARN_BG, LOW_BG = "#7F1D1D", "#5C4A0B", "#0E4A5A"
# Accent colour per Live Status tile (top line + label), like a bedside monitor.
A_RF, A_PERMIT, A_PA, A_LINK, A_UP, A_BAD, A_CMD = (
    "#7DD3FC", "#4ADE80", "#5EEAD4", "#C4B5FD", "#F9A8D4", "#BFDBFE", "#A5B4FC")
FONT = "'Inter','DejaVu Sans','Segoe UI','Helvetica Neue',Arial,sans-serif"
SCALE = 1.0
PORT_CANDIDATES = ("/dev/serial0", "/dev/ttyAMA0", "/dev/ttyS0", "/dev/ttyUSB0", "/dev/ttyACM0")


def S(px):
    """Scale a size given for an 800 x 480 screen."""
    return max(1, int(round(px * SCALE)))


def make_qss():
    return f"""
QWidget#WatchdogRoot {{ background:{BG}; }}
QWidget {{ color:{TEXT}; font-family:{FONT}; font-size:{S(13)}px; background:transparent; }}
QLabel {{ background:transparent; }}
QFrame#header {{ background:{HEADER}; border:none; }}
QLabel#title {{ font-size:{S(17)}px; font-weight:800; letter-spacing:0.5px; }}
QFrame#panel {{ background:{PANEL}; border:none; border-radius:{S(8)}px; }}
QLabel#panelTitle {{ font-size:{S(13)}px; font-weight:800; }}
QPushButton {{ background:{TILE}; border:1px solid {TILE_LINE}; border-radius:{S(6)}px;
    padding:{S(4)}px {S(12)}px; font-weight:700; font-size:{S(13)}px; }}
QPushButton:pressed {{ background:#2E66B0; }}
QPushButton:disabled {{ color:{DISABLED}; background:#1B3D6B; border-color:#1B3D6B; }}
QPushButton#reset {{ background:{AMBER}; color:#2A2100; border:none; }}
QPushButton#reset:pressed {{ background:#FDE047; }}
QPushButton#reset:disabled {{ background:#1B3D6B; color:{DISABLED}; }}
QPushButton#yes {{ background:#16A34A; border:none; font-size:{S(18)}px; }}
QPushButton#no {{ font-size:{S(18)}px; }}
QComboBox {{ background:{TILE}; border:1px solid {TILE_LINE}; border-radius:{S(6)}px;
    padding:{S(4)}px {S(8)}px; min-height:{S(30)}px; }}
QComboBox:disabled {{ color:{DISABLED}; }}
QComboBox::drop-down {{ width:{S(26)}px; border:none; }}
QComboBox QAbstractItemView {{ background:{PANEL}; border:1px solid {TILE_LINE}; font-size:{S(15)}px;
    selection-background-color:{TILE}; }}
QComboBox QAbstractItemView::item {{ min-height:{S(40)}px; }}
QMenu {{ background:{HEADER}; border:2px solid {TILE_LINE}; padding:{S(6)}px; }}
QMenu::item {{ padding:{S(14)}px {S(40)}px {S(14)}px {S(20)}px; font-size:{S(17)}px;
    font-weight:700; border-radius:{S(6)}px; }}
QMenu::item:selected {{ background:{TILE}; }}
QMenu::item:checked {{ background:{TILE}; color:{TEXT}; }}
QMenu::indicator {{ width:0px; }}
QTableWidget {{ background:{PANEL}; border:none; font-size:{S(12)}px; }}
QPlainTextEdit {{ background:#0A0A0A; border:none; border-radius:{S(4)}px;
    font-family:'DejaVu Sans Mono',Consolas,monospace; font-size:{S(10)}px; }}
QScrollBar:vertical {{ background:transparent; width:{S(12)}px; margin:2px; }}
QScrollBar::handle:vertical {{ background:{TILE_LINE}; border-radius:{S(5)}px; min-height:{S(30)}px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
QDialog {{ background:{HEADER}; border:2px solid {TILE_LINE}; }}
"""


class Bridge(QObject):
    """Carries alarm events from the watchdog thread to the GUI thread."""
    alarm_event = Signal(str, str, str, str)


def alarm_texts(ids):
    return ", ".join(ALARMS[i].text for i in sorted(ids))


def make_panel(title, extra=None):
    frame = QFrame()
    frame.setObjectName("panel")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(S(10), S(6), S(10), S(10))
    lay.setSpacing(S(6))
    top = QHBoxLayout()
    t = QLabel(title)
    t.setObjectName("panelTitle")
    top.addWidget(t)
    top.addStretch(1)
    if extra is not None:
        top.addWidget(extra)
    lay.addLayout(top)
    return frame, lay, t


def set_led(label, color):
    if label.property("led") != color:
        label.setProperty("led", color)
        label.setStyleSheet(f"background:{color}; border-radius:{label.width() // 2}px;")


class MenuButton(QPushButton):
    """Three-bar menu button, drawn so it looks the same on every system."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(S(58), S(52))
        self.setStyleSheet(f"QPushButton {{ background:{HEADER}; border:none; border-radius:0; }}"
                           f"QPushButton:pressed {{ background:{TILE}; }}")

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#05070F"))
        w, h = S(28), S(4)
        x = (self.width() - w) // 2
        y0 = self.height() // 2 - S(10)
        for i in range(3):
            p.drawRoundedRect(x, y0 + i * S(9), w, h, h / 2, h / 2)
        p.end()


class StatusBox(QFrame):
    """One item of the system check: light, name, plain-language state."""

    def __init__(self, title):
        super().__init__()
        self.setObjectName("status")
        self._last = None
        self.setMinimumHeight(S(56))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(S(10), S(6), S(8), S(6))
        lay.setSpacing(S(8))
        self.led = QLabel()
        self.led.setFixedSize(S(12), S(12))
        lay.addWidget(self.led, 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(S(1))
        name = QLabel(title)
        name.setStyleSheet(f"font-weight:800; font-size:{S(13)}px;")
        self.state = QLabel()
        self.state.setWordWrap(True)
        col.addWidget(name)
        col.addWidget(self.state)
        lay.addLayout(col, 1)
        self.set_state("Not connected", GREY)

    def set_state(self, text, color):
        if (text, color) == self._last:
            return
        self._last = (text, color)
        set_led(self.led, color)
        self.state.setText(text)
        bg = {RED: ALARM_BG, AMBER: WARN_BG}.get(color, TILE)
        fg = {RED: "#FFFFFF", AMBER: "#FEF08A", GREY: MUTED}.get(color, color)
        self.state.setStyleSheet(f"color:{fg}; font-size:{S(11)}px;")
        self.setStyleSheet(f"QFrame#status {{ background:{bg}; border:none; border-radius:{S(6)}px; }}")


class ParamTile(QFrame):
    """Large reading tile. state: normal / ok / on / muted / alarm."""
    STATES = {"normal": (TILE, TEXT), "ok": (TILE, GREEN), "on": (TILE, BLUE),
              "muted": (TILE, MUTED), "alarm": (ALARM_BG, "#FFFFFF")}

    def __init__(self, label, unit="", size=22, accent=None):
        super().__init__()
        self.setObjectName("tile")
        self.size = size
        self.accent = accent
        self._text, self._state = None, None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(S(10), S(8), S(10), S(8))
        lay.setSpacing(0)
        top = QHBoxLayout()
        name = QLabel(label)
        name.setStyleSheet(f"color:{accent or LABEL}; font-weight:800; font-size:{S(12)}px;")
        top.addWidget(name)
        top.addStretch(1)
        if unit:
            u = QLabel(unit)
            u.setStyleSheet(f"color:{LABEL}; font-size:{S(11)}px;")
            top.addWidget(u)
        lay.addLayout(top)
        lay.addStretch(1)
        self.val = QLabel()
        lay.addWidget(self.val)
        self.sub = QLabel()
        self.sub.setWordWrap(True)
        lay.addWidget(self.sub)
        lay.addStretch(1)
        self.set("--", "", "muted")

    def set(self, value, sub="", state="normal"):
        if state != self._state:
            self._state = state
            bg, fg = self.STATES[state]
            line = f"border-top:{S(4)}px solid {self.accent};" if self.accent else ""
            self.setStyleSheet(f"QFrame#tile {{ background:{bg}; border:none; {line}"
                               f" border-radius:{S(6)}px; }}")
            self.val.setStyleSheet(f"color:{fg}; font-size:{S(self.size)}px; font-weight:800;")
            self.sub.setStyleSheet(f"color:{LABEL}; font-size:{S(11)}px;")
        if (value, sub) != self._text:
            self._text = (value, sub)
            self.val.setText(value)
            self.sub.setText(sub)


class Confirm(QDialog):
    """Large touch-friendly yes/no dialog."""

    def __init__(self, parent, question, yes_text, no_text="Cancel"):
        super().__init__(parent)
        self.setModal(True)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setMinimumWidth(S(440))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(S(24), S(20), S(24), S(20))
        lay.setSpacing(S(18))
        q = QLabel(question)
        q.setWordWrap(True)
        q.setAlignment(Qt.AlignCenter)
        q.setStyleSheet(f"font-size:{S(18)}px; font-weight:700;")
        lay.addWidget(q)
        row = QHBoxLayout()
        row.setSpacing(S(16))
        no = QPushButton(no_text)
        no.setObjectName("no")
        yes = QPushButton(yes_text)
        yes.setObjectName("yes")
        for b in (no, yes):
            b.setMinimumHeight(S(64))
            row.addWidget(b, 1)
        no.clicked.connect(self.reject)
        yes.clicked.connect(self.accept)
        lay.addLayout(row)


def fmt_uptime(ms):
    s = ms // 1000
    return f"{s // 3600}:{(s // 60) % 60:02d}:{s % 60:02d}"


# ============================================================ AUDITORY ALARM SIGNALS
# IEC 60601-1-8 Table 3 (burst) and Table 4 (pulse). Values chosen inside the
# allowed ranges; verify the real sound with a sound level meter / scope.
SR = 44100
F0 = 500.0                                  # 150-1000 Hz
HARMONICS = (1.0, 0.7, 0.5, 0.4, 0.3)       # 500,1000,1500,2000,2500 Hz (>=4 in 300-4000 Hz, within 15 dB)
HIGH_TD, HIGH_X, HIGH_GAP56, HIGH_TB = 0.150, 0.100, 0.60, 5.0     # td 75-200 ms, x 50-125 ms, 0.35-1.3 s, tb 2.5-15 s
MED_TD, MED_Y, MED_TB = 0.200, 0.200, 10.0                         # td 125-250 ms, y 125-250 ms, tb 2.5-30 s


def _pulse(td):
    n = int(td * SR)
    ramp = max(1, int(0.15 * n))            # rise 10-20 % of td
    norm = sum(HARMONICS)
    out = []
    for i in range(n):
        env = min(1.0, i / ramp, (n - 1 - i) / ramp)
        v = sum(a * math.sin(2 * math.pi * F0 * (k + 1) * i / SR) for k, a in enumerate(HARMONICS))
        out.append(env * v / norm)
    return out


def _burst(prio):
    if prio == Prio.HIGH:
        td, x = HIGH_TD, HIGH_X
        gaps = [x, x, 2 * x + td, x, HIGH_GAP56, x, x, 2 * x + td, x]   # 10 pulses
    else:
        td, y = MED_TD, MED_Y
        gaps = [y, y]                                                    # 3 pulses
    p = _pulse(td)
    samples = list(p)
    for g in gaps:
        samples += [0.0] * int(g * SR) + p
    return samples


def make_burst_wav(prio):
    path = os.path.join(tempfile.gettempdir(), f"ablation_alarm_{prio.name.lower()}.wav")
    samples = _burst(prio)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"".join(struct.pack("<h", int(v * 30000)) for v in samples))
    return path, len(samples) / SR


class AlarmSound:
    """Plays priority-encoded bursts. A started burst is completed (6.10)
    unless the operator pauses audio or resets.
    Windows: winsound (standard library). Linux / Raspberry Pi: aplay.
    Otherwise: system beep (NOT 60601-1-8 compliant - for bench use only)."""

    def __init__(self, parent=None):
        self.cur, self.next_t, self.proc = None, 0.0, None
        self.files, self.length = {}, {}
        self.backend = "beep"
        try:
            for prio in (Prio.HIGH, Prio.MEDIUM):
                self.files[prio], self.length[prio] = make_burst_wav(prio)
            if sys.platform.startswith("win") and importlib.util.find_spec("winsound"):
                self.backend = "winsound"
            elif shutil.which("aplay"):
                self.backend = "aplay"
        except Exception as e:
            print(f"[alarm sound] using system beep: {e}", file=sys.stderr)
            self.length = {Prio.HIGH: 3.5, Prio.MEDIUM: 1.0}

    def stop(self):
        self.cur = None
        try:
            if self.backend == "winsound":
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            elif self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass
        self.proc = None

    def _play(self, prio, now):
        try:
            if self.backend == "winsound":
                import winsound
                winsound.PlaySound(self.files[prio], winsound.SND_FILENAME | winsound.SND_ASYNC)
            elif self.backend == "aplay":
                if self.proc is not None and self.proc.poll() is None:
                    self.proc.terminate()
                self.proc = subprocess.Popen(["aplay", "-q", self.files[prio]],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                QApplication.beep()
        except Exception as e:
            print(f"[alarm sound] {e}", file=sys.stderr)
            QApplication.beep()
        tb = HIGH_TB if prio == Prio.HIGH else MED_TB
        self.next_t = now + self.length[prio] + tb

    def update(self, prio, paused, now):
        if paused:
            if self.cur is not None:
                self.stop()
            return
        if prio not in (Prio.HIGH, Prio.MEDIUM):   # LOW: no sound (note d of Table 3)
            self.cur = None                       # let a started burst finish
            return
        if prio != self.cur or now >= self.next_t:
            self.cur = prio
            self._play(prio, now)


LINK_IDS = {"SERIAL_PORT_LOST", "LINK_LOST", "BAD_FRAMES"}
STM_IDS = {"STM_RESET", "STM_CLOCK_FAULT", "CMD_NOT_ACKED"}
GROUPS = {
    "pa":     {"PA_NOT_CONNECTED", "PA_DATA_INVALID", "PA_ALARM", "PA_OVER_TEMP"},
    "rf":     {"RF_MEAS_INVALID", "REFLECTED_HIGH", "ENABLE_LINE_MISMATCH",
               "RF_ON_UNCOMMANDED", "RF_SWITCHED_OFF_BY_STM"},
    "adc":    {"ADC_FAULT"},
    "supply": {"SUPPLY_FAULT"},
    "temp":   {"TEMP_SENSOR_FAULT", "PROBE_TEMP_CRITICAL", "PROBE_TEMP_HIGH",
               "PROBE_TEMP_WARN", "PROBE_TEMP_EXTRAPOLATED"},
    "host":   {"GUI_NOT_RESPONDING", "CLOCK_JUMP", "LOG_FAILURE", "CONFIG_INCOMPLETE"},
}
EVENT_WORDS = {"RAISED": "Alarm raised", "CLEARED": "Alarm cleared",
               "CONDITION_CLEARED_STILL_LATCHED": "Cause gone, still latched",
               "RESET_BY_OPERATOR": "Reset by operator"}
PRIO_COLOR = {Prio.HIGH: RED, Prio.MEDIUM: AMBER, Prio.LOW: CYAN}
PRIO_MARK = {Prio.HIGH: "!!!", Prio.MEDIUM: "!!", Prio.LOW: "!"}

PAGE_LIVE, PAGE_SYSTEM = 0, 1
PAGE_NAMES = {PAGE_LIVE: "Live Status", PAGE_SYSTEM: "System Check"}


class WatchdogPage(QWidget):
    """The watchdog screen as a widget.
    Embedded:   WatchdogPage(wd=<shared Watchdog>, back=<callable>)
    Stand-alone: WatchdogPage(port="/dev/serial0") - creates its own Watchdog."""
    status_changed = Signal(int, int)        # (active alarm count, highest priority 0-3)

    def __init__(self, wd=None, back=None, port=None, baud=115200, log_dir=LOG_DIR):
        super().__init__()
        self.wd = wd
        self.embedded = wd is not None
        self.baud = baud
        self.log_dir = log_dir
        self._status = None
        self.bridge = Bridge()
        self.bridge.alarm_event.connect(self.on_alarm_event)
        self.setObjectName("WatchdogRoot")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(make_qss())
        self._banner_key = None
        self._alarm_key = None
        self._msg_until, self._msg_text = 0.0, ""
        self.sound = AlarmSound(self)

        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        # ---- header: menu button + title (always visible)
        header = QFrame()
        header.setObjectName("header")
        header.setFixedHeight(S(52))
        hl = QHBoxLayout(header)
        hl.setContentsMargins(0, 0, S(14), 0)
        hl.setSpacing(S(12))
        self.btn_menu = MenuButton()
        self.btn_menu.clicked.connect(self.open_menu)
        hl.addWidget(self.btn_menu)
        title = QLabel("MICROWAVE ABLATION SYSTEM WATCHDOG")
        title.setObjectName("title")
        hl.addWidget(title)
        hl.addStretch(1)
        self.conn_led = QLabel()                       # small link light, top right
        self.conn_led.setFixedSize(S(12), S(12))
        self.conn_led.setToolTip("Link: green = data from STM32, red = no data, grey = not connected")
        hl.addWidget(self.conn_led)
        if back is not None:
            btn_back = QPushButton("Back")
            btn_back.setMinimumSize(S(84), S(36))
            btn_back.clicked.connect(back)
            hl.addWidget(btn_back)
        main.addWidget(header)

        self.menu = QMenu(self)
        self.page_actions = {}
        for idx in (PAGE_LIVE, PAGE_SYSTEM):
            act = QAction(PAGE_NAMES[idx], self)
            act.setCheckable(True)
            act.triggered.connect(lambda _=False, i=idx: self.show_page(i))
            self.menu.addAction(act)
            self.page_actions[idx] = act

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(S(8), S(8), S(8), S(8))
        bl.setSpacing(S(8))
        main.addWidget(body, 1)

        # ---- alarm banner
        self.banner = QLabel()
        self.banner.setAlignment(Qt.AlignCenter)
        self.banner.setWordWrap(True)
        self.banner.setFixedHeight(S(56))
        bl.addWidget(self.banner)

        # ---- content: left page + right column
        content = QHBoxLayout()
        content.setSpacing(S(8))
        bl.addLayout(content, 1)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_live_page())
        self.pages.addWidget(self._build_system_page())
        content.addWidget(self.pages, 7)

        right = QVBoxLayout()
        right.setSpacing(S(8))
        self.btn_reset = QPushButton("Reset")
        self.btn_reset.setObjectName("reset")
        self.btn_reset.setMinimumSize(S(84), S(32))
        self.btn_reset.setEnabled(False)
        self.btn_reset.clicked.connect(self.on_reset)
        af, alo, self.alarm_title = make_panel("Active Alarms", self.btn_reset)
        self.alarm_empty = QLabel("No active alarms")
        self.alarm_empty.setAlignment(Qt.AlignCenter)
        self.alarm_empty.setStyleSheet(f"color:{MUTED}; font-size:{S(13)}px;")
        self.table = QTableWidget(0, 2)
        self.table.horizontalHeader().setVisible(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setShowGrid(False)
        self.table.setWordWrap(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        QScroller.grabGesture(self.table.viewport(), QScroller.LeftMouseButtonGesture)
        self.table.hide()
        alo.addWidget(self.alarm_empty, 1)
        alo.addWidget(self.table, 1)
        right.addWidget(af, 1)

        ef, elo, _ = make_panel("Event Log")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(300)
        QScroller.grabGesture(self.log.viewport(), QScroller.LeftMouseButtonGesture)
        elo.addWidget(self.log, 1)
        right.addWidget(ef, 1)
        content.addLayout(right, 3)

        self.show_page(PAGE_LIVE)                     # Live Status by default
        if self.embedded:
            self.conn_row.hide()                      # port belongs to the treatment app
            self.btn_reset.setEnabled(True)
            cb = lambda event, d: self.bridge.alarm_event.emit(event, d.id, d.prio.name, d.text)  # noqa: E731
            self.wd.alarms.callbacks.append(cb)
            self.log_line("Watchdog running, serial port shared with the treatment app", BLUE)
        else:
            self.refresh_ports(port)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(100)
        self.refresh()
        if port:
            QTimer.singleShot(500, lambda: self.connect_port(port, self.baud))

    # ---------------------------------------------------------- pages
    def _build_live_page(self):
        pf, plo, _ = make_panel("Live Status")
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(S(6))
        self.p = {
            "rf": ParamTile("RF output", "from STM32", 40, A_RF),
            "permit": ParamTile("RF permit", "", 22, A_PERMIT),
            "pa": ParamTile("Amplifier", "", 17, A_PA),
            "link": ParamTile("Link", "ms", 22, A_LINK),
            "uptime": ParamTile("STM32 uptime", "h:mm:ss", 22, A_UP),
            "bad": ParamTile("Bad frames", "", 22, A_BAD),
            "cmd": ParamTile("Last command", "", 15, A_CMD),
        }
        grid.addWidget(self.p["rf"], 0, 0, 1, 2)
        grid.addWidget(self.p["permit"], 0, 2)
        grid.addWidget(self.p["pa"], 0, 3)
        grid.addWidget(self.p["link"], 1, 0)
        grid.addWidget(self.p["uptime"], 1, 1)
        grid.addWidget(self.p["bad"], 1, 2)
        grid.addWidget(self.p["cmd"], 1, 3)
        for col, w in enumerate((145, 225, 140, 140)):      # proportions from the design
            grid.setColumnStretch(col, w)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        plo.addLayout(grid, 1)
        return pf

    def _build_system_page(self):
        sf, slo, _ = make_panel("System Check")
        grid = QGridLayout()
        grid.setSpacing(S(8))
        self.b_pc, self.b_uart, self.b_stm = (StatusBox("Raspberry Pi"),
                                              StatusBox("UART link"), StatusBox("STM32"))
        self.dev = {k: StatusBox(t) for k, t in
                    [("pa", "Amplifier"), ("rf", "RF chain"), ("adc", "Meas. ADC"),
                     ("supply", "Supply"), ("temp", "Temp sensor"), ("host", "Software")]}
        for i, w in enumerate([self.b_pc, self.b_uart, self.b_stm, *self.dev.values()]):
            grid.addWidget(w, i // 3, i % 3)
        for i in range(3):
            grid.setColumnStretch(i, 1)
        slo.addLayout(grid)
        slo.addStretch(1)

        # connection row (uses the free space under the grid)
        row = QHBoxLayout()
        row.setSpacing(S(8))
        lbl = QLabel("Port")
        lbl.setStyleSheet(f"color:{LABEL}; font-weight:700;")
        row.addWidget(lbl)
        self.cb_port = QComboBox()
        self.cb_port.setMinimumWidth(S(140))
        row.addWidget(self.cb_port)
        self.btn_conn = QPushButton("Connect")
        self.btn_conn.setMinimumSize(S(100), S(36))
        self.btn_conn.clicked.connect(self.toggle_connection)
        row.addWidget(self.btn_conn)
        row.addStretch(1)
        quit_btn = QPushButton("Quit")
        quit_btn.setMinimumSize(S(70), S(36))
        quit_btn.clicked.connect(self.on_quit)
        row.addWidget(quit_btn)
        self.conn_row = QWidget()
        self.conn_row.setLayout(row)
        slo.addWidget(self.conn_row)
        self.info = QLabel()
        self.info.setStyleSheet(f"color:{MUTED}; font-size:{S(10)}px;")
        slo.addWidget(self.info)
        return sf

    def open_menu(self):
        cur = self.pages.currentIndex()
        for idx, act in self.page_actions.items():
            act.setChecked(idx == cur)
        self.menu.setMinimumWidth(S(240))
        self.menu.exec(self.btn_menu.mapToGlobal(QPoint(0, self.btn_menu.height())))

    def show_page(self, idx):
        self.pages.setCurrentIndex(idx)
        for i, act in self.page_actions.items():
            act.setChecked(i == idx)
        self.refresh()

    # ---------------------------------------------------------- connection
    def refresh_ports(self, preferred=None):
        ports = [p for p in PORT_CANDIDATES if os.path.exists(p)]
        ports += [p.device for p in serial.tools.list_ports.comports() if p.device not in ports]
        if preferred and preferred not in ports:
            ports.insert(0, preferred)
        if not ports:
            ports = ["/dev/serial0"]
        self.cb_port.clear()
        self.cb_port.addItems(ports)
        if preferred:
            self.cb_port.setCurrentText(preferred)

    def toggle_connection(self):
        if self.wd is None:
            self.connect_port(self.cb_port.currentText().strip(), self.baud)
        elif Confirm(self, "Disconnect from the STM32?\nRF will be switched off.",
                     "Disconnect").exec() == QDialog.Accepted:
            self.disconnect_port()

    def connect_port(self, port, baud=115200):
        if self.wd is not None:
            return
        try:                                   # quick check so the user gets a clear message
            serial.Serial(port, baud).close()
        except (serial.SerialException, OSError) as e:
            self.log_line(f"Cannot open {port}: {e}", AMBER)
            self._flash_message(f"Cannot open {port}. Check the cable and port.")
            return
        cb = lambda event, d: self.bridge.alarm_event.emit(event, d.id, d.prio.name, d.text)  # noqa: E731
        self.wd = Watchdog(Config(port=port, baud=baud, log_dir=self.log_dir), [cb])
        self.wd.start()
        self.btn_conn.setText("Disconnect")
        self.cb_port.setEnabled(False)
        self.btn_reset.setEnabled(True)
        self.log_line(f"Connected to {port} at {baud} baud", BLUE)
        missing = self.wd.cfg.missing_limits()
        if missing:
            self.log_line("Safety limits not set in Config: " + ", ".join(missing), CYAN)

    def disconnect_port(self):
        was = self.wd is not None
        if self.wd:
            self.wd.shutdown()
        self.wd = None
        self.sound.stop()
        self.btn_conn.setText("Connect")
        self.cb_port.setEnabled(True)
        self.btn_reset.setEnabled(False)
        if was:
            self.log_line("Disconnected, DISABLE sent to STM32", MUTED)

    # ---------------------------------------------------------- actions
    def log_line(self, text, color=TEXT, stamp=None):
        self.log.appendHtml(f'<span style="color:{MUTED}">{stamp or time.strftime("%H:%M:%S")}</span>&nbsp;'
                            f'<span style="color:{color}">{html.escape(text)}</span>')

    def on_reset(self):
        if not self.wd:
            return
        self.sound.stop()
        stuck = self.wd.reset_alarms()
        if stuck:
            self.log_line(f"Reset refused, cause still present: {alarm_texts(stuck)}", AMBER)
            self._flash_message("Some alarms cannot be reset: cause still present")
        else:
            self.log_line("Alarms reset", GREEN)

    def on_quit(self):
        if Confirm(self, "Quit the watchdog?\nRF will be switched off.", "Quit").exec() == QDialog.Accepted:
            self.window().close()

    def stop(self):
        """Stop the screen timer and sound. Stand-alone: also shut the watchdog down."""
        self.timer.stop()
        self.sound.stop()
        if not self.embedded:
            self.disconnect_port()

    def _flash_message(self, text):
        self._msg_until = time.monotonic() + 3.0
        self._msg_text = text

    def on_alarm_event(self, event, aid, prio, text):
        if event == "RAISED":
            color = {"HIGH": "#FCA5A5", "MEDIUM": AMBER}.get(prio, CYAN)
        elif event in ("CLEARED", "RESET_BY_OPERATOR"):
            color = GREEN
        else:
            color = MUTED
        self.log_line(f"{EVENT_WORDS.get(event, event)}: {text} ({prio.lower()})", color)

    # ---------------------------------------------------------- refresh
    def _set_banner(self, text, bg, fg, border=None):
        key = (text, bg, fg, border)
        if key == self._banner_key:
            return
        self._banner_key = key
        self.banner.setText(text)
        size = S(20) if len(text) < 46 else S(15)
        bd = f"border:{S(2)}px solid {border};" if border else "border:none;"
        self.banner.setStyleSheet(f"background:{bg}; color:{fg}; {bd} border-radius:{S(10)}px;"
                                  f" font-size:{size}px; font-weight:800; padding:0 {S(12)}px;")

    def _show_alarms(self, alarms):
        key = tuple((d.id, cond) for d, cond in alarms)
        if key == self._alarm_key:
            return
        self._alarm_key = key
        rows = sorted(alarms, key=lambda x: (-x[0].prio, x[0].req))
        self.alarm_title.setText(f"Active Alarms ({len(rows)})" if rows else "Active Alarms")
        self.table.setVisible(bool(rows))
        self.alarm_empty.setVisible(not rows)
        self.table.setRowCount(len(rows))
        bold = QFont()
        bold.setBold(True)
        for r, (d, cond) in enumerate(rows):
            bg, fg = {Prio.HIGH: (ALARM_BG, "#FCA5A5"), Prio.MEDIUM: (WARN_BG, "#FDE68A"),
                      Prio.LOW: (LOW_BG, "#A5F3FC")}[d.prio]
            text = d.text if cond else f"{d.text}\n(cause gone, press Reset)"
            for col, txt in enumerate((PRIO_MARK[d.prio], text)):
                it = QTableWidgetItem(txt)
                it.setBackground(QColor(bg))
                it.setForeground(QColor(fg if col == 0 else TEXT))
                if col == 0:
                    it.setFont(bold)
                    it.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(r, col, it)
        self.table.resizeRowsToContents()
        for r in range(len(rows)):
            self.table.setRowHeight(r, max(S(32), self.table.rowHeight(r) + S(4)))

    def _drain_info(self):
        out = []
        while self.wd and self.wd.info:
            try:
                out.append(self.wd.info.popleft())
            except IndexError:
                break
        return out

    def refresh(self):
        if not hasattr(self, "timer"):
            return
        now = time.monotonic()
        msg = self._msg_until > now
        if self.wd is None:
            if msg:
                self._set_banner(self._msg_text, WARN_BG, "#FEF08A")
            else:
                self._set_banner("Not connected. Open the menu, choose System Check and press Connect.",
                                 TILE, LABEL)
            set_led(self.conn_led, GREY)
            for w in (self.b_pc, self.b_uart, self.b_stm, *self.dev.values()):
                w.set_state("Not connected", GREY)
            for t in self.p.values():
                t.set("--", "", "muted")
            self.info.setText(f"{SOFTWARE_NAME} {SOFTWARE_VERSION}")
            self._show_alarms([])
            self.sound.update(None, False, now)
            return
        self.wd.gui_beat()
        for stamp, text, level in self._drain_info():
            self.log_line(text, {"ok": GREEN, "warn": AMBER, "alarm": "#FCA5A5"}.get(level, TEXT), stamp)
        s = self.wd.snapshot()
        c = self.wd.cfg
        tel, link, alarms = s["tel"], s["link_ok"], s["alarms"]
        present = {d.id for d, cond in alarms if cond}
        latched = {d.id for d, cond in alarms if not cond}
        stalled = s["loop_age"] > c.loop_stall_s
        set_led(self.conn_led, GREEN if link else RED)

        # banner (60601-1-8 Table 2): HIGH red 2 Hz 50 %, MEDIUM yellow 0.625 Hz 50 %, LOW cyan constant
        hi = max((d.prio for d, _ in alarms), default=None)
        if stalled:
            hi = Prio.HIGH

        def names(prio):
            lst = [d.text for d, _ in sorted(alarms, key=lambda x: x[0].req) if d.prio == prio]
            if prio == Prio.HIGH and stalled:
                lst.insert(0, "Monitor software stalled")
            return lst[0] + (f"  (+{len(lst) - 1} more)" if len(lst) > 1 else "") if lst else ""

        if hi == Prio.HIGH:
            on = int(now * 4) % 2 == 0
            self._set_banner(f"!!!  {names(Prio.HIGH)}", RED if on else "#4A1111", "#FFFFFF")
        elif hi == Prio.MEDIUM:
            on = (now % 1.6) < 0.8
            self._set_banner(f"!!  {names(Prio.MEDIUM)}", AMBER if on else "#3D3306",
                             "#1A1400" if on else AMBER)
        elif msg:
            self._set_banner(self._msg_text, WARN_BG, "#FEF08A")
        elif hi == Prio.LOW:
            self._set_banner(f"!  {names(Prio.LOW)}", CYAN, "#062A33")
        else:
            rf_on = bool(tel and link and tel["rf_state"] == "ENABLED")
            self._set_banner("System OK.  RF on" if rf_on else "System OK.  RF off", TILE, GREEN, GREEN)
        self.sound.update(hi, False, now)
        status = (len(alarms), int(hi or 0))
        if status != self._status:
            self._status = status
            self.status_changed.emit(*status)
        if not self.isVisible():
            return                                    # page hidden: watchdog keeps running
        self._show_alarms(alarms)

        if self.pages.currentIndex() == PAGE_LIVE:
            self._refresh_live(s, c, tel, link, present)
        else:
            self._refresh_system(s, c, tel, link, present, latched, stalled)

    def _refresh_live(self, s, c, tel, link, present):
        P = self.p
        if tel and link:
            rf_on = tel["rf_state"] == "ENABLED"
            P["rf"].set("ON" if rf_on else "OFF",
                        "Energy delivery active" if rf_on else "No energy delivered",
                        "on" if rf_on else "muted")
            if not tel["conn"]:
                P["pa"].set("Not connected", "Check I2C cable", "alarm")
            elif not tel["valid"]:
                P["pa"].set("No data", "Status read failed", "alarm")
            elif tel["alarm"]:
                P["pa"].set("Alarm", "Amplifier fault", "alarm")
            else:
                P["pa"].set("Ready", "No alarm", "ok")
            reset = "STM_RESET" in present
            P["uptime"].set(fmt_uptime(tel["ms"]), "Restart detected" if reset else "Running",
                            "alarm" if reset else "normal")
        else:
            P["rf"].set("--", "No data from STM32", "muted")
            P["pa"].set("--", "No data", "muted")
            P["uptime"].set("--", "No data", "muted")
        P["permit"].set("ON" if s["permit"] else "OFF",
                        "Heartbeat sending" if s["permit"] else "Heartbeat stopped",
                        "ok" if s["permit"] else "muted")
        age = s["rx_age_s"]
        P["link"].set("--" if age is None else f"{age * 1000:.0f}",
                      f"Limit {c.link_timeout_s * 1000:.0f}",
                      "alarm" if present & {"LINK_LOST", "SERIAL_PORT_LOST"} else "normal")
        bad = s["bad_frames"]
        P["bad"].set(str(bad), f"Alarm at {c.max_bad_frames}",
                     "alarm" if bad >= c.max_bad_frames else "normal")
        what, result = s["last_cmd"]
        P["cmd"].set(what, result.capitalize() if result else "None yet",
                     "alarm" if result in ("no reply", "refused") else "normal")

    def _refresh_system(self, s, c, tel, link, present, latched, stalled):
        self.b_pc.set_state("Monitor loop stalled" if stalled else "Running", RED if stalled else GREEN)
        lf = present & LINK_IDS
        if lf:
            self.b_uart.set_state(alarm_texts(lf), RED)
        elif link:
            self.b_uart.set_state("OK", GREEN)
        elif latched & LINK_IDS:
            self.b_uart.set_state("Restored, press Reset", AMBER)
        else:
            self.b_uart.set_state("Waiting for data", GREY)
        sf = present & STM_IDS
        if sf:
            self.b_stm.set_state(alarm_texts(sf), RED)
        elif latched & STM_IDS:
            self.b_stm.set_state("Cleared, press Reset", AMBER)
        elif not link:
            self.b_stm.set_state("Unknown, no data", GREY)
        else:
            self.b_stm.set_state("Running", GREEN)
        for k, ids in GROUPS.items():
            box = self.dev[k]
            now_ids = present & ids
            if now_ids:
                worst = max(ALARMS[i].prio for i in now_ids)
                box.set_state(alarm_texts(now_ids), RED if worst == Prio.HIGH else
                              AMBER if worst == Prio.MEDIUM else CYAN)
            elif latched & ids:
                box.set_state("Cleared, press Reset", AMBER)
            elif k == "host":
                box.set_state("OK", GREEN)
            elif k == "supply" and c.supply_min_v is None:
                box.set_state("Limit not set", GREY)
            elif not link:
                box.set_state("Unknown, no link", GREY)
            else:
                box.set_state("OK", GREEN)
        hb = "Heartbeats: treatment app" if self.embedded else f"Heartbeats {s['hb_count']}"
        self.info.setText(f"{SOFTWARE_NAME} {SOFTWARE_VERSION}   {hb}   "
                          f"Log: {os.path.basename(self.wd.logger.path)}")



class StandaloneWindow(QMainWindow):
    """Bench testing only: the watchdog page as its own window, with its own port."""

    def __init__(self, port=None, baud=115200):
        super().__init__()
        self.setWindowTitle("Microwave Ablation System Watchdog")
        self.page = WatchdogPage(port=port, baud=baud)
        self.setCentralWidget(self.page)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Q and e.modifiers() & Qt.ControlModifier:
            self.page.on_quit()
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e):
        self.page.stop()
        e.accept()


def default_port():
    for p in PORT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def create_shared_watchdog(app_window, log_dir=None):
    """Called by the treatment app. Builds a Watchdog that shares the app's
    SerialWorker. Returns (watchdog, force_off_signal_holder)."""
    holder = ForceOffSignal()
    w = app_window
    cfg = Config(port=getattr(w, "port", ""), baud=115200,
                 log_dir=log_dir or os.path.join(
                     os.environ.get("MWA_LOG_DIR", str(Path.home() / "MWA_Logs")), "watchdog"))
    wd = Watchdog(
        cfg,
        transport=w.worker.send_command,
        rf_requested=lambda: bool(w.commanded_on) or w.pending_on_deadline > time.monotonic(),
        on_force_off=lambda aid, text: holder.force_off.emit(aid, text),
    )
    return wd, holder


class ForceOffSignal(QObject):
    """Carries the watchdog's RF-off request from its thread to the GUI thread."""
    force_off = Signal(str, str)


def main():
    global SCALE
    ap = argparse.ArgumentParser(description="Microwave ablation system watchdog (stand-alone, bench use)")
    ap.add_argument("--port", help="serial port (default: /dev/serial0 if present)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--no-autoconnect", action="store_true", help="do not connect at start")
    ap.add_argument("--windowed", action="store_true", help="800x480 window instead of full screen")
    ap.add_argument("--hide-cursor", action="store_true", help="hide the mouse cursor (touch only)")
    args = ap.parse_args()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    if args.windowed:
        SCALE = 1.0
    else:
        geo = app.primaryScreen().geometry()
        SCALE = max(0.8, min(geo.width() / 800, geo.height() / 480))
    if args.hide_cursor:
        app.setOverrideCursor(Qt.BlankCursor)
    port = None if args.no_autoconnect else (args.port or default_port())
    win = StandaloneWindow(port=port, baud=args.baud)
    if args.windowed:
        win.resize(800, 480)
        win.show()
    else:
        win.showFullScreen()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
