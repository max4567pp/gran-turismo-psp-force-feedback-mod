"""Fase 3: buscador de valores en la RAM del juego (estilo Cheat Engine).

    py scanner.py

Flujo típico (con el juego en PAUSA, Esc en PPSSPP, para que la memoria no cambie mientras se lee):
    scan f32 unknown            -> foto inicial de la RAM
    next increased              -> ahora el valor es MAYOR que en la foto anterior
    next decreased              -> ahora es MENOR
    next 143 tol=1              -> ahora vale ~143 (lo que muestra el HUD)
    list / watch #0 #1          -> ver candidatos y observarlos en vivo
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import time
from typing import Optional

from ppsspp_memory import (MemoryReadError, PPSSPPMemory, PPSSPPNotRunning, PSP_RAM_BASE)

SCAN_START = 0x00800000          # offset desde 0x08000000: se omiten los 8 MB de kernel
CHUNK = 1 << 20                  # lecturas de 1 MB (alineadas con los límites de región)
PAGE = 4096
MAX_CANDIDATES = 3_000_000

TYPES = {"i32": ("i", 4), "u32": ("I", 4), "i16": ("h", 2), "u16": ("H", 2),
         "u8": ("B", 1), "f32": ("f", 4)}
CONDITIONS = ("changed", "unchanged", "increased", "decreased")

HELP = """Comandos:
  scan <tipo> <valor>|unknown [tol=N] [factor=F]   primera búsqueda
        tipos: f32 i32 u32 i16 u16 u8
        valor: lo que muestra el HUD. Se busca en [valor-tol, valor+tol] * factor
        tol (por defecto 0.5) cubre el redondeo del HUD; factor convierte unidades,
        p. ej. el juego guarda m/s y el HUD muestra km/h: factor=0.27778
        'unknown' hace solo una foto, sin valor
  next <valor>|changed|unchanged|increased|decreased  filtra los candidatos
  list [n]                       muestra n candidatos con su valor actual (def. 30)
  watch <#n|0xDIRECCION> ...     muestra valores en vivo (Ctrl+C para parar)
  save [archivo]  /  load [archivo]   guardar / cargar candidatos (candidates.json)
  reset                          empezar de cero
  help  /  quit
"""


def _fmt(v) -> str:
    return f"{v:.4f}" if isinstance(v, float) else str(v)


class ScanSession:
    def __init__(self, mem) -> None:
        self.mem = mem
        self.tol = 0.5
        self.factor = 1.0
        self.reset()

    def reset(self) -> None:
        self.typ: Optional[str] = None
        self.fmt = ""
        self.size = 0
        self.idx: Optional[list[int]] = None     # índices de elemento (None = sin lista)
        self.prev: list = []
        self.snap0: Optional[bytes] = None       # foto del modo 'unknown'

    # ----------------------------------------------------------- utilidades
    def snapshot(self) -> bytes:
        parts = []
        for off in range(SCAN_START, self.mem.ram_size, CHUNK):
            parts.append(self.mem.read(PSP_RAM_BASE + off, min(CHUNK, self.mem.ram_size - off)))
        return b"".join(parts)

    def addr(self, i: int) -> int:
        return PSP_RAM_BASE + SCAN_START + i * self.size

    def _set_type(self, typ: str) -> None:
        if typ not in TYPES:
            raise ValueError(f"Tipo desconocido '{typ}'. Usa: {', '.join(TYPES)}")
        self.typ = typ
        self.fmt, self.size = TYPES[typ]

    def _range(self, value: float) -> tuple[float, float]:
        a, b = (value - self.tol) * self.factor, (value + self.tol) * self.factor
        return (a, b) if a <= b else (b, a)

    # ----------------------------------------------------------- primera búsqueda
    def scan(self, typ: str, value: Optional[float]) -> int:
        self._set_type(typ)
        snap = self.snapshot()
        if value is None:
            self.snap0, self.idx, self.prev = snap, None, []
            return -1
        lo, hi = self._range(value)
        mv = memoryview(snap).cast(self.fmt)
        self.idx = [i for i, v in enumerate(mv) if lo <= v <= hi]    # NaN nunca cumple
        self.prev = [mv[i] for i in self.idx]
        self.snap0 = None
        return len(self.idx)

    # ----------------------------------------------------------- filtros
    def next(self, cond: Optional[str], value: Optional[float]) -> int:
        if self.typ is None:
            raise ValueError("Primero usa 'scan'.")
        snap = self.snapshot()
        mv = memoryview(snap).cast(self.fmt)

        if self.snap0 is not None and self.idx is None:            # venimos de 'unknown'
            if cond == "unchanged":
                raise ValueError("Tras 'unknown' usa primero changed, increased, decreased o un valor.")
            if cond is None:                                        # valor numérico sobre toda la RAM
                lo, hi = self._range(value)
                self.idx = [i for i, v in enumerate(mv) if lo <= v <= hi]
                self.prev = [mv[i] for i in self.idx]
            else:
                self.idx, self.prev = self._diff(self.snap0, snap, cond)
            self.snap0 = None
            return len(self.idx)

        keep_i, keep_v = [], []
        if cond is None:
            lo, hi = self._range(value)
            for i, old in zip(self.idx, self.prev):
                new = mv[i]
                if lo <= new <= hi:
                    keep_i.append(i); keep_v.append(new)
        else:
            for i, old in zip(self.idx, self.prev):
                new = mv[i]
                if self._test(cond, old, new):
                    keep_i.append(i); keep_v.append(new)
        self.idx, self.prev = keep_i, keep_v
        return len(keep_i)

    def _ok(self, v) -> bool:
        return math.isfinite(v) if self.fmt == "f" else True

    def _test(self, cond: str, old, new) -> bool:
        if not self._ok(new):
            return False
        if cond == "changed":
            return new != old
        if cond == "unchanged":
            return new == old
        if cond == "increased":
            return new > old
        return new < old                                            # decreased

    def _diff(self, old: bytes, new: bytes, cond: str) -> tuple[list[int], list]:
        mo, mn = memoryview(old).cast(self.fmt), memoryview(new).cast(self.fmt)
        per = PAGE // self.size
        idx, vals = [], []
        for pb in range(0, len(old), PAGE):                         # páginas idénticas se saltan
            if old[pb:pb + PAGE] == new[pb:pb + PAGE]:
                continue
            base = pb // self.size
            for i in range(base, base + per):
                a, b = mo[i], mn[i]
                if self._test(cond, a, b):
                    idx.append(i); vals.append(b)
        return idx, vals

    # ----------------------------------------------------------- visualización
    def show(self, n: int = 30) -> None:
        if self.idx is None:
            print("No hay lista todavía (¿hiciste solo 'scan ... unknown'? usa 'next increased' etc.).")
            return
        snap = self.snapshot()
        mv = memoryview(snap).cast(self.fmt)
        print(f"{len(self.idx)} candidato(s) [{self.typ}]. Primeros {min(n, len(self.idx))}:")
        for k, i in enumerate(self.idx[:n]):
            print(f"  #{k:<4d} PSP 0x{self.addr(i):08X}  actual={_fmt(mv[i]):>14}  anterior={_fmt(self.prev[k]):>14}")

    def read_value(self, psp_addr: int, typ: str):
        fmt, size = TYPES[typ]
        return struct.unpack("<" + fmt, self.mem.read(psp_addr, size))[0]

    def resolve(self, token: str) -> tuple[str, int]:
        if token.startswith("#"):
            if self.idx is None:
                raise ValueError("No hay lista de candidatos.")
            return token, self.addr(self.idx[int(token[1:])])
        return token, int(token, 0)

    def watch(self, tokens: list[str], typ: str, hz: float = 10.0) -> None:
        targets = [self.resolve(t) for t in tokens]
        print("Observando en vivo. Ctrl+C para parar.\n")
        try:
            while True:
                cells = [f"{lbl}={_fmt(self.read_value(a, typ)):>12}" for lbl, a in targets]
                sys.stdout.write("\r" + "   ".join(cells) + "   ")
                sys.stdout.flush()
                time.sleep(1.0 / hz)
        except KeyboardInterrupt:
            print()

    # ----------------------------------------------------------- persistencia
    def save(self, path: str, limit: int = 20000) -> None:
        if self.idx is None:
            raise ValueError("No hay lista que guardar.")
        data = {"type": self.typ, "addresses": [self.addr(i) for i in self.idx[:limit]]}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        print(f"Guardados {len(data['addresses'])} candidatos en {path}")

    def load(self, path: str) -> None:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self._set_type(data["type"])
        base = PSP_RAM_BASE + SCAN_START
        self.idx = [(a - base) // self.size for a in data["addresses"]]
        mv = memoryview(self.snapshot()).cast(self.fmt)
        self.prev = [mv[i] for i in self.idx]
        self.snap0 = None
        print(f"Cargados {len(self.idx)} candidatos [{self.typ}]")


# --------------------------------------------------------------------------- REPL
def _parse(tokens: list[str]) -> tuple[list[str], dict[str, float]]:
    pos, opts = [], {}
    for t in tokens:
        if "=" in t:
            k, v = t.split("=", 1)
            opts[k.lower()] = float(v)
        else:
            pos.append(t)
    return pos, opts


def run_command(sess: ScanSession, line: str) -> bool:
    """Devuelve False si hay que salir."""
    parts = line.split()
    if not parts:
        return True
    cmd, args = parts[0].lower(), parts[1:]

    if cmd in ("quit", "exit", "q"):
        return False
    if cmd == "help":
        print(HELP)
    elif cmd == "reset":
        sess.reset(); print("Reiniciado.")
    elif cmd == "scan":
        pos, opts = _parse(args)
        if len(pos) != 2:
            raise ValueError("Uso: scan <tipo> <valor|unknown> [tol=N] [factor=F]")
        sess.tol = opts.get("tol", 0.5)
        sess.factor = opts.get("factor", 1.0)
        t0 = time.perf_counter()
        n = sess.scan(pos[0].lower(), None if pos[1].lower() == "unknown" else float(pos[1]))
        dt = time.perf_counter() - t0
        if n < 0:
            print(f"Foto guardada ({dt:.1f} s). Ahora cambia la situación del juego y usa 'next increased/decreased/changed'.")
        else:
            print(f"{n} candidato(s) en {dt:.1f} s.")
            if n > MAX_CANDIDATES:
                print("Demasiados candidatos: usa un valor más específico o 'unknown'.")
            elif n <= 30:
                sess.show()
    elif cmd == "next":
        pos, opts = _parse(args)
        if len(pos) != 1:
            raise ValueError("Uso: next <valor|changed|unchanged|increased|decreased> [tol=N] [factor=F]")
        sess.tol = opts.get("tol", sess.tol)
        sess.factor = opts.get("factor", sess.factor)
        word = pos[0].lower()
        t0 = time.perf_counter()
        n = sess.next(word if word in CONDITIONS else None, None if word in CONDITIONS else float(word))
        print(f"{n} candidato(s) en {time.perf_counter() - t0:.1f} s.")
        if 0 < n <= 30:
            sess.show()
    elif cmd == "list":
        sess.show(int(args[0]) if args else 30)
    elif cmd == "watch":
        pos, opts = _parse(args)
        typ = sess.typ or "f32"
        if not pos:
            raise ValueError("Uso: watch #0 #1 ...   o   watch 0x08A1B2C4")
        sess.watch(pos, typ)
    elif cmd == "save":
        sess.save(args[0] if args else "candidates.json")
    elif cmd == "load":
        sess.load(args[0] if args else "candidates.json")
    else:
        print(f"Comando desconocido: {cmd}. Escribe 'help'.")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Buscador de valores en la RAM de PPSSPP (Fase 3)")
    ap.add_argument("--process", default="PPSSPPWindows64.exe")
    args = ap.parse_args()
    try:
        with PPSSPPMemory(args.process) as mem:
            print(f"Conectado a PPSSPP (PID {mem.pid}). RAM: {mem.ram_size >> 20} MB en host 0x{mem.ram_host_base:X}")
            print("Escribe 'help' para ver los comandos. TIP: pausa el juego con Esc antes de cada scan/next.\n")
            sess = ScanSession(mem)
            while True:
                try:
                    line = input("scan> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                try:
                    if not run_command(sess, line):
                        break
                except (ValueError, KeyError, IndexError, MemoryReadError) as exc:
                    print(f"[ERROR] {exc}")
    except (PPSSPPNotRunning, OSError) as exc:
        print(f"[ERROR] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
