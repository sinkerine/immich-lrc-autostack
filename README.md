# immich-lrc-autostack

Mirror **Lightroom Classic stacks** into **[Immich](https://github.com/immich-app/immich) stacks**.

If Immich indexes the same photo files that Lightroom Classic manages (as an
external library), this tool reads your Lightroom catalog backups and stacks
those photos in Immich the way they are stacked in Lightroom. HDR and panorama
merges stay grouped with their source frames, as do bursts and any stacks you
made by hand.

- Reads the newest catalog backup (`*.zip` as written by Lightroom, or a plain
  `*.lrcat`) from one or more backup folders. Backups are opened read-only,
  from a temporary copy.
- Maps Lightroom paths (e.g. `P:/Photos/2025-04-Trip/IMG_0001.CR3`) to Immich
  asset paths automatically by matching file names and folders. Renamed parent
  folders are fine as long as the files themselves keep their names.
- Keeps the Lightroom order: the top image of the Lightroom stack becomes the
  Immich cover.
- Plays well with other stacking tools such as
  [immich-stack](https://github.com/Majorfi/immich-stack): if a stack member is
  already in an Immich stack (e.g. a RAW+JPG pair), that whole stack is merged
  in and **keeps its existing cover**, so an exported JPG can stay in front.
- Never unstacks, deletes or modifies files. Repeated runs are safe; stacks that
  already match are left alone.
- Single Python file, standard library only (Python 3.9+).

## Requirements

- Immich v3.2 or newer (uses the `filter` / `cursor` search API).
- The photos are in Immich as an
  [external library](https://immich.app/docs/features/libraries), so
  Immich assets point at the same files Lightroom uses.
- Lightroom catalog backups reachable from where this runs. Lightroom writes a
  backup on exit when *Catalog Settings → Back up catalog* is enabled; point the
  backup folder at a share that this host can read.
- An Immich API key with `asset.read`, `stack.read` and `stack.create`.
  Stacks are created for the key owner's assets only.

## Usage

### Docker Compose (recommended)

Add a service to the Compose project that runs Immich:

```yaml
services:
  immich-lrc-autostack:
    build: https://github.com/sinkerine/immich-lrc-autostack.git
    env_file: .lrc-autostack.env        # IMMICH_API_KEY=...
    environment:
      IMMICH_API_URL: http://immich-server:2283/api
      LRC_BACKUPS_DIRS: /backups/catalog
      IMMICH_LIBRARY_PATH: /mnt/photos  # where immich-server sees the photos
      INTERVAL: "3600"                  # run hourly
      DRY_RUN: "true"                   # set to "false" once the logs look right
    volumes:
      - /srv/lightroom-backups:/backups/catalog:ro
    restart: unless-stopped
```

```sh
cp example.env .lrc-autostack.env && chmod 600 .lrc-autostack.env   # then add your key
docker compose up -d --build immich-lrc-autostack
docker compose logs -f immich-lrc-autostack
```

A dry run logs how each Lightroom folder was mapped and every stack it would
create:

```
starting (dry_run=True)
12034 assets under /mnt/photos/, 310 existing stacks
/backups/catalog: using /backups/catalog/2025-06-01 1830/Lightroom Catalog.zip
  folder 'P:/Photos/2025-04-Trip/' -> '/mnt/photos/2025-04-trip/' (500/500 sampled files matched)
  stack IMG_0412-HDR.dng (4 in Lightroom) -> 5 assets
  stack IMG_0530.CR3 (12 in Lightroom) -> 12 assets
done: 86 stacks would be created/merged, 3 unchanged or not in Immich
```

When it looks right, set `DRY_RUN: "false"` and `docker compose up -d`.

### Docker

```sh
docker build -t immich-lrc-autostack https://github.com/sinkerine/immich-lrc-autostack.git

docker run --rm \
  --network immich_default \
  -e IMMICH_API_KEY \
  -v /srv/lightroom-backups:/backups:ro \
  immich-lrc-autostack \
    --api-url http://immich-server:2283/api \
    --backups-dir /backups \
    --library-path /mnt/photos \
    --dry-run
```

### Plain Python

```sh
export IMMICH_API_KEY=...   # or --api-key-file /path/to/key
python3 immich_lrc_autostack.py \
  --api-url https://immich.example.com/api \
  --backups-dir /srv/lightroom-backups/main \
  --backups-dir /srv/lightroom-backups/travel-laptop \
  --library-path /mnt/photos \
  --apply
```

## Options

Every option can also come from an environment variable, which is handy in
Compose. Command-line flags win.

| Flag | Environment | Default | Description |
|---|---|---|---|
| `--api-url` | `IMMICH_API_URL` | `http://immich-server:2283/api` | Immich API base URL. |
| — | `IMMICH_API_KEY` | — | API key. Keep it in an env file, not in Compose YAML. |
| `--api-key-file` | `IMMICH_API_KEY_FILE` | — | Read the key from a file (e.g. a Docker secret). |
| `--backups-dir` (repeatable) | `LRC_BACKUPS_DIRS` (comma-separated) | required | Folder with Lightroom backups. Searched recursively; the newest `*.zip` / `*.lrcat` is used. One per catalog. |
| `--library-path` | `IMMICH_LIBRARY_PATH` | `/` | The photo folder **as mounted inside immich-server** (asset `originalPath` prefix). Limits which assets are considered. |
| `--path-map` (repeatable) | `PATH_MAP` (semicolon-separated) | auto | Explicit `LRC_PREFIX=IMMICH_PREFIX`, e.g. `P:/Photos/=/mnt/photos/`. Use when auto-detection skips a folder. |
| `--dry-run` / `--apply` | `DRY_RUN` | dry run | Only log, or actually create stacks. |
| `--interval` | `INTERVAL` | `0` | Seconds between runs. `0` runs once and exits. |

## How path matching works

Lightroom stores each photo as *root folder* + *relative path*, e.g.
`P:/Photos/2025-04-Trip/` + `IMG_0001.CR3`. For every root folder, the tool
samples up to 500 of its files, looks for Immich assets whose `originalPath`
ends with the same relative path, and takes the most common prefix. A folder is
used only if at least 20% of sampled files match; otherwise it is skipped and
logged. Matching is case-insensitive. Use `--path-map` to force a mapping.

## Notes and limits

- Only **folder stacks** are mirrored. Collection-only stacks and virtual
  copies are ignored (a virtual copy is the same file as its master).
- Unstacking in Lightroom does not unstack in Immich. Unstack there by hand.
- Photos that Immich has not indexed yet are skipped and picked up on a later
  run, after the library scan.
- If another tool rewrites stacks (e.g. immich-stack with `REPLACE_STACKS=true`)
  the two may fight. With immich-stack's default `REPLACE_STACKS=false`, merged
  stacks are left alone.

## License

[MIT](LICENSE)
