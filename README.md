# 👁️ Eye-Tracking Suite v3.0

Sistema de eye-tracking por webcam para analizar **qué mira una persona y en
qué orden** al ver una página. Funciona con cualquier cámara, sin hardware
especial.

**Novedad v3.0 — mirada invariante a la cabeza.** Antes, mover la cabeza
aunque fuera un poco rompía la estimación. Ahora el modelo es geométrico 3D:
medido sobre el simulador cabeza-ojo-pantalla, girar la cabeza ±16° desviaba
la mirada **754 px**; ahora **51 px**. Y el error es el mismo con la cabeza
quieta o moviéndose, que es justo lo que se busca.

---

## 🧠 Cómo funciona la mirada (v3.0)

El problema de fondo: medir el iris **en píxeles de la imagen** mezcla dos
cosas distintas — hacia dónde mira el ojo y hacia dónde apunta la cabeza. Por
eso la calibración sólo valía mientras no te movieras.

La solución es pasar todo a **geometría 3D**:

```
1. solvePnP (14 puntos)  →  R (orientación de la cabeza)  y  t (posición)
2. Globo ocular          →  esfera de 12 mm centrada en un punto conocido
                            del modelo facial, colocado con R y t
3. Rayo del iris         →  sale de la cámara por el píxel del iris y corta
                            esa esfera; el corte más cercano es el iris
4. Dirección de mirada   →  vector centro→iris, normalizado, en coordenadas
                            de CÁMARA
5. Calibración           →  se corta ese rayo con el plano de la pantalla y
                            se ajusta una transformación afín a píxeles
```

El paso 4 es la clave: la dirección sale en coordenadas de cámara, así que
**girar la cabeza no la cambia** mientras se siga mirando al mismo punto. Y
como el paso 5 usa la posición 3D del ojo, **acercarse o alejarse tampoco**.

### El plano de la pantalla

La calibración sólo tiene que estimar una cosa: dónde está el plano de la
pantalla (`Z`). Con `Z` fijo el ajuste es **lineal**, así que se barre una
rejilla de distancias y se resuelve un sistema lineal por cada una — barato y
sin mínimos locales.

Con la cabeza quieta, `Z` **no es identificable**: cualquier `Z` da un ajuste
afín exacto (el punto de corte del rayo y el punto real difieren en una
transformación afín). Por eso se elige la distancia **más cercana a 0** entre
las que dan un RMS prácticamente idéntico. Es la geometría real de una webcam
de portátil — la pantalla está en el plano del objetivo — y equivocarse en
±50 mm cuesta menos de 20 px.

---

## 🚀 Arranque rápido (Windows)

```powershell
cd C:\VSCode\EyeTrackingCosa
.\run.bat
```

`run.bat` busca un Python que tenga `tkinter` **y** las dependencias. Es
necesario porque el `python` que está primero en el `PATH` de esta máquina
**no tiene `tkinter`** y la aplicación moría al arrancar sin decir por qué.

Si prefieres lanzarlo a mano:

```powershell
C:\Python313\python.exe main_app.py          # el que tiene todo instalado
C:\Python313\python.exe main_app.py --camera 1        # forzar cámara 1
C:\Python313\python.exe main_app.py --quality high    # intentar 1280×720
```

---

## 📋 Requisitos

* **Python 3.11 – 3.13 con `tcl/tk`** (marcar *"tcl/tk and IDLE"* en el
  instalador de python.org).
* Una webcam cualquiera (integrada, USB, o virtual tipo OBS / DroidCam).
* Windows 10/11, macOS o Linux.

```powershell
python -m pip install -r requirements.txt
```

Dependencias reales: `opencv-python`, `mediapipe`, `numpy`, `scipy`,
`Pillow`, `screeninfo`, `pywin32` (sólo Windows, para el overlay).

---

## 🖥️ Guía de uso

1. **Cámara.** Al abrir, la app busca sola una cámara que entregue imagen.
   La línea *Cámara:* muestra cuál eligió y a cuántos FPS reales.
   * `🔍 Detectar cámaras` lista todas y puedes cambiar en caliente.
   * `🎥 Debug` abre la vista de la cámara con iris, ejes de cabeza y
     contadores en vivo. **Si algo va mal, mira aquí primero.**

2. **Calibración.** Elige 9 puntos (recomendado) y pulsa `🎯 Calibrar Ahora`.
   * **Amarillo pulsante** → lleva la mirada al punto.
   * **Verde fijo** → se están capturando muestras.
   * **No hace falta quedarte rígido.** El modelo es invariante a la cabeza;
     puedes sentarte con naturalidad. De hecho, moverte un poco durante la
     calibración ayuda: permite estimar la distancia a la pantalla.
   * Al terminar te dice el **error medio en píxeles**. Por debajo de 250 px
     es buena. Se guarda sola en `calibracion.json`.

3. **Sesión.** Escribe el ID del participante y pulsa `▶ Iniciar`.
   * Aparece un círculo verde siguiendo tu mirada.
   * **El círculo deja pasar los clics**: puedes usar Chrome, Word o lo que
     sea con total normalidad mientras graba. (Antes no: el overlay se comía
     todos los clics y era imposible navegar.)
   * `⬛ Detener` guarda el CSV en `sessions/`.

4. **Análisis.**
   * `🗺 Heatmaps` → elige el tipo con los botones de radio y pulsa el botón.
     - **No hace falta elegir ningún CSV**: la app usa sola la sesión que
       acabas de grabar, o la más reciente de `sessions/`. Sólo pregunta si
       todavía no hay ninguna.
     - Después pregunta por un **fondo opcional**: si le das una captura de
       la página que estabas mirando, el heatmap se pinta encima y se
       interpreta mucho mejor. Cancelar = fondo blanco.
     - Genera hasta 3 PNG **junto al CSV** de la sesión y abre la carpeta:
       `..._density.png`, `..._dwell.png`, `..._scanpath.png`.
     - Ver la sección [Los heatmaps explicados](#-los-heatmaps-explicados).
   * `📋 Informe: ¿qué miró primero?` → abre una ventana con:
     - **Orden de fijación**: las primeras N fijaciones con instante,
       posición, duración y zona de pantalla.
     - **TTFF**: cuánto tardó en fijar la mirada.
     - **Mapa de atención 3×3** con porcentajes y ranking de zonas.
     - Métricas: fijaciones, sacadas, duración media.
     Se guarda como `*_informe.txt` junto al CSV.

5. **Página de prueba.** `🌐 Página de prueba` abre `pagina_prueba.html`,
   una página con zonas bien diferenciadas (título, bloque morado, tarjetas,
   botón) pensada para que el heatmap y el informe sean interpretables.

---

## 🔧 Qué se arregló (y por qué no funcionaba)

El síntoma era "la app abre pero no hace nada / la mirada salta por toda la
pantalla". Estas eran las causas, en orden de gravedad:

### 1. El modelo 3D de `solvePnP` tenía el eje Y invertido
Con la cara de frente, la orientación de cabeza salía con **pitch ≈ 170°**
(debería ser ≈ 0°). El vector de mirada se construía como
`gx = iris_dx + 0.65·yaw`, `gy = iris_dy − 0.55·pitch`, así que **toda la
señal de mirada era basura de pose, no mirada**. Corregido el signo de Y y
cambiado a `SOLVEPNP_SQPNP`: pitch pasa a ~14° ± 1° con la cara de frente.

### 2. El frame se espejaba antes de detectar los landmarks
`cv2.flip(raw, 1)` intercambia los landmarks izquierda/derecha, lo que rompe
la correspondencia con el modelo 3D y cambia el signo del yaw. Ahora se
detecta sobre el frame **real**; el espejo se aplica sólo a la
previsualización de debug, para que te veas natural.

### 3. `cv2.VideoCapture(0)` y ya
Si ese índice no existía, estaba ocupado o el backend fallaba, el hilo moría
**en silencio** y la app parecía colgada. Ahora:
* Prueba backends (DSHOW / MSMF / AVFoundation / V4L2).
* Prueba índices y descarta cámaras que no entregan imagen real (negras o
  planas, típico de cámaras virtuales sin señal).
* **Mide los FPS reales** y baja la resolución si no alcanzan el mínimo.
* Si la cámara se cae, reintenta sola. Si no hay ninguna, lo dice en la UI.

### 4. Se forzaba 1280×720 aunque la cámara no pudiera
Medido en esta máquina: la webcam da **15 fps a 640×480 pero sólo 5 fps a
1280×720** (y declara `YUY2`, ignora el MJPG que se le pedía). A 5 fps la
detección de fijaciones no funciona: dos muestras quedan separadas 200 ms.
Ahora se mide y se negocia la resolución automáticamente.

### 5. El clasificador de fijaciones era inútil con webcam
I-VT con umbral 80 px/s sobre 15 fps y ruido de ~60 px etiqueta **casi todo
como sacada**. En tu sesión anterior: **772 sacadas y 8 fijaciones** de 781
muestras. Con señal sintética de 6 fijaciones conocidas, I-VT localiza
**0/6**. Sustituido por **I-DT con métrica de desviación estándar**: localiza
**6/6**.

### 6. La vista previa saturaba en las esquinas
`px = centro + gx · ancho · 3.2` con recorte `clip()` mandaba el punto a la
esquina y ahí se quedaba (se veía `gaze=(1916, 0)`). Ahora usa `tanh`, que
nunca satura.

### 7. El overlay se comía los clics
Una ventana a pantalla completa *always-on-top* bloquea el ratón. Se añadió
`WS_EX_TRANSPARENT` (+ `LAYERED`, `TOOLWINDOW`, `NOACTIVATE`), verificado por
test. Ahora puedes usar el navegador mientras graba.

### 8. La calibración no daba ninguna señal de calidad
Ahora rechaza outliers por punto (iris + pose, criterio MAD×3), estandariza
las columnas antes de Ridge, informa el error RMS y se puede guardar/cargar.
Validado end-to-end: **error de predicción de 10 px** sobre 1920 px.

### 9. El modelo L2CS-Net no existe
El README prometía un modelo ONNX de Hugging Face que **no se puede
descargar** (`RepositoryNotFoundError` en `ashwinraj97/L2CS-Net`). Se eliminó
la promesa y el código que dependía de él.

### 10. La mirada se rompía al mover la cabeza ← *el problema grande*
El cálculo del iris era **puramente 2D y en el sistema de la cabeza**: se
medía el desplazamiento del iris respecto a las esquinas del ojo, que son
solidarias al cráneo. Mientras no te movieras, la regresión polinómica
compensaba; en cuanto giradas la cabeza, esas features cambiaban aunque
siguieras mirando al mismo punto y **la predicción se iba a cualquier parte**.

Medido sobre el simulador `synth_gaze.py` (cabeza 3D + ojos esféricos +
pantalla, con verdad conocida):

| Movimiento | Antes | Ahora | Mejora |
| :--- | ---: | ---: | ---: |
| Cabeza quieta | 54 px | 51 px | 1× |
| Giro leve ±8° | 509 px | 51 px | 10× |
| Giro medio ±16° | 754 px | 51 px | 15× |
| Lateral ±60 mm | 1118 px | 52 px | 22× |
| Acercarse/alejarse ±80 mm | 240 px | 51 px | 5× |
| Arriba/abajo ±40 mm | 557 px | 52 px | 11× |
| Movimiento combinado | 1138 px | 53 px | 22× |

Lo importante no es sólo el factor: es que **el error ya no depende del
movimiento**. Antes crecía sin límite al moverte; ahora es una constante de
~51 px, que es el error de calibración, no de postura.

Además se encontraron dos bugs que impedían que esto funcionara:

* **La descomposición Euler tenía una singularidad justo en la postura
  normal.** Con la cara mirando de frente al objetivo, `R ≈ Ry(180°)` y el
  pitch salía **180°** en vez de 0°. Ahora se detecta esa zona y se descompone
  respecto a la postura "de frente".
* **La pose podía caer en la rama espejada** (la cara "mirando hacia afuera")
  porque el modelo facial es casi plano y `solvePnP` tiene ahí una ambigüedad
  real. Se añadió el **error de reproyección** como control de calidad de la
  pose, que no depende de ninguna convención de ángulos.

### 11. El ruido de landmarks se amplificaba ×12
La reconstrucción 3D es sensible al ruido: el globo ocular mide 12 mm y un
píxel a 550 mm son ~0,9 mm, o sea ~4° de ángulo. Dos medidas lo resolvieron:

* **Filtro 1€ sobre el vector geométrico** (6 componentes), con los
  coeficientes `beta` escalados a cada señal. Reduce el ruido ~4× y se abre
  solo durante las sacadas para no añadir lag.
* **Ruido correlacionado.** Medido en vivo con `_t_noise.py`, el ruido de
  MediaPipe es mayormente **global** (toda la cara se desplaza junta,
  1,1 px/frame), no independiente. Ese componente **se cancela** al restar
  iris − centro del ojo. Sólo molesta el independiente (1,05 px), y contra ese
  está el filtro.

Resultado medido en vivo: el ruido de la mirada baja de **151 px** (método
antiguo) a **31 px** (geométrico) y **8 px** con filtro.

---

### 12. El grabador perdía la mitad de las muestras ← *por qué los heatmaps salían mal*

Este fue el último bug y el más silencioso. Todos los consumidores —la GUI,
el calibrador y el grabador— leían la **misma** cola:

```python
def get_latest(self):
    while True:
        try:
            latest = self.sample_queue.get_nowait()   # ← VACÍA la cola
        except queue.Empty:
            break
    return latest
```

Como `get_latest()` vaciaba la cola, **el que llegaba primero se llevaba la
muestra**. La GUI refresca a 60 Hz, así que le robaba datos al grabador.

Medido sobre un CSV real: la cámara daba 15 Hz, el tracker reportaba 15 Hz,
pero el CSV sólo tenía **7,3 Hz**, con huecos de hasta **736 ms**. A esa
frecuencia la detección de fijaciones no puede funcionar, y el scanpath sale
como una maraña de líneas.

**Arreglo.** El tracker ahora mantiene un histórico circular que se lee sin
consumir:

* `get_latest()` → devuelve el último sample. **No destruye nada**, así que la
  GUI puede consultar a 60 Hz sin robarle datos a nadie.
* `get_since(frame_id)` → devuelve *todas* las muestras nuevas desde un
  cursor. Lo usa el grabador, que ya no pierde ninguna aunque su bucle se
  retrase.

Medido después del arreglo, con la GUI consultando 706 veces durante la
grabación: **0,0 % de muestras perdidas** y **30,1 Hz** efectivos.

---

### 13. El archivo de calibración degradaba el modelo en silencio

`load()` hacía esto:

```python
self.method = d.get("method", self.method)
```

Es decir, **el archivo guardado decidía qué modelo usaba la aplicación**. Si
había un `calibracion.json` ajustado con el modelo polinómico antiguo, al
abrirlo la app bajaba a ese modelo — sin invariancia a la cabeza — y no lo
decía. El usuario creía estar usando el modelo nuevo y estaba usando el viejo.

Los parámetros guardados sólo son válidos para el modelo con el que se
ajustaron, así que ahora `load()` **rechaza** el archivo si el método no
coincide y pide recalibrar. Hay una comprobación en `validacion.py` que lo
verifica (`una calibración de OTRO modelo se rechaza`).

> **Consecuencia práctica:** si tenías una calibración vieja, al abrir la app
> verás *SIN CALIBRAR*. Es lo correcto — pulsa `🎯 Calibrar Ahora` una vez y
> ya quedas en el modelo geométrico.

---

## 🗺️ Los heatmaps explicados

### El recorrido completo

```
cámara → 478 landmarks → mirada (x, y) en pantalla → CSV
                                                      │
                                          (al pulsar "Heatmaps")
                                                      ↓
                          se agrupan las muestras en FIJACIONES
                                                      ↓
                              se pinta un mapa y se le aplica color
                                                      ↓
                                     3 PNG junto al CSV de la sesión
```

### Paso 1 · De muestras a fijaciones

El CSV tiene una fila por frame (30 por segundo). Pero el ojo no lee así: hace
**fijaciones** (se queda quieto 200–600 ms mirando algo) separadas por
**sacadas** (saltos rápidos). Cada fila ya viene etiquetada como `Fixation` o
`Saccade` por el clasificador I-DT.

El heatmap agrupa las filas consecutivas marcadas `Fixation` en un solo punto:
el **centroide** (el promedio de X y de Y) y su **duración**. Una fijación de
600 ms cuenta como un punto, no como 18.

Sólo se conservan las fijaciones de **≥ 150 ms**: por debajo de eso el ojo está
en tránsito, no fijando.

### Paso 2 · De fijaciones a mapa de calor

Se crea una imagen de 1920×1080 en negro y se suma un punto en la posición de
cada fijación. Ese mapa se pasa por un **desenfoque gaussiano**
(`scipy.ndimage.gaussian_filter`, σ = 55 px), que reparte cada punto en un halo
suave. Después se normaliza a 0–1 y se le aplica un **mapa de color térmico**
(`cv2.COLORMAP_JET`): azul = frío, rojo = caliente.

Finalmente se mezcla sobre el fondo con transparencia (α = 0,65), pero sólo
donde la densidad supera un umbral — si no, el halo gaussiano acabaría
coloreando la pantalla entera.

### Paso 3 · Los tres archivos

| Archivo | Qué responde | Cómo se calcula |
| :--- | :--- | :--- |
| `..._density.png` | **¿Dónde** miró? | Cada fijación pesa **1**, sin importar cuánto duró |
| `..._dwell.png` | **¿Cuánto tiempo** miró ahí? | Cada fijación pesa su **duración en ms** |
| `..._scanpath.png` | **¿En qué orden** miró? | Círculos numerados unidos por flechas |

La diferencia entre los dos primeros importa: una zona puede tener **una** sola
fijación muy larga (dwell alto, densidad baja) o **muchas** fijaciones cortas
(densidad alta, dwell bajo). Son dos preguntas distintas.

### Cómo leer el scanpath

* El **número** es el orden temporal. `1` es lo **primero** que miró.
* El **tamaño del círculo** es cuánto duró esa fijación.
* Las **flechas** unen fijaciones consecutivas: son las sacadas.

Si la numeración salta de un lado a otro de la pantalla, o hay fijaciones de
1–2 muestras, la estimación de mirada está ruidosa. Un scanpath sano avanza en
tramos cortos y se detiene en las zonas con contenido.

### Por qué salían mal (y ya no)

Con la mitad de las muestras perdidas, el clasificador I-DT recibía datos cada
~137 ms en vez de cada ~33 ms. Eso partía fijaciones reales en trozos y creaba
fijaciones falsas, y el resultado era una maraña de líneas cruzando la pantalla.

Con el arreglo del punto 12 hay 4× más datos, las fijaciones son más estables y
el scanpath se puede leer.

---

## 🧪 Validación

```powershell
C:\Python313\python.exe validacion.py     # 17 comprobaciones
C:\Python313\python.exe _t_pose.py        # invariancia a la cabeza (detallado)
C:\Python313\python.exe _t_noise.py       # ruido real de los landmarks
C:\Python313\python.exe _t_live.py        # verificación en vivo
```

`validacion.py` incluye una prueba que mete landmarks sintéticos por las
**mismas clases que usa la app** (`_HeadPoseEstimator`, `_EyeGazeExtractor`,
`GazeCalibrator`) y mide el error al mover la cabeza. Así se valida el código
de producción, no un prototipo aparte.

Resultado actual: **17/17**.

---

## 🏗️ Arquitectura

```
main_app.py                    UI Tkinter + overlay click-through
pagina_prueba.html             Página de estímulo para las pruebas
run.bat                        Lanzador que encuentra el Python correcto

et_suite/
  camera.py                    Detección y apertura robusta de cámaras
  gaze_tracker.py              Motor: pose → mirada 3D → calibración → filtro
  calibration_engine.py        Ventana de calibración 5/9 puntos
  filters.py                   Filtro 1€ (One Euro Filter)
  pupillometry.py              Pupila, clasificadores I-VT e I-DT, AOI, TTFF
  data_logger.py               Grabación a CSV en segundo plano
  heatmap_generator.py         Heatmaps de densidad / dwell / scanpath
  report.py                    Informe: orden de fijación y zonas de atención
  experiment_server.py         Servidor TCP para OpenSesame

synth_gaze.py                  Simulador cabeza-ojo-pantalla (verdad conocida)
validacion.py                  17 comprobaciones del sistema completo
```

### Pipeline

```
cámara → FaceLandmarker (478 pts) → solvePnP 14 pts → mirada 3D → calibración
                                          │                          → filtro 1€ → CSV
                                          ├─ R, t: orientación y posición de la cabeza
                                          ├─ E: centro de los globos oculares (mm)
                                          └─ d: dirección de mirada en coordenadas
                                                de cámara  ← invariante a la cabeza
```

La calibración geométrica corta el rayo `E + λ·d` con el plano de la pantalla
y ajusta una **transformación afín** de 6 parámetros. El polinomio de 2º orden
sigue disponible (`method="polynomial"`) como respaldo, pero es peor en todos
los escenarios medidos.

---

## 📊 Columnas del CSV

| Columna | Descripción |
| :--- | :--- |
| `Timestamp_ms` | Tiempo absoluto (`time.perf_counter()` × 1000) |
| `Frame_ID` | Número de fotograma |
| `Gaze_X` / `Gaze_Y` | Coordenadas en pantalla, filtradas |
| `Pupil_Size` | Radio del iris en px |
| `Event_Type` | `Fixation` / `Saccade` / `EVENT:<label>` |
| `Head_Pitch` / `Head_Yaw` / `Head_Roll` | Orientación de cabeza en grados |
| `Sample_Hz` / `Jitter_ms` | Frecuencia y estabilidad temporal |
| `Face_Detected` | 1 = rostro visible |

---

## ❓ Si algo falla delante del profesor

| Síntoma | Qué hacer |
| :--- | :--- |
| "No se encontró ninguna cámara" | Cierra Zoom/Teams/OBS. Pulsa `🔍 Detectar cámaras`. |
| Se abre pero no hay imagen | Mira `🎥 Debug`. Si sale negro, la cámara está ocupada. |
| El punto va a la esquina y no se mueve | Falta calibrar. El texto ámbar *SIN CALIBRAR* lo indica. |
| Va a saltos / pocos FPS | La línea *Cámara* muestra los FPS. Si son <12, cambia a `Auto (recomendado)`. |
| Dice "Sin cara detectada" | Ilumina la cara de frente, a 50-70 cm. Evita contraluz. |
| La calibración sale MALA | Que la ventana de calibración te vea la cara (indicador verde abajo). **Ya no hace falta quedarse quieto**: el modelo es invariante a la cabeza. |
| La mirada se va al moverme | No debería. Comprueba en `🎥 Debug` que el *error de reproyección* de la pose es bajo. Si te mueves mucho, la cámara puede perder la cara. |
| El CSV sale vacío | La sesión duró <1 s o no se detectó el rostro. |

---

## 📚 Referencias

* MediaPipe Face Landmarker — https://ai.google.dev/edge/mediapipe
* One Euro Filter — Casiez, Roussel & Vogel, CHI 2012
* I-DT — Salvucci & Goldberg, ETRA 2000
* Eye model-based gaze estimation — Guestrin & Eizenman, IEEE TBME 2006
* L2CS-Net (referencia, no usado) — Abdelrahman et al., 2022
