"""Zero-dependency Windows System Tray icon & menu via Win32 Shell_NotifyIconW."""
import os
from pathlib import Path
import threading
import webbrowser

from .icon import ensure_icon_file
from .paths import app_root, data_root


class SystemTray:
    """Runs Offset cleanly in the Windows notification tray with right-click controls."""

    def __init__(self, server, on_exit):
        self.server = server
        self.on_exit = on_exit
        self.supported = (os.name == "nt")
        self._hwnd = None
        self._stop_event = threading.Event()
        self._thread = None

    def url(self, dock=False):
        base = f"http://127.0.0.1:{self.server.server_port}"
        return base + ("/?dock=1" if dock else "/")

    def run_blocking(self):
        """Run the Win32 tray message loop on the calling thread (blocks until Exit)."""
        if not self.supported:
            self._stop_event.wait()
            return
        self._win32_loop()

    def stop(self):
        self._stop_event.set()
        if self.supported and self._hwnd:
            try:
                import ctypes
                ctypes.windll.user32.PostMessageW(self._hwnd, 0x0010, 0, 0)  # WM_CLOSE
            except Exception:
                pass

    def _win32_loop(self):
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32

        WM_DESTROY = 0x0002
        WM_CLOSE = 0x0010
        WM_COMMAND = 0x0111
        WM_TIMER = 0x0113
        WM_USER = 0x0400
        WM_TRAY = WM_USER + 20
        WM_LBUTTONDBLCLK = 0x0203
        WM_RBUTTONUP = 0x0205

        NIM_ADD = 0x00000000
        NIM_MODIFY = 0x00000001
        NIM_DELETE = 0x00000002
        NIF_MESSAGE = 0x00000001
        NIF_ICON = 0x00000002
        NIF_TIP = 0x00000004

        MF_STRING = 0x0000
        MF_SEPARATOR = 0x0800
        TPM_LEFTALIGN = 0x0000
        TPM_BOTTOMALIGN = 0x0020
        TPM_RIGHTBUTTON = 0x0002

        ID_OPEN_PANEL = 1001
        ID_OPEN_DOCK = 1002
        ID_TOGGLE_DELAY = 1003
        ID_TOGGLE_PANIC = 1004
        ID_EXIT = 1005

        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HICON),
            ]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
            ]

        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT

        icon_candidate = app_root() / "web" / "offset.ico"
        if not icon_candidate.is_file():
            icon_candidate = ensure_icon_file(data_root() / "offset.ico")
        hicon = user32.LoadImageW(None, str(icon_candidate), 1, 32, 32, 0x0010)  # IMAGE_ICON | LR_LOADFROMFILE
        if not hicon:
            hicon = user32.LoadIconW(None, 32512)  # IDI_APPLICATION

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = hicon
        nid.szTip = "Offset · Relay RTMP dinámico"

        def update_tip():
            try:
                st = self.server.controller.status()
                state = st.get("state", "READY")
                dm = st.get("delay_mode", "live")
                if state in ("READY", "STOPPED", "FINISHED"):
                    tip = "Offset · Sin conectar"
                elif state == "WAITING_INPUT":
                    tip = "Offset · Esperando OBS"
                elif dm == "slate_hold":
                    tip = "Offset · Directo oculto"
                elif dm == "delayed":
                    tip = f"Offset · Delay activo ({st.get('delay_target_s', 0):g}s)"
                elif dm == "slating":
                    tip = "Offset · Cargando delay..."
                elif dm == "catching_up":
                    tip = "Offset · Volviendo a directo"
                else:
                    tip = "Offset · En directo (0s)"
                nid.szTip = tip[:127]
                shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
            except Exception:
                pass

        def show_menu(hwnd):
            menu = user32.CreatePopupMenu()
            user32.AppendMenuW(menu, MF_STRING, ID_OPEN_PANEL, "Abrir panel de control")
            user32.AppendMenuW(menu, MF_STRING, ID_OPEN_DOCK, "Abrir vista Dock OBS")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, ID_TOGGLE_DELAY, "Activar / Quitar Delay")
            user32.AppendMenuW(menu, MF_STRING, ID_TOGGLE_PANIC, "Ocultar / Mostrar directo")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, ID_EXIT, "Salir de Offset")

            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(hwnd)
            user32.TrackPopupMenu(menu, TPM_LEFTALIGN | TPM_BOTTOMALIGN | TPM_RIGHTBUTTON, pt.x, pt.y, 0, hwnd, None)
            user32.DestroyMenu(menu)

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_TRAY:
                if lparam == WM_LBUTTONDBLCLK:
                    webbrowser.open(self.url(dock=False))
                elif lparam == WM_RBUTTONUP:
                    show_menu(hwnd)
                return 0
            if msg == WM_COMMAND:
                cmd = wparam & 0xFFFF
                if cmd == ID_OPEN_PANEL:
                    webbrowser.open(self.url(dock=False))
                elif cmd == ID_OPEN_DOCK:
                    webbrowser.open(self.url(dock=True))
                elif cmd == ID_TOGGLE_DELAY:
                    try:
                        self.server.controller.toggle_delay({})
                    except Exception:
                        pass
                elif cmd == ID_TOGGLE_PANIC:
                    try:
                        self.server.controller.panic_toggle()
                    except Exception:
                        pass
                elif cmd == ID_EXIT:
                    user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_TIMER:
                update_tip()
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        cb = WNDPROC(wnd_proc)
        hinst = kernel32.GetModuleHandleW(None)
        cls_name = "OffsetTrayWindowClass"
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = cb
        wc.hInstance = hinst
        wc.lpszClassName = cls_name
        user32.RegisterClassExW(ctypes.byref(wc))

        hwnd = user32.CreateWindowExW(0, cls_name, "OffsetTray", 0, 0, 0, 0, 0, None, None, hinst, None)
        self._hwnd = hwnd
        nid.hWnd = hwnd
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        user32.SetTimer(hwnd, 1, 2000, None)

        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.KillTimer(hwnd, 1)
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            self._hwnd = None
            if self.on_exit:
                self.on_exit()
