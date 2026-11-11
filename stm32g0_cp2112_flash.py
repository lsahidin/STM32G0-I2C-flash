import sys
import time
import struct
import hid

# CP2112 USB Identifiers
CP2112_VID = 0x10C4
CP2112_PID = 0xEA90

# STM32G030 Bootloader Constants (AN2606 / AN4221)
STM32_I2C_ADDR_7BIT = 0x56
STM32_I2C_ADDR_8BIT = STM32_I2C_ADDR_7BIT << 1  # 0xAC
STM32G030_PID = 0x0466
FLASH_BASE_ADDR = 0x08000000

ACK = 0x79
NACK = 0x1F
BUSY = 0x76

CMD_GET_ID = 0x02
CMD_GO = 0x21
CMD_WRITE_MEM = 0x31
CMD_ERASE_MEM = 0x44


class CP2112I2C:
    def __init__(self, clock_hz=100000):
        self.dev = hid.device()
        self.dev.open(CP2112_VID, CP2112_PID)
        self.dev.set_nonblocking(False)
        self._configure_smbus(clock_hz)
        self._configure_gpios()

    def _configure_smbus(self, clock_hz):
        # Report 0x06: Set SMBus Configuration
        # Clock (4B BE), DevAddr (1B), AutoSendRead (1B), WriteTimeout (2B BE),
        # ReadTimeout (2B BE), SCLTimeout (1B), RetryTime (2B BE)
        cfg = struct.pack(
            ">IBBHHBH",
            clock_hz,
            0x02,   # Dummy host address
            0x00,   # Disable auto-send read
            100,    # Write timeout (ms)
            100,    # Read timeout (ms)
            0x00,   # Disable SCL low timeout
            1       # Retry limit
        )
        self.dev.send_feature_report(b"\x06" + cfg)

    def _configure_gpios(self):
        # Report 0x02: Set GPIO Configuration
        # Direction: 0x03 (GPIO.0 = NRST output, GPIO.1 = BOOT0 output)
        # Push-Pull: 0x03, Special: 0x00, ClockDivider: 0x00
        self.dev.send_feature_report(bytes([0x02, 0x03, 0x03, 0x00, 0x00]))

    def set_gpio(self, nrst_high: bool, boot0_high: bool):
        # Report 0x04: Set GPIO Values (Latch value, Latch mask)
        val = (1 if nrst_high else 0) | ((1 if boot0_high else 0) << 1)
        self.dev.send_feature_report(bytes([0x04, val, 0x03]))

    def enter_bootloader(self):
        # Pull BOOT0 high, pulse NRST low, wait for ROM bootloader init
        self.set_gpio(nrst_high=True, boot0_high=True)
        time.sleep(0.02)
        self.set_gpio(nrst_high=False, boot0_high=True)
        time.sleep(0.05)
        self.set_gpio(nrst_high=True, boot0_high=True)
        time.sleep(0.08)

    def reset_to_app(self):
        # Release BOOT0 low and pulse NRST
        self.set_gpio(nrst_high=True, boot0_high=False)
        time.sleep(0.02)
        self.set_gpio(nrst_high=False, boot0_high=False)
        time.sleep(0.05)
        self.set_gpio(nrst_high=True, boot0_high=False)

    def _wait_transfer_complete(self, timeout_s=5.0):
        start = time.time()
        while (time.time() - start) < timeout_s:
            # Report 0x15: Transfer Status Request
            self.dev.write(bytes([0x15, 0x01]))
            resp = self.dev.read(64, timeout_ms=200)
            if not resp or resp[0] != 0x16:
                continue
            status0 = resp[1]
            # 0x02 = Complete, 0x03 = Complete with error
            if status0 == 0x02:
                return
            elif status0 == 0x03:
                raise IOError(f"CP2112 I2C transfer error (Status1=0x{resp[2]:02X})")
            time.sleep(0.002)
        raise TimeoutError("CP2112 I2C transfer timed out")

    def i2c_write(self, data: bytes):
        if len(data) > 61:
            raise ValueError("CP2112 single HID write max payload is 61 bytes")
        # Report 0x14: Data Write Request [0x14, SlaveAddr8Bit, Length, Data...]
        packet = bytes([0x14, STM32_I2C_ADDR_8BIT, len(data)]) + data
        self.dev.write(packet)
        self._wait_transfer_complete()

    def i2c_read(self, length: int) -> bytes:
        # Report 0x12: Data Read Request [0x12, SlaveAddr8Bit, LengthHigh, LengthLow]
        req = struct.pack(">BBH", 0x12, STM32_I2C_ADDR_8BIT, length)
        self.dev.write(req)
        self._wait_transfer_complete()

        out = bytearray()
        while len(out) < length:
            to_read = min(61, length - len(out))
            # Report 0x13: Data Read Force Send
            self.dev.write(bytes([0x13, to_read]))
            resp = self.dev.read(64, timeout_ms=500)
            if resp and resp[0] == 0x13 and resp[2] > 0:
                out.extend(resp[3:3 + resp[2]])
        return bytes(out)

    def close(self):
        self.dev.close()


class STM32I2CBootloader:
    def __init__(self, bus: CP2112I2C):
        self.bus = bus

    @staticmethod
    def checksum(data: bytes) -> int:
        cs = 0
        for b in data:
            cs ^= b
        return cs

    def wait_ack(self, timeout_s=5.0):
        start = time.time()
        while (time.time() - start) < timeout_s:
            try:
                status = self.bus.i2c_read(1)[0]
                if status == ACK:
                    return
                elif status == NACK:
                    raise RuntimeError("STM32 Bootloader returned NACK (0x1F)")
                elif status == BUSY:
                    time.sleep(0.01)
            except IOError:
                # Target may NACK address while erasing flash
                time.sleep(0.01)
        raise TimeoutError("Timed out waiting for STM32 ACK")

    def send_command(self, cmd: int):
        self.bus.i2c_write(bytes([cmd, cmd ^ 0xFF]))
        self.wait_ack()

    def get_id(self) -> int:
        self.send_command(CMD_GET_ID)
        # Response: [Length - 1 (1 byte), PID MSB, PID LSB]
        resp = self.bus.i2c_read(3)
        self.wait_ack()
        pid = (resp[1] << 8) | resp[2]
        return pid

    def mass_erase(self):
        print("Erasing flash (mass erase)...")
        self.send_command(CMD_ERASE_MEM)
        # Special erase code 0xFFFF (Global Mass Erase) + checksum 0x00
        self.bus.i2c_write(bytes([0xFF, 0xFF, 0x00]))
        self.wait_ack(timeout_s=10.0)
        print("Erase complete.")

    def write_memory(self, addr: int, data: bytes):
        if not (1 <= len(data) <= 256):
            raise ValueError("Write chunk must be between 1 and 256 bytes")
        # Pad to 4-byte alignment if needed
        if len(data) % 4 != 0:
            data += b"\xFF" * (4 - (len(data) % 4))

        self.send_command(CMD_WRITE_MEM)

        # Send 4-byte address + XOR checksum
        addr_bytes = struct.pack(">I", addr)
        self.bus.i2c_write(addr_bytes + bytes([self.checksum(addr_bytes)]))
        self.wait_ack()

        # CP2112 max single HID write is 61 bytes, so split >60B writes cleanly
        # Note: AN4221 requires the entire [N-1, data..., checksum] in one I2C transaction.
        # Because CP2112 HID report 0x14 caps single transactions at 61 bytes,
        # we limit each Write Memory block to 32 bytes (1 + 32 + 1 = 34 bytes total).
        raise NotImplementedError("Use write_chunk_32b for CP2112 HID packet limit")

    def write_chunk_32b(self, addr: int, data: bytes):
        if len(data) > 32:
            raise ValueError("Chunk exceeds 32 bytes (CP2112 61-byte HID transaction cap)")
        if len(data) % 4 != 0:
            data += b"\xFF" * (4 - (len(data) % 4))

        self.send_command(CMD_WRITE_MEM)

        addr_bytes = struct.pack(">I", addr)
        self.bus.i2c_write(addr_bytes + bytes([self.checksum(addr_bytes)]))
        self.wait_ack()

        n_minus_1 = len(data) - 1
        payload = bytes([n_minus_1]) + data
        frame = payload + bytes([self.checksum(payload)])
        self.bus.i2c_write(frame)
        self.wait_ack()

    def flash_binary(self, bin_path: str, base_addr=FLASH_BASE_ADDR):
        with open(bin_path, "rb") as f:
            firmware = f.read()

        total = len(firmware)
        print(f"Flashing {total} bytes to 0x{base_addr:08X}...")

        for offset in range(0, total, 32):
            chunk = firmware[offset:offset + 32]
            self.write_chunk_32b(base_addr + offset, chunk)
            if (offset // 32) % 32 == 0 or (offset + len(chunk)) == total:
                pct = ((offset + len(chunk)) / total) * 100
                print(f"\rProgress: {offset + len(chunk)}/{total} bytes ({pct:.1f}%)", end="")
        print("\nFlashing complete.")


def main():
    if len(sys.argv) < 2:
        print("Usage: python stm32g0_cp2112_flash.py <firmware.bin>")
        sys.exit(1)

    bin_file = sys.argv[1]
    cp = CP2112I2C(clock_hz=100000)
    stm = STM32I2CBootloader(cp)

    try:
        print("Entering STM32G030 bootloader...")
        cp.enter_bootloader()

        pid = stm.get_id()
        print(f"Connected! Chip PID: 0x{pid:04X}", end="")
        if pid == STM32G030_PID:
            print(" (STM32G030/G031/G041)")
        else:
            print(" (Warning: Unexpected PID)")

        stm.mass_erase()
        stm.flash_binary(bin_file, FLASH_BASE_ADDR)

        print("Resetting into user application...")
        cp.reset_to_app()
        print("Done!")

    finally:
        cp.close()


if __name__ == "__main__":
    main()
