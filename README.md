# 👁️ Eye-Tracking Suite v3.0

Sistema de seguimiento ocular **con webcam** (sin hardware especial) para
evaluar la usabilidad de un sitio web con participantes: cada persona hace unas
tareas guiadas mientras el sistema estima **qué mira, en qué orden y cuánto
tiempo**, y al final entrega métricas por tarea y por elemento de la página,
informes, mapas de calor y un análisis de todo el grupo.

Funciona en **Windows** y **macOS** (y en Linux con Tk).

> **Diseñado para** el *Proyecto Iris: Optimización UX/UI y análisis
> biométrico aplicado a plataformas digitales* (estudiantes UDEM, 3 tareas en
> el sitio **Ventana Amarilla = Portal UDEM, `https://portal.udem.edu/`**).
> Cada participante hace la prueba **en su propia computadora**, entra al
> portal **con su propia cuenta** y sube un `.zip` a una carpeta de Google
> Drive. Todo lo que cambia entre un estudio y otro vive en un archivo JSON
> dentro de [`studies/`](studies/); el código no se toca.
>
> **Sin Chrome ni Tampermonkey.** El sitio se abre en un **navegador
> integrado** en el prototipo, que hace lo mismo que el antiguo script de
> Tampermonkey (scroll, posición de cada AOI, clics y navegación) sin que el
> participante instale nada. Ver [Navegador integrado](#-navegador-integrado-sin-tampermonkey).

**Índice:** [Qué hace](#-qué-hace) · [Instalación](#-instalación) ·
[Arranque](#-arranque) · [Protocolo de la prueba](#-protocolo-de-la-prueba-paso-a-paso) ·
[Definir el estudio](#-definir-el-estudio-studiesjson) ·
[Navegador integrado](#-navegador-integrado-sin-tampermonkey) ·
[Métricas](#-métricas-qué-significa-cada-una) · [Archivos de salida](#-archivos-de-salida) ·
[Privacidad](#-privacidad-y-ética) · [Precisión y límites](#-precisión-y-límites-importante) ·
[Pruebas](#-pruebas-automáticas) · [Arquitectura](#-arquitectura) ·
[Problemas frecuentes](#-si-algo-falla) · [Cómo funciona la mirada](#-cómo-funciona-la-mirada-técnico)

---

## ✅ Qué hace

| Necesidad del estudio | Cómo lo resuelve |
| :--- | :--- |
| **3 tareas guiadas** | Panel pequeño con la instrucción, «Comenzar», «Terminé» y «No pude». Tiempo límite opcional por tarea. |
| **Métricas por elemento** (¿lo vio?, ¿cuándo?, ¿cuánto?) | Áreas de interés (AOI) definidas con un **selector CSS**: el navegador mide el elemento real, **aunque haya scroll** o cambie el tamaño de la ventana. También hay AOI por rectángulo. |
| **Proceso de la tarea** | TTFF al objetivo, dwell, fijaciones, visitas/revisitas, orden de descubrimiento, secuencia de AOI, recorrido de la mirada, clics, páginas visitadas, tiempo en tarea y éxito. |
| **Éxito verificado** | El participante dice si terminó; el **facilitador confirma** si fue correcto (la métrica de éxito usa el veredicto del facilitador). |
| **Calidad por participante** | Calibración **con cada persona**, verificación de precisión al inicio y **al final**, y reglas de exclusión automáticas (cara detectada, frecuencia, error de calibración/validación, duración, consentimiento). |
| **Grupo completo** | CSV para Excel/SPSS/R (por sesión, tarea y AOI), resúmenes con media/DE/mediana, comparación entre grupos y **mapas de calor grupales** (cada participante pesa igual). |
| **Flujo de cada tarea** | Los pasos del diagrama (`"flow"` en el estudio): clic en un elemento, que aparezca un menú, bajar hasta un panel, mirarlo, cambiar de página o salir a otro sitio. El informe marca cuáles hizo cada participante, en qué orden y en qué segundo. |
| **Heatmaps sobre lo que vio** | El navegador integrado guarda imágenes de la página (sin permisos extra en macOS); el análisis reconstruye la página completa con el scroll y pinta encima el **mapa de calor** y el **recorrido** de la mirada, por participante y del grupo. |
| **Informe fácil de leer** | `…_informe.html`: si los datos sirven, y por tarea el resultado, los pasos del flujo y las imágenes. |
| **Ética** | Consentimiento en pantalla, códigos P01/P02 (sin nombres), no se guarda video. |
| **Precisión honesta** | Error de calibración y verificación en **px y en grados** de ángulo visual; avisa si un AOI es menor que el error. |

---

## 📋 Instalación

**Requisitos:** Python 3.10 o superior ([python.org](https://www.python.org/downloads/);
en Windows deja marcado *«Add python.exe to PATH»*), una webcam e internet.
**No hace falta Chrome ni ninguna extensión.**

| Sistema | Cómo abrirlo |
| :--- | :--- |
| **Windows** | Doble clic en `run.bat`. Si aparece *«Windows protegió su PC»*: **Más información → Ejecutar de todas formas**. |
| **macOS** | En la Terminal: `bash ` + arrastrar `run.sh` + Enter (no pasa por Gatekeeper). O doble clic en `Iniciar_Mac.command`; si macOS dice que no puede verificarlo: *Ajustes del Sistema → Privacidad y seguridad → Abrir de todos modos*. |

**La primera vez** el lanzador crea solo un entorno `.venv` e instala las
dependencias (≈ 3–5 min, necesita internet); las siguientes veces abre
directo. Si no encuentra un Python adecuado, explica qué instalar.

**Permisos (una sola vez)** — están explicados también en la pestaña **Guía**
de la app:

* **Cámara:** en macOS, *Ajustes del Sistema → Privacidad y seguridad →
  Cámara → Terminal*; en Windows, *Configuración → Privacidad y seguridad →
  Cámara → permitir a las aplicaciones de escritorio*. Si el antivirus avisa
  que «python» quiere usar la cámara, hay que permitirlo.
* **Grabación de pantalla (macOS): no se necesita** en este estudio
  (`"capture_screenshots": false`).
* **Firewall / antivirus:** con el estudio real el prototipo **no abre
  ningún puerto de red**: el navegador integrado le pasa los datos por su
  propio canal (stdin/stdout del proceso). Sólo la página demo
  (`--estudios studies/ejemplos`) levanta un servidor, y únicamente en
  `127.0.0.1`. Tampoco hay ganchos de teclado/ratón globales, capturas de
  pantalla (desactivadas) ni nada instalado fuera de la carpeta.
* **Navegador:** ninguno. En Windows el navegador integrado usa *Microsoft
  Edge WebView2*, que ya viene con Windows 11 (en Windows 10 casi siempre
  también); en macOS usa el motor de Safari.

También se puede lanzar a mano:

```bash
python main_app.py                         # cámara automática
python main_app.py --camera 1              # forzar cámara 1
python main_app.py --estudios studies/ejemplos   # practicar con la página demo
python main_app.py --ensayo                # grabar sin calibrar (sólo equipo)
```

---

## 🚀 Arranque

La ventana tiene tres pestañas:

| Pestaña | Para qué sirve |
| :--- | :--- |
| **Guía** | Permisos (cámara, antivirus, macOS), los pasos y problemas frecuentes. Se adapta a Windows o macOS. |
| **Prueba** | Lo que hace el participante, **en 5 pasos numerados de arriba abajo**: ① código, ② abrir el sitio e iniciar sesión, ③ calibrar, ④ hacer las 3 tareas, ⑤ preparar el `.zip`. El paso que toca está resaltado en amarillo y cada paso hecho aparece con ✓. |
| **Resultados (equipo)** | **🌐 Ver informe (con heatmaps)** de una sesión, **✏ Revisar tareas** (¿fue correcta?) y **👥 Análisis grupal**. |

Arriba hay tres indicadores de color (verde = bien, amarillo = revisar,
rojo = problema): **cámara**, **cara detectada** y **navegador** (si el
navegador integrado está enviando datos). Si una pestaña no cabe en la
pantalla aparece una barra de desplazamiento.

---

## 🧪 Protocolo de la prueba (paso a paso)

**Antes de la jornada (una vez):**

1. [`studies/ventana_amarilla.json`](studies/ventana_amarilla.json) ya trae
   las 3 tareas de los diagramas de flujo y los AOI por **texto visible**
   ([ver abajo](#-estudio-ventana-amarilla)).
2. Practica con la página demo (`python main_app.py --estudios
   studies/ejemplos`) y después haz un **piloto completo con el sitio real en
   Windows y en macOS** con una cuenta del equipo. Si el informe dice
   *«AOI que el navegador nunca encontró»*, ajusta el `text`/`selector` de
   ese AOI.
3. Crea la carpeta de Google Drive (que los participantes sólo puedan
   **subir**, p. ej. un formulario de carga o una carpeta compartida sólo
   con el equipo) y la lista de códigos P01, P02… (uno por participante).
4. Genera el paquete para participantes:
   `python tools/empaquetar_participante.py` → `dist/EyeTracking_ventana_amarilla_2026_<fecha>.zip`
   (sólo la app, el estudio y un `LEEME.txt`; sin pruebas, ejemplos ni
   código histórico, y con los permisos de ejecución de macOS ya puestos).
   Envía ese `.zip`, el código del participante, el enlace de Drive y la
   indicación: *«lee LEEME.txt y sigue la pestaña Guía»*.
5. El navegador integrado se abre maximizado y sin zoom: no hay que
   pedirle nada al participante.

**Lo que hace cada participante (≈ 15–20 min, también está en la pestaña Guía):**

1. Siéntalo a ~60 cm de la pantalla, cámara a la altura de los ojos, cara bien
   iluminada **de frente** (nada de ventanas detrás). Sin gorras ni flequillo
   sobre los ojos. Anota si usa lentes (casilla *Usa lentes*).
2. Pestaña **Prueba**, paso ①: el **código asignado** (P01, P02… según tu
   lista; la app no propone ninguno para que no se repitan entre
   computadoras). **No escribas nombres.**
3. Paso ②: **🌐 Abrir el sitio**. Se abre el portal en una ventana del
   prototipo (siempre en la pantalla principal) y el participante **inicia
   sesión con su cuenta UDEM**. En la página de login no se registra nada.
   Cuando cargue el portal el paso se marca con ✓.
4. Paso ③: **🎯 Calibrar**. Explica: *«Mira cada punto que aparezca, sin
   mover la cabeza»*. Al terminar se hace sola una **verificación** con otros
   5 puntos y se muestra el error en px y grados.
   * Error de verificación **< 250 px**: bien. **> 350 px**: repite
     (mejora la luz, ajusta la cámara/distancia).
5. Paso ④: **▶ Empezar la prueba**.
6. El participante lee y **acepta el consentimiento**. La ventana del
   facilitador se minimiza y aparece el **panel de tareas**.
7. Para cada tarea el participante lee la instrucción y pulsa **Comenzar**
   (se abre el sitio de la tarea), la realiza y pulsa **Terminé** o **No pude**.
   El tiempo y la mirada se miden desde que pulsa «Comenzar».
8. Al terminar la tarea 3 aparece la **verificación final de precisión**
   (5 puntos, ~15 s).
9. La app guarda todo, genera el informe y los heatmaps y dice si los datos
   tienen buena calidad. Con **📦 Preparar archivo para enviar** se crea
   `para_enviar/<código>_<estudio>_<fecha>.zip` y se abre su carpeta; el
   participante lo **sube a la carpeta de Google Drive**.
10. **Después**, el equipo revisa si cada tarea fue correcta: pestaña
    *Resultados* → elegir la sesión → **✏ Revisar tareas**. Si prefieres
    hacerlo justo al terminar cada sesión (prueba presencial), pon
    `"review_at_end": true` en el estudio.

**Al recibir los datos:** descarga los `.zip` de Drive y descomprímelos
**dentro de `sessions/`** de la computadora del equipo (cada zip ya trae la
carpeta `<id_del_estudio>/`). Luego, pestaña **Resultados → ↻ → 👥 Análisis
grupal**.

> **Emergencia:** `Ctrl+Mayús+Q` sobre el panel de tareas (o el botón
> *Terminar sesión*) interrumpe la sesión; lo grabado hasta ese momento se
> conserva y queda marcado como interrumpido. Cerrar la ventana también guarda
> lo grabado.

> **Modo ensayo (sólo para el equipo):** `python main_app.py --ensayo`
> permite grabar sin calibrar para practicar. Esas sesiones quedan marcadas
> como ensayo y **no entran** en el análisis grupal.

---

## 🧩 Definir el estudio (`studies/*.json`)

Cada archivo `.json` de [`studies/`](studies/) es un estudio y aparece en el
selector de la pestaña 1 (pulsa **↻** tras editarlo). Los archivos que
empiezan con `_` son plantillas y se ignoran. Si un archivo tiene errores, la
app **lista todos los problemas** de una vez.

Para que el participante **sólo vea el estudio real**, en `studies/` está
únicamente [`ventana_amarilla.json`](studies/ventana_amarilla.json) (más la
plantilla). Los ejemplos viven en [`studies/ejemplos/`](studies/ejemplos/)
(la app no los lee salvo con `--estudios studies/ejemplos`):
[`demo_pagina_prueba.json`](studies/ejemplos/demo_pagina_prueba.json)
(3 tareas sobre la página de prueba) y
[`udem_portal.json`](studies/ejemplos/udem_portal.json) (el portal **viejo**,
`portalc.udem.edu`; ya no se usa). [`_plantilla_estudio.json`](studies/_plantilla_estudio.json) sirve
para otro sitio.

### 🟨 Estudio Ventana Amarilla

[`studies/ventana_amarilla.json`](studies/ventana_amarilla.json) es el estudio
real: *«Proyecto Iris: Optimización UX/UI y análisis biométrico aplicado a
plataformas digitales.»*, sobre **Ventana UDEM** (`https://portal.udem.edu/`).
Cada estudiante inicia sesión **antes** de empezar, en `login.udem.edu`, donde
**no se inyecta nada**. Las tareas siguen los diagramas de flujo del equipo:

| # | Tarea | Camino esperado (diagrama) | AOI que se miden |
| :- | :--- | :--- | :--- |
| 1 | **Acceso a Trayecto UDEM** | Home → ícono de cuadrícula 3×3 del encabezado → menú flotante de accesos directos → botón «Trayecto UDEM» (ícono «TU») → se abre la plataforma externa | `icono_cuadricula`, `menu_accesos`, **`obj_t1`** (botón Trayecto UDEM) |
| 2 | **Búsqueda y acceso a la Encuesta Avanza** | Home → clic en el buscador → escribir «Encuesta Avanza» → seleccionar el enlace → cargan las preguntas | `buscador`, **`obj_t2`** (enlace en los resultados) |
| 3 | **Consulta del panel «Mis Solicitudes»** | Home → scroll pasando por «Vida académica» y «Servicios escolares» → zona inferior → tabla «Mis Solicitudes» | `bloque_vida_academica`, `bloque_servicios_escolares`, **`obj_t3`** (panel) |

Más `encabezado`, `contenido` y `buscador`, que valen para todas las tareas.
Cada tarea arranca en la página de inicio, tiene 3 min de límite y termina
cuando el participante pulsa «Terminé» (las instrucciones no revelan el
camino; la de la encuesta pide no contestarla).

* **AOI por texto:** como sin cuenta no se puede ver el HTML del portal, los
  AOI se buscan por su **texto visible** (`"text": "Mis Solicitudes"`) o por
  su `aria-label`/`placeholder` (el ícono de cuadrícula y el buscador). El
  `selector` que acompaña al texto dice a qué contenedor subir (p. ej. la
  tarjeta completa de «Mis Solicitudes», no sólo su título). **En el piloto**
  revisa el informe: si dice *«AOI que el navegador nunca encontró»*, ajusta
  ese AOI (lo más probable: el `aria-label` del ícono de cuadrícula).
* **Otros dominios:** Trayecto UDEM y la encuesta pueden abrirse en otro
  dominio. Ahí no se inyecta nada; sólo se anota **el nombre del dominio**
  (aparece en el informe como *«Sitios tarea N»*, útil para confirmar que
  llegó). Si quieres medir AOI dentro de ellos, agrega el dominio a
  `bridge.hosts` (la *nota para el equipo* al final del piloto te lo dice).
* **Consentimiento:** versión `IRIS-5`, actualizada para las tareas nuevas y las imágenes de la página
  (sin contestar la encuesta ni hacer trámites; de otros sitios sólo se
  registra el dominio). Si cambias el texto, sube `consent_version`.

Ya viene configurado para pruebas remotas: sin capturas de pantalla (no pide
el permiso de *Grabación de pantalla*), sin revisión al final (el equipo
evalúa después con **✏ Revisar tareas**) y con verificación final de
precisión.

```jsonc
{
  "id": "udem_usabilidad_2026",          // identificador (letras, números, - _)
  "name": "Evaluación de usabilidad · Sitio UDEM",
  "url": "https://www.udem.edu.mx",      // el script del navegador sólo se activa en este dominio
  "groups": ["A", "B"],                  // opcional: condiciones a comparar

  "tasks": [
    { "id": 1, "title": "Buscar becas",
      "instruction": "Encuentra la información sobre becas y haz clic en ella.",
      "target_aoi": "becas",             // el AOI que debería encontrar
      "time_limit_s": 180,               // opcional
      "start_url": "https://www.udem.edu.mx" }   // opcional: se abre al pulsar «Comenzar»
  ],

  "aois": [
    { "id": "becas", "label": "Enlace Becas", "selector": "a[href*='becas']", "tasks": [1] },
    { "id": "menu",  "label": "Menú principal", "selector": "nav" },
    { "id": "franja", "label": "Franja superior", "rect": [0, 0, 1920, 120] }   // rectángulo manual
  ],

  "quality": { "max_cal_rms_px": 300, "max_validation_px": 350, "min_face_pct": 80 }
}
```

**Cómo se define un AOI** (una de estas formas):

| Forma | Cuándo usarla |
| :--- | :--- |
| `"text": "Mis Solicitudes"` | **La más fácil:** el texto visible del elemento (o su `aria-label`, `title`, `placeholder`). Sin importar mayúsculas ni acentos; una lista `["Apps", "Accesos"]` vale si aparece cualquiera. Con `"selector"` a la vez, se sube desde ese texto al contenedor más cercano que lo cumpla (`"selector": "section, [class*='card' i]"` = toda la tarjeta). No hace falta ver el HTML. |
| `"selector": "nav"` | **Recomendada.** Selector CSS. El navegador mide el elemento real (con scroll, cambios de tamaño, menús desplegables…). Requiere el [script del navegador](#-script-del-navegador-tampermonkey). Para encontrar el selector: clic derecho sobre el elemento → *Inspeccionar* → clic derecho en el nodo → *Copiar → Copiar selector*. |
| `"opens": "Trayecto UDEM"` | El **botón que abre el menú** donde aparece ese texto: para íconos sin texto (como la cuadrícula 3×3). |
| `"rect": [x, y, ancho, alto]` | Rectángulo manual. Con `"space": "viewport"` (por defecto) son píxeles de pantalla a 1920×1080 (se escalan solos) y sólo sirve si la página **no se desplaza**. Con `"space": "page"` son píxeles CSS de la página completa (necesita el script). |

**Flujo esperado de una tarea** (`"flow"`, opcional): la lista de pasos del
diagrama de flujo. Cada paso usa UNA clave; se busca cada uno después del
anterior y el informe dice cuáles hizo y en qué segundo.

| Paso | Se cumple cuando… |
| :--- | :--- |
| `{"click": "aoi"}` | hace clic sobre ese AOI (o sobre su texto) |
| `{"visible": "aoi"}` | el AOI aparece (p. ej. se abre un menú o salen resultados) |
| `{"scroll_to": "aoi"}` | baja la página hasta tenerlo en pantalla |
| `{"look": "aoi"}` | fija la mirada en él |
| `{"navigate": true}` | se carga otra página |
| `{"leave_site": true}` | se abre un sitio de otro dominio (p. ej. Trayecto UDEM) |

Cada paso lleva además `"id"` y `"label"` (el texto que sale en el informe).

* `"tasks": [1, 3]` limita un AOI a esas tareas (sin esto vale para todas).
* `"url_contains"` limita un AOI a las URL que contengan ese texto (útil en sitios con varias páginas).
* Un AOI más pequeño que **2× el error de precisión** se marca en el informe con
  ⚠ — con webcam no se pueden distinguir elementos más pequeños que eso.

Otras opciones útiles: `reference_screen`, `display.width_cm` /
`display.distance_cm` (para convertir px a grados), `consent_text`,
`task_panel.position` (`bottom` · `top` · `bottom-right`),
`capture_screenshots`, `final_validation`, `review_at_end` (ventana para que
el facilitador marque si cada tarea fue correcta al terminar; por defecto
`false`), `show_gaze_overlay` (sólo Windows;
**desactivado**: ver la propia mirada la modifica), `analysis.min_fixation_ms`
y `analysis.heatmap_sigma_px`.

---

## 🌐 Navegador integrado (sin Tampermonkey)

Sin datos del navegador, la app sólo sabe *dónde en la pantalla* mira el
participante. Con ellos sabe *qué elemento de la página* es, aunque haya
scroll. Antes eso exigía instalar Tampermonkey y pegar un script; ahora lo
hace el propio prototipo:

1. **🌐 Abrir el sitio e iniciar sesión** abre el sitio del estudio en una
   ventana propia del prototipo (Edge WebView2 en Windows, Safari/WebKit en
   macOS). Cada «Comenzar» de una tarea lleva esa misma ventana a su
   `start_url`, con la sesión ya iniciada.
2. En cada página **de los dominios del estudio** (`url` + `bridge.hosts`) se
   inyecta el mismo [`web/eyetracking_bridge.user.js`](web/eyetracking_bridge.user.js)
   de siempre: lee el scroll, el tamaño de la ventana, la posición de los
   AOI, los clics (sólo etiqueta, id, clase y los primeros 60 caracteres del
   texto) y la navegación. Los datos pasan del navegador a la app por el
   stdin/stdout del proceso, **sin red ni puertos**: el navegador no bloquea
   ni pregunta nada y el firewall/antivirus no ve ningún servidor.
3. En **cualquier otro dominio** —incluida la página de inicio de sesión—
   **no se inyecta nada**: la contraseña nunca pasa cerca del prototipo. De
   esos dominios sólo se anota el nombre (p. ej. que se llegó a Trayecto UDEM).
4. La posición exacta del área de contenido en la pantalla se mide con el
   ratón, como hace Firefox (`mozInnerScreenX/Y`): la mirada se sitúa en la
   página sin estimar el alto de la barra de título.
5. Los enlaces que abrirían otra pestaña se abren **en la misma ventana**,
   para no perder el registro.
6. Es **privado**: no comparte cookies ni contraseñas con el navegador
   personal, y al terminar la sesión **la ventana se cierra y la sesión del
   portal se borra** de la computadora.

Para tus **propias páginas HTML**, basta con incluir
`<script src="http://127.0.0.1:4243/web/eyetracking_bridge.user.js"></script>`
(como hace `pagina_prueba.html`).

> **Plan B:** si en alguna computadora el navegador integrado no abriera
> (Windows sin WebView2), la app lo dice y abre el navegador normal. Ahí sólo
> se registra la mirada en la pantalla. Solución: instalar *Microsoft Edge
> WebView2 Runtime* (gratis, de Microsoft).

---

## 📐 Métricas: qué significa cada una

Todos los tiempos «hasta» se miden **desde que el participante pulsa «Comenzar»**
en la tarea.

| Métrica | Significado |
| :--- | :--- |
| **Resultado / éxito** | Lo que dijo el participante (*completada*, *abandonada*, *tiempo agotado*) y, si existe, el **veredicto del facilitador** (correcta / incorrecta). El éxito usa el veredicto del facilitador. |
| **Tiempo en tarea** | Desde «Comenzar» hasta «Terminé / No pude / tiempo agotado». |
| **TTFF** (time to first fixation) | Tiempo hasta la **primera fijación dentro de un AOI**. `TTFF objetivo` = del AOI que la tarea pide encontrar. Menor = más fácil de encontrar. |
| **¿Visto?** | Si el AOI recibió al menos una fijación. Un objetivo **no visto** es el síntoma típico de un elemento invisible para el usuario. |
| **Dwell** | Suma de la duración de las fijaciones dentro del AOI (ms). Alto = atrajo atención o costó entenderlo. |
| **Fijaciones / %tiempo** | Nº de fijaciones en el AOI y su porcentaje del tiempo total de fijación de la tarea. |
| **Visitas / revisitas** | Veces que la mirada **entró** al AOI; revisitas = visitas − 1. Muchas revisitas = confusión o comparación. |
| **Orden de descubrimiento** | Posición del AOI entre los primeros que se miraron. |
| **Secuencia de AOI / transiciones** | Recorrido (p. ej. `menú → título → buscador`) y número de cambios de AOI. Más transiciones = búsqueda menos eficiente. |
| **Recorrido (scanpath, px)** | Longitud total de la trayectoria de la mirada. Más largo = búsqueda más dispersa. |
| **Clics / páginas** | Clics y páginas distintas visitadas durante la tarea (necesita el script). |
| **Parpadeos** | Detectados por apertura del ojo (EAR); se excluyen de las fijaciones. |
| **Fijación** | Mirada quieta ≥ 150 ms (clasificador **I-DT**, el adecuado para el ruido de una webcam). |

El **panel de tareas** nunca cuenta como «mirar el sitio»: las fijaciones sobre
él se apartan.

> **Sobre la pupila.** El CSV contiene `Pupil_Size`, que es el **radio del iris
> en píxeles de cámara**, no el diámetro de la pupila. Con una webcam
> normal la pupila no se puede medir de forma fiable, así que **no se reporta
> como métrica** ni se debe usar para inferir carga cognitiva.

---

## 📦 Archivos de salida

Cada sesión crea, en `sessions/<id_del_estudio>/`:

```
P01_20261003_101500.csv            mirada muestra a muestra (ver columnas abajo)
P01_20261003_101500.meta.json      participante, consentimiento, equipo, calibración,
                                   verificaciones, tareas, calidad
P01_20261003_101500.web.json       eventos del navegador (scroll, AOI, clics, páginas)
P01_20261003_101500_shots/         imágenes de la página que vio en cada tarea (t1_001.jpg…)
P01_20261003_101500_informe.html   ⭐ informe para leer: calidad, pasos del flujo e imágenes
P01_20261003_101500_informe.txt    lo mismo en texto
P01_20261003_101500_tarea1_vista1_calor.png      mapa de calor sobre la página que vio
P01_20261003_101500_tarea1_vista1_recorrido.png  orden de la mirada sobre esa página
```

**📦 Preparar archivo para enviar** comprime todo lo anterior en
`para_enviar/P01_<id_del_estudio>_<fecha>.zip` (junto a la carpeta
`sessions/`), listo para subir a Google Drive.

El análisis grupal escribe en `resultados/<id_del_estudio>/`:

| Archivo | Contenido |
| :--- | :--- |
| `resumen_grupal.txt` | Resumen legible: participantes usados/excluidos (con motivo), éxito, tiempos y TTFF por tarea y grupo. |
| `sesiones.csv` | **Una fila por participante**: calidad, error en px y grados, y las métricas clave de cada tarea. |
| `tareas.csv` | Una fila por participante × tarea (todas las métricas de tarea). |
| `aoi.csv` | Una fila por participante × tarea × AOI. |
| `resumen_tareas.csv` / `resumen_aoi.csv` | Media, DE y mediana por tarea/AOI, para todo el grupo y por grupo (comparación entre condiciones). |
| `resumen_flujo.csv` | Por tarea y paso del flujo esperado: % de participantes que lo hizo y en qué segundo (mediana). `tareas.csv` trae los pasos de cada participante. |
| `heatmap_tareaN_vistaK_grupo.png` | Mapa de calor **de todo el grupo sobre la página real** (cada participante pesa igual). Sin capturas: `heatmap_tareaN_dwell.png` / `_densidad.png` / `_pagina.png` sobre fondo blanco. |

Los CSV usan UTF-8 con BOM y encabezados en español: se abren directamente en
**Excel**, y se importan sin cambios a **SPSS / R / pandas**.

**Columnas de `sessions/.../*.csv`:** `Timestamp_ms`, `Frame_ID`,
`Gaze_X/Gaze_Y` (pantalla, filtradas), `Pupil_Size` (radio del iris, px de
cámara), `Event_Type` (`Fixation` · `Saccade` · `Blink` · `Unknown` ·
`EVENT:<marca>`), `Head_Pitch/Yaw/Roll`, `Sample_Hz`, `Jitter_ms`,
`Face_Detected`, **`Task_ID`** (0 = fuera de tarea), **`Scroll_X/Scroll_Y`** y
**`Page_X/Page_Y`** (mirada en coordenadas de la **página**; vacío sin script),
**`Blink`** y **`Eye_Open`**. El archivo se va guardando cada pocos segundos:
un corte de luz no pierde la sesión.

**Reglas de exclusión** (configurables en `quality` del estudio): cara
detectada < 80 %, frecuencia < 8 Hz, error de calibración > 300 px, error de
verificación > 350 px, duración < 20 s, sin tareas con datos o **sin
consentimiento**. También se avisan (sin excluir): frecuencia baja,
precisión que empeora durante la sesión, parpadeos excesivos, AOI menores que el
error y falta de datos del navegador.

---

## 🔒 Privacidad y ética

* **No se graba video ni fotos de la persona.** La cámara sólo alimenta el
  cálculo de la mirada en tiempo real.
* **Imágenes de la página (`page_capture`):** el navegador integrado guarda
  imágenes **sólo del contenido de la ventana del sitio**, durante las
  tareas y en los dominios del estudio (nunca el login, el escritorio u
  otras ventanas). Sirven de fondo para los heatmaps. Como el portal muestra
  datos personales, lo dice el consentimiento (`IRIS-5`) y **no deben
  publicarse**: para presentaciones usa capturas de una cuenta del equipo.
  Se desactiva con `"page_capture": false`.
* **Cuenta del estudiante:** inicia sesión él mismo en el navegador
  integrado. En la página de login no se inyecta ni se registra nada; al
  terminar, la ventana se cierra y la sesión se borra. La captura de la
  pantalla completa del sistema (`capture_screenshots`) está desactivada.
* **Nada que instalar en el navegador personal** (sin extensiones con acceso
  a todas las páginas). El prototipo vive en su carpeta: para quitarlo,
  basta con borrarla.
* **Anónimo:** se usa un código (P01…). La app **rechaza** códigos con
  espacios o acentos para que no se cuelen nombres.
* **Consentimiento** en pantalla antes de grabar (texto estándar o el que
  pongas en `consent_text`); queda registrado con fecha y versión. Sin
  aceptarlo **no se graba nada**.
* Los datos están **sólo en el equipo del participante** hasta que él sube su
  `.zip` a Drive. `sessions/*/`, `resultados/` y `para_enviar/` están en
  `.gitignore` para no subirlos por accidente a un repositorio.
* Confirma con tu asesor/comité si el protocolo requiere aprobación ética
  formal y adapta `consent_text` a sus requisitos.

---

## 🎯 Precisión y límites (importante)

Una webcam no es un eye tracker infrarrojo. Para interpretar bien los
resultados:

* **Error típico: 2–4° de ángulo visual** (≈ 100–250 px en una pantalla de
  laptop). Se mide con cada participante y aparece en el informe en px y en
  grados (se necesita el ancho físico de la pantalla: se detecta solo o se
  fija en `display.width_cm`; se asume 60 cm de distancia).
* Por eso los AOI deben ser **bloques grandes** (menú, banner, tarjeta,
  formulario), **no palabras ni iconos pequeños**. El informe marca con ⚠ los
  AOI menores que el doble del error.
* **Corrección por clics (automática).** Si el participante se acomoda
  distinto después de calibrar, la mirada queda desplazada de forma casi
  constante. Al hacer clic la persona mira lo que pulsa, así que el
  análisis usa los clics para estimar ese desplazamiento y restarlo (como
  WebGazer). Sólo se aplica con ≥ 3 clics y si reduce el error; el
  informe dice cuánto se corrigió. En el piloto real bajó el error en los
  clics de 457 a 153 px. Se desactiva con
  `"analysis": {"click_correction": false}`.
* **Las conclusiones son a nivel de grupo.** Un solo participante con error
  alto no sirve; la app lo excluye con las reglas de calidad.
* La precisión **empeora si el participante se mueve mucho**, con mala luz,
  lentes con reflejos o pestañas/flequillo sobre el ojo. La verificación final
  mide cuánto se degradó durante la sesión.
* **Usa siempre el mismo equipo, tamaño de ventana y zoom (100 %)** con todos
  los participantes.
* Los heatmaps y las métricas dicen **qué miraron**, no **por qué**. Combínalos
  con el éxito de la tarea, los tiempos y, idealmente, un breve cuestionario
  (p. ej. SUS) o una pregunta abierta.

---

## 🧪 Pruebas automáticas

Todo se puede probar **sin cámara ni participantes** con un participante
virtual (`tests/helpers.py`):

```bash
python -m unittest discover -s tests -v        # ≈ 3 min (la parte con ventanas tarda más)
python validacion.py --sin-camara              # modelo geométrico, I-DT, informe
python validacion.py                           # completo (con cámara y una persona delante)
```

| Archivo | Qué comprueba |
| :--- | :--- |
| `tests/test_foundations.py` | Geometría pantalla→página (Windows/macOS/zoom/Firefox), validación del JSON del estudio, detector de parpadeos y servidor del puente web. |
| `tests/test_pipeline.py` | Grabador (alineación de scroll/tareas, evento final, autoguardado), fijaciones, métricas por AOI (visitas, TTFF, orden), AOI por selector **bajo el pliegue con scroll**, calidad/exclusión, informe, heatmaps y análisis grupal. |
| `tests/test_app_flow.py` | La aplicación real con ventanas: calibración → verificación → consentimiento → 3 tareas (terminada, abandonada, tiempo agotado) con eventos del navegador por HTTP → verificación final → revisión (opcional) → informe, heatmaps y análisis grupal; además ensayo, interrupciones y cierre de ventana. *Necesita pantalla.* |

`tools/diag_live.py` y `tools/diag_noise.py` son diagnósticos con cámara real.

---

## 🏗️ Arquitectura

```
main_app.py                    Interfaz (Guía + 3 pestañas), panel de tareas, flujo de la sesión
run.bat / run.sh               Lanzadores (Windows / macOS-Linux); instalan solos la 1.ª vez
Iniciar_Mac.command            Doble clic en macOS (llama a run.sh)
pagina_prueba.html             Sitio de ensayo (con scroll) para el estudio demo
studies/                       Un .json por estudio (tareas, AOI, calidad, consentimiento)
studies/ejemplos/              Demo y portal viejo (no se muestran al participante)
web/eyetracking_bridge.user.js Script del navegador (scroll, AOI, clics); lo inyecta navegador.py

et_suite/
  camera.py                    Detección y apertura robusta de cámaras
  gaze_tracker.py              Motor: pose → mirada 3D → calibración → filtro; parpadeos
  calibration_engine.py        Calibración 5/9 puntos (ventana Tk en el hilo principal)
  validation_engine.py         Verificación de precisión (5 puntos distintos, px y grados)
  filters.py · pupillometry.py Filtro 1€ · I-DT/I-VT, detector de parpadeos
  study.py                     Carga y validación del estudio (JSON)
  display.py                   px ↔ grados; pantalla → coordenadas de página
  web_bridge.py                Puente de eventos del navegador (sin red; HTTP en 127.0.0.1 sólo para la demo)
  navegador.py                 Navegador integrado (pywebview): abre el sitio e inyecta el script
  data_logger.py               CSV en segundo plano, alineado con tarea y scroll
  session_meta.py              .meta.json: participante, consentimiento, calidad
  metrics.py                   Fijaciones, AOI, métricas por tarea, calidad
  report.py · heatmap_generator.py   Informe y mapas de calor
  group_analysis.py            Análisis de todo el grupo (CSV + mapas + resumen)
  screenshots.py               Captura de pantalla (screencapture / ImageGrab)

tests/ · validacion.py · synth_gaze.py   Pruebas y simulador cabeza-ojo-pantalla
tools/                         Diagnósticos con cámara real y empaquetar_participante.py (.zip para enviar)
legacy/                        Código histórico (no se usa)
```

**Flujo de datos de una sesión:**

```
cámara → mirada (x, y) → DataLogger ──────────────► CSV (+Task_ID, +Page_X/Y)
                           ▲   ▲
panel de tareas ───────────┘   └── navegador (scroll, layout, clics) ─► .web.json
                                                         │
                       metrics.py ◄──────────────────────┘
        (fijaciones → AOI → métricas) → informe · heatmaps · análisis grupal
```

---

## ❓ Si algo falla

| Síntoma | Qué hacer |
| :--- | :--- |
| `run.bat` / `run.sh` dice que no hay Python | Instala Python 3.12 de [python.org](https://www.python.org/downloads/) (Windows: marca *Add python.exe to PATH*) y vuelve a abrirlo. |
| Falla la instalación la primera vez | Revisa la conexión a internet y vuelve a ejecutar el lanzador; si persiste, borra la carpeta `.venv` y repite. |
| El antivirus bloquea o pregunta por «python» | Permite el acceso a la cámara: es el prototipo. No necesita conexión entrante ni permisos de administrador. |
| «No se encontró ninguna cámara» | Cierra Zoom/Teams/OBS y pulsa `🔍 Buscar cámaras` (en *▸ ¿No se detecta la cámara?*). En **macOS** da permiso de cámara a la Terminal/editor y reinicia. |
| La línea «Navegador» no se pone verde | Pulsa **🌐 Abrir el sitio e iniciar sesión** e inicia sesión: en el login no se registra nada a propósito. Si ya entraste y sigue amarilla, el portal te llevó a un dominio que no está en `bridge.hosts`. |
| La ventana del sitio no se abre (Windows) | Instala *Microsoft Edge WebView2 Runtime* (gratis, de Microsoft) y vuelve a intentarlo. |
| Al terminar aparece una «nota para el equipo» con otro dominio | El portal usó otro dominio durante las tareas: agrégalo a `bridge.hosts` en el JSON del estudio. |
| Un AOI sale «no visto» siempre / el informe dice «AOI que el navegador nunca encontró» | El `text` o `selector` no coincide con la página: revisa el texto exacto o el `aria-label` del elemento (clic derecho → *Inspeccionar*). |
| No me deja empezar la prueba | En la pestaña **Prueba**, el paso sin ✓ (en rojo o amarillo) dice qué falta (calibrar a *este* participante, código válido, grupo, cámara). |
| La calibración sale mala (> 300 px) | Mejora la luz frontal, acerca/alinea la cámara con los ojos, que se vea la cara en el indicador verde y **repite**. |
| Verificación alta (> 350 px) | Repite la calibración; si persiste, revisa lentes con reflejos y distancia (~60 cm). |
| Sesión «excluida» | Abre el informe: el motivo está al principio. Puedes forzar su inclusión en el análisis con la casilla *Incluir sesiones excluidas*. |
| «Puerto 4243 ocupado» (sólo con la página demo) | Hay otra copia de la app abierta; ciérrala. |
| Va a saltos / pocos FPS | Elige `Auto (recomendado)`; si la cámara da < 12 FPS usa otra o más luz. |
| Los heatmaps salen con fondo blanco | No hay captura: en macOS falta el permiso de *Grabación de pantalla*; también puedes poner una imagen en `background` de la tarea. |
| El CSV sale vacío | La sesión duró < 1 s o no se detectó el rostro. |

---

## 🧠 Cómo funciona la mirada (técnico)

### Mirada invariante a la cabeza

Medir el iris **en píxeles de la imagen** mezcla hacia dónde mira el ojo y
hacia dónde apunta la cabeza; por eso una calibración clásica sólo vale si no te
mueves. La solución es pasar todo a **geometría 3D**:

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

La dirección sale en coordenadas de cámara, así que **girar la cabeza no la
cambia** mientras se mire al mismo punto; y como el paso 5 usa la posición 3D
del ojo, **acercarse o alejarse tampoco**. La calibración sólo estima el plano
de la pantalla (`Z`): con `Z` fijo el ajuste es lineal, se barre una rejilla de
distancias y, como con la cabeza quieta `Z` no es identificable, se elige la
más cercana a 0 entre las de RMS prácticamente idéntico (la pantalla de un
portátil está en el plano del objetivo).

Medido sobre el simulador `synth_gaze.py` (cabeza 3D + ojos esféricos +
pantalla, con verdad conocida):

| Movimiento | Antes (polinomial) | Ahora (geométrico) |
| :--- | ---: | ---: |
| Cabeza quieta | 58 px | 51 px |
| Giro leve ±8° | 460 px | 51 px |
| Giro medio ±16° | 672 px | 51 px |
| Lateral ±60 mm | 930 px | 52 px |
| Acercarse/alejarse | 222 px | 51 px |
| Movimiento combinado | 946 px | 53 px |

(Con el ruido real de landmarks, ≈ 1 px: 610 px frente a 193 px.) El error ya
no depende del movimiento. `validacion.py` repite esta medición con el código
de producción en cada ejecución.

### Del ruido a las fijaciones

* **Filtro 1€** sobre el vector geométrico: reduce el ruido ~4× sin añadir lag
  en las sacadas.
* **I-DT** (por dispersión) en lugar de I-VT: con 15–30 fps y ruido de decenas
  de píxeles, I-VT etiqueta casi todo como sacada (localiza 0 de 6 fijaciones
  conocidas; I-DT, 6 de 6).
* Las muestras de **parpadeo** se excluyen para no contaminar las fijaciones.
* El grabador lee un histórico **no destructivo** (`get_since`): GUI,
  calibración y grabación no compiten por las muestras (0 % de pérdida medida).

### De fijaciones a heatmaps

1. Las filas consecutivas `Fixation` se agrupan en una fijación (centroide +
   duración; sólo se conservan las ≥ 150 ms).
2. Se suma cada fijación en un lienzo y se aplica un desenfoque gaussiano
   (σ = 55 px, configurable) y un mapa térmico (azul → rojo).

| Imagen | Responde | Peso de cada fijación |
| :--- | :--- | :--- |
| `_density.png` | ¿**Dónde** miró? | 1 |
| `_dwell.png` | ¿**Cuánto tiempo**? | su duración (ms) |
| `_scanpath.png` | ¿**En qué orden**? | círculos numerados (tamaño = duración) |
| `_pagina.png` | Lo mismo sobre la **página completa** (con scroll) | su duración |

Un scanpath sano avanza en tramos cortos y se detiene en zonas con contenido; si
salta de un lado a otro de la pantalla la estimación es ruidosa.

---

## 📚 Referencias

* MediaPipe Face Landmarker — https://ai.google.dev/edge/mediapipe
* One Euro Filter — Casiez, Roussel & Vogel, CHI 2012
* I-DT — Salvucci & Goldberg, ETRA 2000
* Eye model-based gaze estimation — Guestrin & Eizenman, IEEE TBME 2006
* Webcam eye tracking en usabilidad — Papoutsaki et al., *WebGazer*, IJCAI 2016
