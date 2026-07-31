"""On-box vision service clients (docs/plans/RAPIDOCR_PLAN.md).

Currently the RapidOCR sidecar client — a deterministic CPU OCR engine that cross-validates
the VLM text extraction and backs the direct `ocr` tools (jerv + the jcode sandbox).
"""

from jbrain.vision.rapidocr import OcrResult, OcrServiceError, RapidOcrClient

__all__ = ["OcrResult", "OcrServiceError", "RapidOcrClient"]
