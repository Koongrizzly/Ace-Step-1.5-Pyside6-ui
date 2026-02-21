#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ace-Step 1.5 — PySide6 UI (no terminal needed)

This UI wraps the ACE-Step cli.py you provided by generating a TOML config and running:
    <env_python> <path_to_cli_py> -c <generated_config.toml>

It streams stdout/stderr into the UI and lists recent output audio files.

No external `toml` dependency: this file writes a simple flat TOML itself.

Windows venv default:
    <FrameVisionRoot>/environments/.ace_15/Scripts/python.exe
"""

from __future__ import annotations

# --- console log silencer (Qt/FFmpeg + Python stderr/stdout) ---
# Hides QtMultimedia/FFmpeg spam (e.g. mp3float timestamp warnings) and any other console noise.
# Does NOT affect subprocess capturing used for the UI, because that reads from pipes directly.
import os as _os, sys as _sys
_os.environ.setdefault('QT_LOGGING_RULES', '*=false')
try:
    _devnull = open(_os.devnull, 'w', encoding='utf-8', errors='ignore')
    _sys.stdout = _devnull
    _sys.stderr = _devnull
    try:
        _os.dup2(_devnull.fileno(), 1)
        _os.dup2(_devnull.fileno(), 2)
    except Exception:
        pass
except Exception:
    pass
# --- end silencer ---


import os
import sys
import time
import io
import json
import re
import random
import shlex
import subprocess
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List

from PySide6 import QtCore, QtGui, QtWidgets
try:
    from PySide6 import QtMultimedia
except Exception:  # QtMultimedia may be missing on some minimal installs
    QtMultimedia = None  # type: ignore


_fv_no_wheel_patched = False


def _fv_patch_wheel_event(cls):
    """Patch wheelEvent on a Qt widget class to always ignore the wheel.

    FrameVision-style wheel guard: sliders/spinboxes/combos never change via mouse
    wheel, so users can scroll the UI without accidental edits.

    This patch is reversible via uninstall_no_wheel_guard().
    """
    if getattr(cls, "_fv_wheel_guard_patched", False):
        return

    # Save original (once) so we can restore later.
    if not hasattr(cls, "_fv_wheel_guard_orig_wheelEvent"):
        try:
            cls._fv_wheel_guard_orig_wheelEvent = cls.wheelEvent
        except Exception:
            cls._fv_wheel_guard_orig_wheelEvent = None

    def _wheel_event(self, event):
        # Do not change the widget's value via mouse wheel.
        # Ignore so any parent scroll area can scroll instead.
        try:
            event.ignore()
        except Exception:
            pass

    cls.wheelEvent = _wheel_event
    cls._fv_wheel_guard_patched = True


def install_no_wheel_guard():
    """Enable wheel guard (disable mouse wheel changes for sliders/spinboxes/combos)."""
    global _fv_no_wheel_patched
    if _fv_no_wheel_patched:
        return
    _fv_no_wheel_patched = True

    # Patch both abstract bases and common concrete widgets.
    # Some Qt widgets override wheelEvent on the concrete class (e.g. QSpinBox/
    # QDoubleSpinBox), so patching only the abstract base won't take effect
    # when toggling live.
    for cls in (
        QtWidgets.QAbstractSlider,
        QtWidgets.QSlider,
        QtWidgets.QScrollBar,
        QtWidgets.QDial,
        QtWidgets.QAbstractSpinBox,
        QtWidgets.QSpinBox,
        QtWidgets.QDoubleSpinBox,
        QtWidgets.QComboBox,
    ):
        try:
            _fv_patch_wheel_event(cls)
        except Exception:
            # Never break startup because of a patch failure
            continue


def uninstall_no_wheel_guard():
    """Disable wheel guard (restore default mouse wheel behavior)."""
    global _fv_no_wheel_patched
    if not _fv_no_wheel_patched:
        return

    for cls in (
        QtWidgets.QAbstractSlider,
        QtWidgets.QSlider,
        QtWidgets.QScrollBar,
        QtWidgets.QDial,
        QtWidgets.QAbstractSpinBox,
        QtWidgets.QSpinBox,
        QtWidgets.QDoubleSpinBox,
        QtWidgets.QComboBox,
    ):
        try:
            if getattr(cls, "_fv_wheel_guard_patched", False):
                orig = getattr(cls, "_fv_wheel_guard_orig_wheelEvent", None)
                if orig is not None:
                    cls.wheelEvent = orig
                cls._fv_wheel_guard_patched = False
        except Exception:
            continue

    _fv_no_wheel_patched = False


# Themes (shared with the main app)
try:
    from .themes import apply_theme, list_themes
except Exception:
    try:
        from themes import apply_theme, list_themes  # type: ignore
    except Exception:
        apply_theme = None  # type: ignore
        list_themes = None  # type: ignore

APP_TITLE = "Ace-Step 1.5 — Music Creator"
SETTINGS_JSON = "ace_step_15_ui.settings.json"
SETTINGS_PATH_POINTER_JSON = "ace_step_15_ui.settings_path.json"  # remembers last settings file location

# Persist queued jobs so they can resume after app restart.
ACE15_QUEUE_JSON = "ace_step_15_ui.queue.json"


# Preset Manager
ACE15_PRESET_MANAGER_JSON = "presetmanager.json"


# ACE-Step 1.5 diffusion inference methods (per upstream docs / Gradio UI)
# Gradio labels these as ODE and SDE. The CLI expects "ode" or "sde".
ACE15_INFER_METHOD_AUTO = ""   # empty/auto = let ACE decide
ACE15_INFER_METHOD_ODE = "ode"
ACE15_INFER_METHOD_SDE = "sde"


def _ace15_normalize_infer_method(v: str) -> str:
    """Normalize legacy UI values to ACE-Step 1.5 supported infer_method values.

    Older builds exposed k-diffusion sampler names (euler/heun/ddim/dpmpp_*) which are
    not valid for ACE-Step 1.5 and can produce broken output. Upstream Gradio uses:
      - ode (deterministic / Euler)
      - sde (stochastic)
    """
    s = (v or "").strip().lower()
    if not s or s == "auto":
        return ""
    if s in {"ode", "euler"}:
        return ACE15_INFER_METHOD_ODE
    if s in {"sde", "dpmpp_sde"}:
        return ACE15_INFER_METHOD_SDE
    # Legacy sampler names – map to deterministic ODE to avoid broken runs.
    if s in {"ddim", "heun", "dpmpp_2m", "dpmpp"}:
        return ACE15_INFER_METHOD_ODE
    return s




def guess_framevision_root() -> Path:
    """Best-effort guess for the FrameVision root folder.

    Strategy:
    - If this UI is inside <root>/helpers/, return its parent.
    - Otherwise, walk upwards until we find a folder that looks like a FrameVision root
      (contains 'models' and 'environments' folders).
    - Fallback: current folder.
    """
    here = Path(__file__).resolve().parent
    if here.name.lower() == "helpers":
        return here.parent

    cur = here
    for _ in range(8):
        try:
            if (cur / "models").exists() and (cur / "environments").exists():
                return cur
        except Exception:
            pass
        if cur.parent == cur:
            break
        cur = cur.parent
    return here


def settings_path_for_root(framevision_root: Path) -> Path:
    return framevision_root / "presets" / "setsave" / SETTINGS_JSON


def queue_path_for_root(framevision_root: Path) -> Path:
    """Queue persistence file stored under presets/setsave."""
    return framevision_root / "presets" / "setsave" / ACE15_QUEUE_JSON


def preset_manager_path_for_root(framevision_root: Path) -> Path:
    return framevision_root / "presets" / "setsave" / "ace15presets" / ACE15_PRESET_MANAGER_JSON

def settings_pointer_path(framevision_root: Path) -> Path:
    """Pointer file stored under presets/setsave (so nothing is written into helpers)."""
    return framevision_root / "presets" / "setsave" / SETTINGS_PATH_POINTER_JSON


def read_last_settings_path(framevision_root: Path) -> Optional[Path]:
    try:
        p = settings_pointer_path(framevision_root)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        sp = str(data.get("settings_path") or "").strip()
        if not sp:
            return None
        spath = Path(sp)
        return spath if spath.exists() else None
    except Exception:
        return None


def write_last_settings_path(framevision_root: Path, settings_path: Path) -> None:
    try:
        p = settings_pointer_path(framevision_root)
        ensure_dir(p.parent)
        p.write_text(json.dumps({"settings_path": str(settings_path)}, indent=2), encoding="utf-8")
    except Exception:
        pass




def _ace15_default_preset_manager_data() -> dict:
    """Default starter presets so the manager shows something on first launch."""
    return {
        "version": 1,
        "genres": {
            "EDM": {
                "subgenres": {
                    "Progressive House": {
                        "backend": "vllm",
                        "device": "auto",
                        "caption": "Uplifting progressive house anthem at 128 BPM with bright plucks, driving four-on-the-floor kick, rolling bass, and airy female vocal chops. Big build with risers into a euphoric drop. Clean club mix, wide stereo pads, punchy drums.",
                        "timesignature": "4",
                        "thinking": False,
                        "use_cot_caption": False,
                        "use_cot_language": False,
                        "use_cot_metas": False,
                        "main_model_path": "acestep-v15-base",
                        "lm_model_path": "acestep-5Hz-lm-4B",
                        "instrumental": False,
                        "use_cot_lyrics": False,
                        "lm_negative_prompt": "lofi, jazz, acoustic, orchestral, ambient drone",
                    },
                    "Deep House": {
                        "backend": "vllm",
                        "device": "auto",
                        "caption": "Warm deep house groove at 122 BPM with round sub-bass, shuffled hats, silky Rhodes chords, subtle vinyl texture, and a late-night underground vibe. Minimal vocals, tasteful reverb, tight mix.",
                        "timesignature": "4",
                        "thinking": False,
                        "use_cot_caption": False,
                        "use_cot_language": False,
                        "use_cot_metas": False,
                        "main_model_path": "acestep-v15-base",
                        "lm_model_path": "acestep-5Hz-lm-4B",
                        "instrumental": False,
                        "use_cot_lyrics": False,
                        "lm_negative_prompt": "trap, dubstep, heavy distortion, screech leads",
                    },
                }
            },
            "Rock": {
                "subgenres": {
                    "Alt Rock": {
                        "backend": "vllm",
                        "device": "auto",
                        "caption": "Energetic alternative rock track with crunchy guitars, steady live drums, and a catchy hook. Modern mix, tight low end, wide choruses, slightly gritty male vocal.",
                        "timesignature": "4",
                        "thinking": True,
                        "use_cot_caption": True,
                        "use_cot_language": True,
                        "use_cot_metas": True,
                        "main_model_path": "acestep-v15-base",
                        "lm_model_path": "acestep-5Hz-lm-4B",
                        "instrumental": False,
                        "use_cot_lyrics": False,
                        "lm_negative_prompt": "EDM, four-on-the-floor club kick, synthwave",
                    }
                }
            },
            "Reggae": {
                "subgenres": {
                    "Roots Reggae": {
                        "backend": "vllm",
                        "device": "auto",
                        "caption": "Classic roots reggae groove with offbeat guitar skank, warm organ bubble, steady one-drop drum feel, and deep melodic bass. Positive conscious vibe, sunny mix, relaxed tempo.",
                        "timesignature": "4",
                        "thinking": False,
                        "use_cot_caption": False,
                        "use_cot_language": False,
                        "use_cot_metas": False,
                        "main_model_path": "acestep-v15-base",
                        "lm_model_path": "acestep-5Hz-lm-4B",
                        "instrumental": False,
                        "use_cot_lyrics": False,
                        "lm_negative_prompt": "metal, harsh screaming, aggressive distortion",
                    }
                }
            },
        },
    }


class PresetManagerDialog(QtWidgets.QDialog):
    """Ace-Step 1.5 Preset Manager popup."""

    def __init__(self, parent: "MainWindow", presets_path: Path):
        super().__init__(parent)
        self.setWindowTitle("Ace-Step 1.5 — Preset Manager")
        self.resize(980, 560)
        self._mw = parent
        self._path = presets_path
        self._data = self._mw._ace15_preset_mgr_load(self._path)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        root.addWidget(splitter, 1)

        # Left: genres
        left = QtWidgets.QWidget()
        lyt_l = QtWidgets.QVBoxLayout(left)
        lyt_l.setContentsMargins(0, 0, 0, 0)
        lyt_l.setSpacing(6)
        lyt_l.addWidget(QtWidgets.QLabel("Genres"))
        self.lst_genres = QtWidgets.QListWidget()
        self.lst_genres.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        lyt_l.addWidget(self.lst_genres, 1)
        splitter.addWidget(left)

        # Right: subgenres/presets
        right = QtWidgets.QWidget()
        lyt_r = QtWidgets.QVBoxLayout(right)
        lyt_r.setContentsMargins(0, 0, 0, 0)
        lyt_r.setSpacing(6)
        self.lbl_right = QtWidgets.QLabel("Subgenres / Presets")
        lyt_r.addWidget(self.lbl_right)
        self.lst_presets = QtWidgets.QListWidget()
        self.lst_presets.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.lst_presets.itemDoubleClicked.connect(self._apply_selected_preset)
        lyt_r.addWidget(self.lst_presets, 1)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        # Bottom buttons
        row = QtWidgets.QHBoxLayout()
        self.btn_add = QtWidgets.QPushButton("Add to presets")
        self.btn_remove = QtWidgets.QPushButton("Remove from presets")
        self.btn_edit = QtWidgets.QPushButton("Edit preset")
        self.btn_close = QtWidgets.QPushButton("Close")
        self.btn_add.clicked.connect(self._add_preset)
        self.btn_remove.clicked.connect(self._remove_preset)
        self.btn_edit.clicked.connect(self._edit_preset)
        self.btn_close.clicked.connect(self.accept)
        row.addWidget(self.btn_add)
        row.addWidget(self.btn_remove)
        row.addWidget(self.btn_edit)
        row.addStretch(1)
        row.addWidget(self.btn_close)
        root.addLayout(row)

        self.lst_genres.currentItemChanged.connect(self._refresh_right)
        self._refresh_left()
        self._select_all()

    def _select_all(self):
        for i in range(self.lst_genres.count()):
            it = self.lst_genres.item(i)
            if it and it.data(QtCore.Qt.UserRole) == "__all__":
                self.lst_genres.setCurrentItem(it)
                return

    def _genres_dict(self) -> dict:
        return dict((self._data or {}).get("genres") or {})

    def _refresh_left(self):
        self.lst_genres.blockSignals(True)
        self.lst_genres.clear()
        it_all = QtWidgets.QListWidgetItem("All")
        it_all.setData(QtCore.Qt.UserRole, "__all__")
        self.lst_genres.addItem(it_all)
        for g in sorted(self._genres_dict().keys(), key=lambda s: s.lower()):
            it = QtWidgets.QListWidgetItem(g)
            it.setData(QtCore.Qt.UserRole, g)
            self.lst_genres.addItem(it)
        self.lst_genres.blockSignals(False)

    def _selected_genre_key(self) -> str:
        it = self.lst_genres.currentItem()
        if not it:
            return "__all__"
        return str(it.data(QtCore.Qt.UserRole) or "__all__")

    def _refresh_right(self):
        self.lst_presets.clear()
        gk = self._selected_genre_key()
        genres = self._genres_dict()

        if gk == "__all__":
            self.lbl_right.setText("Subgenres / Presets (All)")
            rows = []
            for g, gd in genres.items():
                subs = (gd or {}).get("subgenres") or {}
                for sname in subs.keys():
                    rows.append((g, sname))
            for g, sname in sorted(rows, key=lambda t: (t[0].lower(), t[1].lower())):
                it = QtWidgets.QListWidgetItem(f"{g} / {sname}")
                it.setData(QtCore.Qt.UserRole, {"genre": g, "subgenre": sname})
                self.lst_presets.addItem(it)
            return

        self.lbl_right.setText(f"Subgenres / Presets ({gk})")
        subs = ((genres.get(gk) or {}).get("subgenres") or {})
        for sname in sorted(subs.keys(), key=lambda s: s.lower()):
            it = QtWidgets.QListWidgetItem(sname)
            it.setData(QtCore.Qt.UserRole, {"genre": gk, "subgenre": sname})
            self.lst_presets.addItem(it)

    def _get_selected_pair(self) -> Optional[tuple[str, str]]:
        it = self.lst_presets.currentItem()
        if not it:
            return None
        d = it.data(QtCore.Qt.UserRole) or {}
        g = str(d.get("genre") or "").strip()
        s = str(d.get("subgenre") or "").strip()
        if not g or not s:
            return None
        return (g, s)

    def _add_preset(self):
        # Ask for genre
        genre, ok = QtWidgets.QInputDialog.getText(self, "Add to presets", "What genre?")
        if not ok:
            return
        genre = (genre or "").strip()
        if not genre:
            return

        # Ask for subgenre
        sub, ok = QtWidgets.QInputDialog.getText(self, "Add to presets", "What subgenre?")
        if not ok:
            return
        sub = (sub or "").strip()
        if not sub:
            return

        genres = self._data.setdefault("genres", {})
        gd = genres.setdefault(genre, {})
        subs = gd.setdefault("subgenres", {})

        payload = self._mw._ace15_current_preset_payload()
        subs[sub] = payload
        self._mw._ace15_preset_mgr_save(self._path, self._data)

        self._refresh_left()
        # Select genre and sub
        for i in range(self.lst_genres.count()):
            it = self.lst_genres.item(i)
            if it and it.data(QtCore.Qt.UserRole) == genre:
                self.lst_genres.setCurrentItem(it)
                break
        self._refresh_right()
        for i in range(self.lst_presets.count()):
            it = self.lst_presets.item(i)
            if it:
                d = it.data(QtCore.Qt.UserRole) or {}
                if d.get("genre") == genre and d.get("subgenre") == sub:
                    self.lst_presets.setCurrentItem(it)
                    break

    def _remove_preset(self):
        pair = self._get_selected_pair()
        if not pair:
            QtWidgets.QMessageBox.information(self, "Remove", "Select a preset (subgenre) on the right to remove.")
            return
        g, s = pair

        if QtWidgets.QMessageBox.question(
            self,
            "Confirm removal",
            f"Remove preset '{g} / {s}'?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        ) != QtWidgets.QMessageBox.Yes:
            return

        genres = self._data.setdefault("genres", {})
        gd = genres.get(g) or {}
        subs = gd.get("subgenres") or {}
        if s in subs:
            subs.pop(s, None)
        gd["subgenres"] = subs

        # If genre has 0 subgenres, ask whether to remove it too.
        if not subs:
            if QtWidgets.QMessageBox.question(
                self,
                "Remove genre?",
                f"'{g}' now has 0 presets. Remove this genre too?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            ) == QtWidgets.QMessageBox.Yes:
                genres.pop(g, None)
            else:
                genres[g] = gd
        else:
            genres[g] = gd

        self._data["genres"] = genres
        self._mw._ace15_preset_mgr_save(self._path, self._data)
        self._refresh_left()
        self._select_all()
        self._refresh_right()

    def _edit_preset(self):
        pair = self._get_selected_pair()
        if not pair:
            QtWidgets.QMessageBox.information(self, "Edit", "Select a preset (subgenre) on the right to edit.")
            return
        g, s = pair
        genres = self._data.setdefault("genres", {})
        pd = (((genres.get(g) or {}).get("subgenres") or {}).get(s) or {})

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"Edit preset — {g} / {s}")
        dlg.resize(860, 520)
        ly = QtWidgets.QVBoxLayout(dlg)

        form = QtWidgets.QFormLayout()
        ed_genre = QtWidgets.QLineEdit(g)
        ed_sub = QtWidgets.QLineEdit(s)
        form.addRow("Genre", ed_genre)
        form.addRow("Subgenre", ed_sub)
        ly.addLayout(form)

        txt = QtWidgets.QPlainTextEdit()
        txt.setPlainText(json.dumps(pd, indent=2, ensure_ascii=False))
        txt.setToolTip("Edit this preset JSON. It must remain a JSON object.")
        ly.addWidget(txt, 1)

        chk_overwrite = QtWidgets.QCheckBox("Overwrite settings with current UI state (instead of the JSON above)")
        ly.addWidget(chk_overwrite)

        row = QtWidgets.QHBoxLayout()
        btn_ok = QtWidgets.QPushButton("Save")
        btn_cancel = QtWidgets.QPushButton("Cancel")
        row.addStretch(1)
        row.addWidget(btn_ok)
        row.addWidget(btn_cancel)
        ly.addLayout(row)

        def _do_save():
            new_g = ed_genre.text().strip()
            new_s = ed_sub.text().strip()
            if not new_g or not new_s:
                QtWidgets.QMessageBox.warning(dlg, "Invalid", "Genre and Subgenre cannot be empty.")
                return

            if chk_overwrite.isChecked():
                new_pd = self._mw._ace15_current_preset_payload()
            else:
                try:
                    new_pd = json.loads(txt.toPlainText().strip() or "{}")
                    if not isinstance(new_pd, dict):
                        raise ValueError("Preset JSON must be an object")
                except Exception as e:
                    QtWidgets.QMessageBox.warning(dlg, "Invalid JSON", f"Cannot parse preset JSON: {e}")
                    return

            # Remove old
            genres = self._data.setdefault("genres", {})
            old_gd = genres.get(g) or {}
            old_subs = old_gd.get("subgenres") or {}
            old_subs.pop(s, None)
            old_gd["subgenres"] = old_subs
            if old_subs:
                genres[g] = old_gd
            else:
                genres.pop(g, None)

            # Insert new
            gd = genres.setdefault(new_g, {})
            subs = gd.setdefault("subgenres", {})
            subs[new_s] = new_pd
            gd["subgenres"] = subs
            genres[new_g] = gd
            self._data["genres"] = genres
            self._mw._ace15_preset_mgr_save(self._path, self._data)

            dlg.accept()

        btn_ok.clicked.connect(_do_save)
        btn_cancel.clicked.connect(dlg.reject)

        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return

        self._refresh_left()
        # Select edited genre
        for i in range(self.lst_genres.count()):
            it = self.lst_genres.item(i)
            if it and it.data(QtCore.Qt.UserRole) == ed_genre.text().strip():
                self.lst_genres.setCurrentItem(it)
                break
        self._refresh_right()

    def _apply_selected_preset(self):
        pair = self._get_selected_pair()
        if not pair:
            return
        g, s = pair
        genres = self._genres_dict()
        pd = (((genres.get(g) or {}).get("subgenres") or {}).get(s) or {})
        if not pd:
            return
        if QtWidgets.QMessageBox.question(
            self,
            "Apply preset",
            f"Apply preset '{g} / {s}' to the current UI?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes,
        ) != QtWidgets.QMessageBox.Yes:
            return
        # Remember which preset the user applied, so we can name outputs nicely.
        try:
            self._mw._ace15_last_preset_genre = str(g or "").strip()
            self._mw._ace15_last_preset_subgenre = str(s or "").strip()
        except Exception:
            pass
        self._mw._ace15_apply_preset_payload(pd)


# --- TOML writer (no external dependency) ---
def _toml_escape_str(s: str) -> str:
    # TOML basic string escaping (enough for our use-case)
    # TOML basic strings cannot contain literal newlines, so we encode them as "\n".
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\\", "\\\\").replace('"', '\\\"')
    s = s.replace("\n", "\\n")
    return s

def toml_dumps_flat(d: dict) -> str:
    """
    Dump a flat dict (primitive values) to TOML.
    Supports: str, int, float, bool, None.
    """
    lines: List[str] = []
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, bool):
            val = "true" if v else "false"
        elif isinstance(v, int):
            val = str(v)
        elif isinstance(v, float):
            # avoid scientific notation, keep it stable
            val = ("%.6f" % v).rstrip("0").rstrip(".")
            if val == "":
                val = "0"
        else:
            val = f'"{_toml_escape_str(str(v))}"'
        lines.append(f"{k} = {val}")
    return "\n".join(lines) + "\n"


def is_windows() -> bool:
    return os.name == "nt"


def default_env_python(framevision_root: Path) -> Path:
    return framevision_root / "environments" / ".ace_15" / ("Scripts/python.exe" if is_windows() else "bin/python")

def default_ace_project_root(framevision_root: Path) -> Path:
    # <root>/models/ace_step_15/repo/ACE-Step-1.5
    return framevision_root / "models" / "ace_step_15" / "repo" / "ACE-Step-1.5"


def default_ace_cli_py(framevision_root: Path) -> Path:
    return default_ace_project_root(framevision_root) / "cli.py"



def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def open_in_explorer(path: Path) -> None:
    try:
        if is_windows():
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def list_audio_files(folder: Path) -> List[Path]:
    exts = {".wav", ".mp3", ".flac", ".ogg", ".m4a"}
    if not folder.exists():
        return []
    files = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in exts]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files

def discover_lm_models(project_root: Path) -> list[str]:
    """
    Best-effort scan for locally available 5Hz LM models in the ACE repo.
    Typical location: <project_root>/checkpoints/<lm_model_folder>
    If nothing is found, return the known model names so you can select and let ACE auto-download.
    """
    known = [
        "acestep-5Hz-lm-0.6B",
        "acestep-5Hz-lm-1.7B",
        "acestep-5Hz-lm-4B",
    ]
    found: list[str] = []
    ckpt = project_root / "checkpoints"
    if ckpt.exists():
        for p in ckpt.iterdir():
            if p.is_dir() and "acestep-5Hz-lm" in p.name:
                found.append(p.name)
    found = sorted(set(found), key=lambda s: (len(s), s))
    # NOTE: 3B LM variant was an accidental option in this UI; hide it.
    found = [m for m in found if "acestep-5Hz-lm-3B" not in m]
    # If not found yet, still return known so user can pick and let ACE download
    if not found:
        return [k for k in known if k]
    # Include any known model not yet present as selectable (to trigger download)
    for k in known:
        if k and k not in found:
            found.append(k)
    return found


def discover_main_models(project_root: Path) -> list[str]:
    """Best-effort scan for locally available main (DiT) models.

    ACE-Step commonly names these as Hugging Face model IDs like:
      - acestep-v15-base
      - acestep-v15-turbo

    If checkpoints are already present under <project_root>/checkpoints, we list them.
    Otherwise we still list known IDs so selecting one can trigger ACE auto-download.
    """

    known = [
        "acestep-v15-base",
        "acestep-v15-sft",
        "acestep-v15-turbo",
        "acestep-v15-turbo-rl",
    ]

    found: list[str] = []
    ckpt = project_root / "checkpoints"
    if ckpt.exists():
        for p in ckpt.iterdir():
            if p.is_dir() and "acestep" in p.name and "v15" in p.name:
                found.append(p.name)

    found = sorted(set(found), key=lambda s: (len(s), s))
    if not found:
        return [k for k in known if k]
    for k in known:
        if k and k not in found:
            found.append(k)
    return found



@dataclass
class Settings:
    framevision_root: str = ""
    env_python: str = ""
    cli_py: str = ""
    project_root: str = ""
    output_dir: str = ""
    hide_console: bool = True
    auto_open_output: bool = True

    # UI / behavior
    wheel_guard_enabled: bool = True


    banner_enabled: bool = True
    ui_theme: str = "Signal Grey"

    backend: str = "vllm"
    # Advanced DiT parameter (ACE-Step "Shift" / timestep shift factor)
    # Range 1.0-5.0. Default 3.0. (Only effective for base models in ACE-Step.)
    shift: float = 3.0
    log_level: str = "INFO"
    task_type: str = "text2music"
    audio_format: str = "mp3"
    duration: float = 180.0
    batch_size: int = 1
    seed: int = -1


    seed_random: bool = False
    bpm: int = 0  # 0 = auto
    timesignature: int = 0  # 0 = auto
    keyscale: str = ""  # empty = auto
    # ISO 639-1 language code for vocals. Empty/auto lets ACE decide.
    vocal_language: str = ""  # e.g., "en", "es", "ja"; empty = auto
    thinking: bool = False
    # Gradio feature: run multiple LM "thinking" branches in parallel and pick the best.
    # Only meaningful when Enable LM is on.
    parallel_thinking: bool = False
    enable_lm: bool = False
    offload_to_cpu: bool = False
    offload_dit_to_cpu: bool = False
    use_flash_attention: bool = False
    # If enabled, the UI will use the FastAPI server workflow (keeps models in VRAM).
    # Requires restart to take effect.
    keep_in_vram: bool = False
    main_model_path: str = ""
    lm_model_path: str = ""
    lm_enhance: bool = False

    # LM sampling controls (only used when Enable LM is on)
    lm_temperature: float = 0.85
    lm_top_p: float = 0.95
    lm_top_k: int = 0  # 0 = disabled

    # Negative prompt (LM guidance). Empty = let ACE default ("NO USER INPUT").
    lm_negative_prompt: str = ""

    # Generation controls
    guidance_scale: float = 0.0  # 0 = default/auto
    infer_method: str = ""       # empty/auto = let ACE decide
    inference_steps: int = 0     # 0 = default/auto

    def to_dict(self) -> dict:
        d: dict = {}
        # Persist all declared fields, even if they were never assigned on the instance.
        for k in getattr(Settings, '__annotations__', {}).keys():
            try:
                d[k] = getattr(self, k)
            except Exception:
                pass
        # Also include any dynamic instance attributes.
        try:
            for k, v in self.__dict__.items():
                d[k] = v
        except Exception:
            pass
        return d


@dataclass
class QueueJob:
    """A single generation job.

    NOTE: "batch_size" (1–8) is part of the job and represents ONE job that
    produces multiple tracks inside that job.
    """

    job_id: int
    created_epoch: float
    use_api: bool
    out_dir: Path

    # CLI workflow
    cli_args: Optional[list[str]] = None
    cli_cwd: Optional[Path] = None
    cfg_path: Optional[Path] = None

    # API workflow
    api_payload: Optional[dict] = None
    api_base_url: Optional[str] = None

    # Human readable metadata for the Queue tab
    title: str = ""
    batch_size: int = 1
    seed: str = ""
    # IMPORTANT: naming fields are snapshotted at enqueue time so queued jobs
    # don't get renamed using whatever preset happens to be selected later.
    subgenre_for_naming: str = ""
    task_type: str = ""
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        """Serialize this job to JSON-safe primitives."""
        return {
            "job_id": int(self.job_id),
            "created_epoch": float(self.created_epoch),
            "use_api": bool(self.use_api),
            "out_dir": str(self.out_dir) if self.out_dir else "",

            "cli_args": list(self.cli_args) if self.cli_args else None,
            "cli_cwd": str(self.cli_cwd) if self.cli_cwd else None,
            "cfg_path": str(self.cfg_path) if self.cfg_path else None,

            "api_payload": self.api_payload if self.api_payload else None,
            "api_base_url": self.api_base_url,

            "title": self.title,
            "batch_size": int(self.batch_size or 1),
            "seed": self.seed,
            "subgenre_for_naming": self.subgenre_for_naming,
            "task_type": self.task_type,
            "duration_s": float(self.duration_s or 0.0),
        }

    @staticmethod
    def from_dict(d: dict) -> "QueueJob":
        """Deserialize a QueueJob from a dict (created by to_dict)."""

        def _p(v) -> Optional[Path]:
            if v is None:
                return None
            if isinstance(v, Path):
                return v
            if isinstance(v, str):
                s = v.strip()
                return Path(s) if s else None
            return None

        out_dir = _p(d.get("out_dir")) or Path(".")

        return QueueJob(
            job_id=int(d.get("job_id", 0) or 0),
            created_epoch=float(d.get("created_epoch", time.time()) or time.time()),
            use_api=bool(d.get("use_api", False)),
            out_dir=out_dir,

            cli_args=(list(d.get("cli_args") or []) or None),
            cli_cwd=_p(d.get("cli_cwd")),
            cfg_path=_p(d.get("cfg_path")),

            api_payload=(d.get("api_payload") if isinstance(d.get("api_payload"), dict) else None),
            api_base_url=(str(d.get("api_base_url")) if d.get("api_base_url") else None),

            title=str(d.get("title", "") or ""),
            batch_size=int(d.get("batch_size", 1) or 1),
            seed=str(d.get("seed", "") or ""),
            subgenre_for_naming=str(d.get("subgenre_for_naming", "") or ""),
            task_type=str(d.get("task_type", "") or ""),
            duration_s=float(d.get("duration_s", 0.0) or 0.0),
        )


class Runner(QtCore.QObject):
    log = QtCore.Signal(str)
    started = QtCore.Signal()
    finished = QtCore.Signal(int)

    def __init__(self, args: List[str], cwd: Path, hide_console: bool):
        super().__init__()
        self.args = args
        self.cwd = cwd
        self.hide_console = hide_console
        self._proc: Optional[subprocess.Popen] = None
        self._ready_flag: bool = False
        self._stop = False

    @QtCore.Slot()
    def run(self):
        self.started.emit()
        try:
            creationflags = 0
            if is_windows() and self.hide_console:
                creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

            self.log.emit("Command:\n  " + " ".join(shlex.quote(a) for a in self.args))
            self.log.emit(f"Working dir:\n  {self.cwd}")

            env = os.environ.copy()
            # Force UTF-8 so cli.py can print emojis (✅) without crashing on cp1252.
            env.setdefault("PYTHONIOENCODING", "utf-8")
            env.setdefault("PYTHONUTF8", "1")
            env.setdefault("LANG", "C.UTF-8")
            env.setdefault("LC_ALL", "C.UTF-8")
            self._proc = subprocess.Popen(
                self.args,
                cwd=str(self.cwd),
                stdout=subprocess.PIPE,
                stdin=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
                env=env,
            )
            assert self._proc.stdout is not None
            # stdin is used to auto-continue interactive prompts
            assert self._proc.stdin is not None
            for line in self._proc.stdout:
                if self._stop:
                    break
                self.log.emit(line.rstrip("\n"))

                # Auto-continue for ACE-Step interactive draft prompt (it writes instruction.txt and waits for Enter).
                if "Press Enter when ready to continue." in line and self._proc and self._proc.stdin:
                    try:
                        self.log.emit("NOTE: Auto-pressed Enter to continue.")
                        self._proc.stdin.write("\n")
                        self._proc.stdin.flush()
                    except Exception:
                        pass


            if self._stop and self._proc and self._proc.poll() is None:
                self.log.emit("Stop requested. Terminating...")
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except Exception:
                    self._proc.kill()

            code = self._proc.wait()
            self.finished.emit(int(code))
        except Exception as e:
            self.log.emit(f"ERROR: {e!r}")
            self.finished.emit(999)

    def stop(self):
        self._stop = True


class ApiServerManager(QtCore.QObject):
    """Launch and monitor ACE-Step FastAPI server.

    This is used by the "Keep in VRAM" workflow so models stay resident across runs.
    """

    log = QtCore.Signal(str)
    ready = QtCore.Signal()

    def __init__(self, env_python: Path, project_root: Path, host: str = "127.0.0.1", port: int = 8001):
        super().__init__()
        self.env_python = env_python
        self.project_root = project_root
        self.host = host
        self.port = port
        self._proc: Optional[subprocess.Popen] = None
        self._stop = False
        self._reader_thread: Optional[QtCore.QThread] = None
        self._reader_obj: Optional[_ApiLogReader] = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def is_running(self) -> bool:
        return self._proc is not None and (self._proc.poll() is None)


    def _ensure_headless_api_server(self, api_server_py: Path) -> Path:
        """Create a gradio-free copy of api_server.py and return its path.

        The upstream ACE-Step api_server.py imports a helper from acestep.ui.gradio.* which
        pulls in the gradio dependency via package __init__.py. For the PySide workflow
        we don't want to require gradio, so we generate a headless variant next to it.
        """
        headless_py = api_server_py.with_name("api_server_headless.py")
        try:
            src = api_server_py.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            self.log.emit(f"[Keep in VRAM] Failed to read api_server.py: {e}")
            return api_server_py

        # If upstream already doesn't depend on gradio, use it as-is
        if "acestep.ui.gradio" not in src:
            return api_server_py

        # Reuse generated file if it's newer than upstream
        try:
            if headless_py.exists() and headless_py.stat().st_mtime >= api_server_py.stat().st_mtime:
                return headless_py
        except Exception:
            pass

        patched = src

        # 1) Replace the gradio results_handlers import with a local headless helper.
        #    We must NOT inject inside a multi-line import (...) block, so we patch in-place.
        helper = """# --- Headless generation info (gradio-free; generated by PySide UI) --------

from typing import Optional, Any, Dict

def _build_generation_info_headless(
    lm_metadata: Any = None,
    time_costs: Optional[Dict[str, Any]] = None,
    seed_value: Optional[str] = None,
    inference_steps: Optional[int] = None,
    num_audios: int = 1,
) -> str:
    \"""Return a short, human-readable summary without importing gradio.\""" 
    try:
        tc = time_costs or {}
        if not isinstance(tc, dict):
            return ""
        n = int(num_audios or 0)
        lm_total = float(tc.get("lm_total_time", 0.0) or 0.0)
        dit_total = float(tc.get("dit_total_time_cost", 0.0) or 0.0)
        total = lm_total + dit_total
        if total <= 0 or n <= 0:
            return ""
        avg = total / max(1, n)
        seed_part = f" seed={seed_value}" if seed_value else ""
        steps_part = f" steps={inference_steps}" if inference_steps else ""
        return f"Total: {total:.2f}s ({n} audio{'s' if n != 1 else ''}, avg {avg:.2f}s){seed_part}{steps_part}"
    except Exception:
        return ""

# ------------------------------------------------------------------------
"""

        patched = re.sub(
            r"^from\s+acestep\.ui\.gradio\.events\.results_handlers\s+import\s+_build_generation_info\s*$",
            helper,
            patched,
            flags=re.MULTILINE,
        )

        # 2) Replace calls to the helper with our headless helper name
        patched = patched.replace("_build_generation_info(", "_build_generation_info_headless(")
        # 3) Avoid uvicorn importing the original gradio-dependent module.
        #    Upstream uses uvicorn.run("acestep.api_server:app", ...) which would re-import api_server.py.
        #    When running the generated headless file, pass the app object directly instead.
        patched = re.sub(
            r'uvicorn\.run\(\s*["\']acestep\.api_server:app["\']\s*,',
            'uvicorn.run(app,',
            patched,
        )
        patched = re.sub(
            r'uvicorn\.run\(\s*["\']acestep\.api_server:app["\']\s*\)',
            'uvicorn.run(app)',
            patched,
        )

        try:
            headless_py.write_text(patched, encoding="utf-8", errors="replace")
            self.log.emit(f"[Keep in VRAM] Generated headless API server: {headless_py}")
            return headless_py
        except Exception as e:
            self.log.emit(f"[Keep in VRAM] Failed to write headless API server: {e}")
            return api_server_py

    def start(self, lm_model: str = "") -> None:
        if self.is_running():
            return
        self._ready_flag = False

        api_server_py = self.project_root / "acestep" / "api_server.py"
        if not api_server_py.exists():
            self.log.emit(
                "[Keep in VRAM] api_server.py not found under project_root/acestep/. "
                "Disable 'Keep in VRAM' or install the full ACE-Step repo."
            )
            return

        api_server_py = self._ensure_headless_api_server(api_server_py)

        args = [
            str(self.env_python),
            str(api_server_py),
            "--host",
            self.host,
            "--port",
            str(self.port),
        ]

        try:
            self._proc = subprocess.Popen(
                args,
                cwd=str(self.project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            self.log.emit(f"[Keep in VRAM] Failed to start API server: {e!r}")
            self._proc = None
            return

        self._stop = False

        # Reader thread that streams server logs into the UI.
        self._reader_thread = QtCore.QThread()
        reader = _ApiLogReader(self._proc)
        self._reader_obj = reader
        reader.moveToThread(self._reader_thread)
        self._reader_thread.started.connect(reader.run)
        reader.log.connect(self.log)
        reader.ready.connect(self._mark_ready)
        reader.finished.connect(self._reader_thread.quit)
        reader.finished.connect(reader.deleteLater)
        self._reader_thread.finished.connect(self._reader_thread.deleteLater)
        self._reader_thread.start()

    
    def _mark_ready(self) -> None:
        self._ready_flag = True
        try:
            self.ready.emit()
        except Exception:
            pass

    def wait_until_ready(self, timeout_sec: float = 15.0) -> bool:
        """Wait until the API server is reachable on HTTP (best effort)."""
        if not self.is_running():
            return False
        # If the log reader already detected readiness, we're done.
        if self._ready_flag:
            return True

        deadline = time.time() + float(timeout_sec)
        url = self.base_url + "/openapi.json"
        while time.time() < deadline:
            if not self.is_running():
                return False
            if self._ready_flag:
                return True
            try:
                import urllib.request
                req = urllib.request.Request(url, headers={"User-Agent": "Ace15UI"})
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    if 200 <= int(getattr(resp, "status", 200)) < 500:
                        self._ready_flag = True
                        return True
            except Exception:
                pass
            time.sleep(0.25)
        return False


    def stop(self) -> None:
        self._stop = True
        try:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except Exception:
                    self._proc.kill()
        except Exception:
            pass
        self._reader_obj = None
        self._proc = None


class _ApiLogReader(QtCore.QObject):
    log = QtCore.Signal(str)
    ready = QtCore.Signal()
    finished = QtCore.Signal()

    def __init__(self, proc: subprocess.Popen):
        super().__init__()
        self.proc = proc
        self._ready_emitted = False

    def run(self):
        try:
            if self.proc.stdout is None:
                return
            for line in self.proc.stdout:
                s = (line or "").rstrip("\n")
                if s:
                    self.log.emit(s)
                # Best-effort readiness detection: uvicorn prints "Uvicorn running on".
                if (not self._ready_emitted) and ("Uvicorn running on" in s or "Application startup complete" in s):
                    self._ready_emitted = True
                    self.ready.emit()
            try:
                rc = self.proc.wait(timeout=0.1)
                self.log.emit(f"[Keep in VRAM] API server exited (code {rc})")
            except Exception:
                pass
        finally:
            self.finished.emit()


class ApiRunner(QtCore.QObject):
    """Run a single generation via the local API server."""

    log = QtCore.Signal(str)
    started = QtCore.Signal()
    finished = QtCore.Signal(int)

    def __init__(self, base_url: str, payload: dict, output_dir: Path, timeout_s: float = 3600.0):
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.payload = payload
        self.output_dir = output_dir
        self.timeout_s = timeout_s
        self._stop = False

        # Captured outputs for post-processing (renaming with correct per-output seeds).
        # List of (Path, seed_int_or_None)
        self.saved_outputs: list[tuple[Path, Optional[int]]] = []

    def stop(self):
        self._stop = True

    def _http_json(self, path: str, data: dict) -> dict:
        import urllib.request
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        try:
            return json.loads(raw)
        except Exception:
            return {"_raw": raw}

    def _http_get_bytes(self, url: str) -> bytes:
        import urllib.request
        with urllib.request.urlopen(url, timeout=120) as resp:
            return resp.read()

    def run(self):
        self.started.emit()
        t0 = time.time()
        try:
            # The API server *can* accept batch_size>1, but it may reduce batch_size internally
            # under VRAM pressure (and then only return 1 audio). To ensure the UI honors the
            # user's requested "Batch/Outputs", we submit N single-audio jobs sequentially.
            try:
                requested_n = int(self.payload.get("batch_size", 1) or 1)
            except Exception:
                requested_n = 1
            if requested_n < 1:
                requested_n = 1

            base_payload = dict(self.payload)
            base_seed = None
            try:
                base_seed = int(base_payload.get("seed")) if base_payload.get("seed") is not None else None
            except Exception:
                base_seed = None
            use_random_seed = bool(base_payload.get("use_random_seed", False))

            # Always run API jobs as single-output to guarantee we get N files.
            base_payload["batch_size"] = 1

            saved_paths: list[Path] = []
            saved_meta: list[tuple[Path, Optional[int]]] = []
            for i in range(requested_n):
                if self._stop:
                    self.log.emit("[Keep in VRAM] Stopped.")
                    self.finished.emit(5)
                    return

                payload = dict(base_payload)

                # Seed strategy:
                # - If random seeds enabled -> new random seed per output.
                # - Else -> deterministic: base_seed + i (if base_seed exists).
                seed_for_this_output: Optional[int] = None
                if use_random_seed:
                    try:
                        payload["seed"] = random.randint(0, 2**32 - 1)
                    except Exception:
                        payload["seed"] = int(time.time() * 1000) & 0xFFFFFFFF
                    payload["use_random_seed"] = False
                    try:
                        seed_for_this_output = int(payload.get("seed"))
                    except Exception:
                        seed_for_this_output = None
                else:
                    if base_seed is not None:
                        payload["seed"] = int(base_seed + i)
                        payload["use_random_seed"] = False
                    try:
                        if payload.get("seed") is not None:
                            seed_for_this_output = int(payload.get("seed"))
                    except Exception:
                        seed_for_this_output = None

                self.log.emit(f"[Keep in VRAM] Submitting task to API server... ({i+1}/{requested_n})")
                r = self._http_json("/release_task", payload)
                data = r.get("data") if isinstance(r, dict) else None
                if not isinstance(data, dict) or not data.get("task_id"):
                    self.log.emit(f"[Keep in VRAM] Unexpected /release_task response: {r!r}")
                    self.finished.emit(1)
                    return
                task_id = str(data["task_id"])
                self.log.emit(f"[Keep in VRAM] Task queued: {task_id}")

                # Poll until finished.
                last_progress = ""
                while not self._stop:
                    if (time.time() - t0) > self.timeout_s:
                        self.log.emit("[Keep in VRAM] Timeout waiting for task result")
                        self.finished.emit(2)
                        return
                    qr = self._http_json("/query_result", {"task_id_list": [task_id]})
                    qd = qr.get("data") if isinstance(qr, dict) else None
                    if isinstance(qd, list) and qd:
                        item = qd[0] if isinstance(qd[0], dict) else {}
                        status = int(item.get("status", 0) or 0)
                        ptxt = str(item.get("progress_text") or "").strip()
                        if ptxt and ptxt != last_progress:
                            last_progress = ptxt
                            self.log.emit(f"[Keep in VRAM] {ptxt}")

                        if status == 1:
                            # Success. Parse result JSON (string).
                            res_str = item.get("result", "[]")
                            try:
                                res_list = json.loads(res_str) if isinstance(res_str, str) else res_str
                            except Exception:
                                res_list = []

                            file_urls = []
                            if isinstance(res_list, list):
                                for rr in res_list:
                                    if isinstance(rr, dict):
                                        fu = str(rr.get("file") or "").strip()
                                        if fu:
                                            file_urls.append(fu)

                            if not file_urls:
                                self.log.emit("[Keep in VRAM] Task succeeded but no audio file URL found.")
                                self.finished.emit(3)
                                return

                            # Download all returned files into output folder.
                            ensure_dir(self.output_dir)
                            stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
                            ext = (payload.get("audio_format") or "mp3").strip() or "mp3"

                            for j, file_url in enumerate(file_urls):
                                full_url = file_url
                                if full_url.startswith("/"):
                                    full_url = self.base_url + full_url
                                self.log.emit(f"[Keep in VRAM] Downloading audio... ({i+1}/{requested_n}, file {j+1}/{len(file_urls)})")
                                audio_bytes = self._http_get_bytes(full_url)

                                # Name: ace15_api_<timestamp>_<index>.<ext> when multiple outputs.
                                suffix = ""
                                if requested_n > 1 or len(file_urls) > 1:
                                    suffix = f"_{i+1:02d}"
                                    if len(file_urls) > 1:
                                        suffix += f"_{j+1:02d}"
                                out_path = self.output_dir / f"ace15_api_{stamp}{suffix}.{ext}"
                                out_path.write_bytes(audio_bytes)
                                saved_paths.append(out_path)
                                saved_meta.append((out_path, seed_for_this_output))
                                self.log.emit(f"[Keep in VRAM] Saved: {out_path}")

                            break  # done polling this task

                        if status == 2:
                            self.log.emit("[Keep in VRAM] Task failed")
                            self.finished.emit(4)
                            return

                    time.sleep(0.8)

            # All requested outputs done.
            try:
                self.saved_outputs = list(saved_meta)
            except Exception:
                self.saved_outputs = []
            self.finished.emit(0)
            return

        except Exception as e:
            self.log.emit(f"[Keep in VRAM] Error: {e!r}")
            self.finished.emit(9)

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1200, 760)

        self.fv_root_guess = guess_framevision_root()
        self.settings_path = settings_path_for_root(self.fv_root_guess)
        self.settings = self._load_settings()

        # Wheel guard (FrameVision style): prevent accidental edits while scrolling.
        try:
            if bool(getattr(self.settings, "wheel_guard_enabled", True)):
                install_no_wheel_guard()
            else:
                uninstall_no_wheel_guard()
        except Exception:
            pass

        self._thread: Optional[QtCore.QThread] = None
        self._runner: Optional[Runner] = None

        # Job queue
        self._queue: list[QueueJob] = []
        self._next_job_id: int = 1
        self._active_job: Optional[QueueJob] = None
        self._queue_pump_timer: Optional[QtCore.QTimer] = None

        # Load persisted queue (best-effort). If the app was closed while a job
        # was running, we treat it as pending again on next start.
        self._queue_load()

        # Optional API server workflow ("Keep in VRAM")
        self._api_server: Optional[ApiServerManager] = None

        # Generate button busy animation ("Generating…" with dots)
        self._gen_anim_timer: Optional[QtCore.QTimer] = None
        self._gen_anim_phase: int = 0
        self._gen_btn_base_text: str = "Generate"

        # Naming context for results
        self._ace15_last_preset_genre: str = ""
        self._ace15_last_preset_subgenre: str = ""
        self._ace15_out_snapshot: set[str] = set()
        self._ace15_run_started_epoch: float = 0.0

        # Remember last run context so we can archive/move instruction.txt next to outputs.
        self._last_out_dir: Optional[Path] = None
        self._last_cfg_path: Optional[Path] = None
        self._last_proj_root: Optional[Path] = None

        self._current_theme = (getattr(self.settings, "ui_theme", "Signal Grey") or "Signal Grey").strip() or "Signal Grey"
        self._build_ui()
        # Apply a simple default theme (no auto day/evening/night)
        self._apply_theme_simple(self._current_theme)
        self._apply_settings_to_ui()
        # Auto-detect required paths (and fix legacy defaults) without exposing them in the UI.
        self._auto_detect_paths()
        # Re-scan models (project_root may have changed due to auto-detect)
        try:
            if hasattr(self, 'cmb_main_model'):
                self._refresh_main_models()
            if hasattr(self, 'cmb_lm_model'):
                self._refresh_lm_models()
        except Exception:
            pass
        self._refresh_outputs()
        self._queue_refresh_ui()

        # Queue pump: if we have queued jobs and we're idle, start them.
        try:
            self._queue_pump_timer = QtCore.QTimer(self)
            self._queue_pump_timer.setInterval(750)
            self._queue_pump_timer.timeout.connect(self._queue_pump)
            self._queue_pump_timer.start()
        except Exception:
            self._queue_pump_timer = None

        # If enabled, start the FastAPI server now so models stay resident in VRAM.
        try:
            if bool(getattr(self.settings, "keep_in_vram", False)):
                self._ensure_api_server_started()
        except Exception:
            pass

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        # Persist queue so pending jobs can resume after restart.
        try:
            self._queue_save()
        except Exception:
            pass
        # Best-effort: stop API server on exit.
        try:
            if self._api_server is not None:
                self._api_server.stop()
        except Exception:
            pass
        super().closeEvent(event)

    def _ensure_api_server_started(self) -> None:
        """Start the FastAPI server (Keep in VRAM) if needed.

        This method is non-blocking. While the server is starting, the Generate button
        is disabled and the banner shows 'Starting server' with an indeterminate bar.
        """
        try:
            envpy = Path(self.settings.env_python or "").expanduser()
            proj = Path(self.settings.project_root or "").expanduser()
            if not envpy.exists() or not proj.exists():
                return

            if self._api_server is None:
                self._api_server = ApiServerManager(env_python=envpy, project_root=proj)
                self._api_server.log.connect(self._log)
                # When the log reader detects uvicorn readiness, re-enable Generate.
                self._api_server.ready.connect(self._on_api_server_ready)

            # If the server is already running, set UI state based on readiness.
            if self._api_server.is_running():
                if self._is_api_ready():
                    self._set_server_starting(False)
                    return
                # Running but not yet ready -> keep Generate disabled.
                self._set_server_starting(True)

            if not self._api_server.is_running():
                lm_model = str(getattr(self.settings, "lm_model_path", "") or "")
                self._log("[Keep in VRAM] Starting API server...")
                self._set_server_starting(True)
                self._api_server.start(lm_model=lm_model)
        except Exception:
            pass

    def _is_api_ready(self) -> bool:
        try:
            return bool(self._api_server is not None and getattr(self._api_server, "_ready_flag", False))
        except Exception:
            return False

    def _set_server_starting(self, starting: bool) -> None:
        """Lock/unlock Generate while Keep-in-VRAM server is starting."""
        try:
            self._server_starting = bool(starting)
        except Exception:
            self._server_starting = bool(starting)

        # Don't fight the 'busy' (generating) state.
        try:
            busy = bool(self.btn_stop.isEnabled())
        except Exception:
            busy = False

        if busy:
            return

        try:
            if bool(getattr(self.settings, "keep_in_vram", False)) and self._server_starting:
                # Keep enabled so the user can queue jobs while the server warms up.
                self.btn_run.setEnabled(True)
                # Banner + progress bar
                if hasattr(self, "banner"):
                    self.banner.setText("Starting server")
                if hasattr(self, "banner_progress"):
                    self.banner_progress.setVisible(True)
            else:
                # Only re-enable if not busy and not starting
                if hasattr(self, "btn_run"):
                    self.btn_run.setEnabled(True)
                if hasattr(self, "banner"):
                    self.banner.setText(getattr(self, "_banner_base_text", "Music Creation with Ace Step 1.5"))
                if hasattr(self, "banner_progress"):
                    self.banner_progress.setVisible(False)
        except Exception:
            pass

    def _on_api_server_ready(self) -> None:
        try:
            self._log("[Keep in VRAM] API server ready.")
        except Exception:
            pass
        # Unlock Generate if we're not currently generating.
        self._set_server_starting(False)

    def _tick_generate_anim(self) -> None:
        """Animate the Generate button + banner while a run is active (braille spinner)."""
        try:
            # If we're no longer busy, stop the timer.
            if not self.btn_stop.isEnabled():
                if self._gen_anim_timer is not None:
                    self._gen_anim_timer.stop()
                return
        except Exception:
            return

        frames = getattr(self, "_spin_frames", None)
        if not frames:
            frames = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
            self._spin_frames = frames

        self._gen_anim_phase = (self._gen_anim_phase + 1) % len(frames)
        f1 = frames[self._gen_anim_phase]
        f2 = frames[(self._gen_anim_phase + 1) % len(frames)]
        f3 = frames[(self._gen_anim_phase + 2) % len(frames)]

        # Button: small, single-character spinner (constant width)
        try:
            self.btn_run.setText(f"Generating {f1}  (click to queue)")
        except Exception:
            pass

        # Banner: progress animation handled by self.banner_progress (if present)
        try:
            if hasattr(self, "banner") and hasattr(self, "banner_progress") and self.btn_stop.isEnabled():
                self.banner.setText("Generating")
        except Exception:
            pass


    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)

        # Fancy banner at the top (sticky, outside scroll)
        self.banner = QtWidgets.QLabel("Music Creation with Ace Step 1.5")
        self._banner_base_text = "Music Creation with Ace Step 1.5"
        self.banner.setObjectName("aceBanner")
        self.banner.setAlignment(QtCore.Qt.AlignCenter)
        self.banner.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.banner.setFixedHeight(48)
        self.banner.setStyleSheet(
            "#aceBanner {"
            " font-size: 15px;"
            " font-weight: 600;"
            " padding: 8px 17px;"
            " border-radius: 12px;"
            " margin: 0 0 6px 0;"
            " color: #e8f5e9;"
            " background: qlineargradient("
            "   x1:0, y1:0, x2:1, y2:0,"
            "   stop:0 #1e88e5,"
            "   stop:1 #1b5e20"
            " );"
            " letter-spacing: 0.5px;"
            "}"
        )
        # Banner container so we can show an indeterminate progress animation while generating
        self.banner_wrap = QtWidgets.QWidget()
        self.banner_wrap.setObjectName("aceBannerWrap")
        bwl = QtWidgets.QVBoxLayout(self.banner_wrap)
        bwl.setContentsMargins(0, 0, 0, 0)
        bwl.setSpacing(2)
        bwl.addWidget(self.banner)

        self.banner_progress = QtWidgets.QProgressBar()
        self.banner_progress.setObjectName("aceBannerProgress")
        self.banner_progress.setRange(0, 0)  # indeterminate
        self.banner_progress.setTextVisible(False)
        self.banner_progress.setFixedHeight(10)
        self.banner_progress.setVisible(False)
        self.banner_progress.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        # A subtle animated bar (Qt paints this automatically)
        self.banner_progress.setStyleSheet(
            "#aceBannerProgress {"
            " border: 1px solid rgba(255,255,255,0.25);"
            " border-radius: 5px;"
            " background: rgba(255,255,255,0.10);"
            " }"
            "#aceBannerProgress::chunk {"
            " border-radius: 5px;"
            " background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 rgba(30,136,229,0.9), stop:1 rgba(27,94,32,0.9));"
            " }"
        )
        # inset the bar a bit so it looks like it's part of the banner pill
        pb_wrap = QtWidgets.QWidget()
        pb_l = QtWidgets.QHBoxLayout(pb_wrap)
        pb_l.setContentsMargins(22, 0, 22, 0)
        pb_l.setSpacing(0)
        pb_l.addWidget(self.banner_progress)
        bwl.addWidget(pb_wrap)

        outer.addWidget(self.banner_wrap)
        self.banner_gap = QtWidgets.QWidget()
        self.banner_gap.setFixedHeight(4)
        self.banner_gap.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        outer.addWidget(self.banner_gap)

        # Apply banner visibility from saved settings
        try:
            be = bool(getattr(self.settings, "banner_enabled", True))
        except Exception:
            be = True
        self.banner.setVisible(be)
        self.banner_gap.setVisible(be)



        # Tabs: Create / Advanced
        self.tabs = QtWidgets.QTabWidget()
        outer.addWidget(self.tabs, 1)

        # -------------------------
        # Tab 1: Create (scrollable)
        # -------------------------
        tab_create = QtWidgets.QWidget()
        self.tabs.addTab(tab_create, "Create")
        tab_create_l = QtWidgets.QVBoxLayout(tab_create)
        tab_create_l.setContentsMargins(0, 0, 0, 0)
        tab_create_l.setSpacing(0)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        tab_create_l.addWidget(scroll, 1)

        page = QtWidgets.QWidget()
        scroll.setWidget(page)
        page_l = QtWidgets.QVBoxLayout(page)
        page_l.setContentsMargins(12, 12, 12, 12)
        page_l.setSpacing(12)

        # -------------------------
        # Tab 2: Queue
        # -------------------------
        tab_queue = QtWidgets.QWidget()
        self.tabs.addTab(tab_queue, "Queue")
        tq = QtWidgets.QVBoxLayout(tab_queue)
        tq.setContentsMargins(12, 12, 12, 12)
        tq.setSpacing(10)

        self.lbl_queue_state = QtWidgets.QLabel("Idle")
        self.lbl_queue_state.setObjectName("ace15_queue_state")
        tq.addWidget(self.lbl_queue_state, 0)

        self.tbl_queue = QtWidgets.QTableWidget(0, 7)
        self.tbl_queue.setObjectName("ace15_queue_table")
        self.tbl_queue.setHorizontalHeaderLabels([
            "#",
            "Status",
            "Tracks",
            "Task",
            "Duration",
            "Seed",
            "Title",
        ])
        self.tbl_queue.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.tbl_queue.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.tbl_queue.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tbl_queue.verticalHeader().setVisible(False)
        try:
            self.tbl_queue.horizontalHeader().setStretchLastSection(True)
            self.tbl_queue.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
            self.tbl_queue.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
            self.tbl_queue.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
            self.tbl_queue.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
            self.tbl_queue.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)
            self.tbl_queue.horizontalHeader().setSectionResizeMode(5, QtWidgets.QHeaderView.ResizeToContents)
        except Exception:
            pass
        
        # Queue table: refresh is throttled to avoid fighting row selection.
        self.tbl_queue.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tbl_queue.customContextMenuRequested.connect(self._queue_context_menu)

        # Refresh the queue view every 5 seconds (otherwise it can interrupt selection).
        self._queue_last_ui_refresh = 0.0
        self._queue_ui_timer = QtCore.QTimer(self)
        self._queue_ui_timer.setInterval(5000)
        self._queue_ui_timer.timeout.connect(lambda: self._queue_refresh_ui(force=True))
        self._queue_ui_timer.start()
        tq.addWidget(self.tbl_queue, 1)

        row_qbtn = QtWidgets.QHBoxLayout()
        self.btn_queue_start_next = QtWidgets.QPushButton("Start next")
        self.btn_queue_remove = QtWidgets.QPushButton("Remove selected")
        self.btn_queue_clear = QtWidgets.QPushButton("Clear")
        self.btn_queue_start_next.clicked.connect(self._queue_start_next)
        self.btn_queue_remove.clicked.connect(self._queue_remove_selected)
        self.btn_queue_clear.clicked.connect(self._queue_clear)
        row_qbtn.addWidget(self.btn_queue_start_next)
        row_qbtn.addStretch(1)
        row_qbtn.addWidget(self.btn_queue_remove)
        row_qbtn.addWidget(self.btn_queue_clear)
        tq.addLayout(row_qbtn)

        # -------------------------
        # Tab 3: Advanced (scrollable)
        # -------------------------
        tab_adv = QtWidgets.QWidget()
        self.tabs.addTab(tab_adv, "Advanced")
        tab_adv_l = QtWidgets.QVBoxLayout(tab_adv)
        tab_adv_l.setContentsMargins(0, 0, 0, 0)
        tab_adv_l.setSpacing(0)

        adv_scroll = QtWidgets.QScrollArea()
        adv_scroll.setWidgetResizable(True)
        adv_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        tab_adv_l.addWidget(adv_scroll, 1)

        adv_page = QtWidgets.QWidget()
        adv_scroll.setWidget(adv_page)
        adv_page_l = QtWidgets.QVBoxLayout(adv_page)
        adv_page_l.setContentsMargins(12, 12, 12, 12)
        adv_page_l.setSpacing(12)

        # -------------------------
        # Simple UI (always visible)
        # -------------------------
        gb_simple = QtWidgets.QGroupBox("Create music")
        v = QtWidgets.QVBoxLayout(gb_simple)

        self.ed_caption = QtWidgets.QPlainTextEdit()
        self.ed_caption.setPlaceholderText("Caption / description")
        # Caption: keep it compact (about 5 visible lines) and prevent it from
        # ballooning vertically when the window is tall.
        cap_h = None
        try:
            fm = QtGui.QFontMetrics(self.ed_caption.font())
            cap_h = int(fm.lineSpacing() * 5 + 16)
            self.ed_caption.setMinimumHeight(cap_h)
            self.ed_caption.setMaximumHeight(cap_h)
            self.ed_caption.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        except Exception:
            pass
        v.addWidget(QtWidgets.QLabel("Caption / description"))
        v.addWidget(self.ed_caption, 0)

        # Negatives (between caption and lyrics)
        v.addWidget(QtWidgets.QLabel("Negatives (optional)"))
        self.ed_negatives = QtWidgets.QPlainTextEdit()
        self.ed_negatives.setPlaceholderText("Negatives (optional)")
        self.ed_negatives.setToolTip("Negative prompt for LM guidance (helps avoid unwanted characteristics).")
        # Fixed ~2 lines tall
        fm = QtGui.QFontMetrics(self.ed_negatives.font())
        two_lines = int(fm.lineSpacing() * 2 + 12)
        self.ed_negatives.setFixedHeight(max(44, two_lines))
        self.ed_negatives.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        v.addWidget(self.ed_negatives)

        row_ly = QtWidgets.QHBoxLayout()
        row_ly.addWidget(QtWidgets.QLabel("Lyrics (optional)"))
        self.btn_clear_lyrics = QtWidgets.QPushButton("Clear")
        self.btn_clear_lyrics.setToolTip("Clear the lyrics box.")
        self.btn_clear_lyrics.clicked.connect(self._clear_lyrics_clicked)
        self.btn_gen_lyrics = QtWidgets.QPushButton("random lyric")
        self.btn_gen_lyrics.setToolTip("Generate quick placeholder lyrics for testing (does not change caption/BPM/duration).")
        self.btn_gen_lyrics.clicked.connect(self._generate_lyrics_clicked)
        row_ly.addStretch(1)
        row_ly.addWidget(self.btn_clear_lyrics)
        row_ly.addWidget(self.btn_gen_lyrics)
        v.addLayout(row_ly)

        self.ed_lyrics = QtWidgets.QPlainTextEdit()
        self.ed_lyrics.setPlaceholderText("Lyrics (optional)")
        # Match lyrics box height to caption box so the page stays visually balanced.
        try:
            if cap_h is None:
                fm = QtGui.QFontMetrics(self.ed_lyrics.font())
                cap_h = int(fm.lineSpacing() * 5 + 16)
            self.ed_lyrics.setMinimumHeight(cap_h)
            self.ed_lyrics.setMaximumHeight(cap_h)
            self.ed_lyrics.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        except Exception:
            pass
        v.addWidget(self.ed_lyrics, 0)

        # Layout (more responsive): use a grid so the UI spreads out on wide windows,
        # and collapses more gracefully when space is tight.
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(10)
        grid.setContentsMargins(0, 0, 0, 0)
        # label / field pairs across 3 columns (6 grid columns total)
        # Make the "field" columns stretch to consume extra space.
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        grid.setColumnStretch(5, 2)

        r = 0

        # Row 0: Mode toggles + Vocal language
        self.chk_instrumental = QtWidgets.QCheckBox("Instrumental")
        self.chk_instrumental.setToolTip(
            "Generate instrumental music (no vocals).\n"
            "When enabled, the UI sets lyrics to [Instrumental].\n"
            "Default: off."
        )
        self.chk_thinking_mode = QtWidgets.QCheckBox("Thinking")
        self.chk_thinking_mode.setToolTip("Enables ACE 'thinking' (LM reasoning) when LM is enabled.")

        self.chk_parallel_thinking = QtWidgets.QCheckBox("Parallel thinking")
        self.chk_parallel_thinking.setToolTip(
            "Runs multiple LM 'thinking' branches in parallel and selects the best result.\n"
            "Only applies when Enable LM is ON.\n"
            "May use more CPU/RAM while generating."
        )

        lbl_vlang = QtWidgets.QLabel("Vocal language")
        self.cmb_vocal_language = QtWidgets.QComboBox()
        self.cmb_vocal_language.setToolTip("Vocal language (ISO 639-1). 'auto' lets ACE decide / auto-detect.")
        self.cmb_vocal_language.setEditable(True)
        self.cmb_vocal_language.addItem("auto", "")
        self.cmb_vocal_language.addItem("English (en)", "en")
        self.cmb_vocal_language.addItem("Spanish (es)", "es")
        self.cmb_vocal_language.addItem("French (fr)", "fr")
        self.cmb_vocal_language.addItem("German (de)", "de")
        self.cmb_vocal_language.addItem("Italian (it)", "it")
        self.cmb_vocal_language.addItem("Portuguese (pt)", "pt")
        self.cmb_vocal_language.addItem("Dutch (nl)", "nl")
        self.cmb_vocal_language.addItem("Russian (ru)", "ru")
        self.cmb_vocal_language.addItem("Polish (pl)", "pl")
        self.cmb_vocal_language.addItem("Turkish (tr)", "tr")
        self.cmb_vocal_language.addItem("Arabic (ar)", "ar")
        self.cmb_vocal_language.addItem("Hindi (hi)", "hi")
        self.cmb_vocal_language.addItem("Bengali (bn)", "bn")
        self.cmb_vocal_language.addItem("Korean (ko)", "ko")
        self.cmb_vocal_language.addItem("Japanese (ja)", "ja")
        self.cmb_vocal_language.addItem("Chinese (zh)", "zh")
        self.cmb_vocal_language.addItem("Indonesian (id)", "id")
        self.cmb_vocal_language.addItem("Vietnamese (vi)", "vi")

        grid.addWidget(self.chk_instrumental, r, 0, 1, 2)
        grid.addWidget(self.chk_thinking_mode, r, 2)
        grid.addWidget(self.chk_parallel_thinking, r, 3)
        grid.addWidget(lbl_vlang, r, 4)
        grid.addWidget(self.cmb_vocal_language, r, 5)
        r += 1

        # Row 1: LM toggles
        self.chk_thinking = QtWidgets.QCheckBox("Enable LM")
        self.chk_thinking.setToolTip("Turns on the 5Hz LM (optional). Prompt enhancement is controlled by the toggle next to it.")
        self.chk_lm_enhance = QtWidgets.QCheckBox("LM enhance prompt (caption/metas)")
        self.chk_lm_enhance.setToolTip("If enabled, the LM will rewrite/expand your caption and metadata. If disabled, your caption stays as-is (recommended).")

        grid.addWidget(self.chk_thinking, r, 0, 1, 3)
        grid.addWidget(self.chk_lm_enhance, r, 3, 1, 3)
        r += 1

        # Row 2: LM sampling controls
        self.spin_lm_temp = QtWidgets.QDoubleSpinBox()
        self.spin_lm_temp.setRange(0.0, 2.0)
        self.spin_lm_temp.setDecimals(2)
        self.spin_lm_temp.setSingleStep(0.05)
        self.spin_lm_temp.setValue(0.85)
        self.spin_lm_temp.setToolTip("LM sampling temperature (0.0–2.0). Higher = more creative/less stable.")

        self.spin_lm_top_p = QtWidgets.QDoubleSpinBox()
        self.spin_lm_top_p.setRange(0.0, 1.0)
        self.spin_lm_top_p.setDecimals(2)
        self.spin_lm_top_p.setSingleStep(0.01)
        self.spin_lm_top_p.setValue(0.95)
        self.spin_lm_top_p.setToolTip("LM top-p / nucleus sampling (0.0–1.0). Lower = more conservative.")

        self.spin_lm_top_k = QtWidgets.QSpinBox()
        self.spin_lm_top_k.setRange(0, 200)
        self.spin_lm_top_k.setSingleStep(1)
        self.spin_lm_top_k.setValue(0)
        self.spin_lm_top_k.setToolTip("LM top-k sampling (0 = disabled). Lower = more conservative.")

        grid.addWidget(QtWidgets.QLabel("LM temp"), r, 0)
        grid.addWidget(self.spin_lm_temp, r, 1)
        grid.addWidget(QtWidgets.QLabel("LM top-p"), r, 2)
        grid.addWidget(self.spin_lm_top_p, r, 3)
        grid.addWidget(QtWidgets.QLabel("LM top-k"), r, 4)
        grid.addWidget(self.spin_lm_top_k, r, 5)
        r += 1

        # Enable/disable LM sampling controls based on 'Enable LM'
        try:
            self.chk_thinking.toggled.connect(self._update_lm_sampling_enabled)
        except Exception:
            pass
        self._update_lm_sampling_enabled()

        # Core generation settings (as in your screenshot)
        self.spin_duration = QtWidgets.QDoubleSpinBox()
        self.spin_duration.setRange(5.0, 600.0)
        self.spin_duration.setDecimals(1)
        self.spin_duration.setSingleStep(5.0)
        self.spin_duration.setToolTip(
            "Output duration in seconds.\n"
            "Range: 5–600.\n"
            "Default: 60.0."
        )

        self.spin_batch = QtWidgets.QSpinBox()
        self.spin_batch.setRange(1, 8)
        self.spin_batch.setToolTip(
            "How many variations to generate per run.\n"
            "Range: 1–8.\n"
            "Default: 1."
        )

        self.spin_seed = QtWidgets.QSpinBox()
        self.spin_seed.setRange(0, 2_147_483_647)
        self.spin_seed.setToolTip("Seed used for generation.")

        self.chk_seed_random = QtWidgets.QCheckBox("Random")
        self.chk_seed_random.setToolTip(
            "When enabled, a new random seed is generated for every run.\n"
            "The seed box shows the exact seed used for the last run."
        )

        grid.addWidget(QtWidgets.QLabel("Duration (s)"), r, 0)
        grid.addWidget(self.spin_duration, r, 1)
        grid.addWidget(QtWidgets.QLabel("Outputs"), r, 2)
        grid.addWidget(self.spin_batch, r, 3)
        grid.addWidget(QtWidgets.QLabel("Seed"), r, 4)
        # Put seed + random toggle side-by-side.
        seed_row = QtWidgets.QHBoxLayout()
        seed_row.setContentsMargins(0, 0, 0, 0)
        seed_row.setSpacing(8)
        seed_row.addWidget(self.spin_seed, 1)
        seed_row.addWidget(self.chk_seed_random, 0)
        seed_wrap = QtWidgets.QWidget()
        seed_wrap.setLayout(seed_row)
        grid.addWidget(seed_wrap, r, 5)
        r += 1

        # Random seed behavior: when enabled, we generate a new seed per run and
        # keep the seed box display in sync.
        try:
            self.chk_seed_random.toggled.connect(self._on_seed_random_toggled)
        except Exception:
            pass

        self.spin_bpm = QtWidgets.QSpinBox()
        self.spin_bpm.setRange(0, 300)
        self.spin_bpm.setToolTip("0 = auto (let ACE decide)")

        self.cmb_timesig = QtWidgets.QComboBox()
        self.cmb_timesig.addItem("auto", 0)
        for ts in (2, 3, 4, 5, 6, 7, 8, 9, 12):
            self.cmb_timesig.addItem(str(ts), ts)
        self.cmb_timesig.setToolTip(
            "Time signature.\n"
            "auto = let ACE decide.\n"
            "Default: auto."
        )

        self.cmb_keyscale = QtWidgets.QComboBox()
        self.cmb_keyscale.setEditable(True)
        self.cmb_keyscale.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        self.cmb_keyscale.addItem("auto", "")
        self.cmb_keyscale.setToolTip(
            "Key / scale hint for the generation.\n"
            "auto = let ACE decide.\n"
            "Default: auto."
        )
        for k in [
            "C major","C minor","C# major","C# minor","D major","D minor","D# major","D# minor",
            "E major","E minor","F major","F minor","F# major","F# minor","G major","G minor",
            "G# major","G# minor","A major","A minor","A# major","A# minor","B major","B minor",
        ]:
            self.cmb_keyscale.addItem(k, k)

        grid.addWidget(QtWidgets.QLabel("BPM"), r, 0)
        grid.addWidget(self.spin_bpm, r, 1)
        grid.addWidget(QtWidgets.QLabel("Time sig"), r, 2)
        grid.addWidget(self.cmb_timesig, r, 3)
        grid.addWidget(QtWidgets.QLabel("Key/scale"), r, 4)
        grid.addWidget(self.cmb_keyscale, r, 5)
        r += 1

        self.cmb_format = QtWidgets.QComboBox()
        self.cmb_format.addItems(["mp3", "wav", "flac"])
        self.cmb_format.setToolTip(
            "Output audio format.\n"
            "Default: mp3."
        )
        self.cmb_backend = QtWidgets.QComboBox()
        self.cmb_backend.addItems(["vllm", "pt", "mlx"])
        self.cmb_backend.setToolTip(
            "Inference backend.\n"
            "vllm = fastest on supported GPUs (default).\n"
            "pt = PyTorch backend.\n"
            "mlx = Apple Silicon backend.\n"
            "Default: vllm."
        )

        # ACE-Step Advanced DiT parameter: Shift (timestep shift factor)
        self.spin_shift = QtWidgets.QDoubleSpinBox()
        self.spin_shift.setRange(1.0, 5.0)
        self.spin_shift.setDecimals(2)
        self.spin_shift.setSingleStep(0.1)
        self.spin_shift.setValue(3.0)
        self.spin_shift.setToolTip(
            "Shift (timestep shift factor). Range 1.0–5.0. "
            "Recommended 3.0. Note: only effective for ACE-Step base models."
        )

        grid.addWidget(QtWidgets.QLabel("Audio format"), r, 0)
        grid.addWidget(self.cmb_format, r, 1)
        grid.addWidget(QtWidgets.QLabel("Backend"), r, 2)
        grid.addWidget(self.cmb_backend, r, 3)
        grid.addWidget(QtWidgets.QLabel("Shift"), r, 4)
        grid.addWidget(self.spin_shift, r, 5)
        r += 1

        # Extra generation controls: guidance_scale / infer_method / inference_steps
        self.spin_guidance = QtWidgets.QDoubleSpinBox()
        self.spin_guidance.setRange(0.0, 50.0)
        self.spin_guidance.setDecimals(2)
        self.spin_guidance.setSingleStep(0.5)
        self.spin_guidance.setToolTip("Guidance scale (0 = default/auto)")

        self.cmb_infer_method = QtWidgets.QComboBox()
        # IMPORTANT: ACE-Step 1.5 does NOT use the k-diffusion sampler names.
        # Upstream Gradio exposes only ODE and SDE, and the CLI expects "ode" or "sde".
        # Keeping "auto" as the safe default.
        self.cmb_infer_method.setEditable(False)
        self.cmb_infer_method.setToolTip("Inference method (auto recommended; ODE/SDE match the official Gradio UI)")
        self.cmb_infer_method.addItem("auto", ACE15_INFER_METHOD_AUTO)
        self.cmb_infer_method.addItem("ODE (Euler / deterministic)", ACE15_INFER_METHOD_ODE)
        self.cmb_infer_method.addItem("SDE (stochastic)", ACE15_INFER_METHOD_SDE)

        self.spin_steps = QtWidgets.QSpinBox()
        self.spin_steps.setRange(0, 500)
        self.spin_steps.setToolTip("Inference steps (0 = default/auto)")

        grid.addWidget(QtWidgets.QLabel("Guidance"), r, 0)
        grid.addWidget(self.spin_guidance, r, 1)
        grid.addWidget(QtWidgets.QLabel("Infer method"), r, 2)
        grid.addWidget(self.cmb_infer_method, r, 3)
        grid.addWidget(QtWidgets.QLabel("Steps"), r, 4)
        grid.addWidget(self.spin_steps, r, 5)
        r += 1

        # Add the grid to the group
        v.addLayout(grid)

        # Actions (sticky footer)
        # NOTE: This bar is intentionally NOT placed inside the scroll area, so it stays visible
        # while the user scrolls the settings page.
        self.btn_presets = QtWidgets.QPushButton("Music Genre Presets")
        self.btn_presets.setObjectName("ace15_btn_presets")
        self.btn_presets.clicked.connect(self._open_preset_manager)
        self.btn_save = QtWidgets.QPushButton("Save Settings")
        self.btn_save.setObjectName("ace15_btn_save")
        self.btn_save.clicked.connect(self._save_settings)
        self.btn_run = QtWidgets.QPushButton("Generate")
        self.btn_run.setObjectName("ace15_btn_generate")
        self.btn_run.clicked.connect(self._on_generate_clicked)

        # Prepare busy animation timer (text-only, no assets needed)
        try:
            self._gen_btn_base_text = self.btn_run.text() or "Generate"
        except Exception:
            self._gen_btn_base_text = "Generate"
        try:
            self._gen_anim_timer = QtCore.QTimer(self)
            self._gen_anim_timer.setInterval(350)
            self._gen_anim_timer.timeout.connect(self._tick_generate_anim)
        except Exception:
            self._gen_anim_timer = None

        self.btn_stop = QtWidgets.QPushButton("Stop")
        self.btn_stop.setObjectName("ace15_btn_stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)

        page_l.addWidget(gb_simple, 0)

        # Sticky footer bar (bottom, outside scroll)
        def _bump_font_px(btn: QtWidgets.QAbstractButton, px: int):
            f = btn.font()
            if f.pixelSize() > 0:
                f.setPixelSize(max(6, f.pixelSize() + px))
            else:
                # pointSizeF can be -1 on some platforms; fall back to pointSize
                ps = f.pointSizeF() if f.pointSizeF() > 0 else float(max(6, f.pointSize()))
                f.setPointSizeF(max(6.0, ps + float(px)))
            btn.setFont(f)

        self._footer_bar = QtWidgets.QWidget()
        self._footer_bar.setObjectName("ace15_footer_bar")
        footer = QtWidgets.QHBoxLayout(self._footer_bar)
        footer.setContentsMargins(12, 8, 12, 8)
        footer.setSpacing(10)

        # Match the user's request: +3px font size for these buttons.
        for b in (self.btn_presets, self.btn_save, self.btn_run, self.btn_stop):
            _bump_font_px(b, 3)
            b.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
            # Let buttons grow if the font makes them taller.
            b.setMinimumHeight(42)

        # Keep Generate button width stable while animating (reserve space for 'Generating...')
        try:
            fm = QtGui.QFontMetrics(self.btn_run.font())
            target = fm.horizontalAdvance("Generating ⠋  (click to queue)") + 28
            if self.btn_run.minimumWidth() < target:
                self.btn_run.setMinimumWidth(target)
        except Exception:
            pass

        # Hover style for footer buttons: blue→green gradient (same as Generate)
        self._footer_bar.setStyleSheet(
            "QPushButton#ace15_btn_generate:hover,"
            "QPushButton#ace15_btn_presets:hover,"
            "QPushButton#ace15_btn_save:hover,"
            "QPushButton#ace15_btn_stop:hover {"
            "  color: #e8f5e9;"
            "  background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1e88e5, stop:1 #1b5e20);"
            "}"
            "QPushButton#ace15_btn_generate:pressed,"
            "QPushButton#ace15_btn_presets:pressed,"
            "QPushButton#ace15_btn_save:pressed,"
            "QPushButton#ace15_btn_stop:pressed {"
            "  color: #e8f5e9;"
            "  background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1565c0, stop:1 #1b5e20);"
            "}"
        )


        footer.addStretch(1)
        # User request: put Generate first.
        footer.addWidget(self.btn_run)
        footer.addWidget(self.btn_presets)
        footer.addWidget(self.btn_save)
        footer.addWidget(self.btn_stop)

        # Add the sticky footer AFTER the scroll area so it stays at the bottom.
        outer.addWidget(self._footer_bar, 0)

        # -------------------------
        # Preview Player (single shared pane)
        # -------------------------
        gb_preview = QtWidgets.QGroupBox("Preview")
        pv = QtWidgets.QVBoxLayout(gb_preview)
        pv.setContentsMargins(12, 10, 12, 12)
        pv.setSpacing(8)

        self.lbl_preview_hint = QtWidgets.QLabel("Double click a result to play")
        self.lbl_preview_hint.setObjectName("ace15_preview_hint")
        pv.addWidget(self.lbl_preview_hint, 0)

        self.lbl_preview_now = QtWidgets.QLabel("")
        self.lbl_preview_now.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.lbl_preview_now.setObjectName("ace15_preview_now")
        pv.addWidget(self.lbl_preview_now, 0)

        row_controls = QtWidgets.QHBoxLayout()
        row_controls.setSpacing(8)

        self.btn_preview_play = QtWidgets.QPushButton("Play")
        self.btn_preview_stop = QtWidgets.QPushButton("Stop")
        self.btn_preview_open = QtWidgets.QPushButton("Open file")
        self.btn_preview_play.setEnabled(False)
        self.btn_preview_stop.setEnabled(False)
        self.btn_preview_open.setEnabled(False)

        row_controls.addWidget(self.btn_preview_play)
        row_controls.addWidget(self.btn_preview_stop)
        row_controls.addWidget(self.btn_preview_open)
        row_controls.addStretch(1)

        self.lbl_preview_time = QtWidgets.QLabel("0:00 / 0:00")
        row_controls.addWidget(self.lbl_preview_time, 0)
        pv.addLayout(row_controls)

        self.sld_preview = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.sld_preview.setRange(0, 1000)
        self.sld_preview.setEnabled(False)
        pv.addWidget(self.sld_preview, 0)

        # Best-effort internal audio playback (QtMultimedia)
        self._preview_path: Optional[Path] = None
        self._preview_dragging = False
        self._preview_player = None
        self._preview_audio = None
        if QtMultimedia is not None:
            try:
                self._preview_player = QtMultimedia.QMediaPlayer(self)
                self._preview_audio = QtMultimedia.QAudioOutput(self)
                self._preview_player.setAudioOutput(self._preview_audio)
            except Exception:
                self._preview_player = None
                self._preview_audio = None

        self.btn_preview_play.clicked.connect(self._preview_toggle_play)
        self.btn_preview_stop.clicked.connect(self._preview_stop)
        self.btn_preview_open.clicked.connect(self._preview_open_file)
        self.sld_preview.sliderPressed.connect(self._preview_slider_pressed)
        self.sld_preview.sliderReleased.connect(self._preview_slider_released)
        self.sld_preview.sliderMoved.connect(self._preview_slider_moved)

        if self._preview_player is not None:
            try:
                self._preview_player.positionChanged.connect(self._preview_on_position_changed)
                self._preview_player.durationChanged.connect(self._preview_on_duration_changed)
                self._preview_player.playbackStateChanged.connect(self._preview_on_state_changed)
            except Exception:
                pass

        page_l.addWidget(gb_preview, 0)

        # Results list (double-click plays in Preview above)
        gb_recent = QtWidgets.QGroupBox("Results")
        r = QtWidgets.QVBoxLayout(gb_recent)

        self.lst_outputs = QtWidgets.QListWidget()
        self.lst_outputs.itemDoubleClicked.connect(self._open_selected_output)
        # Ensure the results list can show at least ~5 items by default.
        try:
            fm = QtGui.QFontMetrics(self.lst_outputs.font())
            row_h = int(fm.lineSpacing() + 10)
            self.lst_outputs.setMinimumHeight(int(row_h * 5 + 8))
        except Exception:
            pass
        r.addWidget(self.lst_outputs, 1)

        row_out = QtWidgets.QHBoxLayout()
        btn_refresh = QtWidgets.QPushButton("Refresh")
        btn_refresh.clicked.connect(self._refresh_outputs)
        btn_open = QtWidgets.QPushButton("Open output folder")
        btn_open.clicked.connect(self._open_output_folder)
        row_out.addWidget(btn_refresh)
        row_out.addWidget(btn_open)
        row_out.addStretch(1)
        r.addLayout(row_out)

        # Give Results a bit more room by default.
        page_l.addWidget(gb_recent, 1)

        # -------------------------
        # Advanced Settings (moved to Advanced tab)
        # -------------------------
        self.adv_main_widget = QtWidgets.QWidget()
        adv_l = QtWidgets.QVBoxLayout(self.adv_main_widget)
        adv_l.setContentsMargins(0, 0, 0, 0)
        adv_l.setSpacing(12)

        # Section 1: Task / Inputs
        gb_task = QtWidgets.QGroupBox("Task & Inputs")
        task_l = QtWidgets.QVBoxLayout(gb_task)

        self.cmb_task = QtWidgets.QComboBox()
        self.cmb_task.addItems(["text2music", "cover", "repaint", "lego", "extract", "complete"])
        task_l.addWidget(QtWidgets.QLabel("Task type"))
        task_l.addWidget(self.cmb_task)

        gb_src = QtWidgets.QGroupBox("Task inputs (for cover/repaint/lego/extract/complete)")
        src = QtWidgets.QFormLayout(gb_src)
        self.ed_src_audio = QtWidgets.QLineEdit()
        btn_src = QtWidgets.QPushButton("Browse…")
        btn_src.clicked.connect(self._pick_src_audio)
        src.addRow("Source audio", self._row(self.ed_src_audio, btn_src))

        self.ed_repaint_start = QtWidgets.QDoubleSpinBox()
        self.ed_repaint_start.setRange(0.0, 10_000.0)
        self.ed_repaint_start.setDecimals(2)
        self.ed_repaint_end = QtWidgets.QDoubleSpinBox()
        self.ed_repaint_end.setRange(-1.0, 10_000.0)
        self.ed_repaint_end.setDecimals(2)
        self.ed_repaint_end.setValue(-1.0)
        src.addRow("Repaint start (s)", self.ed_repaint_start)
        src.addRow("Repaint end (s)", self.ed_repaint_end)

        self.ed_track = QtWidgets.QLineEdit()
        self.ed_track.setPlaceholderText("track name (lego/extract)")
        src.addRow("Track", self.ed_track)

        self.ed_complete_tracks = QtWidgets.QLineEdit()
        self.ed_complete_tracks.setPlaceholderText("comma-separated tracks (complete)")
        src.addRow("Complete tracks", self.ed_complete_tracks)

        task_l.addWidget(gb_src)
        adv_l.addWidget(gb_task)



        # Section 2: Paths & Models
        gb_paths = QtWidgets.QGroupBox("Paths & Models")
        gb_paths_l = QtWidgets.QVBoxLayout(gb_paths)

        row_status = QtWidgets.QHBoxLayout()
        self.lbl_paths_status = QtWidgets.QLabel("")
        self.lbl_paths_status.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.btn_rescan_paths = QtWidgets.QPushButton("Re-scan")
        self.btn_rescan_paths.setToolTip("Auto-detect all required paths again from the FrameVision root.")
        self.btn_rescan_paths.clicked.connect(self._auto_detect_paths)
        row_status.addWidget(self.lbl_paths_status, 1)
        row_status.addWidget(self.btn_rescan_paths, 0)
        gb_paths_l.addLayout(row_status)

        form = QtWidgets.QFormLayout()
        gb_paths_l.addLayout(form)

        self.ed_outdir = QtWidgets.QLineEdit()
        btn_out = QtWidgets.QPushButton("Browse…")
        btn_out.clicked.connect(self._pick_output_dir)
        form.addRow("Output folder", self._row(self.ed_outdir, btn_out))

        self.cmb_main_model = QtWidgets.QComboBox()
        self.cmb_main_model.setToolTip(
            "Select the main ACE-Step v1.5 model (Base/Turbo). "
            "First use can auto-download into checkpoints/ (progress in logs)."
        )
        self.btn_refresh_main = QtWidgets.QPushButton("Refresh model list")
        self.btn_refresh_main.setToolTip("Scan project_root/checkpoints and repopulate this list.")
        self.btn_refresh_main.clicked.connect(self._refresh_main_models)
        try:
            self.cmb_main_model.currentIndexChanged.connect(self._update_shift_ui)
        except Exception:
            pass
        form.addRow("Main model", self._row(self.cmb_main_model, self.btn_refresh_main))

        self.cmb_lm_model = QtWidgets.QComboBox()
        self.cmb_lm_model.setToolTip("Select a specific LM model. First use will auto-download into checkpoints/ (progress in logs).")
        self.btn_refresh_lm = QtWidgets.QPushButton("Refresh LM list")
        self.btn_refresh_lm.setToolTip("Scan project_root/checkpoints and repopulate this list.")
        self.btn_refresh_lm.clicked.connect(self._refresh_lm_models)
        form.addRow("LLM model", self._row(self.cmb_lm_model, self.btn_refresh_lm))

        # Advanced paths (read-only)
        self.adv_toggle = QtWidgets.QToolButton()
        self.adv_toggle.setText("Show detected paths")
        self.adv_toggle.setCheckable(True)
        self.adv_toggle.setChecked(False)
        self.adv_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        gb_paths_l.addWidget(self.adv_toggle, 0, QtCore.Qt.AlignLeft)

        self.adv_widget = QtWidgets.QWidget()
        adv_form = QtWidgets.QFormLayout(self.adv_widget)

        self.ed_fvroot = QtWidgets.QLineEdit()
        self.ed_fvroot.setReadOnly(True)
        adv_form.addRow("FrameVision root", self.ed_fvroot)

        self.ed_envpy = QtWidgets.QLineEdit()
        self.ed_envpy.setReadOnly(True)
        adv_form.addRow("Env python", self.ed_envpy)

        self.ed_clipypy = QtWidgets.QLineEdit()
        self.ed_clipypy.setReadOnly(True)
        adv_form.addRow("ACE cli.py", self.ed_clipypy)

        self.ed_projectroot = QtWidgets.QLineEdit()
        self.ed_projectroot.setReadOnly(True)
        adv_form.addRow("Project root", self.ed_projectroot)

        self.adv_widget.setVisible(False)
        gb_paths_l.addWidget(self.adv_widget)

        self.adv_toggle.toggled.connect(lambda on: self.adv_widget.setVisible(on))

        adv_l.addWidget(gb_paths)

        # Section 3: Performance / Memory
        gb_perf = QtWidgets.QGroupBox("Performance / Memory")
        perf = QtWidgets.QGridLayout(gb_perf)
        perf.setHorizontalSpacing(16)
        perf.setVerticalSpacing(8)

        self.chk_offload = QtWidgets.QCheckBox("Offload to CPU (LM)")
        self.chk_offload_dit = QtWidgets.QCheckBox("Offload DiT to CPU")
        self.chk_flashattn = QtWidgets.QCheckBox("Use Flash Attention")

        self.chk_keep_in_vram = QtWidgets.QCheckBox("Keep in VRAM (needs restart)")
        self.chk_keep_in_vram.setToolTip(
            "When enabled (after restarting the app), Ace-Step will start the local FastAPI server at launch\n"
            "and generation will run via the API instead of spawning cli.py each time.\n"
            "This keeps the model in VRAM between runs."
        )
        self.chk_keep_in_vram.toggled.connect(lambda _on: self._log("Keep in VRAM changed — restart required to take effect."))

        perf.addWidget(self.chk_offload_dit, 0, 0)
        perf.addWidget(self.chk_offload, 0, 1)
        perf.addWidget(self.chk_flashattn, 1, 0)
        perf.addWidget(self.chk_keep_in_vram, 1, 1)

        adv_l.addWidget(gb_perf)

        # Section 4: Options (removed)
        # Keep behavior deterministic: both options are forced OFF.
        self.chk_hide_console = QtWidgets.QCheckBox()
        self.chk_hide_console.setChecked(False)
        self.chk_hide_console.setEnabled(False)
        self.chk_hide_console.setVisible(False)

        self.chk_auto_open = QtWidgets.QCheckBox()
        self.chk_auto_open.setChecked(False)
        self.chk_auto_open.setEnabled(False)
        self.chk_auto_open.setVisible(False)

        # Section 5 (LAST): Logs
        gb_logs = QtWidgets.QGroupBox("Logs")
        logs_l = QtWidgets.QVBoxLayout(gb_logs)

        row_ll = QtWidgets.QHBoxLayout()
        self.cmb_loglevel = QtWidgets.QComboBox()
        self.cmb_loglevel.addItems(["INFO", "DEBUG", "WARNING", "ERROR"])
        row_ll.addWidget(QtWidgets.QLabel("Log level"))
        row_ll.addWidget(self.cmb_loglevel)
        row_ll.addStretch(1)
        logs_l.addLayout(row_ll)

        self.lbl_status = QtWidgets.QLabel("Idle")
        logs_l.addWidget(self.lbl_status)

        self.txt_log = QtWidgets.QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        # Make log box 100% taller (about 16 lines)
        try:
            fm = QtGui.QFontMetrics(self.txt_log.font())
            self.txt_log.setMinimumHeight(int(fm.lineSpacing() * 16 + 22))
        except Exception:
            pass
        logs_l.addWidget(self.txt_log, 1)

        adv_l.addWidget(gb_logs)

        adv_l.addStretch(1)

        adv_page_l.addWidget(self.adv_main_widget, 0)

        # Menu
        m = self.menuBar()        
        # Themes menu (25 themes from themes.py)
        if list_themes is not None and apply_theme is not None:
            tm = m.addMenu("&Themes")
            grp = QtGui.QActionGroup(self)
            grp.setExclusive(True)
            for tname in list_themes():
                act = QtGui.QAction(tname, self)
                act.setCheckable(True)
                act.setChecked(tname == getattr(self, "_current_theme", "Signal Grey"))
                act.triggered.connect(lambda _=False, n=tname, g=grp: self._on_theme_selected_simple(n, g))
                grp.addAction(act)
                tm.addAction(act)

        hm = m.addMenu("&Help")
        # Moved from the old Info menu: keep Banner toggle under Help.
        self.act_banner = QtGui.QAction("Banner", self)
        self.act_banner.setCheckable(True)
        self.act_banner.setChecked(bool(getattr(self.settings, "banner_enabled", True)))
        self.act_banner.setToolTip("Show/hide the fancy banner at the top.")
        self.act_banner.toggled.connect(self._toggle_banner)
        hm.addAction(self.act_banner)

        hm.addSeparator()
        self.act_wheel_guard = QtGui.QAction("Wheel guard", self)
        self.act_wheel_guard.setCheckable(True)
        self.act_wheel_guard.setChecked(bool(getattr(self.settings, "wheel_guard_enabled", True)))
        self.act_wheel_guard.setToolTip("Prevents mouse wheel from changing settings while scrolling.")
        self.act_wheel_guard.toggled.connect(self._toggle_wheel_guard)
        hm.addAction(self.act_wheel_guard)

        hm.addSeparator()
        self.act_update = QtGui.QAction("Update", self)
        self.act_update.setToolTip("Check GitHub for updates and apply newer files.")
        self.act_update.triggered.connect(self._on_update_clicked)
        hm.addAction(self.act_update)

        hm.addSeparator()
        about = QtGui.QAction("About", self)
        about.triggered.connect(self._about)
        hm.addAction(about)

        # -----------------------------
        # Themes (simple, manual only)
        # -----------------------------

    def _apply_theme_simple(self, name: str) -> None:
        """Apply a theme by name using themes.py (no auto switching)."""
        try:
            if apply_theme is None:
                return
            n = (name or "Signal Grey").strip() or "Signal Grey"
            # If any legacy value like "Auto" slips in, force a stable default.
            if n.lower() == "auto":
                n = "Evening"
            app = QtWidgets.QApplication.instance()
            if app is not None:
                apply_theme(app, n)
        except Exception:
            pass

    def _on_theme_selected_simple(self, name: str, action_group: QtGui.QActionGroup) -> None:
        self._current_theme = (name or "Signal Grey").strip() or "Signal Grey"
        self._apply_theme_simple(self._current_theme)
        try:
            self.settings.ui_theme = self._current_theme
            self._save_settings()
        except Exception:
            pass
        # Keep menu checkmarks in sync.
        try:
            for a in action_group.actions():
                a.setChecked(a.text() == self._current_theme)
        except Exception:
            pass

    def _row(self, edit: QtWidgets.QWidget, button: QtWidgets.QPushButton) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        h.addWidget(button)
        return w

    def _load_settings(self) -> Settings:
        s = Settings()
        fv = getattr(self, "fv_root_guess", guess_framevision_root())
        s.framevision_root = str(fv)
        s.env_python = str(default_env_python(fv))
        s.cli_py = str(default_ace_cli_py(fv))
        # Default output folder (FrameVision root + /output/audio/ace15/)
        s.output_dir = str(fv / "output" / "audio" / "ace15")
        s.project_root = str(default_ace_project_root(fv))
        load_path = self.settings_path
        legacy_path = Path(__file__).resolve().with_name(SETTINGS_JSON)
        pointer_path = read_last_settings_path(self.fv_root_guess)
        # Prefer the last-known settings file (if it exists), then the detected FV-root path,
        # then the legacy file next to the script.
        if pointer_path is not None:
            load_path = pointer_path
        elif not load_path.exists() and legacy_path.exists():
            load_path = legacy_path
        if load_path.exists():
            try:
                s = Settings.from_dict(json.loads(load_path.read_text(encoding="utf-8")))
            except Exception:
                pass

        # If the loaded settings point to a different FrameVision root, use that for future saves.
        try:
            fv2 = Path((s.framevision_root or "").strip()) if (s.framevision_root or "").strip() else fv
            self.settings_path = settings_path_for_root(fv2)
        except Exception:
            pass

        # Migration: older builds used /output/ace_step_15/ as the default.
        # If user never customized it (or it's empty), switch to the new default.
        try:
            old_default = str(fv / "output" / "ace_step_15")
            new_default = str(fv / "output" / "audio" / "ace15")
            if not (s.output_dir or "").strip() or os.path.normpath(s.output_dir) == os.path.normpath(old_default):
                s.output_dir = new_default
        except Exception:
            pass

        # Migration: older builds used seed=-1 to mean random.
        # New builds use a dedicated Random toggle and keep the visible seed non-negative.
        try:
            if int(getattr(s, 'seed', 0) or 0) < 0:
                s.seed = 0
                s.seed_random = True
        except Exception:
            pass
        return s

    def _auto_detect_paths(self):
        """
        Auto-detect FrameVision-root-relative paths and update both settings + UI.
        Keeps user-customized output folder if it is not obviously a legacy/wrong default.
        """
        try:
            fv = guess_framevision_root()
        except Exception:
            fv = Path(os.getcwd())

        # Core paths (match the locations in your screenshot)
        envpy = default_env_python(fv)
        proj = default_ace_project_root(fv)
        clipy = default_ace_cli_py(fv)

        # Output default
        new_out_default = fv / "output" / "audio" / "ace15"

        # Update settings
        s = self.settings
        s.framevision_root = str(fv)
        s.env_python = str(envpy)
        s.project_root = str(proj)
        s.cli_py = str(clipy)

        # Output dir: keep user's choice unless it's empty or points to known legacy defaults
        try:
            cur_out = (s.output_dir or "").strip()
            legacy1 = str(fv / "output" / "ace_step_15")
            legacy2 = str(fv / "helpers" / "output" / "audio" / "ace15")
            if (not cur_out) or os.path.normpath(cur_out) in (os.path.normpath(legacy1), os.path.normpath(legacy2)):
                s.output_dir = str(new_out_default)
        except Exception:
            if not (s.output_dir or "").strip():
                s.output_dir = str(new_out_default)

        # Create output folder if needed
        try:
            ensure_dir(Path(s.output_dir))
        except Exception:
            pass

        # Push into UI fields if present
        try:
            if hasattr(self, "ed_fvroot"):
                self.ed_fvroot.setText(s.framevision_root)
            if hasattr(self, "ed_envpy"):
                self.ed_envpy.setText(s.env_python)
            if hasattr(self, "ed_projectroot"):
                self.ed_projectroot.setText(s.project_root)
            if hasattr(self, "ed_clipypy"):
                self.ed_clipypy.setText(s.cli_py)
            if hasattr(self, "ed_outdir"):
                self.ed_outdir.setText(s.output_dir)
        except Exception:
            pass

        # Status label for quick debugging
        try:
            missing = []
            if not Path(s.env_python).exists():
                missing.append("env python")
            if not Path(s.cli_py).exists():
                missing.append("cli.py")
            if not Path(s.project_root).exists():
                missing.append("project root")
            if hasattr(self, "lbl_paths_status"):
                if missing:
                    self.lbl_paths_status.setText("Missing: " + ", ".join(missing))
                else:
                    self.lbl_paths_status.setText("Auto paths: OK")
        except Exception:
            pass


    def _pull_ui_to_settings(self):
        s = self.settings
        s.framevision_root = self.ed_fvroot.text().strip()
        s.env_python = self.ed_envpy.text().strip()
        s.cli_py = self.ed_clipypy.text().strip()
        s.project_root = self.ed_projectroot.text().strip()
        s.output_dir = self.ed_outdir.text().strip()
        s.hide_console = False  # forced OFF (UI removed)
        s.auto_open_output = False  # forced OFF (UI removed)

        # UI prefs
        try:
            s.ui_theme = (getattr(self, "_current_theme", "Signal Grey") or "Signal Grey").strip() or "Signal Grey"
        except Exception:
            s.ui_theme = "Signal Grey"
        try:
            if hasattr(self, "act_wheel_guard"):
                s.wheel_guard_enabled = bool(self.act_wheel_guard.isChecked())
        except Exception:
            pass

        s.task_type = self.cmb_task.currentText().strip()
        s.backend = self.cmb_backend.currentText().strip()
        if hasattr(self, 'spin_shift'):
            try:
                s.shift = float(self.spin_shift.value())
            except Exception:
                pass
        s.log_level = self.cmb_loglevel.currentText().strip()
        s.audio_format = self.cmb_format.currentText().strip()
        s.duration = float(self.spin_duration.value())
        s.batch_size = int(self.spin_batch.value())
        s.seed = int(self.spin_seed.value())
        if hasattr(self, 'chk_seed_random'):
            s.seed_random = bool(self.chk_seed_random.isChecked())
        s.bpm = int(self.spin_bpm.value()) if hasattr(self, 'spin_bpm') else 0
        s.timesignature = int(self.cmb_timesig.currentData() or 0) if hasattr(self, 'cmb_timesig') else 0
        ks = self.cmb_keyscale.currentData() if hasattr(self, 'cmb_keyscale') else ""
        if ks is None and hasattr(self, 'cmb_keyscale'):
            ks = self.cmb_keyscale.currentText()
        s.keyscale = str(ks or "")
        # Vocal language
        if hasattr(self, 'cmb_vocal_language'):
            vl = str(self.cmb_vocal_language.currentData() or "").strip()
            if not vl and self.cmb_vocal_language.isEditable():
                vl = self.cmb_vocal_language.currentText().strip()
            if vl.lower() == "auto":
                vl = ""
            s.vocal_language = vl
        s.enable_lm = self.chk_thinking.isChecked()
        s.thinking = self.chk_thinking_mode.isChecked() if hasattr(self, 'chk_thinking_mode') else False
        s.parallel_thinking = self.chk_parallel_thinking.isChecked() if hasattr(self, 'chk_parallel_thinking') else False
        if hasattr(self, 'chk_lm_enhance'):
            s.lm_enhance = self.chk_lm_enhance.isChecked()

        # LM sampling controls
        if hasattr(self, 'spin_lm_temp'):
            s.lm_temperature = float(self.spin_lm_temp.value())
        if hasattr(self, 'spin_lm_top_p'):
            s.lm_top_p = float(self.spin_lm_top_p.value())
        if hasattr(self, 'spin_lm_top_k'):
            s.lm_top_k = int(self.spin_lm_top_k.value())
        s.offload_to_cpu = self.chk_offload.isChecked()
        s.offload_dit_to_cpu = self.chk_offload_dit.isChecked()
        s.use_flash_attention = self.chk_flashattn.isChecked()
        if hasattr(self, 'chk_keep_in_vram'):
            s.keep_in_vram = bool(self.chk_keep_in_vram.isChecked())

        # Generation controls
        if hasattr(self, 'spin_guidance'):
            s.guidance_scale = float(self.spin_guidance.value())
        if hasattr(self, 'cmb_infer_method'):
            im = str(self.cmb_infer_method.currentData() or '').strip()
            if not im and self.cmb_infer_method.isEditable():
                im = self.cmb_infer_method.currentText().strip()
            s.infer_method = _ace15_normalize_infer_method(im)
        if hasattr(self, 'spin_steps'):
            s.inference_steps = int(self.spin_steps.value())
        # Main model (optional)
        if hasattr(self, 'cmb_main_model'):
            s.main_model_path = self.cmb_main_model.currentData() or ""
        # LM model (optional)
        if hasattr(self, 'cmb_lm_model'):
            s.lm_model_path = self.cmb_lm_model.currentData() or ""

        # Negatives (LM guidance)
        if hasattr(self, 'ed_negatives'):
            s.lm_negative_prompt = self.ed_negatives.toPlainText().strip()

    def _apply_settings_to_ui(self):
        s = self.settings
        self.ed_fvroot.setText(s.framevision_root)
        self.ed_envpy.setText(s.env_python)
        self.ed_clipypy.setText(s.cli_py)
        self.ed_projectroot.setText(s.project_root)
        self.ed_outdir.setText(s.output_dir)
        self.chk_hide_console.setChecked(False)  # forced OFF (UI removed)
        self.chk_auto_open.setChecked(False)  # forced OFF (UI removed)

        if hasattr(self, 'chk_keep_in_vram'):
            try:
                self.chk_keep_in_vram.setChecked(bool(getattr(s, 'keep_in_vram', False)))
            except Exception:
                self.chk_keep_in_vram.setChecked(False)

        self._set_combo(self.cmb_task, s.task_type)
        self._set_combo(self.cmb_backend, s.backend)
        if hasattr(self, 'spin_shift'):
            try:
                self.spin_shift.setValue(float(getattr(s, 'shift', 3.0) or 3.0))
            except Exception:
                self.spin_shift.setValue(3.0)
        self._set_combo(self.cmb_loglevel, s.log_level)
        self._set_combo(self.cmb_format, s.audio_format)
        self.spin_duration.setValue(float(s.duration))
        self.spin_batch.setValue(int(s.batch_size))
        self.spin_seed.setValue(int(s.seed))
        if hasattr(self, 'chk_seed_random'):
            # Legacy migration: older builds used seed=-1 to mean random.
            try:
                legacy_random = (int(getattr(s, 'seed', 0) or 0) < 0)
            except Exception:
                legacy_random = False
            use_random = bool(getattr(s, 'seed_random', False) or legacy_random)
            try:
                self.chk_seed_random.blockSignals(True)
                self.chk_seed_random.setChecked(use_random)
            finally:
                self.chk_seed_random.blockSignals(False)
            try:
                self.spin_seed.setEnabled(not bool(use_random))
            except Exception:
                pass

        # Negatives (LM guidance)
        if hasattr(self, 'ed_negatives'):
            try:
                self.ed_negatives.blockSignals(True)
                self.ed_negatives.setPlainText(str(getattr(s, 'lm_negative_prompt', '') or ''))
            finally:
                self.ed_negatives.blockSignals(False)

        if hasattr(self, 'spin_bpm'):
            self.spin_bpm.setValue(int(getattr(s, 'bpm', 0) or 0))
        if hasattr(self, 'cmb_timesig'):
            ts_val = int(getattr(s, 'timesignature', 0) or 0)
            i = self.cmb_timesig.findData(ts_val)
            if i >= 0:
                self.cmb_timesig.setCurrentIndex(i)
        if hasattr(self, 'cmb_keyscale'):
            ks_val = str(getattr(s, 'keyscale', '') or '')
            if ks_val:
                i = self.cmb_keyscale.findData(ks_val)
                if i >= 0:
                    self.cmb_keyscale.setCurrentIndex(i)
                else:
                    self.cmb_keyscale.setEditText(ks_val)
            else:
                self.cmb_keyscale.setCurrentIndex(0)

        # Vocal language
        if hasattr(self, 'cmb_vocal_language'):
            vl_val = str(getattr(s, 'vocal_language', '') or '').strip()
            if not vl_val:
                i = self.cmb_vocal_language.findData("")
                if i >= 0:
                    self.cmb_vocal_language.setCurrentIndex(i)
                else:
                    self.cmb_vocal_language.setEditText("auto")
            else:
                i = self.cmb_vocal_language.findData(vl_val)
                if i >= 0:
                    self.cmb_vocal_language.setCurrentIndex(i)
                else:
                    self.cmb_vocal_language.setEditText(vl_val)

        self.chk_thinking.setChecked(bool(getattr(s, 'enable_lm', False)))
        if hasattr(self, 'chk_thinking_mode'):
            self.chk_thinking_mode.setChecked(bool(getattr(s, 'thinking', False)))
        if hasattr(self, 'chk_parallel_thinking'):
            self.chk_parallel_thinking.setChecked(bool(getattr(s, 'parallel_thinking', False)))
        if hasattr(self, 'chk_lm_enhance'):
            self.chk_lm_enhance.setChecked(bool(getattr(s, 'lm_enhance', False)))

        # LM sampling controls
        if hasattr(self, 'spin_lm_temp'):
            try:
                self.spin_lm_temp.setValue(float(getattr(s, 'lm_temperature', 0.85) or 0.85))
            except Exception:
                self.spin_lm_temp.setValue(0.85)
        if hasattr(self, 'spin_lm_top_p'):
            try:
                self.spin_lm_top_p.setValue(float(getattr(s, 'lm_top_p', 0.95) or 0.95))
            except Exception:
                self.spin_lm_top_p.setValue(0.95)
        if hasattr(self, 'spin_lm_top_k'):
            try:
                self.spin_lm_top_k.setValue(int(getattr(s, 'lm_top_k', 0) or 0))
            except Exception:
                self.spin_lm_top_k.setValue(0)

        self._update_lm_sampling_enabled()
        self.chk_offload.setChecked(bool(s.offload_to_cpu))
        self.chk_offload_dit.setChecked(bool(s.offload_dit_to_cpu))
        self.chk_flashattn.setChecked(bool(s.use_flash_attention))

        # Generation controls
        if hasattr(self, 'spin_guidance'):
            self.spin_guidance.setValue(float(getattr(s, 'guidance_scale', 0.0) or 0.0))
        if hasattr(self, 'cmb_infer_method'):
            im = _ace15_normalize_infer_method(str(getattr(s, 'infer_method', '') or '').strip())
            if not im:
                self.cmb_infer_method.setCurrentIndex(0)
            else:
                i = self.cmb_infer_method.findData(im)
                if i >= 0:
                    self.cmb_infer_method.setCurrentIndex(i)
                else:
                    if self.cmb_infer_method.isEditable():
                        self.cmb_infer_method.setEditText(im)
                    else:
                        self.cmb_infer_method.setCurrentIndex(0)
        if hasattr(self, 'spin_steps'):
            self.spin_steps.setValue(int(getattr(s, 'inference_steps', 0) or 0))
        # Refresh Main model list and restore selection
        if hasattr(self, 'cmb_main_model'):
            self._refresh_main_models()
            if getattr(s, 'main_model_path', ''):
                i = self.cmb_main_model.findData(s.main_model_path)
                if i >= 0:
                    self.cmb_main_model.setCurrentIndex(i)
        # Refresh LM list and restore selection
        if hasattr(self, 'cmb_lm_model'):
            self._refresh_lm_models()
            if getattr(s, 'lm_model_path', ''):
                i = self.cmb_lm_model.findData(s.lm_model_path)
                if i >= 0:
                    self.cmb_lm_model.setCurrentIndex(i)

        # Update Shift availability based on restored main model selection
        try:
            self._update_shift_ui()
        except Exception:
            pass

    def _update_lm_sampling_enabled(self) -> None:
        """Enable/disable LM sampling controls based on 'Enable LM'."""
        try:
            on = bool(self.chk_thinking.isChecked()) if hasattr(self, "chk_thinking") else False
        except Exception:
            on = False

        # Parallel thinking only makes sense if LM is enabled.
        if hasattr(self, "chk_parallel_thinking"):
            try:
                self.chk_parallel_thinking.setEnabled(on)
            except Exception:
                pass
        for attr in ("spin_lm_temp", "spin_lm_top_p", "spin_lm_top_k"):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    w.setEnabled(on)
                except Exception:
                    pass



    def _ace15_shift_supported(self, main_sel: str) -> bool:
        """Return True if the selected main model supports the DiT 'shift' parameter.

        Heuristic:
        - Empty/auto selection: treat as supported (ACE will pick base where needed).
        - Base models: supported.
        - Turbo/SFT/RL/distilled variants: not supported (shift can break results).
        """
        s = (main_sel or "").strip().lower()
        if not s or s == "auto":
            return False
        if "base" in s and ("turbo" not in s) and ("sft" not in s) and ("rl" not in s) and ("distill" not in s):
            return True
        return False

    def _update_shift_ui(self) -> None:
        """Enable/disable Shift based on the selected main model."""
        if not hasattr(self, "spin_shift") or not hasattr(self, "cmb_main_model"):
            return
        try:
            main_sel = str(self.cmb_main_model.currentData() or "").strip()
        except Exception:
            main_sel = ""

        supported = self._ace15_shift_supported(main_sel)

        try:
            self.spin_shift.setEnabled(bool(supported))
        except Exception:
            pass

        # Tooltip should explain what's happening.
        try:
            if supported:
                self.spin_shift.setToolTip(
                    "Shift (timestep shift factor). Range 1.0–5.0.\n\n"
                    "Recommended default: 3.0.\n\n"
                    "Only effective for ACE-Step base models."
                )
            else:
                self.spin_shift.setToolTip(
                    "Shift is NOT supported by the selected main model.\n\n"
                    "It will be ignored automatically to avoid broken output.\n\n"
                    "(Switch Main model back to a Base model to enable.)"
                )
        except Exception:
            pass

    def _set_combo(self, cmb: QtWidgets.QComboBox, value: str):
        idx = cmb.findText(value)
        if idx >= 0:
            cmb.setCurrentIndex(idx)

    def _save_settings(self):
        self._pull_ui_to_settings()
        try:
            fv = Path((self.settings.framevision_root or "").strip()) if (self.settings.framevision_root or "").strip() else getattr(self, "fv_root_guess", guess_framevision_root())
            self.settings_path = settings_path_for_root(fv)
            ensure_dir(self.settings_path.parent)
            self.settings_path.write_text(json.dumps(self.settings.to_dict(), indent=2), encoding="utf-8")
            # Remember this location for next launch (in case FV root auto-detection changes).
            write_last_settings_path(self.fv_root_guess, self.settings_path)
            self._log(f"Saved settings: {self.settings_path}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Save failed", str(e))


    # -----------------------------
    # Preset Manager (Ace-Step 1.5)
    # -----------------------------
    def _ace15_preset_mgr_path(self) -> Path:
        fv = Path((self.settings.framevision_root or "").strip()) if (self.settings.framevision_root or "").strip() else getattr(self, "fv_root_guess", guess_framevision_root())
        return preset_manager_path_for_root(fv)

    def _ace15_preset_mgr_load(self, path: Path) -> dict:
        try:
            ensure_dir(path.parent)
            if not path.exists():
                data = _ace15_default_preset_manager_data()
                path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                return data
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            if not isinstance(data, dict):
                raise ValueError("presetmanager.json must be a JSON object")
            data.setdefault("version", 1)
            data.setdefault("genres", {})
            if not isinstance(data.get("genres"), dict):
                data["genres"] = {}
            return data
        except Exception:
            # If anything goes wrong, fall back to defaults.
            data = _ace15_default_preset_manager_data()
            try:
                ensure_dir(path.parent)
                path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            return data

    def _ace15_preset_mgr_save(self, path: Path, data: dict) -> None:
        ensure_dir(path.parent)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    
    def _ace15_current_preset_payload(self) -> dict:
        """Capture current UI settings as a preset payload (Genre Preset Manager)."""
        # Pull fresh UI state
        try:
            self._pull_ui_to_settings()
        except Exception:
            pass

        caption = self.ed_caption.toPlainText().strip()
        neg_prompt = self.ed_negatives.toPlainText().strip() if hasattr(self, "ed_negatives") else ""
        instrumental = bool(self.chk_instrumental.isChecked())

        # Core musical controls
        bpm_val = int(self.spin_bpm.value()) if hasattr(self, "spin_bpm") else 0
        ts_val = int(self.cmb_timesig.currentData() or 0) if hasattr(self, "cmb_timesig") else 0

        ks_val = ""
        if hasattr(self, "cmb_keyscale"):
            ks_val = str(self.cmb_keyscale.currentData() or "").strip() or self.cmb_keyscale.currentText().strip()

        backend = self.cmb_backend.currentText().strip() if hasattr(self, "cmb_backend") else ""

        # Vocal language
        vocal_lang = ""
        try:
            if hasattr(self, "cmb_vocal_language"):
                vocal_lang = str(self.cmb_vocal_language.currentData() or "").strip()
                if not vocal_lang and self.cmb_vocal_language.isEditable():
                    vocal_lang = self.cmb_vocal_language.currentText().strip()
                if vocal_lang.lower() == "auto":
                    vocal_lang = ""
        except Exception:
            vocal_lang = ""

        # Advanced DiT parameter
        shift_val = 3.0
        try:
            shift_val = float(self.spin_shift.value()) if hasattr(self, "spin_shift") else 3.0
        except Exception:
            shift_val = 3.0

        # LM sampling controls
        lm_temp = 0.85
        lm_top_p = 0.95
        lm_top_k = 0
        try:
            lm_temp = float(self.spin_lm_temp.value()) if hasattr(self, "spin_lm_temp") else 0.85
        except Exception:
            lm_temp = 0.85
        try:
            lm_top_p = float(self.spin_lm_top_p.value()) if hasattr(self, "spin_lm_top_p") else 0.95
        except Exception:
            lm_top_p = 0.95
        try:
            lm_top_k = int(self.spin_lm_top_k.value()) if hasattr(self, "spin_lm_top_k") else 0
        except Exception:
            lm_top_k = 0
        enable_lm = bool(self.chk_thinking.isChecked()) if hasattr(self, "chk_thinking") else False
        thinking = bool(self.chk_thinking_mode.isChecked()) if hasattr(self, "chk_thinking_mode") else False
        parallel_thinking = bool(self.chk_parallel_thinking.isChecked()) if hasattr(self, "chk_parallel_thinking") else False
        enhance = bool(self.chk_lm_enhance.isChecked()) if hasattr(self, "chk_lm_enhance") else False

        # Optional generation controls
        gs = 0.0
        try:
            gs = float(self.spin_guidance.value()) if hasattr(self, "spin_guidance") else 0.0
        except Exception:
            gs = 0.0

        steps = 0
        try:
            steps = int(self.spin_steps.value()) if hasattr(self, "spin_steps") else 0
        except Exception:
            steps = 0

        im = ""
        try:
            if hasattr(self, "cmb_infer_method"):
                im = str(self.cmb_infer_method.currentData() or "").strip()
                if not im and self.cmb_infer_method.isEditable():
                    im = self.cmb_infer_method.currentText().strip()
        except Exception:
            im = ""
        im = _ace15_normalize_infer_method(im)

        # Model selections
        main_sel = ""
        lm_sel = ""
        try:
            if hasattr(self, "cmb_main_model"):
                main_sel = str(self.cmb_main_model.currentData() or "").strip()
        except Exception:
            main_sel = ""
        try:
            if hasattr(self, "cmb_lm_model"):
                lm_sel = str(self.cmb_lm_model.currentData() or "").strip()
        except Exception:
            lm_sel = ""

        # Build a compact, UI-focused payload. We intentionally do NOT store duration/seed/batch/audio_format/etc.
        payload: dict = {
            # Required (what the preset manager should save)
            "caption": caption,
            "negatives": neg_prompt,
            "bpm": bpm_val,
            "instrumental": instrumental,
            "thinking": thinking,           # Ace CLI name
            "parallel_thinking": parallel_thinking,
            "enable_lm": enable_lm,         # UI-friendly flag
            "lm_enhance_prompt": enhance,   # UI-friendly flag
            "backend": backend,
            "shift": shift_val,
            "lm_temperature": lm_temp,
            "lm_top_p": lm_top_p,
            "lm_top_k": lm_top_k,
            "time_sig": (ts_val if ts_val > 0 else "auto"),
            "key_scale": (ks_val if ks_val else "auto"),
            "vocal_language": (vocal_lang if vocal_lang else "auto"),
            "guidance": gs,
            "infer_method": (im if im else "auto"),
            "steps": steps,
            "main_model": main_sel,
            "lm_model": lm_sel,
        }

        # Back-compat / CLI-friendly aliases (kept small and harmless)
        payload["lm_negative_prompt"] = neg_prompt
        payload["timesignature"] = str(ts_val) if ts_val > 0 else "auto"
        payload["keyscale"] = ks_val if ks_val else "auto"
        payload["vocal_language"] = vocal_lang if vocal_lang else ""

        if gs and gs > 0.0:
            payload["guidance_scale"] = gs
        if steps and steps > 0:
            payload["inference_steps"] = steps
        if im and im.lower() != "auto":
            payload["infer_method"] = im

        # LM enhance flags map to the Ace COT toggles
        if enable_lm and enhance:
            payload["use_cot_caption"] = True
            payload["use_cot_language"] = True
            payload["use_cot_metas"] = True
        else:
            payload["use_cot_caption"] = False
            payload["use_cot_language"] = False
            payload["use_cot_metas"] = False

        if main_sel:
            payload["main_model_path"] = main_sel
            # Some ACE-Step builds use "main_model" instead of "main_model_path"
            payload["main_model"] = main_sel
        if lm_sel:
            payload["lm_model_path"] = lm_sel
            # Some ACE-Step builds use "lm_model" instead of "lm_model_path"
            payload["lm_model"] = lm_sel

        return payload

    def _ace15_apply_preset_payload(self, preset: dict) -> None:
        """Apply a preset payload back into the UI."""
        if not isinstance(preset, dict):
            return

        # Basic toggles / combos
        try:
            backend = str(preset.get("backend") or "").strip()
            if backend:
                idx = self.cmb_backend.findText(backend, QtCore.Qt.MatchFixedString)
                if idx >= 0:
                    self.cmb_backend.setCurrentIndex(idx)
        except Exception:
            pass

        # Vocal language
        try:
            if hasattr(self, "cmb_vocal_language"):
                vl = preset.get("vocal_language")
                if isinstance(vl, str):
                    vl = vl.strip()
                else:
                    vl = ""
                if vl.lower() == "auto":
                    vl = ""
                if not vl:
                    i = self.cmb_vocal_language.findData("")
                    if i >= 0:
                        self.cmb_vocal_language.setCurrentIndex(i)
                else:
                    i = self.cmb_vocal_language.findData(vl)
                    if i >= 0:
                        self.cmb_vocal_language.setCurrentIndex(i)
                    else:
                        self.cmb_vocal_language.setEditText(vl)
        except Exception:
            pass

        try:
            if "shift" in preset and hasattr(self, 'spin_shift'):
                self.spin_shift.setValue(float(preset.get("shift") or 3.0))
        except Exception:
            pass

        try:
            if "lm_temperature" in preset and hasattr(self, "spin_lm_temp"):
                self.spin_lm_temp.setValue(float(preset.get("lm_temperature") or 0.85))
        except Exception:
            pass
        try:
            if "lm_top_p" in preset and hasattr(self, "spin_lm_top_p"):
                self.spin_lm_top_p.setValue(float(preset.get("lm_top_p") or 0.95))
        except Exception:
            pass
        try:
            if "lm_top_k" in preset and hasattr(self, "spin_lm_top_k"):
                self.spin_lm_top_k.setValue(int(preset.get("lm_top_k") or 0))
        except Exception:
            pass


        try:
            fmt = str(preset.get("audio_format") or "").strip()
            if fmt:
                idx = self.cmb_format.findText(fmt, QtCore.Qt.MatchFixedString)
                if idx >= 0:
                    self.cmb_format.setCurrentIndex(idx)
        except Exception:
            pass

        try:
            task = str(preset.get("task_type") or "").strip()
            if task:
                idx = self.cmb_task.findText(task, QtCore.Qt.MatchFixedString)
                if idx >= 0:
                    self.cmb_task.setCurrentIndex(idx)
        except Exception:
            pass

        # Text fields
        try:
            cap = preset.get("caption")
            if isinstance(cap, str):
                self.ed_caption.setPlainText(cap)
        except Exception:
            pass
        try:
            neg = preset.get("negatives")
            if not isinstance(neg, str):
                neg = preset.get("lm_negative_prompt")
            if isinstance(neg, str) and hasattr(self, 'ed_negatives'):
                self.ed_negatives.setPlainText(neg)
        except Exception:
            pass
        try:
            lyr = preset.get("lyrics")
            if isinstance(lyr, str):
                if lyr.strip() == "[Instrumental]":
                    self.ed_lyrics.setPlainText("")
                else:
                    self.ed_lyrics.setPlainText(lyr)
        except Exception:
            pass

        # Numeric
        try:
            if "duration" in preset:
                self.spin_duration.setValue(float(preset.get("duration") or 0.0))
        except Exception:
            pass
        try:
            if "batch_size" in preset:
                self.spin_batch.setValue(int(preset.get("batch_size") or 1))
        except Exception:
            pass
        try:
            if "seed" in preset:
                sval = int(preset.get("seed") or 0)
                # Legacy preset: seed=-1 meant random.
                use_random = bool(preset.get("seed_random")) or (sval < 0)
                if hasattr(self, 'chk_seed_random'):
                    self.chk_seed_random.setChecked(use_random)
                    self._on_seed_random_toggled(use_random)
                if sval < 0:
                    sval = 0
                self.spin_seed.setValue(int(sval))
        except Exception:
            pass
        try:
            if "bpm" in preset and hasattr(self, 'spin_bpm'):
                self.spin_bpm.setValue(int(preset.get("bpm") or 0))
        except Exception:
            pass
        try:
            ts = preset.get("time_sig")
            if ts is None:
                ts = preset.get("timesignature")
            ts = str(ts or "").strip()
            if ts and ts.lower() not in {"auto", "0"} and hasattr(self, 'cmb_timesig'):
                # cmb_timesig stores int in userData
                for i in range(self.cmb_timesig.count()):
                    if str(self.cmb_timesig.itemData(i) or "") == ts:
                        self.cmb_timesig.setCurrentIndex(i)
                        break
        except Exception:
            pass
        try:
            ks = preset.get("key_scale")
            if ks is None:
                ks = preset.get("keyscale")
            ks = str(ks or "").strip()
            if ks and hasattr(self, 'cmb_keyscale'):
                # data matches keyscale values, fallback to text
                found = False
                for i in range(self.cmb_keyscale.count()):
                    if str(self.cmb_keyscale.itemData(i) or "").strip().lower() == ks.lower() or self.cmb_keyscale.itemText(i).strip().lower() == ks.lower():
                        self.cmb_keyscale.setCurrentIndex(i)
                        found = True
                        break
                if not found and self.cmb_keyscale.isEditable():
                    self.cmb_keyscale.setCurrentText(ks)
        except Exception:
            pass

        # LM flags
        try:
            enable_lm_val = preset.get("enable_lm")
            if enable_lm_val is None:
                # Back-compat: older presets used 'thinking' to mean LM enable
                enable_lm_val = preset.get("thinking")
            self.chk_thinking.setChecked(bool(enable_lm_val))
        except Exception:
            pass
        try:
            thinking_val = preset.get("thinking")
            if thinking_val is None:
                thinking_val = preset.get("enable_lm")
            if hasattr(self, 'chk_thinking_mode'):
                self.chk_thinking_mode.setChecked(bool(thinking_val))
        except Exception:
            pass

        # Parallel thinking
        try:
            pt_val = preset.get("parallel_thinking")
            if pt_val is None:
                pt_val = preset.get("parallelthinking")
            if hasattr(self, 'chk_parallel_thinking'):
                self.chk_parallel_thinking.setChecked(bool(pt_val))
        except Exception:
            pass
        try:
            enhance_val = preset.get("lm_enhance_prompt")
            if enhance_val is None:
                enhance_val = preset.get("use_cot_caption")
            if hasattr(self, "chk_lm_enhance"):
                self.chk_lm_enhance.setChecked(bool(enhance_val))
        except Exception:
            pass

        # Instrumental
        try:
            self.chk_instrumental.setChecked(bool(preset.get("instrumental")))
        except Exception:
            pass

        # Optional generation controls
        try:
            if hasattr(self, 'spin_guidance'):
                if "guidance" in preset:
                    self.spin_guidance.setValue(float(preset.get("guidance") or 0.0))
                elif "guidance_scale" in preset:
                    self.spin_guidance.setValue(float(preset.get("guidance_scale") or 0.0))
        except Exception:
            pass
        try:
            if hasattr(self, 'spin_steps'):
                if "steps" in preset:
                    self.spin_steps.setValue(int(preset.get("steps") or 0))
                elif "inference_steps" in preset:
                    self.spin_steps.setValue(int(preset.get("inference_steps") or 0))
        except Exception:
            pass
        try:
            if hasattr(self, 'cmb_infer_method') and "infer_method" in preset:
                im = _ace15_normalize_infer_method(str(preset.get("infer_method") or "").strip())
                if not im:
                    self.cmb_infer_method.setCurrentIndex(0)
                else:
                    idx = self.cmb_infer_method.findData(im)
                    if idx >= 0:
                        self.cmb_infer_method.setCurrentIndex(idx)
                    else:
                        # editable fallback
                        if self.cmb_infer_method.isEditable():
                            self.cmb_infer_method.setCurrentText(im)
                        else:
                            self.cmb_infer_method.setCurrentIndex(0)
        except Exception:
            pass

        # Models
        try:
            main_sel = preset.get("main_model")
            if not main_sel:
                main_sel = preset.get("main_model_path")
            main_sel = str(main_sel or "").strip()
            if main_sel and hasattr(self, "cmb_main_model"):
                idx = self.cmb_main_model.findData(main_sel)
                if idx >= 0:
                    self.cmb_main_model.setCurrentIndex(idx)
        except Exception:
            pass
        try:
            lm_sel = preset.get("lm_model")
            if not lm_sel:
                lm_sel = preset.get("lm_model_path")
            lm_sel = str(lm_sel or "").strip()
            if lm_sel and hasattr(self, "cmb_lm_model"):
                idx = self.cmb_lm_model.findData(lm_sel)
                if idx >= 0:
                    self.cmb_lm_model.setCurrentIndex(idx)
        except Exception:
            pass

        # Optional: persist after apply
        try:
            self._save_settings()
        except Exception:
            pass

    def _open_preset_manager(self):
        try:
            p = self._ace15_preset_mgr_path()
            ensure_dir(p.parent)
            dlg = PresetManagerDialog(self, p)
            dlg.exec()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Preset Manager", str(e))

    def _pick_dir(self, title: str, start: str) -> Optional[str]:
        p = QtWidgets.QFileDialog.getExistingDirectory(self, title, start)
        return p if p else None

    def _pick_file(self, title: str, start: str, flt: str) -> Optional[str]:
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, title, start, flt)
        return p if p else None

    def _pick_fv_root(self):
        start = self.ed_fvroot.text().strip() or str(Path.cwd())
        p = self._pick_dir("Select FrameVision root", start)
        if p:
            self.ed_fvroot.setText(p)
            fv = Path(p)
            envpy = default_env_python(fv)
            if envpy.exists():
                self.ed_envpy.setText(str(envpy))
            out = fv / "output" / "audio" / "ace15"
            self.ed_outdir.setText(str(out))
            self.ed_projectroot.setText(str(fv))

    def _pick_env_python(self):
        start = str(Path(self.ed_envpy.text().strip() or Path.cwd()).parent)
        p = self._pick_file("Select env python", start, "Python (python.exe;python);;All Files (*)")
        if p:
            self.ed_envpy.setText(p)

    def _pick_cli_py(self):
        start = str(Path(self.ed_clipypy.text().strip() or Path.cwd()).parent)
        p = self._pick_file("Select cli.py", start, "Python Files (*.py);;All Files (*)")
        if p:
            self.ed_clipypy.setText(p)
            self.ed_projectroot.setText(str(Path(p).resolve().parent))
            if hasattr(self, 'cmb_main_model'):
                self._refresh_main_models()
            if hasattr(self, 'cmb_lm_model'):
                self._refresh_lm_models()

    def _pick_project_root(self):
        start = self.ed_projectroot.text().strip() or str(Path.cwd())
        p = self._pick_dir("Select project root (ACE-Step folder)", start)
        if p:
            self.ed_projectroot.setText(p)
            if hasattr(self, 'cmb_main_model'):
                self._refresh_main_models()
            if hasattr(self, 'cmb_lm_model'):
                self._refresh_lm_models()

    def _pick_output_dir(self):
        start = self.ed_outdir.text().strip() or str(Path.cwd())
        p = self._pick_dir("Select output folder", start)
        if p:
            self.ed_outdir.setText(p)

    def _pick_src_audio(self):
        start = str(Path(self.ed_src_audio.text().strip() or Path.cwd()).parent)
        p = self._pick_file("Select source audio", start, "Audio Files (*.wav *.mp3 *.flac *.m4a *.ogg);;All Files (*)")
        if p:
            self.ed_src_audio.setText(p)





    def _generate_lyrics_clicked(self):
        """Generate quick placeholder lyrics for testing (no LM call).

        Goal: a more "real song"-shaped placeholder (verse / pre / hook / bridge),
        still instant, and lightly influenced by the current caption/BPM.
        """
        if self.chk_instrumental.isChecked():
            self.chk_instrumental.setChecked(False)

        import time

        rnd = random.Random(time.time_ns() & 0xFFFFFFFF)

        # Pull a couple of safe keywords from the caption, if present.
        raw_words = [w.strip(".,!?;:()[]{}\"'").lower() for w in self.ed_caption.toPlainText().split()]
        words = [w for w in raw_words if len(w) >= 4 and w.isascii()]
        # Prefer more lyrical words over generic production terms.
        blacklist = {
            "bpm", "kick", "snare", "hats", "hi-hats", "hihat", "bass", "sub",
            "mix", "stereo", "reverb", "delay", "master", "eq", "compressor",
            "house", "techno", "trance", "dubstep", "drums", "synth", "pads",
            "vocal", "vocals", "female", "male", "chops", "loop", "loops",
        }
        words = [w for w in words if w not in blacklist]

        kw1 = rnd.choice(words) if words else "tonight"
        kw2 = kw1
        if len(words) >= 2:
            for _ in range(6):
                cand = rnd.choice(words)
                if cand != kw1:
                    kw2 = cand
                    break
        else:
            kw2 = rnd.choice(["neon", "heartbeat", "river", "echo", "firelight", "midnight"]) if kw1 == "tonight" else "tonight"

        bpm = 0
        try:
            bpm = int(round(float(self.spin_bpm.value())))
        except Exception:
            bpm = 0
        ts = ""
        try:
            ts_v = int(self.cmb_timesig.currentData() or 0)
            ts = f"{ts_v}/4" if ts_v else ""
        except Exception:
            ts = ""

        # Tiny mood heuristic from caption (kept intentionally simple).
        caption_l = (self.ed_caption.toPlainText() or "").lower()
        if any(x in caption_l for x in ["dark", "midnight", "late-night", "underground", "smoke", "shadow"]):
            palette = ["streetlights", "low tide", "cold air", "afterhours", "slow burn"]
        elif any(x in caption_l for x in ["uplift", "euphor", "bright", "sun", "summer", "anthem"]):
            palette = ["sunrise", "open sky", "golden hours", "wide open", "runaway"]
        else:
            palette = ["neon", "sparks", "city rain", "pulse", "gravity"]

        scene = rnd.choice(palette)
        adlibs = ["(oh)", "(yeah)", "(hold up)", "(come on)", "(let it go)", "(mm-hm)"]
        ad1, ad2 = rnd.choice(adlibs), rnd.choice(adlibs)

        # Keep the hook short + repeatable for music generation.
        hook_lines = rnd.choice([
            [f"{kw1} {kw1}, keep it close", f"{kw2} {kw2}, let it go"],
            [f"{kw1} on my tongue {ad1}", f"{kw2} in my lungs {ad2}"],
            [f"Say {kw1}, say it twice", f"Say {kw2}, roll the dice"],
        ])

        tempo_tag = ""
        if bpm >= 60:
            tempo_tag = f"[{bpm} BPM" + (f", {ts}" if ts else "") + "]"
        elif ts:
            tempo_tag = f"[{ts}]"

        txt = (
            f"{tempo_tag}\n".lstrip() +
            "[Verse 1]\n"
            f"{kw1} in the {scene}, I feel it start\n"
            f"Hands on the wheel, {kw2} in my heart\n"
            "Foot on the line, we lean into the sound\n"
            "Count to four, let the floor shake the ground\n\n"
            "[Pre-Chorus]\n"
            f"If we fall, we fall upward {ad1}\n"
            "Breathing in, breathing out, keep it moving\n"
            f"No more ghosts, only {kw1} {ad2}\n"
            "When it drops, you know what we're doing\n\n"
            "[Chorus]\n"
            f"{hook_lines[0]}\n"
            f"{hook_lines[1]}\n"
            "All night long, we don't lose the spark\n"
            f"Meet me where the {kw1} leaves a mark\n\n"
            "[Verse 2]\n"
            f"{kw2} in the mirror, I like that look\n"
            f"{kw1} in the rhythm, got me by the hook\n"
            "We chase the skyline, faster than doubt\n"
            "Turn it up, turn it up, drown it out\n\n"
            "[Bridge]\n"
            "Hold that note, let it hover in the air\n"
            "One step back, then we go everywhere\n"
            "If you need a sign, it's written in the bass\n"
            "Close your eyes, feel the room change its face\n\n"
            "[Final Chorus]\n"
            f"{hook_lines[0]}\n"
            f"{hook_lines[1]}\n"
            "All night long, we don't lose the spark\n"
            f"Meet me where the {kw1} leaves a mark\n\n"
            "[Outro]\n"
            f"{kw1}... {kw2}... fade it slow\n"
        )

        self.ed_lyrics.setPlainText(txt.strip() + "\n")


    def _clear_lyrics_clicked(self):
        """Clear the lyrics box."""
        # If Instrumental is enabled, the UI may force a placeholder lyric.
        # Turn it off so the box can actually be empty.
        try:
            if self.chk_instrumental.isChecked():
                self.chk_instrumental.setChecked(False)
        except Exception:
            pass
        try:
            self.ed_lyrics.clear()
        except Exception:
            try:
                self.ed_lyrics.setPlainText("")
            except Exception:
                pass


    def _refresh_main_models(self):
        """Populate the main model dropdown (Base/Turbo) based on what’s available (and what can be downloaded)."""
        try:
            proj = Path(self.ed_projectroot.text().strip())
            models = discover_main_models(proj) if proj.exists() else [
                "acestep-v15-base",
                "acestep-v15-sft",
                "acestep-v15-turbo",
                "acestep-v15-turbo-rl",
            ]
        except Exception:
            models = [
                "acestep-v15-base",
                "acestep-v15-sft",
                "acestep-v15-turbo",
                "acestep-v15-turbo-rl",
            ]

        current = self.cmb_main_model.currentText().strip() if hasattr(self, "cmb_main_model") else ""
        self.cmb_main_model.blockSignals(True)
        self.cmb_main_model.clear()
        self.cmb_main_model.addItem("auto (let ACE decide)", "")
        for m in models:
            self.cmb_main_model.addItem(m, m)
        if current:
            i = self.cmb_main_model.findData(current)
            if i >= 0:
                self.cmb_main_model.setCurrentIndex(i)
        self.cmb_main_model.blockSignals(False)

        try:
            self._update_shift_ui()
        except Exception:
            pass

    
    def _refresh_lm_models(self):
        """Populate the LM model dropdown based on what’s available (and what can be downloaded)."""
        try:
            proj = Path(self.ed_projectroot.text().strip())
            models = discover_lm_models(proj) if proj.exists() else [
                "acestep-5Hz-lm-0.6B",
                "acestep-5Hz-lm-1.7B",
                "acestep-5Hz-lm-4B",
            ]
        except Exception:
            models = [
                "acestep-5Hz-lm-0.6B",
                "acestep-5Hz-lm-1.7B",
                "acestep-5Hz-lm-4B",
            ]

        # Hide the 3B LM option (it was a mistaken variant).
        models = [m for m in models if m != "acestep-5Hz-lm-3B"]

        current = self.cmb_lm_model.currentText().strip() if hasattr(self, "cmb_lm_model") else ""
        self.cmb_lm_model.blockSignals(True)
        self.cmb_lm_model.clear()
        self.cmb_lm_model.addItem("auto (let ACE decide)", "")
        for m in models:
            self.cmb_lm_model.addItem(m, m)
        # restore selection if possible
        if current:
            i = self.cmb_lm_model.findData(current)
            if i >= 0:
                self.cmb_lm_model.setCurrentIndex(i)
        self.cmb_lm_model.blockSignals(False)

    def _validate(self) -> Optional[str]:
        envpy = Path(self.ed_envpy.text().strip())
        clipy = Path(self.ed_clipypy.text().strip())
        proj = Path(self.ed_projectroot.text().strip())
        out = Path(self.ed_outdir.text().strip())

        if not envpy.exists():
            return "Env python not found. Point it to environments/.ace_15/Scripts/python.exe"
        if not clipy.exists():
            return "cli.py not found. Browse to your ACE-Step cli.py"
        if not proj.exists():
            return "Project root not found."
        try:
            ensure_dir(out)
        except Exception:
            return "Cannot create output folder."

        task = self.cmb_task.currentText().strip()

        # Some task types are known to require the Base model.
        if hasattr(self, 'cmb_main_model'):
            main_sel = str(self.cmb_main_model.currentData() or "").strip()
            if main_sel and "turbo" in main_sel.lower() and task in {"lego", "extract"}:
                return f"Task '{task}' requires the Base model. Switch Main model to 'acestep-v15-base' (or auto)."

        caption = self.ed_caption.toPlainText().strip()
        lyrics = self.ed_lyrics.toPlainText().strip()

        if task == "text2music":
            if not caption and not lyrics:
                return "For text2music: provide a caption or lyrics."
        else:
            if task in {"cover", "repaint", "lego", "complete"} and not caption:
                return f"For task '{task}': caption is required."
            if task in {"cover", "repaint", "lego", "extract", "complete"}:
                src = self.ed_src_audio.text().strip()
                if not src or not Path(src).exists():
                    return f"For task '{task}': source audio is required."

        # Don't hard-block auto-lyrics if "thinking" is off.
        # ACE can still run the LM path and will fall back to the PyTorch backend
        # if nano-vllm isn't installed.

        return None

    # -----------------------------
    # Seed handling
    # -----------------------------
    def _ace15_generate_seed(self) -> int:
        """Generate a random seed within the UI spinbox range."""
        try:
            # Keep seeds in a compact, human-friendly range.
            # Requested: 0 .. 1,000,000
            return int(random.randint(0, 1_000_000))
        except Exception:
            return int(time.time()) & 0x7FFFFFFF

    def _on_seed_random_toggled(self, on: bool):
        """UI handler: enable/disable manual seed entry."""
        try:
            self.spin_seed.setEnabled(not bool(on))
        except Exception:
            pass
        if on:
            # Immediately show a valid random seed (so the user never sees "-1").
            try:
                self.spin_seed.setValue(self._ace15_generate_seed())
            except Exception:
                pass

    def _ace15_prepare_seed_for_run(self):
        """If Random is enabled, generate a fresh seed and show it in the UI."""
        try:
            if hasattr(self, 'chk_seed_random') and self.chk_seed_random.isChecked():
                self.spin_seed.setValue(self._ace15_generate_seed())
        except Exception:
            pass

    def _make_config(self, out_dir: Path) -> Path:
        ts = time.strftime("%Y%m%d_%H%M%S")
        cfg_path = out_dir / f"ace_step_run_{ts}.toml"

        task = self.cmb_task.currentText().strip()
        caption = self.ed_caption.toPlainText().strip()
        neg_prompt = self.ed_negatives.toPlainText().strip() if hasattr(self, 'ed_negatives') else ""
        lyrics_text = self.ed_lyrics.toPlainText().strip()

        instrumental = self.chk_instrumental.isChecked()

        # Main model selection (for shift support)
        main_sel = ""
        try:
            if hasattr(self, 'cmb_main_model'):
                main_sel = str(self.cmb_main_model.currentData() or "").strip()
        except Exception:
            main_sel = ""

        config = {
            "project_root": str(Path(self.ed_projectroot.text().strip()).resolve()),
            "backend": self.cmb_backend.currentText().strip(),
            "log_level": self.cmb_loglevel.currentText().strip(),
            "device": "auto",

            # Advanced DiT parameter (ACE-Step "Shift")
            "shift": (float(self.spin_shift.value()) if hasattr(self, 'spin_shift') else 3.0) if self._ace15_shift_supported(main_sel) else None,

            "use_flash_attention": True if self.chk_flashattn.isChecked() else None,
            "offload_to_cpu": bool(self.chk_offload.isChecked()),
            "offload_dit_to_cpu": bool(self.chk_offload_dit.isChecked()),

            "save_dir": str(out_dir.resolve()),
            "audio_format": self.cmb_format.currentText().strip(),

            "task_type": task,
            "caption": caption,
            "duration": float(self.spin_duration.value()),
            "batch_size": int(self.spin_batch.value()),

            # We always pass an explicit seed value.
            # If the UI Random toggle is enabled, we generate a new seed per run
            # (see _ace15_prepare_seed_for_run) and show it in the seed box.
            "seed": int(self.spin_seed.value()),
        }

        # Vocal language (ISO 639-1). Empty/auto lets ACE decide.
        try:
            if hasattr(self, 'cmb_vocal_language'):
                vl = str(self.cmb_vocal_language.currentData() or "").strip()
                if not vl and self.cmb_vocal_language.isEditable():
                    vl = self.cmb_vocal_language.currentText().strip()
                if vl.lower() == "auto":
                    vl = ""
                if vl:
                    config["vocal_language"] = vl
        except Exception:
            pass

        # Optional generation controls
        try:
            gs = float(self.spin_guidance.value()) if hasattr(self, 'spin_guidance') else 0.0
            if gs > 0.0:
                config["guidance_scale"] = gs
        except Exception:
            pass

        try:
            steps = int(self.spin_steps.value()) if hasattr(self, 'spin_steps') else 0
            if steps > 0:
                config["inference_steps"] = steps
        except Exception:
            pass

        try:
            im = ""
            if hasattr(self, 'cmb_infer_method'):
                im = str(self.cmb_infer_method.currentData() or "").strip()
                if not im and self.cmb_infer_method.isEditable():
                    im = self.cmb_infer_method.currentText().strip()
            im = _ace15_normalize_infer_method(im)
            if im:
                config["infer_method"] = im
        except Exception:
            pass


        # User-selected meta overrides (0/auto = let ACE decide)
        bpm_val = int(self.spin_bpm.value()) if hasattr(self, 'spin_bpm') else 0
        ts_val = int(self.cmb_timesig.currentData() or 0) if hasattr(self, 'cmb_timesig') else 0
        ks_val = ""
        if hasattr(self, 'cmb_keyscale'):
            ks_val = str(self.cmb_keyscale.currentData() or "").strip() or self.cmb_keyscale.currentText().strip()
        if bpm_val > 0:
            config["bpm"] = bpm_val
        if ts_val > 0:
            config["timesignature"] = str(ts_val)
        if ks_val and ks_val.lower() != "auto":
            config["keyscale"] = ks_val

        enable_lm = bool(self.chk_thinking.isChecked())
        thinking = bool(self.chk_thinking_mode.isChecked()) if hasattr(self, 'chk_thinking_mode') else False
        config["thinking"] = (thinking if enable_lm else False)

        # Gradio feature: parallel thinking (safe extra key; ignored by older ACE builds)
        try:
            pt = bool(self.chk_parallel_thinking.isChecked()) if hasattr(self, 'chk_parallel_thinking') else False
            if enable_lm and pt:
                config["parallel_thinking"] = True
        except Exception:
            pass

        # Negative prompt for LM guidance (optional)
        if neg_prompt:
            config["lm_negative_prompt"] = neg_prompt

        # If LM is enabled, choose whether it is allowed to rewrite/expand the prompt.
        # Many users want LM only for auto-lyrics while keeping the caption exactly as typed.
        if enable_lm:
            enhance = bool(self.chk_lm_enhance.isChecked()) if hasattr(self, "chk_lm_enhance") else False
            config["use_cot_caption"] = True if enhance else False
            config["use_cot_language"] = True if enhance else False
            config["use_cot_metas"] = True if enhance else False
            # LM sampling controls
            try:
                config["lm_temperature"] = float(self.spin_lm_temp.value()) if hasattr(self, "spin_lm_temp") else 0.85
            except Exception:
                config["lm_temperature"] = 0.85
            try:
                config["lm_top_p"] = float(self.spin_lm_top_p.value()) if hasattr(self, "spin_lm_top_p") else 0.95
            except Exception:
                config["lm_top_p"] = 0.95
            try:
                config["lm_top_k"] = int(self.spin_lm_top_k.value()) if hasattr(self, "spin_lm_top_k") else 0
            except Exception:
                config["lm_top_k"] = 0

        # Optional: force a specific main model (Base/Turbo). ACE will download on first use.
        if hasattr(self, "cmb_main_model"):
            main_sel = str(self.cmb_main_model.currentData() or "")
            if main_sel:
                config["main_model_path"] = main_sel
                # Some ACE-Step builds primarily route DiT selection through `config_path`.
                # If this is missing, ACE will "Auto-select" and often defaults to turbo.
                # Setting it explicitly ensures SFT (and other zoo models) are honored.
                config["config_path"] = main_sel
                # Compatibility: some ACE-Step versions read `dit_model`/`main_model` instead of `*_path`.
                config["main_model"] = main_sel
                config["dit_model"] = main_sel

        # Optional: force a specific LM model (ACE will download on first use and show progress in logs)
        if hasattr(self, "cmb_lm_model"):
            lm_sel = str(self.cmb_lm_model.currentData() or "")
            if lm_sel:
                config["lm_model_path"] = lm_sel
                # Compatibility: some ACE-Step versions read `lm_model` instead of `lm_model_path`.
                config["lm_model"] = lm_sel

        if task in {"text2music", "cover", "repaint", "lego", "complete"}:
            if instrumental:
                config["instrumental"] = True
                config["lyrics"] = "[Instrumental]"
                config["use_cot_lyrics"] = False
            else:
                config["instrumental"] = False
                config["use_cot_lyrics"] = False
                config["lyrics"] = lyrics_text if lyrics_text else None
        else:
            config["instrumental"] = False
            config["use_cot_lyrics"] = False

        if task in {"cover", "repaint", "lego", "extract", "complete"}:
            config["src_audio"] = self.ed_src_audio.text().strip()
        if task == "repaint":
            config["repainting_start"] = float(self.ed_repaint_start.value())
            config["repainting_end"] = float(self.ed_repaint_end.value())
        if task == "lego":
            config["lego_track"] = self.ed_track.text().strip()
        if task == "extract":
            config["extract_track"] = self.ed_track.text().strip()
        if task == "complete":
            config["complete_tracks"] = self.ed_complete_tracks.text().strip()

        clean = {k: v for k, v in config.items() if v is not None}
        cfg_path.write_text(toml_dumps_flat(clean), encoding="utf-8")
        return cfg_path

    # -----------------------------
    # Queue
    # -----------------------------
    def _queue_save(self) -> None:
        """Persist current queue (and any active job) under presets/setsave."""
        try:
            root = self.fv_root_guess
            p = queue_path_for_root(root)
            ensure_dir(p.parent)

            jobs: list[QueueJob] = []
            if self._active_job is not None:
                # If the app closes mid-run, treat it as pending again on next start.
                jobs.append(self._active_job)
            jobs.extend(list(self._queue or []))

            payload = {
                "version": 1,
                "saved_epoch": time.time(),
                "next_job_id": int(getattr(self, "_next_job_id", 1) or 1),
                "jobs": [j.to_dict() for j in jobs],
            }

            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(p)
        except Exception:
            pass

    def _queue_load(self) -> None:
        """Load persisted queue from presets/setsave (best-effort)."""
        try:
            root = self.fv_root_guess
            p = queue_path_for_root(root)
            if not p.exists():
                return

            data = json.loads(p.read_text(encoding="utf-8"))
            raw = data.get("jobs", [])
            jobs: list[QueueJob] = []
            if isinstance(raw, list):
                for jd in raw:
                    if not isinstance(jd, dict):
                        continue
                    try:
                        j = QueueJob.from_dict(jd)
                        # Only keep jobs that still have enough info to run.
                        if j.use_api:
                            if isinstance(j.api_payload, dict):
                                jobs.append(j)
                        else:
                            if j.cli_args and j.cli_cwd:
                                jobs.append(j)
                    except Exception:
                        continue

            # Loaded jobs are pending; active job is cleared.
            self._active_job = None
            self._queue = jobs

            max_id = max([int(j.job_id) for j in jobs], default=0)
            try:
                nxt = int(data.get("next_job_id", 0) or 0)
            except Exception:
                nxt = 0
            self._next_job_id = max(nxt, max_id + 1, 1)
        except Exception:
            # Corrupt file -> ignore
            self._queue = []
            self._next_job_id = 1

    def _is_running(self) -> bool:
        return self._active_job is not None or self._runner is not None or self._thread is not None

    def _queue_job_title_from_ui(self) -> str:
        try:
            cap = (self.ed_caption.toPlainText() or "").strip()
            if cap:
                first = cap.splitlines()[0].strip()
                if first:
                    return first[:80]
        except Exception:
            pass
        return (self._ace15_pick_subgenre_for_naming() or "Job")[:80]

    def _build_job_from_ui(self) -> Optional[QueueJob]:
        """Snapshot the current UI into a QueueJob.

        This is called both when starting immediately and when enqueueing while a
        run is active.
        """
        err = self._validate()
        if err:
            QtWidgets.QMessageBox.warning(self, "Fix this first", err)
            return None

        # If Random is enabled, generate a fresh seed and show it (instead of -1).
        self._ace15_prepare_seed_for_run()

        # Persist UI settings as usual.
        self._pull_ui_to_settings()
        self._save_settings()

        out_dir = Path(self.ed_outdir.text().strip())
        ensure_dir(out_dir)

        use_api = bool(getattr(self.settings, 'keep_in_vram', False))
        envpy = Path(self.ed_envpy.text().strip())
        proj = Path(self.ed_projectroot.text().strip())
        clipy = Path(self.ed_clipypy.text().strip())

        # Write config now (so queued jobs keep their parameters).
        cfg_path = self._make_config(out_dir)
        self._log(f"Saved config:\n  {cfg_path}")

        # Metadata for display.
        try:
            batch_size = int(self.spin_batch.value())
        except Exception:
            batch_size = 1
        try:
            seed_s = str(int(self.spin_seed.value()))
        except Exception:
            seed_s = "AUTO"
        try:
            task_type = str(self.cmb_task.currentText().strip() or 'text2music')
        except Exception:
            task_type = ""
        try:
            dur = float(self.spin_duration.value())
        except Exception:
            dur = 0.0

        job = QueueJob(
            job_id=self._next_job_id,
            created_epoch=time.time(),
            use_api=use_api,
            out_dir=out_dir,
            cfg_path=cfg_path,
            title=self._queue_job_title_from_ui(),
            batch_size=batch_size,
            seed=seed_s,
            subgenre_for_naming=self._ace15_pick_subgenre_for_naming(),
            task_type=task_type,
            duration_s=dur,
        )
        self._next_job_id += 1

        if use_api:
            caption = self.ed_caption.toPlainText().strip()
            lyrics = self.ed_lyrics.toPlainText().strip()
            if self.chk_instrumental.isChecked():
                lyrics = "[Instrumental]"
            payload = {
                "prompt": caption,
                "lyrics": lyrics,
                "thinking": bool(self.chk_thinking_mode.isChecked()) if hasattr(self, 'chk_thinking_mode') else False,
                "use_format": False,
                "model": str(self.cmb_main_model.currentData() or "").strip() or None,
                "bpm": int(self.spin_bpm.value()) if hasattr(self, 'spin_bpm') else 0,
                "key_scale": str(self.cmb_keyscale.currentData() or ""),
                "time_signature": str(self.cmb_timesig.currentData() or ""),
                "audio_duration": float(self.spin_duration.value()),
                "vocal_language": str(getattr(self.settings, 'vocal_language', '') or 'en') or 'en',
                "inference_steps": int(self.spin_steps.value()) if hasattr(self, 'spin_steps') else 0,
                "guidance_scale": float(self.spin_guidance.value()) if hasattr(self, 'spin_guidance') else 0.0,
                "use_random_seed": bool(getattr(self.settings, 'seed_random', False)),
                "seed": int(self.spin_seed.value()) if hasattr(self, 'spin_seed') else 0,
                "batch_size": int(self.spin_batch.value()),
                "task_type": str(self.cmb_task.currentText().strip() or 'text2music'),
                "infer_method": str(self.cmb_infer_method.currentData() or 'ode') if hasattr(self, 'cmb_infer_method') else 'ode',
                "shift": float(self.spin_shift.value()) if hasattr(self, 'spin_shift') else 3.0,
                "audio_format": str(self.cmb_format.currentText().strip() or 'mp3'),
                "lm_model_path": str(self.cmb_lm_model.currentData() or "").strip() or None,
                "lm_backend": str(self.cmb_backend.currentText().strip() or 'vllm'),
                "lm_temperature": float(getattr(self.settings, 'lm_temperature', 0.85) or 0.85),
                "lm_top_p": float(getattr(self.settings, 'lm_top_p', 0.95) or 0.95),
                "lm_top_k": int(getattr(self.settings, 'lm_top_k', 0) or 0),
                "lm_negative_prompt": str(getattr(self.settings, 'lm_negative_prompt', '') or 'NO USER INPUT') or 'NO USER INPUT',
            }
            if payload.get("bpm") == 0:
                payload["bpm"] = None
            if not (payload.get("key_scale") or "").strip():
                payload["key_scale"] = ""
            if payload.get("time_signature") in {"0", "", None}:
                payload["time_signature"] = ""
            if payload.get("inference_steps") == 0:
                payload["inference_steps"] = 8
            if payload.get("guidance_scale") == 0.0:
                payload["guidance_scale"] = 7.0
            im = _ace15_normalize_infer_method(str(payload.get("infer_method") or ""))
            payload["infer_method"] = im or "ode"

            job.api_payload = payload
        else:
            job.cli_args = [str(envpy), str(clipy), "-c", str(cfg_path)]
            job.cli_cwd = proj

        return job

    def _queue_refresh_ui(self, force: bool = False) -> None:

        # Throttle full table rebuilds; they can interrupt row selection.
        now = time.time()
        last = float(getattr(self, "_queue_last_ui_refresh", 0.0) or 0.0)
        allow_full = force or (now - last) >= 5.0

        # Remember current selection (by job_id) so we can restore it after refresh.
        selected_job_id: Optional[int] = None
        try:
            sel = self.tbl_queue.selectionModel().selectedRows()
            if sel:
                r = int(sel[0].row())
                it = self.tbl_queue.item(r, 0)
                if it is not None:
                    selected_job_id = it.data(QtCore.Qt.UserRole)
        except Exception:
            selected_job_id = None
        try:
            is_running = self._active_job is not None
            qn = len(self._queue)
            if is_running:
                self.lbl_queue_state.setText(f"Running job #{self._active_job.job_id} — queued: {qn}")
            else:
                self.lbl_queue_state.setText(f"Idle — queued: {qn}")
        except Exception:
            pass

        # If we're within the throttle window, don't rebuild the table.
        if not allow_full:
            try:
                self.btn_queue_start_next.setEnabled((not self._is_running()) and len(self._queue) > 0)
                self.btn_queue_clear.setEnabled(len(self._queue) > 0)
            except Exception:
                pass
            return

        try:
            self.tbl_queue.setRowCount(0)
        except Exception:
            return

        rows: list[tuple[str, str, QueueJob]] = []
        if self._active_job is not None:
            rows.append((str(self._active_job.job_id), "Running", self._active_job))
        for j in self._queue:
            rows.append((str(j.job_id), "Queued", j))

        self.tbl_queue.setRowCount(len(rows))
        for r, (jid, status, j) in enumerate(rows):
            def _it(txt: str):
                it = QtWidgets.QTableWidgetItem(txt)
                it.setData(QtCore.Qt.UserRole, j.job_id)
                return it

            self.tbl_queue.setItem(r, 0, _it(jid))
            self.tbl_queue.setItem(r, 1, _it(status))
            self.tbl_queue.setItem(r, 2, _it(str(max(1, int(j.batch_size or 1)))))
            self.tbl_queue.setItem(r, 3, _it(str(j.task_type or "")))
            self.tbl_queue.setItem(r, 4, _it(f"{float(j.duration_s or 0.0):.1f}s" if j.duration_s else ""))
            self.tbl_queue.setItem(r, 5, _it(str(j.seed or "")))
            self.tbl_queue.setItem(r, 6, _it(str(j.title or "")))


        # Mark refresh time
        try:
            self._queue_last_ui_refresh = now
        except Exception:
            pass

        # Restore selection if possible
        if selected_job_id is not None:
            try:
                for r in range(self.tbl_queue.rowCount()):
                    it0 = self.tbl_queue.item(r, 0)
                    if it0 is not None and it0.data(QtCore.Qt.UserRole) == selected_job_id:
                        self.tbl_queue.selectRow(r)
                        break
            except Exception:
                pass

        try:
            self.btn_queue_start_next.setEnabled((not self._is_running()) and len(self._queue) > 0)
            self.btn_queue_clear.setEnabled(len(self._queue) > 0)
        except Exception:
            pass

    def _queue_enqueue(self, job: QueueJob) -> None:
        self._queue.append(job)
        self._queue_save()
        self._queue_refresh_ui(force=True)

    def _queue_clear(self) -> None:
        self._queue.clear()
        self._queue_save()
        self._queue_refresh_ui(force=True)

    def _queue_remove_selected(self) -> None:
        try:
            sel = self.tbl_queue.selectionModel().selectedRows()
            if not sel:
                return
            row = int(sel[0].row())
        except Exception:
            return

        # Row 0 may be the running job.
        if self._active_job is not None and row == 0:
            self._log("Can't remove the running job. Use Stop if you want to cancel it.")
            return

        # Map row -> queue index
        qi = row
        if self._active_job is not None:
            qi -= 1
        if 0 <= qi < len(self._queue):
            j = self._queue.pop(qi)
            self._log(f"Removed job #{j.job_id} from queue")
            self._queue_save()
        self._queue_refresh_ui(force=True)


    def _queue_remove_row(self, row: int) -> None:
        """Remove a queued (pending) job at a specific visible row."""
        # Row 0 may be the running job.
        if self._active_job is not None and row == 0:
            return
        qi = row
        if self._active_job is not None:
            qi -= 1
        if 0 <= qi < len(self._queue):
            j = self._queue.pop(qi)
            self._log(f"Removed job #{j.job_id} from queue")
            self._queue_save()
        self._queue_refresh_ui(force=True)

    def _queue_context_menu(self, pos: QtCore.QPoint) -> None:
        row = self.tbl_queue.rowAt(pos.y())
        if row < 0:
            return

        menu = QtWidgets.QMenu(self.tbl_queue)

        is_running_row = (self._active_job is not None and row == 0)
        if is_running_row:
            act = menu.addAction("Cancel job")
            def _do_cancel():
                if self._active_job is not None:
                    self._log(f"Cancel requested for job #{self._active_job.job_id}")
                self._stop()
            act.triggered.connect(_do_cancel)
        else:
            act = menu.addAction("Remove from queue")
            act.triggered.connect(lambda: self._queue_remove_row(row))

        menu.exec(self.tbl_queue.viewport().mapToGlobal(pos))


    def _queue_start_next(self) -> None:
        if self._is_running():
            return
        self._queue_pump(force=True)

    def _queue_pump(self, force: bool = False) -> None:
        """Start the next queued job if we're idle."""
        if self._is_running() and not force:
            self._queue_refresh_ui(force=True)
            return
        if not self._queue:
            self._queue_refresh_ui(force=True)
            return

        # Peek the next job. If it can't start (e.g. API server not ready), keep it queued.
        job = self._queue[0]
        ok = self._start_job(job)
        if ok:
            self._queue.pop(0)
            self._queue_save()
        self._queue_refresh_ui(force=True)

    def _on_generate_clicked(self) -> None:
        job = self._build_job_from_ui()
        if job is None:
            return

        if self._is_running():
            self._queue_enqueue(job)
            try:
                self.tabs.setCurrentIndex(1)  # Queue tab
            except Exception:
                pass
            self._log(f"Queued job #{job.job_id} (tracks: {job.batch_size}).")
            return

        # Start immediately.
        self._start_job(job)
        self._queue_refresh_ui(force=True)

    def _start_job(self, job: QueueJob) -> bool:
        """Start a job. Returns True if started, False if we should retry later."""

        # Keep in VRAM: ensure server is ready before starting.
        if job.use_api:
            self._ensure_api_server_started()
            if not self._is_api_ready():
                try:
                    self.lbl_status.setText("Starting server…")
                except Exception:
                    pass
                self._log("[Keep in VRAM] Server is still starting. Job will begin when ready.")
                return False

        self._active_job = job

        # Save immediately so if the app closes mid-run we can resume this job.
        try:
            self._queue_save()
        except Exception:
            pass

        # Snapshot outputs just before the run so we can identify what's new.
        try:
            self._ace15_out_snapshot = {str(p.resolve()) for p in list_audio_files(job.out_dir)}
        except Exception:
            self._ace15_out_snapshot = set()
        self._ace15_run_started_epoch = time.time()

        # Remember run context for post-run housekeeping.
        self._last_out_dir = job.out_dir
        self._last_cfg_path = job.cfg_path
        try:
            self._last_proj_root = Path(self.ed_projectroot.text().strip())
        except Exception:
            self._last_proj_root = None

        self._set_busy(True)
        self.lbl_status.setText("Running…")

        self._thread = QtCore.QThread()

        if job.use_api:
            if self._api_server is None or not self._api_server.is_running():
                self._set_busy(False)
                self.lbl_status.setText("Idle")
                QtWidgets.QMessageBox.critical(
                    self,
                    "API server not running",
                    "Keep in VRAM is enabled, but the API server could not be started.\n"
                    "Check the log for details, or disable 'Keep in VRAM (needs restart)'.",
                )
                self._active_job = None
                return False

            if not self._api_server.wait_until_ready(timeout_sec=20.0):
                self._set_busy(False)
                self.lbl_status.setText("Idle")
                QtWidgets.QMessageBox.critical(
                    self,
                    "API server not ready",
                    "The API server process started, but it did not become reachable on the configured port.\n"
                    "Check the log output above for errors, or disable 'Keep in VRAM (needs restart)'.",
                )
                self._active_job = None
                return False

            payload = dict(job.api_payload or {})
            self._runner = ApiRunner(base_url=self._api_server.base_url, payload=payload, output_dir=job.out_dir)
        else:
            if not job.cli_args or not job.cli_cwd:
                self._log("Internal error: queued CLI job is missing args/cwd")
                self._active_job = None
                self._set_busy(False)
                self.lbl_status.setText("Idle")
                return False
            self._runner = Runner(args=job.cli_args, cwd=job.cli_cwd, hide_console=False)

        self._runner.moveToThread(self._thread)
        self._thread.started.connect(self._runner.run)
        self._runner.log.connect(self._log)
        self._runner.finished.connect(self._done)
        self._thread.start()
        return True

    def _start(self):
        """Backward compat: some integrations may still call _start()."""
        self._on_generate_clicked()

    def _stop(self):
        if self._runner:
            self._runner.stop()
        self._log("Stop requested.")

    def _done(self, code: int):
        self._log(f"Finished with exit code {code}")
        self.lbl_status.setText(f"Done (code {code})" if code != 0 else "Done")
        # Keep a reference to the runner for post-processing (we clear self._runner below).
        runner_ref = self._runner

        self._set_busy(False)

        if self._thread:
            self._thread.quit()
            self._thread.wait(2000)
        self._thread = None
        self._runner = None

        # Post-process: rename newly generated outputs to a human-friendly name.
        try:
            if code == 0:
                j = self._active_job
                if j is not None:
                    # If this was an API run, we may have per-output seeds.
                    if j.use_api and isinstance(runner_ref, ApiRunner) and getattr(runner_ref, 'saved_outputs', None):
                        self._ace15_rename_specific_outputs(
                            outputs=list(getattr(runner_ref, 'saved_outputs', []) or []),
                            out_dir=j.out_dir,
                            subgenre=(j.subgenre_for_naming or self._ace15_pick_subgenre_for_naming()),
                        )
                    else:
                        self._ace15_rename_new_outputs(
                            out_dir=j.out_dir,
                            subgenre=(j.subgenre_for_naming or self._ace15_pick_subgenre_for_naming()),
                            seed_s=(j.seed or self._ace15_seed_for_naming()),
                        )
                else:
                    # Fallback (should not normally happen)
                    self._ace15_rename_new_outputs(
                        out_dir=Path(self.ed_outdir.text().strip()),
                        subgenre=self._ace15_pick_subgenre_for_naming(),
                        seed_s=self._ace15_seed_for_naming(),
                    )
        except Exception as e:
            self._log(f"NOTE: Could not rename outputs: {e!r}")

        self._refresh_outputs()

        # Move repo instruction.txt into the output folder so it can be reused later,
        # and to avoid the next run getting "stuck" on the previous draft.
        try:
            self._move_instruction_txt_to_output(exit_code=code)
        except Exception as e:
            self._log(f"NOTE: Could not move instruction.txt: {e!r}")

        if False and code == 0:  # forced OFF (UI removed)
            self._open_output_folder()

        # Clear active job and start next queued one (if any).
        self._active_job = None
        try:
            self._queue_save()
        except Exception:
            pass
        self._queue_refresh_ui()
        # Pump immediately so it feels instant.
        self._queue_pump(force=True)

    # -----------------------------
    # Output naming
    # -----------------------------
    def _ace15_sanitize_filename_part(self, s: str) -> str:
        s = (s or "").strip()
        if not s:
            return ""
        # Windows-safe filename chunk: keep ascii-ish, replace spaces, drop odd chars.
        out = []
        for ch in s:
            if ch.isalnum() or ch in {"-", "_"}:
                out.append(ch)
            elif ch.isspace() or ch in {"/", "\\", ":", "|", "*", "?", "\"", "<", ">"}:
                out.append("_")
            else:
                out.append("_")
        cleaned = "".join(out)
        while "__" in cleaned:
            cleaned = cleaned.replace("__", "_")
        return cleaned.strip("._ ")

    def _ace15_pick_subgenre_for_naming(self) -> str:
        sub = (self._ace15_last_preset_subgenre or "").strip()
        if sub:
            return sub
        # Fallback: try to infer from caption first line if it looks like a tag.
        try:
            cap = self.ed_caption.toPlainText().strip()
            if cap:
                first = cap.splitlines()[0].strip()
                if 2 <= len(first) <= 48 and all(c.isprintable() for c in first):
                    return first
        except Exception:
            pass
        return "Custom"

    def _ace15_seed_for_naming(self) -> str:
        try:
            return str(int(self.spin_seed.value()))
        except Exception:
            return "AUTO"

    def _ace15_rename_specific_outputs(
        self,
        *,
        outputs: list[tuple[Path, Optional[int]]],
        out_dir: Path,
        subgenre: str,
    ) -> None:
        """Rename a known list of output files.

        Used for API mode so each output can carry its *actual* per-output seed
        (the API runner generates one seed per output when Random is enabled).
        """
        if not out_dir.exists() or not outputs:
            return

        subgenre = self._ace15_sanitize_filename_part(subgenre) or "Custom"
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())

        renamed_any = False
        for idx, (src, seed_int) in enumerate(outputs, start=1):
            try:
                src = Path(src)
            except Exception:
                continue
            if not src.exists():
                continue

            ext = src.suffix or ""
            seed_s = self._ace15_sanitize_filename_part(str(seed_int) if seed_int is not None else self._ace15_seed_for_naming()) or "AUTO"
            counter = f"_{idx}" if len(outputs) > 1 else ""
            base = f"{subgenre}__seed{seed_s}__{stamp}{counter}{ext}"
            dst = out_dir / base

            if dst.exists():
                i = 2
                while True:
                    cand = out_dir / f"{dst.stem}_{i}{dst.suffix}"
                    if not cand.exists():
                        dst = cand
                        break
                    i += 1

            try:
                src.rename(dst)
                renamed_any = True
                self._log(f"Renamed output: {src.name} -> {dst.name}")
            except Exception as e:
                self._log(f"NOTE: Could not rename '{src.name}': {e!r}")

        if renamed_any:
            try:
                self._ace15_out_snapshot = {str(p.resolve()) for p in list_audio_files(out_dir)}
            except Exception:
                pass

    def _ace15_rename_new_outputs(self, *, out_dir: Path, subgenre: str, seed_s: str) -> None:
        if not out_dir.exists():
            return

        all_audio = list_audio_files(out_dir)
        new_files: List[Path] = []

        snap = self._ace15_out_snapshot or set()
        if snap:
            for p in all_audio:
                try:
                    if str(p.resolve()) not in snap:
                        new_files.append(p)
                except Exception:
                    pass
        if not new_files:
            started = float(self._ace15_run_started_epoch or 0.0)
            if started > 0:
                for p in all_audio:
                    try:
                        if p.stat().st_mtime >= (started - 2.0):
                            new_files.append(p)
                    except Exception:
                        pass

        if not new_files:
            return

        def _mtime(p: Path) -> float:
            try:
                return float(p.stat().st_mtime)
            except Exception:
                return 0.0
        new_files = sorted(new_files, key=_mtime)

        subgenre = self._ace15_sanitize_filename_part(subgenre) or "Custom"
        seed_s = self._ace15_sanitize_filename_part(seed_s) or "AUTO"
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())

        renamed_any = False
        for idx, src in enumerate(new_files, start=1):
            ext = src.suffix or ""
            counter = f"_{idx}" if len(new_files) > 1 else ""
            base = f"{subgenre}__seed{seed_s}__{stamp}{counter}{ext}"
            dst = out_dir / base

            if dst.exists():
                i = 2
                while True:
                    cand = out_dir / f"{dst.stem}_{i}{dst.suffix}"
                    if not cand.exists():
                        dst = cand
                        break
                    i += 1

            try:
                src.rename(dst)
                renamed_any = True
                self._log(f"Renamed output: {src.name} -> {dst.name}")
            except Exception as e:
                self._log(f"NOTE: Could not rename '{src.name}': {e!r}")

        if renamed_any:
            try:
                self._ace15_out_snapshot = {str(p.resolve()) for p in list_audio_files(out_dir)}
            except Exception:
                pass

    def _move_instruction_txt_to_output(self, exit_code: int) -> None:
        proj = self._last_proj_root
        out_dir = self._last_out_dir
        cfg_path = self._last_cfg_path
        if not proj or not out_dir:
            return

        instr = proj / "instruction.txt"
        if not instr.exists() or not instr.is_file():
            return

        ensure_dir(out_dir)

        stamp = ""
        if cfg_path:
            stem = cfg_path.stem
            if stem.startswith("ace_step_run_"):
                stamp = stem.replace("ace_step_run_", "")
            else:
                stamp = stem
        if not stamp:
            stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())

        base_name = f"instruction_used_{stamp}.txt" if exit_code == 0 else f"instruction_error_{stamp}.txt"
        dst = out_dir / base_name
        if dst.exists():
            # Avoid overwriting; add a counter suffix.
            i = 2
            while True:
                cand = out_dir / f"{dst.stem}_{i}{dst.suffix}"
                if not cand.exists():
                    dst = cand
                    break
                i += 1

        # Move (not copy) so the repo does not keep a sticky instruction.txt.
        shutil.move(str(instr), str(dst))
        self._log(f"NOTE: Moved repo instruction.txt -> {dst}")

    def _set_busy(self, busy: bool):
        # Generate stays enabled while running so clicks can enqueue new jobs.
        try:
            self.btn_run.setEnabled(True)
        except Exception:
            pass
        self.btn_stop.setEnabled(busy)

        try:
            if busy:
                self._gen_anim_phase = 0
                if not hasattr(self, "_spin_frames"):
                    self._spin_frames = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]

                # Initial state
                self.btn_run.setText(f"Generating {self._spin_frames[0]}  (click to queue)")

                # Banner: text + indeterminate progress bar (nicer than braille)
                try:
                    if hasattr(self, "banner"):
                        self.banner.setText("Generating")
                    if hasattr(self, "banner_progress"):
                        self.banner_progress.setVisible(True)
                except Exception:
                    pass

                if self._gen_anim_timer is not None:
                    self._gen_anim_timer.start()
                # Force one immediate tick so it feels responsive
                self._tick_generate_anim()

            else:
                if self._gen_anim_timer is not None:
                    self._gen_anim_timer.stop()

                self.btn_run.setText(self._gen_btn_base_text or "Generate")

                try:
                    if hasattr(self, "banner"):
                        self.banner.setText(getattr(self, "_banner_base_text", "Music Creation with Ace Step 1.5"))
                    if hasattr(self, "banner_progress"):
                        self.banner_progress.setVisible(False)
                except Exception:
                    pass
        except Exception:
            pass



    def _log(self, line: str):
        self.txt_log.appendPlainText(line)
        sb = self.txt_log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _refresh_outputs(self):
        self.lst_outputs.clear()
        out_dir = Path(self.ed_outdir.text().strip())
        files = list_audio_files(out_dir)[:50]
        for p in files:
            it = QtWidgets.QListWidgetItem(f"{p.name}  —  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(p.stat().st_mtime))}")
            it.setData(QtCore.Qt.UserRole, str(p))
            self.lst_outputs.addItem(it)

    def _open_output_folder(self):
        out_dir = Path(self.ed_outdir.text().strip())
        if out_dir.exists():
            open_in_explorer(out_dir)

    def _open_selected_output(self, item: QtWidgets.QListWidgetItem):
        """Double-click behavior: load the file into the Preview pane and play."""
        p = Path(item.data(QtCore.Qt.UserRole))
        self._preview_load(p, autoplay=True)


    # ------------------------------------------------------------------
    # Preview player
    # ------------------------------------------------------------------
    def _preview_load(self, path: Path, autoplay: bool = False) -> None:
        try:
            if not path or not path.exists():
                return
        except Exception:
            return

        self._preview_path = path
        try:
            self.lbl_preview_now.setText(f"{path.name}")
        except Exception:
            pass
        try:
            self.btn_preview_open.setEnabled(True)
        except Exception:
            pass

        if self._preview_player is None:
            # QtMultimedia not available – still allow 'Open file'.
            try:
                self.btn_preview_play.setEnabled(False)
                self.btn_preview_stop.setEnabled(False)
                self.sld_preview.setEnabled(False)
                self.lbl_preview_time.setText("0:00 / 0:00")
            except Exception:
                pass
            return

        try:
            url = QtCore.QUrl.fromLocalFile(str(path))
            self._preview_player.setSource(url)
        except Exception:
            return

        try:
            self.btn_preview_play.setEnabled(True)
            self.btn_preview_stop.setEnabled(True)
            self.sld_preview.setEnabled(True)
        except Exception:
            pass

        if autoplay:
            try:
                self._preview_player.play()
            except Exception:
                pass

    def _preview_toggle_play(self) -> None:
        if self._preview_player is None:
            return
        try:
            state = self._preview_player.playbackState()
            if state == QtMultimedia.QMediaPlayer.PlayingState:
                self._preview_player.pause()
            else:
                self._preview_player.play()
        except Exception:
            pass

    def _preview_stop(self) -> None:
        if self._preview_player is None:
            return
        try:
            self._preview_player.stop()
        except Exception:
            pass

    def _preview_open_file(self) -> None:
        p = getattr(self, "_preview_path", None)
        if not p:
            return
        try:
            p = Path(p)
            if p.exists():
                open_in_explorer(p)
        except Exception:
            pass

    def _preview_slider_pressed(self) -> None:
        self._preview_dragging = True

    def _preview_slider_released(self) -> None:
        self._preview_dragging = False
        self._preview_seek_from_slider()

    def _preview_slider_moved(self, _v: int) -> None:
        self._preview_update_time_label(from_slider=True)

    def _preview_seek_from_slider(self) -> None:
        if self._preview_player is None:
            return
        try:
            dur = int(self._preview_player.duration() or 0)
            if dur <= 0:
                return
            frac = float(self.sld_preview.value()) / 1000.0
            pos = int(dur * frac)
            self._preview_player.setPosition(pos)
        except Exception:
            pass

    def _preview_on_position_changed(self, _pos: int) -> None:
        if self._preview_dragging:
            return
        try:
            dur = int(self._preview_player.duration() or 0)
            pos = int(self._preview_player.position() or 0)
            if dur > 0:
                self.sld_preview.blockSignals(True)
                self.sld_preview.setValue(int((pos / dur) * 1000.0))
                self.sld_preview.blockSignals(False)
        except Exception:
            pass
        self._preview_update_time_label(from_slider=False)

    def _preview_on_duration_changed(self, _dur: int) -> None:
        self._preview_update_time_label(from_slider=False)

    def _preview_on_state_changed(self, _state) -> None:
        if self._preview_player is None:
            return
        try:
            st = self._preview_player.playbackState()
            self.btn_preview_play.setText("Pause" if st == QtMultimedia.QMediaPlayer.PlayingState else "Play")
        except Exception:
            pass

    @staticmethod
    def _preview_fmt_ms(ms: int) -> str:
        try:
            s = max(0, int(ms // 1000))
            m = s // 60
            s = s % 60
            return f"{m}:{s:02d}"
        except Exception:
            return "0:00"

    def _preview_update_time_label(self, from_slider: bool) -> None:
        if self._preview_player is None:
            return
        try:
            dur = int(self._preview_player.duration() or 0)
            if from_slider and dur > 0:
                frac = float(self.sld_preview.value()) / 1000.0
                pos = int(dur * frac)
            else:
                pos = int(self._preview_player.position() or 0)
            self.lbl_preview_time.setText(f"{self._preview_fmt_ms(pos)} / {self._preview_fmt_ms(dur)}")
        except Exception:
            pass


    def _toggle_wheel_guard(self, enabled: bool):
        try:
            if enabled:
                install_no_wheel_guard()
            else:
                uninstall_no_wheel_guard()
        except Exception:
            pass

        # Persist
        try:
            self.settings.wheel_guard_enabled = bool(enabled)
            self._save_settings()
        except Exception:
            pass



    def _toggle_banner(self, enabled: bool):
        # Show/hide the top banner live and persist it
        try:
            self.settings.banner_enabled = bool(enabled)
        except Exception:
            pass

        try:
            if hasattr(self, "banner"):
                self.banner.setVisible(bool(enabled))
            if hasattr(self, "banner_gap"):
                self.banner_gap.setVisible(bool(enabled))
        except Exception:
            pass

        try:
            self._save_settings()
        except Exception:
            pass
    def _on_update_clicked(self):
        """Check GitHub repo for newer/changed files and optionally apply them."""
        try:
            changed = self._update_check_github_repo()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Update", f"Update check failed:\n\n{e}")
            return

        if not changed:
            mb = QtWidgets.QMessageBox(self)
            mb.setWindowTitle("Update")
            mb.setIcon(QtWidgets.QMessageBox.Information)
            mb.setText("No Updates")
            mb.setStandardButtons(QtWidgets.QMessageBox.Ok)
            mb.exec_()
            return

        selected = self._update_pick_files_dialog(changed)
        if selected is None:
            return
        if not selected:
            mb = QtWidgets.QMessageBox(self)
            mb.setWindowTitle("Update")
            mb.setIcon(QtWidgets.QMessageBox.Information)
            mb.setText("No files selected")
            mb.setInformativeText("The app needs restart after applying updates.")
            mb.setStandardButtons(QtWidgets.QMessageBox.Ok)
            mb.exec_()
            return

        changed = selected

        try:
            self._update_apply_files(changed)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Update", f"Failed to apply update:\n\n{e}")
            return

        done = QtWidgets.QMessageBox(self)
        done.setWindowTitle("Update")
        done.setIcon(QtWidgets.QMessageBox.Information)
        done.setText("Update applied")
        done.setInformativeText("The app needs restart after applying updates.")
        done.setStandardButtons(QtWidgets.QMessageBox.Ok)
        done.exec_()


    def _update_pick_files_dialog(self, changed_items):
        """Let the user select which changed items to apply. Returns a list (subset) or None if cancelled."""
        try:
            from PySide6 import QtCore, QtWidgets
        except Exception:
            # Fallback: apply all
            return changed_items

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Update")
        dlg.setModal(True)

        v = QtWidgets.QVBoxLayout(dlg)

        title = QtWidgets.QLabel(f"{len(changed_items)} new/changed files found. Select which files to apply:")
        title.setWordWrap(True)
        v.addWidget(title)

        info = QtWidgets.QLabel("The app needs restart after applying updates.")
        info.setWordWrap(True)
        v.addWidget(info)

        lst = QtWidgets.QListWidget()
        lst.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        v.addWidget(lst, 1)

        # Keep original items addressable by index
        for i, it in enumerate(changed_items):
            rel = it.get("rel_path", "")
            tag = "NEW" if it.get("is_new") else "CHANGED"
            witem = QtWidgets.QListWidgetItem(f"[{tag}] {rel}")
            witem.setFlags(witem.flags() | QtCore.Qt.ItemIsUserCheckable)
            witem.setCheckState(QtCore.Qt.Checked)
            witem.setData(QtCore.Qt.UserRole, i)
            lst.addItem(witem)

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        ok = bb.button(QtWidgets.QDialogButtonBox.Ok)
        if ok is not None:
            ok.setText("Apply Update")
        cancel = bb.button(QtWidgets.QDialogButtonBox.Cancel)
        if cancel is not None:
            cancel.setText("Cancel")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)

        dlg.resize(720, 420)

        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return None

        selected = []
        for row in range(lst.count()):
            witem = lst.item(row)
            if witem.checkState() == QtCore.Qt.Checked:
                idx = witem.data(QtCore.Qt.UserRole)
                if isinstance(idx, int) and 0 <= idx < len(changed_items):
                    selected.append(changed_items[idx])

        return selected

    def _update_check_github_repo(self):
        """Return a list of update items: dict(rel_path, data, is_new, is_changed)."""
        import hashlib, zipfile
        from urllib.request import urlopen, Request

        repo_zip = "https://github.com/Koongrizzly/Ace-Step-1.5-Pyside6-ui/archive/refs/heads/main.zip"
        req = Request(repo_zip, headers={"User-Agent": "Ace-Step-Updater"})
        with urlopen(req, timeout=30) as r:
            blob = r.read()

        zf = zipfile.ZipFile(io.BytesIO(blob))
        names = [n for n in zf.namelist() if not n.endswith('/')]
        if not names:
            return []
        root = names[0].split('/', 1)[0] + '/'

        base_dir = os.path.dirname(os.path.abspath(__file__))
        # If this file lives in a /helpers folder, project root is one level up.
        if os.path.basename(base_dir).lower() == 'helpers':
            base_dir = os.path.abspath(os.path.join(base_dir, '..'))


        def _ignore(rel):
            rel = rel.replace('\\', '/')
            if rel.startswith('.github/'):
                return True
            if rel.startswith('docs/'):
                return True
            low = rel.lower()
            if low in ('.gitignore',):
                return True
            if low.startswith('readme'):
                return True
            if low.endswith('.md'):
                return True
            return False

        changed = []
        for n in names:
            if not n.startswith(root):
                continue
            rel = n[len(root):]
            if not rel or _ignore(rel):
                continue
            data = zf.read(n)
            target = os.path.join(base_dir, rel)
            if not os.path.exists(target):
                changed.append({"rel_path": rel, "data": data, "is_new": True, "is_changed": True})
                continue
            try:
                with open(target, 'rb') as f:
                    old = f.read()
            except Exception:
                changed.append({"rel_path": rel, "data": data, "is_new": False, "is_changed": True})
                continue
            if hashlib.sha256(old).digest() != hashlib.sha256(data).digest():
                changed.append({"rel_path": rel, "data": data, "is_new": False, "is_changed": True})

        return changed

    def _update_apply_files(self, changed_items):
        import shutil, datetime

        base_dir = os.path.dirname(os.path.abspath(__file__))
        # If this file lives in a /helpers folder, project root is one level up.
        if os.path.basename(base_dir).lower() == 'helpers':
            base_dir = os.path.abspath(os.path.join(base_dir, '..'))

        stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_dir = os.path.join(base_dir, f"_update_backup_{stamp}")
        os.makedirs(backup_dir, exist_ok=True)

        for it in changed_items:
            rel = it.get('rel_path')
            data = it.get('data')
            if not rel or data is None:
                continue
            dst = os.path.join(base_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)

            if os.path.exists(dst):
                bdst = os.path.join(backup_dir, rel)
                os.makedirs(os.path.dirname(bdst), exist_ok=True)
                try:
                    shutil.copy2(dst, bdst)
                except Exception:
                    pass

            with open(dst, 'wb') as f:
                f.write(data)


    def _about(self):
        QtWidgets.QMessageBox.information(
            self,
            "About",
            "Ace-Step 1.5 UI\n\n"
            "This UI writes a TOML config and runs your cli.py inside the .ace_15 environment.\n",
            )

    def closeEvent(self, e):
        # Stop audio preview cleanly
        try:
            if getattr(self, "_preview_player", None) is not None:
                self._preview_player.stop()
        except Exception:
            pass
        # Stop generation if running
        try:
            self._stop()
        except Exception:
            pass
        return super().closeEvent(e)


def main():
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    # Start maximized for a more "app-like" experience.
    # (FrameVision embedding uses AceStep15Pane and is unaffected.)
    try:
        w.showMaximized()
    except Exception:
        # Fallback if showMaximized isn't supported on a given platform/window manager
        w.show()
    sys.exit(app.exec())




# -----------------------------
# FrameVision integration hook
# -----------------------------
class AceStep15Pane(QtWidgets.QWidget):
    """Embeddable QWidget wrapper for FrameVision tabs.

    Keeps the existing MainWindow logic intact by instantiating it and
    re-parenting its central widget into this pane.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mw = MainWindow()  # keep alive for slots/state
        try:
            cw = self._mw.takeCentralWidget()
        except Exception:
            cw = self._mw.centralWidget()
        if cw is None:
            cw = QtWidgets.QWidget()

        # If takeCentralWidget succeeded, the window now has no central widget.
        # If it didn't, we still re-parent the existing central widget.
        try:
            cw.setParent(self)
        except Exception:
            pass

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(cw, 1)

    def closeEvent(self, e):
        # Ensure background threads stop when the tab is closed / app exits.
        try:
            if hasattr(self._mw, '_stop_run'):
                self._mw._stop_run()
        except Exception:
            pass
        return super().closeEvent(e)


def create_pane(parent=None) -> QtWidgets.QWidget:
    """Factory used by FrameVision to create the Ace-Step 1.5 tab widget."""
    return AceStep15Pane(parent)


if __name__ == "__main__":
    main()
