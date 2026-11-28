# threads-dl

A command-line downloader for **Threads** videos and images that grabs the **highest-quality rendition the platform actually serves** — not the low-res file the web player hands your browser.

[![ci](https://github.com/StarkDroid/threads-dl/actions/workflows/ci.yml/badge.svg)](https://github.com/StarkDroid/threads-dl/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)

```
$ threads-dl https://www.threads.com/share/FFTcRYbBm
-> https://www.threads.com/share/FFTcRYbBm
  resolved: https://www.threads.com/@artha1335h3/post/DUdIwZsgduZ
  post: DUdIwZsgduZ - 2 item(s): 2 video, 0 image
  [1] video - DASH 1200x1600 @ 2416 kb/s
    saved artha1335h3_DUdIwZsgduZ_01_1200x1600.mp4 (1.1 MB)
  [2] video - DASH 1080x1920 @ 6146 kb/s
    saved artha1335h3_DUdIwZsgduZ_02_1080x1920.mp4 (2.9 MB)
```

## Why this exists

The `<video>` element on a Threads post plays a **progressive MP4 that is often 360×480**, regardless of the upload quality. The same page also embeds a **DASH manifest** with renditions up to the original resolution. Most "Threads downloader" sites and generic tools stop at the first file and hand you the 360p version.

`threads-dl` reads the manifest, sorts the renditions, and downloads the best one — video and audio as separate tracks, muxed with `ffmpeg`.

It also handles the annoying parts:

- `/share/XXXX` links are resolved to real posts (they only redirect for mobile user agents)
- the post data is only server-rendered when the request carries `Sec-Fetch-*` headers, so a plain `curl` just gets an empty JS shell
- Threads serves VP9, which QuickTime and Safari cannot open, so output is re-encoded to H.264 by default

## Install

Requires **Python 3.9+** on Linux, macOS or Windows. `ffmpeg` is optional but strongly recommended (see [below](#ffmpeg)).

```bash
# recommended: isolated install, exposes the `threads-dl` command
pipx install git+https://github.com/StarkDroid/threads-dl

# or with uv
uv tool install git+https://github.com/StarkDroid/threads-dl

# or plain pip
python3 -m pip install --user git+https://github.com/StarkDroid/threads-dl
```

From a local clone:

```bash
git clone https://github.com/StarkDroid/threads-dl
cd threads-dl
python3 -m pip install --user .
```

No install at all — it is a single file with no third-party dependencies:

```bash
git clone https://github.com/StarkDroid/threads-dl
python3 threads-dl/threads_dl.py <url>
```

### ffmpeg

Used to mux split audio/video and to produce H.264. Without it, VP9 downloads still work but may not open in QuickTime/Safari, and videos with a separate audio track will be saved without sound.

| OS | Command |
|---|---|
| macOS | `brew install ffmpeg` |
| Debian/Ubuntu | `sudo apt install ffmpeg` |
| Fedora | `sudo dnf install ffmpeg` |
| Arch | `sudo pacman -S ffmpeg` |
| Windows | `winget install Gyan.FFmpeg` (or `choco install ffmpeg`) |

## Usage

```bash
# one post
threads-dl https://www.threads.com/@user/post/ABC123

# a /share/ link works too - it gets resolved for you
threads-dl https://www.threads.com/share/FFTcRYbBm

# several posts in one run
threads-dl URL1 URL2 URL3 -o ~/Videos

# a file of links, one per line, '#' starts a comment
threads-dl -f links.txt -o ~/Videos

# links from the clipboard (macOS shown; use xclip/xsel on Linux, Get-Clipboard on Windows)
pbpaste | threads-dl -f -

# inspect the renditions without downloading anything
threads-dl URL --list

# a carousel with several videos: grab only the second one
threads-dl URL --video 2

# or a few of them (numbers as shown by --list)
threads-dl URL --video 1,3-4
```

```
  [1] video - DASH 1200x1600 @ 2416 kb/s
         720x960     235 kb/s  vp09.00.31.08.00.06.06.06.00
         720x960    1595 kb/s  vp09.00.31.08.00.06.06.06.00
         1200x1600  2416 kb/s  vp09.00.40.08.00.06.06.06.00
```

### Options

| Flag | Description |
|---|---|
| `URLS...` | One or more post / `/share/` links |
| `-f, --file FILE` | Read links from a file, one per line (`-` for stdin) |
| `-o, --output-dir DIR` | Where to save files (default: current directory) |
| `--max-height N` | Cap vertical resolution, e.g. `1080` (default: no cap) |
| `--video N[,N...]` | Download only these videos, by the `[N]` number shown by `--list` (`2`, `1,3-4`). Applies to every post in a batch |
| `--list` | Print the available renditions and exit |
| `--images` | Also download images in a carousel |
| `--force` | Re-download even when the output file already exists |
| `--h264` | Force H.264 re-encode (already the default for VP9 sources) |
| `--copy` | Keep the original VP9 bitstream — much faster, VLC/IINA only |
| `--crf N` | H.264 quality, lower is better (default `15`, `0` = lossless) |
| `--preset NAME` | x264 preset (default `slow`, `veryslow` for maximum efficiency) |
| `--progressive` | Ignore DASH, take the player's MP4 (usually lower resolution) |
| `--cookies FILE` | Netscape `cookies.txt` for private/login-only posts |
| `--cookie STRING` | Raw `Cookie:` header value |
| `--cookies-from-browser NAME` | `chrome`, `firefox`, `edge`, `brave`, `arc`, `safari` |
| `-V, --version` | Print version |

Browser cookie import needs one optional dependency:

```bash
pipx inject threads-dl browser_cookie3
# or: python3 -m pip install --user "browser_cookie3"
```

## How it works

1. **Resolve** — `/share/` links are fetched with an iPhone user agent, which returns a `302` to the canonical `/@user/post/CODE` URL.
2. **Parse** — the canonical page is fetched with `Sec-Fetch-*` headers so Threads server-renders the post JSON. `threads-dl` extracts the media object from the embedded `<script type="application/json">` payloads and merges repeated, partially-rendered copies by media `pk`.
3. **Select** — for each carousel item it parses `video_dash_manifest`, picks the representation with the largest frame (then highest bitrate), and picks the highest-bitrate audio representation.
4. **Fetch** — downloads with HTTP range resume; interrupted runs continue from a `.part` file.
5. **Mux/encode** — `ffmpeg -c copy` merges tracks, then VP9 is re-encoded to H.264 at CRF 15 / preset slow unless `--copy` is given.

## Quality and file size

Threads only publishes a fixed ladder per post. `--list` shows it, and `threads-dl` always takes the top rung; there is nothing higher to request.

Because sizes are not the constraint here, the default encode is **visually lossless** (CRF 15, `-preset slow`, `yuv420p`, `+faststart`), which typically costs 2–5× the source bitrate. A 5-second 1200×1600 clip lands around 5–6 MB.

If you want the original bitstream exactly, byte for byte, use `--copy` — the file stays VP9 and needs VLC, IINA, mpv, or a browser to play.

## Output files

```
<username>_<shortcode>_<index>_<WxH>.mp4
artha1335h3_DUdIwZsgduZ_01_1200x1600.mp4
```

Carousel images become `<username>_<shortcode>_<index>.jpg`. Existing files are skipped, so re-running the same command only fetches what is missing.

Carousel items keep their post order in the `[N]` listing and in the filename index, so `--video 2` always means the second item and saves `..._02_...mp4`. Images in a carousel are unaffected by `--video`; use `--images` to include them.

Exit codes: `0` success, `1` at least one post failed, `2` bad invocation. Failures are reported per post at the end, and one bad link never aborts the rest of the batch.

## Limitations

- **Public posts only, by default.** Private, deleted, or age-gated posts need cookies (`--cookies-from-browser`). Even then, Threads may refuse.
- **Per post, not per profile.** Pointing it at a profile URL only captures whatever that page server-renders. Walking an entire timeline requires authenticated pagination, which is deliberately not implemented.
- **Signed URLs expire.** Links are fetched fresh on every run, so just re-run rather than saving old URLs.
- **Rate limits.** Posts are processed sequentially on purpose; hammering the endpoint gets you throttled.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| File will not open in QuickTime or Safari | It is VP9. Install `ffmpeg` and re-run (or drop `--copy`). |
| `! failed: 404` | Post deleted, private, or the URL is wrong. Try `--cookies-from-browser`. |
| `no media data in the page` | Login wall. Supply cookies. |
| No audio in the output | `ffmpeg` missing — the audio track is a separate stream and needs muxing. |
| Encoding is slow | Expected at CRF 15 / `slow`. Use `--preset medium` or `--copy`. |

## Development

```bash
python3 -m unittest discover -s tests -v
```

CI runs the suite plus a CLI smoke test on Ubuntu, Windows, and macOS (Python 3.9 and 3.12), and verifies the packaged `threads-dl` console script installs and runs.

## Legal

For personal and archival use. Respect copyright and the Terms of Service of the platforms involved; do not redistribute other people's content. This project is not affiliated with Meta or Threads.

## License

[MIT](LICENSE)
