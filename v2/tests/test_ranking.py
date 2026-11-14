import pytest

from app.ranking import Query, Release
from app.ranking.filter import check, title_similarity
from app.ranking.score import rank

MOVIE = Query(kind="movie", title="The Matrix", year=1999, imdb_id="tt0133093")
EP = Query(kind="episode", title="Breaking Bad", season=1, episode=2, imdb_id="tt0903747")
SEASON = Query(kind="season", title="Breaking Bad", season=1, episode_count=7)
from tests.conftest import plain_profile

P1080 = plain_profile(max_resolution="1080p")
P4K = plain_profile(max_resolution="2160p")


def R(title, size=2.0, proto="torrent", **kw):
    return Release(title=title, protocol=proto, indexer="t", size_gb=size,
                   info_hash=kw.pop("h", title.lower()[:40]), **kw)


@pytest.mark.parametrize("title,q,prof,reason", [
    ("The.Matrix.1999.1080p.BluRay.x264-GRP", MOVIE, P1080, ""),
    ("The.Matrix.Reloaded.2003.1080p.BluRay.x264", MOVIE, P1080, "title mismatch"),
    ("The.Matrix.1999.2160p.UHD.BluRay.x265", MOVIE, P1080, "resolution 2160p"),
    ("The.Matrix.1999.720p.WEB", MOVIE, plain_profile(max_resolution="1080p", resolution_wanted="=="), "resolution 720p"),
    ("The.Matrix.1999.1080p.HDR.WEB", MOVIE, plain_profile(enable_hdr=False), "HDR"),
    ("The.Matrix.1999.2160p.UHD.BluRay.x265", MOVIE, P4K, ""),
    ("The.Matrix.1999.HDCAM.x264", MOVIE, P1080, "cam/screener"),
    ("The.Matrix.1985.1080p.WEB", MOVIE, P1080, "year 1985"),
    ("The Matrix S01E01 1080p WEB", MOVIE, P1080, "looks like a show"),
    ("The.Matrix.1999.FRENCH.1080p.WEB.H264", MOVIE, plain_profile(language="en"), ""),  # penalised, not rejected
    ("Breaking.Bad.S01E02.1080p.WEB-DL", EP, P1080, ""),
    ("Breaking.Bad.S01E03.1080p.WEB-DL", EP, P1080, "wrong episode"),
    ("Breaking.Bad.S02E02.1080p.WEB-DL", EP, P1080, "wrong season"),
    ("Breaking.Bad.S01.1080p.BluRay.x264-ROVERS", EP, P1080, ""),
    ("Breaking.Bad.S01-S05.COMPLETE.1080p.BluRay", EP, P1080, ""),
    ("Mrs Browns Boys S01E02 1080i BluRay X264", Query(kind="episode", title="Mrs. Brown's Boys",
                                                       season=1, episode=2), P1080, ""),
    ("Breaking.Bad.S01E01-E03.1080p.WEB", SEASON, P1080, "partial pack"),
    ("Breaking.Bad.S01.1080p.BluRay.x264", SEASON, P1080, ""),
    ("[Erai-raws] Jujutsu Kaisen - 01 ~ 24 [1080p]",
     Query(kind="episode", title="Jujutsu Kaisen", season=1, episode=5), P1080, ""),
    ("[SubsPlease] One Piece - 1100 (1080p) [ABCD1234].mkv",
     Query(kind="episode", title="One Piece", season=21, episode=9, absolute=1100), P1080, ""),
    ("[SubsPlease] One Piece - 1101 (1080p) [ABCD1234].mkv",
     Query(kind="episode", title="One Piece", season=21, episode=9, absolute=1100), P1080,
     "wrong episode"),
    ("74c9a5b3f00d1e2f3a4b5c6d7e8f9012", MOVIE, P1080, "obfuscated name"),
])
def test_check(title, q, prof, reason):
    got = check(R(title), q, prof)
    assert got.split(" (")[0] == reason


def test_size_limits_are_per_episode():
    prof = plain_profile(max_size_gb=3)
    big_pack = R("Breaking.Bad.S01.1080p.BluRay", size=14)  # 7 eps -> 2 GB each
    assert check(big_pack, SEASON, prof) == ""
    huge = R("Breaking.Bad.S01.1080p.BluRay", size=30)
    assert check(huge, SEASON, prof).startswith("too big")


def test_filter_in_out_like_v1():
    from app.config import FilterPattern as F
    prof = plain_profile(filter_out=[F(pattern="HDTS"), F(pattern=r"\bKOR\b"), F(pattern='"x.y"'),
                                     F(pattern=r"(?-i:WEB-DL)", source="debrid")], language="")
    assert check(R("The.Matrix.1999.1080p.KOR.WEB"), MOVIE, prof).startswith("filter out")
    assert check(R("The.Matrix.1999.1080p.KOREAN.WEB"), MOVIE, prof) == ""
    assert check(R("The.Matrix.1999.1080p.x.y.WEB"), MOVIE, prof).startswith("filter out")
    # debrid-only pattern: rejects the torrent, not the same name on usenet; case-sensitive via (?-i:)
    assert check(R("The.Matrix.1999.1080p.WEB-DL"), MOVIE, prof).startswith("filter out")
    nzb = Release(title="The.Matrix.1999.1080p.WEB-DL", protocol="nzb", indexer="n", size_gb=2, guid="g", nzb_url="u")
    assert check(nzb, MOVIE, prof) == ""
    assert check(R("The.Matrix.1999.1080p.web-dl"), MOVIE, prof) == ""
    prof2 = plain_profile(filter_in=[F(pattern="remux")])
    assert check(R("The.Matrix.1999.1080p.WEB"), MOVIE, prof2) == "no filter-in match"


def test_bitrate_limits_use_runtime():
    q = Query(kind="movie", title="The Matrix", year=1999, runtime=136)
    prof = plain_profile(min_bitrate_mbps=3, max_bitrate_mbps=18)
    assert check(R("The.Matrix.1999.1080p.WEB", size=1.0), q, prof).startswith("bitrate too low")
    assert check(R("The.Matrix.1999.1080p.BluRay", size=40), q, prof).startswith("bitrate too high")
    assert check(R("The.Matrix.1999.1080p.BluRay", size=10), q, prof) == ""      # ~10.5 Mbps
    assert check(R("The.Matrix.1999.1080p.WEB", size=1.0), MOVIE, prof) == ""   # unknown runtime: skipped


def test_your_v1_1080p_version():
    """The ported 1.x '1080p' version with its '720 and below' fallback."""
    from app.config import Config
    prof = Config().quality
    q = Query(kind="movie", title="The Matrix", year=1999, runtime=136)
    ok = R("The.Matrix.1999.1080p.BluRay.x264-GRP", size=9, h="ok")
    assert check(ok, q, prof) == ""
    for title, why in [("The.Matrix.1999.1080p.HDR.BluRay.x265", "HDR"),
                       ("The.Matrix.1999.2160p.BluRay.x265", "resolution"),
                       ("The.Matrix.1999.1080p.WEB-DL.DDP5.1", "filter out"),      # debrid WEB-DL rule
                       ("The.Matrix.1999.1080p.BluRay.HINDI.x264", "filter out"),
                       ("The.Matrix.1999.1080p.DoVi.BluRay", ("HDR", "filter out"))]:
        assert check(R(title, size=9), q, prof).startswith(why), title
    ad = R("The.Matrix.1999.1080p.BluRay.Audio.Description.x264", size=9, h="ad")
    ranked = rank([ok, ad], q, prof)
    assert ranked[0].info_hash == "ok" and ranked[1].score < -5000
    # Nothing at 1080p -> the 720 fallback version is used.
    only720 = R("The.Matrix.1999.720p.BluRay.x264", size=5, h="sd")
    got = rank([only720], q, prof)
    assert [r.info_hash for r in got] == ["sd"] and got[0].parsed["_version"] == "720 and below"


def test_foreign_language_is_a_penalty():
    q = MOVIE
    prof = plain_profile(language="en")
    eng = R("The.Matrix.1999.1080p.WEB", h="en")
    fr = R("The.Matrix.1999.FRENCH.1080p.WEB", h="fr")
    multi = R("The.Matrix.1999.MULTi.1080p.WEB", h="mu")
    out = rank([fr, eng, multi], q, prof)
    assert out[-1].info_hash == "fr" and out[0].score - out[-1].score >= 299


def test_indexer_priority_bonus(monkeypatch):
    from app import config
    cfg = config.Config(indexers=[config.IndexerConfig(type="torrentio", priority=30)])
    monkeypatch.setattr(config, "_current", cfg)
    a = Release(title="The.Matrix.1999.1080p.WEB", protocol="torrent", indexer="torrentio", size_gb=2, info_hash="a")
    b = Release(title="The.Matrix.1999.1080p.WEB", protocol="torrent", indexer="zilean", size_gb=2, info_hash="b")
    out = rank([b, a], MOVIE, P1080)
    assert out[0].info_hash == "a" and round(out[0].score - out[1].score) == 30


def test_tried_releases_skipped():
    r = R("The.Matrix.1999.1080p.WEB", h="abc")
    assert check(r, MOVIE, P1080, tried={"abc"}) == "already tried"


def test_imdb_tag_trusted_over_title_and_rejects_other_ids():
    r = R("Matrix.1999.1080p.WEB", indexer_imdb="tt0133093")
    assert check(r, MOVIE, P1080) == ""
    r2 = R("The.Matrix.1999.1080p.WEB", indexer_imdb="tt9999999")
    assert check(r2, MOVIE, P1080) == "different imdb id"


def test_aliases_and_suffixes():
    assert title_similarity("F1 The Movie", ["F1"]) == 1.0
    assert title_similarity("war", ["WAR (2026)"]) == 1.0
    assert title_similarity("Special Ops Lioness", ["Lioness", "Special Ops: Lioness"]) == 1.0
    assert title_similarity("Law and Order SVU", ["Law & Order: SVU"]) == 1.0
    assert title_similarity("The Office US", ["The Office"]) == 0.9
    assert title_similarity("The Matrix Reloaded", ["The Matrix"]) < 0.82


def test_rank_order_and_dedupe():
    rs = [
        R("The.Matrix.1999.720p.WEB", size=1, h="a"),
        R("The.Matrix.1999.1080p.BluRay.x264", size=8, h="b"),
        R("The.Matrix.1999.1080p.WEBRip", size=2, h="c"),
        R("The.Matrix.1999.1080p.BluRay.x264", size=8, h="b"),
        R("The.Matrix.1999.HDCAM", size=1, h="d"),
    ]
    out = rank(rs, MOVIE, P1080)
    assert [r.info_hash for r in out] == ["b", "c", "a"]
    assert rs[4].reject == "cam/screener"


def test_4k_preferred_but_1080_fallback():
    rs = [R("The.Matrix.1999.1080p.BluRay", size=8, h="a"),
          R("The.Matrix.1999.2160p.UHD.BluRay.HDR", size=40, h="b")]
    assert rank(rs, MOVIE, P4K)[0].info_hash == "b"
    assert [r.info_hash for r in rank(rs[:1], MOVIE, P4K)] == ["a"]


def test_cached_beats_uncached():
    a = R("The.Matrix.1999.1080p.BluRay", size=8, h="a")
    b = R("The.Matrix.1999.1080p.WEBRip", size=2, h="b")
    b.cached = True
    assert rank([a, b], MOVIE, P1080)[0].info_hash == "b"


@pytest.mark.parametrize("title,q,reason", [
    ("Breaking.Bad.S01E02.1080p.WEB-DL", EP, ""),
    ("Breaking.Bad.S01E01E02.1080p.WEB-DL", EP, ""),
    ("Breaking.Bad.S01.1080p.BluRay.x264", EP, "usenet pack"),
    ("Breaking.Bad.S01-S05.COMPLETE.1080p.BluRay", EP, "usenet pack"),
    ("Breaking.Bad.S01.1080p.BluRay.x264", SEASON, "usenet pack"),
    ("[Erai-raws] Jujutsu Kaisen - 01 ~ 24 [1080p]",
     Query(kind="episode", title="Jujutsu Kaisen", season=1, episode=5), "usenet pack"),
    ("The.Matrix.1999.1080p.BluRay.x264", MOVIE, ""),
])
def test_no_usenet_packs(title, q, reason):
    r = Release(title=title, protocol="nzb", indexer="n", size_gb=2, nzb_url="u", guid=title)
    assert check(r, q, P1080) == reason
    # The same packs are fine as debrid torrents.
    if reason:
        assert check(R(title), q, P1080) in ("", "partial pack")
