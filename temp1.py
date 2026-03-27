import spidev
spi = spidev.SpiDev()
spi.open(0,0)
spi.max_speed_hz = 500000
spi.mode = 0

print(spi.xfer2([0x00, 0x00]))


for m in range(4):
    spi.mode = m
    print(m, spi.xfer2([0,0]))
