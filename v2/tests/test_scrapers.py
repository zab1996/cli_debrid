from app.config import IndexerConfig
from app.ranking import Query
from app.scrapers import hash_from_magnet, jackett, magnet_for, newznab, prowlarr, torrentio, zilean

MOVIE = Query(kind="movie", title="The Matrix", year=1999, imdb_id="tt0133093")
EP = Query(kind="episode", title="Breaking Bad", season=1, episode=2, imdb_id="tt0903747")
SEASON = Query(kind="season", title="Breaking Bad", season=1, imdb_id="tt0903747")


def test_magnet_hash_hex_and_base32():
    h = "a" * 40
    assert hash_from_magnet(f"magnet:?xt=urn:btih:{h.upper()}&dn=x") == h
    assert hash_from_magnet("magnet:?xt=urn:btih:" + "A" * 32) == "00" * 20
    assert hash_from_magnet("nope") == ""
    assert hash_from_magnet(magnet_for(h, "x y")) == h


def test_torrentio_urls_and_parse():
    idx = IndexerConfig(type="torrentio")
    assert torrentio.url_for(idx, MOVIE).endswith("/stream/movie/tt0133093.json")
    assert torrentio.url_for(idx, EP).endswith("/stream/series/tt0903747:1:2.json")
    assert torrentio.url_for(idx, SEASON).endswith("/stream/series/tt0903747:1:1.json")
    data = {"streams": [
        {"infoHash": "ABC", "title": "Breaking.Bad.S01.1080p\nBreaking.Bad.S01E02.mkv\n👤 42 💾 1.5 GB ⚙️ TPB"},
        {"infoHash": "", "title": "x"},
    ]}
    rs = torrentio.parse(data, "torrentio")
    assert len(rs) == 1 and rs[0].info_hash == "abc" and rs[0].seeders == 42
    assert rs[0].size_gb == 1.5 and rs[0].title == "Breaking.Bad.S01.1080p"


def test_zilean():
    assert zilean.params_for(EP) == {"Query": "Breaking Bad", "Season": 1, "Episode": 2}
    assert zilean.params_for(MOVIE) == {"Query": "The Matrix", "Year": 1999}
    rs = zilean.parse([{"raw_title": "X", "size": str(2 * 1024 ** 3), "info_hash": "AB"}])
    assert rs[0].size_gb == 2.0 and rs[0].info_hash == "ab"


def test_jackett_parse():
    data = {"Results": [
        {"Title": "A", "Size": 1024 ** 3, "Seeders": 3, "InfoHash": "FF", "Tracker": "x", "Imdb": 133093},
        {"Title": "B", "MagnetUri": "magnet:?xt=urn:btih:" + "b" * 40},
        {"Title": "C", "Link": "http://jackett/dl/file.torrent"},
    ]}
    rs = jackett.parse(data)
    assert [r.title for r in rs] == ["A", "B"]
    assert rs[0].indexer_imdb == "tt0133093"
    assert jackett.query_text(EP) == "Breaking Bad S01E02"


def test_prowlarr_parse_and_params():
    ps = prowlarr.params_list(EP)
    assert ps[0]["query"] == "{ImdbId:tt0903747}{Season:1}{Episode:2}"
    rs = prowlarr.parse([
        {"protocol": "usenet", "title": "N", "downloadUrl": "http://x/nzb", "guid": "g1", "size": 1},
        {"protocol": "torrent", "title": "T", "infoHash": "AA", "seeders": 5},
        {"protocol": "torrent", "title": "U", "downloadUrl": "http://x/t.torrent"},
    ])
    assert [(r.protocol, r.title) for r in rs] == [("nzb", "N"), ("torrent", "T")]


NZB_XML = """<?xml version="1.0"?>
<rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel>
<item><title>Breaking.Bad.S01E02.1080p.WEB</title><guid>g-1</guid>
<enclosure url="https://idx/getnzb/1" length="2147483648" type="application/x-nzb"/>
<newznab:attr name="imdb" value="0903747"/></item>
<item><title>No link</title></item>
</channel></rss>"""


def test_newznab_parse_and_params():
    rs = newznab.parse(NZB_XML, "newznab:idx")
    assert len(rs) == 1
    r = rs[0]
    assert r.size_gb == 2.0 and r.guid == "g-1" and r.indexer_imdb == "tt0903747"
    assert newznab.parse("<<not xml", "x") == []
    idx = IndexerConfig(type="newznab", url="https://idx", api_key="k")
    ps = newznab.params_list(idx, EP)
    assert ps[0]["t"] == "tvsearch" and ps[0]["imdbid"] == "0903747" and ps[0]["ep"] == 2
