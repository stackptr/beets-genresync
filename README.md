# beets-genresync

A [beets](https://beets.io) plugin that populates album genres from two sources:

- **MusicBrainz** genre votes (release and release-group level)
- **Discogs** `genre` (broad) and `style` (specific) fields, via the release's stored `discogs_albumid`

Results from both sources are merged and deduplicated (case-insensitively) into
the album's `genres` field, then written to both the library and the file tags.
File modification time is preserved by default (see `update_mtime` below), so
genre-only syncs don't trip "recently added" sorting in media servers that key
off `mtime`.

## Requirements

Albums are matched by ID, not re-searched: MusicBrainz lookups use the
existing `mb_albumid`/`mb_releasegroupid`, and Discogs lookups use the
existing `discogs_albumid`. An album with neither ID produces no genres.

## Configuration

```yaml
genresync:
    auto: yes          # sync genres automatically after `beet import`
    discogs_token: ""  # optional; unauthenticated requests are rate-limited more heavily
    update_mtime: no   # if yes, let genre-only writes bump the file's mtime as usual
```

## Usage

Runs automatically after every `beet import` when `auto` is enabled.

To sync (or dry-run) existing albums:

```sh
# Show proposed changes for the first 20 matched albums, without writing them
beet genresync --dry-run --limit 20

# Sync every album in the library
beet genresync

# Sync a specific query
beet genresync albumartist:Boards of Canada
```

## Testing

```sh
pip install .[test]
pytest
```

Tests use `beets.test.helper.PluginTestHelper` for isolated library/config
fixtures and `responses` to mock MusicBrainz/Discogs HTTP calls -- no network
access or real library required. CI (`.github/workflows/test.yml`) runs the
suite on every push and pull request.
