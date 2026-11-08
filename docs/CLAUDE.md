# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An indoor autonomous blimp that has to hover at 2 m (band 1.85–2.25 m). It is a two-chip system:

| Path | Chip | Role |
|---|---|---|
| `firmware/projects/i2c_tools/` | ESP32-S3 (Waveshare ESP32-S3-Zero) | The flight-controller-to-be: GY-88 IMU/mag/baro, altimeter, four DRV8833 thrusters. The name comes from the Espressif `i2c_tools` example it grew out of, and the console prompt is still `i2c-tools>`. |
| `firmware/projects/optflow_cam/` | ESP32-CAM (AI-Thinker, original ESP32) | Downward optical flow. It prints CSV over UART to a laptop. |
| `firmware/projects/target_cam/` | ESP32-CAM (AI-Thinker, original ESP32) | Forward colour-blob detector for the red waypoint balloons. It prints CSV over UART. `host/viewer/` (`tcview`, its own `targetcam` conda env) shows the raw image over the same serial link and is where the user calibrates by hand. Settings persist in NVS. |
| `firmware/projects/blink/` | — | Stock ESP-IDF example, kept as a toolchain smoke test. |
| `software/` | host | Python bench tools imported from a **separate quadcopter project (esp-drone)**. They speak CRTP-over-UDP to esp-drone firmware, **not** to anything in this repo. See `docs/esp-drone-tools-report.md` §8 for what would need to change to use them here. |

The two chips are not linked yet. `docs/inter-board-link-plan.md` (untracked draft) proposes UART links. `docs/sensor-fusion-plan.md` is a proposal (`blimp_ins`) that is **not implemented**. `hardware/`, `scripts/`, `simulation/`, `tests/` and `external/` are empty placeholders, and the top-level `README.md` is empty.

## Building firmware (ESP-IDF v5.5.5)

ESP-IDF was installed with EIM, so the activation script sits at a non-standard path:

```bash
source $HOME/.espressif/tools/activate_idf_v5.5.5.sh
```

After activation `idf.py` is a shell function, so `timeout`, `xargs` and similar can't exec it. From scripts or tool calls, invoke it directly:

```bash
IDF_PY="$HOME/.espressif/tools/python/v5.5.5/venv/bin/python $HOME/.espressif/v5.5.5/esp-idf/tools/idf.py"
$IDF_PY -C firmware/projects/i2c_tools set-target esp32s3   # once per fresh build dir
$IDF_PY -C firmware/projects/i2c_tools build
$IDF_PY -C firmware/projects/i2c_tools -p /dev/ttyACM0 flash
$IDF_PY -C firmware/projects/optflow_cam set-target esp32
$IDF_PY -C firmware/projects/optflow_cam -p /dev/ttyUSB0 flash
```

- `sdkconfig`, `build/`, `managed_components/` and `dependencies.lock` are gitignored. Persistent config changes go in `sdkconfig.defaults` / `sdkconfig.defaults.<target>` or `Kconfig.projbuild`, never in `sdkconfig`.
- `idf.py monitor` fully buffers its output when not on a TTY. To capture logs non-interactively, read the port with pyserial.
- **ESP32-S3-Zero:** it uses native USB with no UART bridge, so `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y` is required (it is set in `sdkconfig.defaults.esp32s3`). It flashes without the BOOT button. **Opening `/dev/ttyACM0` resets the chip**, so wait about 3 s and discard the boot log before you send console commands.
- **ESP32-CAM:** it flashes through an external programmer and has a GPIO0 bootloader trap. Read `docs/esp32-cam-optical-flow-report.md` §7 before flashing it.

## Tests

The `pytest_*.py` files under `firmware/projects/` are Espressif's stock example tests for other hardware (a CCS811 and an LED). They are not this project's tests.

The real automated test is the optical-flow matcher, run on the host with no hardware:

```bash
make -C firmware/projects/optflow_cam/host test    # verification suite (test_flow_match.c)
make -C firmware/projects/optflow_cam/host sweep   # sub-pixel bias sweep (bias_sweep.c)
```

Everything else is verified on the bench through the S3 console (`help` lists commands; each sensor/motor has its own command). The user does the physical steps, so ask for a physical action, then stop and wait for confirmation before you run anything that depends on it.

## Python tools (`software/`)

Use the conda env described in `software/SETUP.md`: `blimp-tools`, Python 3.10, with `PYTHONNOUSERSITE=1` set **before** any `pip install`, because user-site packages have already broken this env once. Checks that need no hardware:

```bash
python software/drone_dynamics.py      # self-check
python software/motor_diag.py --help
QT_QPA_PLATFORM=offscreen python -c "import sys; sys.path.insert(0, 'software'); import drone_gui"
```

The GUI labels are in Spanish. `optflow_cam/host/flow_monitor.py` is a separate serial viewer/logger and needs `pyserial`.

## S3 firmware architecture (`i2c_tools`)

- `app_main` (`i2ctools_example_main.c`) creates the one I2C master bus (`tool_bus_handle`, SDA=4, SCL=5) and starts an `esp_console` REPL. Every peripheral is a `cmd_<name>.c` module that exposes `register_<name>()` plus a small C API for other modules to call, e.g. `baro_read_temperature_c()`. To add a module you need to touch four places: `main/CMakeLists.txt` SRCS, the include, the `register_*()` call and the banner line in `app_main`.
- `motors_init()` runs **first**, before the console. The DRV8833 inputs float from reset while VM is already live, so the driver must claim the pins immediately. A motor init failure is logged, not fatal, so the console survives.
- The motor driver uses **all eight LEDC channels**, one per DRV8833 input, which gives glitch-free reversal. Nothing else may use LEDC. The status LED is a WS2812 on GPIO21, driven by RMT through `led_strip`.
- **Motor failsafe** (`CONFIG_MOTOR_FAILSAFE_MS`, default 1000): it is armed only while some motor has a non-zero command. If no thrust command arrives within the window, every motor coasts. Any future controller must keep calling the thrust API (which feeds the failsafe) or the motors will stop.

### Altitude sensing

- **Primary altimeter: VL53L1X time-of-flight**, through the ST ULD driver in `components/vl53l1x_uld/` and the `tof` command. Its datasheet suggests its long-mode range may fall short of 2.25 m under room ambient IR. That is an **open question, to be settled by experimental measurement** at hover height, so don't treat it as decided either way. On the bench (dark room, about 1.57 m), timing budgets below 100 ms read about 5 % short.
- **Backup: Parallax PING))) 28015 ultrasonic**, in `cmd_ping.c`. It is **parked and not yet tested in this role**: it isn't in SRCS because the motors took GPIO 7–14, and its header comment explains how to restore it. It needs temperature compensation (air temperature from the BMP180) and a 5 V SIG divider.
- Either sensor measures slant range, so correct it with tilt: `h = d·cos θ`.

### Hardware facts that shape code

- **The GY-88 keeps its register state across ESP32 resets and reflashes.** Drivers must write ranges/modes explicitly at init and read them back. Never derive scale factors from whatever the chip currently holds.
- I2C addresses: MPU-6050 `0x68` (boots asleep; write `PWR_MGMT_1`=0), HMC5883L `0x1E` (directly on the bus, no MPU bypass), BMP180 `0x77`.
- The code uses the new `driver/i2c_master.h` API. `esp-idf-lib`'s drivers for these chips use the legacy `driver/i2c.h` API, so don't mix the two on one bus.
- Motors and the sensors' AMS1117 share one 5 V step-up. If the IMU drops out exactly when thrust is applied, suspect a brownout before you suspect code (see the header comment in `cmd_motor.c`).

## Optical flow architecture (`optflow_cam`)

- `main/flow_match.{c,h}` is the SAD block matcher. It is **portable C with no ESP-IDF dependency**, and that is what makes host testing possible. Keep it that way. `optflow_main.c` holds the camera setup, frame pacing, CSV output and single-letter serial commands (`h<mm>`, `f<fps>`, `z`, `s`, `?`).
- The matcher is designed to **fail honestly**: a match on the search boundary is rejected, never clamped. `ok=0` means "no measurement", not "zero motion". `spread_px` is the confidence figure. Preserve these properties.
- The flow is contaminated by rotation (about 35 mm/s per °/s), and the CAM has no gyro, so it reports raw `dx_px`/`dy_px` for de-rotation elsewhere.
- **`optflow_cam/README.md` is partly stale.** `docs/esp32-cam-optical-flow-report.md` supersedes it (§9 lists the disproved figures), and the current `sdkconfig.defaults` (240 MHz CPU, 20 MHz XCLK) differs from the README's thermal table. The committed sub-pixel estimator is `FLOW_SUBPIX_VFIT`. Measurement CSVs live in `optflow_cam/data/`.

## Docs conventions

`docs/` holds long-form technical reports. They are dated, they keep "measured" separate from "reasoned", and they list claims that couldn't be verified. Raw session reports go in `docs/history/`. Reference PDFs in `docs/reference/` are gitignored, and their citations are in `docs/reference/README.md`. Code comments follow the same style: prose explaining *why*, including the hardware reasoning. Match that density when editing.
