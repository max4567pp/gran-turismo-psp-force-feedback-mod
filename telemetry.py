"""Capa de telemetría: define QUÉ datos consume el FFB, no de DÓNDE vienen.

Fase 1: SimulatedTelemetry (sin juego).
Fase 2: PPSSPPTelemetry (lectura de la RAM emulada con ReadProcessMemory).
El resto del sistema solo conoce la interfaz TelemetrySource.
"""
from __future__ import annotations

import logging
import math
import struct
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from ppsspp_memory import MemoryReadError, PPSSPPMemory, PPSSPPNotRunning

log = logging.getLogger("telemetry")

# Direcciones PSP de la velocidad (float32, km/h) encontradas con scanner.py en
# Gran Turismo PSP UCUS98632. Las dos siguen la velocidad sin retraso; se usa la primera válida.
DEFAULT_SPEED_ADDRS = (0x0BD05D94, 0x0BD05EE0)


@dataclass(frozen=True, slots=True)
class Telemetry:
    """Snapshot inmutable. Al ser inmutable se puede compartir entre hilos sin locks."""
    timestamp: float          # time.perf_counter() del momento de la lectura
    speed_kmh: float = 0.0
    steering: float = 0.0     # -1.0 (izq) .. +1.0 (der)
    slip: float = 0.0         # 0.0 (agarre total) .. 1.0 (derrape total)


class TelemetryUnavailable(RuntimeError):
    """El origen de datos no está disponible ahora (p. ej. PPSSPP cerrado)."""


class TelemetrySource(ABC):
    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def read(self) -> Telemetry: ...

    def close(self) -> None:
        pass


class SimulatedTelemetry(TelemetrySource):
    """Acelera y frena suavemente 0 -> max -> 0 para probar el efecto sin el juego."""

    def __init__(self, max_speed_kmh: float = 300.0, period_s: float = 12.0) -> None:
        self._max = max_speed_kmh
        self._period = period_s
        self._t0 = 0.0

    def open(self) -> None:
        self._t0 = time.perf_counter()

    def read(self) -> Telemetry:
        now = time.perf_counter()
        t = now - self._t0
        phase = 0.5 - 0.5 * math.cos(2.0 * math.pi * t / self._period)   # 0..1..0
        slip = min(1.0, max(0.0, (phase - 0.75) / 0.25))
        return Telemetry(
            timestamp=now,
            speed_kmh=self._max * phase,
            steering=0.8 * math.sin(2.0 * math.pi * t / 4.0),
            slip=slip,
        )


class PPSSPPTelemetry(TelemetrySource):
    """Lee la velocidad real del juego desde la RAM emulada de PPSSPP.

    - open() no falla si PPSSPP aún no está abierto: read() reintenta conectar cada retry_s.
    - Si PPSSPP se cierra o se reinicia (la base de RAM cambia), se reengancha solo.
    - Valores no válidos (NaN, fuera de rango) se tratan como 'sin telemetría', y el modelo
      de físicas relaja las fuerzas.
    """

    def __init__(self, speed_addrs: Sequence[int] = DEFAULT_SPEED_ADDRS,
                 process_name: str = "PPSSPPWindows64.exe", retry_s: float = 1.0,
                 max_speed_kmh: float = 1000.0,
                 mem_factory: Optional[Callable[[], PPSSPPMemory]] = None) -> None:
        self._addrs = tuple(speed_addrs)
        self._max = max_speed_kmh
        self._retry_s = retry_s
        self._factory = mem_factory or (lambda: PPSSPPMemory(process_name))
        self._mem: Optional[PPSSPPMemory] = None
        self._next_try = 0.0

    def open(self) -> None:
        self._try_attach()

    def close(self) -> None:
        self._drop()

    def _drop(self) -> None:
        if self._mem is not None:
            try:
                self._mem.detach()
            except Exception:                                   # noqa: BLE001
                pass
            self._mem = None

    def _try_attach(self) -> bool:
        now = time.perf_counter()
        if now < self._next_try:
            return False
        self._next_try = now + self._retry_s
        mem = self._factory()
        try:
            mem.attach()
        except (PPSSPPNotRunning, MemoryReadError, OSError) as exc:
            log.debug("Sin conexión a PPSSPP: %s", exc)
            try:
                mem.detach()
            except Exception:                                   # noqa: BLE001
                pass
            return False
        self._mem = mem
        log.info("PPSSPP conectado (PID %s, RAM %d MB)", mem.pid, mem.ram_size >> 20)
        return True

    def read(self) -> Telemetry:
        if self._mem is None and not self._try_attach():
            raise TelemetryUnavailable("PPSSPP no disponible")
        try:
            for addr in self._addrs:
                v = struct.unpack("<f", self._mem.read(addr, 4))[0]
                if math.isfinite(v) and -1.0 <= v <= self._max:
                    return Telemetry(timestamp=time.perf_counter(), speed_kmh=max(v, 0.0))
        except (MemoryReadError, OSError, ValueError) as exc:
            self._drop()                                        # reconectará en el próximo intento
            raise TelemetryUnavailable(str(exc)) from exc
        raise TelemetryUnavailable("valor de velocidad no válido")
