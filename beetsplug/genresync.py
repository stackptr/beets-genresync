from __future__ import annotations

import os
import re
import time
from typing import Iterable

import requests
from beets.plugins import BeetsPlugin
from beets.ui import Subcommand, decargs, print_

DISCOGS_RELEASE_URL = "https://api.discogs.com/releases/{release_id}"
MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/{entity}/{mbid}"
MUSICBRAINZ_MIN_INTERVAL = 1.0
MUSICBRAINZ_MAX_RETRIES = 3
USER_AGENT = "beets-genresync/0.1 ( https://github.com/stackptr/beets-genresync )"

# MBIDs are always UUIDs. Some albums end up with a non-MusicBrainz id (e.g.
# a Discogs release id) stored in mb_albumid/mb_releasegroupid by whatever
# imported them -- catch that before wasting a request on a guaranteed 400.
MUSICBRAINZ_MBID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

# MusicBrainz stores genre names lowercase (e.g. "idm", "uk garage"); a plain
# str.title() call mangles acronyms into "Idm"/"Uk Garage" instead of the
# conventional "IDM"/"UK Garage". Discogs' equivalents are already correctly
# cased, so this only matters for MB-sourced names.
MUSICBRAINZ_ACRONYMS = frozenset({"idm", "edm", "aor", "uk", "us"})

# Some releases carry dozens of low-vote MusicBrainz folksonomy tags, which
# swamps the handful of genres that actually have consensus behind them.
# Discogs' genre/style fields are curated rather than voted, so they're not
# subject to this and stay uncapped.
MUSICBRAINZ_GENRE_LIMIT = 15


class GenreSyncPlugin(BeetsPlugin):
    def __init__(self):
        super().__init__()
        self.config.add(
            {
                "auto": True,
                "discogs_token": None,
                "update_mtime": False,
            }
        )
        self._last_mb_request = 0.0
        self.register_listener("album_imported", self.album_imported)

    def album_imported(self, lib, album):
        if self.config["auto"].get(bool):
            self.sync_album(album)

    def commands(self):
        cmd = Subcommand(
            "genresync",
            help="sync album genres from MusicBrainz and Discogs",
        )
        cmd.parser.add_option(
            "-n",
            "--dry-run",
            action="store_true",
            default=False,
            help="show proposed genre changes without writing them",
        )
        cmd.parser.add_option(
            "-l",
            "--limit",
            type="int",
            default=None,
            help="only process the first N matched albums",
        )
        cmd.func = self._run_command
        return [cmd]

    def _run_command(self, lib, opts, args):
        albums = list(lib.albums(decargs(args)))
        if opts.limit is not None:
            albums = albums[: opts.limit]
        for album in albums:
            self.sync_album(album, dry_run=opts.dry_run)

    def sync_album(self, album, dry_run=False):
        mb_genres, mb_ok = self._musicbrainz_genres(album)
        discogs_broad, discogs_specific, discogs_ok = self._discogs_genres(album)

        merged = self._dedupe([*discogs_broad, *mb_genres, *discogs_specific])
        if not merged:
            if mb_ok and discogs_ok:
                self._log.info("{0}: no genre data found", album)
            else:
                self._log.warning("{0}: skipping, genre lookup failed", album)
            return

        old_value = list(album.genres or [])
        if set(merged) == set(old_value):
            self._log.debug("{0}: genres unchanged", album)
            return

        if dry_run:
            print_(f"{album}: {'; '.join(old_value)!r} -> {'; '.join(merged)!r}")
            return

        self._log.info("{0}: {1!r} -> {2!r}", album, old_value, merged)
        update_mtime = self.config["update_mtime"].get(bool)
        album.genres = merged
        album.store()
        for item in album.items():
            stat = None if update_mtime else os.stat(item.path)
            item.genres = merged
            item.store()
            item.try_write()
            if stat is not None:
                os.utime(item.path, (stat.st_atime, stat.st_mtime))

    def _musicbrainz_genres(self, album) -> tuple[list[str], bool]:
        votes: dict[str, int] = {}
        ok = True
        for entity, mbid in (
            ("release", album.mb_albumid),
            ("release-group", album.mb_releasegroupid),
        ):
            if not mbid:
                continue
            if not self._is_valid_mbid(mbid):
                self._log.warning(
                    "MusicBrainz {0} id {1!r} is not a valid MBID, skipping",
                    entity,
                    mbid,
                )
                continue
            genres, entry_ok = self._mb_genre_names(entity, mbid)
            ok = ok and entry_ok
            self._tally_votes(votes, genres)

        ranked = sorted(votes, key=votes.__getitem__, reverse=True)
        return ranked[:MUSICBRAINZ_GENRE_LIMIT], ok

    @staticmethod
    def _is_valid_mbid(value: str) -> bool:
        return bool(MUSICBRAINZ_MBID_RE.fullmatch(value))

    def _tally_votes(
        self, votes: dict[str, int], genres: list[tuple[str, int]]
    ) -> None:
        for name, count in genres:
            title = self._title_case_genre(name)
            votes[title] = max(votes.get(title, 0), count)

    @staticmethod
    def _title_case_genre(name: str) -> str:
        return " ".join(
            word.upper() if word.lower() in MUSICBRAINZ_ACRONYMS else word.title()
            for word in name.strip().split(" ")
        )

    def _mb_genre_names(
        self, entity: str, mbid: str
    ) -> tuple[list[tuple[str, int]], bool]:
        url = MUSICBRAINZ_URL.format(entity=entity, mbid=mbid)

        for attempt in range(MUSICBRAINZ_MAX_RETRIES + 1):
            wait = MUSICBRAINZ_MIN_INTERVAL - (time.monotonic() - self._last_mb_request)
            if wait > 0:
                time.sleep(wait)

            try:
                response = requests.get(
                    url,
                    params={"inc": "genres", "fmt": "json"},
                    headers={"User-Agent": USER_AGENT},
                    timeout=10,
                )
            except requests.RequestException as exc:
                self._last_mb_request = time.monotonic()
                # Connection-level failures (timeouts, resets) are just as
                # transient as a 503 response, so retry them the same way.
                if attempt < MUSICBRAINZ_MAX_RETRIES:
                    time.sleep(2**attempt)
                    continue
                self._log.warning(
                    "MusicBrainz {0} {1} lookup failed: {2}", entity, mbid, exc
                )
                return [], False

            self._last_mb_request = time.monotonic()

            # MusicBrainz's overloaded backend signals "back off" via 503
            # rather than a rate-limit-specific status; retry a bounded
            # number of times before giving up on this album.
            if response.status_code == 503 and attempt < MUSICBRAINZ_MAX_RETRIES:
                time.sleep(float(response.headers.get("Retry-After", 2**attempt)))
                continue

            try:
                response.raise_for_status()
            except requests.RequestException as exc:
                self._log.warning(
                    "MusicBrainz {0} {1} lookup failed: {2}", entity, mbid, exc
                )
                return [], False

            genre_list = response.json().get("genres", [])
            return [
                (g["name"], g.get("count", 0)) for g in genre_list if g.get("name")
            ], True

        return [], False

    def _discogs_genres(self, album) -> tuple[list[str], list[str], bool]:
        release_id = album.discogs_albumid
        if not release_id:
            return [], [], True

        params = {}
        token = self.config["discogs_token"].get()
        if token:
            params["token"] = token

        try:
            response = requests.get(
                DISCOGS_RELEASE_URL.format(release_id=release_id),
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=10,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            self._log.warning("Discogs release {0} lookup failed: {1}", release_id, exc)
            return [], [], False

        data = response.json()
        return list(data.get("genres") or []), list(data.get("styles") or []), True

    @staticmethod
    def _dedupe(values: Iterable[str]) -> list[str]:
        seen: dict[str, str] = {}
        for value in values:
            value = value.strip()
            key = value.lower()
            if value and key not in seen:
                seen[key] = value
        return list(seen.values())
