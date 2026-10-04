# Waveshare's STEP -> GLB, keeping the STEP's per-face colours as glTF materials.
# Usage: python step2glb.py <in.stp> <out.glb>
import sys

from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.Message import Message_ProgressRange
from OCP.OCP.collections import (
    IndexedDataMap_TCollection_AsciiString_TCollection_AsciiString as FileInfo,
)
from OCP.RWGltf import RWGltf_CafWriter
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
from OCP.TDocStd import TDocStd_Document
from OCP.XCAFDoc import XCAFDoc_DocumentTool

doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
reader = STEPCAFControl_Reader()
reader.SetColorMode(True)
reader.SetNameMode(True)
reader.ReadFile(sys.argv[1])
reader.Transfer(doc)
shapes = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
# 0.01 mm chord: fine enough that the USB-C shell and button caps stay round up close.
BRepMesh_IncrementalMesh(shapes.GetOneShape(), 0.01, False, 0.15, True)
RWGltf_CafWriter(TCollection_AsciiString(sys.argv[2]), True).Perform(
    doc, FileInfo(), Message_ProgressRange()
)
