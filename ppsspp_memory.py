"""Fase 2: acceso de solo lectura a la RAM emulada de PPSSPP (Windows 64 bit).

Cómo funciona (hipótesis a verificar con `memscan.py info` en tu build):
  PPSSPP reserva espacio virtual y mapea la RAM de la PSP (32 o 64 MB) como vistas
  MEM_MAPPED a offsets fijos desde una base `g_base`:
      host = g_base + dirección_psp
  La vista "normal" de RAM es la de la dirección PSP 0x08000000; hay espejos (0x48000000,
  0x88000000...) con el MISMO contenido. g_base cambia en cada ejecución (ASLR), así que se
  redescubre al adjuntarse enumerando regiones con VirtualQueryEx.

Todo usa ctypes con tipos explícitos (imprescindible en 64 bit: sin argtypes ctypes
trunca punteros a 32 bit).
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterator, Optional

# ----------------------------------------------------------------------- constantes
PSP_RAM_BASE = 0x08000000        # inicio de la RAM PSP (kernel: 0x08000000, usuario: 0x08800000)
PSP_USER_RAM = 0x08800000
PSP_ADDR_MASK = 0x3FFFFFFF       # quita los bits de caché/kernel (0x48.., 0x88.., 0xA8..)
RAM_SIZES = (0x02000000, 0x04000000)   # 32 MB (fat) o 64 MB (slim/extendida)
SAMPLE_PSP_ADDR = 0x08800000     # inicio de la RAM de usuario: tiene datos reales (el kernel deja 4 MB de ceros antes)
SAMPLE_LEN = 0x10000             # 64 KB

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
TH32CS_SNAPPROCESS = 0x00000002
MEM_COMMIT = 0x1000
MEM_MAPPED = 0x40000
MEM_PRIVATE = 0x20000
MEM_FREE = 0x10000
WRITABLE_MASK = 0xCC   # PAGE_READWRITE | WRITECOPY | EXECUTE_READWRITE | EXECUTE_WRITECOPY
PAGE_READWRITE = 0x04
_MAX_USER_ADDR = 0x7FFFFFFF0000


class PPSSPPNotRunning(RuntimeError):
    pass


class MemoryReadError(RuntimeError):
    pass


# ----------------------------------------------------------------------- estructuras Win32
class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),   # ULONG_PTR
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


_k32 = None


def _kernel32():
    """Carga perezosa: permite importar el módulo (y testear lógica pura) fuera de Windows."""
    global _k32
    if _k32 is not None:
        return _k32
    if sys.platform != "win32":
        raise OSError("ppsspp_memory solo funciona en Windows.")
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    k.VirtualQueryEx.argtypes = [wintypes.HANDLE, wintypes.LPCVOID,
                                 ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
    k.VirtualQueryEx.restype = ctypes.c_size_t
    k.ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID,
                                    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    k.ReadProcessMemory.restype = wintypes.BOOL
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k.Process32FirstW.restype = wintypes.BOOL
    k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k.Process32NextW.restype = wintypes.BOOL
    _k32 = k
    return k


# ----------------------------------------------------------------------- lógica pura (testeable)
@dataclass(frozen=True)
class Region:
    base: int
    size: int
    type: int
    state: int
    protect: int
    alloc_base: int = 0


def psp_offset(psp_addr: int) -> int:
    """Dirección PSP -> offset dentro de la RAM (0 = 0x08000000). Resuelve espejos."""
    off = (psp_addr & PSP_ADDR_MASK) - PSP_RAM_BASE
    if not 0 <= off < RAM_SIZES[-1]:
        raise ValueError(f"Dirección PSP fuera de la RAM: 0x{psp_addr:08X}")
    return off


def merge_adjacent(regions: list[Region]) -> list[Region]:
    """Une regiones consecutivas del mismo tipo (Windows parte una vista en varias si se
    mapeó con varias llamadas). En tu PPSSPP la RAM aparece como 31 + 31 + 2 MB contiguos."""
    good = sorted((r for r in regions
                   if r.type in (MEM_MAPPED, MEM_PRIVATE) and r.state == MEM_COMMIT
                   and (r.protect & WRITABLE_MASK)), key=lambda r: r.base)
    runs: list[Region] = []
    for r in good:
        if runs and runs[-1].base + runs[-1].size == r.base and runs[-1].type == r.type:
            prev = runs[-1]
            runs[-1] = Region(prev.base, prev.size + r.size, prev.type, prev.state, prev.protect)
        else:
            runs.append(r)
    return runs


def _same(read_fn, a: int, b: int, retries: int = 5) -> bool:
    """Compara dos zonas; reintenta porque el juego puede escribir entre ambas lecturas."""
    for _ in range(retries):
        if read_fn(a, SAMPLE_LEN) == read_fn(b, SAMPLE_LEN):
            return True
    return False


def select_ram_view(regions: list[Region], read_fn) -> tuple[Optional[Region], int, list[Region]]:
    """Localiza el bloque de RAM PSP (32/64 MB contiguos) y su dirección host de PSP 0x08000000.

    1) Une regiones consecutivas. 2) Candidatas = bloques de 32 o 64 MB. 3) Verifica con los
    ESPEJOS (la PSP ve la misma RAM en 0x08.., 0x48.., 0x88..): se elige el bloque con más
    espejos de contenido idéntico en el inicio de la RAM de usuario (0x08800000); a igualdad,
    la menor dirección host.
    Devuelve (bloque, nº de espejos coincidentes, candidatas).
    """
    cands = [r for r in merge_adjacent(regions) if r.size in RAM_SIZES]
    if not cands:
        return None, 0, []
    off = SAMPLE_PSP_ADDR - PSP_RAM_BASE
    best, best_matches = None, -1
    for r in cands:
        matches = sum(1 for o in cands
                      if o is not r and o.size == r.size
                      and _same(read_fn, r.base + off, o.base + off))
        if matches > best_matches:
            best, best_matches = r, matches
    return best, best_matches, cands


# ----------------------------------------------------------------------- acceso al proceso
class PPSSPPMemory:
    def __init__(self, process_name: str = "PPSSPPWindows64.exe") -> None:
        self.process_name = process_name
        self.pid: Optional[int] = None
        self._handle = None
        self.ram_host_base = 0        # host address de PSP 0x08000000
        self.ram_size = 0
        self.candidates: list[Region] = []
        self.mirror_matches = 0

    # ---- ciclo de vida
    def attach(self) -> None:
        self.open_process()
        self.locate_ram()

    def open_process(self) -> None:
        k = _kernel32()
        self.pid = self._find_pid(self.process_name)
        if self.pid is None:
            raise PPSSPPNotRunning(f"No se encontró el proceso '{self.process_name}'. "
                                   "¿PPSSPP abierto con el juego cargado?")
        handle = k.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, self.pid)
        if not handle:
            raise MemoryReadError(f"OpenProcess falló (error {ctypes.get_last_error()}). "
                                  "Si PPSSPP corre como administrador, ejecuta también esto como administrador.")
        self._handle = handle

    def locate_ram(self) -> None:
        chosen, matches, cands = select_ram_view(list(self.regions()), self._read_raw)
        self.candidates, self.mirror_matches = cands, matches
        if chosen is None:
            raise PPSSPPNotRunning(
                "PPSSPP está abierto, pero no encontré la RAM emulada con el patrón esperado. "
                "Ejecuta: python memscan.py regions   y pégame la salida.")
        self.ram_host_base = chosen.base
        self.ram_size = chosen.size

    def detach(self) -> None:
        if self._handle:
            _kernel32().CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "PPSSPPMemory":
        self.attach()
        return self

    def __exit__(self, *exc) -> None:
        self.detach()

    # ---- proceso / regiones
    @staticmethod
    def _find_pid(name: str) -> Optional[int]:
        k = _kernel32()
        snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap is None or snap == ctypes.c_void_p(-1).value:
            return None
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = k.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                if entry.szExeFile.lower() == name.lower():
                    return int(entry.th32ProcessID)
                ok = k.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k.CloseHandle(snap)
        return None

    def regions(self) -> Iterator[Region]:
        k = _kernel32()
        mbi = MEMORY_BASIC_INFORMATION()
        addr = 0
        while addr < _MAX_USER_ADDR:
            if not k.VirtualQueryEx(self._handle, addr, ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            base = mbi.BaseAddress or 0
            yield Region(base, mbi.RegionSize, mbi.Type, mbi.State, mbi.Protect,
                         mbi.AllocationBase or 0)
            nxt = base + mbi.RegionSize
            if nxt <= addr:
                break
            addr = nxt

    # ---- lectura
    def _read_raw(self, host_addr: int, size: int) -> bytes:
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        ok = _kernel32().ReadProcessMemory(self._handle, host_addr, buf, size, ctypes.byref(got))
        if not ok or got.value != size:
            raise MemoryReadError(f"ReadProcessMemory falló en 0x{host_addr:X} "
                                  f"({got.value}/{size} bytes, error {ctypes.get_last_error()})")
        return buf.raw

    def read(self, psp_addr: int, size: int) -> bytes:
        off = psp_offset(psp_addr)
        if off + size > self.ram_size:
            raise ValueError("Lectura fuera de la RAM emulada.")
        return self._read_raw(self.ram_host_base + off, size)

    def host_address(self, psp_addr: int) -> int:
        return self.ram_host_base + psp_offset(psp_addr)

    def find_ascii(self, text: str, max_hits: int = 20, chunk: int = 4 * 1024 * 1024) -> list[int]:
        """Busca una cadena ASCII en toda la RAM; devuelve direcciones PSP."""
        pat = text.encode("ascii")
        hits: list[int] = []
        pos = 0
        while pos < self.ram_size and len(hits) < max_hits:
            n = min(chunk + len(pat) - 1, self.ram_size - pos)
            data = self._read_raw(self.ram_host_base + pos, n)
            i = data.find(pat)
            while i != -1 and len(hits) < max_hits:
                hits.append(PSP_RAM_BASE + pos + i)
                i = data.find(pat, i + 1)
            pos += chunk
        return hits
