"""On-box vision service clients (docs/plans/RAPIDOCR_PLAN.md).

Currently the RapidOCR sidecar client — a deterministic CPU OCR engine that cross-validates
the VLM text extraction and backs the direct `ocr` tools (jerv + the jcode sandbox).
"""

from jbrain.vision.rapidocr import (
    FaceBox,
    FaceDetectUnavailable,
    OcrResult,
    OcrServiceError,
    RapidOcrClient,
    detect_faces,
)

__all__ = [
    "FaceBox",
    "FaceDetectUnavailable",
    "OcrResult",
    "OcrServiceError",
    "RapidOcrClient",
    "detect_faces",
]
