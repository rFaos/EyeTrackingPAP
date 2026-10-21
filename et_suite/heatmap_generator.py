"""
et_suite/heatmap_generator.py  —  Heatmaps, scanpaths y mapas de AOI
=====================================================================
Tres tipos de visualización:

  1. Densidad de fijaciones — cada fijación suma 1 (¿dónde miró más veces?)
  2. Dwell time             — cada fijación pesa su duración (¿cuánto tiempo?)
  3. Scanpath               — fijaciones numeradas en orden (¿en qué orden?)

Novedades respecto a la versión anterior:

  • Por TAREA: un juego de imágenes por cada tarea del estudio.
  • Se dibujan los rectángulos de los AOI con su nombre.
  • Mapas en coordenadas de PÁGINA cuando hay scroll (puente web): el calor se
    acumula sobre la página completa, no sobre la ventana visible.
  • Mapas AGREGADOS de varios participantes (``render_group``), donde cada
    persona pesa lo mismo aunque haya mirado más tiempo.

Técnica:
    • scipy.ndimage.gaussian_filter para suavizado Gaussiano 2D
    • cv2.COLORMAP_JET (o cualquier otro de OpenCV) como mapa de calor
    • Blend alpha sobre imagen de fondo (captura de pantalla o fondo blanco)

API antigua (sigue funcionando)::

    gen = HeatmapGenerator(screen_w=1920, screen_h=1080)
    gen.fixation_density("sessions/s.csv", background="captura.png")
    gen.dwell_time("sessions/s.csv")
    gen.scanpath("sessions/s.csv")
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from scipy.ndimage import gaussian_filter

from .metrics import MIN_FIXATION_MS, Analysis, Fix, extract_fixations, read_rows

# Rect de AOI para dibujar: (etiqueta, x0, y0, x1, y1)
Rect = Tuple[str, float, float, float, float]

_AOI_COLOR = (200, 90, 20)       # BGR: azul oscuro, se lee sobre calor y blanco
_MAX_PAGE_W = 1600               # tope de ancho del mapa de página (px)
_MAX_PAGE_H = 7000               # tope de alto


def _shots_dir(csv_path: str) -> str:
    from .session_meta import shots_dir_for
    return shots_dir_for(csv_path)


def _sin_hash(url) -> str:
    return str(url or "").split("#")[0]


# Fuentes del sistema con acentos (la de Pillow por defecto no los tiene)
_FUENTES = [
    os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", "segoeui.ttf"),
    os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", "arial.ttf"),
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _font(size: int):
    from PIL import ImageFont
    for p in _FUENTES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                         # Pillow antiguo
        return ImageFont.load_default()


def _ascii(s) -> str:
    """Texto para cv2.putText (sólo ASCII): quita acentos y comillas «»."""
    import unicodedata
    s = unicodedata.normalize("NFD", str(s or ""))
    return "".join(c for c in s if ord(c) < 128 and not unicodedata.combining(c))


def _precision_px(an: Analysis) -> Optional[float]:
    """Error estimado de la mirada de la sesión (px): el de los clics tras
    corregir si se corrigió; si no, el de la verificación."""
    sm = an.metrics.get("session", {})
    cc = sm.get("click_correction") or {}
    if cc.get("applied") and cc.get("err_after_px"):
        return float(cc["err_after_px"])
    v = sm.get("val_post_px") or sm.get("val_pre_px") or sm.get("cal_rms_px")
    return float(v) if v else None


def _with_header(img: np.ndarray, title: str, lines: List[str]) -> np.ndarray:
    lines = [ln for ln in lines if ln]
    """
    Añade ARRIBA de la imagen una franja blanca con título y explicación
    (sin tapar la página).  Usa Pillow para que se vean acentos y «».
    """
    w = img.shape[1]
    try:
        from PIL import Image, ImageDraw, ImageFont
        f_t, f_l = _font(22), _font(16)
        alto = 20 + 30 + 24 * len(lines) + 10
        band = Image.new("RGB", (w, alto), (255, 255, 255))
        d = ImageDraw.Draw(band)
        d.text((16, 14), title, fill=(20, 20, 20), font=f_t)
        for i, ln in enumerate(lines):
            d.text((16, 48 + 24 * i), ln, fill=(80, 80, 80), font=f_l)
        d.line([(0, alto - 1), (w, alto - 1)], fill=(200, 200, 200), width=2)
        band_bgr = cv2.cvtColor(np.asarray(band), cv2.COLOR_RGB2BGR)
    except Exception:
        alto = 30 + 22 * len(lines)
        band_bgr = np.full((alto, w, 3), 255, dtype=np.uint8)
        cv2.putText(band_bgr, title.encode("ascii", "ignore").decode(),
                    (12, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (20, 20, 20), 2)
        for i, ln in enumerate(lines):
            cv2.putText(band_bgr, ln.encode("ascii", "ignore").decode(),
                        (12, 46 + 22 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        (80, 80, 80), 1)
    return np.vstack([band_bgr, img])


class HeatmapGenerator:
    """
    Genera heatmaps de densidad de fijaciones y dwell time.

    Args:
        screen_w / screen_h: resolución de la pantalla en píxeles
        colormap:            cv2.COLORMAP_* (default: COLORMAP_JET)
        sigma_px:            radio del desenfoque Gaussiano en píxeles
        alpha:               transparencia del heatmap sobre el fondo (0–1)
    """

    def __init__(
        self,
        screen_w:  int   = 1920,
        screen_h:  int   = 1080,
        colormap:  int   = cv2.COLORMAP_JET,
        sigma_px:  float = 55.0,
        alpha:     float = 0.65,
    ) -> None:
        self.sw       = screen_w
        self.sh       = screen_h
        self.colormap = colormap
        self.sigma    = sigma_px
        self.alpha    = alpha

    # ════════════════════════════════════════════════════════════════════
    #  API original (a partir de un CSV)
    # ════════════════════════════════════════════════════════════════════

    def fixation_density(self, csv_path: str,
                         background: Optional[str] = None,
                         output: Optional[str] = None,
                         show_markers: bool = False) -> str:
        """Heatmap de densidad de fijaciones.  Devuelve la ruta del PNG."""
        fix = self._extract_fixations(self._load(csv_path))
        img = self.render_density(fix, (self.sw, self.sh),
                                  self._load_bg(background),
                                  weighted=False, markers=show_markers)
        self._draw_legend(
            img, f"DENSIDAD DE FIJACIONES  ·  {len(fix)} fijaciones",
            ["Cada fijacion suma 1 punto de calor",
             "Rojo/caliente = miro AHI muchas veces",
             "Azul/frio = apenas paso por esa zona"])
        out = output or self._out_path(csv_path, "density")
        cv2.imwrite(out, img)
        print(f"[HeatmapGenerator] Densidad de fijaciones -> {out}")
        return out

    def dwell_time(self, csv_path: str,
                   background: Optional[str] = None,
                   output: Optional[str] = None,
                   show_markers: bool = False) -> str:
        """Heatmap de dwell time (tiempo de permanencia acumulado)."""
        fix = self._extract_fixations(self._load(csv_path))
        img = self.render_density(fix, (self.sw, self.sh),
                                  self._load_bg(background),
                                  weighted=True, markers=show_markers)
        total_s = sum(f.dur_ms for f in fix) / 1000.0
        self._draw_legend(
            img, f"DWELL TIME  ·  {len(fix)} fijaciones  ·  {total_s:.1f} s",
            ["Cada fijacion pesa segun cuanto duro",
             "Rojo/caliente = miro AHI durante MAS tiempo",
             "El circulo blanco marca cada fijacion"])
        out = output or self._out_path(csv_path, "dwell")
        cv2.imwrite(out, img)
        print(f"[HeatmapGenerator] Dwell time -> {out}")
        return out

    def scanpath(self, csv_path: str,
                 background: Optional[str] = None,
                 output: Optional[str] = None,
                 max_fixations: int = 30) -> str:
        """Scanpath: fijaciones numeradas unidas por flechas."""
        fix = self._extract_fixations(self._load(csv_path))
        img = self.render_scanpath(fix[:max_fixations], (self.sw, self.sh),
                                   self._load_bg(background))
        self._draw_legend(
            img,
            f"SCANPATH  ·  {min(len(fix), max_fixations)} fijaciones  ·  "
            f"{os.path.basename(csv_path)}",
            ["El numero indica el ORDEN de la mirada (1 = primero)",
             "El tamano del circulo = cuanto tiempo fijo la vista ahi",
             "Las flechas unen fijaciones consecutivas (sacadas)"])
        out = output or self._out_path(csv_path, "scanpath")
        cv2.imwrite(out, img)
        print(f"[HeatmapGenerator] Scanpath -> {out}")
        return out

    def all_three(self, csv_path: str,
                  background: Optional[str] = None) -> dict:
        """Los tres tipos en un solo llamado."""
        return {
            "density":  self.fixation_density(csv_path, background,
                                              show_markers=True),
            "dwell":    self.dwell_time(csv_path, background,
                                        show_markers=True),
            "scanpath": self.scanpath(csv_path, background),
        }

    # ════════════════════════════════════════════════════════════════════
    #  Renderizado a partir de fijaciones
    # ════════════════════════════════════════════════════════════════════

    def render_density(self, fixes: Sequence[Fix], size: Tuple[int, int],
                       bg: Optional[np.ndarray] = None, weighted: bool = False,
                       markers: bool = False,
                       aois: Optional[List[Rect]] = None,
                       use_page: bool = False, scale: float = 1.0,
                       sigma: Optional[float] = None) -> np.ndarray:
        """
        Imagen de calor de una lista de fijaciones.

        Args:
            size:     (ancho, alto) del lienzo ya multiplicado por ``scale``.
            weighted: True = dwell time (peso = duración); False = densidad.
            use_page: usa las coordenadas de página (px, py) en vez de las de
                      pantalla; las fijaciones sin ellas se omiten.
            scale:    factor píxeles-del-mapa / píxeles-de-origen.
        """
        pts, _ = self._points(fixes, use_page)
        weights = ([f.dur_ms for f in fixes if self._has(f, use_page)]
                   if weighted else [1.0] * len(pts))
        return self.render_points(pts, weights, size, bg, markers=markers,
                                  marker_sizes=([f.dur_ms for f in fixes
                                                 if self._has(f, use_page)]
                                                if weighted else None),
                                  aois=aois, scale=scale, sigma=sigma)

    def render_points(self, pts: Sequence[Tuple[float, float]],
                      weights: Sequence[float], size: Tuple[int, int],
                      bg: Optional[np.ndarray] = None, markers: bool = False,
                      marker_sizes: Optional[Sequence[float]] = None,
                      aois: Optional[List[Rect]] = None, scale: float = 1.0,
                      sigma: Optional[float] = None) -> np.ndarray:
        """Heatmap genérico de puntos con peso (lo usa también el grupal)."""
        w, h = int(size[0]), int(size[1])
        spts = [(x * scale, y * scale) for x, y in pts]
        density = self._build_density_map(
            spts, list(weights), w, h,
            (sigma if sigma is not None else self.sigma) * scale)
        img = self._render(density, self._fit_bg(bg, w, h), w, h)
        if markers and spts:
            for i, (x, y) in enumerate(spts):
                r = 10
                if marker_sizes is not None:
                    r = max(8, min(35, int(marker_sizes[i] / 25)))
                cv2.circle(img, (int(x), int(y)), r, (255, 255, 255), 1)
                cv2.circle(img, (int(x), int(y)), 3, (255, 255, 255), -1)
        if aois:
            self.draw_aois(img, aois, scale)
        return img

    def render_scanpath(self, fixes: Sequence[Fix], size: Tuple[int, int],
                        bg: Optional[np.ndarray] = None,
                        aois: Optional[List[Rect]] = None,
                        use_page: bool = False, scale: float = 1.0,
                        labels: Optional[Dict[str, str]] = None
                        ) -> np.ndarray:
        """Scanpath numerado.  La fijación 1 se marca como "lo primero".
        ``labels`` = {id_aoi: nombre legible} para rotular cada fijación."""
        w, h = int(size[0]), int(size[1])
        fixes = [f for f in fixes if self._has(f, use_page)]
        canvas = self._fit_bg(bg, w, h)
        canvas = canvas.copy() if canvas is not None else \
            np.ones((h, w, 3), dtype=np.uint8) * 255
        if aois:
            self.draw_aois(canvas, aois, scale)

        colors = [(0, 180, 255), (0, 220, 100), (255, 100, 0)]
        xy = [((f.px, f.py) if use_page else (f.cx, f.cy)) for f in fixes]
        xy = [(int(x * scale), int(y * scale)) for x, y in xy]

        for i, fix in enumerate(fixes):
            cx, cy = xy[i]
            r = max(10, min(40, int(fix.dur_ms / 20)))
            color = colors[i % len(colors)]
            if i < len(fixes) - 1:
                largo = max(1.0, float(np.hypot(xy[i + 1][0] - cx,
                                                xy[i + 1][1] - cy)))
                cv2.arrowedLine(canvas, (cx, cy), xy[i + 1],
                                (90, 90, 90), 2, tipLength=min(0.3, 18.0 / largo))
            cv2.circle(canvas, (cx, cy), r, color, 2)
            cv2.circle(canvas, (cx, cy), 4, color, -1)
            aoi = fix.primary_aoi
            if aoi and labels:
                aoi = _ascii(labels.get(aoi, aoi))[:30]
            label = str(i + 1) + (f" {aoi}" if aoi else "")
            cv2.putText(canvas, label, (cx + r + 4, cy + 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (50, 50, 50), 2)

        if fixes:
            cx, cy = xy[0]
            cv2.circle(canvas, (cx, cy), 46, (0, 0, 220), 3)
            txt = "1 = LO PRIMERO QUE MIRO"
            (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
            # Mantener el texto dentro del lienzo
            tx = int(np.clip(cx - tw // 2, 8, max(8, w - tw - 8)))
            ty = int(np.clip(cy - 56, th + 10, max(th + 10, h - 10)))
            cv2.putText(canvas, txt, (tx, ty),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 220), 2)
        return canvas

    # ════════════════════════════════════════════════════════════════════
    #  Imágenes de una sesión (por tarea) y de un grupo
    # ════════════════════════════════════════════════════════════════════

    def session_images(self, an: Analysis, out_prefix: str,
                       backgrounds: Optional[Dict[int, str]] = None,
                       fallback_bg: Optional[str] = None) -> List[str]:
        """
        Genera las imágenes de una sesión analizada.

        Para cada tarea con fijaciones: densidad, dwell y scanpath.  Si la
        sesión tiene coordenadas de página (hubo puente web y scroll) además
        genera un mapa de calor sobre la PÁGINA completa.  Al final, un
        juego con la sesión completa.

        Args:
            backgrounds: ``{id_tarea: ruta_imagen}`` (captura de pantalla).
            fallback_bg: fondo para las tareas que no tengan uno propio.
        Returns:
            Lista de rutas de PNG generadas.
        """
        backgrounds = backgrounds or {}
        out: List[str] = []
        tasks = [t for t in an.metrics["tasks"] if t["n_fixations"] > 0]
        scope: List[Tuple[Optional[Dict], str, List[Fix]]] = []
        if an.metrics["has_tasks"]:
            for t in tasks:
                scope.append((t, f"_tarea{t['task']}",
                              an.fixes_for_task(t["task"])))
        scope.append((None, "", list(an.fixes)))     # sesión completa

        con_vistas = False
        for t, suffix, fixes in scope:
            if not fixes:
                continue
            tid = t["task"] if t else None
            # Con capturas del navegador: imágenes sobre lo que vio (más
            # claras que las de pantalla sobre fondo blanco).
            if t is not None:
                vistas = self.page_views(an, tid, fixes, out_prefix,
                                         f"Tarea {tid}: {t['title']}")
                if vistas:
                    out += vistas
                    con_vistas = True
                    continue
            elif con_vistas:
                continue
            bg_path = backgrounds.get(tid) if tid is not None else None
            bg = self._load_bg(bg_path or fallback_bg)
            title = (f"{t['title']}" if t else "SESION COMPLETA")
            aois = self.screen_aois(an, tid)

            dens = self.render_density(fixes, (self.sw, self.sh), bg,
                                       weighted=False, markers=True, aois=aois)
            self._draw_legend(
                dens, f"DENSIDAD  ·  {title}  ·  {len(fixes)} fijaciones",
                ["Cada fijacion suma 1 punto de calor",
                 "Rojo = miro AHI muchas veces",
                 "Azul = apenas paso por esa zona"])
            out.append(self._save(dens, f"{out_prefix}{suffix}_density.png"))

            dw = self.render_density(fixes, (self.sw, self.sh), bg,
                                     weighted=True, markers=True, aois=aois)
            tot = sum(f.dur_ms for f in fixes) / 1000.0
            self._draw_legend(
                dw, f"DWELL TIME  ·  {title}  ·  {tot:.1f} s",
                ["Cada fijacion pesa segun cuanto duro",
                 "Rojo = miro AHI durante MAS tiempo",
                 "El circulo blanco marca cada fijacion"])
            out.append(self._save(dw, f"{out_prefix}{suffix}_dwell.png"))

            sp = self.render_scanpath(fixes[:30], (self.sw, self.sh), bg,
                                      aois=aois)
            self._draw_legend(
                sp, f"SCANPATH  ·  {title}",
                ["El numero indica el ORDEN de la mirada (1 = primero)",
                 "El tamano del circulo = cuanto tiempo fijo la vista ahi",
                 "Junto al numero va el AOI en el que cayo (si lo hay)"])
            out.append(self._save(sp, f"{out_prefix}{suffix}_scanpath.png"))

            page_img = self.page_image(an, fixes, tid, weighted=True)
            if page_img is not None:
                self._draw_legend(
                    page_img, f"DWELL SOBRE LA PAGINA  ·  {title}",
                    ["Coordenadas de la PAGINA (incluye lo que"
                     " se vio al hacer scroll)"])
                out.append(self._save(
                    page_img, f"{out_prefix}{suffix}_pagina.png"))
        return out

    # ════════════════════════════════════════════════════════════════════
    #  Sobre lo que VIO el participante (capturas del navegador integrado)
    # ════════════════════════════════════════════════════════════════════

    def page_views(self, an: Analysis, task_id: int, fixes: Sequence[Fix],
                   out_prefix: str, title: str, max_views: int = 3
                   ) -> List[str]:
        """
        Heatmap y recorrido de la mirada sobre la PÁGINA tal como la vio el
        participante: se pegan las capturas del navegador integrado en su
        posición de scroll (la página completa, aunque haya bajado) y encima
        se pintan las fijaciones en coordenadas de página.

        Una pareja de imágenes por página visitada en la tarea (las
        ``max_views`` con más tiempo de mirada):
          ``…_tareaN_vistaK_calor.png``     dónde y cuánto miró
          ``…_tareaN_vistaK_recorrido.png`` en qué orden
        Devuelve [] si la sesión no tiene capturas de esa tarea.
        """
        shots_dir = _shots_dir(an.session.csv_path)
        shots = [e for e in an.session.web
                 if e.get("k") == "shot" and int(e.get("task") or 0) == task_id
                 and os.path.exists(os.path.join(shots_dir, str(e.get("file"))))]
        if not shots:
            return []
        por_url: Dict[str, List[dict]] = {}
        for e in shots:
            por_url.setdefault(_sin_hash(e.get("url")), []).append(e)

        pf = [f for f in fixes if f.px is not None and f.py is not None]
        fx_url: Dict[str, List[Fix]] = {}
        for f in pf:
            fx_url.setdefault(_sin_hash(an.resolver.url_at(f.start_ms)),
                              []).append(f)
        urls = sorted(por_url, key=lambda u: -sum(f.dur_ms
                                                   for f in fx_url.get(u, [])))
        err = _precision_px(an)
        nota_err = ("" if err is None else
                    f"Precisión de la mirada: ±{err:.0f} px"
                    + (" — BAJA: lee las zonas, no elementos pequeños."
                       if err > 200 else "."))
        out: List[str] = []
        for k, url in enumerate(urls[:max_views], start=1):
            fx = fx_url.get(url, [])
            if not fx and len(urls) > 1:
                continue
            bg, s = self.stitch(por_url[url], shots_dir, fx)
            h, w = bg.shape[:2]
            ruta = url.split("://", 1)[-1][:70]
            secs = sum(f.dur_ms for f in fx) / 1000.0

            heat = self.render_density(fx, (w, h), bg, weighted=True,
                                       markers=True, use_page=True, scale=s)
            heat = _with_header(heat, f"{title} · Mapa de calor",
                                [f"Página: {ruta}",
                                 f"{len(fx)} fijaciones · {secs:.1f} s mirando. "
                                 "Rojo = donde más tiempo miró; sin color = "
                                 "casi no miró.", nota_err])
            out.append(self._save(heat, f"{out_prefix}_tarea{task_id}"
                                        f"_vista{k}_calor.png"))

            labels = ({a.id: a.label for a in an.study.aois}
                      if an.study else None)
            path = self.render_scanpath(fx[:40], (w, h), bg, use_page=True,
                                        scale=s, labels=labels)
            path = _with_header(path, f"{title} · Recorrido de la mirada",
                                [f"Página: {ruta}",
                                 "El número es el ORDEN (1 = lo primero que "
                                 "miró); el círculo crece con el tiempo que "
                                 "se quedó ahí.", nota_err])
            out.append(self._save(path, f"{out_prefix}_tarea{task_id}"
                                        f"_vista{k}_recorrido.png"))
        return out

    def group_page_views(self, analyses: Sequence[Analysis], task_id: int,
                         out_dir: str, title: str, max_views: int = 3
                         ) -> List[str]:
        """
        Mapa de calor de TODO el grupo sobre la página real: de fondo, la
        página reconstruida del participante con más capturas de esa vista;
        encima, las fijaciones de todos (cada participante pesa lo mismo).
        """
        mejor: Dict[str, Tuple[str, List[dict]]] = {}
        fx: Dict[str, List[List[Fix]]] = {}
        for an in analyses:
            sd = _shots_dir(an.session.csv_path)
            por_url: Dict[str, List[dict]] = {}
            for e in an.session.web:
                if (e.get("k") == "shot" and int(e.get("task") or 0) == task_id
                        and os.path.exists(os.path.join(sd, str(e.get("file"))))):
                    por_url.setdefault(_sin_hash(e.get("url")), []).append(e)
            for url, lst in por_url.items():
                if len(lst) > len(mejor.get(url, ("", []))[1]):
                    mejor[url] = (sd, lst)
            mias: Dict[str, List[Fix]] = {}
            for f in an.fixes_for_task(task_id):
                if f.px is not None and f.py is not None:
                    mias.setdefault(_sin_hash(an.resolver.url_at(f.start_ms)),
                                    []).append(f)
            for url, lst in mias.items():
                fx.setdefault(url, []).append(lst)
        urls = sorted((u for u in mejor if u in fx),
                      key=lambda u: -sum(f.dur_ms for p in fx[u] for f in p))
        out: List[str] = []
        for k, url in enumerate(urls[:max_views], start=1):
            sd, shots = mejor[url]
            todas = [f for p in fx[url] for f in p]
            bg, s = self.stitch(shots, sd, todas)
            h, w = bg.shape[:2]
            img = self.render_group(fx[url], (w, h), bg, weighted=True,
                                    use_page=True, scale=s)
            img = _with_header(
                img, f"{title} · Mapa de calor del GRUPO (n={len(fx[url])})",
                [f"Página: {url.split('://', 1)[-1][:70]}",
                 "Cada participante pesa lo mismo. Rojo = donde más miró el "
                 "grupo. Fondo: la página de un participante."])
            out.append(self._save(img, os.path.join(
                out_dir, f"heatmap_tarea{task_id}_vista{k}_grupo.png")))
        return out

    @staticmethod
    def stitch(shots: Sequence[dict], shots_dir: str,
               fixes: Sequence[Fix] = ()) -> Tuple[np.ndarray, float]:
        """
        Reconstruye la página pegando cada captura en su posición de scroll.
        Devuelve (imagen, escala) con escala = px de la imagen / px CSS.
        Las zonas que nunca se vieron quedan en gris.
        """
        W = max([float(e.get("sx") or 0) + float(e.get("iw") or 0)
                 for e in shots] + [f.px or 0 for f in fixes] + [10.0])
        H = max([float(e.get("sy") or 0) + float(e.get("ih") or 0)
                 for e in shots] + [f.py or 0 for f in fixes] + [10.0])
        s = min(1.0, _MAX_PAGE_W / W, _MAX_PAGE_H / H)
        cw, ch = max(10, int(W * s)), max(10, int(H * s))
        canvas = np.full((ch, cw, 3), 225, dtype=np.uint8)
        # Primero las vistas «normales» y encima las que tienen un menú o
        # resultados abiertos (más AOI visibles): así el menú de cuadrícula
        # no queda tapado por una captura posterior con el menú cerrado.
        for e in sorted(shots, key=lambda e: (int(e.get("n_vis") or 0),
                                              e.get("t_ms", 0))):
            img = cv2.imread(os.path.join(shots_dir, str(e.get("file"))))
            if img is None:
                continue
            iw, ih = float(e.get("iw") or 0), float(e.get("ih") or 0)
            if iw <= 0 or ih <= 0:
                continue
            x0 = int(float(e.get("sx") or 0) * s)
            y0 = int(float(e.get("sy") or 0) * s)
            tw, th = max(1, int(iw * s)), max(1, int(ih * s))
            img = cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)
            tw, th = min(tw, cw - x0), min(th, ch - y0)
            if tw > 0 and th > 0:
                canvas[y0:y0 + th, x0:x0 + tw] = img[:th, :tw]
        return canvas, s

    def page_image(self, an: Analysis, fixes: Sequence[Fix],
                   task_id: Optional[int], weighted: bool = True
                   ) -> Optional[np.ndarray]:
        """
        Heatmap sobre la página completa (coordenadas px, py).

        Devuelve None si no hay fijaciones con coordenadas de página o si
        la página cabe en una pantalla sin scroll (el mapa normal ya basta).
        """
        pf = [f for f in fixes if f.px is not None and f.py is not None]
        if not pf:
            return None
        pw, ph = self._page_size(an)
        # sin scroll real no aporta nada sobre el mapa de pantalla
        if ph <= an.session.screen_h * 1.05 and \
                max(f.py for f in pf) <= an.session.screen_h:
            return None
        s = min(1.0, _MAX_PAGE_W / max(pw, 1), _MAX_PAGE_H / max(ph, 1))
        size = (max(10, int(pw * s)), max(10, int(ph * s)))
        bg = None
        if an.study and task_id:
            t = an.study.task(task_id)
            p = an.study.resolve_path(t.page_screenshot) if t else None
            bg = self._load_raw(p)
        aois = self.page_aois(an)
        return self.render_density(pf, size, bg, weighted=weighted,
                                   markers=False, aois=aois, use_page=True,
                                   scale=s)

    def render_group(self, per_participant: Sequence[Sequence[Fix]],
                     size: Tuple[int, int], bg: Optional[np.ndarray] = None,
                     weighted: bool = True, use_page: bool = False,
                     scale: float = 1.0, aois: Optional[List[Rect]] = None,
                     sigma: Optional[float] = None) -> np.ndarray:
        """
        Heatmap de VARIOS participantes.

        Cada participante aporta peso total 1 (se normaliza), de modo que
        quien miró más tiempo no domina el mapa: el resultado responde a
        "¿dónde mira el participante típico?".
        """
        pts: List[Tuple[float, float]] = []
        wts: List[float] = []
        for fixes in per_participant:
            fx = [f for f in fixes if self._has(f, use_page)]
            if not fx:
                continue
            raw = [(f.dur_ms if weighted else 1.0) for f in fx]
            tot = sum(raw) or 1.0
            for f, r in zip(fx, raw):
                pts.append((f.px, f.py) if use_page else (f.cx, f.cy))
                wts.append(r / tot)
        return self.render_points(pts, wts, size, bg, markers=False,
                                  aois=aois, scale=scale, sigma=sigma)

    # ── AOI para dibujar ────────────────────────────────────────────────

    def screen_aois(self, an: Analysis, task_id: Optional[int]) -> List[Rect]:
        """Rectángulos de pantalla de los AOI "viewport" de una tarea."""
        if an.study is None:
            return []
        lst = (an.study.aois_for_task(task_id) if task_id is not None
               else an.study.aois)
        out: List[Rect] = []
        for a in lst:
            r = a.screen_rect(an.session.screen_w, an.session.screen_h,
                              an.study.ref_w, an.study.ref_h)
            if r:
                out.append((a.label, *r))
        return out

    def page_aois(self, an: Analysis) -> List[Rect]:
        """Rectángulos de página de los AOI (manuales y medidos por selector)."""
        if an.study is None:
            return []
        out: List[Rect] = []
        for a in an.study.aois:
            if a.rect is not None and a.space == "page":
                x, y, w, h = a.rect
                out.append((a.label, x, y, x + w, y + h))
            elif a.selector or a.text or a.opens:
                best = None
                for _ts, snapshots in an.resolver._layouts.values():  # noqa: SLF001
                    for m in snapshots:
                        if a.id in m and m[a.id].get("vis", True) \
                                and not m[a.id].get("fixed"):
                            best = m[a.id]                   # la más reciente
                if best:
                    out.append((a.label, best["x"], best["y"],
                                best["x"] + best["w"], best["y"] + best["h"]))
        return out

    @staticmethod
    def _page_size(an: Analysis) -> Tuple[float, float]:
        pw = ph = 0.0
        for e in an.session.web:
            if e.get("k") in ("vp", "layout"):
                pw = max(pw, float(e.get("pw") or 0))
                ph = max(ph, float(e.get("ph") or 0))
        if pw <= 0:
            pw = float(an.session.screen_w)
        if ph <= 0:
            ph = float(an.session.screen_h)
        return pw, ph

    @staticmethod
    def draw_aois(img: np.ndarray, rects: List[Rect], scale: float = 1.0
                  ) -> None:
        """Dibuja el contorno y el nombre de cada AOI."""
        h, w = img.shape[:2]
        for label, x0, y0, x1, y1 in rects:
            p0 = (int(x0 * scale), int(y0 * scale))
            p1 = (int(x1 * scale), int(y1 * scale))
            cv2.rectangle(img, p0, p1, _AOI_COLOR, 2)
            ty = p0[1] - 6 if p0[1] > 20 else p0[1] + 18
            tx = int(np.clip(p0[0] + 4, 2, max(2, w - 150)))
            cv2.putText(img, str(label)[:28], (tx, int(np.clip(ty, 12, h - 4))),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 3)
            cv2.putText(img, str(label)[:28], (tx, int(np.clip(ty, 12, h - 4))),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, _AOI_COLOR, 1)

    # ════════════════════════════════════════════════════════════════════
    #  Leyenda
    # ════════════════════════════════════════════════════════════════════

    @staticmethod
    def _draw_legend(canvas: np.ndarray, title: str,
                     lines: List[str]) -> None:
        """
        Dibuja un recuadro blanco con el título y la explicación.

        Se pinta sobre fondo blanco con texto oscuro para que sea legible
        tanto si el fondo del heatmap es blanco como si es una captura de
        pantalla oscura.
        """
        pad   = 12
        hline = 24
        alto  = pad * 2 + hline * (len(lines) + 1)
        ancho = 560
        x0, y0 = 14, 14
        H, W = canvas.shape[:2]
        ancho = min(ancho, W - 2 * x0)
        if ancho < 120 or alto > H - y0:
            return

        cv2.rectangle(canvas, (x0, y0), (x0 + ancho, y0 + alto),
                      (255, 255, 255), -1)
        cv2.rectangle(canvas, (x0, y0), (x0 + ancho, y0 + alto),
                      (60, 60, 60), 2)
        cv2.putText(canvas, title[:64], (x0 + pad, y0 + pad + 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.52, (20, 20, 20), 2)
        for k, ln in enumerate(lines):
            cv2.putText(canvas, ln, (x0 + pad, y0 + pad + hline * (k + 1) + 15),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.44, (70, 70, 70), 1)

    # ════════════════════════════════════════════════════════════════════
    #  Internos
    # ════════════════════════════════════════════════════════════════════

    def _load(self, csv_path: str) -> List[dict]:
        """Filas del CSV (compatibilidad con la API antigua)."""
        return read_rows(csv_path)

    def _extract_fixations(self, rows: List[dict]) -> List[Fix]:
        """Fijaciones de una lista de filas (compatibilidad)."""
        return extract_fixations(rows, MIN_FIXATION_MS)

    @staticmethod
    def _has(f: Fix, use_page: bool) -> bool:
        return (f.px is not None and f.py is not None) if use_page else True

    @staticmethod
    def _points(fixes: Sequence[Fix], use_page: bool
                ) -> Tuple[List[Tuple[float, float]], int]:
        pts = [((f.px, f.py) if use_page else (f.cx, f.cy))
               for f in fixes if HeatmapGenerator._has(f, use_page)]
        return pts, len(pts)

    @staticmethod
    def _build_density_map(pts: Sequence[Tuple[float, float]],
                           weights: Sequence[float], w: int, h: int,
                           sigma: float) -> np.ndarray:
        """Construye y suaviza el mapa de densidad 2D."""
        density = np.zeros((h, w), dtype=np.float32)
        for (px, py), wt in zip(pts, weights):
            xi = int(np.clip(px, 0, w - 1))
            yi = int(np.clip(py, 0, h - 1))
            density[yi, xi] += float(wt)
        density = gaussian_filter(density, sigma=max(sigma, 1.0))

        # Normalizar a [0, 1] con un percentil alto en vez del máximo: si se
        # normaliza por el máximo, un único punto muy caliente aplasta toda
        # la escala y el resto del mapa queda uniformemente azul.
        nz = density[density > 0]
        if nz.size:
            ref = float(np.percentile(nz, 99.0))
            if ref <= 0:
                ref = float(nz.max())
            density = np.clip(density / ref, 0.0, 1.0)
        return density

    def _render(self, density: np.ndarray, bg: Optional[np.ndarray],
                w: int, h: int) -> np.ndarray:
        """Aplica colormap y blend sobre el fondo."""
        density_u8 = (density * 255).astype(np.uint8)
        colored = cv2.applyColorMap(density_u8, self.colormap)
        if bg is None:
            bg = np.ones((h, w, 3), dtype=np.uint8) * 255

        # Máscara: no mezclar zonas con densidad baja.  El desenfoque
        # gaussiano reparte un poco de densidad por toda la pantalla, y con
        # un umbral bajo el heatmap se convierte en una mancha que no
        # comunica nada.
        mask = (density > 0.20).astype(np.float32)
        mask = np.stack([mask, mask, mask], axis=-1)

        result = bg.copy().astype(np.float32)
        alpha = self.alpha * mask
        result = result * (1. - alpha) + colored.astype(np.float32) * alpha
        return result.clip(0, 255).astype(np.uint8)

    @staticmethod
    def _load_raw(path: Optional[str]) -> Optional[np.ndarray]:
        if path and os.path.exists(path):
            return cv2.imread(path)
        return None

    def _load_bg(self, path: Optional[str]) -> Optional[np.ndarray]:
        img = self._load_raw(path)
        if img is not None:
            return cv2.resize(img, (self.sw, self.sh))
        return None

    @staticmethod
    def _fit_bg(bg: Optional[np.ndarray], w: int, h: int
                ) -> Optional[np.ndarray]:
        if bg is None:
            return None
        if bg.shape[1] == w and bg.shape[0] == h:
            return bg
        return cv2.resize(bg, (w, h))

    @staticmethod
    def _save(img: np.ndarray, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        cv2.imwrite(path, img)
        print(f"[HeatmapGenerator] -> {path}")
        return path

    @staticmethod
    def _out_path(csv_path: str, suffix: str) -> str:
        base = os.path.splitext(csv_path)[0]
        return f"{base}_{suffix}.png"
