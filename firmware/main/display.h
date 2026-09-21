#pragma once

#include <stdbool.h>

/* Bring up the AMOLED and draw the test pattern. NEVER fails fatally: a display fault must
   not stop this unit reaching the box, because reaching the box is how the fault gets
   fixed. Returns whether the panel came up; the caller logs it and carries on either way. */
bool display_start(void);

/* Redraw the test pattern, alternating it so a running panel is distinguishable from a
   frozen one by eye. Returns whether the SPI writes succeeded — which is NOT the same
   question as whether anything appeared. See display.c. */
bool display_repaint(void);

/* Start the render loop: the robot, and a tap that changes its colour. Its own task, so the
   OTA loop's timing is not something a frame rate can disturb. */
void display_run_face(void);

/* The loudest microphone sample since this was last called, 0..32767; reading clears it.
   Reported in telemetry so the microphone can be confirmed from the box rather than by
   asking someone to describe a sound. */
int display_mic_peak(void);

/* The panel's 0x51 brightness, 0..255. Also becomes what the periodic re-assert re-sends, so
   a setting cannot be quietly undone thirty seconds later by the recovery write. */
void display_set_brightness(int level);

/* The face task's smallest observed stack headroom, in words. Reported in telemetry so a
   near-overflow shows up as a shrinking number rather than as a panic. */
int display_stack_free(void);

/* The render loop stage reached just before the last restart, or -1 on a cold boot.
   1 loop top, 2 touch, 3 beep, 4 stack probe, 5 IMU, 6 face_draw, 7 label, 8 flip,
   9 full blit, 10 mic read, 11 meter blit, 12 panel re-assert, 13 PMU sample. */
int display_crash_phase(void);
