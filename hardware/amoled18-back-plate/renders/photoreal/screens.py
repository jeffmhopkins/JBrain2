# The two screen textures from pet_frame's PPMs. Each image is the panel as seen from the front:
# its columns run along the board's +x (the USB-C edge), its top row at the board's +y.
# Usage: python screens.py <work dir>
import sys
from pathlib import Path

from PIL import Image

work = Path(sys.argv[1])
Image.open(work / "pet_up.ppm").save(work / "screen_portrait.png")
# On the desk stand the USB-C edge is the sky, so the pet's head turns toward +x.
square = Image.open(work / "pet_side.ppm").crop((0, 40, 368, 408)).rotate(-90)
landscape = Image.new("RGB", (368, 448))
landscape.paste(square, (0, 40))
landscape.save(work / "screen_landscape.png")
