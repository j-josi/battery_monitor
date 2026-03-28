#!/usr/bin/env python3
"""Battery State of Charge (SOC) logger — writes current battery status to a JSON file.

Purpose:
    Runs as a standalone background process that periodically reads the battery
    voltage via ADC, estimates the remaining capacity, and writes the result to
    a JSON file. Other applications (e.g. CamUI-WebRTC) can read this file
    independently without needing direct access to the battery hardware or this
    module. This decouples battery monitoring from the consuming application.

Usage:
    python scripts/battery_soc_logger.py <output_file> [options]

Example:
    python scripts/battery_soc_logger.py /run/battery.json \
        --cell-type panasonic_ncr18650ga \
        --voltage-scale 2.0 \
        --capacity-discharge-offset 1.0 \
        --interval 30

Output JSON format:
    {"capacity_pct": 85, "runtime_min": null}

    capacity_pct  — remaining capacity in percent (0–100)
    runtime_min   — estimated remaining runtime in minutes (null if not implemented)

The output file is updated atomically (write to temp file, then os.replace)
so readers never observe a partially written file.
"""

import argparse
import json
import os
import sys
import time

# Add repo root to path so battery_monitor package is importable
# regardless of the working directory the script is called from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from battery_monitor.battery import Battery


def parse_args():
    parser = argparse.ArgumentParser(
        description="Write battery state to a JSON file."
    )
    parser.add_argument(
        "output_file",
        help="Path to the JSON output file (e.g. /run/battery.json)",
    )
    parser.add_argument(
        "--cell-type",
        default="panasonic_ncr18650ga",
        help="Built-in cell name or path to a discharge curve CSV "
             "(default: panasonic_ncr18650ga)",
    )
    parser.add_argument(
        "--adc-vref",
        type=float,
        default=3.3,
        help="ADC reference voltage in V (default: 3.3)",
    )
    parser.add_argument(
        "--voltage-scale",
        type=float,
        default=1.0,
        help="Voltage divider scaling factor (default: 1.0)",
    )
    parser.add_argument(
        "--avg-samples",
        type=int,
        default=10,
        help="Number of samples for moving average (default: 10)",
    )
    parser.add_argument(
        "--capacity-discharge-offset",
        type=float,
        default=0.0,
        help="Percentage points subtracted at the lower voltage cutoff "
             "to ensure 0%% is displayed before the battery cuts off (default: 0.0)",
    )
    parser.add_argument(
        "--spi-bus",
        type=int,
        default=0,
        help="SPI bus number for external ADC (default: 0)",
    )
    parser.add_argument(
        "--spi-device",
        type=int,
        default=0,
        help="SPI chip-select for external ADC (default: 0)",
    )
    parser.add_argument(
        "--spi-resolution",
        type=int,
        default=10,
        help="Bit resolution of the external SPI ADC (default: 10)",
    )
    parser.add_argument(
        "--analog-pin",
        type=int,
        default=None,
        help="Native analog pin number (ESP32 etc.); overrides SPI params",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="Measurement interval in seconds (default: 30)",
    )
    parser.add_argument(
        "--full-runtime-min",
        type=float,
        default=None,
        help="Estimated runtime in minutes at 100%% charge. "
             "When set, runtime_min in the JSON output is estimated proportionally. "
             "(default: None — runtime_min will be null in output)",
    )
    return parser.parse_args()


def write_atomic(path, data):
    """Write JSON atomically: write to a temp file, then rename."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def main():
    args = parse_args()

    battery = Battery(
        cell_type=args.cell_type,
        adc_vref=args.adc_vref,
        voltage_scale=args.voltage_scale,
        avg_samples=args.avg_samples,
        capacity_discharge_offset=args.capacity_discharge_offset,
        full_runtime_min=args.full_runtime_min,
        spi_bus=args.spi_bus,
        spi_device=args.spi_device,
        spi_resolution=args.spi_resolution,
        analog_pin=args.analog_pin,
    )

    print(f"Battery logger started — writing to {args.output_file} every {args.interval}s")

    while True:
        try:
            battery.update()
            status = battery.get_status()
            write_atomic(args.output_file, status)
            runtime_str = f"| {status['runtime_remaining']:.0f} min" \
                if status["runtime_remaining"] is not None else ""
            print(f"  {status['voltage_v']:.3f} V | {status['state_of_charge_mah']:.0f} mAh "
                  f"| {status['state_of_charge_pct']:.1f} % {runtime_str}")
        except Exception as exc:
            print(f"  Error: {exc}", file=sys.stderr)

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
