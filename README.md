## AN4221 Protocol
Implements the ST AN4221 I2C bootloader protocol, including automatic BOOT0 / NRST toggling via CP2112 GPIOs, chip ID verification (0x0466), mass erase, 256-byte chunked flash writing, and booting into the new firmware.

## Hardware
> **STM32G030** using the **CP2112** over USB HID (hidapi).

## Prerequisites
Install the hidapi Python wrapper:
```bash
pip install hidapi
```
(Note: On Linux, you may need a udev rule for VID 0x10C4 / PID 0xEA90 or run with sudo.)

## Usage & Key Implementation Notes
- <b>Run Command:</b>
```py
python stm32g0_cp2112_flash.py build/firmware.bin 
```

- **32-Byte Chunk Sizing:** 
While AN4221 supports up to 256 bytes per Write Memory command, the CP2112 USB HID write report (0x14) caps a single I2C write transaction at 61 bytes. Using 32-byte chunks (1 byte length + 32 bytes data + 1 byte checksum = 34 bytes) keeps every write strictly inside a single atomic I2C transaction without needing multi-packet HID buffering.
- **Manual Boot Mode:** 
If you do not connect GPIO.0 (NRST) and GPIO.1 (BOOT0), tie BOOT0 (PA14) to 3.3V and reset the board manually right before running the script.
- **Supported Pins:**
You must connect the CP2112 to I2C1 on PB6 (SCL) and PB7 (SDA), or I2C2 on PB10 (SCL) and PB11 (SDA) (available on higher-pin packages).