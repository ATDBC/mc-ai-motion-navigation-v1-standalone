"""Windows handle operations limited to the exact owned game process; no input spoofing/capture."""
from dataclasses import asdict
import ctypes
from ctypes import wintypes

from scripts.visual_demo_session import identity_alive


def owned_windows(identity) -> list[dict]:
    if identity is None or not identity_alive(asdict(identity)): return []
    user32=ctypes.WinDLL("user32",use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes=[wintypes.HWND]
    user32.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
    user32.GetClassNameW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    user32.GetWindowTextW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    windows=[]
    @callback_type
    def visit(hwnd,unused):
        pid=wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        if pid.value!=identity.pid or not user32.IsWindowVisible(hwnd): return True
        name=ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd,name,len(name))
        if name.value!="GLFW30": return True
        rect=wintypes.RECT()
        if not user32.GetWindowRect(hwnd,ctypes.byref(rect)): return True
        if rect.right-rect.left<400 or rect.bottom-rect.top<300: return True
        title=ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd,title,len(title))
        windows.append(dict(hwnd=int(hwnd),pid=pid.value,title=title.value,
            rect=[rect.left,rect.top,rect.right,rect.bottom],class_name=name.value))
        return True
    user32.EnumWindows.argtypes=[callback_type,wintypes.LPARAM]
    if not user32.EnumWindows(visit,0): raise ctypes.WinError(ctypes.get_last_error())
    return windows if identity_alive(asdict(identity)) else []


def close_owned_windows(identity) -> int:
    windows=owned_windows(identity)
    if not windows: return 0
    user32=ctypes.WinDLL("user32",use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    user32.PostMessageW.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
    sent=0
    for window in windows:
        pid=wintypes.DWORD()
        user32.GetWindowThreadProcessId(window["hwnd"],ctypes.byref(pid))
        if pid.value==identity.pid and identity_alive(asdict(identity)):
            sent+=bool(user32.PostMessageW(window["hwnd"],0x0010,0,0))  # Normal OS close request, not a key event.
    return sent
