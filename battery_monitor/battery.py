import os
from collections import deque
from time import sleep

_CELL_DATA_DIR = os.path.join(os.path.dirname(__file__), "cell_data")


def _load_discharge_table(cell_type):
    """Load a discharge table from a CSV file.

    cell_type can be:
    - A built-in cell name (e.g. "panasonic_ncr18650ga") — resolved from cell_data/
    - An absolute or relative path to a CSV file

    Expected CSV columns: voltage_v, consumed_mah
    Rows must be ordered from highest to lowest voltage.
    Lines starting with '#' and empty lines are ignored.

    Returns a list of (voltage_v, consumed_mah) tuples sorted descending by voltage.
    The total cell capacity (mAh) equals the consumed_mah value of the last row.
    """
    path = str(cell_type)
    if not os.path.isabs(path) and os.sep not in path and not path.endswith(".csv"):
        path = os.path.join(_CELL_DATA_DIR, path + ".csv")

    table = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            if len(parts) < 2:
                continue
            try:
                table.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue  # skip header row if present

    if len(table) < 2:
        raise ValueError(f"Discharge table in '{path}' must have at least 2 data rows.")

    table.sort(key=lambda x: x[0], reverse=True)  # descending by voltage
    return table


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
        cell_type,
        adc_vref=3.3,
        voltage_scale=1.0,
        avg_samples=10,
        capacity_discharge_offset=0.0,
        full_runtime_min=None,
        # SPI ADC (Raspberry Pi + external ADC, e.g. MCP3001)
        spi_bus=0,
        spi_device=0,
        spi_resolution=10,
        # Native analog pin (ESP32 etc.) — if set, SPI params are ignored
        analog_pin=None,
    ):
        """
        :param cell_type: Built-in cell name (e.g. "panasonic_ncr18650ga") or path to
            a CSV file with columns voltage_v, consumed_mah. The total cell capacity
            is derived from the highest consumed_mah value in the table.
        :param adc_vref: ADC reference voltage in V
        :param voltage_scale: Scaling factor for external voltage divider
        :param avg_samples: Number of samples for moving average
        :param capacity_discharge_offset: Percentage points linearly subtracted from
            the reported capacity across the usable voltage range. Reduction is 0 at
            full voltage and equals capacity_discharge_offset at the lower cutoff,
            so the display reaches 0 % before the battery cuts off.
        :param full_runtime_min: Estimated runtime in minutes at 100 % capacity.
            When set, get_runtime_min() and get_status() return a proportional
            runtime estimate. If None, runtime is not estimated (returns None).
        :param spi_bus: SPI bus for external ADC (ignored when analog_pin is set)
        :param spi_device: SPI chip-select for external ADC (ignored when analog_pin is set)
        :param spi_resolution: Bit resolution of external SPI ADC (ignored when analog_pin is set)
        :param analog_pin: Native analog pin number (ESP32 etc.); when set, uses AnalogPin
        """
        if analog_pin is not None:
            self.adc = AnalogPin(pin=analog_pin)
        else:
            self.adc = Adc(bus=spi_bus, device=spi_device, resolution=spi_resolution)

        self._discharge_table = _load_discharge_table(cell_type)
        self._v_max = self._discharge_table[0][0]
        self._v_min = self._discharge_table[-1][0]
        self._max_capacity = max(consumed for _, consumed in self._discharge_table)

        self.adc_vref = adc_vref
        self.voltage_scale = voltage_scale
        self._capacity_discharge_offset = capacity_discharge_offset
        self._full_runtime_min = full_runtime_min

        self.voltage_samples = deque(maxlen=avg_samples)

        # Current state — populated by update()
        self.voltage_v            = None
        self.state_of_charge_pct  = None
        self.state_of_charge_mah  = None
        self.runtime_remaining    = None

    def _read_voltage(self):
        """Read battery voltage from ADC and apply voltage divider scaling."""
        return self.adc.value * self.adc_vref * self.voltage_scale

    def _voltage_to_soc_pct(self, voltage):
        """Interpolate state of charge in percent from the discharge table."""
        if voltage >= self._v_max:
            return 100.0
        if voltage <= self._v_min:
            return 0.0

        table = self._discharge_table
        for i in range(len(table) - 1):
            v_high, consumed_high = table[i]
            v_low, consumed_low = table[i + 1]
            if v_low <= voltage <= v_high:
                consumed = consumed_high + (consumed_low - consumed_high) * \
                           (v_high - voltage) / (v_high - v_low)
                return (1.0 - consumed / self._max_capacity) * 100.0

        return 0.0

    def update(self, disable_cap_discharge_offset=False):
        """Read voltage once and compute all derived values.

        Stores results in voltage_v, state_of_charge_pct, state_of_charge_mah
        and runtime_remaining. Returns True if any value changed, False otherwise.
        """
        raw = self._read_voltage()
        self.voltage_samples.append(raw)
        voltage = sum(self.voltage_samples) / len(self.voltage_samples)

        pct = self._voltage_to_soc_pct(voltage)
        if self._capacity_discharge_offset and not disable_cap_discharge_offset:
            norm = max(0.0, min(1.0, (voltage - self._v_min) / (self._v_max - self._v_min)))
            pct -= self._capacity_discharge_offset * (1.0 - norm)
        pct = max(0.0, min(100.0, pct))

        new_voltage_v           = round(voltage, 3)
        new_soc_pct             = round(pct, 1)
        new_soc_mah             = round(self._max_capacity * pct / 100.0, 1)
        new_runtime_remaining   = round(self._full_runtime_min * pct / 100.0, 1) \
                                  if self._full_runtime_min is not None else None

        changed = (
            new_voltage_v         != self.voltage_v           or
            new_soc_pct           != self.state_of_charge_pct or
            new_soc_mah           != self.state_of_charge_mah or
            new_runtime_remaining != self.runtime_remaining
        )

        self.voltage_v           = new_voltage_v
        self.state_of_charge_pct = new_soc_pct
        self.state_of_charge_mah = new_soc_mah
        self.runtime_remaining   = new_runtime_remaining

        return changed

    def get_voltage(self):
        """Return the last measured battery voltage in V."""
        return self.voltage_v

    def get_state_of_charge_pct(self):
        """Return the last computed state of charge in %."""
        return self.state_of_charge_pct

    def get_state_of_charge_mah(self):
        """Return the last computed state of charge in mAh."""
        return self.state_of_charge_mah

    def get_runtime_remaining(self):
        """Return the last computed estimated remaining runtime in minutes, or None."""
        return self.runtime_remaining

    def get_status(self):
        """Return all current battery values as a dict.

        Returns:
            {
                "voltage_v":           float        — last measured voltage in V,
                "state_of_charge_mah": float        — last computed state of charge in mAh,
                "state_of_charge_pct": float        — last computed state of charge in %,
                "runtime_remaining":   float | None — last computed remaining runtime in minutes,
                                                      None if full_runtime_min was not set.
            }
        """
        return {
            "voltage_v":           self.voltage_v,
            "state_of_charge_mah": self.state_of_charge_mah,
            "state_of_charge_pct": self.state_of_charge_pct,
            "runtime_remaining":   self.runtime_remaining,
        }


if __name__ == "__main__":
    # Raspberry Pi example (MCP3001 via SPI):
    battery = Battery(
        cell_type="panasonic_ncr18650ga",
        adc_vref=3.3,
        voltage_scale=2.0,       # adjust for your voltage divider
        avg_samples=10,
        capacity_discharge_offset=1.0,
        spi_bus=0,
        spi_device=0,
        spi_resolution=10,
    )

    # ESP32 example (native analog pin):
    # battery = Battery(
    #     cell_type="panasonic_ncr18650ga",
    #     adc_vref=3.3,
    #     voltage_scale=2.0,
    #     avg_samples=10,
    #     analog_pin=34,
    # )

    while True:
        battery.update()
        status = battery.get_status()
        print(f"Voltage: {status['voltage_v']:.3f} V | "
              f"{status['state_of_charge_mah']:.0f} mAh | "
              f"{status['state_of_charge_pct']:.1f} %")
        sleep(1)
