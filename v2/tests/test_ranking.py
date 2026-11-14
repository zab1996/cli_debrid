import pytest

from app.config import QualityConfig
from app.ranking import Query, Release
from app.ranking.filter import check, title_similarity
from app.ranking.score import rank

MOVIE = Query(kind="movie", title="The Matrix", year=1999, imdb_id="tt0133093")
EP = Query(kind="episode", title="Breaking Bad", season=1, episode=2, imdb_id="tt0903747")
SEASON = Query(kind="season", title="Breaking Bad", season=1, episode_count=7)
P1080 = QualityConfig(preset="1080p")
P4K = QualityConfig(preset="4k_1080p")


def R(title, size=2.0, proto="torrent", **kw):
    return Release(title=title, protocol=proto, indexer="t", size_gb=size,
                   info_hash=kw.pop("h", title.lower()[:40]), **kw)


@pytest.mark.parametrize("title,q,prof,reason", [
    ("The.Matrix.1999.1080p.BluRay.x264-GRP", MOVIE, P1080, ""),
    ("The.Matrix.Reloaded.2003.1080p.BluRay.x264", MOVIE, P1080, "title mismatch"),
    ("The.Matrix.1999.2160p.UHD.BluRay.x265", MOVIE, P1080, "resolution 2160p"),
    ("The.Matrix.1999.2160p.UHD.BluRay.x265", MOVIE, P4K, ""),
    ("The.Matrix.1999.HDCAM.x264", MOVIE, P1080, "cam/screener"),
    ("The.Matrix.1985.1080p.WEB", MOVIE, P1080, "year 1985"),
    ("The Matrix S01E01 1080p WEB", MOVIE, P1080, "looks like a show"),
    ("The.Matrix.1999.FRENCH.1080p.WEB.H264", MOVIE, QualityConfig(language="en"), "language fr"),
    ("The.Matrix.1999.MULTi.1080p.WEB.H264", MOVIE, QualityConfig(language="en"), ""),
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
    prof = QualityConfig(max_size_gb=3)
    big_pack = R("Breaking.Bad.S01.1080p.BluRay", size=14)  # 7 eps -> 2 GB each
    assert check(big_pack, SEASON, prof) == ""
    huge = R("Breaking.Bad.S01.1080p.BluRay", size=30)
    assert check(huge, SEASON, prof).startswith("too big")


def test_never_and_must_words():
    prof = QualityConfig(never_contain=["HDTS", "/\\bkor\\b/"], language="")
    assert check(R("The.Matrix.1999.1080p.KOR.WEB"), MOVIE, prof).startswith("contains")
    assert check(R("The.Matrix.1999.1080p.KOREAN.WEB"), MOVIE, prof) == ""
    prof2 = QualityConfig(must_contain=["remux"])
    assert check(R("The.Matrix.1999.1080p.WEB"), MOVIE, prof2) == "missing required word"


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
