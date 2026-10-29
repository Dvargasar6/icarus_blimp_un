# train_gui — entrenar, verificar e instalar la tabla de color y el clasificador

Interfaz gráfica para las etapas 1 (la tabla de color aprendida) y 2 (el
clasificador de blobs) del detector. Hace lo mismo que
`host/train/train_lut.py`, `host/train/train_clf.py` y
`host/tools/eval_frames.py`, con las mismas funciones, pero sin escribir
comandos y mostrando qué fotogramas fallan. La pestaña Firmware instala la
tabla elegida en `main/models`, compila, flashea y comprueba la cámara.

```bash
conda activate targetcam
cd firmware/projects/target_cam
python -m train_gui
```

Necesita `host/libblob_detect.so` (el viewer lo compila solo si falta o está
viejo: `make -C host lib`).

## Carpetas

| Carpeta | Qué es |
|---|---|
| `data/train/`, `data/test/` | Sesiones de captura: una carpeta por sesión, con `.rgb565` + `.json` + `.png` + `.labels.png` por fotograma (se etiquetan en `tcview --label`). La GUI busca hasta tres niveles bajo `data/` (`data/train/Without_AWB/red_s1` también) y agrupa por carpeta padre. Una sesión se identifica por su ruta bajo `data/`, así que `test/red_s1` y `train/Without_AWB/red_s1` no se confunden aunque se llamen igual. Una carpeta sin etiquetas aparece gris y no cuenta. |
| `tables/` | Las tablas entrenadas aquí, una carpeta por tabla con `lut_model.bin` (el registro que la cámara comprueba), `lut_model.h`, `lut_model.json` (cómo se hizo) y `train_log.txt` (la salida del entrenador). Si se le entrena un clasificador, va en la misma carpeta: `clf_model.{bin,h,json}` y `clf_train_log.txt`. Los respaldos del firmware (`firmware_antes_<fecha>`) y las copias de la tabla del firmware (`firmware_id<id>`) también son tablas. |
| `main/models/` | La tabla del firmware (y su clasificador, si tiene). Aparece como "firmware (main/models)". **Solo** la pestaña Firmware la escribe, y solo al pulsar *Instalar* y confirmarlo. |

### Nombre de las tablas

En la sección **Salida** de la pestaña Entrenar, el campo *Nombre de la tabla*
decide la carpeta `tables/<nombre>`:

- **Vacío**: se usa el nombre sugerido, que aparece en gris dentro del campo:
  `tabla_n_r_m_o_j_p_l`, tabla número `n`, entrenada con las sesiones rojas
  `m`, naranjas `j` y rosadas `l`. Varias sesiones del mismo color van unidas
  con guion; ninguna se escribe `0`. Ejemplo: `tabla_3_r_5-7_o_5_p_0` = tabla 3,
  con `red_s5`, `red_s7` y `orange_s5`. Una carpeta que no se llame
  `<color>_s<N>` se añade como `x_<carpeta>`. `n` es el siguiente número libre.
- **Con texto**: ese es el nombre. Solo letras sin tilde, números, `_`, `-` y
  `.`, sin espacios (es un nombre de carpeta y va en el comando copiable). Si
  ya existe o no es válido, el aviso lo dice y *Entrenar* queda desactivado.
- **Sugerido** copia el nombre sugerido al campo, para editarlo (p. ej. añadirle
  `_awb2`).

Con cualquier nombre, `lut_model.json` guarda con qué sesiones y parámetros se
entrenó, y la pestaña Verificar lo muestra bajo la tabla elegida.

## Entrenar

1. Marca las sesiones. Con dos o más, el entrenador elige `sigma`, `k` y
   `min_evidence` dejando una sesión fuera a la vez (*leave-one-session-out*).
   Con una sola hay que marcar **Fijar** y dar los valores (el README del
   proyecto sugiere sigma 8, k 7).
2. El panel "Salida" muestra el nombre que tendrá la tabla y el comando
   equivalente de terminal (botón *Copiar comando*).
3. **Entrenar** lanza `train_lut.py` como proceso aparte; su salida aparece
   en vivo y se guarda en `train_log.txt`. El aviso de arriba resume lo que
   importa:
   - verde: sin blob 1 falso en las sesiones dejadas fuera y ningún globo
     perdido;
   - ámbar: sin blob 1 falso, pero algún globo perdido (FN);
   - rojo: el ajuste elegido está `EXCLUDED` o el entrenador avisó
     `WARNING: every setting puts a false blob 1…`: no volar esa tabla.
4. **Verificar esta tabla →** la deja elegida en la pestaña Verificar.

## Verificar

Una tabla (en etapa 1, o 2 si tiene clasificador) y una o varias carpetas.
Muestra:

- el veredicto según el README (paso 4 de "Retraining the colour table"):
  la tabla cumple si encuentra **todos** los globos (FN 0) y **ningún**
  fotograma sin globo recibe un blob 1 falso. Si alguna carpeta fue de
  entrenamiento de esa tabla, lo dice: ahí las cifras son optimistas (se
  reconoce por la ruta; las tablas anteriores a este cambio solo guardan el
  nombre, y entonces dice "por nombre");
- las tarjetas de totales (fotogramas, blobs TP/FN/FP/split, blob 1,
  píxeles) y la tabla por sesión;
- cada fotograma coloreado por resultado (`ok`, `parcial`, `falso`,
  `perdido`, `b1 falso`), filtrable con *Todos / Fallan / Aciertan*; al
  elegir uno se dibuja con la máscara de la tabla (amarillo), los globos
  etiquetados (cian punteado) y los blobs reportados (verde en un globo,
  rojo si falso; `b1` es hacia donde iría el dirigible);
- *Copiar informe* copia un texto con el mismo formato que `eval_frames.py`.

## Comparar

Dos tablas (A la referencia, B la candidata) y las carpetas. Evalúa las dos,
pone sus totales lado a lado, una tabla por sesión con FN, FP, blob 1 falso y
px F1 de cada una, y decide según el README:

| Veredicto | Cuándo |
|---|---|
| **B es mejor** | B iguala o mejora a A en todas las sesiones y en todas las cifras (blob 1 falso, FN, FP, px F1 con 1 punto de margen) y mejora al menos una. También si A tiene blob 1 falso y B no, sin perder más globos. |
| **A es mejor** | Lo contrario. |
| **Equivalentes** | Todas las cifras iguales. |
| **No se puede concluir** | Una cifra mejora y otra empeora: el README dice que entonces se conserva la tabla anterior. |

Si las dos ponen un blob 1 falso en algún fotograma sin globo, el titular lo
dice: ninguna cumple el criterio, aunque una sea menos mala. Abajo se listan
los fotogramas donde las tablas difieren (otro resultado de blobs, o
precisión/recall de píxeles a más de 5 puntos), y al elegir uno se dibuja
con cada tabla.

### Etapa 2 en Verificar y Comparar

Una tabla con clasificador aparece dos veces en las listas de Verificar y
Comparar: `tabla` (etapa 1, la tabla sola) y `tabla  + clasificador`
(etapa 2). Así se compara "la tabla sola" contra "con clasificador" como dos
tablas: A = `tabla`, B = `tabla + clasificador`. En etapa 2 cada blob dibujado
lleva su confianza (`b1 0.93`); las cifras de píxeles no cambian, porque el
clasificador quita blobs, no píxeles.

## Clasificador

Entrena el clasificador de blobs de la etapa 2 **para una tabla**: las
características de cada blob se calculan con los píxeles que esa tabla
acepta, así que un clasificador solo sirve detrás de la tabla con la que se
entrenó (la cámara lo rechaza detrás de otra: `clf=mismatch`).

1. Elige la tabla. Si es la del firmware, primero se copia a
   `tables/firmware_id<id>/` y el clasificador va ahí; `main/models` no se toca.
2. Marca sesiones **con blobs falsos**: globos naranja o rosados pintados como
   fondo, puertas, lámparas. Cada blob que la tabla encuentra es un ejemplo:
   globo si su centro cae en un globo pintado, falso si no. Si ninguna sesión
   tiene blobs falsos, no hay nada que aprender y el entrenador lo dice.
3. Con dos o más sesiones se buscan `l2` y el umbral dejando una sesión fuera
   a la vez. Si al dejar fuera una sesión las demás tienen solo globos (o solo
   falsos), no se puede buscar y hay que fijarlos.
4. El aviso resume lo que importa, en las sesiones dejadas fuera:
   - verde: el costo (blob 1 falso + blob 1 equivocado + globos perdidos) baja
     frente a la tabla sola, sin blob 1 falso ni globos perdidos de más;
   - ámbar: ayuda, pero aún hay blob 1 falso o pierde globos;
   - rojo: no ayuda; seguir en etapa 1.
5. **Verificar tabla + clasificador →** la deja elegida en Verificar (etapa 2)
   y prepara Comparar con A = la tabla sola y B = con clasificador.

Si la tabla ya tiene clasificador, hay que marcar *Reemplazar*; el anterior
queda como `clf_model_prev.*`.

### Con conjuntos de blobs (.npz)

Además de carpetas de fotos, la lista muestra como «nombre (blobs)» los `.npz`
que haya en `data/` (hasta tres niveles, p. ej. `data/datasets/`): los que se
guardan en la pestaña **Blobs** de tcview. Cada archivo cuenta como una sesión.
El tooltip dice cuántos blobs tiene etiquetados, en qué etapa se recogieron,
detrás de qué tabla y con qué ganancias de blanco.

Para recoger uno que sirva:

1. Instala, compila y flashea la tabla (pestaña Firmware) y comprueba que la
   cámara la tiene.
2. En tcview: `!stage 1`, ganancias de blanco fijas, pestaña Blobs,
   *Collect* (`!collect 1`). Mueve la cámara por globos y cosas que confundan.
3. Etiqueta cada blob (B globo, N no) y guarda el `.npz` en `data/`, uno por
   sesión. tcview guarda con cada blob el id de la tabla de la cámara.

Lo que comprueba la GUI (y `train_clf.py`):

- Un conjunto recogido detrás de **otra tabla** no se puede usar con esta.
- Blobs de **etapa 0 (HSV)** no se usan salvo que marques *Permitir blobs de
  etapa 0*: el HSV encuentra otros blobs que la tabla, así que solo sirve para
  probar.
- Los conjuntos de antes de que tcview guardara la tabla (como
  `data/datasets/blobs_1.npz`) se aceptan con un aviso: no se puede comprobar.
- Sesiones con ganancias de blanco distintas dan un aviso.

Un conjunto de blobs solo contiene los globos que la tabla encontró, así que
«globos perdidos» cuenta los que el clasificador borra, no los que la tabla no
vio. Los blobs sin etiquetar («?») dejan fuera su fotograma. Para medir todo,
pruébalo después en Verificar con carpetas de fotos pintadas.

## Firmware: instalar, compilar, flashear y comprobar

Los pasos 5 y 6 del README, uno por botón y en orden:

1. **Instalar en main/models**: elige la tabla principal. Se comprueba antes
   con el mismo código que la cámara usa al arrancar (y que su clasificador,
   si tiene, sea de esa tabla). Tras confirmar, la tabla actual se respalda en
   `tables/firmware_antes_<fecha>` (salvo que ya esté guardada en `tables/`) y
   se copian `lut_model.*` y `clf_model.*`. Una tabla sin clasificador deja el
   marcador "sin clasificador" en `main/models/clf_model.h`, para que el
   firmware no se quede con una etapa 2 de otra tabla.
   **Deshacer** = elegir el respaldo e instalarlo.
2. **Compilar** (`idf.py build` con el ESP-IDF instalado por EIM). Al
   terminar se comprueba que `build/target_cam.bin` contiene, byte a byte, la
   tabla y el clasificador instalados.
3. **Flashear**: mantén BOOT pulsado en la ESP32-CAM-MB, desconecta y
   reconecta el USB sin soltarlo, y pulsa *Flashear* (esptool a 115200,
   `--before no_reset --after no_reset`). No deja flashear una compilación
   que no contiene lo instalado. Cierra tcview antes: el puerto debe estar
   libre.
4. **Comprobar**: suelta BOOT, reconecta el USB y pulsa *Comprobar*. La GUI
   abre el puerto sin reiniciar la cámara, le pide `k` y compara `lut_id` y
   `clf` de `#!CFG` con `main/models`. Para usar el clasificador en vuelo:
   `!stage 2` y `!save` (en tcview).

El paso 7 (copiar las sesiones a `host/testdata/` y correr los tests del
viewer antes de hacer commit) sigue siendo a mano.

## Pruebas

```bash
(cd train_gui && python -m pytest -q tests)
```
