"""Punto de entrada. Fase 1: FFB del DFGT con telemetría simulada.

Uso:
    python main.py --list                 # diagnóstico de dispositivos
    python main.py                        # prueba con velocidad simulada
    python main.py --strength 0.3 --duration 30
    python main.py --source ppsspp --strength 0.3   # velocidad real de Gran Turismo PSP
"""
from __future__ import annotations

import argparse
import logging
import sys

import winutils
from bridge import Bridge
from ffb_device import DEFAULT_NAME_HINT, FFBError, list_devices
from physics import PhysicsConfig
from telemetry import DEFAULT_SPEED_ADDRS, PPSSPPTelemetry, SimulatedTelemetry


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Puente FFB para Gran Turismo PSP (PPSSPP) - Fase 1")
    p.add_argument("--list", action="store_true", help="lista dispositivos y sale")
    p.add_argument("--source", choices=("sim", "ppsspp"), default="sim",
                   help="sim = velocidad simulada; ppsspp = velocidad real del juego")
    p.add_argument("--speed-addr", type=lambda x: int(x, 0), action="append", default=None,
                   help="dirección PSP de la velocidad (float32 km/h); repetible")
    p.add_argument("--device", default=DEFAULT_NAME_HINT, help="fragmento del nombre del volante")
    p.add_argument("--index", type=int, default=None, help="índice SDL (ver --list); anula --device")
    p.add_argument("--ffb-hz", type=float, default=200.0)
    p.add_argument("--telemetry-hz", type=float, default=120.0)
    p.add_argument("--strength", type=float, default=0.5, help="techo global de fuerza 0..1")
    p.add_argument("--duration", type=float, default=None, help="segundos; por defecto hasta Ctrl+C")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    if args.list:
        list_devices()
        return 0
    if not 0.0 <= args.strength <= 1.0:
        print("--strength debe estar entre 0 y 1")
        return 2

    winutils.enable_high_resolution_timer(1)
    winutils.boost_process_priority()
    if args.source == "ppsspp":
        source = PPSSPPTelemetry(speed_addrs=args.speed_addr or DEFAULT_SPEED_ADDRS)
    else:
        source = SimulatedTelemetry()
    bridge = Bridge(
        source=source,
        physics_cfg=PhysicsConfig(strength=args.strength),
        telemetry_hz=args.telemetry_hz,
        ffb_hz=args.ffb_hz,
        device_name=args.device,
        device_index=args.index,
    )
    try:
        bridge.start()
        if args.source == "ppsspp":
            print("FFB activo con la velocidad REAL del juego. Si PPSSPP está cerrado o pausado en "
                  "un menú verás 'vel nan': es normal. Ctrl+C para salir.")
        else:
            print("FFB activo. Gira el volante: la rigidez del muelle sube y baja con la velocidad "
                  "simulada (ciclo de 12 s). Ctrl+C para salir.")
        bridge.run(duration_s=args.duration)
    except FFBError as exc:
        print(f"\n[ERROR FFB] {exc}")
        return 1
    finally:
        winutils.disable_high_resolution_timer(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
