# Blimp Sensor Bring-Up Report

**Project:** `icarus_blimp_un` (repo root) — autonomous blimp flight controller
**Target:** Waveshare ESP32-S3-Zero (ESP32-S3, Xtensa LX7, 4 MB flash)
**Framework:** ESP-IDF v5.5.5
**Bench project:** `firmware/projects/i2c_tools/`
**Date of this report:** 2026-10-11
**Status:** 4 of 5 sensors verified on hardware; 1 written but not yet wired

---

## 1. Purpose and Scope

`i2c_tools` started as the stock Espressif I²C console example. It has been grown
into the **sensor bench** for the blimp: a single firmware image exposing one
console command per sensor, so each part can be characterised interactively
before any of it is committed to a flight-control architecture.

This report captures everything established on that bench so far — what exists,
what is proven on hardware, what is merely written, and every open question —
so that the flight firmware can be designed from measured facts rather than
datasheet optimism.

**This is a bench instrument, not flight firmware.** Section 10 explains exactly
why, and what has to change.

---

## 2. Hardware Inventory

### 2.1 Microcontroller

| Property | Value |
|---|---|
| Board | Waveshare ESP32-S3-Zero |
| SoC | ESP32-S3 (`CONFIG_IDF_TARGET_ESP32S3`) |
| Flash | 4 MB (`CONFIG_ESPTOOLPY_FLASHSIZE="4MB"`) |
| Console | Native USB Serial/JTAG (`CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`) |
| Secondary console | None (`CONFIG_ESP_CONSOLE_SECONDARY_NONE=y`) |
| FreeRTOS tick | 100 Hz → **10 ms tick** (`CONFIG_FREERTOS_HZ=100`) |
| Main task stack | 7168 B |
| Optimisation | `-Og` debug, assertions enabled |
| Partition table | custom, `partitions_example.csv` (FAT partition for console history) |

**Critical board facts**
- The on-board RGB LED is a **WS2812 on GPIO21** (addressable, not a plain GPIO LED).
- GPIO19/20 are the native USB D−/D+ — **not available** for anything else.
- **ESP32-S3 GPIOs are not 5 V tolerant.** Any 5 V sensor needs level shifting.
- One ESP32-S3-Zero unit in the lab has a **physically dead USB connector**;
  the working unit is the one to use.

### 2.2 Sensors

| Sensor | Part | Bus / pin | Address | Role | Verified |
|---|---|---|---|---|---|
| IMU | MPU-6050 (on GY-88) | I²C GPIO4/5 | `0x68` | accel + gyro + die temp | ✅ |
| Magnetometer | HMC5883L (on GY-88) | I²C GPIO4/5 | `0x1E` | 3-axis field, heading | ✅ |
| Barometer | BMP085/BMP180 (on GY-88) | I²C GPIO4/5 | `0x77` | pressure, temperature, pressure-altitude | ✅ |
| Time-of-flight | VL53L1X (breakout) | I²C GPIO4/5 | `0x29` | close-range / ground proximity | ✅ |
| Ultrasonic | Parallax PING))) 28015 | GPIO6 (single-wire) | — | **primary altimeter** | ❌ not connected |

### 2.3 I²C bus

```
ESP32-S3-Zero
  GPIO4 ── SDA ──┬── GY-88 (MPU-6050 0x68, HMC5883L 0x1E, BMP180 0x77)
  GPIO5 ── SCL ──┴── VL53L1X breakout (0x29)
  3V3   ── VCC
  GND   ── GND
```
- Internal pull-ups enabled (`flags.enable_internal_pullup = true`), plus whatever
  the breakouts carry. Working, but see §9.11.
- Bus speed **100 kHz** for every device.
- `glitch_ignore_cnt = 7`.

`i2cdetect` on a settled bus reports exactly: `1e  29  68  77`.

### 2.4 PING))) wiring (designed, built, NOT yet verified on hardware)

The PING))) is a 5 V part with a **single bidirectional SIG pin**. Its datasheet
explicitly states the interface *"can communicate with 5 V TTL or 3.3 V CMOS
microcontrollers"*, so no active level shifter is required — but the **echo it
drives back is a 5 V output** and must be divided down.

```
  PING)))                                  ESP32-S3
  ┌─────────┐
  │ 5V  ────┼──────────────── 5 V rail (USB VBUS / BEC)
  │ GND ────┼──────────────── GND
  │ SIG ────┼───[ 1.0k ]──┬── GPIO6
  └─────────┘             │
                        [ 1.8k ]
                          │
                         GND
```

Why this works in both directions:
- **Echo (sensor → MCU):** 5 V × 1.8/(1.0+1.8) = **3.21 V** at GPIO6. Safe.
- **Trigger (MCU → sensor):** at the instant we drive the trigger, SIG is
  **high-impedance** on the sensor side, so the divider is unloaded and GPIO6
  presents the full 3.3 V to SIG. The 1.0 k also limits current if both ends
  ever drive at once.
- **Idle:** the 1.8 k holds the line low, so no floating input.

This mirrors Parallax's own Propeller application note, which was revised
*"03/20/2007 to change SIG resistor from 10K to 1K"*. Resistor values chosen from
parts already on hand — no purchases required.

---

## 3. Toolchain and Build Environment

ESP-IDF is installed via **EIM (ESP-IDF Installation Manager)**, which puts it in
a non-standard location:

```bash
# Activation script
source $HOME/.espressif/tools/activate_idf_v5.5.5.sh
```

> **Gotcha that costs time:** after activation, `idf.py` is a **shell function**,
> not an executable on `PATH`. It is therefore invisible to non-interactive
> shells and to scripted builds. Invoke the real thing directly:

```bash
"$HOME/.espressif/tools/python/v5.5.5/venv/bin/python" \
  "$HOME/.espressif/v5.5.5/esp-idf/tools/idf.py" \
  -C firmware/projects/i2c_tools build flash
```

> **Second gotcha:** `idf.py monitor` buffers its output when stdout is not a TTY,
> so it cannot be scripted. Bench testing is driven through small **pyserial**
> helpers that write a command and read until the `i2c-tools>` prompt returns
> (see §8.1).

---

## 4. What Was Created

### 4.1 File inventory

```
firmware/projects/i2c_tools/
├── main/
│   ├── i2ctools_example_main.c      113 lines   (modified: registers + banner)
│   ├── CMakeLists.txt                           (modified: sources + deps)
│   ├── cmd_i2ctools.c               394 lines   (upstream: i2cconfig/detect/get/set/dump)
│   ├── cmd_imu.c                    198 lines   ← MPU-6050
│   ├── cmd_mag.c                    215 lines   ← HMC5883L
│   ├── cmd_baro.c                   307 lines   ← BMP180 + shared temperature export
│   ├── cmd_ping.c                   344 lines   ← PING))) 28015          [NEW]
│   ├── cmd_tof.c                    378 lines   ← VL53L1X                [NEW]
│   ├── led_blink.c                   64 lines   (WS2812 status LED task)
│   └── *.h                                      (one header per command)
└── components/
    └── vl53l1x_uld/                             ← vendored ST driver     [NEW]
        ├── st/VL53L1X_api.c                     byte-for-byte upstream
        ├── st/VL53L1X_calibration.c             byte-for-byte upstream
        ├── include/VL53L1X_api.h                byte-for-byte upstream
        ├── include/VL53L1X_calibration.h        byte-for-byte upstream
        ├── include/vl53l1_types.h               byte-for-byte upstream
        ├── include/vl53l1_platform.h            2 documented edits
        ├── include/vl53l1x_port.h        39 lines
        ├── port/vl53l1x_port.c         212 lines  ← our platform layer
        ├── CMakeLists.txt
        ├── LICENSE.txt                          ST SLA0103 (BSD-style)
        └── README.md                            provenance + usage
```

Total hand-written driver/command code: **~1 450 lines**.

### 4.2 `main/CMakeLists.txt` (final)

```cmake
idf_component_register(SRCS "i2ctools_example_main.c"
                            "cmd_i2ctools.c"
                            "led_blink.c"
                            "cmd_imu.c"
                            "cmd_mag.c"
                            "cmd_baro.c"
                            "cmd_ping.c"
                            "cmd_tof.c"
                       PRIV_REQUIRES fatfs esp_driver_i2c esp_driver_gpio esp_timer vl53l1x_uld
                       INCLUDE_DIRS ".")
```

> **Note for future work:** this component uses an **explicit `PRIV_REQUIRES`
> list**, so it does *not* get the usual "common" components for free. Every new
> dependency must be added here or the build fails with a bare
> `fatal error: <header>: No such file or directory`. This bit us twice
> (`esp_timer`, `esp_driver_gpio`).

### 4.3 New: `cmd_ping.c` — Parallax PING))) 28015 driver

**Protocol implemented** (from the Parallax datasheet):

| Phase | Symbol | Timing | Who drives |
|---|---|---|---|
| Trigger pulse | t<sub>OUT</sub> | 2 µs min, **5 µs typical** | MCU (output) |
| Holdoff | t<sub>HOLDOFF</sub> | 750 µs | — |
| 40 kHz burst | t<sub>BURST</sub> | 200 µs | sensor |
| Echo pulse high | t<sub>IN-MIN</sub> … t<sub>IN-MAX</sub> | **115 µs … 18.5 ms** | sensor |
| Recovery | — | 200 µs before next measurement | — |

Range 2 cm – 3 m. Supply +5 V, 30 mA typ / 35 mA max. **9 g**, 22 × 46 × 16 mm,
0–70 °C.

**Measurement technique.** A GPIO `ANYEDGE` interrupt timestamps both edges of
the echo with `esp_timer_get_time()` (1 µs ≈ 0.17 mm of range), and wakes the
calling task with `vTaskNotifyGiveFromISR`. The trigger pulse itself is a
`esp_rom_delay_us(5)` busy-spin, because 5 µs is far below one 10 ms tick.

The ISR deliberately **never reads the pin level** — it uses an `armed` flag to
ignore the edges caused by our own trigger pulse, then takes the first edge as
rise and the second as fall:

```c
static void IRAM_ATTR ping_isr(void *arg)
{
    if (!s_cap.armed) {
        return;             // our own trigger pulse, not the sensor's echo
    }
    int64_t now = esp_timer_get_time();
    if (!s_cap.have_rise) {
        s_cap.t_rise = now;
        s_cap.have_rise = true;
        return;
    }
    s_cap.t_fall = now;
    s_cap.armed = false;
    BaseType_t hp_woken = pdFALSE;
    vTaskNotifyGiveFromISR(s_cap.waiter, &hp_woken);
    if (hp_woken) { portYIELD_FROM_ISR(); }
}
```

**Three distinguishable failure modes** — a bad measurement never prints as a
plausible number:

| Return | Meaning |
|---|---|
| `ESP_ERR_NOT_FOUND` | no edge at all → sensor absent, unpowered, or miswired |
| `ESP_ERR_TIMEOUT` | rise seen, no fall → echo never terminated |
| `ESP_ERR_INVALID_RESPONSE` | pulse shorter than t<sub>IN-MIN</sub> (115 µs) → not a real echo |

**The tick-rounding trap** (worth remembering for all future timing code):

```c
/* Holdoff + burst + longest echo is 19.45 ms. pdMS_TO_TICKS rounds down and
   the first tick of a delay can land almost immediately, so at this build's
   10 ms tick a 20 ms timeout could expire after only 10 ms of real waiting -
   aborting a measurement that was still in flight. 40 ms gives at least three
   whole ticks past the worst case. */
#define PING_WAIT_MS            40
```

**Temperature compensation is built in, not optional.** Speed of sound:

```
c_air = 331.5 + 0.6 × T_C   [m/s]
distance_cm = (echo_us / 2) × c_air × 1e-4
```

The datasheet warns the uncompensated error across its 0–70 °C operating range
is *"significant, in the magnitude of 11 to 12 percent."* Air temperature is read
live from the BMP180 (§4.5); `-t <C>` overrides it; 20 °C is the last-resort
fallback.

> **Independent cross-check:** Parallax's own Propeller object uses
> `TO_CM = 29_034`, which back-computes to 344.4 m/s — exactly
> 331.5 + 0.6 × 21.5 °C. The formula is confirmed by the vendor's own constant.

The command also classifies each reading against the **hover band**
(`PING_BAND_LOW_CM 185.0f` … `PING_BAND_HIGH_CM 225.0f`) as LOW / OK / HIGH.

### 4.4 New: `cmd_tof.c` + `components/vl53l1x_uld/` — VL53L1X driver

**The problem.** Every off-the-shelf ESP-IDF VL53L1X component found
(three were downloaded and inspected) wraps ST's driver in a layer that
**creates and owns its own I²C bus**. That is unacceptable here: the GY-88
already occupies GPIO4/5, and two masters on one pair of pins is not a design,
it is a fault.

**The solution — vendor only ST's core, write our own platform layer.**

| Kept | Discarded |
|---|---|
| `VL53L1X_api.c` / `.h` (ULD core) | upstream `core/src/vl53l1_platform.c` |
| `VL53L1X_calibration.c` / `.h` | the entire `esp_wrapper/` directory |
| `vl53l1_types.h` | `i2c_handler.h` coupling |
| `vl53l1_platform.h` (2 edits) | function-pointer indirection |

ST **Ultra Lite Driver, implementation version 3.5.5**, licensed under ST's
**SLA0103 (BSD OPEN SOURCE)** — `LICENSE.txt` is included verbatim.

**Exactly two edits to `vl53l1_platform.h`**, both marked with a `Removed:`
comment so future diffs against upstream stay readable:
1. `#include "i2c_handler.h"` → replaced with an explanatory comment (this was
   the single line coupling ST's header to the discarded wrapper).
2. The 17 lines of `vl53l1x_*` function-pointer typedefs and
   `g_vl53l1x_*_ptr` externs → removed.

> **CRLF trap:** the first `sed` patch silently did nothing. `cat -A` revealed
> the upstream headers use **CRLF line endings**. The patch was redone in Python
> matching `\r\n` explicitly and **preserving CRLF**, so the files still diff
> cleanly against upstream.

**Verification performed** (claims were checked, not assumed):
- `cmp` — all five untouched files are byte-identical to upstream.
- `diff` — `vl53l1_platform.h` differs by exactly the two documented edits.
- `nm` — every `VL53L1_*` platform symbol resolves to our port; no `g_vl53l1x*`
  or `i2c_handler` symbols survive in the binary.

**The platform layer** (`port/vl53l1x_port.c`, 212 lines) implements all
**9 required functions**:

```
VL53L1_WriteMulti   VL53L1_ReadMulti
VL53L1_WrByte       VL53L1_RdByte
VL53L1_WrWord       VL53L1_RdWord
VL53L1_WrDWord      VL53L1_RdDWord
VL53L1_WaitMs
```

Design points:
- Public API is just three symbols — the bus comes **from the caller**:
  ```c
  #define VL53L1X_ADDR_DEFAULT 0x29
  esp_err_t vl53l1x_port_attach(i2c_master_bus_handle_t bus, uint16_t addr_7bit);
  void      vl53l1x_port_detach(uint16_t addr_7bit);
  ```
- ST's `dev` handle is an **opaque `uint16_t`**, so the port keeps a small
  address → `i2c_master_dev_handle_t` table (`PORT_MAX_DEVICES 2`) and logs
  "not attached" rather than crashing on a bad handle.
- VL53L1X register indices are **16-bit big-endian** — every transaction
  prefixes the index MSB-first.
- Reads are a **single `i2c_master_transmit_receive`**, so another master cannot
  interleave between the index write and the data read.
- `PORT_TIMEOUT_MS 100`, `PORT_SCL_SPEED_HZ 100 kHz`, `PORT_MAX_PAYLOAD 64`.

**`VL53L1_WaitMs` is the subtle one.** `VL53L1X_SensorInit()` polls boot
completion with `WaitMs(1)` up to a thousand times. At a 10 ms tick, routing
that through `vTaskDelay` would stretch a boot that really takes a few
milliseconds into **several seconds**:

```c
int8_t VL53L1_WaitMs(uint16_t dev, int32_t wait_ms)
{
    if (wait_ms <= 0) { return 0; }
    if (wait_ms <= portTICK_PERIOD_MS) {
        esp_rom_delay_us((uint32_t)wait_ms * 1000);   // spin: sub-tick
        return 0;
    }
    TickType_t ticks = ((TickType_t)wait_ms + portTICK_PERIOD_MS - 1) / portTICK_PERIOD_MS;
    vTaskDelay(ticks + 1);                            // ceil, +1 for the partial first tick
    return 0;
}
```

**`cmd_tof.c`** adds boot-state polling, mode/budget configuration *with
readback*, frame-synchronised sampling, and a **15-entry range-status decoder**
(valid / sigma fail / signal fail / below min range / out of bounds / hardware
fail / no update / wraparound / processing fail / xtalk fail / sync error /
merged pulse / signal too low / other / no measurement). A non-zero status
prints `-`, **never a distance**.

Accepted timing budgets: `15, 20, 33, 50, 100, 200, 500` ms (15 ms is rejected
in long mode). Inter-measurement period must be ≥ the budget.

### 4.5 New: `baro_read_temperature_c()` — cross-sensor temperature

So the ultrasonic range is never computed from an *assumed* temperature, the
barometer exports air temperature for the `ping` command:

```c
esp_err_t baro_read_temperature_c(float *temp_c);
```

Only Bosch's `B5` intermediate matters for temperature, so those three lines are
repeated rather than calling the full `compensate()` with a zero pressure
reading — which would **underflow its unsigned `B7` term**.

**Cross-validated from both ends:** `baro` reported 27.0 °C and `ping` reported
the identical 27.0 °C pulled through this new function.

---

## 5. Console Command Reference

Banner shown at boot:

```
 ==============================================================
 |             Steps to Use i2c-tools                         |
 |                                                            |
 |  1. Try 'help', check all supported commands               |
 |  2. Try 'i2cconfig' to configure your I2C bus              |
 |  3. Try 'i2cdetect' to scan devices on the bus             |
 |  4. Try 'i2cget' to get the content of specific register   |
 |  5. Try 'i2cset' to set the value of specific register     |
 |  6. Try 'i2cdump' to dump all the register (Experiment)    |
 |  7. Try 'imu' for decoded MPU-6050 readings                |
 |  8. Try 'mag' for HMC5883L field and heading               |
 |  9. Try 'baro' for BMP180 pressure and altitude            |
 | 10. Try 'ping' for PING))) ultrasonic distance             |
 | 11. Try 'tof' for VL53L1X time-of-flight range             |
 |                                                            |
 ==============================================================
```

### 5.1 `imu` — MPU-6050

```
imu [-c <addr>] [-n <n>] [-i <ms>] [-a <0-3>] [-g <0-3>]
```
| Flag | Meaning | Default |
|---|---|---|
| `-c, --chip <addr>` | I²C address | `0x68` |
| `-n, --count <n>` | samples to print | 10 |
| `-i, --interval <ms>` | delay between samples | 200 |
| `-a, --accel <0-3>` | 0 = ±2 g, 1 = ±4 g, 2 = ±8 g, 3 = ±16 g | 0 |
| `-g, --gyro <0-3>` | 0 = ±250, 1 = ±500, 2 = ±1000, 3 = ±2000 dps | 0 |

Validates `WHO_AM_I == 0x68`. Columns: `ax[g] ay[g] az[g] gx[dps] gy[dps] gz[dps] T[C]`.

### 5.2 `mag` — HMC5883L

```
mag [-c <addr>] [-n <n>] [-i <ms>]
```
Defaults `0x1E` / 10 / 200 ms. Validates the ID registers spell **`H43`**
(`48 34 33`) — a genuine Honeywell part, not a QMC5883L clone.

Sets `CONFIG_A = 0x70` (8-sample average, 15 Hz, normal bias) and forces
**continuous mode**, then reads the mode register back. Waits on the `DRDY`
status bit before every sample — reading the data registers clears it, so a
frozen sensor becomes a *reported error* instead of a plausible repeated row.
Gain is read back from `CONFIG_B` so scaling stays right even if changed by
`i2cset`. Handles the **X, Z, Y** register order (not X, Y, Z) and the
`-4096` per-axis overflow sentinel.

Columns: `mx[uT] my[uT] mz[uT] |m|[uT] hdg[deg]`.

### 5.3 `baro` — BMP085/BMP180

```
baro [-c <addr>] [-n <n>] [-i <ms>] [-o <0-3>]
```
Defaults `0x77` / 10 / 500 ms / oversampling 3. Full Bosch integer compensation
chain. Columns: `T[C] P[hPa] alt[m]`.

### 5.4 `ping` — Parallax PING))) 28015

```
ping [-p <gpio>] [-n <n>] [-i <ms>] [-t <C>]
```
| Flag | Meaning | Default |
|---|---|---|
| `-p, --pin <gpio>` | SIG GPIO | 6 |
| `-n, --count <n>` | samples | 10 |
| `-i, --interval <ms>` | delay between samples | 100 |
| `-t, --temp <C>` | air temperature override | **reads the BMP180** |

Max 200 samples. Prints echo width, distance, and LOW/OK/HIGH against the
1.85–2.25 m hover band, plus min/median/mean/max/sd.

### 5.5 `tof` — VL53L1X

```
tof [-c <addr>] [-n <n>] [-d <1|2>] [-b <ms>] [-i <ms>] [-x <gpio>]
```
| Flag | Meaning | Default |
|---|---|---|
| `-c, --chip <addr>` | I²C address | `0x29` |
| `-n, --count <n>` | samples | 10 |
| `-d, --mode <1\|2>` | 1 = short (~1.3 m), 2 = long (~4 m) | 2 |
| `-b, --budget <ms>` | timing budget 20/33/50/100/200/500 | 100 |
| `-i, --interval <ms>` | inter-measurement period | = budget |
| `-x, --xshut <gpio>` | drive XSHUT high on this GPIO first, then wait 20 ms | — |

Columns: `dist[mm] status sig[kcps/sp] amb[kcps/sp] spads`. Warns if ambient
exceeds 50 kcps/SPAD. Always calls `VL53L1X_StopRanging` and
`vl53l1x_port_detach` on the way out, including on error paths.

---

## 6. Measured Results (hardware, 2026-10-11)

### 6.1 Bus scan

```
i2c-tools> i2cdetect
     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
00: -- -- -- -- -- -- -- -- -- -- -- -- --
10: -- -- -- -- -- -- -- -- -- -- -- -- -- -- 1e --
20: -- -- -- -- -- -- -- -- -- 29 -- -- -- -- -- --
...
60: -- -- -- -- -- -- -- -- 68 -- -- -- -- -- -- --
70: -- -- -- -- -- -- -- 77
```
All four devices present and distinct. ✅

### 6.2 `imu -n 10` — MPU-6050 ✅

```
MPU-6050 @ 0x68   accel +/-2g   gyro +/-250 dps
    ax[g]     ay[g]     az[g]   gx[dps]   gy[dps]   gz[dps]     T[C]
   -0.075    -0.013     0.929     -2.63     -0.40     -0.34     27.1
   -0.080    -0.017     0.929     -2.68     -0.53     -0.40     27.2
   -0.081    -0.016     0.929     -2.59     -0.53     -0.46     27.2
   -0.076    -0.022     0.933     -2.84     -0.34     -0.34     27.3
   -0.082    -0.018     0.922     -2.61     -0.24     -0.42     27.3
   -0.082    -0.019     0.914     -2.69     -0.31     -0.55     27.4
   -0.081    -0.018     0.932     -2.69     -0.40     -0.42     27.4
   -0.079    -0.021     0.929     -2.43     -0.45     -0.52     27.4
   -0.083    -0.017     0.919     -2.71     -0.31     -0.41     27.5
   -0.079    -0.013     0.932     -2.68     -0.49     -0.36     27.5
```

Derived figures, at rest:

| Quantity | Value | Comment |
|---|---|---|
| Accel noise (az) | ~±0.01 g p-p | fine |
| **Accel magnitude ‖a‖** | **0.933 g** | ⚠️ should be 1.000 g — **−6.7 % error**, see §9.1 |
| **gx bias** | **−2.66 °/s** | ⚠️ large — see §9.2 |
| gy bias | −0.40 °/s | |
| gz bias | −0.42 °/s | |
| Gyro noise | ~±0.2 °/s p-p | fine |
| Die temperature | 27.1 → 27.5 °C over ~2 s | ⚠️ self-heating, see §9.5 |

### 6.3 `mag -n 5` — HMC5883L ✅

```
HMC5883L @ 0x1e   range +/-1.30 Ga   1090 LSB/Ga
   mx[uT]    my[uT]    mz[uT]   |m|[uT]  hdg[deg]
    21.65    -12.48      7.43     26.07     330.0
    21.65    -13.39      7.98     26.68     328.3
    21.65    -13.21      7.80     26.54     328.6
    21.65    -13.30      7.98     26.64     328.4
    21.65    -13.30      7.80     26.58     328.4
```

ID registers read `H43` (no warning raised) → genuine Honeywell part.
‖m‖ ≈ 26.1–26.7 µT, heading stable within 1.7°. Samples are provably fresh
(DRDY-gated). See §9.3 for the calibration caveat.

### 6.4 `baro -n 3` — BMP180 ✅

```
BMP085/180 @ 0x77   oversampling 3 (26 ms)   sea level 1013.25 hPa
    T[C]      P[hPa]     alt[m]
    27.1      851.80     1440.3
    27.0      851.74     1440.8
    27.0      851.71     1441.1
```

Pressure spread over 3 samples: **0.09 hPa ≈ 0.75 m of altitude.** This single
number is the whole justification for the PING))) — see §9.6.

### 6.5 `tof` — VL53L1X ✅

Static target at ~1.57 m, dark room.

```
VL53L1X @ 0x29   model 0xeacc   long mode   budget 100 ms   period 100 ms
 dist[mm]            status sig[kcps/sp] amb[kcps/sp]   spads
     1567             valid            3            0     210
     ... (10 samples)
10/10 valid   min 1563  median 1566  mean 1565.8  max 1567  sd 1.5 mm
```

Full matrix:

| Mode | Budget | Valid | Median | sd | Note |
|---|---|---|---|---|---|
| long | 100 ms | 10/10 | 1566 mm | **1.5 mm** | reference |
| long | 500 ms | 4/4 | 1571 mm | 4.2 mm | agrees with 100 ms |
| long | **20 ms** | 5/5 | **1481 mm** | 4.1 mm | ⚠️ **−85 mm (−5.5 %) bias** |
| short | 100 ms | 0/5 | — | — | ✅ all `out of bounds` — correct, target is past short mode's ~1.3 m |

Signal 3 kcps/SPAD, **ambient 0 kcps/SPAD** (dark), 210 effective SPADs.

**Three findings:**
1. The full vendored-ULD + custom-platform-layer stack works end to end.
2. Timing budgets below 100 ms are **not usable for absolute distance** on this
   unit — the 20 ms bias (85 mm) is systematic, not noise (its sd is only 4 mm).
3. Short mode correctly *refuses* an out-of-range target rather than inventing a
   number — the status decoder is doing its job.

### 6.6 `ping` — Parallax PING))) ❌ NOT TESTED

Sensor is not physically connected. Code is written and compiles; the voltage
divider of §2.4 exists on paper only. **Everything about this driver is unverified.**

---

## 7. Design Rules Applied (carry these into the flight firmware)

These were adopted deliberately and are worth keeping as project conventions.

### 7.1 Never scale from found state
An ESP32 reset does **not** power-cycle the sensors. Their configuration
survives a reflash, so the first run after a cold power-up starts from different
state than every run after it. Therefore:
- Every driver **writes** the configuration it intends to use, and then
  **reads it back** and warns on mismatch.
- Scale factors are derived from the register actually read, never from the
  reset default (`mag` reads `CONFIG_B` gain; `tof` reads mode and budget back;
  `imu` reads its range registers back).

### 7.2 Every failure mode is distinguishable, and nothing fabricates
A bad reading must never surface as a plausible number.
- `ping` has three separate error returns for three physically different faults.
- `tof` prints `-` for any non-zero range status, never a distance.
- `mag` prints `-` on the overflow sentinel and aborts if the mode register
  isn't continuous (rather than repeating a frozen sample).
- `mag` waits on DRDY so a stalled sensor is an error, not a repeated row.

### 7.3 Measure, don't assume
`ping` reads real air temperature from the BMP180 instead of assuming 20 °C —
the datasheet's own warning is an 11–12 % error across its range.

### 7.4 Respect the 10 ms tick
`pdMS_TO_TICKS` rounds **down**, and the first tick of a `vTaskDelay` can land
almost immediately. For any sub-tick or near-tick hardware delay, use
**ceiling division plus one tick**, or busy-spin with `esp_rom_delay_us`.
Both `cmd_ping.c` and `vl53l1x_port.c` carry comments explaining this.

### 7.5 Drivers do not own the bus
The VL53L1X port takes a caller-supplied `i2c_master_bus_handle_t`. This is the
rule that made it possible to put a fourth device on the GY-88's bus at all, and
it is a hard requirement for the flight firmware.

### 7.6 Vendored code is verified, not trusted
`cmp`, `diff` and `nm` were all run to prove the ULD core is unmodified, the two
header edits are the only ones, and no wrapper symbols leaked into the binary.

---

## 8. Test Infrastructure

### 8.1 Scratchpad helpers

| File | Purpose |
|---|---|
| `repl.py` | send commands, fixed 2.5 s drain |
| **`repl2.py`** | send commands, **wait for the `i2c-tools>` prompt**; timeout as `argv[1]` |
| `read_serial.py` | passive serial reader |
| `fast.py` | quick one-shot |

`repl2.py` was written because `tof`'s init-plus-ranging sequence exceeds
`repl.py`'s fixed 2.5 s drain. Usage:

```bash
python3 repl2.py 30 "tof -n 10" "imu -n 10" "baro -n 3"
```

> These live in the session scratchpad and **should be moved into
> `scripts/`** in the repo so they survive.

### 8.2 Flashing while a monitor is attached
If `esp_idf_monitor` holds the port, **do not kill it**. Either press
`Ctrl-T Ctrl-F` inside the monitor to build-and-flash in place, or `Ctrl-]` to
exit cleanly.

---

## 9. Problems, Doubts and Challenges

Ordered roughly by how much they threaten the flight firmware.

### 9.1 ⚠️ The accelerometer reads 0.933 g at rest, not 1.000 g
‖a‖ = √(0.080² + 0.017² + 0.929²) = **0.933 g** — a **−6.7 % error**.

A Kalman/AHRS filter's accelerometer update assumes the rest magnitude *is* 1 g;
feeding it 0.933 g biases attitude and corrupts the gravity-vector reference.
MPU-6050 spec allows ±3 % sensitivity tolerance plus ±80 mg Z-axis zero-g
offset, so this is *plausibly within spec* — a Z offset of −71 mg would explain
most of it. **But it must be calibrated out before fusion.** This needs a
6-position accelerometer calibration (scale + offset per axis).
**Unresolved — highest-priority calibration task.**

### 9.2 ⚠️ Gyro X bias is −2.66 °/s
Integrated naively that is **−160°/minute** of attitude drift. It is stable
across samples, so it is a bias, not noise — and therefore removable, either by
a rest-average at boot or (better) by online estimation in the filter.
A blimp manoeuvres slowly and flies long missions, so unbounded gyro drift is a
first-order threat to heading hold.

### 9.3 ⚠️ The magnetometer is completely uncalibrated
- ‖m‖ ≈ 26.5 µT. Earth's total field at this location (≈1 440 m altitude,
  equatorial Andes) is roughly 28–30 µT, so the reading is in the right
  neighbourhood but **not verified**.
- No **hard-iron** (offset) or **soft-iron** (scale/cross-axis) compensation
  exists. Heading from an uncalibrated magnetometer can easily be 10–30° wrong,
  and the error is heading-dependent, so it does not average out.
- `mx` read **exactly 21.65 µT on all five samples** while `my`/`mz` wobbled by
  1–2 LSB. This is *probably* benign quantisation on an 8-sample-averaged axis
  sitting on a count boundary, but it has not been proven. Worth re-checking
  with the board rotated.

### 9.4 ⚠️⚠️ Motors will corrupt the magnetometer — this is the biggest unknown
The HMC5883L sits on the same small board as everything else, and the blimp will
carry motors and current-carrying wires. Motor magnets and drive currents produce
fields far larger than Earth's 26 µT. **Nothing has been measured about this yet**
because no motors have been run near the sensor. This could invalidate heading
entirely. It must be characterised before the magnetometer is trusted in flight.

### 9.5 ⚠️ IMU self-heating and temperature-dependent bias
Die temperature climbed 27.1 → 27.5 °C within ~2 s of sampling. MPU-6050 gyro
bias is temperature dependent, so a bias captured at boot will be wrong once the
part warms up. Argues strongly for **online** bias estimation rather than a
one-shot calibration.

### 9.6 ⚠️ The barometer cannot hold a 40 cm band
Pressure varied 0.09 hPa across three consecutive samples ≈ **0.75 m of
altitude noise**, against a hover band of only 40 cm (1.85–2.25 m). Additionally:
- The 1 440 m figure is **pressure altitude** against an assumed 1013.25 hPa
  sea-level reference. It is not true altitude and the offset is unknown.
- Indoor pressure shifts with HVAC, doors and weather, so the *absolute* value
  drifts over a flight.

The barometer is therefore usable for **relative/vertical-rate** sensing and
long-term drift anchoring, **not** for absolute hover altitude.

### 9.7 ⚠️ The VL53L1X only reached 1.57 m because the room was dark
Ambient measured **0 kcps/SPAD**. Under normal room lighting the datasheet puts
long-mode maximum range at **1.1–1.7 m**, and at 200 kcps/SPAD ambient it
collapses to **0.68–0.73 m**. This is precisely why it is *not* the altimeter.
Its role is close-range: ground proximity on landing (below ~1.3 m) and
obstacle/wall detection, where its narrow spot and mm resolution beat the
PING)))'s wide cone.

### 9.8 ⚠️ The VL53L1X is biased 5.5 % low at short timing budgets
Measured: 1481 mm at a 20 ms budget vs 1566/1569 mm at 100/500 ms. Cause not
investigated. Practical rule: **do not use a budget below 100 ms for absolute
distance on this unit.** That caps its update rate at ~10 Hz.

### 9.9 ⚠️ The entire PING))) path is unverified
Driver written, compiles, never run. Specifically unverified: the 1.0 k/1.8 k
divider on real silicon; whether a 3.3 V trigger reliably fires a 5 V sensor;
echo timing against a tape measure; behaviour over soft floors (carpet absorbs
40 kHz); the ~20–30° beam footprint, which is roughly **a metre wide at 2 m** and
will see furniture, people and walls under the airframe; and the 9 g mass against
the lift budget.

### 9.10 ⚠️ Both rangefinders report slant range, not altitude
A tilted airframe measures the hypotenuse. Needs `h = d·cos θ` using MPU-6050
attitude. Not implemented anywhere yet.

### 9.11 I²C robustness has not been addressed
Four devices on a 100 kHz bus with internal pull-ups and no:
- bus-error recovery or retry logic,
- clock-stretch/NAK handling beyond the per-call timeout,
- bus-reset-on-hang,
- measurement of actual rise times with the current pull-up value.

On a vibrating airframe with motor noise this *will* eventually matter.

### 9.12 `i2cdetect` phantom ACKs on the first scan after reset
Once, on the first scan after reset with the VL53L1X newly added, `i2cdetect`
reported ACKs at **every even address 0x06–0x1C plus 0x1F**. Four immediate
re-scans were all clean. The same artefact had been seen once before. It is
reproducible only immediately after reset, so it is probably a bus-settling or
VL53L1X boot-state artefact — but it is **not explained**, and a flight firmware
that enumerates the bus at boot could be misled by it.

### 9.13 The console architecture is wrong for flight (by design)
This is a bench tool and behaves like one:
- Each command **adds and removes its I²C device handle per invocation** — fine
  interactively, wasteful and racy in flight.
- Every command **blocks** its calling task while polling.
- Each command **reconfigures the sensor on every call**.
- Samples are **not timestamped** — there is no consistent `dt` for a filter.
- There is **no shared state**; nothing can consume readings but `printf`.
- `CONFIG_FREERTOS_HZ=100` (10 ms tick) is too coarse for a flight loop.
- Optimisation is `-Og` with assertions on.

### 9.14 No axis-alignment calibration between IMU and magnetometer
The MPU-6050 and HMC5883L are separate dies on the GY-88 and are **not
guaranteed to share axis conventions or be mechanically aligned**. A 9-DOF
fusion filter assumes a common body frame. The relative rotation between them
has not been determined.

### 9.15 Missing from the bench entirely
No sensor exists yet for **horizontal position or velocity**. See §11.

---

## 10. What Is Proven vs What Is Assumed

| Claim | Status |
|---|---|
| ESP32-S3-Zero builds, flashes and runs a USB console | ✅ proven |
| Four I²C devices coexist on GPIO4/5 | ✅ proven |
| MPU-6050 accel/gyro/temp decode correctly | ✅ proven |
| HMC5883L decodes, genuine `H43` part, DRDY-gated | ✅ proven |
| BMP180 full Bosch compensation chain | ✅ proven |
| Cross-sensor temperature export works | ✅ proven (both ends agree) |
| ST ULD core is unmodified; platform layer is ours | ✅ proven (`cmp`/`diff`/`nm`) |
| VL53L1X ranges, with correct status decoding | ✅ proven |
| VL53L1X is unsuitable as a 2 m altimeter | ✅ proven (datasheet + ambient-0 measurement) |
| Accelerometer is accurate | ❌ **disproven** (0.933 g) |
| Gyro is unbiased | ❌ **disproven** (−2.66 °/s on X) |
| Magnetometer heading is accurate | ❓ unverified, uncalibrated |
| PING))) driver works | ❓ **entirely unverified** |
| PING))) divider is safe and functional | ❓ paper design only |
| Sensors survive motor noise | ❓ **never tested** |
| Horizontal position is observable | ❌ **no sensor exists** |

---

## 11. Implications for the Flight Firmware

The honest summary of what this sensor set can and cannot do:

**Fully observable, good confidence**
- **Attitude** (roll, pitch, yaw): gyro for rate, accelerometer for the gravity
  vector, magnetometer for heading. A blimp's slow dynamics are a genuine
  *advantage* here — specific force is small, so the accelerometer is a clean
  gravity reference almost all the time.
- **Altitude and vertical rate**: PING))) as the primary absolute reference
  (2 cm–3 m, immune to floor colour and ambient light), barometer for
  relative/rate and drift anchoring, VL53L1X below ~1.3 m for landing, vertical
  accelerometer for the fast path.

**Not observable with the current sensor set**
- **Horizontal position and velocity.** This is the hard limit. Dead reckoning
  from the accelerometer is hopeless: even after calibration a residual 10 mg
  bias integrates to **2.5 m of position error in 10 seconds**
  (½ · 0.098 m/s² · 100 s²). At the *current* uncalibrated 70 mg it is 0.34 m in
  the first second. There is no filter that fixes this — it needs a **new
  sensor** that observes position or velocity directly.

Candidate additions (to be decided, not yet evaluated):
- **Optical flow** (PMW3901 / PAA5100) — gives body-frame velocity, cheap, light,
  needs a textured floor and a height estimate (which the PING))) provides).
- **UWB anchors** (DW1000/DW3000) — true indoor absolute position, ~10 cm, but
  needs fixed infrastructure.
- **Downward or forward camera with ArUco/fiducial markers** — absolute pose,
  needs compute and lighting.
- **Multiple ultrasonic/ToF rangefinders aimed at walls** — cheap, works in a
  known room, degrades badly in open space.

This decision shapes everything above it in the stack and should be made early.

---

## 12. Repository State

- Branch: `master` (note: the stated main branch for PRs is `main`).
- Git user: `namontoy`.
- **Nothing in `firmware/` is committed yet** — `git status` shows the whole
  tree as untracked, and there are no commits on the branch. All of the work in
  this report is **uncommitted**. This should be fixed before anything else.

Suggested first commits:
1. `firmware/projects/i2c_tools` — the bench, as it stands.
2. `components/vl53l1x_uld` — as its own commit, so the vendoring and its
   provenance are isolated and auditable.
3. `scripts/` — move the pyserial helpers out of the scratchpad.
4. `docs/sensor-bringup-report.md` — this document.

---

## 13. Open Questions for the Project Owner

1. **Horizontal position** — which sensor? (§11) This gates the whole estimator
   design.
2. **Lift budget** — how many grams are actually available? The PING))) alone is
   9 g.
3. **Flight arena** — known fixed room (enables UWB/wall-ranging/markers) or
   arbitrary space (forces optical flow)?
4. **Floor surface** — hard or carpeted? Carpet absorbs the 40 kHz burst and
   degrades the primary altimeter.
5. **Lighting** — affects the VL53L1X's useful range by more than 2×.
6. **Motor type and placement** — determines how badly the magnetometer is
   compromised (§9.4).
7. **Is 5 V available in flight**, or does the PING))) need a boost converter?

---

## Appendix A — Key Constants

```c
/* PING))) 28015 */
#define PING_GPIO_DEFAULT       6
#define PING_TRIGGER_US         5       /* 2 us min, 5 us typical */
#define PING_HOLDOFF_US         750
#define PING_BURST_US           200
#define PING_ECHO_MIN_US        115
#define PING_ECHO_MAX_US        18500
#define PING_WAIT_MS            40      /* see the tick-rounding note */
#define PING_FALLBACK_TEMP_C    20.0f
#define PING_BAND_LOW_CM        185.0f
#define PING_BAND_HIGH_CM       225.0f
#define PING_MAX_SAMPLES        200

/* VL53L1X */
#define TOF_MODEL_ID            0xEACC  /* empirically confirmed; ST's header
                                           comment claiming 0xEEAC is wrong */
#define TOF_BOOT_TIMEOUT_MS     200
#define TOF_MODE_SHORT          1
#define TOF_MODE_LONG           2
#define TOF_AMBIENT_WARN_KCPS   50
static const uint16_t tof_budgets[] = { 15, 20, 33, 50, 100, 200, 500 };

/* VL53L1X platform layer */
#define PORT_TIMEOUT_MS         100
#define PORT_SCL_SPEED_HZ       (100 * 1000)
#define PORT_MAX_PAYLOAD        64
#define PORT_MAX_DEVICES        2

/* HMC5883L */
#define HMC5883L_CONFIG_A_DEFAULT 0x70  /* 8-sample avg, 15 Hz, normal bias */
#define HMC5883L_OVERFLOW       (-4096)
static const float mag_lsb_per_gauss[8] = { 1370, 1090, 820, 660, 440, 390, 330, 230 };

/* MPU-6050 */
#define MPU6050_WHO_AM_I_VALUE  0x68
```

## Appendix B — Reference Values for Regression Testing

Resting references for this specific board, useful for spotting regressions
after any change:

| Reading | Expected |
|---|---|
| `imu` az | ≈ 0.92–0.93 g |
| `imu` gx | ≈ −2.4 to −3.1 °/s |
| `imu` T | ≈ 27.1–27.5 °C (rising) |
| `mag` ‖m‖ | ≈ 26.1–26.7 µT |
| `mag` heading | ≈ 328–330° (bench orientation) |
| `baro` P | ≈ 851.7–851.8 hPa |
| `baro` alt | ≈ 1440–1441 m |
| `tof` @ 1.57 m, long, 100 ms | sd ≈ 1.5 mm, 210 SPADs, sig 3 kcps/SPAD |
| `i2cdetect` | `1e 29 68 77` |

## Appendix C — Datasheet Facts Worth Not Re-deriving

**Parallax PING))) 28015**
- *"Bidirectional TTL pulse interface on a single I/O pin can communicate with
  5 V TTL or 3.3 V CMOS microcontrollers."*
- Uncompensated temperature error over 0–70 °C is *"significant, in the magnitude
  of 11 to 12 percent."*
- Propeller app-note revision: *"v1.1 - Updated 03/20/2007 to change SIG resistor
  from 10K to 1K."*
- `c_air = 331.5 + 0.6 × T_C` m/s, confirmed by Parallax's own `TO_CM = 29_034`
  (→ 344.4 m/s = 331.5 + 0.6 × 21.5).

**VL53L1X long-mode maximum range** (why it is not the altimeter)

| Target | Darkness typ | Darkness min | 50 kcps/SPAD | 200 kcps/SPAD |
|---|---|---|---|---|
| White 88 % | 360 cm | 260 cm | 166 cm | 73 cm |
| Grey 54 % | 340 cm | 220 cm | 154 cm | 69 cm |
| Grey 17 % | 170 cm | 80 cm | 114 cm | 68 cm |

The 2 m hover needs 1.85–2.25 m. Even the **darkness minimum** for a mid-grey
floor (220 cm) falls short of the upper bound, and under normal room ambient the
whole mode collapses below the band.

- Default address `0x29`, **not persistent across power-off**.
- Register indices are **16-bit big-endian**.
- `VL53L1X_ERROR` is a `uint8_t`; `dev` is an opaque `uint16_t`.
- `FIRMWARE__SYSTEM_STATUS` is non-zero once booted. ST's header says
  *"1:booted"* but **this is not literal** — the part on this bench reports **3**,
  and ST's own reference implementation waits for *any* non-zero value.
