#!/usr/bin/env python3
"""Download videos (and images) from Threads posts in the highest quality served.

The <video> tag on a Threads page plays a downscaled progressive MP4, often
360p, while the page also embeds a DASH manifest with much better renditions -
usually up to the original upload resolution. This tool reads that manifest and
takes the top rendition instead of whatever the player picked.

Threads ships VP9, which QuickTime and Safari cannot open, so output is
re-encoded to H.264 at CRF 15 by default (visually lossless; use --copy to keep
the original VP9 bitstream for VLC/IINA).

Pure standard library. ffmpeg is optional but recommended: it muxes split audio
and handles the H.264 conversion.

Usage:
    threads-dl https://www.threads.com/share/FFTcRYbBm
    threads-dl URL1 URL2 URL3 -o ~/Videos          # several posts in one run
    threads-dl -f links.txt -o ~/Videos            # one link per line
    pbpaste | threads-dl -f -                      # links from the clipboard
    threads-dl URL --list                 # show renditions, download nothing
    threads-dl URL --max-height 1080      # cap resolution
    threads-dl URL --copy                 # keep original VP9 (VLC/IINA, faster)
    threads-dl URL --images               # also grab images in a carousel
    threads-dl URL --cookies-from-browser chrome   # private/login-only posts
    threads-dl URL --force                # re-download even if already saved
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

__version__ = "1.0.0"

DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

CHUNK = 256 * 1024
TIMEOUT = 45


# --------------------------------------------------------------------------- #
# networking
# --------------------------------------------------------------------------- #
def _request(url: str, ua: str, extra: dict | None = None, method: str = "GET",
             page: bool = True):
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.threads.com/",
    }
    if page:
        # Without these Threads serves a JS-only shell with no post data in it.
        headers.update({
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        })
    else:
        headers["Accept"] = "*/*"
        headers["Sec-Fetch-Mode"] = "no-cors"
        headers["Sec-Fetch-Dest"] = "video"
    if extra:
        headers.update(extra)
    return urllib.request.Request(url, headers=headers, method=method)


def fetch(url: str, ua: str = DESKTOP_UA, extra: dict | None = None,
          retries: int = 3) -> tuple[str, bytes]:
    """GET url, follow redirects, return (final_url, body)."""
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(_request(url, ua, extra), timeout=TIMEOUT) as r:
                return r.geturl(), r.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 500, 502, 503) and attempt + 1 < retries:
                last = e
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"request failed: {url} ({last})")


def resolve_post_url(url: str, cookie_header: str | None) -> str:
    """Turn a /share/XXXX link into the canonical /@user/post/CODE URL."""
    extra = {"Cookie": cookie_header} if cookie_header else None
    if "/share/" not in url:
        return url
    # A mobile UA makes Threads answer with a 302 to the canonical post.
    final_url, _ = fetch(url, ua=MOBILE_UA, extra=extra)
    if "/post/" not in final_url:
        # Some share links need the desktop flow; fall back to it.
        final_url, _ = fetch(url, ua=DESKTOP_UA, extra=extra)
    return final_url.split("?")[0]


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def walk(node):
    """Yield every dict nested in a JSON value."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def load_ssr_blocks(html_text: str) -> list:
    blocks = re.findall(
        r'<script type="application/json"[^>]*>(.*?)</script>', html_text, re.S
    )
    parsed = []
    for raw in blocks:
        try:
            parsed.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return parsed


class PostError(Exception):
    """A post could not be read (private, deleted, or login wall)."""


def find_media(blocks: list, code: str | None) -> dict:
    """Locate the post object and merge duplicate fragments by item pk."""
    matches = []
    for block in blocks:
        for node in walk(block):
            has_media = node.get("carousel_media") or node.get("video_versions") \
                or node.get("image_versions2")
            if not has_media:
                continue
            if code and node.get("code") != code:
                continue
            matches.append(node)

    if not matches:
        raise PostError(
            "no media data in the page (private, deleted, or login wall) - "
            "retry with --cookies-from-browser <browser>"
        )

    # The page repeats the post in several payloads, sometimes truncated.
    # Merge all of them, keyed by media pk, keeping the richest entry.
    items: dict[str, dict] = {}
    for node in matches:
        for item in (node.get("carousel_media") or [node]):
            key = str(item.get("pk") or item.get("id") or id(item))
            if key not in items or _richness(item) > _richness(items[key]):
                items[key] = item
    merged = dict(matches[0])
    merged["carousel_media"] = list(items.values())
    return merged


def _richness(item: dict) -> int:
    return (1000 if item.get("video_dash_manifest") else 0) + \
           len(item.get("video_versions") or []) + \
           len(item.get("image_versions2", {}).get("candidates") or [])


def parse_mpd(mpd_text: str) -> dict[str, list[dict]]:
    """Return {'video': [rendition...], 'audio': [rendition...]} from an MPD."""
    out: dict[str, list[dict]] = {"video": [], "audio": []}
    try:
        root = ET.fromstring(mpd_text)
    except ET.ParseError:
        return out

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    for aset in root.iter():
        if local(aset.tag) != "AdaptationSet":
            continue
        content = aset.get("contentType")
        for rep in aset:
            if local(rep.tag) != "Representation":
                continue
            base = None
            for child in rep:
                if local(child.tag) == "BaseURL" and child.text:
                    base = html_mod.unescape(child.text.strip())
                    break
            if not base:
                continue
            kind = content
            if kind is None:
                mime = rep.get("mimeType", "")
                kind = "audio" if mime.startswith("audio") else "video"
            if kind not in out:
                continue
            out[kind].append({
                "url": base,
                "width": int(rep.get("width") or 0),
                "height": int(rep.get("height") or 0),
                "bandwidth": int(rep.get("bandwidth") or 0),
                "codec": rep.get("codecs") or "",
                "size": int(rep.get("FBContentLength") or 0),
            })
    return out


def best_rendition(rends: list[dict], max_height: int | None, by_bandwidth=False) -> dict | None:
    if not rends:
        return None
    pool = [r for r in rends if not max_height or not r["height"] or r["height"] <= max_height]
    if not pool:
        pool = sorted(rends, key=lambda r: r["height"] or 0)[:1]
    if by_bandwidth:
        return max(pool, key=lambda r: (r["bandwidth"], r["width"] * r["height"]))
    return max(pool, key=lambda r: (r["width"] * r["height"], r["bandwidth"]))


def progressive_url(item: dict) -> str | None:
    """The plain MP4 the web player uses - fallback when there is no manifest."""
    versions = item.get("video_versions") or []
    seen, uniq = set(), []
    for v in versions:  # types 101/102/103 usually point at the same file
        u = v.get("url")
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq[0] if uniq else None


def best_image(item: dict) -> dict | None:
    cands = (item.get("image_versions2") or {}).get("candidates") or []
    if not cands:
        return None
    return max(cands, key=lambda c: (c.get("width", 0) * c.get("height", 0)))


# --------------------------------------------------------------------------- #
# downloading
# --------------------------------------------------------------------------- #
def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def download(url: str, dest: str, label: str = "", cookie_header: str | None = None) -> str:
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    extra = {"Referer": "https://www.threads.com/"}
    if cookie_header:
        extra["Cookie"] = cookie_header
    if have:
        extra["Range"] = f"bytes={have}-"

    req = _request(url, DESKTOP_UA, extra, page=False)
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code == 416 and have:  # already complete
            os.replace(part, dest)
            return dest
        raise

    mode = "ab" if have and resp.status == 206 else "wb"
    if mode == "wb":
        have = 0
    total = resp.headers.get("Content-Length")
    total = int(total) + have if total else 0
    done = have
    started = time.time()

    with open(part, mode) as fh:
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 / total
                speed = done / max(time.time() - started, 0.001)
                sys.stderr.write(
                    f"\r    {label} {pct:5.1f}%  {human(done)}/{human(total)}  "
                    f"{human(speed)}/s   "
                )
            else:
                sys.stderr.write(f"\r    {label} {human(done)}   ")
            sys.stderr.flush()
    sys.stderr.write("\n")
    os.replace(part, dest)
    return dest


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode(errors="replace")[-800:] + "\n")
        raise SystemExit(f"command failed: {' '.join(cmd[:3])} ...")


def mux(video: str, audio: str | None, out: str) -> str:
    if not shutil.which("ffmpeg"):
        sys.stderr.write("  ! ffmpeg not found - keeping video and audio separate\n")
        return video
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", video]
    if audio:
        cmd += ["-i", audio]
    cmd += ["-c", "copy", "-movflags", "+faststart", out]
    run(cmd)
    for path in (video, audio):
        if path and path != out and os.path.exists(path):
            os.remove(path)
    return out


def transcode_h264(src: str, out: str, crf: int = 15, preset: str = "slow",
                   delete_src: bool = True) -> str:
    if not shutil.which("ffmpeg"):
        sys.stderr.write(
            "  ! ffmpeg not found - file stays VP9, which QuickTime cannot open.\n"
            "    Install ffmpeg (brew install ffmpeg) or play it in VLC/IINA.\n"
        )
        return src
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
         "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
         "-pix_fmt", "yuv420p", "-c:a", "copy",
         "-movflags", "+faststart", out])
    if delete_src and os.path.exists(src) and os.path.abspath(src) != os.path.abspath(out):
        os.remove(src)
    return out


# --------------------------------------------------------------------------- #
# cookies
# --------------------------------------------------------------------------- #
def cookie_header_from_file(path: str) -> str:
    jar = {}
    with open(path, "r", errors="ignore") as fh:
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 7:
                jar[parts[5]] = parts[6]
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def cookie_header_from_browser(browser: str) -> str:
    try:
        import browser_cookie3  # type: ignore
    except ImportError:
        raise SystemExit(
            "browser cookie import needs one dependency:\n"
            "    python3 -m pip install --user browser_cookie3\n"
            "Then re-run, or export cookies to a file and use --cookies."
        )
    fn = getattr(browser_cookie3, browser, None)
    if fn is None:
        raise SystemExit(f"unsupported browser: {browser}")
    jar = fn(domain_name="threads.com")
    try:
        jar += fn(domain_name="instagram.com")
    except Exception:
        pass
    return "; ".join(f"{c.name}={c.value}" for c in jar)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")[:60] or "post"


def collect_urls(args) -> list[str]:
    """Positional URLs + --file lists + stdin, deduped by shortcode."""
    raw: list[str] = list(args.urls)
    for path in args.file or []:
        if path == "-":
            raw.extend(sys.stdin.read().splitlines())
            continue
        with open(path, "r", errors="ignore") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    raw.append(line)

    seen, urls = set(), []
    for url in raw:
        url = url.strip().strip("'\"")
        if not url:
            continue
        if not url.startswith("http"):
            url = "https://" + url if "threads." in url else url
        code = re.search(r"/(?:post|share)/([A-Za-z0-9_-]+)", url)
        key = code.group(1) if code else url
        if key in seen:
            continue
        seen.add(key)
        urls.append(url)
    return urls


def process_post(url: str, args, cookie_header: str | None) -> tuple[int, int]:
    """Download one post. Returns (saved, skipped)."""
    print(f"-> {url}")
    page_url = resolve_post_url(url, cookie_header)
    code_match = re.search(r"/post/([A-Za-z0-9_-]+)", page_url)
    shortcode = code_match.group(1) if code_match else None
    user_match = re.search(r"/@([^/]+)/post/", page_url)
    username = safe(user_match.group(1)) if user_match else "threads"
    print(f"  resolved: {page_url}")

    extra = {"Cookie": cookie_header} if cookie_header else None
    # Reload with a desktop UA: the mobile redirect can land on an app banner
    # page instead of one carrying the SSR post data.
    _, body = fetch(page_url, ua=DESKTOP_UA, extra=extra)
    blocks = load_ssr_blocks(body.decode("utf-8", "replace"))
    media = find_media(blocks, shortcode)
    items = media.get("carousel_media") or [media]

    n_video = sum(1 for it in items
                  if it.get("video_versions") or it.get("video_dash_manifest"))
    n_image = sum(1 for it in items
                  if not (it.get("video_versions") or it.get("video_dash_manifest"))
                  and it.get("image_versions2"))
    print(f"  post: {media.get('code') or shortcode} - {len(items)} item(s): "
          f"{n_video} video, {n_image} image")

    saved = skipped = 0

    for idx, item in enumerate(items, 1):
        is_video = bool(item.get("video_versions") or item.get("video_dash_manifest"))
        if not is_video:
            if not args.images:
                continue
            img = best_image(item)
            if not img:
                continue
            name = f"{username}_{shortcode}_{idx:02d}.jpg"
            dest = os.path.join(args.output_dir, name)
            if os.path.exists(dest) and not args.force:
                print(f"  [{idx}] image exists, skipping")
                skipped += 1
                continue
            print(f"  [{idx}] image {img['width']}x{img['height']}")
            download(img["url"], dest, label=name, cookie_header=cookie_header)
            print(f"    saved {dest}")
            saved += 1
            continue

        plan = {}
        if not args.progressive and item.get("video_dash_manifest"):
            plan = parse_mpd(item["video_dash_manifest"])
        video = best_rendition(plan.get("video", []), args.max_height) if plan else None
        audio = best_rendition(plan.get("audio", []), None, by_bandwidth=True) if plan else None

        if video:
            label = f"DASH {video['width']}x{video['height']} @ {video['bandwidth'] // 1000} kb/s"
            src_url = video["url"]
        else:
            src_url = progressive_url(item)
            if not src_url:
                continue
            label = f"progressive {item.get('original_width')}x{item.get('original_height')}"

        if args.list:
            print(f"  [{idx}] video <- {label}")
            for r in (plan.get("video") or []):
                print(f"        {r['width']:>5}x{r['height']:<5} "
                      f"{r['bandwidth'] // 1000:>5} kb/s  {r['codec']}")
            if not video:
                print(f"        (no DASH manifest) {src_url[:80]}...")
            continue

        dims = f"{video['width']}x{video['height']}" if video else "prog"
        base = f"{username}_{shortcode}_{idx:02d}_{dims}"
        final = os.path.join(args.output_dir, base + ".mp4")
        if os.path.exists(final) and not args.force:
            print(f"  [{idx}] exists, skipping: {base}.mp4")
            skipped += 1
            continue

        print(f"  [{idx}] video <- {label}")
        v_tmp = os.path.join(args.output_dir, base + ".video.mp4")
        download(src_url, v_tmp, label=base, cookie_header=cookie_header)

        a_tmp = None
        if audio:
            a_tmp = os.path.join(args.output_dir, base + ".audio.mp4")
            download(audio["url"], a_tmp, label=base + " audio",
                     cookie_header=cookie_header)

        if audio:
            mux(v_tmp, a_tmp, final)
        else:
            os.replace(v_tmp, final)

        # Threads DASH renditions are VP9-in-MP4, which QuickTime and Safari
        # refuse to open. Re-encode to H.264 unless the user asked for a
        # straight copy (VLC/IINA users, or to keep the original bitstream).
        source_codec = (video or {}).get("codec", "")
        needs_h264 = not args.copy and source_codec.startswith("vp09")
        if needs_h264 or args.h264:
            if needs_h264:
                print("    encoding H.264 (QuickTime-compatible)...")
            produced = transcode_h264(final, final + ".tmp.mp4",
                                      crf=args.crf, preset=args.preset)
            if produced != final and os.path.exists(produced):
                os.replace(produced, final)

        size = os.path.getsize(final)
        print(f"    saved {final} ({human(size)})")
        saved += 1

    print()
    return saved, skipped


def _setup_console() -> None:
    """Keep non-ASCII output and Windows code pages from raising on write."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    ap = argparse.ArgumentParser(
        description="Download Threads videos in the best available quality. "
                    "Accepts any number of post links.")
    ap.add_argument("urls", nargs="*", help="post or /share/ links")
    ap.add_argument("-f", "--file", action="append", metavar="FILE",
                    help="read links from a file, one per line ('-' for stdin)")
    ap.add_argument("-o", "--output-dir", default=".", help="where to save (default: .)")
    ap.add_argument("--max-height", type=int, default=None,
                    help="cap vertical resolution, e.g. 1080")
    ap.add_argument("--list", action="store_true", help="only list renditions")
    ap.add_argument("--images", action="store_true", help="also download carousel images")
    ap.add_argument("--force", action="store_true",
                    help="re-download even if the file already exists")
    ap.add_argument("--h264", action="store_true",
                    help="force H.264 re-encode (default when the source is VP9)")
    ap.add_argument("--copy", action="store_true",
                    help="keep the original VP9 bitstream (VLC/IINA only, faster)")
    ap.add_argument("--crf", type=int, default=15,
                    help="H.264 quality, lower is better (default: 15; 0 = lossless, huge)")
    ap.add_argument("--preset", default="slow",
                    help="x264 speed/efficiency preset (default: slow; use veryslow for max)")
    ap.add_argument("--progressive", action="store_true",
                    help="skip DASH and take the player's MP4 (usually lower res)")
    ap.add_argument("--cookies", metavar="FILE", help="Netscape cookies.txt")
    ap.add_argument("--cookie", metavar="STRING", help='raw "k=v; k2=v2" header')
    ap.add_argument("--cookies-from-browser", metavar="BROWSER",
                    help="chrome|firefox|safari|edge|brave|arc (needs browser_cookie3)")
    ap.add_argument("-V", "--version", action="version",
                    version=f"threads-dl {__version__}")
    args = ap.parse_args(argv)

    try:
        urls = collect_urls(args)
    except OSError as exc:
        ap.error(f"cannot read link file: {exc}")
    if not urls:
        ap.error("no links given (pass URLs, --file links.txt, or pipe them on stdin)")

    cookie_header = args.cookie
    if args.cookies:
        cookie_header = cookie_header_from_file(args.cookies)
    if args.cookies_from_browser:
        cookie_header = cookie_header_from_browser(args.cookies_from_browser)

    os.makedirs(args.output_dir, exist_ok=True)
    print(f"{len(urls)} post(s) queued\n")

    saved = skipped = 0
    failures: list[tuple[str, str]] = []
    seen_codes: set[str] = set()
    for i, url in enumerate(urls, 1):
        if len(urls) > 1:
            print(f"[{i}/{len(urls)}]")
        try:
            # Resolve first so a /share/ link and the post URL it points to
            # are recognised as the same post.
            page_url = resolve_post_url(url, cookie_header)
            code_match = re.search(r"/post/([A-Za-z0-9_-]+)", page_url)
            if code_match:
                code = code_match.group(1)
                if code in seen_codes:
                    print(f"-> {url}\n  duplicate of {code}, skipping\n")
                    skipped += 1
                    continue
                seen_codes.add(code)

            got, skip = process_post(page_url, args, cookie_header)
            saved += got
            skipped += skip
        except (PostError, urllib.error.HTTPError, urllib.error.URLError,
                TimeoutError, OSError) as exc:
            reason = getattr(exc, "code", None) or str(exc) or type(exc).__name__
            print(f"  ! failed: {reason}\n")
            failures.append((url, str(reason)))
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # keep the batch alive on anything unexpected
            print(f"  ! failed: {type(exc).__name__}: {exc}\n")
            failures.append((url, f"{type(exc).__name__}: {exc}"))

    print(f"Done - {saved} file(s) saved, {skipped} skipped, "
          f"{len(failures)}/{len(urls)} post(s) failed")
    if failures:
        for url, reason in failures:
            print(f"  x {url}\n    {reason}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        raise SystemExit(130)
