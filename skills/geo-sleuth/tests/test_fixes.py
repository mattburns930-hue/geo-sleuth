#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow", "numpy"]
# ///
"""Offline regressions for Street View lookup, cache validation, lens fitting, board assumptions and upload diagnosis.
Run: uv run skills/geo-sleuth/tests/test_fixes.py"""
from __future__ import annotations

import asyncio
import http.server
import io
import json
import math
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from PIL import Image  # noqa: E402

import _browser  # noqa: E402
import _net  # noqa: E402
import _streetview  # noqa: E402
import doctor  # noqa: E402
import geo  # noqa: E402
import tiles  # noqa: E402


def _png(color=(10, 120, 30), noise=True) -> bytes:
    im = Image.new("RGB", (64, 64), color)
    if noise:
        random.seed(0)
        for _ in range(600):
            im.putpixel((random.randrange(64), random.randrange(64)), (random.randrange(256),) * 3)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _meta_response(pid="AbCdEfGhIjKlMnOpQrStUv", lat=51.3837, lon=-2.3645) -> str:
    """Minimal SingleImageSearch body in the layout parse_meta reads."""
    nb = [[[2, "N1nNnNnNnNnNnNnNnNnNnN"], None, [[None, None, lat + 1e-4, lon]]],
          [[2, "N2nNnNnNnNnNnNnNnNnNnN"], None, [[None, None, lat - 1e-4, lon]]]]
    body = [[1], [2, pid], None, [None, None, [["Queen's Parade", "en"], ["Bath, England", "en"]]], None,
            [[[1], [[None, None, lat, lon], [35.0, None, 84.0], [41.1, 87.3, 0.3]], None, [nb], None, None, None, None,
              [[0, [2024, 8]], [1, [2018, 8]]]]],
            [None, None, None, None, None, None, None, [2014, 7]]]
    return json.dumps([[0], body])


class StreetViewTests(unittest.TestCase):
    def test_parse_new_rpc_response(self):
        r = _streetview.parse_meta(_meta_response())
        self.assertEqual(r["id"], "AbCdEfGhIjKlMnOpQrStUv")
        self.assertEqual(r["date"], "2014-07")
        self.assertEqual(r["address"], "Queen's Parade / Bath, England")
        self.assertEqual([h["date"] for h in r["history"]], ["2024-08", "2018-08"])
        self.assertEqual(r["dates_seen"], ["2014-07", "2018-08", "2024-08"])

    def test_parse_accepts_old_jsonp_wrapping(self):
        self.assertEqual(_streetview.parse_meta("/**/cb && cb( " + _meta_response() + " )")["id"], "AbCdEfGhIjKlMnOpQrStUv")

    def test_decommissioned_endpoint_is_no_panorama(self):
        dead = '/**/cb && cb( [[5,"generic","GeoPhotoService.SingleImageSearch is decommissioned and turned down."]] )'
        self.assertIsNone(_streetview.parse_meta(dead))
        self.assertIsNone(_streetview.parse_meta(""))

    def test_request_body_carries_coordinates_and_radius(self):
        body = json.loads(_streetview.meta_body(51.5, -2.25, 75))
        self.assertEqual(body[1], [[None, None, 51.5, -2.25], 75])

    def test_doctor_fails_when_lookup_returns_nothing(self):
        dead = '[[5,"generic","decommissioned"]]'
        with patch.object(doctor, "fetch_meta", return_value=dead):
            row = doctor.streetview_check("direct")
        self.assertEqual(row["status"], "FAIL")
        with patch.object(doctor, "fetch_meta", return_value=_meta_response()):
            self.assertEqual(doctor.streetview_check("direct")["status"], "PASS")


class _Server:
    """Loopback server: /png → image, /blocked → 403 text, /html → 200 HTML."""

    def __enter__(self):
        png = _png()

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/png"):
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.end_headers()
                    self.wfile.write(png)
                elif self.path.startswith("/html"):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"<html>maintenance</html>")
                else:
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b"Host not in allowlist: tiles.example. Add this host to your network egress settings.")

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()


@unittest.skipUnless(shutil.which("curl"), "curl required")
class CacheTests(unittest.TestCase):
    def test_image_kind(self):
        self.assertEqual(_net.image_kind(_png()), "png")
        self.assertEqual(_net.image_kind(b"\xff\xd8\xff\xe0rest"), "jpeg")
        self.assertIsNone(_net.image_kind(b"Host not in allowlist: x"))
        self.assertIsNone(_net.image_kind(b""))

    def test_error_bodies_are_never_cached(self):
        with tempfile.TemporaryDirectory() as d, _Server() as s:
            for path in ("/blocked", "/html"):
                p = Path(d) / f"t{path[1:]}.png"
                ok, reason = _net.download_image(s.base + path, p, "direct", timeout=10)
                self.assertFalse(ok)
                self.assertFalse(p.exists(), f"{path} must not be cached")
                self.assertIn("not an image", reason)
            self.assertEqual(list(Path(d).iterdir()), [])  # no .part leftovers

    def test_poisoned_cache_is_replaced(self):
        with tempfile.TemporaryDirectory() as d, _Server() as s:
            p = Path(d) / "tile.png"
            p.write_bytes(b"Host not in allowlist: server.arcgisonline.com.")
            ok, reason = _net.download_image(s.base + "/png", p, "direct", timeout=10)
            self.assertTrue(ok)
            self.assertEqual(reason, "downloaded")
            self.assertTrue(_net.is_image_file(p))
            self.assertEqual(_net.download_image(s.base + "/blocked", p, "direct")[1], "cached")

    def test_tiles_get_reports_host_and_reason(self):
        with tempfile.TemporaryDirectory() as d, _Server() as s:
            report: dict = {}
            self.assertFalse(tiles._get(s.base + "/blocked", Path(d) / "a.jpg", "direct", report))
            self.assertEqual(report["failed"], 1)
            self.assertIn("HTTP 403", next(iter(report["reasons"].values())))

    def test_placeholder_detection(self):
        self.assertTrue(tiles._is_placeholder(Image.new("RGB", (256, 256), (204, 204, 204))))
        self.assertFalse(tiles._is_placeholder(Image.open(io.BytesIO(_png()))))
        self.assertFalse(tiles._is_placeholder(Image.new("RGB", (256, 256), (20, 60, 110))))  # open sea is not a placeholder


class LensFitTests(unittest.TestCase):
    W, H = 1200, 1600
    CAM = (51.38365, -2.36445)

    def _feats(self, mm, heading=55.0, noise=4.0, seed=1):
        random.seed(seed)
        f = geo.lens_focal_px(mm, self.W, self.H)
        hfov = 2 * math.degrees(math.atan(self.W / 2 / f))
        out = []
        for k, (off, dist) in enumerate([(-0.8, 25), (-0.3, 60), (0.1, 40), (0.45, 90), (0.75, 30)]):
            a = off * hfov / 2
            out.append((f"F{k}", self.W / 2 + f * math.tan(math.radians(a)) + random.gauss(0, noise),
                        [geo.dest(self.CAM, heading + a, dist)]))
        return out

    def test_focal_px_follows_aspect(self):
        self.assertAlmostEqual(geo.lens_focal_px(24, 1200, 1600), 1109.4, delta=1)   # 3:4 portrait, 1x
        self.assertAlmostEqual(geo.lens_focal_px(13, 1200, 1600), 600.9, delta=1)    # 0.5x
        self.assertAlmostEqual(geo.lens_focal_px(24, 1179, 2093), geo.lens_focal_px(24, 1569, 2093), delta=1)  # 9:16 story keeps height (same as its 3:4 frame)

    def test_recovers_lens_and_position(self):
        for lens, mm in (("1x", 24.0), ("0.5x", 13.0), ("2x", 48.0)):
            fits = geo.fit_camera(self._feats(mm), self.W, self.H, self.CAM, 100, 5, continuous=False)
            self.assertEqual(fits[0]["lens"], lens)
            self.assertLess(geo.distance(tuple(fits[0]["cam"]), self.CAM), 10)
            self.assertAlmostEqual(fits[0]["heading"], 55.0, delta=1.5)

    def test_pixel_score_does_not_favour_telephoto(self):
        fits = geo.fit_camera(self._feats(24.0), self.W, self.H, self.CAM, 100, 5, continuous=False)
        tele = next(r for r in fits if r["lens"] == "5x")
        self.assertGreater(tele["rms_px"], 5 * fits[0]["rms_px"])

    def test_alternative_identity_is_chosen(self):
        feats = self._feats(24.0)
        wrong = geo.dest(self.CAM, 0, 200)
        name, x, alts = feats[2]
        feats[2] = (name, x, [wrong, alts[0]])
        best = geo.fit_camera(feats, self.W, self.H, self.CAM, 100, 5, continuous=False)[0]
        self.assertEqual(best["identities"][name], 1)

    def test_verdict_prefers_1x_unless_clearly_beaten(self):
        mk = lambda lens, mm, rms, c=False: {"lens": lens, "equiv_mm": mm, "zoom": mm / 24, "rms_px": rms, "continuous": c}  # noqa: E731
        self.assertIn("1x fits best", geo.lens_verdict([mk("1x", 24, 4), mk("0.5x", 13, 40)]))
        self.assertIn("unresolved", geo.lens_verdict([mk("0.5x", 13, 5), mk("1x", 24, 6)]))
        self.assertIn("clearly better", geo.lens_verdict([mk("0.5x", 13, 3), mk("1x", 24, 50)]))
        # a fitted zoom that lands on a lens step is that lens, not a rival to it
        self.assertTrue(geo.lens_verdict([mk("fitted 1.00x", 24.1, 2.8, True), mk("1x", 24, 2.9)]).startswith("1x fits best"))
        self.assertIn("between lens steps", geo.lens_verdict([mk("fitted 2.69x", 64.6, 2.8, True), mk("3x", 77, 23), mk("1x", 24, 110)]))

    def test_zoom_is_fitted_between_lens_steps(self):
        for mm in (64.8, 38.4):  # 2.7x and 1.6x: digital zoom / crops, not a phone lens
            best = geo.fit_camera(self._feats(mm), self.W, self.H, self.CAM, 100, 5)[0]
            self.assertTrue(best["continuous"], best["lens"])
            self.assertAlmostEqual(best["equiv_mm"], mm, delta=mm * 0.03)
            self.assertLess(geo.distance(tuple(best["cam"]), self.CAM), 10)

    def test_needs_three_features(self):
        with self.assertRaises(ValueError):
            geo.fit_camera(self._feats(24.0)[:2], self.W, self.H, self.CAM, 50, 10)


class BoardAssumptionTests(unittest.TestCase):
    def _board(self, d, *args):
        r = subprocess.run([sys.executable, str(SCRIPTS / "board.py"), *args], cwd=d, capture_output=True, text=True,
                           encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_revised_lens_reopens_and_sets_aside_old_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "fit.json").write_text("{}")
            self._board(d, "init", "--photo", "p.jpg")
            self._board(d, "add", "NW corner", "SW corner", "--level", "point")
            self._board(d, "clue", "bearing fit", "--kind", "geometry", "--status", "computed", "--file", "fit.json")
            self._board(d, "evidence", "--clue", "K1", "--against", "NW corner:0.1", "--for", "SW corner:5",
                        "--assumes", "lens=1x", "--file", "fit.json")
            self._board(d, "exclude", "NW corner", "--clue", "K1", "--computed", "fit.json", "--assumes", "lens=1x")
            self.assertIn("unverified assumption lens=1x", self._board(d, "check"))
            listing = self._board(d, "assume", "lens=0.5x")
            self.assertIn("excluded under lens=1x: NW corner", listing)
            self.assertIn("supported under lens=1x: SW corner", listing)
            b = json.loads(Path(d, "board.json").read_text())
            self.assertEqual(b["candidates"]["NW corner"]["status"], "excluded")  # listing alone changes nothing
            self._board(d, "assume", "lens=0.5x", "--verified", "--reopen")
            b = json.loads(Path(d, "board.json").read_text())
            self.assertEqual(b["candidates"]["NW corner"]["status"], "open")
            self.assertEqual(b["candidates"]["NW corner"]["exclusion_history"][0]["reopened_by"], "lens=0.5x")
            self.assertTrue(all(e.get("superseded_by") == "lens=0.5x" for e in b["evidence"]))
            self.assertIn("50.0%", self._board(d, "rank"))

    def test_evidence_without_assumptions_is_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            self._board(d, "init", "--photo", "p.jpg")
            self._board(d, "add", "A", "B", "--level", "city")
            self._board(d, "clue", "plate", "--kind", "plate", "--status", "observed")
            self._board(d, "evidence", "--clue", "K1", "--for", "A:3")
            self.assertIn("Nothing on the board", self._board(d, "assume", "lens=2x", "--reopen"))


class UploadDiagnosisTests(unittest.IsolatedAsyncioTestCase):
    def _page(self, url="https://graph.baidu.com/pcpage/index", body="上传图片 识图"):
        page = MagicMock()
        page.url = url
        page.inner_text = AsyncMock(return_value=body)
        return page

    async def test_blocked_resources_are_a_network_problem(self):
        why = await _browser.missing_upload_reason(self._page(body=""), ["s1.bdstatic.com", "mms-static.cdn.bcebos.com"], "Baidu")
        self.assertIn("network", why)
        self.assertIn("bdstatic.com", why)
        self.assertNotIn("changed its upload UI", why)

    async def test_captcha_is_reported_as_captcha(self):
        why = await _browser.missing_upload_reason(self._page(body="百度安全验证 请完成验证"), [], "Baidu")
        self.assertIn("captcha", why)
        why = await _browser.missing_upload_reason(self._page(url="https://wappass.baidu.com/static/captcha"), [], "Baidu")
        self.assertIn("captcha", why)

    async def test_empty_page_and_real_redesign(self):
        self.assertIn("rendered empty", await _browser.missing_upload_reason(self._page(body=""), [], "Baidu"))
        self.assertIn("changed its upload UI", await _browser.missing_upload_reason(
            self._page(body="新版识图 欢迎使用 这里没有上传控件的页面文字很多"), [], "Baidu"))

    async def test_open_page_distinguishes_network_and_http(self):
        page = MagicMock()
        page.goto = AsyncMock(side_effect=Exception("net::ERR_TUNNEL_CONNECTION_FAILED at https://graph.baidu.com/"))
        self.assertIn("network unreachable", await _browser.open_page(page, "https://graph.baidu.com/x"))
        page.goto = AsyncMock(return_value=MagicMock(status=403))
        self.assertIn("HTTP 403", await _browser.open_page(page, "https://graph.baidu.com/x"))
        page.goto = AsyncMock(return_value=MagicMock(status=200))
        self.assertIsNone(await _browser.open_page(page, "https://graph.baidu.com/x"))

    async def test_find_upload_prefers_file_input(self):
        page = MagicMock()
        el = object()
        page.query_selector = AsyncMock(side_effect=lambda sel: el if sel == "input[accept*='image']" else None)
        self.assertEqual(await _browser.find_upload(page, ()), ("input", el))
        page.query_selector = AsyncMock(return_value=None)
        loc = MagicMock()
        loc.count = AsyncMock(return_value=0)
        page.get_by_text = MagicMock(return_value=loc)
        page.locator = MagicMock(return_value=loc)
        self.assertEqual(await _browser.find_upload(page, ("上传图片", "css=[class*='upload']")), (None, None))


if __name__ == "__main__":
    unittest.main(verbosity=1)
