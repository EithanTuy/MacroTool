"""
MacroTool — build and run mouse/keyboard macro sequences
"""

import sys
import json
import time
import uuid
import threading
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QListWidget, QListWidgetItem, QPushButton, QLabel, QDialog,
    QFormLayout, QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox,
    QSplitter, QStatusBar, QGroupBox, QAbstractItemView,
    QMessageBox, QInputDialog, QTextEdit, QCheckBox, QFrame,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QFont, QColor

import pyautogui
import pyautogui as pag

pag.FAILSAFE = True  # move mouse to top-left corner to abort

SAVE_FILE = Path(__file__).parent / "macros.json"

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

# ── helpers ──────────────────────────────────────────────────────────────────

def describe(step: dict) -> str:
    t = step["type"]
    p = step.get("params", {})
    if t == "move":
        return f"Move  ({p.get('x', 0)}, {p.get('y', 0)})  in {p.get('duration', 0.5):.2f}s"
    if t == "click":
        return f"Click  ({p.get('x', 0)}, {p.get('y', 0)})"
    if t == "right_click":
        return f"Right-click  ({p.get('x', 0)}, {p.get('y', 0)})"
    if t == "double_click":
        return f"Double-click  ({p.get('x', 0)}, {p.get('y', 0)})"
    if t == "wait":
        return f"Wait  {p.get('seconds', 1.0):.2f}s"
    if t == "type":
        txt = p.get("text", "")
        return f'Type  "{txt[:50]}{"…" if len(txt) > 50 else ""}"'
    if t == "key":
        return f"Key  {p.get('key', '')}"
    if t == "scroll":
        amt = p.get("amount", 3)
        return f"Scroll {'↑' if amt > 0 else '↓'}{abs(amt)}  at ({p.get('x', 0)}, {p.get('y', 0)})"
    return t


def run_step(step: dict):
    t = step["type"]
    p = step.get("params", {})
    if t == "move":
        pag.moveTo(p.get("x", 0), p.get("y", 0), duration=p.get("duration", 0.5))
    elif t == "click":
        pag.click(p.get("x", 0), p.get("y", 0))
    elif t == "right_click":
        pag.rightClick(p.get("x", 0), p.get("y", 0))
    elif t == "double_click":
        pag.doubleClick(p.get("x", 0), p.get("y", 0))
    elif t == "wait":
        time.sleep(p.get("seconds", 1.0))
    elif t == "type":
        pag.typewrite(p.get("text", ""), interval=p.get("interval", 0.05))
    elif t == "key":
        raw = p.get("key", "")
        if "+" in raw:
            pag.hotkey(*[k.strip() for k in raw.split("+")])
        else:
            pag.press(raw)
    elif t == "scroll":
        pag.scroll(p.get("amount", 3), x=p.get("x", 0), y=p.get("y", 0))


def default_params(step_type: str) -> dict:
    x, y = pag.position()
    coords = {"x": x, "y": y}
    defaults = {
        "move":         {**coords, "duration": 0.5},
        "click":        coords,
        "right_click":  coords,
        "double_click": coords,
        "wait":         {"seconds": 1.0},
        "type":         {"text": "", "interval": 0.05},
        "key":          {"key": ""},
        "scroll":       {**coords, "amount": 3},
    }
    return defaults.get(step_type, {})


# ── macro runner thread ───────────────────────────────────────────────────────

class Runner(QThread):
    status = pyqtSignal(str)
    done   = pyqtSignal()

    def __init__(self, steps: list, repeat: int = 1, delay: float = 3.0):
        super().__init__()
        self.steps  = steps
        self.repeat = repeat
        self.delay  = delay
        self._stop  = False

    def stop(self):
        self._stop = True

    def run(self):
        for i in range(int(self.delay), 0, -1):
            if self._stop:
                self.done.emit()
                return
            self.status.emit(f"Starting in {i}…")
            time.sleep(1)
        for rep in range(self.repeat):
            if self._stop:
                break
            for idx, step in enumerate(self.steps):
                if self._stop:
                    break
                self.status.emit(
                    f"Run {rep + 1}/{self.repeat}  ·  Step {idx + 1}/{len(self.steps)}: {describe(step)}"
                )
                try:
                    run_step(step)
                except pag.FailSafeException:
                    self.status.emit("Aborted — mouse moved to corner")
                    self.done.emit()
                    return
                except Exception as exc:
                    self.status.emit(f"Step error: {exc}")
        self.status.emit("Finished")
        self.done.emit()


# ── step editor dialog ────────────────────────────────────────────────────────

class StepDialog(QDialog):
    def __init__(self, parent, step: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Add Step" if step is None else "Edit Step")
        self.setMinimumWidth(360)
        self._step = step

        layout = QVBoxLayout(self)

        # type selector
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Type:"))
        self.type_cb = QComboBox()
        for key, label in STEP_TYPES:
            self.type_cb.addItem(label, key)
        if step:
            idx = [k for k, _ in STEP_TYPES].index(step["type"])
            self.type_cb.setCurrentIndex(idx)
        type_row.addWidget(self.type_cb, 1)
        layout.addLayout(type_row)

        # dynamic fields area
        self.fields_group = QGroupBox("Parameters")
        self.fields_layout = QFormLayout(self.fields_group)
        layout.addWidget(self.fields_group)

        self._fields: dict = {}
        self.type_cb.currentIndexChanged.connect(self._rebuild_fields)
        self._rebuild_fields()

        # capture position helper
        self.capture_btn = QPushButton("📍  Use current mouse position")
        self.capture_btn.clicked.connect(self._capture_pos)
        layout.addWidget(self.capture_btn)

        # OK / Cancel
        btn_row = QHBoxLayout()
        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(cancel)
        btn_row.addWidget(ok)
        layout.addLayout(btn_row)

    def _rebuild_fields(self):
        # clear old widgets
        while self.fields_layout.rowCount():
            self.fields_layout.removeRow(0)
        self._fields.clear()

        key = self.type_cb.currentData()
        p = self._step.get("params", {}) if self._step and self._step["type"] == key else default_params(key)

        def spin(val, mn=-9999, mx=9999, dec=0, suffix=""):
            w = QDoubleSpinBox() if dec else QSpinBox()
            w.setRange(mn, mx)
            if dec:
                w.setDecimals(dec)
            if suffix:
                w.setSuffix(suffix)
            w.setValue(val)
            return w

        if key in ("move", "click", "right_click", "double_click", "scroll"):
            self._fields["x"] = spin(p.get("x", 0))
            self._fields["y"] = spin(p.get("y", 0))
            self.fields_layout.addRow("X:", self._fields["x"])
            self.fields_layout.addRow("Y:", self._fields["y"])

        if key == "move":
            self._fields["duration"] = spin(p.get("duration", 0.5), 0, 30, dec=2, suffix=" s")
            self.fields_layout.addRow("Duration:", self._fields["duration"])

        if key == "scroll":
            self._fields["amount"] = spin(p.get("amount", 3), -50, 50)
            self.fields_layout.addRow("Amount (+ up / − down):", self._fields["amount"])

        if key == "wait":
            self._fields["seconds"] = spin(p.get("seconds", 1.0), 0, 3600, dec=2, suffix=" s")
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
            self._fields["key"].setPlaceholderText("e.g.  enter   ctrl+c   f5   win")
            self.fields_layout.addRow("Key / combo:", self._fields["key"])

        self.capture_btn.setVisible(key in ("move", "click", "right_click", "double_click", "scroll"))

    def _capture_pos(self):
        x, y = pag.position()
        if "x" in self._fields:
            self._fields["x"].setValue(x)
        if "y" in self._fields:
            self._fields["y"].setValue(y)

    def get_step(self) -> dict:
        key = self.type_cb.currentData()
        params = {}
        for name, widget in self._fields.items():
            if isinstance(widget, QTextEdit):
                params[name] = widget.toPlainText()
            elif isinstance(widget, (QDoubleSpinBox, QSpinBox)):
                params[name] = widget.value()
            elif isinstance(widget, QLineEdit):
                params[name] = widget.text().strip()
        return {"type": key, "params": params}


# ── main window ───────────────────────────────────────────────────────────────

class MacroTool(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MacroTool")
        self.resize(860, 580)
        self._macros: list[dict] = []   # [{id, name, steps}]
        self._runner: Runner | None = None

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

        macro_btns = QHBoxLayout()
        self.new_btn = QPushButton("+ New")
        self.new_btn.clicked.connect(self._new_macro)
        self.rename_btn = QPushButton("Rename")
        self.rename_btn.clicked.connect(self._rename_macro)
        self.del_macro_btn = QPushButton("Delete")
        self.del_macro_btn.clicked.connect(self._delete_macro)
        macro_btns.addWidget(self.new_btn)
        macro_btns.addWidget(self.rename_btn)
        macro_btns.addWidget(self.del_macro_btn)
        lv.addLayout(macro_btns)

        splitter.addWidget(left)

        # ── RIGHT: step editor ──
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)

        self.macro_label = QLabel("<b>Steps</b>")
        rv.addWidget(self.macro_label)

        self.step_list = QListWidget()
        self.step_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.step_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.step_list.doubleClicked.connect(self._edit_step)
        self.step_list.model().rowsMoved.connect(self._on_steps_reordered)
        rv.addWidget(self.step_list, 1)

        step_btns = QHBoxLayout()
        add_btn = QPushButton("+ Add Step")
        add_btn.clicked.connect(self._add_step)
        edit_btn = QPushButton("Edit")
        edit_btn.clicked.connect(self._edit_step)
        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(self._delete_step)
        up_btn = QPushButton("↑")
        up_btn.setFixedWidth(32)
        up_btn.clicked.connect(self._move_step_up)
        down_btn = QPushButton("↓")
        down_btn.setFixedWidth(32)
        down_btn.clicked.connect(self._move_step_down)
        dup_btn = QPushButton("Duplicate")
        dup_btn.clicked.connect(self._duplicate_step)
        for w in (add_btn, edit_btn, del_btn, up_btn, down_btn, dup_btn):
            step_btns.addWidget(w)
        rv.addLayout(step_btns)

        # run controls
        run_group = QGroupBox("Run")
        run_layout = QHBoxLayout(run_group)

        run_layout.addWidget(QLabel("Repeat:"))
        self.repeat_spin = QSpinBox()
        self.repeat_spin.setRange(1, 9999)
        self.repeat_spin.setValue(1)
        self.repeat_spin.setFixedWidth(70)
        run_layout.addWidget(self.repeat_spin)

        run_layout.addWidget(QLabel("Delay:"))
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0, 30)
        self.delay_spin.setValue(3)
        self.delay_spin.setSuffix(" s")
        self.delay_spin.setFixedWidth(75)
        run_layout.addWidget(self.delay_spin)

        self.run_btn = QPushButton("▶   Run")
        self.run_btn.setFixedSize(160, 56)
        self.run_btn.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self.run_btn.setStyleSheet(
            "QPushButton { background: #00cc88; color: white; font-weight: bold; border-radius: 6px; }"
            "QPushButton:hover { background: #00aa77; }"
            "QPushButton:disabled { background: #aaa; }"
        )
        self.run_btn.clicked.connect(self._run)

        self.stop_btn = QPushButton("■   Stop")
        self.stop_btn.setFixedSize(160, 56)
        self.stop_btn.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet(
            "QPushButton { background: #e44; color: white; font-weight: bold; border-radius: 6px; }"
            "QPushButton:hover { background: #c33; }"
            "QPushButton:disabled { background: #ccc; color: #888; }"
        )
        self.stop_btn.clicked.connect(self._stop)

        run_layout.addStretch()
        run_layout.addWidget(self.run_btn)
        run_layout.addWidget(self.stop_btn)
        rv.addWidget(run_group)

        splitter.addWidget(right)
        splitter.setSizes([220, 640])

        # status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.pos_label = QLabel("Mouse: (0, 0)")
        self.pos_label.setStyleSheet("color: #555; padding-right: 12px;")
        self.status_bar.addPermanentWidget(self.pos_label)
        self.status_bar.showMessage("Ready  ·  Move mouse to top-left corner to abort a running macro")

    # ── macro management ──────────────────────────────────────────────────────

    def _current_macro(self) -> dict | None:
        row = self.macro_list.currentRow()
        return self._macros[row] if 0 <= row < len(self._macros) else None

    def _new_macro(self):
        name, ok = QInputDialog.getText(self, "New Macro", "Macro name:")
        if not ok or not name.strip():
            return
        macro = {"id": str(uuid.uuid4()), "name": name.strip(), "steps": []}
        self._macros.append(macro)
        self.macro_list.addItem(name.strip())
        self.macro_list.setCurrentRow(len(self._macros) - 1)
        self._save()

    def _rename_macro(self):
        macro = self._current_macro()
        if not macro:
            return
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=macro["name"])
        if not ok or not name.strip():
            return
        macro["name"] = name.strip()
        self.macro_list.currentItem().setText(name.strip())
        self._save()

    def _delete_macro(self):
        macro = self._current_macro()
        if not macro:
            return
        if QMessageBox.question(self, "Delete", f"Delete macro \"{macro['name']}\"?") != QMessageBox.StandardButton.Yes:
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
            QMessageBox.information(self, "No macro", "Create or select a macro first.")
            return
        dlg = StepDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        step = dlg.get_step()
        macro["steps"].append(step)
        self.step_list.addItem(describe(step))
        self._save()

    def _edit_step(self):
        macro = self._current_macro()
        if not macro:
            return
        row = self.step_list.currentRow()
        if row < 0 or row >= len(macro["steps"]):
            return
        dlg = StepDialog(self, macro["steps"][row])
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        macro["steps"][row] = dlg.get_step()
        self.step_list.item(row).setText(describe(macro["steps"][row]))
        self._save()

    def _delete_step(self):
        macro = self._current_macro()
        if not macro:
            return
        row = self.step_list.currentRow()
        if row < 0:
            return
        macro["steps"].pop(row)
        self.step_list.takeItem(row)
        self._save()

    def _move_step_up(self):
        macro = self._current_macro()
        if not macro:
            return
        row = self.step_list.currentRow()
        if row <= 0:
            return
        macro["steps"].insert(row - 1, macro["steps"].pop(row))
        self._refresh_steps(macro, row - 1)
        self._save()

    def _move_step_down(self):
        macro = self._current_macro()
        if not macro:
            return
        row = self.step_list.currentRow()
        if row < 0 or row >= len(macro["steps"]) - 1:
            return
        macro["steps"].insert(row + 1, macro["steps"].pop(row))
        self._refresh_steps(macro, row + 1)
        self._save()

    def _duplicate_step(self):
        macro = self._current_macro()
        if not macro:
            return
        row = self.step_list.currentRow()
        if row < 0:
            return
        import copy
        step = copy.deepcopy(macro["steps"][row])
        macro["steps"].insert(row + 1, step)
        self.step_list.insertItem(row + 1, describe(step))
        self.step_list.setCurrentRow(row + 1)
        self._save()

    def _on_steps_reordered(self, *_):
        macro = self._current_macro()
        if not macro:
            return
        new_order = []
        for i in range(self.step_list.count()):
            text = self.step_list.item(i).text()
            match = next((s for s in macro["steps"] if describe(s) == text), None)
            if match:
                new_order.append(match)
        macro["steps"] = new_order
        self._save()

    def _refresh_steps(self, macro: dict, select_row: int = -1):
        self.step_list.clear()
        for step in macro["steps"]:
            self.step_list.addItem(describe(step))
        if select_row >= 0:
            self.step_list.setCurrentRow(select_row)

    # ── run ───────────────────────────────────────────────────────────────────

    def _run(self):
        macro = self._current_macro()
        if not macro:
            QMessageBox.information(self, "No macro", "Select a macro to run.")
            return
        if not macro["steps"]:
            QMessageBox.information(self, "Empty macro", "Add at least one step first.")
            return
        self._runner = Runner(
            macro["steps"],
            repeat=self.repeat_spin.value(),
            delay=self.delay_spin.value()
        )
        self._runner.status.connect(self.status_bar.showMessage)
        self._runner.done.connect(self._on_run_done)
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._runner.start()

    def _stop(self):
        if self._runner:
            self._runner.stop()

    def _on_run_done(self):
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._runner = None

    # ── mouse tracker ─────────────────────────────────────────────────────────

    def _start_mouse_tracker(self):
        timer = QTimer(self)
        timer.timeout.connect(self._update_mouse_pos)
        timer.start(100)

    def _update_mouse_pos(self):
        x, y = pag.position()
        self.pos_label.setText(f"Mouse: ({x}, {y})")

    # ── persistence ───────────────────────────────────────────────────────────

    def _save(self):
        SAVE_FILE.write_text(json.dumps(self._macros, indent=2), encoding="utf-8")

    def _load(self):
        if SAVE_FILE.exists():
            try:
                self._macros = json.loads(SAVE_FILE.read_text(encoding="utf-8"))
            except Exception:
                self._macros = []
        for macro in self._macros:
            self.macro_list.addItem(macro.get("name", "Macro"))
        if self._macros:
            self.macro_list.setCurrentRow(0)


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MacroTool()
    win.show()
    sys.exit(app.exec())
