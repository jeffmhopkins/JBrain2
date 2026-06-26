# Anthropic<->OpenAI shim for code mode (jcode): a LiteLLM proxy that serves the
# Anthropic /v1/messages API the Claude Agent SDK calls and translates it to the
# on-box llama-swap gateway's OpenAI API. The config is BAKED from this dir, so there
# is no host-file bind mount to lay down on update.
#
# SECURITY: LiteLLM's PyPI 1.82.7 / 1.82.8 shipped credential-stealing malware. The
# published Docker stable tag is a separate artifact, but treat the supply chain with
# care: PIN A DIGEST once verified on the box, never move to those versions, and bump
# the tag deliberately. The tag is an ARG so pinning is a one-line build-arg change.
ARG LITELLM_TAG=main-stable
FROM ghcr.io/berriai/litellm:${LITELLM_TAG}

COPY litellm-config.yaml /app/config.yaml

# The base image's entrypoint is `litellm`; pin it explicitly so a base change can't
# silently swap the command out from under us.
ENTRYPOINT ["litellm"]
CMD ["--config", "/app/config.yaml", "--host", "0.0.0.0", "--port", "4000"]
