import spidev
import time

# SPI initialisieren (Bus 0, Device 0 = CE0)
spi = spidev.SpiDev()
spi.open(0, 0)  # (Bus 0, CE0)
spi.max_speed_hz = 1000000
spi.mode = 0

VREF = 3.3  # Referenzspannung an MCP3001

def read_mcp3001():
    # MCP3001 liefert 2 Bytes (insgesamt 16 Bit)
    raw = spi.xfer2([0x00, 0x00])

    # 10-Bit Wert extrahieren
    # Datenformat laut Datasheet:
    # xxxx xxB9 B8B7 B6B5 B4B3 B2B1 B0xx
    value = ((raw[0] & 0x1F) << 5) | (raw[1] >> 3)

    return value

def read_voltage():
    adc_value = read_mcp3001()
    voltage = (adc_value / 1023.0) * VREF
    return voltage

try:
    while True:
        v = read_voltage()
        print(f"Spannung: {v:.3f} V")
        time.sleep(0.5)

except KeyboardInterrupt:
    spi.close()
    print("Beendet.")
