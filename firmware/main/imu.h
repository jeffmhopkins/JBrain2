#pragma once

#include <stdbool.h>
#include <stdint.h>

/* The QMI8658 six-axis IMU at 0x6b, confirmed present by the bus scan (§10.4x).
 *
 * "Gyroscope" is what the board advertises, but keeping the robot upright wants the
 * ACCELEROMETER: gravity is a constant 1 g pointing down, and which way that lands on the
 * chip's axes is the whole answer. A gyro measures rotation RATE — it says the panel is
 * turning, never which way is up, and integrating it drifts. So the gyro stays powered down;
 * it costs current and answers a question nothing here is asking.
 *
 * THE AXES ARE NOT ASSUMED. Neither the vendor BSP nor the sample repo mentions this part, so
 * nothing on this box knows how the chip is glued down relative to the screen. Guessing the
 * sign gives a robot that is upside down all the time, which looks exactly like a bug — so
 * this release reports the raw readings and the next one acts on them.
 */

/* False means nothing answered at 0x6b, or it answered with the wrong WHO_AM_I. */
bool imu_start(void);

/* Raw accelerometer counts at +/-4 g, so 1 g is about 8192. False means the read failed. */
bool imu_read(int16_t *ax, int16_t *ay, int16_t *az);
