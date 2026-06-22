"""On-box fish identification via the fishial models (docs/FISH_ID_PLAN.md).

A sibling adapter to the image-gen adapter: classifying a fish is not an LLM call,
so it does not route through the LLM router, but `jbrain.fish_id` is the *only* path
to the fishial model (a localhost ROCm service running the DINOv2+ViT classifier,
the sibling of the `comfyui` profile). Protocol + a fake; all HTTP rides the shared
`httpx.AsyncClient` — zero new runtime deps. The photo never leaves the box, so there
is no egress Proposal (invariant #9 holds by construction).
"""
