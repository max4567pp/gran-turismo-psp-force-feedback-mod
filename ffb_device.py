"""Acceso al Force Feedback mediante SDL2 Haptic (que en Windows usa DirectInput).

Por qué SDL2 y no DirectInput a mano con ctypes: SDL ya implementa los vtables COM de
IDirectInput8 / IDirectInputDevice8 / IDirectInputEffect, y sus llamadas
(SDL_HapticUpdateEffect -> IDirectInputEffect::SetParameters) son las mismas que haría
tu código C++. La latencia la domina el USB y el firmware del volante, no Python.
"""
from __future__ import annotations

import ctypes
import logging
from typing import Optional

try:
    import sdl2
except ImportError as exc:  # pragma: no cover
    raise ImportError("Falta PySDL2. Ejecuta: pip install pysdl2 pysdl2-dll") from exc

from physics import FFBCommand

log = logging.getLogger("ffb")

DEFAULT_NAME_HINT = "driving force"   # coincide con "Logitech Driving Force GT USB"


class FFBError(RuntimeError):
    pass


# --------------------------------------------------------------------------- helpers
def _sdl_error() -> str:
    return sdl2.SDL_GetError().decode("utf-8", "replace")


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _s16(x: float) -> int:
    return int(_clamp(x, -1.0, 1.0) * 32767)


def _u16(x: float) -> int:
    return int(_clamp(x, 0.0, 1.0) * 65535)


def _q(x: float) -> float:
    """Cuantiza a 8 bits: evita llamadas a SetParameters por cambios imperceptibles."""
    return round(x * 255.0) / 255.0


def _joy_name(index: int) -> str:
    raw = sdl2.SDL_JoystickNameForIndex(index)
    return raw.decode("utf-8", "replace") if raw else "(sin nombre)"


def _init_sdl() -> None:
    # Forzamos el backend DirectInput: los drivers alternativos de SDL (HIDAPI/RawInput/
    # XInput) pueden "capturar" el volante y entonces NO expone la API haptic.
    for hint, value in (
        (b"SDL_JOYSTICK_HIDAPI", b"0"),
        (b"SDL_JOYSTICK_RAWINPUT", b"0"),
        (b"SDL_XINPUT_ENABLED", b"0"),
        (b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1"),
    ):
        sdl2.SDL_SetHint(hint, value)
    if sdl2.SDL_Init(sdl2.SDL_INIT_JOYSTICK | sdl2.SDL_INIT_HAPTIC) != 0:
        raise FFBError(f"SDL_Init falló: {_sdl_error()}")


def list_devices() -> None:
    """Diagnóstico: lista dispositivos y si exponen FFB."""
    _init_sdl()
    try:
        n = sdl2.SDL_NumJoysticks()
        print(f"SDL detecta {n} dispositivo(s):")
        for i in range(n):
            joy = sdl2.SDL_JoystickOpen(i)
            flag = "?"
            if joy:
                flag = "SI" if sdl2.SDL_JoystickIsHaptic(joy) == 1 else "NO"
                sdl2.SDL_JoystickClose(joy)
            print(f"  [{i}] {_joy_name(i)}  | FFB: {flag}")
    finally:
        sdl2.SDL_Quit()


# --------------------------------------------------------------------------- efectos
class _EffectSlot:
    """Un efecto DirectInput vivo en el volante + su último valor enviado."""

    def __init__(self, haptic, effect, name: str, initial: tuple) -> None:
        self._haptic = haptic
        self.effect = effect          # estructura ctypes: se muta y se reenvía
        self.name = name
        self.last = initial
        self.id = sdl2.SDL_HapticNewEffect(haptic, ctypes.byref(effect))
        if self.id < 0:
            raise FFBError(f"No se pudo crear el efecto '{name}': {_sdl_error()}")

    def run(self) -> None:
        # length = INFINITY, iterations = 1 -> corre hasta que lo detengamos.
        if sdl2.SDL_HapticRunEffect(self._haptic, self.id, 1) != 0:
            raise FFBError(f"No se pudo iniciar '{self.name}': {_sdl_error()}")

    def push(self) -> None:
        if sdl2.SDL_HapticUpdateEffect(self._haptic, self.id, ctypes.byref(self.effect)) != 0:
            log.warning("UpdateEffect('%s') falló: %s", self.name, _sdl_error())

    def destroy(self) -> None:
        sdl2.SDL_HapticStopEffect(self._haptic, self.id)
        sdl2.SDL_HapticDestroyEffect(self._haptic, self.id)


class FFBDevice:
    """Encapsula el volante: abre el dispositivo, crea efectos y actualiza parámetros.

    IMPORTANTE: crea, usa y cierra el dispositivo siempre desde el MISMO hilo.
    """

    def __init__(self, name_hint: str = DEFAULT_NAME_HINT, index: Optional[int] = None) -> None:
        self._name_hint = name_hint
        self._index = index
        self._joystick = None
        self._haptic = None
        self._naxes = 1
        self._sdl_ready = False
        self._spring: Optional[_EffectSlot] = None
        self._damper: Optional[_EffectSlot] = None
        self._constant: Optional[_EffectSlot] = None

    # ------------------------------------------------------------ ciclo de vida
    def open(self) -> None:
        _init_sdl()
        self._sdl_ready = True
        try:
            self._open_device()
        except Exception:
            self.close()
            raise

    def _open_device(self) -> None:
        idx = self._find_index()
        log.info("Usando dispositivo [%d] %s", idx, _joy_name(idx))

        self._joystick = sdl2.SDL_JoystickOpen(idx)
        if not self._joystick:
            raise FFBError(f"SDL_JoystickOpen falló: {_sdl_error()}")
        if sdl2.SDL_JoystickIsHaptic(self._joystick) != 1:
            raise FFBError("El dispositivo no expone Force Feedback (revisa driver Logitech y "
                           "el selector PS3/PC del volante).")

        self._haptic = sdl2.SDL_HapticOpenFromJoystick(self._joystick)
        if not self._haptic:
            raise FFBError(f"SDL_HapticOpenFromJoystick falló: {_sdl_error()}")

        supported = sdl2.SDL_HapticQuery(self._haptic)
        self._naxes = max(1, min(sdl2.SDL_HapticNumAxes(self._haptic), 3))
        log.info("Ejes FFB: %d | efectos: spring=%s damper=%s constant=%s",
                 self._naxes,
                 bool(supported & sdl2.SDL_HAPTIC_SPRING),
                 bool(supported & sdl2.SDL_HAPTIC_DAMPER),
                 bool(supported & sdl2.SDL_HAPTIC_CONSTANT))

        # Apagamos el autocentrado interno del firmware para controlarlo nosotros.
        if supported & sdl2.SDL_HAPTIC_AUTOCENTER:
            sdl2.SDL_HapticSetAutocenter(self._haptic, 0)
        if supported & sdl2.SDL_HAPTIC_GAIN:
            sdl2.SDL_HapticSetGain(self._haptic, 100)

        if not supported & sdl2.SDL_HAPTIC_SPRING:
            raise FFBError("El dispositivo no soporta el efecto SPRING.")
        self._spring = self._make_condition_slot(sdl2.SDL_HAPTIC_SPRING, "spring", 0.0, 0.0)

        if supported & sdl2.SDL_HAPTIC_DAMPER:
            self._damper = self._make_condition_slot(sdl2.SDL_HAPTIC_DAMPER, "damper", 0.0, 1.0)
        else:
            log.warning("Sin soporte DAMPER: se omite.")

        if supported & sdl2.SDL_HAPTIC_CONSTANT:
            self._constant = self._make_constant_slot()
        else:
            log.warning("Sin soporte CONSTANT: se omite.")

        for slot in (self._spring, self._damper, self._constant):
            if slot is not None:
                slot.run()

    def close(self) -> None:
        if self._haptic:
            sdl2.SDL_HapticStopAll(self._haptic)
        for slot in (self._spring, self._damper, self._constant):
            if slot is not None:
                try:
                    slot.destroy()
                except Exception:  # pragma: no cover
                    pass
        self._spring = self._damper = self._constant = None
        if self._haptic:
            sdl2.SDL_HapticClose(self._haptic)
            self._haptic = None
        if self._joystick:
            sdl2.SDL_JoystickClose(self._joystick)
            self._joystick = None
        if self._sdl_ready:
            sdl2.SDL_Quit()
            self._sdl_ready = False

    def __enter__(self) -> "FFBDevice":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ construcción de efectos
    def _find_index(self) -> int:
        n = sdl2.SDL_NumJoysticks()
        if n <= 0:
            raise FFBError("SDL no detecta ningún joystick. ¿Volante conectado y en modo PC?")
        if self._index is not None:
            if not 0 <= self._index < n:
                raise FFBError(f"Índice {self._index} fuera de rango (0..{n - 1}).")
            return self._index
        hint = self._name_hint.lower()
        for i in range(n):
            if hint in _joy_name(i).lower():
                return i
        names = ", ".join(f"[{i}] {_joy_name(i)}" for i in range(n))
        raise FFBError(f"Ningún dispositivo coincide con '{self._name_hint}'. Detectados: {names}")

    def _fill_condition(self, effect, coeff: float, saturation: float) -> None:
        c = effect.condition
        k, s = _s16(coeff), _u16(saturation)
        for i in range(self._naxes):
            c.right_coeff[i] = k
            c.left_coeff[i] = k
            c.right_sat[i] = s
            c.left_sat[i] = s
            c.deadband[i] = 0
            c.center[i] = 0

    def _make_condition_slot(self, kind: int, name: str, coeff: float, sat: float) -> _EffectSlot:
        effect = sdl2.SDL_HapticEffect()
        c = effect.condition
        c.type = kind
        c.direction.type = sdl2.SDL_HAPTIC_CARTESIAN
        c.direction.dir[0] = 1
        c.length = sdl2.SDL_HAPTIC_INFINITY
        c.delay = 0
        self._fill_condition(effect, coeff, sat)
        return _EffectSlot(self._haptic, effect, name, initial=(coeff,) if name == "damper" else (coeff, sat))

    def _make_constant_slot(self) -> _EffectSlot:
        effect = sdl2.SDL_HapticEffect()
        c = effect.constant
        c.type = sdl2.SDL_HAPTIC_CONSTANT
        c.direction.type = sdl2.SDL_HAPTIC_CARTESIAN
        c.direction.dir[0] = 1          # eje X; el signo de 'level' decide izquierda/derecha
        c.length = sdl2.SDL_HAPTIC_INFINITY
        c.delay = 0
        c.level = 0
        c.attack_length = 0
        c.fade_length = 0
        return _EffectSlot(self._haptic, effect, "constant", initial=(0.0,))

    # ------------------------------------------------------------ API en tiempo real
    def set_spring(self, coeff: float, saturation: float) -> None:
        slot = self._spring
        if slot is None:
            return
        q = (_q(coeff), _q(saturation))
        if q == slot.last:
            return
        self._fill_condition(slot.effect, q[0], q[1])
        slot.push()
        slot.last = q

    def set_damper(self, coeff: float) -> None:
        slot = self._damper
        if slot is None:
            return
        q = (_q(coeff),)
        if q == slot.last:
            return
        self._fill_condition(slot.effect, q[0], 1.0)
        slot.push()
        slot.last = q

    def set_constant(self, level: float) -> None:
        slot = self._constant
        if slot is None:
            return
        q = (_q(level),)
        if q == slot.last:
            return
        slot.effect.constant.level = _s16(q[0])
        slot.push()
        slot.last = q

    def apply(self, cmd: FFBCommand) -> None:
        self.set_spring(cmd.spring_coeff, cmd.spring_saturation)
        self.set_damper(cmd.damper_coeff)
        self.set_constant(cmd.constant_level)
