# Sistema de reconocimiento de globos — target_cam

**Fecha:** 2026-10-29 · **Rama:** `target-cam-learned-detector` · **Estado:** plan aprobado (incluye
`CONSIDERATIONS_RECOGNITION.md`), en implementación

Este documento reúne en un solo lugar cómo funciona el reconocimiento de los
globos rojos de waypoint en la ESP32-CAM frontal (`firmware/projects/target_cam`),
por qué está construido así, qué está hecho y qué falta. Resume, sin
reemplazarlos, estos documentos:

| Documento | Qué aporta |
|---|---|
| `firmware/projects/target_cam/README.md` | Firmware actual: comandos, CSV v2, registros `#!BLOB`, reentrenamiento de la tabla |
| `docs/artifacts/target_cam_and_tcview.md` | Detector HSV (etapa 0), geometría, protocolo serie, tcview original |
| `docs/artifacts/Balloon_NN_Feasibility.md` | Análisis de viabilidad: opciones A–D, presupuesto de CPU, rango |
| `docs/claude_code_prompt_learned detector.md` | Enunciado de la tarea: etapas 0–3, restricciones duras |
| `docs/learned-detector-report.md` | Lo medido: fases 0, A1, A2, B1, balance de blancos fijo |
| `docs/GUI_REDESIGN.md` | Diseño actual de tcview (4 pestañas, tema, widgets) |
| `CONSIDERATIONS_RECOGNITION.md` | Cambios y pestañas nuevas pedidos para tcview (2026-10-29) |
| rama `recognize-interface` (`c362037`) | Referencia, no copia: etapa 2 (`blob_clf`, `train_clf.py`) y la GUI `train_gui`. Será eliminada; se porta a esta rama lo que sirva |

Convención de este documento, la misma de `docs/`: **medido** son números del
board o de `eval_frames.py` sobre cuadros etiquetados; **razonado** es lo que
se deduce y todavía no se ha probado.

---

## 1. El problema

El blimp debe volar entre cuatro globos de mylar rojos (22", 50–60 cm, centro a
2 m del suelo, en un rectángulo de 4 m × 2 m, arena de 10 m × 5 m). Una
AI-Thinker ESP32-CAM mirando hacia adelante recibe cuadros de 160×120 RGB565 a
12.5 FPS y envía por UART una fila CSV por cuadro con hasta cuatro blobs:
azimut, elevación, tamaño aparente, rango y, desde la etapa 2, una confianza.
Esa fila es lo único que recibirá el ESP32-S3 controlador de vuelo.

Se observaron dos fallos en la etapa 0 (HSV):

1. **Globos no detectados.** El mylar refleja la habitación: el centro del globo
   midió H 304 ± 109°, S 78, V 188 (2026-10-13). Un solo intervalo de tono no
   describe eso, y el preset rojo no vio el globo a 1.5 m.
2. **Detecciones falsas.** Objetos que pasan el test de color (madera oscura,
   piel, paredes rosadas, objetos rojos, otros globos) aparecen como saltos
   repentinos en la salida. El jitter pequeño de cada cuadro se acepta como
   ruido y queda fuera de alcance.

### 1.1 Restricciones duras (del enunciado, no cambian)

- Resolución 160×120 RGB565; 115200 baudios (el ESP32-CAM-MB falla por encima,
  medido 2026-10-14).
- **12.5 FPS con todas las etapas activas:** `dt_ms = 80.0` en cada fila. Un modo
  de recolección de datos puede ir más lento solo mientras esté activado
  explícitamente.
- El detector es **C portable** junto a `blob_detect.c`, probado en el host con
  cuadros grabados. **Sin TensorFlow Lite Micro.**
- **Fallar honestamente:** `ok=0` significa "no hay medición". Una confianza baja
  significa que el blob no se reporta, nunca una posición adivinada.
- **El overlay de tcview es el código de la cámara:** cada etapa se expone por la
  biblioteca compartida (`libblob_detect.so`, ctypes), de modo que tcview
  muestra el veredicto de la cámara y no una reimplementación en Python.
- El CSV está versionado; los modelos y ajustes son registros con versión y CRC;
  un registro inválido se rechaza y se cae a la etapa anterior.
- Las escrituras a flash requieren palabras completas (`!save`); cualquier carga
  de modelo necesita su propio comando protegido con CRC.

### 1.2 Presupuesto de la CPU

| Recurso | Valor | Tipo |
|---|---|---|
| CPU | ESP32 LX6, 2 núcleos, 240 MHz, **sin SIMD** | conocido |
| Período de cuadro | 79.968 ms (el sensor es el reloj) | medido |
| Etapa 1, pasada de píxeles `t_px_us` | 4.7 ms, constante | medido 2026-10-15 |
| Etapa 1, total `proc_us` | ≈ 8.8 ms | medido |
| Características de blob `t_b_us` | ≈ 1.9 ms | medido 2026-10-18 |
| `loop_us` con características activas | p50 14.1 ms, máx 14.2 ms (≈ 18 % del período) | medido |
| RAM interna | ≈ 520 KB; ya ocupada por 2 × 38.4 KB de frame buffers y 28 KB de workspace | conocido |
| Flash de la app | ≈ 64 % libre de 1 MB | medido |
| Enlace | fila CSV v2 ≈ 11.5–12.2 ms en el cable, ≈ 16 % de 115200 | medido |

Quedan unos **65 ms libres por cuadro**. Cada 10 ms añadidos son +10 % de ciclo
de trabajo, y la placa se calienta: hay que medir temperatura, no solo tiempo.
ESP-NN/ESP-DL apuntan sobre todo al S3 y al P4; en el ESP32 original se asume
velocidad de C plano (razonado, de memoria de la documentación de Espressif).

---

## 2. Arquitectura: una cascada de etapas

```
OV2640 ──RGB565 160×120──> frame buffer
        │
  [0] test de píxel HSV ──┐
  [1] tabla de color ─────┴─> máscara ─> celdas 2×2 ─> union-find ─> 4 blobs mayores
                                                                        │
  [2] características (12) por blob ─> clasificador ─> confianza ─> filtra
                                                                        │
  [3] recorte 24×24 (o 16×16) de los 2 mejores ─> CNN int8 ─> confianza ─> filtra
                                                                        │
            az/el (pinhole), rango = 550 mm · f / lado mayor ─> fila CSV v2
```

**Por qué una cascada y no una red sobre el cuadro completo.** Incluso el modelo
de detección de personas más pequeño de TensorFlow (MobileNet v1 0.25, 96×96)
cuesta ≈ 7.5 M multiplicaciones-acumulaciones (MAC), es decir 125–300+ ms por
cuadro en este chip. No cabe en 80 ms. En cambio, el detector de color es un
generador de propuestas barato (unos ms), y cada etapa posterior solo mira lo
que la anterior ya encontró: 4 blobs como mucho, y 2 recortes para la CNN. El
costo queda acotado por el número de blobs, no por el cuadro.

**Fallback por prefijo** (`tc_pipeline.c`). Una etapa necesita todos los modelos
inferiores, así que la etapa que corre es
`min(solicitada, mayor N con modelos 1..N válidos)`. Un modelo que falla su
verificación al cargar se pasa como `NULL`. La etapa efectiva sale en cada fila
(`stage`) y en `#!CFG` (`stage_eff`), así que una caída nunca es silenciosa.

**Un solo punto de entrada** (`tc_pipeline_run()`), compilado igual en la
cámara y en el PC. Si las etapas se cablearan en `target_main.c`, el visor
tendría que repetir ese cableado en Python y podría discrepar de la cámara.

**Formato único de registro de modelo** (`tc_model_open()`): cabecera de 20 B
(magic de 4 caracteres, versión, tamaño de cabecera, tamaño del payload,
train id, CRC-32 de cabecera + payload), little-endian leído byte a byte. Se usa
igual para un modelo compilado en el firmware (`main/models/<x>_model.h`) que
para uno subido a flash en el futuro.

**Lo que ya está preparado para las etapas 2 y 3** (fase 0, hecho):
`!stage <0-3>` guardado en NVS; CSV v2 con las columnas `stage`, `conf`,
`b2_conf`…`b4_conf`, `t_px_us`, `t_b_us`, `t_c_us`, `loop_us`, `csv_v`;
`tc_models_t` con los punteros `clf` y `cnn`; `#!CFG` con `clf=` y `cnn=`
(`ok | none | bad`) y `cnn_k=`. **Añadir las etapas 2 y 3 no requiere cambiar el
formato CSV.**

---

## 3. Etapa 0 — Detección en el espacio de color HSV

**Estado: implementada, probada en host y en board.** `main/blob_detect.{c,h}`.

### 3.1 Cómo funciona

1. **Test de píxel.** RGB565 big-endian, cada canal expandido a 8 bits por
   replicación de bits (`r = r5<<3 | r5>>2`). Un píxel coincide si
   `V = max ≥ val_min`, `S = (max−min)·255/max ≥ sat_min` y la distancia
   circular de tono a `hue` es ≤ `tol`. Los tests van del más barato al más caro;
   la saturación se compara como `(max−min)·255 ≥ S_min·max`, sin división, de
   modo que la mayoría de los píxeles se rechazan sin dividir.
2. **Agrupación en celdas 2×2.** Una celda está activa si al menos la mitad de
   sus 4 píxeles coincide: desaparece el moteado de un píxel y el etiquetado
   cuesta cuatro veces menos.
3. **Componentes conexas** por 4-conectividad con union-find. Los arreglos de
   etiquetas están dimensionados para el peor caso (un tablero de ajedrez de
   cuadro completo), así que ninguna entrada puede desbordarlos (verificado con
   ASan).
4. **Los 4 blobs mayores** de al menos `min_area` (25 px). Centroide y caja
   exactos a partir de los **píxeles** coincidentes del blob, incluidas las
   celdas de borde por debajo del umbral que lo tocan. Sin esa pasada de borde,
   los bordes curvos perdían ≈ 3 % del área y sesgaban el centroide hasta
   0.07 px; con ella el error es 0.000 px (suite del host).
5. **Geometría** (modelo pinhole). `f = 80 / tan(hFOV/2)` = 157 px a 54°;
   `az = atan((cx−80)/f)`, `el = atan((60−cy)/f)`;
   `range = 550 mm · f / max(w, h)`, **−1 si la caja toca el borde** (un globo
   recortado parece más pequeño y el rango saldría largo).

### 3.2 Fundamento

- **Tono en lugar de RGB:** un globo rojo en sombra es más oscuro (baja V), no
  menos rojo (H se mantiene). Los pisos de S y V eliminan grises y casi-negros,
  cuyo tono es ruido.
- **Cuatro blobs y no uno:** hay cuatro globos idénticos. El mayor suele ser el
  más cercano, que no siempre es el siguiente waypoint; elegir es tarea del
  navegador en el S3.
- **Rango por el lado mayor de la caja, no por el área:** en mylar, el brillo
  especular blanco y el lado oscuro fallan el test de color; el área subestima
  el globo (rango 10–13 % largo en simulación), la caja abarca el hueco (5–11 %
  largo). El sesgo restante es sistemático y se calibra con `d<mm>` contra un
  globo a distancia medida.
- **Es la red de seguridad.** No necesita ningún modelo, sus deslizadores se
  reajustan en minutos en la sede, y es la etapa a la que caen todas las demás.

### 3.3 Lo medido

- Sobre 20 cuadros etiquetados (`red_s1`, `red_s2`), **la elección de la
  calibración hace variar los blobs falsos de 0 a 38** en los mismos cuadros, y a
  nivel de píxel el mejor ajuste capta solo la mitad de cada globo
  (report §4).
- Píxeles falsos: sobre todo madera oscura (tono 23–51, V 41–90). Píxeles
  perdidos: reflejos azul-púrpura oscuros, el lado en sombra, los brillos
  blancos (report §5).

---

## 4. Etapa 1 — Tabla de color aprendida

**Estado: implementada, probada en host y en board.** `main/color_lut.{c,h}`,
`main/models/lut_model.{h,bin,json}`, `host/train/train_lut.py`.

### 4.1 Cómo funciona

Reemplaza el test HSV del paso 1 por una consulta: un bit por cada uno de los
65 536 valores RGB565, 8 KB. Todo lo demás (celdas, etiquetado, área mínima,
orden) es el de la etapa 0, así que cualquier cambio en lo reportado viene solo
de qué píxeles coinciden. La tabla se compila en el firmware como registro de
modelo (magic `TCLU`), se verifica al arrancar (magic, versión, tamaño, CRC-32)
y se copia a un buffer de 8 KB en DRAM para que las consultas aleatorias no
pasen por la caché de flash. Si se rechaza, `#!CFG` dice `lut=bad` y `!stage 1`
cae a 0.

**Entrenamiento** (`train_lut.py`):

1. Contar, por sesión, etiquetas de globo y de fondo para cada valor RGB565. Los
   píxeles del borde sin etiquetar (banda de 2 px) no cuentan.
2. Suavizar ambos conteos con una gaussiana 3-D en la rejilla 32×64×32: los
   cuadros muestran ≈ 2 500 colores de globo distintos y el siguiente cuadro
   mostrará sus vecinos.
3. Decidir: un color entra si es `k` veces más probable entre píxeles de globo
   que de fondo y tiene al menos `min_evidence` de evidencia de globo.
4. `sigma`, `k` y `min_evidence` se eligen por **validación dejando una sesión
   fuera**, maximizando el F1 de píxel promedio; se excluye cualquier ajuste que
   produzca un blob 1 falso en un cuadro vacío no visto.

**Tabla actual:** train id **1792216162**, entrenada en `red_s1`–`red_s4`,
sigma 2, k 30, min_evidence 1.0, 3 005 colores aceptados. `red_s5` (comedor, de
noche) se dejó fuera como examen de la etapa 2. Un test del visor regenera los
bytes comprometidos desde `host/testdata/`.

### 4.2 Fundamento

- Un píxel RGB565 solo tiene 65 536 valores, así que **cualquier** decisión por
  píxel, por compleja que sea, cabe en una tabla. El modelo corre una vez en el
  PC; la cámara solo consulta la respuesta. El conjunto aceptado puede tener
  cualquier forma; el test HSV es una caja convexa.
- En los cuadros, el rojo del globo está en tono 330–360 con S ≥ 100, la madera
  en 20–50, los reflejos son púrpura oscuro: una tabla puede incluir
  púrpura/magenta y excluir naranja-marrón; una caja HSV no puede hacer ambas
  cosas.
- **Lo que no puede arreglar:** colores que realmente se solapan (rojo oscuro
  del globo contra madera oscura; brillo blanco contra pared blanca). Eso le toca
  a la etapa 2, que mira forma y superficie.
- **Costo:** se pierden deslizadores legibles y reajustables en la sede. Por eso
  la etapa 0 sigue disponible con `!stage 0`.

### 4.3 Lo medido

| Dirección (primera tabla, sigma 8, k 7) | Detector | TP | FN | FP | px P | px R | px F1 |
|---|---|---|---|---|---|---|---|
| s1 → s2 | HSV, mejor en s1 | 7 | 0 | 1 | 48.3 % | 60.8 % | 53.8 % |
| s1 → s2 | tabla entrenada en s1 | 7 | 0 | 1 | 73.8 % | 60.0 % | 66.2 % |
| s2 → s1 | HSV, preset de s2 | 6 | 1 | 0 | 92.4 % | 44.9 % | 60.4 % |
| s2 → s1 | tabla entrenada en s2 | 7 | 0 | 1 | 91.9 % | 63.4 % | 75.0 % |

En el board (escena fija, 2026-10-15): 80.0 ms en todas las filas; pasada de
píxeles de 8.5 a **4.7 ms** y constante; globo encontrado en el **100 %** de las
filas contra 43 % con HSV; 0 saltos contra 82. Persiste un **segundo blob falso
en madera** en cada fila: color compartido, caso para la etapa 2.

### 4.4 Lo pendiente en esta etapa

- **Balance de blancos fijo** (`w2`, `Wc`, hecho en firmware 2026-10-18/19,
  convergió a 81,64,76 en el board). Se descubrió que un mismo globo derivaba
  ≈ 25° de tono entre habitaciones con AWB congelado a ganancias desconocidas
  (report §10.7). Pasos siguientes según el report §10.8: verificar `W` en modo
  auto con escena cálida y fría; `Wc` + `!save` con la luz de vuelo; capturar
  cuadros etiquetados con ganancias fijas y **puntuar la tabla actual antes** de
  decidir reentrenar.
- Generalización a la sede de competencia: no establecida. El plan es reentrenar
  allí (README, "Retraining the colour table").

---

## 5. Etapa 2 — Clasificador de blobs por forma y superficie

**Estado: B1 hecho en esta rama; B2 implementado en la rama de referencia
`recognize-interface` y en port a esta (fase P1, §9).**
Hecho aquí: `main/blob_feat.{c,h}`, `!collect`, registros `#!BLOB`, pestaña
Blobs, `tcview/blobset.py`, `host/tools/blobs_from_frames.py`, verificación
board = PC. En la referencia: `main/blob_clf.{c,h}` (regresión logística,
registro `TCBC`, guarda el `lut_id` de su tabla), la etapa 2 en
`tc_pipeline.c`, la carga en `target_main.c` (`clf=ok|none|bad|mismatch`,
`clf_id`), `host/train/train_clf.py` y sus tests.

### 5.1 Cómo funciona (diseño)

Para cada uno de los ≤ 4 blobs reportados por la etapa 1:

1. **12 características** (`blob_features()`, ya implementado, versión
   `BLOB_FEAT_VERSION = 1`):

   | # | Nombre | Qué mide y por qué separaría un globo |
   |---|---|---|
   | 0 | `fill` | área / caja; una esfera llena π/4 = 0.785, una puerta ≈ 1, una pata de silla mucho menos |
   | 1 | `log_aspect` | ln(w/h); 0 para un globo, negativo para una puerta, positivo para un borde de mesa |
   | 2 | `hue_spread` | desviación circular del tono; el mylar refleja la habitación y varía más que la pintura |
   | 3 | `sat_mean` | saturación media |
   | 4 | `sat_std` | su desviación; mylar especular alta, pintura baja |
   | 5 | `val_mean` | brillo medio |
   | 6 | `highlight` | fracción de la caja brillante y casi gris (V ≥ 200, S < 60): el brillo especular de una esfera |
   | 7 | `edge` | gradiente de luma en el borde; un globo contra la pared tiene borde nítido |
   | 8 | `texture` | gradiente en el interior; pintura lisa baja, arrugas de mylar y tela estampada altas |
   | 9 | `ring` | fracción del anillo de 2 px fuera de la caja que pasa el test de color; alta para un fragmento de una región roja mayor |
   | 10 | `cy_norm` | fila del centroide / alto; los globos flotan, el mobiliario está abajo (válido solo porque el montaje es fijo) |
   | 11 | `log_area` | ln(área); condiciona a las demás (un blob de 30 px no tiene textura que mostrar) |

   Aritmética entera por píxel (tabla seno Q14 en vez de `sinf/cosf`), así que la
   cámara y el PC obtienen las mismas sumas; solo `logf`/`sqrtf` finales pueden
   diferir en un ulp. El costo está acotado por el blob: se muestrea con a lo
   sumo 1 024 puntos.
2. **Clasificador:** regresión logística o MLP de 2 capas sobre las 12
   características estandarizadas. Unos cientos de MAC por blob: microsegundos.
   Sale una confianza 0–1 por blob, en `conf`, `b2_conf`…
3. **Filtrado:** un blob con confianza por debajo del umbral **no se reporta**.
   Si ninguno pasa, `ok=0`.

### 5.2 Fundamento

- Los blobs falsos que sobreviven a la tabla (una puerta roja, una silla, una
  camisa, madera oscura) difieren de un globo de mylar en **forma y superficie,
  no en color**: su color ya pasó el test.
- **Clasificador pequeño y no CNN primero:** es casi gratuito en CPU, cada
  característica es interpretable (se puede explicar por qué rechazó un blob) y
  necesita muchos menos ejemplos etiquetados.
- **Características en C portable compartido:** el clasificador se entrena con
  los números que la cámara calculará en vuelo, no con una reimplementación en
  Python. Verificado en board: 4 de 4 registros iguales, diferencia máxima
  4×10⁻⁶ relativa, recortes idénticos bit a bit (report §10.6).
- `fv` viaja con cada registro y dataset; `blobset.save` rechaza mezclar
  versiones, así que nunca se alimenta el clasificador con características
  calculadas con otras reglas.

### 5.3 Lo medido (ensayo, 2026-10-18)

Tres sets de blobs en vivo (491, 589, 487 blobs), regresión logística entrenada
en dos y puntuada en el tercero:

| Set fuera | Precisión promedio | Con p ≥ 0.5 |
|---|---|---|
| 15:42 | 1.000 | 0 de 5 negativos pasan (muy pocos) |
| 22:16 | 0.684 | P 0.59, R 0.20 |
| 22:25 | 0.617 | P 0.65, R 0.99; 160 de 192 negativos pasan |

**Las características separan dentro de una escena** (AP 0.70–0.99) **y fallan
entre escenas.** Los negativos eran sobre todo otros globos (rosado, naranja),
que tienen la misma forma; solo los distingue el color, y el tono del propio
globo rojo se movía más entre escenas (−23° a +3°) que la distancia al otro
globo. Causa razonada: balance de blancos. Decisión del usuario: fijar las
ganancias (hecho, §4.4) y **volver a recolectar los sets de B2** con ganancias
fijas; los tres sets actuales quedan como lote de prueba.

### 5.3b Primera corrida con ganancias fijas (2026-10-29, report §11)

- Con la tabla del firmware, la etapa 1 pone un blob 1 falso en **77 de 102**
  cuadros sin globo rojo (sobre todo los globos naranja y rosado). Una tabla
  reentrenada con `s1`+`s3`+`s4` no mejora: el color solo no los separa.
- El clasificador baja el costo con sesión fuera de 94 a 83: casi nada. Las 12
  características son de forma y superficie, y otro globo tiene las mismas.
- Dentro de `s3` (unas mismas ganancias) el tono medio sí separa: rojo +1°,
  naranja +10°, rosado −34°. Entre `s1` y `s3` el propio rojo se mueve ≈ 20°
  (ganancias 64,64,80 contra 76,64,75). **Hace falta que todas las sesiones
  usen las mismas ganancias**, y probar una característica de tono medio
  (`BLOB_FEAT_VERSION` 2) o la CNN.

### 5.4 Lo que falta construir

| Pieza | Descripción |
|---|---|
| Datos | Cuadros etiquetados con ganancias fijas (ya hay en `data/train/s1`–`s4`, `data/test`) y sets `.npz` de la misma tabla; varias sesiones, incluidos negativos de globos naranja/rosado (pintados como fondo) y madera. `red_s5` reservado como examen. |
| `host/train/train_clf.py` | Lee sets `.npz` (y/o cuadros etiquetados vía `blobs_from_frames`), valida dejando una sesión fuera, elige modelo y umbral, escribe `main/models/clf_model.{h,bin,json}` e informe con importancia de características (para comprobar que no aprende solo `cy_norm`). |
| `main/blob_clf.{c,h}` | Registro de modelo (p. ej. magic `TCCL`): medias y desviaciones de estandarización, pesos, umbral, `fv` esperado. Inferencia en float o punto fijo; C portable. |
| `tc_pipeline.c` | Etapa 2: calcular `conf`, eliminar los blobs por debajo del umbral, rellenar `r`. |
| `target_main.c` | Cargar y verificar el modelo al arrancar, `clf=ok|bad|none` y `clf_id` en `#!CFG`. |
| Tests | Host: modelo dañado rechazado y caída a etapa 1; un modelo que acepta todo da exactamente la salida de la etapa 1; conf idéntica PC/C. Visor: el entrenador reproduce el modelo comprometido. |

---

## 6. Etapa 3 — Red neuronal pequeña sobre recortes

**Estado: no implementada.** Solo existen los recortes (`blob_crop()`,
`blob_crop_down16()`, 24×24 en cada registro `#!BLOB`).

### 6.1 Cómo funciona (diseño)

Sobre los 2 mejores blobs que sobreviven a la etapa 2 (`cnn_k`, hoy 2):

1. Recorte cuadrado de 1.5 × el lado mayor de la caja (mínimo 12 px), centrado en
   el centroide, promediado por área a 24×24 (negro fuera del cuadro). Opción
   16×16 por reducción exacta.
2. CNN int8:

   | Capa | Salida (N = 24) | MAC | Pesos |
   |---|---|---|---|
   | Conv 3×3, 3→8, ReLU | 24×24×8 | 124 416 | 216 |
   | Max-pool 2×2 | 12×12×8 | – | – |
   | Conv 3×3, 8→16, ReLU | 12×12×16 | 165 888 | 1 152 |
   | Max-pool 2×2 | 6×6×16 | – | – |
   | Conv 3×3, 16→16, ReLU | 6×6×16 | 82 944 | 2 304 |
   | Promedio global, densa → 1 | confianza | 16 | 17 |
   | **Total** | | **≈ 373 k** (≈ 166 k a 16×16) | **≈ 3.7 k int8** |

   Activaciones de 4.6 KB como máximo. C plano junto a `blob_detect.c`, sin
   TFLM, probado en la suite del host y llamado desde tcview por ctypes.
3. Su confianza se combina con la de la etapa 2 (regla por decidir) y filtra
   igual: por debajo del umbral, el blob no se reporta.

### 6.2 Fundamento

- Aprende directamente de los píxeles redondez, brillo y gradiente de
  sombreado, para el desorden cuya forma engaña a la etapa 2.
- **Solo si la etapa 2 no basta** (orden recomendado en el análisis de
  viabilidad). Es la etapa más cara y la que más datos necesita.
- **Costo estimado:** 4–10 ciclos por MAC int8 en C plano en el LX6 (estimación
  **sin medir**). A 6 ciclos/MAC: ≈ 9.3 ms por recorte de 24×24, ≈ 4.2 ms a
  16×16; dos recortes a 24 serían ≈ 19 ms, que cabe en los ≈ 65 ms libres pero
  sube el ciclo de trabajo del ≈ 18 % al ≈ 41 %. **Hay que medir los ciclos
  reales por MAC en el board antes de elegir el tamaño del recorte**, y la
  temperatura en una corrida larga.

### 6.3 Lo que falta construir

| Pieza | Descripción |
|---|---|
| Medición previa | Microbenchmark de una conv 3×3 int8 en el board: ciclos/MAC, y si conviene IRAM / núcleo 1. |
| Datos | Los mismos sets `.npz` de la etapa 2 (llevan el recorte). Probablemente necesite aumentación (espejos, brillo) por el bajo número de globos físicos. |
| `host/train/train_cnn.py` | Entrenamiento en float, cuantización int8 por tensor, verificación de que la inferencia en C sobre los pesos cuantizados da lo mismo que la referencia Python, salida `main/models/cnn_model.{h,bin,json}`. Framework: numpy puro (§10). |
| `main/blob_cnn.{c,h}` | Registro (p. ej. magic `TCCN`), capas int8 con escala por tensor, buffers estáticos. |
| `tc_pipeline.c` | Etapa 3, medida en `t_c_us`. |
| Tests | Igual que la etapa 2, más la equivalencia bit a bit C ↔ referencia entera en Python. |

---

## 7. Rango (transversal, no es una etapa)

El análisis de viabilidad concluye que una red **gana poco** en distancia: el
error viene de cuántos píxeles cubre el globo (1 px = 2 % a 2 m, 7 % a 6 m) y de
un sesgo sistemático (5–11 % largo, en simulación). Lo recomendado es una
**corrección ajustada con 3–4 distancias medidas** (lineal en 1/tamaño o tabla
pequeña), sin costo en tiempo de ejecución. Opcional: ajuste de círculo
subpíxel sobre el borde (≈ 1 ms). La FOV (54°) también es supuesta y escala
todos los ángulos y rangos; debe calibrarse.

---

## 8. Interfaz gráfica: tcview integrado para las 4 etapas

### 8.1 Lo que existe

tcview (PyQt6 + pyqtgraph, env `targetcam`) tras el rediseño de
`docs/GUI_REDESIGN.md`: barra de conexión, consola fija abajo, barra de estado y
cuatro pestañas.

| Pestaña | Etapas que cubre hoy |
|---|---|
| **Image & calibration** | 0 (deslizadores HSV, región, `c`), 1 (selector de etapa, banner "Colour table … decides the pixels", chip de MISMATCH), balance de blancos (`Wc`), `!flip` |
| **Flight data** | Todas, pero solo como lectura de la fila CSV: etapa efectiva, `conf`, tiempos por etapa, `dt≠80` |
| **Label frames** | Pintado de etiquetas de píxel (datos para la etapa 1); puntuación detector vs etiquetas para etapas 0 y 1 |
| **Blobs** | Recolección (`!collect`), etiquetado de blobs, vector de 12 características, recorte, verificación board = PC (datos para 2 y 3) |

El entrenamiento y la evaluación (`train_lut.py`, `eval_frames.py`,
`blobs_from_frames.py`, `csv_report.py`) se hacen **por línea de comandos**.

### 8.2 Lo que se construye (decidido 2026-10-29)

**Una sola GUI: tcview.** `train_gui` de la rama de referencia no se porta como
programa aparte; sus funciones pasan a pestañas de tcview. Pestañas finales:

| Pestaña | Estado | Qué hace |
|---|---|---|
| Image & calibration | se modifica | Selector de etapa que decide qué calibraciones se muestran (abajo) |
| Flight data | **sin cambios** | Lo que recibe el S3 |
| Label frames | se modifica | Zoom con lupas, relleno de interior |
| Blobs | se mantiene | Recolección en vivo y etiquetado de blobs |
| **Train** | nueva | Entrenar etapas 1–3 desde carpetas de datos |
| **Verify** | nueva | Evaluar uno o dos modelos sobre carpetas de prueba, con veredicto |
| **Firmware** | nueva | Instalar un modelo en `main/models/`, compilar, flashear, comprobar |

**Image & calibration: calibración por etapa.** Se elige una etapa y se
muestran solo sus controles. La etapa elegida es la que corre la cámara para
tomar datos de la siguiente (para recolectar blobs para la etapa 2 se corre la
tabla con la que se entrenará el clasificador).

| Etapa | Controles |
|---|---|
| 0 | Hue, tolerancia, sat mín., val mín., presets, `c` (como hoy) |
| 1 | Solo lectura: id y colores de la tabla, coincide o no con el PC |
| 2 | Lo de 1 + **umbral del clasificador** (comando nuevo, NVS) e id |
| 3 | Lo de 2 + **umbral de la CNN**, `cnn_k` e id |
| Todas | Balance de blancos (`Wc`), área mínima, tamaño del objetivo, FOV, `!flip` |

**Zoom con lupas − / +** en Image & calibration y Label frames: pasos de ×1.25
desde Fit, botón Fit, Ctrl+rueda. El factor se redondea a la escala entera más
cercana para que todos los píxeles de cámara tengan el mismo tamaño en pantalla.

**Relleno de interior (Label frames):** opción del pincel con semántica de
lazo: al soltar, el trazo se cierra uniendo el último punto con el primero y se
rellena el polígono. Funciona aunque el trazo no cierre exactamente. Con
deshacer.

**Train.** Carpetas de datos (una o varias, por defecto
`firmware/projects/target_cam/data/`), etapa **1, 2 o 3** (la 0 se calibra a
mano), modelo padre cuando la etapa lo necesita (la 2 elige la tabla; la 3, la
tabla), nombre elegido por el usuario y botón Entrenar. El entrenador corre
como proceso aparte con su salida en vivo y el comando equivalente copiable. El
resultado es un **juego de modelos** en `firmware/projects/target_cam/models/<nombre>/`
(§11): entrenar la etapa 2 sobre la tabla X crea un juego nuevo con una copia
de X más el clasificador. Para las etapas 2–3 también se aceptan sets `.npz`,
solo si su `lut_id` coincide con la tabla elegida.

**Verify.** Un modelo (cualquier etapa; la 0 son ajustes HSV) o dos, y una o
varias carpetas de prueba. Muestra blobs TP/FN/FP/split, blob 1 falso en
cuadros vacíos, precisión/recall/F1 de píxel (que solo distinguen las etapas
0–1: las 2–3 quitan blobs, no píxeles), una tabla por sesión y cada cuadro con
la detección dibujada y filtrable por resultado. Veredicto según el README
(FN 0 y ningún blob 1 falso); con dos modelos, cuál es mejor, o **"no se puede
concluir"** cuando una cifra mejora y otra empeora, cuando hay pocos cuadros, o
cuando una carpeta de prueba se usó para entrenar ese modelo (lo advierte).

**Firmware.** Elegir un juego de `models/`, compararlo con lo instalado y con
los ids que reporta la cámara (`#!CFG`), e instalar:
1. el contenido actual de `main/models/` se mueve a
   `models/firmware_<lut_id>_<fecha>/` (si ese juego ya existe con los mismos
   ids, no se duplica); si los ids de `main/models/` no coinciden con los que
   reporta la cámara, se avisa de que el respaldo no es lo que tiene el micro;
2. el juego elegido se copia a `main/models/`;
3. compilar con el Python de ESP-IDF (no está en el env `targetcam`);
4. desconectar el puerto, pedir el paso físico (mantener BOOT, reconectar USB)
   y esperar confirmación, flashear a 115200;
5. reconectar y comprobar en `#!CFG` que `lut_id`, `clf_id` y `cnn_id` son los
   instalados.

Más adelante (fase D): comando protegido `!dump` para leer de la cámara los
registros que tiene, y carga de modelos por serie a NVS.

Reglas que se mantienen: sin dependencias nuevas; el overlay y las
evaluaciones usan el código C de la cámara (`libblob_detect.so`); todo texto
seleccionable con botón de copiar; usable a 1280×720; los textos de la GUI en
inglés como el resto de tcview.

---

## 9. Plan

Cada fase deja el firmware y el visor funcionando, termina con una
verificación (tests del host y del visor; en el board: 0 filas `dt≠80`, tiempos
por etapa, detecciones contra cuadros etiquetados), se hace commit y se detiene
para que el usuario pruebe. Como máximo 3 comandos por paso. Las preguntas que
queden abiertas se hacen al implementar cada funcionalidad.

| Fase | Contenido | Verificación |
|---|---|---|
| **P1 — Etapa 2, núcleo** ✓ | Port desde la referencia: `blob_clf.{c,h}`, etapa 2 en `tc_pipeline`, carga y `clf_id` en `target_main.c`, `lut_id` en los sets de blobs, `train_clf.py`, `eval_frames.py --clf`, tests. `main/models/clf_model.h` como marcador `CLF_MODEL_NONE` mientras no haya clasificador instalado | `make test`, pytest, firmware compila sin warnings |
| **P2 — Biblioteca de modelos y motor** ✓ | `models/` (juegos, metadatos, migración de `tables/`), motor de evaluación compartido por Verify y Train, ejecución de entrenadores en segundo plano | Tests del motor |
| **P3 — Pestaña Train** ✓ | Etapas 1 y 2 (la 3 se activa en P8) | Tests offscreen; un entrenamiento real con `data/train` |
| **P4 — Pestaña Verify** ✓ | Uno y dos modelos, por cuadro, veredicto | Tests; resultados iguales a `eval_frames.py` |
| **P5 — Pestaña Firmware** | Respaldo, instalación, compilación, flasheo guiado, comprobación de ids | Tests con un compilador simulado; **en el board**: instalar tabla + clasificador, `!stage 2` |
| **B3 — Etapa 2 en el board** | `csv_report.py` a `!stage 2`, escena con globo + madera + globo naranja | 0 filas `dt≠80`; `t_b_us`; desaparecen los falsos; el globo se conserva |
| **P6 — Cambios en tcview** | Calibración por etapa (+ comandos de umbral en el firmware, NVS v6), zoom con lupas, relleno de interior | Tests; capturas a 1280×720 |
| **C0 — Medir antes de la CNN** | Microbenchmark de conv 3×3 int8 en el board | Ciclos/MAC medidos; decisión 16 vs 24 y `cnn_k` |
| **P7 — Etapa 3, núcleo** | `train_cnn.py` (numpy), `blob_cnn.{c,h}` (registro `TCCN`, guarda `lut_id`), etapa 3 en `tc_pipeline`, equivalencia bit a bit C ↔ referencia entera en numpy | `make test`, pytest |
| **P8 — Etapa 3 en la GUI y en el board** | Train/Verify/Firmware para la etapa 3; corrida larga | 0 filas `dt≠80`; temperatura ≥ 10 min; mejora medida sobre la etapa 2 |
| **D — `!dump` y carga a NVS** | Leer y escribir registros de modelo por serie, con CRC | Modelo leído = modelo instalado |
| **R — Rango** | Corrección con 3–4 distancias medidas; calibración de FOV | Error de rango antes/después |

En paralelo, del lado de los datos: etiquetar `train/s2/*`, `test/red_s2`,
`test/orange_s2` (hoy sin `.labels.png`), y seguir capturando con `Wc` en cada
sesión.

---

## 10. Decisiones (2026-10-29)

| # | Pregunta | Decisión |
|---|---|---|
| 1 | Orden tras filtrar | Los blobs que sobreviven suben de posición: el blob 1 es el mayor que el filtro cree (como la referencia) |
| 2 | Blobs rechazados | No se reportan; si no queda ninguno, `ok=0` |
| 3 | Umbrales | El modelo trae un umbral por defecto; la cámara acepta un umbral propio por comando, guardado en NVS (P6) |
| 4 | CNN vs clasificador | La CNN se entrena con **todos** los blobs de la etapa 1 (no depende del clasificador) y guarda el `lut_id`. En vuelo corre sobre los `cnn_k` mayores que pasaron la etapa 2; un blob se reporta si pasa ambos umbrales. Qué hacer con los supervivientes fuera de `cnn_k` se decide en P7 |
| 5 | Globos naranja/rosado | Se entrenan como negativos (pintados como fondo) |
| 6 | Framework de la CNN | **numpy puro** (abajo) |
| 7 | GUI | Todo en tcview: pestañas Train, Verify, Firmware |
| 8 | Rango | Fase aparte (R), al final |
| 9 | Carga a NVS | Fase D, después; el flujo normal es compilar y flashear desde la pestaña Firmware |
| 10 | Etapa 0 en Train | No: se calibra a mano |
| 11 | Imagen en Flight data | Descartado; la pestaña queda como está |
| 12 | Dónde se guardan los modelos | `firmware/projects/target_cam/models/<nombre>/`, versionado en git. Al compilar, el juego elegido se copia a `main/models/` y lo que había allí pasa a `models/` |
| 13 | Rama de referencia | Se conserva hasta terminar el port; su `target_cam/CLAUDE.md` (ajeno al proyecto) no se trae |

**Por qué numpy para entrenar la CNN.** La documentación solo fija la
inferencia: C plano int8, sin TFLM, ≈ 3.7 k pesos (análisis de viabilidad §06;
enunciado). El framework de entrenamiento corre en el portátil, así que las
limitaciones del ESP32 no lo condicionan; condicionan la arquitectura y la
cuantización, que son las mismas con cualquier framework. El env `targetcam`
tiene numpy y no tiene torch, sklearn ni scipy. Con numpy: la red es diminuta
(3 convoluciones, cientos o pocos miles de recortes; entrena en CPU en segundos
o minutos); la referencia entera que el C debe igualar bit a bit hay que
escribirla en numpy de todos modos; y no se añade una dependencia de ~200 MB a
un env que ya se rompió una vez y que debe funcionar sin internet en la sede.
El costo es escribir la retropropagación a mano, que se verifica con un test de
gradiente numérico. Si en P7 resultara insuficiente, el entrenador es un
script aislado y puede cambiarse a PyTorch sin tocar el firmware.

---

## 11. Cadena de modelos y despliegue

**Las etapas no son independientes.** Cada modelo depende de los inferiores y
se enlazan por *train id*:

```
cuadros etiquetados (.rgb565 + .labels.png + .json con wb_gain)
   ├─► train_lut.py ─► lut_model.bin  (id L)
   │        el pipeline en C corre la tabla L sobre los cuadros → blobs
   ├─► train_clf.py ─► clf_model.bin  (id C, guarda lut_id = L)
   │        mismos blobs → recortes 24×24
   └─► train_cnn.py ─► cnn_model.bin  (id N, guarda lut_id = L)
```

- Las 12 características se calculan sobre los píxeles que la tabla acepta,
  así que un clasificador vale solo detrás de su tabla; la cámara lo comprueba
  al arrancar (`clf=mismatch` → corre la etapa 1).
- La tabla decide qué blobs existen y su caja, así que la CNN también pertenece
  a una tabla.
- Por debajo de todo están las ganancias de blanco: una tabla aprende colores
  crudos para las ganancias con que se tomaron sus cuadros (`wb_gain` en cada
  `.json`).
- **La fuente de verdad son los cuadros etiquetados.** Los blobs se regeneran
  desde ellos con cualquier tabla nueva. Los sets `.npz` en vivo no sobreviven
  a un cambio de tabla (`blobs_1.npz`, recolectado en etapa 0, no sirve para la
  etapa 2).

**Juego de modelos:** `models/<nombre>/` con `lut_model.*`, `clf_model.*`,
`cnn_model.*` y los logs del entrenamiento; cada `.json` guarda sesiones,
parámetros e id del padre. Se instala completo.

**Qué va al microcontrolador.** Un único binario de firmware, con los modelos
compilados como arreglos C:

| Archivo en `main/models/` | ¿Al micro? | Para qué |
|---|---|---|
| `lut_model.h` | sí | Tabla, 8 KB + cabecera |
| `clf_model.h` | sí | Clasificador, 180 B (o marcador `CLF_MODEL_NONE`) |
| `cnn_model.h` | sí (P7) | ≈ 4 KB de pesos int8 |
| `*.bin` | no | Los mismos bytes, para las herramientas del PC |
| `*.json` | no | Cómo se entrenó |

En NVS van los ajustes de campo: HSV de la etapa 0, `!stage`, ganancias de
blanco, `!flip`, FOV, tamaño del objetivo y, desde P6, los umbrales.

**Qué código ejecuta cada filtro.** `target_main.c` carga y verifica los
modelos al arrancar (`color_lut_load`, `blob_clf_load`, `blob_cnn_load`; un
modelo inválido o de otra tabla queda `NULL`) y en cada cuadro llama a
`tc_pipeline_run()`:

| Etapa | Código |
|---|---|
| 0/1 | `blob_detect.c`: `blob_pixel_matches()` (HSV o bit de la tabla), celdas, union-find, 4 mayores |
| 2 | `blob_feat.c` `blob_features()` → `blob_clf.c` `blob_clf_conf()` → `tc_pipeline.c` descarta y sube |
| 3 | `blob_feat.c` `blob_crop()` → `blob_cnn.c` (P7) → `tc_pipeline.c` descarta y sube |

Los mismos archivos forman `host/libblob_detect.so`, que usan tcview, los
entrenadores y `eval_frames.py`: lo que el PC verifica es lo que la cámara
ejecuta.

---

## 12. No establecido

- Generalización a la sede: todas las sesiones son de una casa, con un globo.
- Si las ganancias fijas resuelven la deriva de tono entre habitaciones.
- Si alguna característica separa globos de desorden real **entre** escenas con
  ganancias fijas (es la medición de B2).
- Ciclos por MAC int8 reales y efecto térmico de la etapa 3.
- Rango más allá de 7.3 m y precisión real del rango (el sesgo es simulado).
- Comportamiento en escenas en movimiento (saltos por minuto): los datos son
  sobre todo cuadros sueltos y escenas fijas.
