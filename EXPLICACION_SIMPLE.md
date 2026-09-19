# Explicación simple para la presentación

Dos partes: **(1)** lo que cambió y hay que corregir en las diapositivas, y
**(2)** cómo explicar cada una en palabras simples.

---

# 1. Qué cambió en las diapositivas

Tu tabla de "Tecnologías Utilizadas" tiene cosas que **ya no son ciertas**. Si
el profesor pregunta, no vas a poder defenderlas. Corrígelas antes de exponer.

| Dice la diapositiva | Realidad | Qué poner |
| :--- | :--- | :--- |
| **ONNX Runtime** — "modelo de mirada invariante a la cabeza (L2CS-Net)" | ❌ **Ese modelo no existe** (el repositorio de Hugging Face da error). Nunca se usó. | **Borra esa fila.** ONNX no se usa. |
| **OpenCV** — "homografía (findHomography con RANSAC)" | ⚠️ La homografía es sólo un método de respaldo. El que se usa es el **geométrico 3D**. | "estimación de pose cefálica 3D (solvePnP) y proyección geométrica de la mirada" |
| **NumPy** — "regresión polinomial multivariada con Ridge" | ⚠️ Ese era el método viejo. El nuevo ajusta una **transformación afín** sobre la mirada reconstruida en 3D. | "álgebra lineal: reconstrucción 3D del globo ocular y ajuste afín de la calibración" |
| **Algoritmo I-VT** — "separa Fijaciones vs Sacadas" | ⚠️ Se usa **I-DT** (por dispersión), no I-VT. I-VT sólo localizaba 0 de 6 fijaciones. | "Algoritmo **I-DT** (Dispersion-Threshold)" |
| **Pillow (PIL)** — "composición de capturas de fondo" | ❌ **No se usa.** Eso lo hace OpenCV (`cv2.resize`, `cv2.addWeighted`). | **Borra esa fila.** |
| MediaPipe, SciPy, Filtro 1€, Tkinter | ✅ Correctas | Déjalas igual |

**También:**

* La **captura de pantalla de la app** es de la versión 2.0 y muestra
  *"Método: Polinomial / Homografía"*. Ahora hay una opción **"Geométrico
  (recomendado)"** que viene marcada por defecto. **Vuelve a tomar la captura.**
* La diapositiva del CSV dice *"Clasificación algorítmica I-VT"* → cámbialo a
  **I-DT**.
* Las imágenes de la diapositiva de heatmaps parecen ideales, no de una sesión
  real. Si el profesor pregunta *"¿esos datos son tuyos?"* vas a quedar mal.
  Usa los PNG que están en `sessions/` (son de sesiones reales).

---

# 2. Cómo explicarlo, en palabras simples

## Diapositiva 1 — Tecnologías Utilizadas

No leas la tabla. Di una frase por bloque:

> "Usé Python porque es el lenguaje que tiene las librerías de visión por
> computadora. **MediaPipe** es la que encuentra los 478 puntos de la cara y el
> centro del iris en cada cuadro de video. **OpenCV** se encarga de la cámara y
> de calcular hacia dónde está orientada la cabeza en 3D. **NumPy y SciPy** hacen
> los cálculos: el álgebra de la calibración y el difuminado de los mapas de
> calor. Y **Tkinter** es la ventana de la aplicación."

Si pregunta por alguna en concreto, ahí sí entras en detalle. Pero no las
enumeres todas de corrido.

---

## Diapositiva 2 — Funcionamiento de la App

> "La app hace dos cosas. Primero **calibra**: te muestra nueve puntos en la
> pantalla y tienes que mirarlos uno por uno. Con eso aprende la relación entre
> cómo se ve tu ojo y dónde estás mirando. Es como enseñarle a leer tu mirada.
>
> Después **graba**: mientras la persona navega, la app estima hacia dónde mira
> unas 30 veces por segundo y lo guarda en un archivo. La ventana de calibración
> es a pantalla completa para que nada te distraiga, y durante la grabación
> aparece un círculo que sigue tu mirada pero **deja pasar los clics** — así
> puedes usar el navegador con total normalidad."

---

## Diapositiva 3 — Datos del CSV

> "El CSV es el cuaderno de campo de la sesión. Cada fila es un instante, y hay
> unos 30 por segundo.
>
> Las columnas que importan son cuatro: **Gaze_X y Gaze_Y** dicen dónde estaba
> mirando; **Event_Type** dice si en ese momento el ojo estaba **fijo** en algo
> o **saltando** entre dos puntos; y **Face_Detected** es un control de calidad
> — avisa si la cara se dejó de ver, para poder descartar esos tramos."

---

## Diapositiva 4 — Análisis y Visualización de Heatmaps

Esta es la importante. Explícala así:

> "El ojo no mira de forma continua. Va **saltando y deteniéndose**: se queda
> quieto un cuarto de segundo en algo, salta, se vuelve a quedar quieto. A esas
> paradas se les llama **fijaciones**.
>
> La app junta todas las paradas y pinta un **mapa de calor**: dónde se detuvo
> más veces aparece en rojo, y donde casi no miró aparece en azul.
>
> Y el **scanpath** — el de las flechitas — es lo mismo pero **en orden**: cada
> círculo es una parada, el número dice en qué momento ocurrió, y las flechas
> unen una parada con la siguiente. El número 1 es lo **primero** que miró la
> persona.
>
> Genero **tres imágenes** porque responden tres preguntas distintas: el mapa de
> **densidad** dice *dónde* miró, el de **dwell time** dice *cuánto tiempo* se
> quedó en cada zona, y el **scanpath** dice *en qué orden*."

### Si el profesor pregunta "¿y eso para qué sirve?"

> "Para evaluar el diseño de una página. Si el heatmap muestra que nadie mira
> el botón de comprar, o que todos miran primero el banner y no el título,
> eso te dice que el diseño está mal jerarquizado. Es la base de las pruebas
> de usabilidad."

### Si pregunta "¿qué tan preciso es?"

Sé honesto, esto suma puntos:

> "Con una webcam normal la precisión es de unos 2 a 4 grados de ángulo visual.
> Eso alcanza para saber **en qué zona** de la pantalla está mirando y en qué
> orden, que es lo que se necesita para evaluar un diseño. No sirve para medir
> distancias exactas en milímetros — para eso haría falta un eye-tracker
> dedicado."

---

# 3. Las tres frases que resumen el proyecto

Si sólo te queda un minuto:

1. **"Estimo hacia dónde mira una persona usando sólo una webcam."**
2. **"Lo hago con geometría 3D, así que funciona aunque el usuario mueva la
   cabeza."**
3. **"El resultado es un mapa de calor y un recorrido visual ordenado, que sirve
   para evaluar si un diseño funciona."**

---

# 4. Recordatorio antes de presentar

- [ ] **Calibra una vez** al abrir la app (`🎯 Calibrar Ahora`). Si no, verás
      *SIN CALIBRAR*.
- [ ] **Vuelve a tomar la captura de pantalla** de la app (la actual es de la
      v2.0).
- [ ] Corrige la tabla de tecnologías (borra ONNX Runtime y Pillow).
- [ ] Cambia "I-VT" por "I-DT" en la diapositiva del CSV.
- [ ] Haz una **sesión de prueba** y usa esos PNG reales, no los ideales.
- [ ] Si algo falla en vivo, abre `🎥 Debug` y mira ahí.
