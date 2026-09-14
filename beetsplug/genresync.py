from __future__ import annotations

import os
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
        if merged == old_value:
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
        names: list[str] = []
        ok = True
        if album.mb_albumid:
            release_names, release_ok = self._mb_genre_names(
                "release", album.mb_albumid
            )
            names += release_names
            ok = ok and release_ok
        if album.mb_releasegroupid:
            rg_names, rg_ok = self._mb_genre_names(
                "release-group", album.mb_releasegroupid
            )
            names += rg_names
            ok = ok and rg_ok
        return self._dedupe(name.title() for name in names), ok

    def _mb_genre_names(self, entity: str, mbid: str) -> tuple[list[str], bool]:
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
            return [g["name"] for g in genre_list if g.get("name")], True

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
