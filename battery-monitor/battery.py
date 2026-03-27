from collections import deque
from time import sleep


class Adc:
    """SPI ADC (e.g. MCP3001) using spidev.

    Imports spidev lazily so this module can be imported on platforms
    that don't have spidev (e.g. ESP32 / MicroPython).

    :param bus: SPI bus number (e.g. 0 for /dev/spidev0.x)
    :param device: SPI chip-select number (e.g. 0 for /dev/spidev0.0)
    :param resolution: ADC bit resolution (default 10 for MCP3001)
    :param max_speed_hz: SPI clock speed
    """

    def __init__(self, bus=0, device=0, resolution=10, max_speed_hz=1_000_000):
        import os
        import spidev
        dev_path = f"/dev/spidev{bus}.{device}"
        if not os.path.exists(dev_path):
            raise OSError(
                f"{dev_path} not found — enable SPI via raspi-config"
            )
        self._spi = spidev.SpiDev()
        self._spi.open(bus, device)
        self._spi.max_speed_hz = max_speed_hz
        self._spi.mode = 0b00
        self._max_value = (2 ** resolution) - 1

    @property
    def value(self):
        """Normalised reading 0.0–1.0 (IN+ / VREF)."""
        r = self._spi.xfer2([0x00, 0x00])
        # MCP3001: null bit then B9..B0 across 16 clocks
        # Byte 0 bits [4:0] → B9..B5; Byte 1 bits [7:3] → B4..B0
        raw = ((r[0] & 0x1F) << 5) | (r[1] >> 3)
        return raw / self._max_value


class AnalogPin:
    """Native analog input pin (e.g. ESP32 via machine.ADC).

    :param pin: GPIO pin number connected to the battery voltage signal
    :param resolution: Pin ADC bit resolution (default 12 for ESP32)
    """

    def __init__(self, pin, resolution=12):
        from machine import ADC, Pin
        self._adc = ADC(Pin(pin))
        self._max_value = (2 ** resolution) - 1

    @property
    def value(self):
        """Normalised reading 0.0–1.0 (IN+ / VREF)."""
        return self._adc.read() / self._max_value


class Battery:
    def __init__(
        self,
        max_capacity,
        adc_vref=3.3,
        voltage_scale=1.0,
        avg_samples=10,
        # SPI ADC (Raspberry Pi + external ADC, e.g. MCP3001)
        spi_bus=0,
        spi_device=0,
        spi_resolution=10,
        # Native analog pin (ESP32 etc.) — if set, SPI params are ignored
        analog_pin=None,
    ):
        """
        :param max_capacity: Maximum battery capacity in mAh
        :param adc_vref: ADC reference voltage in V
        :param voltage_scale: Scaling factor for external voltage divider
        :param avg_samples: Number of samples for moving average
        :param spi_bus: SPI bus for external ADC (ignored when analog_pin is set)
        :param spi_device: SPI chip-select for external ADC (ignored when analog_pin is set)
        :param spi_resolution: Bit resolution of external SPI ADC (ignored when analog_pin is set)
        :param analog_pin: Native analog pin number (ESP32 etc.); when set, uses AnalogPin
        """
        if analog_pin is not None:
            self.adc = AnalogPin(pin=analog_pin)
        else:
            self.adc = Adc(bus=spi_bus, device=spi_device, resolution=spi_resolution)

        self.max_capacity = max_capacity
        self.adc_vref = adc_vref
        self.voltage_scale = voltage_scale

        self.voltage_samples = deque(maxlen=avg_samples)
        self.filtered_voltage = 0.0

    def _read_voltage(self):
        """
        Read battery voltage from ADC, apply reference voltage
        and external scaling factor (e.g. voltage divider).
        """
        adc_voltage = self.adc.value * self.adc_vref
        return adc_voltage * self.voltage_scale

    def _update_voltage(self):
        """Calculate moving average of recent voltage measurements."""
        voltage = self._read_voltage()
        self.voltage_samples.append(voltage)
        self.filtered_voltage = sum(self.voltage_samples) / len(self.voltage_samples)
        return self.filtered_voltage

    def _voltage_to_capacity_mAh(self, voltage):
        """
        Map battery voltage to remaining capacity in mAh
        using NCR18650GA discharge curve under load.
        """
        table = [
            (4.20, max(self.max_capacity, 3332)),
            (4.07, 3332),
            (4.02, 3292),
            (3.97, 3042),
            (3.93, 2842),
            (3.77, 2342),
            (3.50, 1342),
            (3.36, 742),
            (3.29, 542),
            (3.24, 442),
            (3.17, 342),
            (3.00, 205),
            (2.80, 102),
            (2.50, 0),
        ]

        if voltage >= 4.20:
            return max(self.max_capacity, 3332)
        if voltage <= 2.50:
            return 0.0

        for i in range(len(table) - 1):
            v_high, c_high = table[i]
            v_low, c_low = table[i + 1]
            if v_low <= voltage <= v_high:
                slope = (c_high - c_low) / (v_high - v_low)
                return c_low + slope * (voltage - v_low)

        return 0.0

    def get_capacity(self):
        """
        Return remaining battery capacity in percent
        (based on moving averaged voltage).
        """
        voltage = self._update_voltage()
        capacity_mAh = self._voltage_to_capacity_mAh(voltage)
        percent = (capacity_mAh / self.max_capacity) * 100.0
        return max(0.0, min(100.0, percent))


if __name__ == "__main__":
    # Raspberry Pi example (MCP3001 via SPI):
    battery = Battery(
        max_capacity=3342,  # Panasonic NCR18650GA (mAh)
        adc_vref=3.3,
        voltage_scale=2.0,  # adjust for your voltage divider
        avg_samples=10,
        spi_bus=0,
        spi_device=0,
        spi_resolution=10,
    )

    # ESP32 example (native analog pin):
    # battery = Battery(
    #     max_capacity=3342,
    #     adc_vref=3.3,
    #     voltage_scale=2.0,
    #     avg_samples=10,
    #     analog_pin=34,
    # )

    while True:
        capacity = battery.get_capacity()
        print(f"Voltage: {battery.filtered_voltage:.3f} V | Capacity: {capacity:.1f} %")
        sleep(1)
