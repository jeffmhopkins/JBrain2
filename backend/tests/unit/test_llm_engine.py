from jbrain.llm import engine


def test_parse_accepts_known_engines_and_defaults_the_rest() -> None:
    assert engine.parse("standard") == "standard"
    assert engine.parse("flash-next") == "flash-next"
    for bad in (None, "", "Flash-Next", 3, ["flash-next"]):
        assert engine.parse(bad) == engine.DEFAULT_ENGINE


def test_each_engine_has_its_own_service_and_config_file() -> None:
    assert len(set(engine.SERVICE.values())) == len(engine.ENGINES)
    assert len(set(engine.CONFIG_FILE.values())) == len(engine.ENGINES)
    assert engine.SERVICE[engine.STANDARD] == "local-llm"
    assert engine.config_path("/models", engine.FLASH_NEXT) == (
        "/models/llama-swap.flash-next.yaml"
    )


def test_models_for_splits_the_catalog_by_engine() -> None:
    models = [
        {"id": "gpt-oss-120b"},  # predates the field: standard
        {"id": "qwen3.8-27b", "engine": "standard"},
        {"id": "qwen3.8-flash-next", "engine": "flash-next"},
    ]
    assert [m["id"] for m in engine.models_for(engine.STANDARD, models)] == [
        "gpt-oss-120b",
        "qwen3.8-27b",
    ]
    assert [m["id"] for m in engine.models_for(engine.FLASH_NEXT, models)] == ["qwen3.8-flash-next"]
