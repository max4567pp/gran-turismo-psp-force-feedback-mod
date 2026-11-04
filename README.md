# GT PSP FFB Bridge

Force Feedback (FFB) para **Gran Turismo PSP** en **PPSSPP (Windows)** con un volante
**Logitech Driving Force GT**. El juego original no tiene FFB, así que este proyecto:

1. Lee la telemetría (velocidad) de la RAM emulada de PPSSPP.
2. La convierte con un modelo de físicas en efectos de Force Feedback.
3. Los envía al volante por DirectInput (vía SDL2 Haptic).

> Autor: **max4567pp** · Proyecto independiente y desarrollado con asistencia de IA (Claude, de Anthropic), no afiliado a Sony, Polyphony Digital ni PPSSPP.
> No incluye ni distribuye el juego ni ninguna ROM/ISO.

## Estado

| Fase | Estado |
|---|---|
| 1. FFB base (muelle dinámico) con el DFGT | Hecho |
| 2. Localizar y leer la RAM de PPSSPP | Hecho |
| 3. Direcciones de memoria de Gran Turismo PSP (UCUS98632) | Velocidad hecha; ángulo/agarre en curso |
| 4. Modelo de físicas completo (fuerza lateral, derrapes, curbs) | Pendiente |

Probado con PPSSPP v1.20.4 y el juego `UCUS98632` en Windows 64 bit.

## Instalación

Requisitos: Windows, Python 3.10+ de 64 bit, driver de Logitech para el DFGT, volante con su
adaptador de corriente y el selector en modo **PC**.

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Uso

```
py main.py --list                                  # detectar volante
py main.py --strength 0.3                          # prueba con velocidad simulada
py main.py --source ppsspp --strength 0.3          # con la velocidad real del juego
py scanner.py                                      # buscador de direcciones de memoria
py memscan.py info                                 # diagnóstico de la RAM de PPSSPP
```

## Estructura

| Archivo | Función |
|---|---|
| `main.py`, `bridge.py` | Arranque y bucles de telemetría/FFB |
| `physics.py` | Modelo de físicas (telemetría -> fuerzas) |
| `ffb_device.py` | Efectos DirectInput vía SDL2 |
| `telemetry.py`, `ppsspp_memory.py` | Lectura de la RAM emulada |
| `scanner.py`, `memscan.py` | Herramientas para encontrar y diagnosticar direcciones |

## Licencia

MIT. Si usas o modificas este código, debes conservar el aviso de copyright.
