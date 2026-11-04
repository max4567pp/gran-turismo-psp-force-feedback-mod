"""Orquestación: dos bucles con ritmo fijo desacoplados por un bus de telemetría.

    [TelemetryThread] --publish--> [TelemetryBus] --latest--> [FFBThread] --> volante
        (lee RAM, ~120 Hz)         (ref. atómica)              (físicas + DInput, ~200 Hz)

El hilo FFB nunca espera al de telemetría: si los datos se atrasan, usa el último
snapshot y, si pasa de stale_timeout_s, el modelo relaja las fuerzas a cero.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from ffb_device import DEFAULT_NAME_HINT, FFBDevice
from physics import FFBCommand, FFBPhysics, PhysicsConfig
from telemetry import Telemetry, TelemetrySource, TelemetryUnavailable

log = logging.getLogger("bridge")


class TelemetryBus:
    """Asignar una referencia es atómico en CPython; Telemetry es inmutable => sin locks."""
    __slots__ = ("_snap",)

    def __init__(self) -> None:
        self._snap: Optional[Telemetry] = None

    def publish(self, snap: Telemetry) -> None:
        self._snap = snap

    def latest(self) -> Optional[Telemetry]:
        return self._snap


class LoopStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0
        self._busy_max = 0.0

    def record(self, busy_s: float) -> None:
        with self._lock:
            self._count += 1
            if busy_s > self._busy_max:
                self._busy_max = busy_s

    def drain(self) -> tuple[int, float]:
        with self._lock:
            res = (self._count, self._busy_max)
            self._count = 0
            self._busy_max = 0.0
            return res


class PacedThread(threading.Thread):
    """Bucle a frecuencia fija con deadline absoluto (no acumula deriva)."""

    def __init__(self, name: str, hz: float, stop_event: threading.Event,
                 spin_margin_s: float = 0.0007) -> None:
        super().__init__(name=name, daemon=True)
        self.period = 1.0 / hz
        self.stop_event = stop_event
        self.spin_margin_s = spin_margin_s
        self.stats = LoopStats()
        self.ready = threading.Event()
        self.error: Optional[BaseException] = None

    def on_start(self) -> None: ...
    def on_stop(self) -> None: ...
    def tick(self, now: float, dt: float) -> None: raise NotImplementedError

    def run(self) -> None:
        try:
            self.on_start()
            self.ready.set()
            last = next_t = time.perf_counter()
            while not self.stop_event.is_set():
                t0 = time.perf_counter()
                dt = min(t0 - last, 0.1) if t0 > last else self.period
                last = t0
                self.tick(t0, dt)
                t1 = time.perf_counter()
                self.stats.record(t1 - t0)
                next_t += self.period
                if t1 > next_t + 5 * self.period:      # nos quedamos muy atrás: resincronizar
                    next_t = t1
                self._wait_until(next_t)
        except BaseException as exc:                     # noqa: BLE001
            self.error = exc
            log.exception("Hilo %s terminó con error", self.name)
            self.stop_event.set()
        finally:
            self.ready.set()
            try:
                self.on_stop()
            except Exception:                            # noqa: BLE001
                log.exception("Error en on_stop de %s", self.name)

    def _wait_until(self, deadline: float) -> None:
        # sleep() grueso + spin corto final: buena precisión sin quemar un núcleo entero.
        while not self.stop_event.is_set():
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return
            if remaining > 2 * self.spin_margin_s:
                time.sleep(remaining - self.spin_margin_s)


class TelemetryThread(PacedThread):
    def __init__(self, source: TelemetrySource, bus: TelemetryBus, hz: float,
                 stop_event: threading.Event) -> None:
        super().__init__("telemetry", hz, stop_event)
        self._source = source
        self._bus = bus
        self.misses = 0

    def on_start(self) -> None:
        self._source.open()

    def tick(self, now: float, dt: float) -> None:
        try:
            self._bus.publish(self._source.read())
        except TelemetryUnavailable:
            self.misses += 1

    def on_stop(self) -> None:
        self._source.close()


class FFBThread(PacedThread):
    def __init__(self, bus: TelemetryBus, physics: FFBPhysics, hz: float,
                 stop_event: threading.Event, name_hint: str, index: Optional[int]) -> None:
        super().__init__("ffb", hz, stop_event)
        self._bus = bus
        self._physics = physics
        self._name_hint = name_hint
        self._index = index
        self._device: Optional[FFBDevice] = None
        self.last_cmd = FFBCommand()
        self.last_snap: Optional[Telemetry] = None

    def on_start(self) -> None:
        self._device = FFBDevice(self._name_hint, self._index)
        self._device.open()

    def tick(self, now: float, dt: float) -> None:
        snap = self._bus.latest()
        cmd = self._physics.step(snap, now, dt)
        self._device.apply(cmd)
        self.last_cmd, self.last_snap = cmd, snap

    def on_stop(self) -> None:
        if self._device is not None:
            try:
                self._device.apply(FFBCommand())   # soltar el volante antes de cerrar
            finally:
                self._device.close()


class Bridge:
    def __init__(self, source: TelemetrySource, physics_cfg: Optional[PhysicsConfig] = None,
                 telemetry_hz: float = 120.0, ffb_hz: float = 200.0,
                 device_name: str = DEFAULT_NAME_HINT, device_index: Optional[int] = None) -> None:
        self.stop_event = threading.Event()
        self.bus = TelemetryBus()
        self.telemetry = TelemetryThread(source, self.bus, telemetry_hz, self.stop_event)
        self.ffb = FFBThread(self.bus, FFBPhysics(physics_cfg), ffb_hz, self.stop_event,
                             device_name, device_index)

    def start(self) -> None:
        for th in (self.telemetry, self.ffb):
            th.start()
            th.ready.wait(timeout=10.0)
            if th.error is not None:
                self.stop()
                raise th.error

    def stop(self) -> None:
        self.stop_event.set()
        for th in (self.ffb, self.telemetry):
            if th.is_alive():
                th.join(timeout=2.0)

    def run(self, duration_s: Optional[float] = None, report_every_s: float = 2.0) -> None:
        t_start = last_report = time.perf_counter()
        try:
            # Espera corta: en Windows Ctrl+C solo se procesa cuando wait() retorna.
            while not self.stop_event.wait(0.25):
                now = time.perf_counter()
                if duration_s is not None and now - t_start >= duration_s:
                    break
                if now - last_report >= report_every_s:
                    self._report(now - t_start, now - last_report)
                    last_report = now
        except KeyboardInterrupt:
            print("\nInterrumpido por el usuario.")
        finally:
            self.stop()

    def _report(self, elapsed: float, window: float) -> None:
        t_cnt, _ = self.telemetry.stats.drain()
        f_cnt, f_max = self.ffb.stats.drain()
        snap, cmd = self.ffb.last_snap, self.ffb.last_cmd
        speed = snap.speed_kmh if snap else float("nan")
        print(f"t={elapsed:6.1f}s | telem {t_cnt / window:5.0f} Hz | "
              f"ffb {f_cnt / window:5.0f} Hz (tick max {f_max * 1000:4.2f} ms) | "
              f"vel {speed:5.1f} km/h | spring {cmd.spring_coeff:.2f} | damper {cmd.damper_coeff:.2f}")
