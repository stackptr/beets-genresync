from __future__ import annotations

from typing import Iterable

import musicbrainzngs
import requests
from beets.plugins import BeetsPlugin
from beets.ui import Subcommand, decargs, print_

DISCOGS_RELEASE_URL = "https://api.discogs.com/releases/{release_id}"
USER_AGENT = "beets-genresync/0.1 +https://github.com/stackptr/beets-genresync"


class GenreSyncPlugin(BeetsPlugin):
    def __init__(self):
        super().__init__()
        self.config.add(
            {
                "auto": True,
                "discogs_token": None,
                "separator": "; ",
            }
        )
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
        mb_genres = self._musicbrainz_genres(album)
        discogs_broad, discogs_specific = self._discogs_genres(album)

        merged = self._dedupe([*discogs_broad, *mb_genres, *discogs_specific])
        if not merged:
            self._log.info("{0}: no genre data found", album)
            return

        new_value = self.config["separator"].get(str).join(merged)
        old_value = album.genres or ""
        if new_value == old_value:
            self._log.debug("{0}: genres unchanged", album)
            return

        if dry_run:
            print_(f"{album}: {old_value!r} -> {new_value!r}")
            return

        self._log.info("{0}: {1!r} -> {2!r}", album, old_value, new_value)
        album.genres = new_value
        album.store()
        for item in album.items():
            item.genres = new_value
            item.store()
            item.try_write()

    def _musicbrainz_genres(self, album) -> list[str]:
        names: list[str] = []
        if album.mb_albumid:
            names += self._mb_genre_names("release", album.mb_albumid)
        if album.mb_releasegroupid:
            names += self._mb_genre_names("release-group", album.mb_releasegroupid)
        return self._dedupe(name.title() for name in names)

    def _mb_genre_names(self, entity: str, mbid: str) -> list[str]:
        try:
            if entity == "release":
                result = musicbrainzngs.get_release_by_id(mbid, includes=["genres"])
                genre_list = result.get("release", {}).get("genre-list", [])
            else:
                result = musicbrainzngs.get_release_group_by_id(
                    mbid, includes=["genres"]
                )
                genre_list = result.get("release-group", {}).get("genre-list", [])
        except musicbrainzngs.WebServiceError as exc:
            self._log.warning("MusicBrainz {0} lookup failed: {1}", mbid, exc)
            return []
        return [g["name"] for g in genre_list if g.get("name")]

    def _discogs_genres(self, album) -> tuple[list[str], list[str]]:
        release_id = album.discogs_albumid
        if not release_id:
            return [], []

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
            return [], []

        data = response.json()
        return list(data.get("genres") or []), list(data.get("styles") or [])

    @staticmethod
    def _dedupe(values: Iterable[str]) -> list[str]:
        seen: dict[str, str] = {}
        for value in values:
            value = value.strip()
            key = value.lower()
            if value and key not in seen:
                seen[key] = value
        return list(seen.values())
