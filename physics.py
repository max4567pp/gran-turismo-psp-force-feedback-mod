"""Modelo de físicas: Telemetry -> FFBCommand (valores normalizados).

Todo aquí es matemática pura (sin SDL, sin Windows) => testeable y portable a C++.
Convenciones: coeficientes/saturaciones en 0..1, constant_level en -1..1.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from telemetry import Telemetry


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _slew(current: float, target: float, max_delta: float) -> float:
    """Limita la velocidad de cambio: evita tirones bruscos en el volante."""
    d = target - current
    if d > max_delta:
        return current + max_delta
    if d < -max_delta:
        return current - max_delta
    return target


@dataclass(frozen=True, slots=True)
class FFBCommand:
    spring_coeff: float = 0.0
    spring_saturation: float = 0.0
    damper_coeff: float = 0.0
    constant_level: float = 0.0


@dataclass(slots=True)
class PhysicsConfig:
    max_speed_kmh: float = 300.0
    spring_min: float = 0.05      # rigidez del muelle parado
    spring_max: float = 0.85      # rigidez a velocidad máxima
    spring_curve: float = 0.6     # <1: sube rápido a baja velocidad y satura después
    damper_min: float = 0.05
    damper_max: float = 0.20
    slew_per_sec: float = 3.0     # máx. cambio de parámetro (unidades/s)
    strength: float = 0.5         # techo global de seguridad 0..1
    stale_timeout_s: float = 0.25 # sin telemetría reciente -> relajar a cero


class FFBPhysics:
    def __init__(self, cfg: Optional[PhysicsConfig] = None) -> None:
        self.cfg = cfg or PhysicsConfig()
        self._spring = 0.0
        self._damper = 0.0

    def step(self, telemetry: Optional[Telemetry], now: float, dt: float) -> FFBCommand:
        cfg = self.cfg
        fresh = telemetry is not None and (now - telemetry.timestamp) <= cfg.stale_timeout_s

        if fresh:
            v = _clamp(telemetry.speed_kmh / cfg.max_speed_kmh, 0.0, 1.0)
            target_spring = _lerp(cfg.spring_min, cfg.spring_max, v ** cfg.spring_curve)
            target_damper = _lerp(cfg.damper_min, cfg.damper_max, v)
        else:
            # Seguridad: si el juego se pausa/cierra, la rueda se libera progresivamente.
            target_spring = 0.0
            target_damper = 0.0

        max_delta = cfg.slew_per_sec * max(dt, 0.0)
        self._spring = _slew(self._spring, target_spring, max_delta)
        self._damper = _slew(self._damper, target_damper, max_delta)

        s = _clamp(cfg.strength, 0.0, 1.0)
        return FFBCommand(
            spring_coeff=self._spring * s,
            spring_saturation=s if self._spring > 0.0 else 0.0,
            damper_coeff=self._damper * s,
            constant_level=0.0,   # Fase 3: fuerza lateral / pérdida de tracción
        )
