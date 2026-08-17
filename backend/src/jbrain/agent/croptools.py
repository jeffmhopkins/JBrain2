"""The `crop_regions` tool handler (docs/plans/AGENT_CANVAS_PLAN.md W4, lane L3).

Cut N regions out of one image and return them as N saveable chat images under a single
`image_set` card.

**Why one card and not N cards.** `ToolOutput.view` is a single slot and persistence is
one view per tool call (`transcript_accumulator` keeps the last), so a handler emitting N
view events shows N cards live and ONE after a reload. A single view carrying N image ids
— the shape `video_analysis` already uses for frame thumbs — is the only contract that
survives the owner reopening the chat.

**The honest failure mode, surfaced rather than hidden.** VLM grounding *undercounts
silently*: ask for "each face" in a crowd and it returns six boxes for fourteen people
with no error anywhere. So the result always states the count found, and every crop
carries the `origin` of its box (a real detector vs the model's guess) which the card
renders next to the region it claims. That is also why the chosen gallery keeps the
source photo visible (§10.7) — a wrong crop is only obvious beside the region it came
from.
"""

from __future__ import annotations

import io

import structlog
from PIL import Image, ImageOps
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.attachments import TurnAttachmentRepo
from jbrain.agent.chat_images import (
    ImageTooLarge,
    UndecodableImage,
    image_dimensions,
    persist_chat_image,
    resolve_source,
    vision_read_spec,
)
from jbrain.agent.contracts import ViewPayload
from jbrain.agent.grounding import (
    GroundingError,
    UnknownGroundingModel,
    parse_grounding,
    to_pixels,
)
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.llm import LlmImage, LlmRouter
from jbrain.llm.errors import LlmError
from jbrain.models.images import GeneratedImageRepo
from jbrain.storage import BlobStore
from jbrain.vision import FaceDetectUnavailable, RapidOcrClient, detect_faces

log = structlog.get_logger()

PROVENANCE_CROP = "crop"

# Owner-ratified cap (§10.4). Sits between MAX_COMPARE_IMAGES (6, an in-context read) and
# MAX_IMAGES_PER_TURN (20, the model-side ceiling), and covers a realistic group photo.
# Over-cap TRUNCATES with a note rather than erroring — the MAX_PDF_PAGES posture.
MAX_CROPS = 12

# A crop below this is not a usable image — it is a mis-grounded sliver, and returning it
# as a "face" would be worse than returning nothing.
MIN_CROP_PX = 16

_GROUND_SYSTEM = (
    "You locate things in images. Reply with ONLY a JSON array, no prose, no code fence. "
    'Each element: {"bbox_2d": [x1, y1, x2, y2], "label": "<what it is>"}. Corners, not '
    "width/height. Return one element per distinct instance. If nothing matches, reply []."
)


def build_crop_handlers(
    maker: async_sessionmaker[AsyncSession],
    blobs: BlobStore,
    images: GeneratedImageRepo,
    attachments: TurnAttachmentRepo,
    router: LlmRouter,
    ocr: RapidOcrClient | None = None,
) -> dict[str, ToolHandler]:
    async def crop_regions_tool(arguments: dict, ctx: ToolContext) -> str:
        attachment_id = str(arguments.get("source_attachment_id", "")).strip()
        image_id = str(arguments.get("source_image_id", "")).strip()
        if bool(attachment_id) == bool(image_id):
            return "Give exactly one of source_attachment_id or source_image_id."
        target = str(arguments.get("target", "")).strip()
        boxes_in = arguments.get("boxes")
        if not target and not isinstance(boxes_in, list):
            return (
                "crop_regions needs either `target` (what to find, in words) or `boxes` "
                "(explicit pixel boxes you already know)."
            )

        resolved = await resolve_source(
            image_id,
            attachment_id,
            session_ctx=ctx.session,
            agent_session_id=ctx.agent_session_id,
            blobs=blobs,
            repo=images,
            attachments=attachments,
            maker=maker,
        )
        if isinstance(resolved, str):
            return resolved
        data, _sha = resolved
        try:
            width, height = image_dimensions(data)
        except (UndecodableImage, ImageTooLarge) as exc:
            return str(exc)

        if isinstance(boxes_in, list):
            regions, note = _explicit_boxes(boxes_in, width, height)
            origin = "owner"
        elif _wants_faces(target) and ocr is not None:
            # Faces route to the deterministic detector, not the vision model: VLM
            # grounding misses people in a crowd and reports no error (§6.5). If the
            # detector is unavailable we fall back — and SAY we fell back, because a
            # quietly-degraded count is the failure this whole lane guards against.
            detected = await _faces(ocr, data)
            if isinstance(detected, tuple):
                regions, note, origin = detected[0], "", "detector"
            else:
                found = await _ground(ctx, data, target, width, height)
                if isinstance(found, str):
                    return found
                regions = found
                note = (
                    f"The face detector was unavailable ({detected}), so these boxes are"
                    " the vision model's guess — it can miss people in a crowd."
                )
                origin = "vlm"
        else:
            found = await _ground(ctx, data, target, width, height)
            if isinstance(found, str):
                return found
            regions, note = found, ""
            origin = "vlm"
        if not regions:
            return (
                f"Found nothing matching {target!r} in that image — nothing was cropped. "
                "Try naming the thing differently, or pass explicit `boxes`."
            )

        truncated = len(regions) > MAX_CROPS
        regions = regions[:MAX_CROPS]

        try:
            source = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        except Exception as exc:  # noqa: BLE001 - a decode failure is recoverable
            log.warning("crop.source_undecodable", error=str(exc))
            return "That image could not be opened for cropping."

        crops: list[dict] = []
        skipped = 0
        for index, (box, label) in enumerate(regions, start=1):
            if (box.x2 - box.x1) < MIN_CROP_PX or (box.y2 - box.y1) < MIN_CROP_PX:
                skipped += 1
                continue
            buf = io.BytesIO()
            source.crop((box.x1, box.y1, box.x2, box.y2)).save(buf, format="PNG")
            try:
                row = await persist_chat_image(
                    maker,
                    ctx.session,
                    blobs,
                    images,
                    data=buf.getvalue(),
                    provenance=PROVENANCE_CROP,
                    model="crop",
                    prompt=label or f"{target or 'region'} {index}",
                )
            except (UndecodableImage, ImageTooLarge) as exc:
                log.warning("crop.persist_failed", error=str(exc))
                skipped += 1
                continue
            crops.append(
                {
                    "image_id": str(row.id),
                    "label": label or f"{target or 'region'} {index}",
                    # Fractions so the card can draw the region on the source at any
                    # rendered size — the same unit the canvas scene stores.
                    "box": [
                        round(box.x1 / width, 5),
                        round(box.y1 / height, 5),
                        round(box.x2 / width, 5),
                        round(box.y2 / height, 5),
                    ],
                    "w": box.x2 - box.x1,
                    "h": box.y2 - box.y1,
                    "origin": origin,
                }
            )
        if not crops:
            return "Every region found was too small to be a usable crop — nothing was cut."

        summary = [
            f"Cut {len(crops)} crop(s) from that image and showed them to the owner"
            f" (image_ids: {', '.join(c['image_id'] for c in crops)})."
        ]
        # The count is stated because an undercount is the documented, SILENT failure —
        # the model must be able to notice it and say so rather than imply completeness.
        summary.append(
            "That is what was FOUND, not necessarily all there are — say so if the owner"
            " expected more."
        )
        if truncated:
            summary.append(f"More than {MAX_CROPS} matched; only the first {MAX_CROPS} were cut.")
        if skipped:
            summary.append(f"{skipped} region(s) were too small to crop and were skipped.")
        if note:
            summary.append(note)
        return ToolOutput(
            " ".join(summary),
            view=ViewPayload(
                view="image_set",
                data={
                    "source_kind": "attachment" if attachment_id else "image",
                    "source_id": attachment_id or image_id,
                    "label": target or "regions",
                    "crops": crops,
                    "truncated": truncated,
                },
            ),
        )

    async def _ground(
        ctx: ToolContext, data: bytes, target: str, width: int, height: int
    ) -> list[tuple] | str:
        try:
            spec = await vision_read_spec(router, ctx.model_override)
            provider, model = await router.effective_spec("agent.vision", "vision")
            result = await router.complete(
                "agent.vision",
                system=_GROUND_SYSTEM,
                user_text=f"Locate {target}. One element per distinct instance.",
                images=[LlmImage(media_type="image/png", data=_b64(data))],
                spec_override=spec,
                strength="vision",
            )
        except LlmError as exc:
            return f"Couldn't look at that image: {exc}"
        raw_boxes, _points = parse_grounding(result.text)
        try:
            pixels = to_pixels(raw_boxes, served_model=spec or model, width=width, height=height)
        except UnknownGroundingModel as exc:
            # Refuse rather than guess: a wrong coordinate base crops confidently wrong.
            log.warning("crop.unqualified_model", model=spec or model)
            return f"Can't crop with the current vision model: {exc}"
        except GroundingError as exc:
            return f"That model's box coordinates couldn't be read: {exc}"
        return [(b, b.label) for b in pixels]

    return {"crop_regions": crop_regions_tool}


# Matching on the word is deliberate and narrow: "face" is the one target with an on-box
# detector, and routing anything else to it would silently return face boxes for a
# question about product labels.
_FACE_WORDS = ("face", "faces", "person", "people", "everyone", "headshot")


def _wants_faces(target: str) -> bool:
    words = {w.strip(".,!?").lower() for w in target.split()}
    return bool(words & set(_FACE_WORDS))


async def _faces(ocr: RapidOcrClient, data: bytes) -> tuple[list[tuple], str] | str:
    """Detector boxes, or a reason string the caller reports while falling back."""
    from jbrain.agent.grounding import PixelBox

    try:
        found = await detect_faces(ocr, data)
    except FaceDetectUnavailable as exc:
        return str(exc)
    return (
        [
            (PixelBox(f.x1, f.y1, f.x2, f.y2, label=f"face {i + 1}"), f"face {i + 1}")
            for i, f in enumerate(found)
        ],
        "",
    )


def _explicit_boxes(raw: list, width: int, height: int) -> tuple[list[tuple], str]:
    """Boxes the model already knows, in pixels — e.g. read off a canvas `look`."""
    from jbrain.agent.grounding import PixelBox

    out: list[tuple] = []
    bad = 0
    for entry in raw:
        if not isinstance(entry, dict):
            bad += 1
            continue
        try:
            x = int(float(entry["x"]))
            y = int(float(entry["y"]))
            w = int(float(entry["w"]))
            h = int(float(entry["h"]))
        except (KeyError, TypeError, ValueError):
            bad += 1
            continue
        x1, y1 = max(0, x), max(0, y)
        x2, y2 = min(width, x + w), min(height, y + h)
        if x2 <= x1 or y2 <= y1:
            bad += 1
            continue
        label = str(entry.get("label", "")).strip()
        out.append((PixelBox(x1, y1, x2, y2, label=label), label))
    note = f"{bad} box(es) were malformed or off-image and skipped." if bad else ""
    return out, note


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")
