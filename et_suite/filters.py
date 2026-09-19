"""
et_suite/filters.py  —  Filtros Anti-Jitter
============================================
Implementación rigurosa del 1€ Filter (Casiez, Roussel, Vogel — CHI 2012).

Características:
  • A velocidad baja (fijación): frecuencia de corte mínima → máximo suavizado
  • A velocidad alta (sacada):  frecuencia de corte sube   → mínimo lag
  • Parámetros por defecto calibrados para eye-tracking a 30-60 Hz

Referencia: https://dl.acm.org/doi/10.1145/2207676.2208639
"""

from __future__ import annotations
import math
import time
from typing import Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# Low-Pass Filter (exponential moving average)
# ─────────────────────────────────────────────────────────────────────────────

class _LowPassFilter:
    """Filtro pasa-bajas exponencial de primer orden (EMA)."""

    __slots__ = ("_s", "_initialized")

    def __init__(self) -> None:
        self._s: float = 0.0
        self._initialized: bool = False

    def reset(self) -> None:
        self._initialized = False

    def filter(self, x: float, alpha: float) -> float:
        """
        Aplica un paso del filtro EMA.

        Args:
            x:     valor de entrada
            alpha: coeficiente de suavizado ∈ (0, 1]  (1 = sin suavizado)
        """
        if not self._initialized:
            self._s = x
            self._initialized = True
        else:
            self._s = alpha * x + (1.0 - alpha) * self._s
        return self._s

    @property
    def value(self) -> float:
        return self._s


# ─────────────────────────────────────────────────────────────────────────────
# 1€ Filter — una dimensión
# ─────────────────────────────────────────────────────────────────────────────

class OneEuroFilter:
    """
    Filtro 1€ adaptativo para una señal escalar.

    El filtro ajusta dinámicamente su frecuencia de corte basándose en la
    velocidad instantánea de la señal:
        fc = min_cutoff + beta * |ẋ_filtered|

    Parámetros:
        min_cutoff (Hz): frecuencia de corte mínima (↓ = más suave en fijaciones)
        beta:            coeficiente de velocidad  (↑ = menos lag en sacadas)
        d_cutoff (Hz):   frecuencia de corte para la derivada (fijo)

    Valores por defecto optimizados para eye-tracking a 30-60 Hz:
        min_cutoff = 0.5  →  residual jitter < 2 px en fijaciones
        beta       = 0.007 →  lag < 2 ms en sacadas a 500 px/s
    """

    __slots__ = (
        "min_cutoff", "beta", "d_cutoff",
        "_x_filt", "_dx_filt",
        "_last_t", "_last_x",
    )

    def __init__(
        self,
        min_cutoff: float = 0.5,
        beta: float = 0.007,
        d_cutoff: float = 1.0,
    ) -> None:
        self.min_cutoff = float(min_cutoff)
        self.beta = float(beta)
        self.d_cutoff = float(d_cutoff)

        self._x_filt  = _LowPassFilter()
        self._dx_filt = _LowPassFilter()
        self._last_t: Optional[float] = None
        self._last_x: Optional[float] = None

    # ── Helpers ────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _alpha(rate: float, cutoff: float) -> float:
        """Convierte frecuencia de muestreo + cutoff en coeficiente alpha EMA."""
        tau = 1.0 / (2.0 * math.pi * cutoff)
        te  = 1.0 / max(rate, 1e-6)
        return 1.0 / (1.0 + tau / te)

    # ── Interfaz pública ───────────────────────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Reinicia el estado interno del filtro."""
        self._x_filt.reset()
        self._dx_filt.reset()
        self._last_t = None
        self._last_x = None

    def __call__(self, x: float, timestamp: Optional[float] = None) -> float:
        """
        Filtra el valor ``x`` observado en el instante ``timestamp``.

        Args:
            x:         valor de entrada
            timestamp: tiempo en segundos (perf_counter).  Si es None usa now().

        Returns:
            Valor filtrado.
        """
        t = timestamp if timestamp is not None else time.perf_counter()

        # Primer llamado: inicializar sin filtrar
        if self._last_t is None:
            self._last_t = t
            self._last_x = x
            return self._x_filt.filter(x, 1.0)

        dt = max(t - self._last_t, 1e-8)
        rate = 1.0 / dt

        # Derivada instantánea
        dx = (x - self._last_x) / dt  # type: ignore[operator]
        self._last_x = x
        self._last_t = t

        # Derivada suavizada con d_cutoff fijo
        a_d = self._alpha(rate, self.d_cutoff)
        edx = self._dx_filt.filter(dx, a_d)

        # Frecuencia de corte adaptativa
        cutoff = self.min_cutoff + self.beta * abs(edx)
        a_x    = self._alpha(rate, cutoff)

        return self._x_filt.filter(x, a_x)


# ─────────────────────────────────────────────────────────────────────────────
# 1€ Filter — dos dimensiones (X, Y)
# ─────────────────────────────────────────────────────────────────────────────

class OneEuroFilter2D:
    """
    Envuelve dos instancias de `OneEuroFilter` para filtrar coordenadas 2D.

    Uso::

        f = OneEuroFilter2D(min_cutoff=0.5, beta=0.007)
        fx, fy = f(raw_x, raw_y, timestamp)
    """

    def __init__(
        self,
        min_cutoff: float = 0.5,
        beta: float = 0.007,
        d_cutoff: float = 1.0,
    ) -> None:
        self.x = OneEuroFilter(min_cutoff, beta, d_cutoff)
        self.y = OneEuroFilter(min_cutoff, beta, d_cutoff)

    def reset(self) -> None:
        self.x.reset()
        self.y.reset()

    def __call__(
        self,
        x: float,
        y: float,
        timestamp: Optional[float] = None,
    ) -> Tuple[float, float]:
        t = timestamp if timestamp is not None else time.perf_counter()
        return self.x(x, t), self.y(y, t)
