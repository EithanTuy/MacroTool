"""
MacroTool — build, record, and run mouse/keyboard macro sequences
"""

import sys
import json
import time
import uuid
import copy
import threading
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QListWidget, QPushButton, QLabel, QDialog, QFormLayout, QLineEdit,
    QDoubleSpinBox, QSpinBox, QComboBox, QSplitter, QStatusBar,
    QGroupBox, QAbstractItemView, QMessageBox, QInputDialog, QTextEdit,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QFont

import pyautogui as pag
pag.FAILSAFE = True

from pynput import mouse as _mouse, keyboard as _kb

# ── persistence paths ─────────────────────────────────────────────────────────

SAVE_FILE     = Path(__file__).parent / "macros.json"
SETTINGS_FILE = Path(__file__).parent / "settings.json"

# ── step registry ─────────────────────────────────────────────────────────────

STEP_TYPES = [
    ("move",         "Move Mouse"),
    ("click",        "Click"),
    ("right_click",  "Right Click"),
    ("double_click", "Double Click"),
    ("wait",         "Wait"),
    ("type",         "Type Text"),
    ("key",          "Press Key / Hotkey"),
    ("scroll",       "Scroll"),
]
STEP_LABEL = {k: v for k, v in STEP_TYPES}

# ── helpers ───────────────────────────────────────────────────────────────────

def describe(step: dict) -> str:
    t = step["type"]
    p = step.get("params", {})
    if t == "move":
        return f"Move  ({p.get('x',0)}, {p.get('y',0)})  in {p.get('duration',0):.3f}s"
    if t == "click":        return f"Click  ({p.get('x',0)}, {p.get('y',0)})"
    if t == "right_click":  return f"Right-click  ({p.get('x',0)}, {p.get('y',0)})"
    if t == "double_click": return f"Double-click  ({p.get('x',0)}, {p.get('y',0)})"
    if t == "wait":         return f"Wait  {p.get('seconds',1.0):.3f}s"
    if t == "type":
        txt = p.get("text", "")
        return f'Type  "{txt[:50]}{"…" if len(txt) > 50 else ""}"'
    if t == "key":    return f"Key  {p.get('key','')}"
    if t == "scroll":
        amt = p.get("amount", 3)
        return f"Scroll {'↑' if amt > 0 else '↓'}{abs(amt)}  at ({p.get('x',0)}, {p.get('y',0)})"
    return t


def run_step(step: dict):
    t = step["type"]
    p = step.get("params", {})
    if t == "move":         pag.moveTo(p["x"], p["y"], duration=p.get("duration", 0))
    elif t == "click":      pag.click(p["x"], p["y"])
    elif t == "right_click":  pag.rightClick(p["x"], p["y"])
    elif t == "double_click": pag.doubleClick(p["x"], p["y"])
    elif t == "wait":       time.sleep(p.get("seconds", 1.0))
    elif t == "type":       pag.typewrite(p.get("text", ""), interval=p.get("interval", 0.05))
    elif t == "key":
        raw = p.get("key", "")
        if "+" in raw:
            pag.hotkey(*[k.strip() for k in raw.split("+")])
        else:
            pag.press(raw)
    elif t == "scroll":     pag.scroll(p.get("amount", 3), x=p["x"], y=p["y"])


def default_params(step_type: str) -> dict:
    x, y = pag.position()
    c = {"x": x, "y": y}
    return {
        "move":         {**c, "duration": 0.5},
        "click":        c,
        "right_click":  c,
        "double_click": c,
        "wait":         {"seconds": 1.0},
        "type":         {"text": "", "interval": 0.05},
        "key":          {"key": ""},
        "scroll":       {**c, "amount": 3},
    }.get(step_type, {})


def pynput_key_label(key) -> str:
    """Human-readable label for a pynput key."""
    try:
        if key.char:
            return key.char.upper()
    except AttributeError:
        pass
    name = getattr(key, "name", str(key)).replace("_", " ").title()
    return name


def pynput_key_to_pag(key) -> str:
    """Convert a pynput Key to a pyautogui key name."""
    _map = {
        _kb.Key.enter: "enter", _kb.Key.tab: "tab",
        _kb.Key.backspace: "backspace", _kb.Key.delete: "delete",
        _kb.Key.esc: "esc", _kb.Key.space: "space",
        _kb.Key.up: "up", _kb.Key.down: "down",
        _kb.Key.left: "left", _kb.Key.right: "right",
        _kb.Key.home: "home", _kb.Key.end: "end",
        _kb.Key.page_up: "pageup", _kb.Key.page_down: "pagedown",
        **{getattr(_kb.Key, f"f{i}"): f"f{i}" for i in range(1, 13)},
    }
    return _map.get(key, "")


# ── playback thread ───────────────────────────────────────────────────────────

class Runner(QThread):
    status  = pyqtSignal(str)
    done    = pyqtSignal()

    def __init__(self, steps: list, repeat: int = 1, delay: float = 3.0):
        super().__init__()
        self.steps  = steps
        self.repeat = repeat
        self.delay  = delay
        self._stop  = False

    def stop(self): self._stop = True

    def run(self):
        for i in range(int(self.delay), 0, -1):
            if self._stop:
                self.done.emit(); return
            self.status.emit(f"Starting in {i}…")
            time.sleep(1)
        for rep in range(self.repeat):
            if self._stop: break
            for idx, step in enumerate(self.steps):
                if self._stop: break
                self.status.emit(
                    f"Run {rep+1}/{self.repeat}  ·  Step {idx+1}/{len(self.steps)}: {describe(step)}"
                )
                try:
                    run_step(step)
                except pag.FailSafeException:
                    self.status.emit("Aborted — mouse moved to corner")
                    self.done.emit(); return
                except Exception as exc:
                    self.status.emit(f"Step error: {exc}")
        self.status.emit("Finished")
        self.done.emit()


# ── recorder ──────────────────────────────────────────────────────────────────

class Recorder(QObject):
    """Captures mouse + keyboard into macro steps. Thread-safe via Qt signals."""
    recording_finished = pyqtSignal(list)   # emits list[step]

    # min pixels moved before recording a move event
    MOVE_THRESHOLD = 3
    # minimum time between recorded move events (seconds)
    MOVE_INTERVAL  = 1 / 60

    def __init__(self):
        super().__init__()
        self._events: list[tuple[float, dict]] = []
        self._start   = 0.0
        self._active  = False
        self._ml      = None   # mouse listener
        self._kl      = None   # keyboard listener
        self._last_move_t   = 0.0
        self._last_pos      = (0, 0)
        self._text_buf      = ""
        self._record_key    = None   # the hotkey — don't record it

    @property
    def active(self):
        return self._active

    def start(self, record_key=None):
        self._events    = []
        self._start     = time.time()
        self._last_move_t = self._start
        self._last_pos  = pag.position()
        self._active    = True
        self._text_buf  = ""
        self._record_key = record_key

        self._ml = _mouse.Listener(
            on_move=self._on_move,
            on_click=self._on_click,
            on_scroll=self._on_scroll,
            daemon=True,
        )
        self._kl = _keyboard.Listener(
            on_press=self._on_key_press,
            daemon=True,
        )
        self._ml.start()
        self._kl.start()

    def stop(self):
        if not self._active:
            return
        self._active = False
        self._flush_text()
        if self._ml: self._ml.stop()
        if self._kl: self._kl.stop()
        steps = self._build_steps()
        self.recording_finished.emit(steps)

    # ── event callbacks (run in pynput threads) ────────────────────────────

    def _now(self) -> float:
        return time.time() - self._start

    def _add(self, step_type: str, params: dict):
        self._events.append((self._now(), {"type": step_type, "params": params}))

    def _on_move(self, x: int, y: int):
        now = time.time()
        if now - self._last_move_t < self.MOVE_INTERVAL:
            return
        if (abs(x - self._last_pos[0]) < self.MOVE_THRESHOLD and
                abs(y - self._last_pos[1]) < self.MOVE_THRESHOLD):
            return
        self._flush_text()
        self._add("move", {"x": x, "y": y, "duration": 0})
        self._last_move_t = now
        self._last_pos = (x, y)

    def _on_click(self, x: int, y: int, button, pressed: bool):
        if not pressed or not self._active:
            return
        self._flush_text()
        step = "right_click" if button == _mouse.Button.right else "click"
        self._add(step, {"x": x, "y": y})

    def _on_scroll(self, x: int, y: int, dx: int, dy: int):
        if not self._active:
            return
        self._flush_text()
        self._add("scroll", {"x": x, "y": y, "amount": int(dy * 3)})

    def _on_key_press(self, key):
        if not self._active:
            return
        # Don't record the toggle key itself
        if self._record_key is not None and key == self._record_key:
            return
        try:
            char = key.char
            if char and char.isprintable():
                self._text_buf += char
                return
        except AttributeError:
            pass
        # Special key
        self._flush_text()
        name = pynput_key_to_pag(key)
        if name:
            self._add("key", {"key": name})

    def _flush_text(self):
        if self._text_buf:
            self._add("type", {"text": self._text_buf, "interval": 0.05})
            self._text_buf = ""

    def _build_steps(self) -> list:
        steps: list[dict] = []
        prev_t = 0.0
        for t, step in self._events:
            gap = round(t - prev_t, 3)
            if gap >= 0.05:
                steps.append({"type": "wait", "params": {"seconds": gap}})
            steps.append(step)
            prev_t = t
        return steps


# ── global hotkey watcher ─────────────────────────────────────────────────────

class HotkeyWatcher(QObject):
    """Single persistent keyboard listener.

    Handles both hotkey toggle detection and one-shot key capture.
    Signals are emitted from the pynput thread but PyQt6 queues them
    safely to the main thread via auto-connection.
    """
    toggled      = pyqtSignal()         # hotkey was pressed
    key_captured = pyqtSignal(object)   # one-shot capture result

    def __init__(self):
        super().__init__()
        self._key       = None   # the configured hotkey (pynput key object)
        self._listener  = None
        self._capturing = False  # True while waiting for a one-shot capture

    def set_key(self, key):
        self._key = key

    def begin_capture(self):
        """Next key press will be emitted via key_captured instead of checked as hotkey."""
        self._capturing = True

    def start(self):
        def on_press(key):
            if self._capturing:
                self._capturing = False
                self.key_captured.emit(key)   # safe: Qt queues cross-thread signals
                return
            if self._key is not None and key == self._key:
                self.toggled.emit()            # safe: same reason
        self._listener = _kb.Listener(on_press=on_press, daemon=True)
        self._listener.start()


# ── step editor dialog ────────────────────────────────────────────────────────

class StepDialog(QDialog):
    def __init__(self, parent, step: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Add Step" if step is None else "Edit Step")
        self.setMinimumWidth(360)
        self._step = step
        layout = QVBoxLayout(self)

        row = QHBoxLayout()
        row.addWidget(QLabel("Type:"))
        self.type_cb = QComboBox()
        for k, lbl in STEP_TYPES:
            self.type_cb.addItem(lbl, k)
        if step:
            idx = [k for k, _ in STEP_TYPES].index(step["type"])
            self.type_cb.setCurrentIndex(idx)
        row.addWidget(self.type_cb, 1)
        layout.addLayout(row)

        self.fields_group = QGroupBox("Parameters")
        self.fields_layout = QFormLayout(self.fields_group)
        layout.addWidget(self.fields_group)
        self._fields: dict = {}

        # Must be created before _rebuild() because _rebuild calls setVisible on it
        self.capture_btn = QPushButton("📍  Use current mouse position")
        self.capture_btn.clicked.connect(self._capture_pos)
        layout.addWidget(self.capture_btn)

        self.type_cb.currentIndexChanged.connect(self._rebuild)
        self._rebuild()

        btns = QHBoxLayout()
        ok = QPushButton("OK"); ok.setDefault(True); ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        btns.addStretch(); btns.addWidget(cancel); btns.addWidget(ok)
        layout.addLayout(btns)

    def _rebuild(self):
        while self.fields_layout.rowCount():
            self.fields_layout.removeRow(0)
        self._fields.clear()

        key = self.type_cb.currentData()
        p = self._step.get("params", {}) if self._step and self._step["type"] == key else default_params(key)

        def spin(val, mn=-9999, mx=9999, dec=0, suffix=""):
            w = QDoubleSpinBox() if dec else QSpinBox()
            w.setRange(mn, mx)
            if dec: w.setDecimals(dec)
            if suffix: w.setSuffix(suffix)
            w.setValue(val)
            return w

        if key in ("move", "click", "right_click", "double_click", "scroll"):
            self._fields["x"] = spin(p.get("x", 0))
            self._fields["y"] = spin(p.get("y", 0))
            self.fields_layout.addRow("X:", self._fields["x"])
            self.fields_layout.addRow("Y:", self._fields["y"])
        if key == "move":
            self._fields["duration"] = spin(p.get("duration", 0.5), 0, 30, dec=3, suffix=" s")
            self.fields_layout.addRow("Duration:", self._fields["duration"])
        if key == "scroll":
            self._fields["amount"] = spin(p.get("amount", 3), -50, 50)
            self.fields_layout.addRow("Amount (+ up / − down):", self._fields["amount"])
        if key == "wait":
            self._fields["seconds"] = spin(p.get("seconds", 1.0), 0, 3600, dec=3, suffix=" s")
            self.fields_layout.addRow("Seconds:", self._fields["seconds"])
        if key == "type":
            self._fields["text"] = QTextEdit()
            self._fields["text"].setPlainText(p.get("text", ""))
            self._fields["text"].setMaximumHeight(100)
            self.fields_layout.addRow("Text:", self._fields["text"])
            self._fields["interval"] = spin(p.get("interval", 0.05), 0, 1, dec=3, suffix=" s")
            self.fields_layout.addRow("Interval per char:", self._fields["interval"])
        if key == "key":
            self._fields["key"] = QLineEdit(p.get("key", ""))
            self._fields["key"].setPlaceholderText("e.g.  enter   ctrl+c   f5")
            self.fields_layout.addRow("Key / combo:", self._fields["key"])

        self.capture_btn.setVisible(key in ("move", "click", "right_click", "double_click", "scroll"))

    def _capture_pos(self):
        x, y = pag.position()
        if "x" in self._fields: self._fields["x"].setValue(x)
        if "y" in self._fields: self._fields["y"].setValue(y)

    def get_step(self) -> dict:
        key = self.type_cb.currentData()
        params = {}
        for name, w in self._fields.items():
            if isinstance(w, QTextEdit):         params[name] = w.toPlainText()
            elif isinstance(w, (QDoubleSpinBox, QSpinBox)): params[name] = w.value()
            elif isinstance(w, QLineEdit):       params[name] = w.text().strip()
        return {"type": key, "params": params}

    class QGroupBox(QGroupBox):  # local alias so QGroupBox is accessible inside
        pass


# bring QGroupBox back into scope (it was shadowed above — fix)
from PyQt6.QtWidgets import QGroupBox


# ── main window ───────────────────────────────────────────────────────────────

class MacroTool(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MacroTool")
        self.resize(900, 640)
        self._macros: list[dict] = []
        self._runner:   Runner   | None = None
        self._recorder: Recorder = Recorder()
        self._watcher:  HotkeyWatcher = HotkeyWatcher()
        self._record_key = None        # current pynput key object
        self._record_key_label = "—"  # human-readable

        self._recorder.recording_finished.connect(self._on_recording_done)
        self._watcher.toggled.connect(self._toggle_recording)
        self._watcher.key_captured.connect(self._on_key_captured)
        self._watcher.start()

        self._build_ui()
        self._load()
        self._start_mouse_tracker()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        # ── LEFT: macro list ──
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QLabel("<b>Macros</b>"))
        self.macro_list = QListWidget()
        self.macro_list.currentRowChanged.connect(self._on_macro_select)
        lv.addWidget(self.macro_list, 1)
        mb = QHBoxLayout()
        for label, slot in [("+ New", self._new_macro), ("Rename", self._rename_macro), ("Delete", self._delete_macro)]:
            b = QPushButton(label); b.clicked.connect(slot); mb.addWidget(b)
        lv.addLayout(mb)
        splitter.addWidget(left)

        # ── RIGHT: step editor ──
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.macro_label = QLabel("<b>Steps</b>")
        rv.addWidget(self.macro_label)
        self.step_list = QListWidget()
        self.step_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.step_list.doubleClicked.connect(self._edit_step)
        self.step_list.model().rowsMoved.connect(self._on_steps_reordered)
        rv.addWidget(self.step_list, 1)

        sb = QHBoxLayout()
        for label, slot in [("+ Add Step", self._add_step), ("Edit", self._edit_step),
                             ("Delete", self._delete_step), ("Duplicate", self._duplicate_step)]:
            b = QPushButton(label); b.clicked.connect(slot); sb.addWidget(b)
        up = QPushButton("↑"); up.setFixedWidth(32); up.clicked.connect(self._move_up)
        dn = QPushButton("↓"); dn.setFixedWidth(32); dn.clicked.connect(self._move_down)
        sb.addWidget(up); sb.addWidget(dn)
        rv.addLayout(sb)

        # ── Record group ──
        rec_group = QGroupBox("Record")
        rec_layout = QHBoxLayout(rec_group)

        rec_layout.addWidget(QLabel("Toggle hotkey:"))
        self.hotkey_label = QLabel("<b>—</b>  (none set)")
        rec_layout.addWidget(self.hotkey_label)

        set_btn = QPushButton("Set Hotkey")
        set_btn.clicked.connect(self._set_hotkey)
        rec_layout.addWidget(set_btn)

        rec_layout.addStretch()

        self.rec_btn = QPushButton("⏺  Record")
        self.rec_btn.setFixedSize(160, 56)
        self.rec_btn.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        self.rec_btn.setStyleSheet(
            "QPushButton { background: #555; color: white; border-radius: 6px; }"
            "QPushButton:hover { background: #d04; }"
        )
        self.rec_btn.clicked.connect(self._toggle_recording)
        rec_layout.addWidget(self.rec_btn)
        rv.addWidget(rec_group)

        # ── Run group ──
        run_group = QGroupBox("Run")
        run_layout = QHBoxLayout(run_group)

        run_layout.addWidget(QLabel("Repeat:"))
        self.repeat_spin = QSpinBox()
        self.repeat_spin.setRange(1, 9999); self.repeat_spin.setValue(1)
        self.repeat_spin.setFixedWidth(70)
        run_layout.addWidget(self.repeat_spin)

        run_layout.addWidget(QLabel("Delay:"))
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0, 30); self.delay_spin.setValue(3)
        self.delay_spin.setSuffix(" s"); self.delay_spin.setFixedWidth(75)
        run_layout.addWidget(self.delay_spin)

        run_layout.addStretch()

        self.run_btn = QPushButton("▶   Run")
        self.run_btn.setFixedSize(160, 56)
        self.run_btn.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self.run_btn.setStyleSheet(
            "QPushButton { background: #00cc88; color: white; border-radius: 6px; }"
            "QPushButton:hover { background: #00aa77; }"
            "QPushButton:disabled { background: #aaa; color: #eee; }"
        )
        self.run_btn.clicked.connect(self._run)

        self.stop_btn = QPushButton("■   Stop")
        self.stop_btn.setFixedSize(160, 56)
        self.stop_btn.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet(
            "QPushButton { background: #e44; color: white; border-radius: 6px; }"
            "QPushButton:hover { background: #c33; }"
            "QPushButton:disabled { background: #ccc; color: #888; }"
        )
        self.stop_btn.clicked.connect(self._stop)

        run_layout.addWidget(self.run_btn)
        run_layout.addWidget(self.stop_btn)
        rv.addWidget(run_group)

        splitter.addWidget(right)
        splitter.setSizes([220, 680])

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.pos_label = QLabel("Mouse: (0, 0)")
        self.pos_label.setStyleSheet("color: #555; padding-right: 12px;")
        self.status_bar.addPermanentWidget(self.pos_label)
        self.status_bar.showMessage("Ready  ·  Move mouse to top-left corner to abort playback")

    # ── recording ─────────────────────────────────────────────────────────────

    def _set_hotkey(self):
        self.hotkey_label.setText("<i>Press any key…</i>")
        self._watcher.begin_capture()   # next key press → key_captured signal

    def _on_key_captured(self, key):
        """Slot — called in the main Qt thread when a key is captured."""
        self._record_key = key
        self._record_key_label = pynput_key_label(key)
        self._watcher.set_key(key)
        self.hotkey_label.setText(f"<b>{self._record_key_label}</b>")
        self._save_settings()

    def _toggle_recording(self):
        if self._recorder.active:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        macro = self._current_macro()
        if not macro:
            self.status_bar.showMessage("Create or select a macro before recording.")
            return
        self.rec_btn.setText("⏹  Stop Recording")
        self.rec_btn.setStyleSheet(
            "QPushButton { background: #cc0022; color: white; border-radius: 6px; }"
            "QPushButton:hover { background: #aa0011; }"
        )
        self.run_btn.setEnabled(False)
        self.status_bar.showMessage(
            f"● Recording into \"{macro['name']}\"  —  "
            f"press {self._record_key_label} again to stop"
            if self._record_key else
            f"● Recording into \"{macro['name']}\"  —  click Stop Recording when done"
        )
        self._recorder.start(record_key=self._record_key)

    def _stop_recording(self):
        self._recorder.stop()   # emits recording_finished → _on_recording_done

    def _on_recording_done(self, steps: list):
        macro = self._current_macro()
        if macro and steps:
            macro["steps"].extend(steps)
            self._refresh_steps(macro)
            self._save()
            self.status_bar.showMessage(
                f"Recorded {len(steps)} step(s) and appended to \"{macro['name']}\"."
            )
        else:
            self.status_bar.showMessage("Recording stopped — no steps captured.")

        self.rec_btn.setText("⏺  Record")
        self.rec_btn.setStyleSheet(
            "QPushButton { background: #555; color: white; border-radius: 6px; }"
            "QPushButton:hover { background: #d04; }"
        )
        self.run_btn.setEnabled(True)

    # ── macro management ──────────────────────────────────────────────────────

    def _current_macro(self) -> dict | None:
        row = self.macro_list.currentRow()
        return self._macros[row] if 0 <= row < len(self._macros) else None

    def _new_macro(self):
        name, ok = QInputDialog.getText(self, "New Macro", "Macro name:")
        if not ok or not name.strip(): return
        macro = {"id": str(uuid.uuid4()), "name": name.strip(), "steps": []}
        self._macros.append(macro)
        self.macro_list.addItem(name.strip())
        self.macro_list.setCurrentRow(len(self._macros) - 1)
        self._save()

    def _rename_macro(self):
        macro = self._current_macro()
        if not macro: return
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=macro["name"])
        if not ok or not name.strip(): return
        macro["name"] = name.strip()
        self.macro_list.currentItem().setText(name.strip())
        self._save()

    def _delete_macro(self):
        macro = self._current_macro()
        if not macro: return
        if QMessageBox.question(self, "Delete", f"Delete \"{macro['name']}\"?") != QMessageBox.StandardButton.Yes:
            return
        row = self.macro_list.currentRow()
        self._macros.pop(row)
        self.macro_list.takeItem(row)
        self._save()

    def _on_macro_select(self, row: int):
        self.step_list.clear()
        macro = self._macros[row] if 0 <= row < len(self._macros) else None
        if macro:
            self.macro_label.setText(f"<b>Steps</b> — {macro['name']}")
            for step in macro["steps"]:
                self.step_list.addItem(describe(step))
        else:
            self.macro_label.setText("<b>Steps</b>")

    # ── step management ───────────────────────────────────────────────────────

    def _add_step(self):
        macro = self._current_macro()
        if not macro:
            QMessageBox.information(self, "No macro", "Select a macro first."); return
        dlg = StepDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted: return
        step = dlg.get_step()
        macro["steps"].append(step)
        self.step_list.addItem(describe(step))
        self._save()

    def _edit_step(self):
        macro = self._current_macro()
        if not macro: return
        row = self.step_list.currentRow()
        if row < 0 or row >= len(macro["steps"]): return
        dlg = StepDialog(self, macro["steps"][row])
        if dlg.exec() != QDialog.DialogCode.Accepted: return
        macro["steps"][row] = dlg.get_step()
        self.step_list.item(row).setText(describe(macro["steps"][row]))
        self._save()

    def _delete_step(self):
        macro = self._current_macro()
        if not macro: return
        row = self.step_list.currentRow()
        if row < 0: return
        macro["steps"].pop(row)
        self.step_list.takeItem(row)
        self._save()

    def _duplicate_step(self):
        macro = self._current_macro()
        if not macro: return
        row = self.step_list.currentRow()
        if row < 0: return
        step = copy.deepcopy(macro["steps"][row])
        macro["steps"].insert(row + 1, step)
        self.step_list.insertItem(row + 1, describe(step))
        self.step_list.setCurrentRow(row + 1)
        self._save()

    def _move_up(self):
        macro = self._current_macro()
        if not macro: return
        row = self.step_list.currentRow()
        if row <= 0: return
        macro["steps"].insert(row - 1, macro["steps"].pop(row))
        self._refresh_steps(macro, row - 1)
        self._save()

    def _move_down(self):
        macro = self._current_macro()
        if not macro: return
        row = self.step_list.currentRow()
        if row < 0 or row >= len(macro["steps"]) - 1: return
        macro["steps"].insert(row + 1, macro["steps"].pop(row))
        self._refresh_steps(macro, row + 1)
        self._save()

    def _on_steps_reordered(self, *_):
        macro = self._current_macro()
        if not macro: return
        new_order = []
        for i in range(self.step_list.count()):
            text = self.step_list.item(i).text()
            match = next((s for s in macro["steps"] if describe(s) == text), None)
            if match: new_order.append(match)
        macro["steps"] = new_order
        self._save()

    def _refresh_steps(self, macro: dict, select_row: int = -1):
        self.step_list.clear()
        for step in macro["steps"]:
            self.step_list.addItem(describe(step))
        if select_row >= 0:
            self.step_list.setCurrentRow(select_row)

    # ── playback ──────────────────────────────────────────────────────────────

    def _run(self):
        macro = self._current_macro()
        if not macro:
            QMessageBox.information(self, "No macro", "Select a macro to run."); return
        if not macro["steps"]:
            QMessageBox.information(self, "Empty", "Add at least one step."); return
        self._runner = Runner(macro["steps"], self.repeat_spin.value(), self.delay_spin.value())
        self._runner.status.connect(self.status_bar.showMessage)
        self._runner.done.connect(self._on_run_done)
        self.run_btn.setEnabled(False); self.stop_btn.setEnabled(True)
        self._runner.start()

    def _stop(self):
        if self._runner: self._runner.stop()

    def _on_run_done(self):
        self.run_btn.setEnabled(True); self.stop_btn.setEnabled(False)
        self._runner = None

    # ── mouse position tracker ────────────────────────────────────────────────

    def _start_mouse_tracker(self):
        t = QTimer(self); t.timeout.connect(self._update_pos); t.start(16)  # ~60 Hz

    def _update_pos(self):
        x, y = pag.position()
        self.pos_label.setText(f"Mouse: ({x}, {y})")

    # ── persistence ───────────────────────────────────────────────────────────

    def _save(self):
        SAVE_FILE.write_text(json.dumps(self._macros, indent=2), encoding="utf-8")

    def _load(self):
        if SAVE_FILE.exists():
            try: self._macros = json.loads(SAVE_FILE.read_text(encoding="utf-8"))
            except Exception: self._macros = []
        for macro in self._macros:
            self.macro_list.addItem(macro.get("name", "Macro"))
        if self._macros:
            self.macro_list.setCurrentRow(0)
        self._load_settings()

    def _save_settings(self):
        SETTINGS_FILE.write_text(
            json.dumps({"record_key_label": self._record_key_label}, indent=2),
            encoding="utf-8"
        )

    def _load_settings(self):
        # Hotkey object can't be serialised; just restore the label so UI shows it
        if SETTINGS_FILE.exists():
            try:
                s = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                lbl = s.get("record_key_label", "—")
                if lbl and lbl != "—":
                    self._record_key_label = lbl
                    self.hotkey_label.setText(f"<b>{lbl}</b>  (re-set after restart to activate)")
            except Exception:
                pass


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MacroTool()
    win.show()
    sys.exit(app.exec())
