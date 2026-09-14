from __future__ import annotations

import os
import time

import pytest
import responses
from beets.library import Item
from beets.test.helper import PluginTestHelper
from requests.exceptions import ConnectionError as RequestsConnectionError

from beetsplug.genresync import GenreSyncPlugin

MB_RELEASE = "https://musicbrainz.org/ws/2/release/{0}"
MB_RELEASE_GROUP = "https://musicbrainz.org/ws/2/release-group/{0}"
DISCOGS_RELEASE = "https://api.discogs.com/releases/{0}"


def mb_body(*names):
    return {"genres": [{"name": n} for n in names]}


def discogs_body(genres=None, styles=None):
    return {"genres": genres or [], "styles": styles or []}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    # Retry/backoff logic sleeps between attempts; skip that in tests.
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)


class TestDedupe:
    def test_case_insensitive_first_occurrence_wins(self):
        result = GenreSyncPlugin._dedupe(["Rock", "rock", "Pop"])
        assert result == ["Rock", "Pop"]

    def test_strips_whitespace_and_drops_blanks(self):
        result = GenreSyncPlugin._dedupe(["  Jazz  ", "", "   "])
        assert result == ["Jazz"]


class TestGenreSync(PluginTestHelper):
    plugin = "genresync"

    @pytest.fixture(autouse=True)
    def _plugin_instance(self):
        self.genresync = GenreSyncPlugin()

    @responses.activate
    def test_musicbrainz_merges_release_and_release_group(self):
        responses.add(
            responses.GET, MB_RELEASE.format("rel-1"), json=mb_body("rock")
        )
        responses.add(
            responses.GET,
            MB_RELEASE_GROUP.format("rg-1"),
            json=mb_body("indie rock", "rock"),
        )
        album = self.add_album(mb_albumid="rel-1", mb_releasegroupid="rg-1")

        names, ok = self.genresync._musicbrainz_genres(album)

        assert ok is True
        assert names == ["Rock", "Indie Rock"]

    @responses.activate
    def test_musicbrainz_retries_503_then_succeeds(self):
        responses.add(responses.GET, MB_RELEASE.format("rel-1"), status=503)
        responses.add(
            responses.GET, MB_RELEASE.format("rel-1"), json=mb_body("rock")
        )
        album = self.add_album(mb_albumid="rel-1")

        names, ok = self.genresync._musicbrainz_genres(album)

        assert ok is True
        assert names == ["Rock"]

    @responses.activate
    def test_musicbrainz_retries_connection_error_then_succeeds(self):
        responses.add(
            responses.GET,
            MB_RELEASE.format("rel-1"),
            body=RequestsConnectionError("connection reset"),
        )
        responses.add(
            responses.GET, MB_RELEASE.format("rel-1"), json=mb_body("rock")
        )
        album = self.add_album(mb_albumid="rel-1")

        names, ok = self.genresync._musicbrainz_genres(album)

        assert ok is True
        assert names == ["Rock"]

    @responses.activate
    def test_musicbrainz_gives_up_after_max_retries(self):
        for _ in range(4):
            responses.add(responses.GET, MB_RELEASE.format("rel-1"), status=503)
        album = self.add_album(mb_albumid="rel-1")

        names, ok = self.genresync._musicbrainz_genres(album)

        assert ok is False
        assert names == []

    def test_musicbrainz_skipped_without_ids(self):
        album = self.add_album()

        names, ok = self.genresync._musicbrainz_genres(album)

        assert (names, ok) == ([], True)

    @responses.activate
    def test_discogs_returns_genres_and_styles(self):
        responses.add(
            responses.GET,
            DISCOGS_RELEASE.format("123"),
            json=discogs_body(genres=["Rock"], styles=["Indie Rock"]),
        )
        album = self.add_album(discogs_albumid="123")

        genres, styles, ok = self.genresync._discogs_genres(album)

        assert ok is True
        assert genres == ["Rock"]
        assert styles == ["Indie Rock"]

    @responses.activate
    def test_discogs_logs_failure(self):
        responses.add(responses.GET, DISCOGS_RELEASE.format("123"), status=500)
        album = self.add_album(discogs_albumid="123")

        genres, styles, ok = self.genresync._discogs_genres(album)

        assert (genres, styles, ok) == ([], [], False)

    def test_discogs_skipped_without_release_id(self):
        album = self.add_album()

        genres, styles, ok = self.genresync._discogs_genres(album)

        assert (genres, styles, ok) == ([], [], True)

    def test_sync_album_no_data_logs_info(self, caplog):
        album = self.add_album()

        self.genresync.sync_album(album)

        assert any(
            "no genre data found" in r.message and r.levelname == "INFO"
            for r in caplog.records
        )

    @responses.activate
    def test_sync_album_logs_warning_on_failed_lookup(self, caplog):
        for _ in range(4):
            responses.add(responses.GET, MB_RELEASE.format("rel-1"), status=503)
        album = self.add_album(mb_albumid="rel-1")

        self.genresync.sync_album(album)

        assert any(
            "skipping, genre lookup failed" in r.message and r.levelname == "WARNING"
            for r in caplog.records
        )

    @responses.activate
    def test_sync_album_unchanged_ignores_order(self, caplog):
        responses.add(
            responses.GET,
            MB_RELEASE_GROUP.format("rg-1"),
            json=mb_body("rock", "indie rock"),
        )
        album = self.add_album(
            mb_releasegroupid="rg-1", genres=["Indie Rock", "Rock"]
        )

        self.genresync.sync_album(album)

        assert any("genres unchanged" in r.message for r in caplog.records)

    @responses.activate
    def test_sync_album_dry_run_does_not_write(self):
        responses.add(
            responses.GET, MB_RELEASE_GROUP.format("rg-1"), json=mb_body("rock")
        )
        album = self.add_album(mb_releasegroupid="rg-1")

        self.genresync.sync_album(album, dry_run=True)

        album.load()
        assert list(album.genres or []) == []

    def _add_album_with_backdated_file(self, **values):
        # add_album_fixture()/add_item_fixture() depend on media resource
        # files (test/rsrc/*) that beets doesn't ship in its installed
        # package, only in its own source checkout. Our os.stat/os.utime
        # calls just need a real file to exist, not a parseable one, so
        # create an empty one ourselves and stub out the actual tag write
        # (mutagen's behavior on a real file isn't what we're testing here).
        # Backdate it so a later "bump to now" is unambiguously detectable.
        album = self.add_album(**values)
        item = album.items()[0]
        os.makedirs(os.path.dirname(item.path), exist_ok=True)
        open(item.path, "wb").close()
        old_time = time.time() - 100_000
        os.utime(item.path, (old_time, old_time))
        return album, item, old_time

    @staticmethod
    def _bump_mtime_try_write(self, *args, **kwargs):
        # Stands in for a real tag write, which changes the file's mtime.
        os.utime(self.path, None)

    @responses.activate
    def test_sync_album_writes_and_preserves_mtime_by_default(self, monkeypatch):
        responses.add(
            responses.GET, MB_RELEASE_GROUP.format("rg-1"), json=mb_body("rock")
        )
        monkeypatch.setattr(Item, "try_write", self._bump_mtime_try_write)
        album, item, old_time = self._add_album_with_backdated_file(
            mb_releasegroupid="rg-1"
        )

        self.genresync.sync_album(album)

        album.load()
        assert list(album.genres) == ["Rock"]
        item.load()
        assert list(item.genres) == ["Rock"]
        assert os.stat(item.path).st_mtime == old_time

    @responses.activate
    def test_sync_album_update_mtime_true_allows_bump(self, monkeypatch):
        responses.add(
            responses.GET, MB_RELEASE_GROUP.format("rg-1"), json=mb_body("rock")
        )
        monkeypatch.setattr(Item, "try_write", self._bump_mtime_try_write)
        album, item, old_time = self._add_album_with_backdated_file(
            mb_releasegroupid="rg-1"
        )

        self.config["genresync"]["update_mtime"] = True
        self.genresync.sync_album(album)

        assert os.stat(item.path).st_mtime != old_time
