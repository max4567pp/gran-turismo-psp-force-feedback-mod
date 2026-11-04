"""Herramienta de diagnóstico de la Fase 2.

    python memscan.py info                       # localiza PPSSPP y la RAM emulada
    python memscan.py info --all-mapped          # además lista TODAS las vistas mapeadas grandes
    python memscan.py layout 0x13906CE0000       # vecinas + mapa datos/ceros de una región
    python memscan.py scanall                    # busca cadenas del juego en TODA la memoria
    python memscan.py analyze                    # perfil de contenido de cada candidata a RAM
    python memscan.py regions                    # diagnóstico: regiones grandes del proceso
    python memscan.py dump 0x08804000 64         # hexdump de memoria PSP
    python memscan.py find "UCUS98632"           # busca texto ASCII en la RAM
"""
from __future__ import annotations

import argparse
import sys

from ppsspp_memory import (MEM_FREE, MEM_MAPPED, PSP_USER_RAM, SAMPLE_LEN, SAMPLE_PSP_ADDR, MemoryReadError,
                           PPSSPPMemory, PPSSPPNotRunning)


def hexdump(data: bytes, base: int) -> str:
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hx = " ".join(f"{b:02X}" for b in chunk).ljust(47)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"0x{base + i:08X}  {hx}  {asc}")
    return "\n".join(lines)


def cmd_info(mem: PPSSPPMemory, all_mapped: bool) -> None:
    print(f"Proceso : {mem.process_name} (PID {mem.pid})")
    if all_mapped:
        print("\nVistas MEM_MAPPED >= 1 MB:")
        for r in mem.regions():
            if r.type == MEM_MAPPED and r.size >= 1 << 20:
                print(f"  host 0x{r.base:016X}  tamaño {r.size / (1 << 20):7.1f} MB  "
                      f"estado 0x{r.state:X}  protección 0x{r.protect:X}")
    print(f"\nCandidatas a RAM PSP (32/64 MB): {len(mem.candidates)}")
    for r in mem.candidates:
        print(f"  host 0x{r.base:016X}  {r.size >> 20} MB")
    print(f"\nRAM elegida : host 0x{mem.ram_host_base:016X} = PSP 0x08000000 | {mem.ram_size >> 20} MB")
    print(f"RAM usuario : host 0x{mem.host_address(PSP_USER_RAM):016X} = PSP 0x{PSP_USER_RAM:08X}")
    total = len(mem.candidates) - 1
    sample = mem.read(SAMPLE_PSP_ADDR, SAMPLE_LEN)
    nonzero = any(sample)
    print(f"Espejos con contenido idéntico: {mem.mirror_matches} de {total}")
    print(f"Muestra en 0x{SAMPLE_PSP_ADDR:08X} no vacía: {'SI' if nonzero else 'NO (¿juego ya cargado?)'}")
    verdict = "OK" if (mem.mirror_matches >= 1 and nonzero) else "DUDOSO - pégame esta salida completa"
    print(f"Veredicto   : {verdict}")
    print(f"\nPrimeros 64 bytes en 0x{SAMPLE_PSP_ADDR:08X}:")
    print(hexdump(sample[:64], SAMPLE_PSP_ADDR))


_PATTERNS = [b"UCUS98632", b"disc0:", b"PSP_GAME", b"ms0:", b"umd0:", b"\x7fELF", b"~PSP"]
_SHADES = " .:-=+*#%@"


def cmd_analyze(mem: PPSSPPMemory) -> None:
    """Perfil de contenido de cada candidata: ¿cuál parece la RAM real del juego?"""
    mb = 1 << 20
    if not mem.candidates:
        print("Sin candidatas de 32/64 MB; usa 'regions'.")
        return
    for idx, r in enumerate(mem.candidates):
        print(f"\n[{idx}] host 0x{r.base:016X}  {r.size >> 20} MB")
        bar, total_nz, data_parts = [], 0, []
        try:
            for off in range(0, r.size, mb):
                chunk = mem._read_raw(r.base + off, min(mb, r.size - off))
                nz = len(chunk) - chunk.count(0)
                total_nz += nz
                ratio = nz / len(chunk)
                bar.append(_SHADES[0] if nz == 0 else _SHADES[max(1, min(9, int(ratio * 10)))])
                data_parts.append(chunk)
        except MemoryReadError as exc:
            print(f"    error de lectura: {exc}")
            continue
        print(f"    densidad por MB (offset 0..{(r.size >> 20) - 1}): |{''.join(bar)}|")
        print(f"    bytes no cero: {100.0 * total_nz / r.size:5.2f}%")
        blob = b"".join(data_parts)
        for pat in _PATTERNS:
            pos = blob.find(pat)
            shown = pat.decode("latin1").replace("\x7f", "\\x7f")
            if pos == -1:
                print(f"    {shown:10s}: no encontrado")
            else:
                print(f"    {shown:10s}: offset 0x{pos:07X}  (si esta vista fuese PSP 0x08000000 -> PSP 0x{0x08000000 + pos:08X})")
        del blob, data_parts


_SCAN_PATTERNS = [b"UCUS98632", b"disc0:", b"PSP_GAME"]
_MEM_IMAGE = 0x1000000


def cmd_scanall(mem: PPSSPPMemory, chunk_mb: int = 16) -> None:
    """Busca cadenas del juego en TODA la memoria comprometida del proceso."""
    from ppsspp_memory import MEM_COMMIT
    chunk = chunk_mb << 20
    overlap = 16
    total = 0
    hits: dict[int, dict] = {}      # base de región -> info
    for r in mem.regions():
        if r.state != MEM_COMMIT or r.type == _MEM_IMAGE:
            continue
        if r.protect & 0x01 or r.protect & 0x100:      # NOACCESS / GUARD
            continue
        off = 0
        while off < r.size:
            n = min(chunk + overlap, r.size - off)
            try:
                data = mem._read_raw(r.base + off, n)
            except MemoryReadError:
                break                                   # región no legible: saltar
            total += n
            for pat in _SCAN_PATTERNS:
                pos = data.find(pat)
                if pos != -1:
                    info = hits.setdefault(r.base, {"r": r, "pats": {}})
                    c, first = info["pats"].get(pat, (0, None))
                    cnt = data.count(pat)
                    info["pats"][pat] = (c + cnt, first if first is not None else off + pos)
            off += chunk
    print(f"Memoria escaneada: {total / (1 << 30):.2f} GB")
    if not hits:
        print("\nNingún hit. ¿El juego está realmente cargado y corriendo (no en el menú de PPSSPP)?")
        return
    print(f"\nRegiones con coincidencias: {len(hits)}")
    for base in sorted(hits)[:40]:
        r, pats = hits[base]["r"], hits[base]["pats"]
        print(f"\n  región host 0x{r.base:016X}  {r.size / (1 << 20):9.2f} MB  "
              f"{_TYPES.get(r.type, hex(r.type))}  prot=0x{r.protect:02X}")
        for pat, (cnt, first) in pats.items():
            print(f"      {pat.decode():10s} x{cnt:<5d} primer hit en offset 0x{first:08X}")


def cmd_layout(mem: PPSSPPMemory, host: int, before_mb: int = 32, after_mb: int = 96,
               profile_mb: int = 16, block_kb: int = 64, skip_regions: bool = False) -> None:
    """Regiones vecinas a 'host' + mapa datos/ceros de sus primeros MB (para fijar PSP 0x08000000)."""
    mb = 1 << 20
    lo, hi = host - before_mb * mb, host + after_mb * mb
    if not skip_regions:
        print(f"Regiones entre 0x{lo:X} y 0x{hi:X} (cualquier tamaño/estado):\n")
    for r in ([] if skip_regions else mem.regions()):
        if r.base + r.size <= lo or r.base >= hi:
            continue
        mark = "  <== objetivo" if r.base == host else ""
        print(f"  0x{r.base:016X}  +0x{r.size:09X} ({r.size / mb:8.3f} MB)  "
              f"{_TYPES.get(r.type, hex(r.type)):8s} {_STATES.get(r.state, hex(r.state)):8s} "
              f"prot=0x{r.protect:02X}{mark}")

    print(f"\nMapa de contenido de los primeros {profile_mb} MB desde 0x{host:X} (bloques de {block_kb} KB):")
    block = block_kb * 1024
    segs: list[list] = []          # [inicio, fin, tiene_datos]
    try:
        for off in range(0, profile_mb * mb, block):
            chunk = mem._read_raw(host + off, block)
            has = chunk.count(0) != len(chunk)
            if segs and segs[-1][2] == has:
                segs[-1][1] = off + block
            else:
                segs.append([off, off + block, has])
    except MemoryReadError as exc:
        print(f"  (lectura detenida: {exc})")
    for a, b, has in segs[:60]:
        print(f"  offset 0x{a:07X} - 0x{b - 1:07X}  {'DATOS' if has else 'ceros'}")
    if len(segs) > 60:
        print(f"  ... {len(segs) - 60} tramos más")


_TYPES = {0x1000000: "IMAGE", 0x40000: "MAPPED", 0x20000: "PRIVATE"}
_STATES = {0x1000: "COMMIT", 0x2000: "RESERVE"}


def cmd_regions(mem: PPSSPPMemory, min_mb: float) -> None:
    """Lista regiones grandes del proceso SIN asumir cómo está organizada la RAM."""
    print(f"Proceso : {mem.process_name} (PID {mem.pid})")
    print(f"Regiones no libres >= {min_mb} MB (dirección host, tamaño, tipo, estado, protección):\n")
    n = 0
    for r in mem.regions():
        if r.state == MEM_FREE or r.size < min_mb * (1 << 20):
            continue
        n += 1
        print(f"  0x{r.base:016X}  {r.size / (1 << 20):9.2f} MB  "
              f"{_TYPES.get(r.type, hex(r.type)):8s} {_STATES.get(r.state, hex(r.state)):8s} "
              f"prot=0x{r.protect:02X}  alloc=0x{r.alloc_base:X}")
    print(f"\n{n} región(es).")


def main() -> int:
    p = argparse.ArgumentParser(description="Diagnóstico de memoria de PPSSPP (Fase 2)")
    p.add_argument("--process", default="PPSSPPWindows64.exe")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("info")
    pi.add_argument("--all-mapped", action="store_true")
    sub.add_parser("analyze")
    sub.add_parser("scanall")
    pl = sub.add_parser("layout")
    pl.add_argument("host", type=lambda x: int(x, 0), help="dirección host de la región, p. ej. 0x13906CE0000")
    pl.add_argument("--profile-mb", type=int, default=16)
    pl.add_argument("--block-kb", type=int, default=64)
    pl.add_argument("--skip-regions", action="store_true")
    pr = sub.add_parser("regions")
    pr.add_argument("--min-mb", type=float, default=4.0)
    pd = sub.add_parser("dump")
    pd.add_argument("addr", type=lambda s: int(s, 0))
    pd.add_argument("length", type=lambda s: int(s, 0), nargs="?", default=64)
    pf = sub.add_parser("find")
    pf.add_argument("text")
    args = p.parse_args()

    try:
        if args.cmd == "regions":
            mem = PPSSPPMemory(args.process)
            mem.open_process()
            try:
                cmd_regions(mem, args.min_mb)
            finally:
                mem.detach()
            return 0
        if args.cmd == "layout":
            mem = PPSSPPMemory(args.process)
            mem.open_process()
            try:
                cmd_layout(mem, args.host, profile_mb=args.profile_mb,
                           block_kb=args.block_kb, skip_regions=args.skip_regions)
            finally:
                mem.detach()
            return 0
        if args.cmd == "scanall":
            mem = PPSSPPMemory(args.process)
            mem.open_process()
            try:
                cmd_scanall(mem)
            finally:
                mem.detach()
            return 0
        with PPSSPPMemory(args.process) as mem:
            if args.cmd == "info":
                cmd_info(mem, args.all_mapped)
            elif args.cmd == "analyze":
                cmd_analyze(mem)
            elif args.cmd == "dump":
                print(hexdump(mem.read(args.addr, args.length), args.addr))
            elif args.cmd == "find":
                hits = mem.find_ascii(args.text)
                print(f"{len(hits)} coincidencia(s) para {args.text!r}:")
                for h in hits:
                    print(f"  PSP 0x{h:08X}")
    except (PPSSPPNotRunning, MemoryReadError, ValueError, OSError) as exc:
        print(f"[ERROR] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
