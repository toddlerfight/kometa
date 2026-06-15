# Kometa — Fresh Install

Kometa is a self-hosted comic collection manager: browse your library, track a
pull list, and fetch what you're missing — individual issues *and* collected
editions (trade paperbacks / hardcovers).

## Requirements

- Docker (Desktop on macOS/Windows, or docker + the compose plugin on Linux)

That's it. Everything else builds inside the container.

## Install & run

```bash
./run.sh
```

Then open **http://localhost:6970**. The first run builds the image (a couple of
minutes); after that it's instant.

## Add your first series

Click **Add Series**, start typing a title, pick the right match — done. Kometa
tracks the series, shows every issue, and can fetch the ones you're missing.
Downloads are filed into your library automatically.

Open a series and check the **Trades** tab to grab a collected edition instead —
handy when single issues are hard to find.

Nothing to configure to get going: search and downloads work out of the box, no
API keys. Connecting Komga, Metron, or SABnzbd in **Settings** is optional — it
adds metadata, in-app reading, and usenet — but none of it is required.

## Commands

```bash
./run.sh          # build + start (state persists between runs)
./run.sh --wipe   # delete all local state, next start is factory-fresh
./run.sh --down   # stop the container (state survives)
```

## Where things live

All state stays inside this folder:

- `local/data/` — database
- `local/comics/` — your comic library (drop in existing CBZ/CBR files if you have them)
- `local/downloads/` — staging area for downloads

## On Linux

- Talking to Docker needs root. Either run `sudo ./run.sh`, or (recommended, one
  time) add yourself to the docker group: `sudo usermod -aG docker $USER`, then
  log out and back in. After that it behaves exactly like macOS.
- Files under `local/` are created as **your** user (not root), so you can read,
  move, and delete your comics normally.

## Notes

- Listens on port 6970. Change the `ports` mapping in `docker-compose.local.yml`
  if that clashes with something.
- There is no authentication. Do not expose this port to the internet.
