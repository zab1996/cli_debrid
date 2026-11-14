import os

from app.config import LibraryConfig
from app.downloads.engine import EngineFile
from app.library import match, symlink
from app.models import Item, State

GB = 1024 ** 3


def item(id, kind="episode", s=None, e=None, absolute=None, title="Breaking Bad", year=2008):
    return Item(id=id, kind=kind, tmdb_id=1396, tvdb_id=None, imdb_id=None, title=title, year=year,
                season=s, episode=e, absolute=absolute, state=State.DOWNLOADING)


def F(name, size=GB):
    return EngineFile(name=name, size=size, path=f"/mnt/x/{name}")


def test_movie_picks_biggest_video_and_skips_samples():
    files = [F("Movie.1999.1080p.mkv", 8 * GB), F("Sample/movie-sample.mkv", 60 * 1024 ** 2),
             F("Movie.nfo", 100), F("extras.mkv", 1 * GB)]
    assert match.for_movie(files).name == "Movie.1999.1080p.mkv"
    assert match.for_movie([F("a.nfo")]) is None


def test_pack_matching_by_season_episode_folder_and_absolute():
    files = [F("Breaking.Bad.S01E01.mkv"), F("Breaking.Bad.S01E02.mkv"),
             F("Season 2/Breaking Bad - 03.mkv"), F("0123456789abcdef0123456789abcdef.mkv")]
    items = [item(1, s=1, e=2), item(2, s=2, e=3), item(3, s=1, e=7)]
    got = match.for_episodes(files, items)
    assert got[1].name == "Breaking.Bad.S01E02.mkv"
    assert got[2].name == "Season 2/Breaking Bad - 03.mkv"
    assert 3 not in got
    anime = [F("[SubsPlease] One Piece - 1100 (1080p).mkv")]
    assert match.for_episodes(anime + [F("x.S01E01.mkv")], [item(9, s=21, e=9, absolute=1100)])[9] == anime[0]


def test_single_file_single_item():
    assert match.for_episodes([F("whatever.mkv")], [item(1, s=3, e=4)])[1].name == "whatever.mkv"


def test_symlink_names_and_replace(tmp_path):
    lib = LibraryConfig(movies_path=str(tmp_path / "movies"), shows_path=str(tmp_path / "shows"))
    src = tmp_path / "src.mkv"
    src.write_text("x")
    m = item(1, kind="movie", title='What: If? "Cut"', year=2021)
    p = symlink.place(m, lib, str(src))
    assert p == tmp_path / "movies" / "What If Cut (2021) {tmdb-1396}" / "What If Cut (2021).mkv"
    assert os.readlink(p) == str(src)
    src2 = tmp_path / "src.mp4"
    src2.write_text("y")
    p2 = symlink.place(m, lib, str(src2))
    assert p2.suffix == ".mp4" and not p.exists() and os.readlink(p2) == str(src2)
    ep = symlink.place(item(2, s=1, e=2), lib, str(src))
    assert ep.relative_to(tmp_path).as_posix() == \
        "shows/Breaking Bad (2008) {tmdb-1396}/Season 01/Breaking Bad (2008) - S01E02.mkv"
    symlink.remove(str(ep))
    assert not (tmp_path / "shows" / "Breaking Bad (2008) {tmdb-1396}").exists()
