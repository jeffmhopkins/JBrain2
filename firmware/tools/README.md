# firmware/tools

## `face_preview.c` — look at the robot without a panel

`face.c` deliberately has no ESP dependencies, so it compiles on a host. This renders one
frame to a PPM, which is how the geometry gets checked before an OTA cycle rather than after.

```sh
cc -I../main -o /tmp/face face_preview.c ../main/face.c -lm
/tmp/face 0 /tmp/robot.ppm     # colour index; 0 is the robot's default steel
```

It has already earned itself once: it showed the figure sitting high with ~100 px of dead
black beneath, which is what revealed the omitted legs as load-bearing for the composition
rather than decoration (ROOM_ENDPOINT_PLAN.md §10.4p). W4's rig is where this matters more —
a wrong sign in a tween is free to find here and costs an update cycle to find on the wall.
