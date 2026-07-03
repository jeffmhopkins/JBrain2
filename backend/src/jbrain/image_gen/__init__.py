"""Local diffusion image generation via ComfyUI (docs/archive/IMAGE_GEN_PLAN.md).

A sibling adapter to the LLM adapter: image generation is not an LLM call, so it
does not route through the LLM router, but `jbrain.image_gen` is the *only* path
to the image model (a localhost ComfyUI running Qwen-Image). Protocol + a fake;
all HTTP rides the shared `httpx.AsyncClient` — zero new runtime deps.
"""
