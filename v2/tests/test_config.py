from app import config


def test_missing_file_gives_defaults():
    cfg = config.load()
    assert cfg.quality.preset == "1080p"
    assert cfg.library.mode == "symlink"
    assert cfg.sources == []


def test_save_and_reload_roundtrip():
    cfg = config.Config()
    cfg.plex.url = "http://plex:32400"
    cfg.indexers.append(config.IndexerConfig(type="torrentio"))
    cfg.advanced["search_limit"] = 5
    config.save(cfg)
    again = config.load()
    assert again.plex.url == "http://plex:32400"
    assert again.indexers[0].type == "torrentio"
    assert again.adv("search_limit", 1) == 5
    assert again.adv("missing", 7) == 7


def test_unknown_preset_rejected():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        config.Config.model_validate({"quality": {"preset": "8k"}})
