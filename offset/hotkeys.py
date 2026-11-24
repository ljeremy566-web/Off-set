"""Zero-dependency Windows global hotkeys via Win32 RegisterHotKey (no-op elsewhere)."""
import os
import threading

DEFAULT_BINDINGS = {
    "toggle_delay": "Ctrl+Shift+F9",
    "jump_live": "Ctrl+Shift+F10",
    "catchup_live": "Ctrl+Shift+F11",
    "panic_slate": "Ctrl+Shift+F12",
}

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

_VK_MAP = {
    **{f"F{i}": 0x6F + i for i in range(1, 13)},
    **{chr(c): c for c in range(ord("A"), ord("Z") + 1)},
    **{str(d): 0x30 + d for d in range(10)},
    "SPACE": 0x20,
    "INSERT": 0x2D,
    "DELETE": 0x2E,
    "HOME": 0x24,
    "END": 0x23,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
}

_MOD_MAP = {
    "CTRL": MOD_CONTROL,
    "CONTROL": MOD_CONTROL,
    "ALT": MOD_ALT,
    "SHIFT": MOD_SHIFT,
    "WIN": MOD_WIN,
    "META": MOD_WIN,
}


def parse_hotkey(combo):
    """Parse 'Ctrl+Shift+F9' into (modifiers_mask, vk_code)."""
    if not isinstance(combo, str) or not combo.strip():
        raise ValueError("Atajo vacío.")
    parts = [p.strip().upper() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError("Atajo inválido.")
    mods = MOD_NOREPEAT
    vk = None
    for part in parts:
        if part in _MOD_MAP:
            mods |= _MOD_MAP[part]
        elif part in _VK_MAP and vk is None:
            vk = _VK_MAP[part]
        else:
            raise ValueError(f"Tecla no reconocida en atajo: {part}")
    if vk is None:
        raise ValueError("Falta una tecla principal en el atajo (ej. F9).")
    return mods, vk


class HotkeyManager:
    """Manages optional Windows global hotkeys in a dedicated Win32 message thread."""

    def __init__(self, callback):
        self.callback = callback
        self.lock = threading.Lock()
        self.enabled = False
        self.bindings = dict(DEFAULT_BINDINGS)
        self.registered = {}
        self.supported = (os.name == "nt")
        self._thread = None
        self._thread_id = None
        self._ready = threading.Event()

    def status(self):
        with self.lock:
            return {
                "supported": self.supported,
                "enabled": self.enabled,
                "bindings": dict(self.bindings),
                "registered": dict(self.registered),
            }

    def configure(self, enabled, bindings=None):
        new_bindings = dict(self.bindings)
        if bindings is not None:
            if not isinstance(bindings, dict):
                raise ValueError("Atajos inválidos.")
            for action in DEFAULT_BINDINGS:
                if action in bindings:
                    val = str(bindings[action] or "").strip()
                    if val:
                        parse_hotkey(val)
                    new_bindings[action] = val
        self.stop()
        with self.lock:
            self.bindings = new_bindings
            self.enabled = bool(enabled) and self.supported
            self.registered = {}
        if self.enabled:
            self.start()
        return self.status()

    def start(self):
        if not self.supported:
            return
        with self.lock:
            if self._thread and self._thread.is_alive():
                return
            self.enabled = True
            self._ready.clear()
            self._thread = threading.Thread(target=self._run_win32, daemon=True, name="offset-hotkeys")
            self._thread.start()
        self._ready.wait(timeout=2.0)

    def stop(self):
        with self.lock:
            tid = self._thread_id
            thread = self._thread
        if self.supported and tid:
            try:
                import ctypes
                ctypes.windll.user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:
                pass
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self.lock:
            self._thread = None
            self._thread_id = None
            self.registered = {}

    def _run_win32(self):
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        tid = kernel32.GetCurrentThreadId()
        id_to_action = {}
        reg_status = {}

        with self.lock:
            self._thread_id = tid
            bindings = dict(self.bindings)

        # Ensure thread message queue exists before RegisterHotKey / PostThreadMessageW.
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)

        for idx, (action, combo) in enumerate(bindings.items(), start=1):
            if not combo:
                reg_status[action] = {"ok": False, "combo": "", "error": "Sin asignar"}
                continue
            try:
                mods, vk = parse_hotkey(combo)
                ok = bool(user32.RegisterHotKey(None, idx, mods, vk))
                if ok:
                    id_to_action[idx] = action
                    reg_status[action] = {"ok": True, "combo": combo, "error": None}
                else:
                    reg_status[action] = {"ok": False, "combo": combo, "error": "En uso por otra app"}
            except ValueError as exc:
                reg_status[action] = {"ok": False, "combo": combo, "error": str(exc)}

        with self.lock:
            self.registered = reg_status
        self._ready.set()

        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    action = id_to_action.get(int(msg.wParam))
                    if action:
                        try:
                            self.callback(action)
                        except Exception:
                            pass
        finally:
            for idx in id_to_action:
                try:
                    user32.UnregisterHotKey(None, idx)
                except Exception:
                    pass
            with self.lock:
                self._thread_id = None
                self.registered = {}
