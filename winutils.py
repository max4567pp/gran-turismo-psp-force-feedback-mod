"""Utilidades específicas de Windows: resolución del timer y prioridad del proceso.

Por defecto Windows planifica hilos con ticks de ~15.6 ms. Para un bucle de FFB a
200-500 Hz eso es inaceptable, así que pedimos resolución de 1 ms al sistema.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

_IS_WINDOWS = sys.platform == "win32"
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000


def enable_high_resolution_timer(period_ms: int = 1) -> bool:
    """winmm.timeBeginPeriod(1). Debe emparejarse con disable_high_resolution_timer."""
    if not _IS_WINDOWS:
        return False
    try:
        return ctypes.windll.winmm.timeBeginPeriod(period_ms) == 0  # TIMERR_NOERROR
    except OSError:
        return False


def disable_high_resolution_timer(period_ms: int = 1) -> None:
    if not _IS_WINDOWS:
        return
    try:
        ctypes.windll.winmm.timeEndPeriod(period_ms)
    except OSError:
        pass


def boost_process_priority() -> bool:
    """Sube el proceso a ABOVE_NORMAL (no requiere admin, evita ser relegado por PPSSPP)."""
    if not _IS_WINDOWS:
        return False
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Tipos explícitos: sin ellos ctypes trunca HANDLEs de 64 bit a int de 32 bit.
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.SetPriorityClass.restype = wintypes.BOOL
    return bool(k32.SetPriorityClass(k32.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS))
