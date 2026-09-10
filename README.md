# beets-genresync

A [beets](https://beets.io) plugin that populates album genres from two sources:

- **MusicBrainz** genre votes (release-group level, falling back to the
  specific release only if the release-group has none)
- **Discogs** `genre` (broad) and `style` (specific) fields, via the release's stored `discogs_albumid`

Results from both sources are merged and deduplicated (case-insensitively) into
the album's `genres` field, then written to both the library and the file tags.

## Requirements

Albums are matched by ID, not re-searched: MusicBrainz lookups use the
existing `mb_albumid`/`mb_releasegroupid`, and Discogs lookups use the
existing `discogs_albumid`. An album with neither ID produces no genres.

## Configuration

```yaml
genresync:
    auto: yes          # sync genres automatically after `beet import`
    discogs_token: ""  # optional; unauthenticated requests are rate-limited more heavily
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
