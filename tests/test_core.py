"""Offline unit tests - no network, no ffmpeg required."""

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threads_dl as tdl  # noqa: E402

MPD = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static">
  <Period id="0" duration="PT2S">
    <AdaptationSet id="0" contentType="video" par="3:4">
      <Representation id="v1" bandwidth="800000" codecs="vp09.00.31.08" width="720" height="960">
        <BaseURL>https://cdn.example/v1.mp4?a=1&amp;b=2</BaseURL>
        <SegmentBase indexRange="818-861"/>
      </Representation>
      <Representation id="v2" bandwidth="500000" codecs="vp09.00.40.08" width="1200" height="1600">
        <BaseURL>https://cdn.example/v2.mp4</BaseURL>
      </Representation>
    </AdaptationSet>
    <AdaptationSet id="1" contentType="audio" lang="en">
      <Representation id="a1" bandwidth="64000" codecs="mp4a.40.2">
        <BaseURL>https://cdn.example/a1.mp4</BaseURL>
      </Representation>
      <Representation id="a2" bandwidth="128000" codecs="mp4a.40.2">
        <BaseURL>https://cdn.example/a2.mp4</BaseURL>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""


def ssr(*nodes):
    """Fake the page's <script type="application/json"> payloads."""
    return [{"require": [{"__bbox": {"result": {"data": n}}}]} for n in nodes]


class MpdTests(unittest.TestCase):
    def test_groups_by_kind_and_unescapes_url(self):
        plan = tdl.parse_mpd(MPD)
        self.assertEqual(len(plan["video"]), 2)
        self.assertEqual(len(plan["audio"]), 2)
        self.assertTrue(plan["video"][0]["url"].endswith("a=1&b=2"))

    def test_picks_biggest_frame_regardless_of_bitrate(self):
        best = tdl.best_rendition(tdl.parse_mpd(MPD)["video"], None)
        self.assertEqual((best["width"], best["height"]), (1200, 1600))

    def test_max_height_caps_resolution(self):
        best = tdl.best_rendition(tdl.parse_mpd(MPD)["video"], 1080)
        self.assertEqual((best["width"], best["height"]), (720, 960))

    def test_max_height_falls_back_to_smallest_when_nothing_fits(self):
        best = tdl.best_rendition(tdl.parse_mpd(MPD)["video"], 100)
        self.assertEqual(best["height"], 960)

    def test_audio_uses_highest_bitrate(self):
        best = tdl.best_rendition(tdl.parse_mpd(MPD)["audio"], None, by_bandwidth=True)
        self.assertEqual(best["bandwidth"], 128000)

    def test_manifest_without_audio(self):
        plan = tdl.parse_mpd(MPD.split("<AdaptationSet id=\"1\"")[0] + "</Period></MPD>")
        self.assertEqual(plan["audio"], [])
        self.assertEqual(len(plan["video"]), 2)

    def test_malformed_manifest_is_not_fatal(self):
        self.assertEqual(tdl.parse_mpd("not xml"), {"video": [], "audio": []})


class MediaTests(unittest.TestCase):
    def test_progressive_url_dedupes_identical_types(self):
        item = {"video_versions": [{"type": 101, "url": "u"}, {"type": 102, "url": "u"}]}
        self.assertEqual(tdl.progressive_url(item), "u")

    def test_progressive_url_missing(self):
        self.assertIsNone(tdl.progressive_url({"video_versions": None}))

    def test_find_media_merges_partial_payloads(self):
        blocks = ssr(
            {"code": "ABC", "carousel_media": [{"pk": "1"}, {"pk": "2"}]},
            {"code": "ABC", "carousel_media": [{"pk": "2", "video_dash_manifest": "x"}]},
        )
        media = tdl.find_media(blocks, "ABC")
        self.assertEqual(len(media["carousel_media"]), 2)
        richest = [i for i in media["carousel_media"] if i["pk"] == "2"][0]
        self.assertIn("video_dash_manifest", richest)

    def test_find_media_ignores_related_posts(self):
        blocks = ssr(
            {"code": "OTHER", "video_versions": [{"url": "related"}]},
            {"code": "ABC", "video_versions": [{"url": "wanted"}]},
        )
        media = tdl.find_media(blocks, "ABC")
        self.assertEqual(media["video_versions"][0]["url"], "wanted")

    def test_find_media_raises_on_login_wall(self):
        with self.assertRaises(tdl.PostError):
            tdl.find_media(ssr({"nothing": 1}), "ABC")

    def test_best_image_picks_largest(self):
        item = {"image_versions2": {"candidates": [
            {"width": 640, "height": 800, "url": "small"},
            {"width": 1440, "height": 1800, "url": "big"},
        ]}}
        self.assertEqual(tdl.best_image(item)["url"], "big")


class SelectionTests(unittest.TestCase):
    def test_single_number(self):
        self.assertEqual(tdl.parse_selection("2"), {2})

    def test_list_and_ranges(self):
        self.assertEqual(tdl.parse_selection("1, 3-5"), {1, 3, 4, 5})

    def test_none_means_every_video(self):
        self.assertIsNone(tdl.parse_selection(None))

    def test_rejects_bad_specs(self):
        for spec in ("", "0", "-1", "3-1", "x", "1-", "1,0"):
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError):
                    tdl.parse_selection(spec)

    def test_video_indexes_filters_carousel_items(self):
        items = [
            {"video_dash_manifest": "x"},
            {"image_versions2": {"candidates": []}},
            {"video_versions": [{"url": "u"}]},
        ]
        self.assertEqual(tdl.video_indexes(items), [1, 3])
        self.assertEqual(tdl.video_indexes(items, {2, 3}), [3])
        self.assertEqual(tdl.video_indexes(items, {9}), [])


class InputTests(unittest.TestCase):
    def test_collect_urls_strips_comments_and_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "links.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# comment\nhttps://www.threads.com/@a/post/AAA\n\n"
                         "https://www.threads.com/@a/post/AAA  # again\n")
            args = SimpleNamespace(urls=[], file=[path])
            urls = tdl.collect_urls(args)
        self.assertEqual(urls, ["https://www.threads.com/@a/post/AAA"])

    def test_collect_urls_adds_scheme(self):
        args = SimpleNamespace(urls=["threads.com/@a/post/AAA"], file=None)
        self.assertEqual(tdl.collect_urls(args), ["https://threads.com/@a/post/AAA"])

    def test_cookie_file_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cookies.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# Netscape HTTP Cookie File\n")
                fh.write(".threads.com\tTRUE\t/\tTRUE\t0\tsessionid\tabc123\n")
                fh.write(".threads.com\tTRUE\t/\tTRUE\t0\tds_user_id\t42\n")
            header = tdl.cookie_header_from_file(path)
        self.assertIn("sessionid=abc123", header)
        self.assertIn("ds_user_id=42", header)

    def test_safe_filename(self):
        self.assertEqual(tdl.safe("weird/user name!"), "weird_user_name")

    def test_source_has_no_non_ascii(self):
        """Windows consoles and CI logs must not choke on the output markers."""
        with open(tdl.__file__, "rb") as fh:
            self.assertTrue(all(b < 128 for b in fh.read()))


class CliTests(unittest.TestCase):
    def test_version(self):
        with self.assertRaises(SystemExit) as ctx:
            tdl.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_missing_links_is_an_error(self):
        with mock.patch.object(sys, "argv", ["threads-dl"]):
            with self.assertRaises(SystemExit) as ctx:
                tdl.main([])
        self.assertEqual(ctx.exception.code, 2)

    def test_bad_video_selection_is_an_error(self):
        with self.assertRaises(SystemExit) as ctx:
            tdl.main(["https://www.threads.com/@a/post/AAA", "--video", "0"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
