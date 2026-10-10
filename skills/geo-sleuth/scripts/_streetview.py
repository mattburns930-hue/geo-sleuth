"""Google Street View metadata lookup shared by gsv.py and doctor.py (standard library only).

The JSONP GeoPhotoService.SingleImageSearch endpoint was turned down ("decommissioned and turned down").
The Maps JS client now calls the gRPC-web JSON method below; its response body keeps the old layout,
so the parser is unchanged. The request filters to official Street View car coverage (user-uploaded
panorama photos have ids like CIHM0og…, and the perspective endpoint can't render them).
"""
from __future__ import annotations

import json
import re
import subprocess

from _net import curl_args

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
META = "https://maps.googleapis.com/$rpc/google.internal.maps.mapsjs.v1.MapsJsInternalService/SingleImageSearch"
THUMB = ("https://streetviewpixels-pa.googleapis.com/v1/thumbnail?panoid={id}&cb_client=maps_sv.tactile"
         "&w={w}&h={h}&yaw={yaw:.1f}&pitch={pitch:.1f}&thumbfov={fov:.0f}")


def meta_body(lat: float, lon: float, radius: float) -> str:
    return json.dumps([
        ["apiv3", None, None, None, "US", None, None, None, None, None, [[0]]],
        [[None, None, lat, lon], radius],
        [[None, None, None, None, None, None, None, None, None, None, [None, None]],
         None, None, None, None, None, None, None, [2], None, [[[2, True, 2]]]],
        [[1, 2, 3, 4, 8, 6]],
    ])


def fetch_meta(lat: float, lon: float, radius: float, proxy: str | None, timeout: int = 40) -> str:
    cmd = ["curl", "-q", "-s", "-m", str(timeout), "-A", UA, *curl_args(proxy),
           "-X", "POST", "-H", "Content-Type: application/json+protobuf",
           "-H", "Origin: https://www.google.com", "--data-binary", meta_body(lat, lon, radius), META]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
    return r.stdout.decode("utf-8", "replace")


def _ym(v: list) -> str:
    return f"{v[0]}-{int(v[1]):02d}"


def parse_meta(t: str) -> dict | None:
    """Parse a SingleImageSearch response (also accepts the old JSONP `cb(...)` wrapping). None if no panorama."""
    m = re.search(r"cb\(\s*(.*)\s*\)\s*;?\s*$", t, re.S)
    try:
        d = json.loads(m.group(1) if m else t)
        body = d[1]
    except (json.JSONDecodeError, IndexError, TypeError, KeyError):
        return None
    out: dict = {}
    try:
        out["id"] = body[1][1]
        loc = body[5][0][1]
        out["wgs"] = [loc[0][2], loc[0][3]]
        out["pano_heading"] = round(loc[2][0], 1) if loc[2] else None
    except (IndexError, TypeError):
        return None
    try:
        out["address"] = " / ".join(x[0] for x in body[3][2])
    except (IndexError, TypeError):
        out["address"] = ""
    # this panorama's capture date is in body[6][7]; body[5][0][8] holds historical captures [neighbor index, [year, month(, day)]], don't take it as this panorama's date
    try:
        out["date"] = _ym(body[6][7])
    except (IndexError, TypeError):
        out["date"] = None
    nbrs = []
    try:
        for e in body[5][0][3][0]:
            nbrs.append({"id": e[0][1], "wgs": [e[2][0][2], e[2][0][3]]})
    except (IndexError, TypeError):
        pass
    hist = []
    try:
        for idx, ym, *_ in body[5][0][8] or []:
            # user-uploaded panoramas get mixed into historical captures (perspective renders come out gray): ids start with CIHM0og/CIAB/CAoS, length varies from 22 to 28
            pid = nbrs[idx]["id"] if idx < len(nbrs) else ""
            if len(pid) == 22 and not pid.startswith(("CIHM", "CIAB", "CAoS")):
                hist.append({**nbrs[idx], "date": _ym(ym)})
    except (IndexError, TypeError, ValueError):
        pass
    out["history"] = sorted(hist, key=lambda h: h["date"], reverse=True)
    out["dates_seen"] = sorted({d for d in [out["date"], *(h["date"] for h in hist)] if d})
    out["neighbors"] = nbrs[:40]
    return out


def near(lat: float, lon: float, radius: float, proxy: str | None) -> dict | None:
    return parse_meta(fetch_meta(lat, lon, radius, proxy))
