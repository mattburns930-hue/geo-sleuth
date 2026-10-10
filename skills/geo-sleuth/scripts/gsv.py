#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow"]
# ///
"""Google Street View (the main source outside China): find points, see dates and nearby points, render by heading, build comparison sheets. Usage mirrors baidu_pano.py.

Uses the endpoints the Google Maps web page itself calls; no key needed (lookup code: _streetview.py).
There is almost no Google Street View coverage in mainland China; for photos from mainland China use baidu_pano.py.

Examples:
  gsv.py near 35.6595,139.7005 --radius 50                   # nearest panorama point: id, coordinates, capture date, historical captures, address, nearby points
  gsv.py render XlVh96-Z9lAI5tKrU2O4Yg --heading 90 --out v.jpg
  gsv.py sheet --at 35.6595,139.7005 --headings 0,60,120,180,240,300 --out around.jpg   # look around from a single point
  gsv.py sheet --ids ID1,ID2 --toward 35.6600,139.7010 --out s.jpg                     # every point faces the same target
  gsv.py sheet --points pts.json --heading 90 --date 2018 --out s2018.jpg              # only one capture (the year of the street-view watermark in the photo)
"""
from __future__ import annotations

import argparse
from _net import curl_args, is_image_file, PROXY_HELP
from _streetview import THUMB, UA, near, parse_meta  # noqa: F401  (parse_meta re-exported for tests)
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
import geo  # noqa: E402
from baidu_pano import _font  # noqa: E402


def _curl(url: str, proxy: str | None, out: Path | None = None) -> bytes:
    cmd = ["curl", "-q", "-s", "-m", "40", "-A", UA]
    cmd += curl_args(proxy)
    if out:
        cmd += ["-o", str(out)]
    r = subprocess.run(cmd + [url], capture_output=True)
    return r.stdout


def pick_date(res: dict, date: str) -> dict | None:
    """Pick one capture ('2018' or '2018-07') from a near() result: the first match among this panorama and its historical captures; None if there is none."""
    for p in [{"id": res["id"], "wgs": res["wgs"], "date": res["date"]}, *res.get("history", [])]:
        if p["date"] and p["date"].startswith(date):
            return p
    return None


def render(pid: str, heading: float, pitch: float, fov: float, w: int, h: int, proxy: str | None,
           cache: Path) -> Image.Image:
    """heading: compass bearing; positive pitch = looking up; fov: horizontal field of view."""
    cache.mkdir(parents=True, exist_ok=True)
    p = cache / f"{pid}_{heading:.0f}_{pitch:.0f}_{fov:.0f}_{w}x{h}.jpg"
    if not is_image_file(p):
        p.unlink(missing_ok=True)
        _curl(THUMB.format(id=pid, w=w, h=h, yaw=heading % 360, pitch=-pitch, fov=fov), proxy, p)
    try:
        return Image.open(p).convert("RGB")
    except Exception:  # noqa: BLE001
        p.unlink(missing_ok=True)  # never keep an error body under an image name
        print(f"warning: Street View render failed for {pid} (heading {heading:.0f})", file=sys.stderr)
        return Image.new("RGB", (w, h), "gray")


def sheet(items: list[dict], out: Path, proxy: str | None, cache: Path, cols: int = 3, tw: int = 480, th: int = 360) -> None:
    with ThreadPoolExecutor(8) as ex:
        ims = list(ex.map(lambda it: render(it["id"], it["heading"], it.get("pitch", 0), it.get("fov", 90),
                                           640, 480, proxy, cache), items))
    rows = (len(items) + cols - 1) // cols
    S = Image.new("RGB", (cols * tw, max(1, rows) * th), "black")
    d = ImageDraw.Draw(S)
    f = _font(16)
    for i, (it, im) in enumerate(zip(items, ims)):
        x, y = (i % cols) * tw, (i // cols) * th
        S.paste(im.resize((tw, th)), (x, y))
        d.rectangle([x, y, x + tw, y + 22], fill="black")
        d.text((x + 4, y + 2), it.get("label") or f"{i}: …{it['id'][-8:]} h{it['heading']:.0f}", fill="yellow", font=f)
    S.save(out, quality=88)



def _neg_coords(argv: list[str]) -> list[str]:
    """argparse treats negative coordinates like -1.45,-48.5 as option names; prefixing a space makes them plain values (float ignores the space). Needed for every puzzle in the southern or western hemisphere."""
    return [" " + a if re.match(r"^-\d[\d.]*(,-?[\d.]+)+$", a) else a for a in argv]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--proxy", default=os.environ.get("GEO_PROXY"), help=PROXY_HELP)
    ap.add_argument("--cache", type=Path, default=Path(".geo-cache/gsv"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--proxy", default=argparse.SUPPRESS, help="can go before or after the subcommand")
        sp.add_argument("--cache", type=Path, default=argparse.SUPPRESS)

    n = sub.add_parser("near")
    common(n)
    n.add_argument("latlon")
    n.add_argument("--radius", type=float, default=50)

    r = sub.add_parser("render")
    common(r)
    r.add_argument("id")
    r.add_argument("--heading", type=float, required=True)
    r.add_argument("--pitch", type=float, default=0, help="positive = looking up")
    r.add_argument("--fov", type=float, default=90, help="horizontal field of view")
    r.add_argument("--width", type=int, default=1024)
    r.add_argument("--height", type=int, default=768)
    r.add_argument("--out", type=Path, required=True)

    s = sub.add_parser("sheet")
    common(s)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--ids", help="comma-separated panoids")
    g.add_argument("--at", help="lat,lon: take the nearest panorama point")
    g.add_argument("--points", type=Path, help="JSON {name:[lat,lon]}: take the nearest panorama point for each")
    h = s.add_mutually_exclusive_group(required=True)
    h.add_argument("--heading", type=float)
    h.add_argument("--headings", help="comma-separated, e.g. 0,60,120,180,240,300")
    h.add_argument("--toward", help="lat,lon: every point faces this target")
    s.add_argument("--offset", type=float, default=0)
    s.add_argument("--pitch", type=float, default=0)
    s.add_argument("--fov", type=float, default=90)
    s.add_argument("--radius", type=float, default=50)
    s.add_argument("--date", help="only this capture (2018 or 2018-07), picked from this panorama and its historical captures; use it for the street-view watermark year in the photo")
    s.add_argument("--limit", type=int, default=12)
    s.add_argument("--out", type=Path, required=True)

    args = ap.parse_args(_neg_coords(sys.argv[1:]))
    if args.cmd == "near":
        lat, lon = map(float, args.latlon.split(","))
        res = near(lat, lon, args.radius, args.proxy)
        print(json.dumps(res, ensure_ascii=False, indent=1) if res else "no usable Google Street View result (increase --radius, check coverage, or run doctor.py --network to check service access)")
    elif args.cmd == "render":
        render(args.id, args.heading, args.pitch, args.fov, args.width, args.height, args.proxy, args.cache).save(args.out)
        print(args.out)
    else:
        panos: dict[str, dict] = {}
        if args.ids:
            panos = {pid: {"ll": None, "name": "", "date": ""} for pid in args.ids.split(",")}
        else:
            pts = {"at": list(map(float, args.at.split(",")))} if args.at else json.loads(args.points.read_text(encoding="utf-8"))
            for name, (la, lo) in pts.items():
                res = near(la, lo, args.radius, args.proxy)
                if res and args.date:
                    p = pick_date(res, args.date)
                    if not p:
                        print(f"{name}: no {args.date} capture (available: {', '.join(res['dates_seen'])})", file=sys.stderr)
                        continue
                    panos.setdefault(p["id"], {"ll": p["wgs"], "name": name, "date": p["date"]})
                elif res:
                    panos.setdefault(res["id"], {"ll": res["wgs"], "name": name, "date": res.get("date") or ""})
                else:
                    print(f"{name}: no panorama nearby", file=sys.stderr)
        target = tuple(map(float, args.toward.split(","))) if args.toward else None
        items = []
        for pid, meta in panos.items():
            ll = meta["ll"]
            if args.headings:
                heads = [float(x) for x in args.headings.split(",")]
            elif target:
                if ll is None:
                    info = near(*target, 5000, args.proxy)  # with only an id there are no coordinates, so estimate from near the target; --points is recommended
                    ll = info["wgs"] if info else list(target)
                heads = [geo.bearing(tuple(ll), target) + args.offset]
            else:
                heads = [args.heading]
            for hd in heads:
                where = f"{ll[0]:.5f},{ll[1]:.5f}" if ll else f"…{pid[-8:]}"
                label = " ".join(x for x in (meta["name"][:18], where, meta["date"], f"h{hd % 360:.0f}") if x)
                items.append({"id": pid, "heading": hd % 360, "pitch": args.pitch, "fov": args.fov,
                              "label": f"{len(items)}: {label}", "point": meta["name"], "wgs": ll, "date": meta["date"]})
        pages = [items[k:k + args.limit] for k in range(0, len(items), args.limit)] or [[]]
        for pi, page in enumerate(pages):
            out = args.out if pi == 0 else args.out.with_name(f"{args.out.stem}_{pi + 1}{args.out.suffix}")
            sheet(page, out, args.proxy, args.cache)
            print(out)
        args.out.with_suffix(".index.json").write_text(json.dumps(items, indent=1), encoding="utf-8")


if __name__ == "__main__":
    # Chinese-locale Windows writes GBK by default: m², ñ make it crash, and the Chinese the agent reads comes out garbled
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    main()
