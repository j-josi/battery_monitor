from collections import deque
from time import sleep, time
from gpiozero import MCP3001
import csv
import os


class Battery:
    def __init__(
        self,
        max_capacity: int,
        adc: MCP3001,
        adc_vref: float = 3.3,
        voltage_scale: float = 1.0,
        avg_samples: int = 10
    ):
        """
        :param max_capacity: Maximum battery capacity in mAh
        :param adc: MCP3001 instance
        :param adc_vref: ADC reference voltage (VDD)
        :param voltage_scale: Scaling factor for external voltage divider
        :param avg_samples: Number of samples for moving average
        """
        self.max_capacity = max_capacity
        self.adc = adc
        self.adc_vref = adc_vref
        self.voltage_scale = voltage_scale

        self.voltage_samples = deque(maxlen=avg_samples)
        self.filtered_voltage = 0.0

    def _read_voltage(self) -> float:
        """
        Read battery voltage from ADC, apply reference voltage
        and external scaling factor (e.g. voltage divider).
        """
        adc_voltage = self.adc.value * self.adc_vref
        return adc_voltage * self.voltage_scale

    def _update_voltage(self) -> float:
        """Calculate moving average of recent voltage measurements"""
        voltage = self._read_voltage()
        self.voltage_samples.append(voltage)
        self.filtered_voltage = sum(self.voltage_samples) / len(self.voltage_samples)
        return self.filtered_voltage

    def _voltage_to_capacity_mAh(self, voltage: float) -> float:
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

        # Linear interpolation between table points
        for i in range(len(table) - 1):
            v_high, c_high = table[i]
            v_low, c_low = table[i + 1]

            if v_low <= voltage <= v_high:
                slope = (c_high - c_low) / (v_high - v_low)
                return c_low + slope * (voltage - v_low)

        return 0.0

    def get_capacity(self) -> float:
        """
        Return remaining battery capacity in percent
        (based on moving averaged voltage).
        """
        voltage = self._update_voltage()
        capacity_mAh = self._voltage_to_capacity_mAh(voltage)
        percent = (capacity_mAh / self.max_capacity) * 100.0
        return max(0.0, min(100.0, percent))


if __name__ == "__main__":
    ADC_VREF = 3.3
    BATTERY_CAPACITY = 3342  # Panasonic NCR18650GA

    # Example: voltage divider with r1=100k and r2=220k
    VOLTAGE_SCALE = 1 / (220 / (100 + 220))  # = 1.4545

    LOGFILE = "battery_log_2.csv"
    LOG_INTERVAL = 60  # seconds

    mcp3001 = MCP3001()
    battery = Battery(
        max_capacity=BATTERY_CAPACITY,
        adc=mcp3001,
        adc_vref=ADC_VREF,
        voltage_scale=VOLTAGE_SCALE,
        avg_samples=10
    )

    start_time = time()
    last_log_time = 0

    # Create CSV file with header if it does not exist
    file_exists = os.path.isfile(LOGFILE)
    if not file_exists:
        with open(LOGFILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["runtime_minutes", "voltage_V", "capacity_percent"])

    while True:
        voltage = battery.filtered_voltage
        capacity = battery.get_capacity()

        runtime_minutes = (time() - start_time) / 60.0

        if time() - last_log_time >= LOG_INTERVAL:
            with open(LOGFILE, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    f"{runtime_minutes:.1f}",
                    f"{voltage:.3f}",
                    f"{capacity:.1f}"
                ])

            last_log_time = time()

        print(
            f"Runtime: {runtime_minutes:.1f} min | "
            f"Voltage: {voltage:.3f} V | "
            f"Capacity: {capacity:.1f} %"
        )

        sleep(1)
