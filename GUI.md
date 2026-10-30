# Cómo ejecutar la GUI de target_cam (`tcview`)

`tcview` es el programa de PC para la cámara de globos (`firmware/projects/target_cam`).
Tiene las pestañas de imagen y calibración, datos de vuelo, etiquetado de cuadros,
datos del clasificador, entrenamiento, verificación y firmware (compilar y flashear).
Habla con la ESP32-CAM por el mismo cable USB que se usa para flashear.

Esta guía va de un equipo Linux vacío hasta la GUI abierta. Los comandos están probados
en Arch Linux; se dan también los equivalentes para Ubuntu/Debian. Las rutas son relativas
a la raíz del repositorio salvo que se diga otra cosa.

Qué hace falta:

| Para… | Se necesita |
|---|---|
| Abrir la GUI, calibrar, etiquetar, entrenar, verificar | Miniconda + el entorno `targetcam` + un compilador de C |
| Compilar y flashear desde la pestaña **Firmware** | Además, ESP-IDF v5.5.5 instalado con EIM |

Si solo vas a etiquetar o entrenar, puedes saltarte el paso 2.

---

## 1. Paquetes del sistema

El compilador de C hace falta siempre: la GUI compila `host/libblob_detect.so` (el mismo
código C que corre la cámara) la primera vez que arranca. El resto son los requisitos de
ESP-IDF y de Qt.

**Ubuntu / Debian:**

```bash
sudo apt update
sudo apt install -y build-essential git wget flex bison gperf python3 python3-pip \
    python3-venv cmake ninja-build ccache libffi-dev libssl-dev dfu-util \
    libusb-1.0-0 libxcb-cursor0
```

**Arch Linux:**

```bash
sudo pacman -S --needed base-devel git wget flex bison gperf python python-pip \
    cmake ninja ccache dfu-util libusb xcb-util-cursor
```

**Acceso al puerto serie:** tu usuario tiene que estar en el grupo del puerto. En Ubuntu y
Debian el grupo es `dialout`; en Arch es `uucp`. Después hay que cerrar la sesión y volver
a entrar.

```bash
sudo usermod -aG dialout $USER     # Ubuntu / Debian
sudo usermod -aG uucp $USER        # Arch
```

---

## 2. Herramientas de ESP32 (ESP-IDF v5.5.5 con EIM)

La pestaña **Firmware** compila con `idf.py` y flashea con `esptool`. Busca ESP-IDF a través
del script de activación que escribe el instalador oficial EIM (ESP-IDF Installation Manager),
en `~/.espressif/tools/activate_idf_v5.5.5.sh`. Por eso hay que instalar ESP-IDF con EIM y
no a mano.

### 2.1 Instalar EIM

**Ubuntu / Debian:** desde el repositorio APT de Espressif.

```bash
echo "deb [trusted=yes] https://dl.espressif.com/dl/eim/apt/ stable main" | \
    sudo tee /etc/apt/sources.list.d/espressif.list
sudo apt update
sudo apt install -y eim
```

**Cualquier distribución:** descarga el binario `eim` de Linux de
<https://github.com/espressif/idf-im-ui/releases>, dale permiso de ejecución y ponlo en el `PATH`:

```bash
chmod +x eim
sudo mv eim /usr/local/bin/
```

Comprueba la instalación:

```bash
eim --version
```

### 2.2 Instalar ESP-IDF v5.5.5

```bash
eim install -i v5.5.5 -t esp32,esp32s3 -n true
```

- `-i v5.5.5` es la versión con la que está hecho todo el firmware del proyecto.
- `-t esp32,esp32s3` instala los compiladores de los dos chips: la ESP32-CAM es `esp32` y el
  controlador de vuelo es `esp32s3`.
- `-n true` hace la instalación sin preguntas.

Si prefieres elegir la carpeta de instalación con un asistente, ejecuta `eim wizard`. EIM deja
siempre el script de activación en `~/.espressif/tools/`, sea cual sea la carpeta de ESP-IDF;
ese script ya conoce su propio `IDF_PATH`.

La descarga ocupa varios GB y tarda unos minutos.

### 2.3 Comprobar que compila

```bash
source ~/.espressif/tools/activate_idf_v5.5.5.sh
cd firmware/projects/target_cam
idf.py set-target esp32      # solo la primera vez, con la carpeta build/ vacía
idf.py build
```

Si termina con `Project build complete`, la pestaña **Firmware** podrá compilar. No hace falta
activar ESP-IDF antes de abrir la GUI: la pestaña lo activa por su cuenta en cada compilación.

---

## 3. Miniconda

La GUI corre en un entorno conda propio, `targetcam`, separado de cualquier otro Python del
sistema y del entorno de ESP-IDF.

### 3.1 Instalar Miniconda

```bash
cd ~
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p ~/miniconda3
~/miniconda3/bin/conda init bash      # o zsh / fish, según tu shell
```

Cierra la terminal y abre una nueva. El prompt debe empezar por `(base)`. Comprueba con:

```bash
conda --version
```

Las versiones recientes de conda piden aceptar los términos de uso de los canales de Anaconda
antes del primer `create`. Si `conda create` te lo pide, acéptalos con:

```bash
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r
```

### 3.2 Crear el entorno `targetcam`

```bash
conda create -n targetcam python=3.10 -y

# ANTES de cualquier pip install: dejar fuera los paquetes de usuario y los de ROS
conda env config vars set PYTHONNOUSERSITE=1 PYTHONPATH= -n targetcam

conda activate targetcam
cd firmware/projects/target_cam/host/viewer
pip install -r requirements.txt
```

`requirements.txt` instala pyserial, numpy, PyQt6, pyqtgraph y pytest con versiones fijas.

- **`PYTHONNOUSERSITE=1`** evita que se mezclen paquetes instalados con `pip install --user`.
  Ya rompieron otro entorno de este proyecto una vez.
- **`PYTHONPATH=` vacío es a propósito.** El setup de ROS mete su propio `site-packages` en
  `PYTHONPATH` y su plugin de pytest rompe las pruebas de este entorno. Al activar el entorno,
  conda avisa de que "sobrescribe variables de entorno"; ese aviso es normal.
- El entrenamiento (`host/train/`) solo usa numpy, que ya está en este entorno. No hace falta
  PyTorch ni ninguna GPU.

### 3.3 Comprobar el entorno (opcional, sin hardware)

```bash
conda activate targetcam
make -C firmware/projects/target_cam/host lib       # compila libblob_detect.so
cd firmware/projects/target_cam/host/viewer
python -m pytest tests -q
```

Todas las pruebas deben pasar. Usan una cámara simulada y no necesitan la placa.

---

## 4. Ejecutar la GUI

Cada vez que vayas a usarla:

```bash
conda activate targetcam
cd firmware/projects/target_cam/host/viewer
```

Y luego uno de estos comandos:

```bash
python -m tcview --port /dev/ttyUSB0     # la cámara real, a través del ESP32-CAM-MB
python -m tcview --fake                  # cámara simulada: para probar sin hardware
python -m tcview --label                 # solo etiquetar cuadros guardados (carpeta target_cam/data)
python -m tcview --label RUTA/A/CUADROS  # etiquetar los cuadros de otra carpeta
```

**Cómo saber el puerto:** conecta la cámara y ejecuta

```bash
ls /dev/ttyUSB* /dev/ttyACM*
```

Según el chip USB de la placa MB, el puerto es `/dev/ttyUSB0` (CH340) o `/dev/ttyACM0`.
Si aparecen varios, desconecta la cámara, repite el comando y mira cuál desaparece.

Abrir el puerto **no reinicia** la cámara: la GUI baja DTR y RTS antes de abrirlo. La cámara
arranca siempre a 115200 baudios y la GUI también.

`python -m tcview --help` muestra todas las opciones.

---

## 5. Flashear la cámara desde la GUI

En la pestaña **Firmware**:

1. Elige el juego de modelos de `target_cam/models/` y pulsa **Install into main/models**.
   El juego que estaba en `main/models` se guarda antes en `target_cam/models/`, así que no
   se pierde nada.
2. Pulsa **Build**. Compila con ESP-IDF; la primera vez tarda unos minutos.
3. Pulsa **Flash…** y sigue el asistente. Te pedirá mantener pulsado el botón **BOOT** de la
   placa MB mientras desconectas y vuelves a conectar el USB. Después te pedirá soltarlo y
   reconectar otra vez para arrancar el firmware nuevo.

Con un firmware de esta rama en la cámara también puedes **subir modelos sin flashear**
(sección *Camera models (no flashing)*). Los detalles están en
`firmware/projects/target_cam/README.md` y en `RECOGNITION.md`.

---

## 6. Problemas frecuentes

| Síntoma | Causa y solución |
|---|---|
| Error sobre el *platform plugin* `xcb` al abrir la GUI | Falta `libxcb-cursor0` (Ubuntu) o `xcb-util-cursor` (Arch). |
| `Permission denied` al abrir `/dev/ttyUSB0` | Tu usuario no está en `dialout` / `uucp`, o no has vuelto a iniciar sesión después de añadirlo. |
| `cc: command not found` o fallo al compilar `libblob_detect.so` | Falta el compilador: `build-essential` (Ubuntu) o `base-devel` (Arch). |
| La pestaña Firmware dice que no encuentra el script de activación | ESP-IDF no se instaló con EIM, o no está en `~/.espressif/tools/activate_idf_v*.sh`. Repite el paso 2.2 o indica la ruta del script en la pestaña. |
| La pestaña Firmware avisa de un `sdkconfig` desactualizado | El `sdkconfig` local es anterior a la tabla de particiones nueva. Acepta regenerarlo: el viejo queda como `sdkconfig.old`. |
| El flasheo no conecta | La placa no entró en modo bootloader. Mantén BOOT pulsado **mientras** reconectas el USB, no después. |
| `pytest` falla con errores de `launch_testing` | `PYTHONPATH` trae paquetes de ROS. Repite el `conda env config vars set ... PYTHONPATH=` del paso 3.2 y reactiva el entorno. |
| Aviso *Colour table MISMATCH* en Image & calibration | La cámara y `main/models` tienen tablas distintas. Instala en `main/models` el juego que tiene la cámara, o reflashea. |
