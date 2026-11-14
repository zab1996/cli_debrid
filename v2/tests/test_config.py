from app import config


def test_missing_file_gives_defaults():
    cfg = config.load()
    assert cfg.quality.name == "1080p" and cfg.quality.resolution_wanted == "=="
    assert cfg.quality.fallback.name == "720 and below"
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


def test_bad_resolution_rejected_and_old_settings_converted():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        config.Config.model_validate({"quality": {"max_resolution": "8k"}})
    old = config.Config.model_validate({"quality": {"preset": "4k", "never_contain": ["HDCAM"],
                                                    "must_contain": ["REMUX"]}})
    assert old.quality.max_resolution == "2160p"
    assert any(f.pattern == '"HDCAM"' for f in old.quality.filter_out)
    assert [f.pattern for f in old.quality.filter_in] == ['"REMUX"']
