#!/usr/bin/env python3
"""Build the animated SVG assets behind the Bluenot3 profile README.

Every asset is a single self-contained SVG so it survives GitHub's <img> sandbox:
  * fonts are subset to the exact glyphs used and embedded as WOFF2 data URIs
  * the ZEN "ZZ" monogram is inline vector geometry (traced from the master PNG)
  * all motion is CSS keyframes or SMIL, no scripts, no external requests
  * no blur filters on anything that animates, so repaint stays cheap

    pip install fonttools brotli
    python3 scripts/build_profile_assets.py

Fonts (SIL OFL) and the world-atlas topology are downloaded once into .cache/.
"""
from __future__ import annotations

import base64
import io
import json
import math
import random
import tarfile
import urllib.request
from collections import defaultdict
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "profile"
CACHE = ROOT / ".cache"

# ─────────────────────────────────────────────────────────────── brand tokens
INK = "#03050A"
PANEL = "#070B14"
RED = "#EF4444"
RED_HOT = "#FF4D4D"
RED_SOFT = "#FCA5A5"
BLUE = "#2563EB"
BLUE_HOT = "#3B82F6"
ICE = "#93C5FD"
WHITE = "#F8FAFC"
MUTED = "#94A3B8"
DIM = "#64748B"
LINE = "#1E293B"
GOLD = "#E8B030"
GOLD_DEEP = "#C89218"
GREEN = "#22C55E"

# ─────────────────────────────────────────────────────────────── fonts
FONT_SOURCES = {
    "unbounded": "ofl/unbounded/Unbounded%5Bwght%5D.ttf",
    "jbmono": "ofl/jetbrainsmono/JetBrainsMono%5Bwght%5D.ttf",
    "inter": "ofl/inter/Inter%5Bopsz,wght%5D.ttf",
}
# key -> (source, axes, css family name)
FONTS = {
    "display": ("unbounded", {"wght": 800}, "ZDisplay"),
    "displayM": ("unbounded", {"wght": 500}, "ZDisplayM"),
    "mono": ("jbmono", {"wght": 500}, "ZMono"),
    "monoB": ("jbmono", {"wght": 800}, "ZMonoB"),
    "sans": ("inter", {"wght": 500, "opsz": 14}, "ZSans"),
    "sansB": ("inter", {"wght": 700, "opsz": 18}, "ZSansB"),
}
FALLBACK = {
    "display": "'Arial Black',Impact,sans-serif",
    "displayM": "'Segoe UI',Helvetica,Arial,sans-serif",
    "mono": "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace",
    "monoB": "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace",
    "sans": "'Segoe UI',Helvetica,Arial,sans-serif",
    "sansB": "'Segoe UI',Helvetica,Arial,sans-serif",
}


def _fetch(url: str, dest: Path) -> Path:
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"  fetching {url}")
        with urllib.request.urlopen(url, timeout=120) as r:
            dest.write_bytes(r.read())
    return dest


class FontBank:
    def __init__(self):
        self.fonts: dict[str, TTFont] = {}
        self.paths: dict[str, Path] = {}
        for key, (src, axes, _) in FONTS.items():
            static = CACHE / "fonts" / f"{key}.ttf"
            if not static.exists():
                var = _fetch(
                    "https://raw.githubusercontent.com/google/fonts/main/" + FONT_SOURCES[src],
                    CACHE / "fonts" / f"{src}-var.ttf",
                )
                instancer.instantiateVariableFont(TTFont(var), axes).save(static)
            self.paths[key] = static
            self.fonts[key] = TTFont(static)
        self._subset_cache: dict[tuple, str] = {}

    def advance(self, key: str, ch: str) -> float:
        f = self.fonts[key]
        cmap = f.getBestCmap()
        g = cmap.get(ord(ch)) or cmap.get(ord("?"))
        return f["hmtx"][g][0] / f["head"].unitsPerEm

    def width(self, key: str, text: str, size: float, ls: float = 0.0) -> float:
        return sum(self.advance(key, c) for c in text) * size + ls * max(len(text) - 1, 0)

    def woff2(self, key: str, chars: set[str]) -> str:
        chars = set(chars) | {" "}
        ck = (key, "".join(sorted(chars)))
        if ck not in self._subset_cache:
            opts = subset.Options()
            opts.flavor = "woff2"
            opts.layout_features = ["kern"]
            opts.hinting = False
            opts.desubroutinize = True
            opts.name_IDs = []
            opts.notdef_outline = False
            font = TTFont(self.paths[key], recalcTimestamp=False)
            font["head"].created = font["head"].modified = 0x7C259DC0  # fixed stamp: reproducible bytes
            s = subset.Subsetter(opts)
            s.populate(text="".join(chars))
            s.subset(font)
            buf = io.BytesIO()
            font.flavor = "woff2"
            font.save(buf)
            self._subset_cache[ck] = base64.b64encode(buf.getvalue()).decode()
        return self._subset_cache[ck]


FB: FontBank  # set in main()


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def f2(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def f4(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


class Doc:
    """Accumulates one SVG: defs, css, body, and the glyphs each font needs."""

    def __init__(self, w: int, h: int, title: str, desc: str):
        self.w, self.h, self.title, self.desc = w, h, title, desc
        self.used: dict[str, set] = defaultdict(set)
        self.defs: list[str] = []
        self.css: list[str] = []
        self.body: list[str] = []
        self._id = 0

    def uid(self, p: str = "i") -> str:
        self._id += 1
        return f"{p}{self._id}"

    def add(self, *parts: str):
        self.body.extend(parts)

    def text(self, x, y, s, font="sans", size=16, fill=WHITE, anchor="start", ls=0.0,
             opacity=None, cls="", attrs="") -> str:
        self.used[font].update(s)
        o = f' opacity="{f2(opacity)}"' if opacity is not None else ""
        a = f' text-anchor="{anchor}"' if anchor != "start" else ""
        l = f' letter-spacing="{f2(ls)}"' if ls else ""
        c = f"f-{font} {cls}".strip()
        return (f'<text x="{f2(x)}" y="{f2(y)}" class="{c}" font-size="{f2(size)}" fill="{fill}"'
                f'{a}{l}{o} {attrs}>{esc(s)}</text>')

    def need(self, font: str, chars: str):
        self.used[font].update(chars)

    def render(self) -> str:
        faces = []
        for key, chars in sorted(self.used.items()):
            fam = FONTS[key][2]
            faces.append(
                f"@font-face{{font-family:{fam};font-display:swap;src:url(data:font/woff2;base64,{FB.woff2(key, chars)}) format('woff2');}}"
                f".f-{key}{{font-family:{fam},{FALLBACK[key]};}}"
            )
        css = "\n".join(faces + self.css + [
            "@media (prefers-reduced-motion:reduce){*{animation:none!important}}",
        ])
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{self.w}" height="{self.h}" viewBox="0 0 {self.w} {self.h}" role="img" '
            f'aria-labelledby="t d" fill="none">\n'
            f'<title id="t">{esc(self.title)}</title>\n<desc id="d">{esc(self.desc)}</desc>\n'
            f"<style>\n{css}\n</style>\n<defs>\n" + "\n".join(self.defs) + "\n</defs>\n"
            + "\n".join(self.body) + "\n</svg>\n"
        )

    def save(self, name: str):
        OUT.mkdir(parents=True, exist_ok=True)
        data = self.render()
        (OUT / name).write_text(data, encoding="utf-8")
        print(f"  {name:<34} {len(data) / 1024:7.1f} KB")


# ─────────────────────────────────────────────────────────────── ZZ monogram
# Traced from the 1250px master and snapped so every diagonal is exactly 45°.
LOGO_D = ("M162 157V467H281L376 372H251V262H587L162 687V888L250 800V748L736 262H888"
          "L69 1083H1081V776H965L862 879H988V986H651L1081 556V355L990 446V508L512 986"
          "H352L1181 157Z")
LOGO_BOX = (69, 157, 1181, 1083)  # x0, y0, x1, y1
LOGO_W = LOGO_BOX[2] - LOGO_BOX[0]
LOGO_H = LOGO_BOX[3] - LOGO_BOX[1]


def logo_path_length() -> float:
    pts = []
    x = y = 0.0
    import re
    toks = re.findall(r"[MLHVZ]|-?\d+\.?\d*", LOGO_D)
    i = 0
    cmd = None
    while i < len(toks):
        t = toks[i]
        if t in "MLHVZ":
            cmd = t
            i += 1
            if cmd == "Z":
                pts.append(pts[0])
            continue
        if cmd in "ML":
            x, y = float(toks[i]), float(toks[i + 1])
            i += 2
        elif cmd == "H":
            x = float(t)
            i += 1
        elif cmd == "V":
            y = float(t)
            i += 1
        pts.append((x, y))
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


LOGO_LEN = logo_path_length()


def logo_transform(x: float, y: float, h: float) -> tuple[str, float]:
    s = h / LOGO_H
    return f"translate({f2(x)} {f2(y)}) scale({f4(s)}) translate({-LOGO_BOX[0]} {-LOGO_BOX[1]})", s


def logo_width(h: float) -> float:
    return LOGO_W * h / LOGO_H


def laser_logo(doc: Doc, x: float, y: float, h: float, *, dur: float = 7.0, fill: str = WHITE,
               fill_opacity: float = 1.0, glow: str = RED_HOT, trace: str = WHITE,
               draw_frac: float = 0.42, spark: bool = True, fill_url: str | None = None) -> str:
    """The ZZ monogram: laser-traced outline, then the solid mark resolves."""
    tf, s = logo_transform(x, y, h)
    pid = doc.uid("logo")
    doc.defs.append(f'<path id="{pid}" d="{LOGO_D}"/>')
    L = LOGO_LEN
    k1 = draw_frac
    k2 = min(draw_frac + 0.1, 0.9)
    sw = 1.6 / s  # 1.6px on screen
    parts = [f'<g transform="{tf}">']
    # the solid mark
    fl = f'url(#{fill_url})' if fill_url else fill
    parts.append(
        f'<use href="#{pid}" fill="{fl}" opacity="{f2(fill_opacity)}">'
        f'<animate attributeName="opacity" values="0;0;{f2(fill_opacity)};{f2(fill_opacity)};{f2(fill_opacity)}" '
        f'keyTimes="0;{f2(k1 * 0.8)};{f2(k2)};0.94;1" dur="{dur}s" repeatCount="indefinite"/></use>'
    )
    # glow + crisp traced outline
    for width, col, op in ((sw * 5, glow, 0.18), (sw * 2.4, glow, 0.35), (sw, trace, 1)):
        parts.append(
            f'<use href="#{pid}" stroke="{col}" stroke-width="{f2(width)}" stroke-opacity="{op}" '
            f'stroke-linejoin="round" fill="none" stroke-dasharray="{f2(L)}" stroke-dashoffset="{f2(L)}">'
            f'<animate attributeName="stroke-dashoffset" values="{f2(L)};0;0;0" keyTimes="0;{f2(k1)};0.94;1" '
            f'dur="{dur}s" repeatCount="indefinite"/>'
            f'<animate attributeName="opacity" values="1;1;{0.25 if col == trace else 0};1" keyTimes="0;{f2(k2)};0.97;1" '
            f'dur="{dur}s" repeatCount="indefinite"/></use>'
        )
    if spark:
        parts.append(
            f'<g><circle r="{f2(9 / s)}" fill="{glow}" opacity=".35"/><circle r="{f2(3.2 / s)}" fill="#fff"/>'
            f'<animateMotion dur="{dur}s" repeatCount="indefinite" keyPoints="0;1;1;1" keyTimes="0;{f2(k1)};0.99;1" '
            f'calcMode="linear"><mpath href="#{pid}"/></animateMotion>'
            f'<animate attributeName="opacity" values="1;1;0;0" keyTimes="0;{f2(k1)};{f2(k1 + 0.02)};1" dur="{dur}s" repeatCount="indefinite"/></g>'
        )
    parts.append("</g>")
    return "".join(parts)


def static_logo(x: float, y: float, h: float, fill: str = WHITE, extra: str = "") -> str:
    tf, _ = logo_transform(x, y, h)
    return f'<path transform="{tf}" d="{LOGO_D}" fill="{fill}" {extra}/>'


# ─────────────────────────────────────────────────────────────── world data
def load_world():
    topo_path = CACHE / "countries-110m.json"
    if not topo_path.exists():
        tgz = _fetch("https://registry.npmjs.org/world-atlas/-/world-atlas-2.0.2.tgz",
                     CACHE / "world-atlas-2.0.2.tgz")
        with tarfile.open(tgz) as t:
            topo_path.write_bytes(t.extractfile("package/countries-110m.json").read())
    topo = json.loads(topo_path.read_text())
    sx, sy = topo["transform"]["scale"]
    tx, ty = topo["transform"]["translate"]
    arcs = []
    for arc in topo["arcs"]:
        x = y = 0
        pts = []
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append((x * sx + tx, y * sy + ty))
        arcs.append(pts)

    def ring(idx):
        pts = []
        for a in idx:
            seg = arcs[a] if a >= 0 else arcs[~a][::-1]
            pts.extend(seg if not pts else seg[1:])
        return pts

    countries = []
    for g in topo["objects"]["countries"]["geometries"]:
        polys = []
        if g["type"] == "Polygon":
            polys = [[ring(r) for r in g["arcs"]]]
        elif g["type"] == "MultiPolygon":
            polys = [[ring(r) for r in p] for p in g["arcs"]]
        for p in polys:
            xs = [q[0] for q in p[0]]
            ys = [q[1] for q in p[0]]
            countries.append((g.get("id"), (min(xs), min(ys), max(xs), max(ys)), p))
    return countries


def _pip(x, y, poly):
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def country_at(countries, lon, lat):
    for cid, (x0, y0, x1, y1), rings in countries:
        if x0 <= lon <= x1 and y0 <= lat <= y1:
            if _pip(lon, lat, rings[0]) and not any(_pip(lon, lat, h) for h in rings[1:]):
                return cid or "land"
    return None


# ─────────────────────────────────────────────────────────────── globe
def globe(doc: Doc, cx: float, cy: float, R: float, *, tilt: float = 22, period: float = 72,
          step: float = 2.8, face_lon: float = -77.04, beacon=True, lat_range=(-56, 78),
          see_through: bool = False) -> tuple[str, str, str, str]:
    """A rotating dotted Earth in pure SVG.

    Each latitude ring is a single path of zero-length round-capped segments laid on the unit
    circle, placed with translate+scale so the unit circle projects to that ring's ellipse.
    Rotating the path (one animateTransform per ring) moves every dot at the correct angular
    speed, `vector-effect: non-scaling-stroke` keeps dots round, and a per-ring clip at the
    true horizon hides the far side. `see_through` adds a faint far-side hemisphere (costs ~2x
    paint time, so it is off by default). Returns (back, sphere, front, beacon) layers; draw the
    beacon last so its label sits above any orbits.
    """
    countries = load_world()
    a = math.radians(tilt)
    offset = -face_lon  # longitude that faces the viewer at t=0
    back, front, top = [], [], []
    NA = {"840": "us", "124": "na", "484": "na"}
    lat = lat_range[0]
    rings = []
    while lat <= lat_range[1]:
        rings.append(lat)
        lat += step
    for i, lat in enumerate(rings):
        phi = math.radians(lat)
        r = R * math.cos(phi)
        b = r * math.sin(a)
        cy0 = cy - R * math.sin(phi) * math.cos(a)
        t = -math.tan(phi) * math.tan(a)
        if t >= 0.999:
            continue
        n = max(8, round(360 * math.cos(phi) / step))
        buckets = defaultdict(list)
        for k in range(n):
            lon = -180 + (k + 0.5) * 360 / n
            c = country_at(countries, lon, lat)
            kind = NA.get(c, "land") if c else None
            lam = math.radians(lon + offset)
            p = f"M{f4(math.sin(lam))} {f4(math.cos(lam))}h.002"
            if kind:
                buckets[kind].append(p)
            elif abs(((lon + 180) % 30) - 15) > 15 - 180 / n:  # meridian grid
                buckets["grid"].append(p)
            elif i % 2 == 0 and k % 2 == 0:  # sparse ocean field
                buckets["ocean"].append(p)
        if abs(lat % 30) < step / 2 or abs(lat % 30 - 30) < step / 2:  # latitude grid lines
            m = int(360 * math.cos(phi) / 1.6)
            for k in range(m):
                lam = math.radians(-180 + k * 360 / m + offset)
                buckets["grid"].append(f"M{f4(math.sin(lam))} {f4(math.cos(lam))}h.002")
        rid = doc.uid("rg")
        tv = max(-1.2, t)
        doc.defs.append(f'<clipPath id="{rid}f"><rect x="-1.3" y="{f4(tv)}" width="2.6" height="{f4(1.3 - tv)}"/></clipPath>')
        if see_through:
            doc.defs.append(f'<clipPath id="{rid}b"><rect x="-1.3" y="-1.3" width="2.6" height="{f4(tv + 1.3)}"/></clipPath>')
        inner = [f'<g id="{rid}"><animateTransform attributeName="transform" type="rotate" from="0" to="-360" dur="{period}s" repeatCount="indefinite"/>']
        for kind, cls in (("ocean", "oc"), ("grid", "gd"), ("land", "ld"), ("na", "na"), ("us", "us")):
            if buckets[kind]:
                inner.append(f'<path class="{cls}" d="{"".join(buckets[kind])}"/>')
        inner.append("</g>")
        tf = f'translate({f2(cx)} {f2(cy0)}) scale({f4(r)} {f4(b)})'
        front.append(f'<g transform="{tf}" clip-path="url(#{rid}f)">{"".join(inner)}</g>')
        if see_through:
            back.append(f'<g transform="{tf}" clip-path="url(#{rid}b)"><use href="#{rid}"/></g>')

    doc.css.append(
        ".oc,.gd,.ld,.na,.us,.bc{vector-effect:non-scaling-stroke;stroke-linecap:round;fill:none}"
        f".oc{{stroke:#3B5BA9;stroke-width:1.2;stroke-opacity:.38}}"
        f".gd{{stroke:{BLUE_HOT};stroke-width:1.15;stroke-opacity:.42}}"
        f".ld{{stroke:#DCE7FF;stroke-width:2.35;stroke-opacity:.92}}"
        f".na{{stroke:#6D9BFF;stroke-width:2.5}}"
        f".us{{stroke:{RED_HOT};stroke-width:2.6}}"
    )

    if beacon:
        lat = 38.9
        phi = math.radians(lat)
        r = R * math.cos(phi)
        b = r * math.sin(a)
        cy0 = cy - R * math.sin(phi) * math.cos(a)
        t = -math.tan(phi) * math.tan(a)
        lam = math.radians(face_lon + offset)
        px, py = math.sin(lam), math.cos(lam)
        bid = doc.uid("bc")
        doc.defs.append(f'<clipPath id="{bid}f"><rect x="-1.6" y="{f4(t)}" width="3.2" height="{f4(1.6 - t)}"/></clipPath>')
        label_w = FB.width("monoB", "WASHINGTON, D.C.", 11, 1.2) + 22
        doc.need("monoB", "WASHINGTON, D.C.")
        doc.need("mono", "HQ · 38.9°N 77.0°W")
        lbl = (
            f'<g transform="scale({f4(1 / r)} {f4(1 / b)})">'
            f'<path d="M0 0L14 -26H{f2(18 + label_w)}" stroke="{RED_SOFT}" stroke-width="1" stroke-opacity=".8"/>'
            f'<rect x="14" y="-58" width="{f2(label_w + 4)}" height="30" rx="6" fill="#0B0F1A" fill-opacity=".86" stroke="{RED}" stroke-opacity=".6"/>'
            + doc.text(25, -45, "WASHINGTON, D.C.", "monoB", 11, WHITE, ls=1.2)
            + doc.text(25, -33, "HQ · 38.9°N 77.0°W", "mono", 9, RED_SOFT, ls=0.6)
            + "</g>"
        )
        top.append(
            f'<g transform="translate({f2(cx)} {f2(cy0)}) scale({f4(r)} {f4(b)})" clip-path="url(#{bid}f)">'
            f'<g><animateTransform attributeName="transform" type="rotate" from="0" to="-360" dur="{period}s" repeatCount="indefinite"/>'
            f'<path class="bc" d="M{f4(px)} {f4(py)}h.002" stroke="{RED_HOT}" stroke-width="6" stroke-opacity=".9">'
            f'<animate attributeName="stroke-width" values="6;30" dur="2.2s" repeatCount="indefinite"/>'
            f'<animate attributeName="stroke-opacity" values=".55;0" dur="2.2s" repeatCount="indefinite"/></path>'
            f'<path class="bc" d="M{f4(px)} {f4(py)}h.002" stroke="#fff" stroke-width="5.5"/>'
            f'<g transform="translate({f4(px)} {f4(py)})"><g>'
            f'<animateTransform attributeName="transform" type="rotate" from="0" to="360" dur="{period}s" repeatCount="indefinite"/>'
            f"{lbl}</g></g></g></g>"
        )

    gid = doc.uid("gl")
    doc.defs.append(
        f'<radialGradient id="{gid}base" cx="38%" cy="32%" r="75%"><stop offset="0" stop-color="#0E1D3D"/>'
        f'<stop offset=".6" stop-color="#060C1C"/><stop offset="1" stop-color="#03060E"/></radialGradient>'
        f'<radialGradient id="{gid}atm" cx="50%" cy="50%" r="50%"><stop offset=".78" stop-color="{BLUE_HOT}" stop-opacity="0"/>'
        f'<stop offset=".855" stop-color="{BLUE_HOT}" stop-opacity=".30"/><stop offset=".9" stop-color="{BLUE}" stop-opacity=".12"/>'
        f'<stop offset="1" stop-color="{BLUE}" stop-opacity="0"/></radialGradient>'
        f'<radialGradient id="{gid}limb" cx="50%" cy="50%" r="50%"><stop offset=".72" stop-color="{INK}" stop-opacity="0"/>'
        f'<stop offset=".93" stop-color="{INK}" stop-opacity=".55"/><stop offset="1" stop-color="{INK}" stop-opacity=".85"/></radialGradient>'
        f'<radialGradient id="{gid}spec" cx="34%" cy="26%" r="40%"><stop offset="0" stop-color="#BFD4FF" stop-opacity=".16"/>'
        f'<stop offset="1" stop-color="#BFD4FF" stop-opacity="0"/></radialGradient>'
        f'<linearGradient id="{gid}rim" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="{RED}"/>'
        f'<stop offset=".45" stop-color="#FFFFFF" stop-opacity=".25"/><stop offset="1" stop-color="{BLUE_HOT}"/></linearGradient>'
    )
    Ra = R / 0.855
    sphere_under = (
        f'<circle cx="{f2(cx)}" cy="{f2(cy)}" r="{f2(Ra)}" fill="url(#{gid}atm)"/>'
    )
    sphere = f'<circle cx="{f2(cx)}" cy="{f2(cy)}" r="{f2(R)}" fill="url(#{gid}base)" fill-opacity="{".9" if see_through else "1"}"/>'
    overlay = (
        f'<circle cx="{f2(cx)}" cy="{f2(cy)}" r="{f2(R)}" fill="url(#{gid}limb)"/>'
        f'<circle cx="{f2(cx)}" cy="{f2(cy)}" r="{f2(R)}" fill="url(#{gid}spec)"/>'
        f'<circle cx="{f2(cx)}" cy="{f2(cy)}" r="{f2(R)}" stroke="url(#{gid}rim)" stroke-width="1.4" stroke-opacity=".9"/>'
    )
    back_layer = sphere_under + (f'<g opacity=".16">{"".join(back)}</g>' if back else "")
    return back_layer, sphere, "".join(front) + overlay, "".join(top)


def orbit(doc: Doc, cx, cy, a, b, tilt_deg, period, color, *, reverse=False, phase=0.0, dash="2 7",
          line_opacity=0.35, trail=True) -> tuple[str, str]:
    """A tilted orbit with a satellite; returns (behind_globe, in_front_of_globe)."""
    oid = doc.uid("ob")
    doc.defs.append(
        f'<clipPath id="{oid}f"><rect x="-1.4" y="0" width="2.8" height="1.4"/></clipPath>'
        f'<clipPath id="{oid}b"><rect x="-1.4" y="-1.4" width="2.8" height="1.4"/></clipPath>'
    )
    sgn = 1 if reverse else -1
    frm, to = phase, phase + sgn * 360
    sat = [f'<g id="{oid}"><animateTransform attributeName="transform" type="rotate" from="{f2(frm)}" to="{f2(to)}" dur="{period}s" repeatCount="indefinite"/>']
    if trail:
        # arc trail behind the head, fading out
        for j in range(5):
            a0 = math.radians(j * 4 * sgn)
            a1 = math.radians((j + 1) * 4 * sgn)
            sat.append(
                f'<path class="tr" d="M{f4(math.sin(a0))} {f4(math.cos(a0))}L{f4(math.sin(a1))} {f4(math.cos(a1))}" '
                f'stroke="{color}" stroke-width="{f2(3 - j * 0.5)}" stroke-opacity="{f2(0.75 - j * 0.14)}"/>'
            )
    sat.append(f'<path class="tr" d="M0 1h.002" stroke="{color}" stroke-width="12" stroke-opacity=".25"/>')
    sat.append('<path class="tr" d="M0 1h.002" stroke="#fff" stroke-width="5"/>')
    sat.append("</g>")
    doc.css.append(".tr{vector-effect:non-scaling-stroke;stroke-linecap:round;fill:none}")
    tf = f"translate({f2(cx)} {f2(cy)}) rotate({f2(tilt_deg)}) scale({f2(a)} {f2(b)})"
    ring = (f'<ellipse rx="1" ry="1" stroke="{color}" stroke-opacity="{line_opacity}" stroke-width="1" '
            f'stroke-dasharray="{dash}" style="vector-effect:non-scaling-stroke"/>')
    behind = f'<g transform="{tf}" clip-path="url(#{oid}b)">{ring}{"".join(sat)}</g>'
    infront = f'<g transform="{tf}" clip-path="url(#{oid}f)">{ring}<use href="#{oid}"/></g>'
    return behind, infront


# ─────────────────────────────────────────────────────────────── shared pieces
def card_frame(doc: Doc, w, h, rx=28, *, border=True, bg=INK) -> tuple[str, str]:
    """Rounded card. Returns (open, close).

    `open` paints the background and opens a clip group for the *backdrop only* (aurora, grid,
    stars); callers must end it with BACKDROP_END before adding animated content. Clipping a
    whole card that contains SMIL animation makes Chrome's CPU rasterizer drop 256px tiles.
    """
    cid = doc.uid("card")
    doc.defs.append(
        f'<clipPath id="{cid}"><rect x="1" y="1" width="{w - 2}" height="{h - 2}" rx="{rx}"/></clipPath>'
        f'<linearGradient id="{cid}e" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{RED}" stop-opacity=".9"/>'
        f'<stop offset=".35" stop-color="#FFFFFF" stop-opacity=".10"/><stop offset=".65" stop-color="#FFFFFF" stop-opacity=".10"/>'
        f'<stop offset="1" stop-color="{BLUE_HOT}" stop-opacity=".95"/></linearGradient>'
    )
    op = f'<rect x="1" y="1" width="{w - 2}" height="{h - 2}" rx="{rx}" fill="{bg}"/><g clip-path="url(#{cid})">'
    cl = ""
    if border:
        cl += f'<rect x="1" y="1" width="{w - 2}" height="{h - 2}" rx="{rx}" stroke="url(#{cid}e)" stroke-width="1.5"/>'
    return op, cl


BACKDROP_END = "</g>"


def aurora(doc: Doc, w, h, blobs) -> str:
    """Slow drifting light fields made from radial gradients (no blur filters)."""
    out = []
    for i, (x, y, r, color, op, dx, dy, dur) in enumerate(blobs):
        gid = doc.uid("au")
        doc.defs.append(
            f'<radialGradient id="{gid}"><stop offset="0" stop-color="{color}" stop-opacity="{op}"/>'
            f'<stop offset=".45" stop-color="{color}" stop-opacity="{f2(op * 0.35)}"/>'
            f'<stop offset="1" stop-color="{color}" stop-opacity="0"/></radialGradient>'
        )
        kf = doc.uid("drift")
        doc.css.append(f"@keyframes {kf}{{0%,100%{{transform:translate(0,0)}}50%{{transform:translate({dx}px,{dy}px)}}}}"
                       f".{kf}{{animation:{kf} {dur}s ease-in-out infinite}}")
        out.append(f'<g class="{kf}"><circle cx="{x}" cy="{y}" r="{r}" fill="url(#{gid})"/></g>')
    return "".join(out)


def starfield(doc: Doc, w, h, n=90, seed=7, avoid=None) -> str:
    """Twinkling stars. Add *after* BACKDROP_END: CSS-animated groups inside the card clip
    make Chrome's CPU rasterizer leave visible 256px tile seams."""
    rnd = random.Random(seed)
    groups = defaultdict(list)
    for _ in range(n):
        x, y = rnd.uniform(0, w), rnd.uniform(0, h)
        if min(x, w - x) < 24 or min(y, h - y) < 24 or (avoid and avoid(x, y)):
            continue
        rr = rnd.choice([0.5, 0.7, 0.9, 1.2])
        groups[rnd.randrange(3)].append(f'<circle cx="{f2(x)}" cy="{f2(y)}" r="{rr}"/>')
    doc.css.append("@keyframes tw{0%,100%{opacity:.15}50%{opacity:.9}}"
                   ".tw0{animation:tw 4.2s ease-in-out infinite}.tw1{animation:tw 5.8s ease-in-out 1.3s infinite}"
                   ".tw2{animation:tw 7.1s ease-in-out 2.6s infinite}")
    return "".join(f'<g class="tw{k}" fill="#CBD5E1">{"".join(v)}</g>' for k, v in groups.items())


def grid_bg(doc: Doc, w, h, size=40, opacity=0.05, color=ICE) -> str:
    pid = doc.uid("grid")
    doc.defs.append(f'<pattern id="{pid}" width="{size}" height="{size}" patternUnits="userSpaceOnUse">'
                    f'<path d="M{size} 0H0V{size}" stroke="{color}" stroke-opacity="{opacity}"/></pattern>')
    return f'<rect width="{w}" height="{h}" fill="url(#{pid})"/>'


def marquee(doc: Doc, x, y, w, items, *, font="mono", size=13, gap=34, color=MUTED, sep="◆",
            sep_colors=(RED, WHITE, BLUE_HOT), speed=42, ls=1.4, h=40) -> str:
    """Seamless horizontally scrolling ticker (two copies of the same strip)."""
    parts, cx = [], 0.0
    for i, it in enumerate(items):
        hl = it.startswith("!")
        s = it.lstrip("!")
        f = "monoB" if hl else font
        parts.append(doc.text(cx, 0, s, f, size, WHITE if hl else color, ls=ls))
        cx += FB.width(f, s, size, ls) + gap / 2
        parts.append(doc.text(cx, 0, sep, "mono", size * 0.8, sep_colors[i % len(sep_colors)]))
        cx += FB.width("mono", sep, size * 0.8) + gap / 2
    strip = "".join(parts)
    W = cx
    dur = W / speed
    kf = doc.uid("mq")
    doc.css.append(f"@keyframes {kf}{{from{{transform:translateX(0)}}to{{transform:translateX(-{f2(W)}px)}}}}"
                   f".{kf}{{animation:{kf} {f2(dur)}s linear infinite}}")
    cid = doc.uid("mqc")
    doc.defs.append(f'<clipPath id="{cid}"><rect x="{x}" y="{y}" width="{w}" height="{h}"/></clipPath>')
    fid = doc.uid("mqf")
    doc.defs.append(f'<linearGradient id="{fid}" x1="0" x2="1"><stop offset="0" stop-color="{INK}"/>'
                    f'<stop offset=".06" stop-color="{INK}" stop-opacity="0"/><stop offset=".94" stop-color="{INK}" stop-opacity="0"/>'
                    f'<stop offset="1" stop-color="{INK}"/></linearGradient>')
    reps = int(w // W) + 2
    body = "".join(f'<g transform="translate({f2(i * W)} 0)">{strip}</g>' for i in range(reps))
    return (f'<g clip-path="url(#{cid})"><g transform="translate({x} {y + h / 2 + size * 0.36})">'
            f'<g class="{kf}">{body}</g></g><rect x="{x}" y="{y}" width="{w}" height="{h}" fill="url(#{fid})"/></g>')


def typewriter(doc: Doc, x, y, lines, *, font="mono", size=18, color=WHITE, cps=16, hold=2.0,
               erase_cps=45, gap=0.35, cursor_color=RED_HOT, prefix_w=0.0) -> str:
    """Cycles through lines with a typed/erased effect using SMIL-animated clip widths."""
    cw = FB.advance(font, "M") * size
    timeline = []
    t = 0.0
    for s in lines:
        n = len(s)
        ev = [(t, 0)]
        for k in range(1, n + 1):
            ev.append((t + k / cps, k))
        t_end_type = t + n / cps
        t_hold = t_end_type + hold
        steps = max(4, int(n / 4))
        for k in range(1, steps + 1):
            ev.append((t_hold + k * (n / erase_cps) / steps, round(n * (1 - k / steps))))
        t = t_hold + n / erase_cps + gap
        timeline.append((s, ev))
    T = t
    out = []
    cursor_ev = []
    for s, ev in timeline:
        cid = doc.uid("tw")
        vals = ["0"] + [f2(k * cw) for _, k in ev] + ["0"]
        kts = ["0"] + [f4(tt / T) for tt, _ in ev] + [f4(min(1.0, (ev[-1][0] + 0.01) / T))]
        # dedupe monotonic keyTimes
        kv = []
        for kt, v in zip(kts, vals):
            if kv and kv[-1][0] == kt:
                kv[-1] = (kt, v)
            else:
                kv.append((kt, v))
        doc.defs.append(
            f'<clipPath id="{cid}"><rect x="{f2(x)}" y="{f2(y - size)}" height="{f2(size * 1.4)}" width="0">'
            f'<animate attributeName="width" calcMode="discrete" values="{";".join(v for _, v in kv)}" '
            f'keyTimes="{";".join(k for k, _ in kv)}" dur="{f2(T)}s" repeatCount="indefinite"/></rect></clipPath>'
        )
        out.append(f'<g clip-path="url(#{cid})">{doc.text(x, y, s, font, size, color)}</g>')
        cursor_ev.extend(ev)
    cursor_ev.sort()
    kv = [("0", f2(x))]
    for tt, k in cursor_ev:
        kt = f4(tt / T)
        val = f2(x + k * cw + 2)
        if kv[-1][0] == kt:
            kv[-1] = (kt, val)
        else:
            kv.append((kt, val))
    doc.css.append("@keyframes blink{0%,49%{opacity:1}50%,100%{opacity:0}}.blink{animation:blink 1s steps(1) infinite}")
    out.append(
        f'<rect class="blink" y="{f2(y - size * 0.82)}" width="{f2(cw * 0.55)}" height="{f2(size * 1.0)}" fill="{cursor_color}" x="{f2(x)}">'
        f'<animate attributeName="x" calcMode="discrete" values="{";".join(v for _, v in kv)}" '
        f'keyTimes="{";".join(k for k, _ in kv)}" dur="{f2(T)}s" repeatCount="indefinite"/></rect>'
    )
    return "".join(out)


def pill(doc: Doc, x, y, label, *, font="monoB", size=12, fg=WHITE, stroke=LINE, fill="#0A0F1B",
         dot=None, ls=1.3, h=32, pad=16, icon=None) -> tuple[str, float]:
    tw = FB.width(font, label, size, ls)
    extra = 16 if dot or icon else 0
    w = tw + pad * 2 + extra
    s = [f'<rect x="{f2(x)}" y="{f2(y)}" width="{f2(w)}" height="{h}" rx="{h / 2}" fill="{fill}" stroke="{stroke}"/>']
    tx = x + pad
    if dot:
        s.append(f'<circle cx="{f2(x + pad + 4)}" cy="{f2(y + h / 2)}" r="4" fill="{dot}"/>')
        tx += extra
    if icon:
        s.append(icon(x + pad + 5, y + h / 2))
        tx += extra
    s.append(doc.text(tx, y + h / 2 + size * 0.36, label, font, size, fg, ls=ls))
    return "".join(s), w


def star_path(cx, cy, r, inner=0.42, points=5, rot=-90) -> str:
    pts = []
    for i in range(points * 2):
        rr = r if i % 2 == 0 else r * inner
        ang = math.radians(rot + i * 180 / points)
        pts.append(f"{f2(cx + rr * math.cos(ang))} {f2(cy + rr * math.sin(ang))}")
    return "M" + "L".join(pts) + "Z"


def decode_word(doc: Doc, x, y, word, *, font="display", size=86, fill=WHITE, T=12.0, t0=0.15,
                per=0.075, settle0=0.62, pool="ZEN01AI#/<>", seed=3, ghost=True) -> tuple[str, float]:
    """Big type that 'decrypts' in on each loop: scramble glyphs, then the true letter locks."""
    rnd = random.Random(seed + len(word))
    xs, cx = [], x
    for ch in word:
        adv = FB.advance(font, ch) * size
        xs.append((ch, cx + adv / 2))
        cx += adv
    total_w = cx - x
    doc.need(font, pool)
    out, ghosts = [], []
    slot = 0.055
    for i, (ch, mx) in enumerate(xs):
        if ch == " ":
            continue
        settle = settle0 + i * per
        s0 = t0 + i * 0.025
        nslots = int((settle - s0) / slot)
        glyphs = rnd.sample(pool, 3)
        for j, g in enumerate(glyphs):
            vals, kts = ["0"], ["0"]
            for s in range(nslots):
                kts.append(f4((s0 + s * slot) / T))
                vals.append("1" if s % 3 == j else "0")
            kts.append(f4(settle / T))
            vals.append("0")
            out.append(
                f'<text x="{f2(mx)}" y="{f2(y)}" class="f-{font}" font-size="{f2(size)}" fill="{RED_SOFT if j == 1 else fill}" text-anchor="middle" opacity="0">{esc(g)}'
                f'<animate attributeName="opacity" calcMode="discrete" values="{";".join(vals)}" keyTimes="{";".join(kts)}" dur="{T}s" repeatCount="indefinite"/></text>'
            )
        out.append(
            f'<text x="{f2(mx)}" y="{f2(y)}" class="f-{font}" font-size="{f2(size)}" fill="{fill}" text-anchor="middle">{esc(ch)}'
            f'<animate attributeName="opacity" calcMode="discrete" values="0;1" keyTimes="0;{f4(settle / T)}" dur="{T}s" repeatCount="indefinite"/></text>'
        )
        if ghost:
            ghosts.append((ch, mx))
        doc.need(font, ch)
    g = ""
    if ghost:
        for dx, col in ((-4, RED_HOT), (4, BLUE_HOT)):
            g += (f'<g opacity="0" fill="{col}">'
                  + "".join(f'<text x="{f2(mx + dx)}" y="{f2(y)}" class="f-{font}" font-size="{f2(size)}" text-anchor="middle">{esc(ch)}</text>' for ch, mx in ghosts)
                  + f'<animate attributeName="opacity" values="0;0;.75;0;.5;0;0" keyTimes="0;{f4(1.1 / T)};{f4(1.16 / T)};{f4(1.24 / T)};{f4(1.3 / T)};{f4(1.4 / T)};1" dur="{T}s" repeatCount="indefinite"/></g>')
    return g + "".join(out), total_w


# ═══════════════════════════════════════════════════════════════ HERO
def build_hero():
    W, H = 1200, 640
    doc = Doc(W, H, "Alex Leschik — Founder, ZEN AI Co. · Creator, Arsenal",
              "Animated hero: the ZEN AI Co. ZZ monogram is laser-traced beside Alex Leschik's name, "
              "next to a rotating dotted Earth that highlights the United States, Canada and Mexico, "
              "with a Washington, D.C. beacon and orbiting satellites.")
    op, cl = card_frame(doc, W, H, 30)
    doc.add(op)
    doc.add(aurora(doc, W, H, [
        (930, 300, 520, BLUE, 0.30, -40, 20, 22),
        (120, 660, 520, RED, 0.26, 50, -30, 19),
        (520, 40, 380, "#7C3AED", 0.12, 40, 30, 27),
    ]))
    doc.add(grid_bg(doc, W, H, 40, 0.045))
    gcx, gcy, R = 898, 314, 214

    # vignette for legibility on the left
    vid = doc.uid("vg")
    doc.defs.append(f'<linearGradient id="{vid}" x1="0" x2="1"><stop offset="0" stop-color="{INK}" stop-opacity=".85"/>'
                    f'<stop offset=".45" stop-color="{INK}" stop-opacity=".35"/><stop offset=".62" stop-color="{INK}" stop-opacity="0"/></linearGradient>')
    doc.add(f'<rect width="{W}" height="{H}" fill="url(#{vid})"/>', BACKDROP_END)
    doc.add(starfield(doc, W, H, 150, avoid=lambda x, y: math.dist((x, y), (gcx, gcy)) < R + 8))

    # ── globe + orbits
    back, sphere, front, beacon = globe(doc, gcx, gcy, R, tilt=22, period=80, step=3.0)
    o1b, o1f = orbit(doc, gcx, gcy, R * 1.30, R * 0.26, -16, 16, RED_HOT, phase=40)
    o2b, o2f = orbit(doc, gcx, gcy, R * 1.17, R * 0.40, 21, 23, ICE, reverse=True, phase=200, dash="1 5")
    # scan plane sweeping the globe
    sid = doc.uid("scan")
    doc.defs.append(f'<linearGradient id="{sid}" x1="0" x2="1"><stop offset="0" stop-color="{RED_HOT}" stop-opacity="0"/>'
                    f'<stop offset=".5" stop-color="{RED_HOT}" stop-opacity=".9"/><stop offset="1" stop-color="{RED_HOT}" stop-opacity="0"/></linearGradient>')
    lats = [70, 50, 30, 10, -10, -30, -50, -62, -50, -30, -10, 10, 30, 50, 70]
    a = math.radians(22)
    cys = ";".join(f2(gcy - R * math.sin(math.radians(l)) * math.cos(a)) for l in lats)
    rxs = ";".join(f2(R * math.cos(math.radians(l))) for l in lats)
    rys = ";".join(f2(R * math.cos(math.radians(l)) * math.sin(a)) for l in lats)
    scan = (f'<ellipse cx="{gcx}" stroke="url(#{sid})" stroke-width="1.2" stroke-opacity=".7" fill="{RED_HOT}" fill-opacity=".022">'
            f'<animate attributeName="cy" values="{cys}" dur="14s" repeatCount="indefinite"/>'
            f'<animate attributeName="rx" values="{rxs}" dur="14s" repeatCount="indefinite"/>'
            f'<animate attributeName="ry" values="{rys}" dur="14s" repeatCount="indefinite"/></ellipse>')
    doc.add(o1b, o2b, back, sphere, front, scan, o1f, o2f, beacon)

    # HUD brackets around the globe
    hb = []
    for sx, sy in ((-1, 1), (1, 1)):
        x0, y0 = gcx + sx * (R + 26), gcy + sy * (R + 26)
        hb.append(f'<path d="M{f2(x0)} {f2(y0 - sy * 18)}V{f2(y0)}H{f2(x0 - sx * 18)}" stroke="{ICE}" stroke-opacity=".45" stroke-width="1.5"/>')
    doc.add("".join(hb))
    doc.add(doc.text(gcx - R - 2, gcy + R + 30, "ZEN ORBITAL · LIVE", "mono", 11, DIM, ls=1.6))
    doc.add(doc.text(gcx + R + 2, gcy + R + 30, "NORTH AMERICA → WORLD", "mono", 11, ICE, anchor="end", ls=1.6, opacity=0.75))

    # ── header row
    doc.add(laser_logo(doc, 56, 40, 40, dur=9, draw_frac=0.38))
    doc.add(doc.text(112, 69, "ZEN AI CO.", "display", 19, WHITE, ls=1.5))
    wz = FB.width("display", "ZEN AI CO.", 19, 1.5)
    doc.add(f'<rect x="{f2(112 + wz + 14)}" y="54" width="1" height="18" fill="{DIM}"/>')
    doc.add(doc.text(112 + wz + 29, 68, "ARSENAL · AI PIONEER · WASHINGTON, D.C.", "mono", 12, MUTED, ls=1.8))
    stat_txt = "ALL SYSTEMS OPERATIONAL"
    sw_ = FB.width("monoB", stat_txt, 11, 1.6)
    sx0 = W - 56 - sw_ - 44
    doc.css.append("@keyframes pulseDot{0%,100%{opacity:1}50%{opacity:.25}}.pd{animation:pulseDot 1.6s ease-in-out infinite}")
    doc.add(f'<rect x="{f2(sx0)}" y="44" width="{f2(sw_ + 44)}" height="32" rx="16" fill="#07120C" stroke="{GREEN}" stroke-opacity=".35"/>'
            f'<circle class="pd" cx="{f2(sx0 + 18)}" cy="60" r="4.5" fill="{GREEN}"/>'
            + doc.text(sx0 + 30, 64, stat_txt, "monoB", 11, "#BBF7D0", ls=1.6))

    # ── identity block
    doc.add(f'<rect x="60" y="148" width="26" height="3" rx="1.5" fill="{RED}"/>')
    doc.add(doc.text(98, 154, "FOUNDER, ZEN AI CO.  /  CREATOR, ARSENAL", "monoB", 13, RED_SOFT, ls=2.2))
    gid = doc.uid("ng")
    doc.defs.append(
        f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" x1="56" y1="0" x2="620" y2="0" spreadMethod="reflect">'
        f'<stop offset="0" stop-color="{RED_HOT}"/><stop offset=".45" stop-color="#FFFFFF"/><stop offset="1" stop-color="{BLUE_HOT}"/>'
        f'<animateTransform attributeName="gradientTransform" type="translate" values="0 0;564 0;0 0" dur="10s" repeatCount="indefinite"/></linearGradient>'
    )
    w1, _ = decode_word(doc, 56, 250, "ALEX", size=90, fill=WHITE, seed=1)
    w2, _ = decode_word(doc, 56, 348, "LESCHIK", size=90, fill=f"url(#{gid})", settle0=0.95, seed=2)
    doc.add(w1, w2)
    doc.add(doc.text(60, 400, "Building the operating system for the AI era —", "sans", 22, "#E2E8F0"))
    doc.add(doc.text(60, 430, "and the first generation that will run it.", "sans", 22, MUTED))

    # typed capabilities
    doc.add(doc.text(60, 484, "›", "monoB", 20, RED_HOT))
    doc.add(typewriter(doc, 84, 484, [
        "agents that execute, not just chat.",
        "apps and automations people own.",
        "AI literacy that ends in deployment.",
        "model routing across every frontier lab.",
        "systems that prove what changed.",
    ], size=18, color=WHITE))

    # credibility chips
    star = lambda cx, cy: f'<path d="{star_path(cx, cy, 6.5)}" fill="{GOLD}"/>'
    c1, w1c = pill(doc, 60, 516, "1ST YOUTH AI LITERACY PROGRAM IN U.S. HISTORY", size=11, icon=star, stroke="#3A2E12", fill="#0F0C05", fg="#FDE68A", ls=1.2)
    c2, _ = pill(doc, 60 + w1c + 10, 516, "PRESIDENTIAL AI CHALLENGE · 2026", size=11, dot=RED_HOT, stroke="#3B1219", fill="#12070A", fg=RED_SOFT, ls=1.2)
    doc.add(c1, c2)

    # ── ticker
    doc.add(f'<rect x="40" y="578" width="{W - 80}" height="40" rx="12" fill="#060A13" stroke="{LINE}"/>')
    doc.add(marquee(doc, 41, 578, W - 82, [
        "!AI PIONEER PROGRAM", "YEAR 4 · 2027", "BOYS & GIRLS CLUBS OF GREATER WASHINGTON",
        "!33,000+ YOUTH", "42 STATES", "AGES 11–18", "!ARSENAL.WORLD", "AGENTS · APPS · AUTOMATIONS",
        "!ZEN AI ARENA", "!QUBIT.EARTH", "OPENAI · ANTHROPIC · GOOGLE · META · MISTRAL · COHERE · HUGGING FACE",
        "!ZENAI.WORLD", "CAPABILITY > CONTENT", "OUTCOMES > OUTPUTS", "OWNERSHIP > DEPENDENCY",
    ], size=12, speed=40))
    doc.add(cl)
    doc.save("hero.svg")


def runs(doc: Doc, x, y, parts, font="mono", size=15, anchor="start", ls=0.0, attrs="") -> str:
    """One <text> with differently coloured/weighted runs: parts = [(text, fill, font|None)]."""
    spans = []
    for p in parts:
        t, fill = p[0], p[1]
        fk = p[2] if len(p) > 2 and p[2] else font
        doc.need(fk, t)
        cls = f' class="f-{fk}"' if fk != font else ""
        spans.append(f'<tspan fill="{fill}"{cls}>{esc(t)}</tspan>')
    doc.need(font, "")
    a = f' text-anchor="{anchor}"' if anchor != "start" else ""
    l = f' letter-spacing="{f2(ls)}"' if ls else ""
    return f'<text x="{f2(x)}" y="{f2(y)}" class="f-{font}" font-size="{f2(size)}"{a}{l} {attrs} xml:space="preserve">{"".join(spans)}</text>'


def glint_line(doc: Doc, x, y, w, *, dur=5.0, delay=0.0, h=1.5) -> str:
    """Hairline in brand gradient with a travelling highlight."""
    gid = doc.uid("gl")
    doc.defs.append(
        f'<linearGradient id="{gid}" x1="0" x2="1"><stop offset="0" stop-color="{RED}"/>'
        f'<stop offset=".5" stop-color="#FFFFFF" stop-opacity=".35"/><stop offset="1" stop-color="{BLUE_HOT}"/></linearGradient>'
        f'<linearGradient id="{gid}h" x1="0" x2="1"><stop offset="0" stop-color="#fff" stop-opacity="0"/>'
        f'<stop offset=".5" stop-color="#fff"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>'
    )
    return (f'<rect x="{f2(x)}" y="{f2(y)}" width="{f2(w)}" height="{h}" fill="url(#{gid})" opacity=".55"/>'
            f'<rect x="{f2(x)}" y="{f2(y - 1)}" width="140" height="{h + 2}" fill="url(#{gid}h)" opacity="0">'
            f'<animate attributeName="x" values="{f2(x - 140)};{f2(x + w)}" dur="{dur}s" begin="{delay}s" repeatCount="indefinite"/>'
            f'<animate attributeName="opacity" values="0;1;1;0" keyTimes="0;.1;.9;1" dur="{dur}s" begin="{delay}s" repeatCount="indefinite"/></rect>')


def ring_text(doc: Doc, cx, cy, r, text, *, font="monoB", size=13, fill=WHITE, ls=3.0, dur=40, reverse=False,
              opacity=1.0) -> str:
    """Text set on a circle, slowly rotating (seal / stamp)."""
    pid = doc.uid("rt")
    doc.defs.append(f'<path id="{pid}" d="M{f2(cx - r)} {f2(cy)}a{f2(r)} {f2(r)} 0 1 1 {f2(2 * r)} 0a{f2(r)} {f2(r)} 0 1 1 {f2(-2 * r)} 0"/>')
    doc.need(font, text)
    circ = 2 * math.pi * r
    ls = (circ - FB.width(font, text, size)) / len(text)
    to = -360 if reverse else 360
    return (f'<g opacity="{f2(opacity)}"><animateTransform attributeName="transform" type="rotate" from="0 {f2(cx)} {f2(cy)}" to="{to} {f2(cx)} {f2(cy)}" dur="{dur}s" repeatCount="indefinite"/>'
            f'<text class="f-{font}" font-size="{size}" fill="{fill}" letter-spacing="{f2(ls)}"><textPath href="#{pid}">{esc(text)}</textPath></text></g>')


def guilloche(cx, cy, r0, r1, rings=9, waves=28, amp=5.0, pts=300, color=GOLD, opacity=0.22, width=0.8) -> str:
    """Security-print rosette: phase-shifted wavy rings."""
    out = []
    for i in range(rings):
        base = r0 + (r1 - r0) * i / max(rings - 1, 1)
        ph = i * math.pi / rings
        d = []
        for k in range(pts + 1):
            t = 2 * math.pi * k / pts
            rr = base + amp * math.cos(waves * t + ph) + amp * 0.5 * math.sin((waves / 2) * t - ph)
            d.append(f"{f2(cx + rr * math.cos(t))} {f2(cy + rr * math.sin(t))}")
        out.append(f'<path d="M{"L".join(d)}Z" stroke="{color}" stroke-opacity="{opacity}" stroke-width="{width}"/>')
    return "".join(out)


# ═══════════════════════════════════════════════════════════════ SECTION HEADERS
SECTIONS = [
    ("01", "THE SIGNAL", "WHAT'S TRUE RIGHT NOW"),
    ("02", "WHOAMI", "FOUNDER · OPERATOR · SYSTEMS ARCHITECT"),
    ("03", "THE ZEN ECOSYSTEM", "ONE OPERATING SYSTEM FOR THE AI ERA"),
    ("04", "AI PIONEER PROGRAM", "FIRST IN U.S. HISTORY · YEAR 4 IN 2027"),
    ("05", "RECOGNITION", "2026 PRESIDENTIAL AI CHALLENGE"),
    ("06", "INSIDE THE SYSTEM", "CREATE · OPERATE · LEARN · OWN · INSPECT"),
    ("07", "WORK WITH ME", "INVEST · PARTNER · CONTRACT · HIRE"),
    ("08", "GITHUB SIGNAL", "BUILT IN PUBLIC"),
    ("09", "CONNECT", "ONE ECOSYSTEM · EVERY CHANNEL"),
]


def build_sections():
    for i, (num, title, sub) in enumerate(SECTIONS):
        W, H = 1200, 100
        doc = Doc(W, H, f"{num} — {title.title()}", f"Section header: {title.title()} — {sub.title()}")
        op, cl = card_frame(doc, W, H, 22, border=False, bg="#05080F")
        doc.add(op)
        doc.add(grid_bg(doc, W, H, 20, 0.05))
        accent = (RED_HOT, WHITE, BLUE_HOT)[i % 3]
        doc.add(aurora(doc, W, H, [(80, 50, 260, (RED, "#FFFFFF", BLUE)[i % 3], 0.16, 30, 0, 11 + i)]), BACKDROP_END)
        # index block
        doc.add(f'<rect x="32" y="26" width="64" height="48" rx="12" fill="#0B1120" stroke="{accent}" stroke-opacity=".55"/>')
        doc.add(doc.text(64, 60, num, "display", 22, accent, anchor="middle"))
        doc.add('<rect x="32" y="26" width="64" height="48" rx="12" fill="none" stroke="#fff" stroke-width="1.5" stroke-dasharray="30 194" stroke-opacity=".8">'
                '<animate attributeName="stroke-dashoffset" values="0;-224" dur="4s" repeatCount="indefinite"/></rect>')
        doc.add(doc.text(120, 62, title, "display", 30, WHITE, ls=1.5))
        tw = FB.width("display", title, 30, 1.5)
        doc.add(f'<rect class="blink" x="{f2(120 + tw + 10)}" y="38" width="12" height="26" fill="{accent}"/>')
        doc.css.append("@keyframes blink{0%,49%{opacity:1}50%,100%{opacity:0}}.blink{animation:blink 1.1s steps(1) infinite}")
        doc.add(doc.text(W - 36, 60, sub, "mono", 13, MUTED, anchor="end", ls=2.2))
        doc.add(glint_line(doc, 32, 94, W - 64, dur=6, delay=i * 0.4))
        doc.add(cl)
        doc.save(f"section-{num}.svg")


# ═══════════════════════════════════════════════════════════════ SIGNAL / ODOMETERS
def odometer(doc: Doc, x, y, text, *, size=58, font="display", fill=WHITE, T=11.0, start=0.25,
             stagger=0.12, roll=1.8) -> tuple[str, float]:
    lh = size * 1.12
    cid = doc.uid("odc")
    parts = []
    cx = x
    digit_w = max(FB.advance(font, d) for d in "0123456789") * size
    col = 0
    for ch in text:
        if ch.isdigit():
            target = int(ch)
            seq = [str(k % 10) for k in range(10 + target + 1)]
            final = (len(seq) - 1) * lh
            kf = doc.uid("od")
            s0 = (start + col * stagger) / T * 100
            s1 = (start + col * stagger + roll) / T * 100
            doc.css.append(
                f"@keyframes {kf}{{0%,{f2(s0)}%{{transform:translateY(0);animation-timing-function:cubic-bezier(.12,.9,.2,1)}}"
                f"{f2(s1)}%,94%{{transform:translateY(-{f2(final)}px)}}100%{{transform:translateY(-{f2(final)}px)}}}}"
                f".{kf}{{animation:{kf} {T}s infinite}}"
            )
            doc.need(font, "0123456789")
            tsp = "".join(f'<tspan x="{f2(cx + digit_w / 2)}" y="{f2(y + k * lh)}">{d}</tspan>' for k, d in enumerate(seq))
            parts.append(f'<g class="{kf}"><text class="f-{font}" font-size="{size}" fill="{fill}" text-anchor="middle">{tsp}</text></g>')
            cx += digit_w
            col += 1
        else:
            adv = FB.advance(font, ch) * size
            parts.append(doc.text(cx, y, ch, font, size, fill))
            cx += adv
    doc.defs.append(f'<clipPath id="{cid}"><rect x="{f2(x - 4)}" y="{f2(y - size * 0.92)}" width="{f2(cx - x + 8)}" height="{f2(size * 1.1)}"/></clipPath>')
    # fade in/out mask for the whole counter each loop
    fk = doc.uid("odf")
    doc.css.append(f"@keyframes {fk}{{0%{{opacity:0}}3%,93%{{opacity:1}}100%{{opacity:0}}}}.{fk}{{animation:{fk} {T}s infinite}}")
    return f'<g clip-path="url(#{cid})"><g class="{fk}">{"".join(parts)}</g></g>', cx - x


def build_signal():
    W, H = 1200, 320
    doc = Doc(W, H, "The signal — ZEN AI Co. by the numbers",
              "Animated counters: first youth AI literacy program in U.S. history; 33,000+ youth reached; "
              "42 states; Year 4 with Boys & Girls Clubs of Greater Washington in 2027.")
    op, cl = card_frame(doc, W, H, 26)
    doc.add(op)
    doc.add(aurora(doc, W, H, [(150, 300, 360, RED, 0.18, 40, -10, 17), (1050, 20, 380, BLUE, 0.22, -40, 20, 21)]))
    doc.add(grid_bg(doc, W, H, 24, 0.04), BACKDROP_END)
    cw = (W - 80) / 4
    T = 11.0
    for i in range(4):
        x0 = 40 + i * cw
        doc.add(f'<rect x="{f2(x0 + 8)}" y="28" width="{f2(cw - 16)}" height="{H - 56}" rx="18" fill="#070C17" fill-opacity=".85" stroke="{LINE}"/>')
        # corner ticks
        doc.add(f'<path d="M{f2(x0 + 22)} 44h14M{f2(x0 + 22)} 44v14" stroke="{(RED_HOT, WHITE, BLUE_HOT, GOLD)[i]}" stroke-width="2"/>')
    # 1 — FIRST
    x0 = 40
    doc.add(doc.text(x0 + 40, 92, "HISTORIC", "monoB", 12, GOLD, ls=2.4))
    doc.add(doc.text(x0 + 38, 168, "1ST", "display", 66, WHITE))
    sx = x0 + cw - 62
    doc.add(ring_text(doc, sx, 84, 27, "FIRST · IN · U.S. · HISTORY · ", font="monoB", size=7.5, fill=GOLD, dur=22, opacity=.9))
    doc.add(f'<path d="{star_path(sx, 84, 12)}" fill="{GOLD}"><animate attributeName="opacity" values="1;.55;1" dur="3s" repeatCount="indefinite"/></path>')
    doc.add(doc.text(x0 + 40, 222, "Youth AI literacy program", "sansB", 17, WHITE))
    doc.add(doc.text(x0 + 40, 246, "in United States history.", "sans", 17, MUTED))
    doc.add(glint_line(doc, x0 + 40, 272, cw - 80, dur=4.5, delay=0.2, h=1))
    # 2 — 33,000+
    x0 = 40 + cw
    doc.add(doc.text(x0 + 40, 92, "REACH", "monoB", 12, RED_SOFT, ls=2.4))
    od, w = odometer(doc, x0 + 38, 168, "33", size=54, T=T, start=0.3)
    doc.add(od)
    doc.add(doc.text(x0 + 38 + w + 2, 168, "K", "display", 54, WHITE))
    doc.add(doc.text(x0 + 38 + w + 2 + FB.width("display", "K", 54), 168, "+", "display", 54, RED_HOT))
    doc.add(doc.text(x0 + 40, 222, "33,000+ youth reached", "sansB", 17, WHITE))
    doc.add(doc.text(x0 + 40, 246, "Ages 11–18 · and counting.", "sans", 17, MUTED))
    # growth bars
    bars = []
    for k in range(14):
        hgt = 3 + (k ** 1.5) * 0.46
        bx = x0 + 40 + k * 15.5
        kf = doc.uid("bar")
        s0 = (0.3 + k * 0.07) / T * 100
        doc.css.append(f"@keyframes {kf}{{0%,{f2(s0)}%{{transform:scaleY(0)}}{f2(s0 + 12)}%,94%{{transform:scaleY(1)}}100%{{transform:scaleY(0)}}}}"
                       f".{kf}{{transform-box:fill-box;transform-origin:50% 100%;animation:{kf} {T}s cubic-bezier(.2,.8,.2,1) infinite}}")
        bars.append(f'<rect class="{kf}" x="{f2(bx)}" y="{f2(284 - hgt)}" width="9" height="{f2(hgt)}" rx="2" fill="{RED_HOT if k == 13 else RED}" fill-opacity="{f2(0.25 + k * 0.05)}"/>')
    doc.add("".join(bars))
    # 3 — 42 states
    x0 = 40 + 2 * cw
    doc.add(doc.text(x0 + 40, 92, "FOOTPRINT", "monoB", 12, "#E2E8F0", ls=2.4))
    od, w = odometer(doc, x0 + 38, 168, "42", size=54, T=T, start=0.5)
    doc.add(od)
    doc.add(doc.text(x0 + 38 + w + 12, 168, "STATES", "displayM", 20, MUTED))
    doc.add(doc.text(x0 + 40, 222, "States active", "sansB", 17, WHITE))
    doc.add(doc.text(x0 + 40, 246, "Expanding across the U.S.", "sans", 17, MUTED))
    dots = []
    for k in range(50):
        dx = x0 + 44 + (k % 25) * 9.6
        dy = 262 + (k // 25) * 11
        lit = k < 42
        if lit:
            kf = doc.uid("st")
            s0 = (0.5 + k * 0.035) / T * 100
            doc.css.append(f"@keyframes {kf}{{0%,{f2(s0)}%{{opacity:.12}}{f2(s0 + 1.5)}%,94%{{opacity:1}}100%{{opacity:.12}}}}.{kf}{{animation:{kf} {T}s infinite}}")
            dots.append(f'<rect class="{kf}" x="{f2(dx)}" y="{f2(dy)}" width="6.4" height="6.4" rx="1.5" fill="{WHITE if k % 3 else BLUE_HOT}"/>')
        else:
            dots.append(f'<rect x="{f2(dx)}" y="{f2(dy)}" width="6.4" height="6.4" rx="1.5" fill="#1E293B"/>')
    doc.add("".join(dots))
    # 4 — Year 4
    x0 = 40 + 3 * cw
    doc.add(doc.text(x0 + 40, 92, "MOMENTUM", "monoB", 12, ICE, ls=2.4))
    doc.add(doc.text(x0 + 38, 168, "YEAR", "displayM", 22, MUTED))
    od, w = odometer(doc, x0 + 38 + FB.width("displayM", "YEAR", 22) + 12, 168, "4", size=54, fill=WHITE, T=T, start=0.7)
    doc.add(od)
    doc.add(doc.text(x0 + 40, 214, "Boys & Girls Clubs of", "sansB", 16, WHITE))
    doc.add(doc.text(x0 + 40, 236, "Greater Washington · 2027", "sans", 16, MUTED))
    segs = []
    yrs = ["'24", "'25", "'26", "'27"]
    for k in range(4):
        bx = x0 + 40 + k * 56
        col = BLUE_HOT if k < 3 else RED_HOT
        cls = ""
        if k == 3:
            doc.css.append("@keyframes nowp{0%,100%{opacity:1}50%{opacity:.35}}.nowp{animation:nowp 1.4s ease-in-out infinite}")
            cls = ' class="nowp"'
        segs.append(f'<rect{cls} x="{f2(bx)}" y="254" width="50" height="7" rx="3.5" fill="{col}"/>')
        segs.append(doc.text(bx + 25, 282, yrs[k], "monoB", 11, WHITE if k == 3 else DIM, anchor="middle", ls=1))
    doc.add("".join(segs))
    doc.add(cl)
    doc.save("signal.svg")


# ═══════════════════════════════════════════════════════════════ TERMINAL
def build_terminal():
    W, H = 1200, 540
    doc = Doc(W, H, "whoami — an animated terminal session",
              "A terminal types out who Alex Leschik is, the status of the ZEN ecosystem (Arsenal, ZEN AI Arena, "
              "Qubit, AI Pioneer), and a deploy of the mission, beside a live model-routing monitor.")
    op, cl = card_frame(doc, W, H, 22, bg="#060911")
    doc.add(op)
    doc.add(aurora(doc, W, H, [(300, 560, 420, RED, 0.12, 30, -20, 19), (1100, 60, 360, BLUE, 0.16, -30, 20, 23)]))
    # chrome
    doc.add(f'<rect width="{W}" height="52" fill="#0A0F1A"/><rect y="52" width="{W}" height="1" fill="{LINE}"/>')
    for k, c in enumerate(("#FF5F57", "#FEBC2E", "#28C840")):
        doc.add(f'<circle cx="{30 + k * 22}" cy="26" r="6.5" fill="{c}"/>')
    doc.add(doc.text(W / 2, 31, "alex@zen — ~/arsenal — zsh — 120×32", "mono", 13, DIM, anchor="middle", ls=0.6))
    doc.add(f'<rect x="{W - 150}" y="16" width="118" height="20" rx="10" fill="#0E1626" stroke="{LINE}"/>')
    doc.add(doc.text(W - 91, 30, "SESSION · LIVE", "monoB", 9.5, ICE, anchor="middle", ls=1.4), BACKDROP_END)

    size, lh = 15.5, 27.5
    cw = FB.advance("mono", "M") * size
    X0, Y0 = 36, 96
    PROMPT = [("alex", RED_HOT, "monoB"), ("@", DIM), ("zen", BLUE_HOT, "monoB"), (" ~/arsenal ", MUTED), ("❯ ", WHITE, "monoB")]
    ptxt = "".join(p[0] for p in PROMPT)
    pw = len(ptxt) * cw
    G, Mu, Wt = GREEN, MUTED, "#E2E8F0"
    lines = [
        ("cmd", "whoami"),
        ("out", [("Alex Leschik", WHITE, "monoB"), (" — founder, ZEN AI Co. · creator, Arsenal · Washington, D.C.", Wt)]),
        ("gap",),
        ("cmd", "zen status --ecosystem"),
        *[("out", [("  ● ", G), (host.ljust(17), WHITE, "monoB"), (what.ljust(32), Mu), ("ONLINE", G, "monoB")])
          for host, what in (("arsenal.world", "agent + app builder"), ("us.zenai.biz", "ZEN AI Arena · models + agents"),
                             ("qubit.earth", "business agent platform"), ("ai-pioneer", "youth AI literacy · year 4"))],
        ("gap",),
        ("cmd", 'zen deploy --mission "AI literacy for everyone"'),
        ("out", [("  ├─ ", DIM), ("route  ", ICE), ("openai · anthropic · google · meta · mistral · cohere", Wt)]),
        ("out", [("  ├─ ", DIM), ("build  ", ICE), ("agents → apps → automations → dashboards → proof", Wt)]),
        ("out", [("  ├─ ", DIM), ("own    ", ICE), ("files · vault · export · github / hugging face handoff", Wt)]),
        ("out", [("  └─ ", DIM), ("shipped. ", G, "monoB"), ("capability > content · outcomes > outputs", Mu)]),
        ("prompt",),
    ]
    T = 24.0
    t = 0.6
    cps = 17
    cursor = []  # (time, x, y)
    body = []
    y = Y0
    for ln in lines:
        kind = ln[0]
        if kind == "gap":
            y += lh * 0.45
            continue
        if kind in ("cmd", "prompt"):
            appear = t
            vis = f'<animate attributeName="opacity" calcMode="discrete" values="0;1;0" keyTimes="0;{f4(appear / T)};{f4(0.975)}" dur="{T}s" repeatCount="indefinite"/>'
            body.append(f'<g opacity="0">{runs(doc, X0, y, PROMPT, "mono", size)}{vis}</g>')
            cursor.append((appear, X0 + pw, y))
            if kind == "cmd":
                cmd = ln[1]
                t += 0.45
                cid = doc.uid("tc")
                vals, kts = ["0"], ["0"]
                for k in range(1, len(cmd) + 1):
                    tk = t + k / cps
                    vals.append(f2(k * cw))
                    kts.append(f4(tk / T))
                    cursor.append((tk, X0 + pw + k * cw, y))
                vals.append("0")
                kts.append(f4(0.975))
                doc.defs.append(f'<clipPath id="{cid}"><rect x="{f2(X0 + pw)}" y="{f2(y - size)}" height="{f2(size * 1.5)}" width="0">'
                                f'<animate attributeName="width" calcMode="discrete" values="{";".join(vals)}" keyTimes="{";".join(kts)}" dur="{T}s" repeatCount="indefinite"/></rect></clipPath>')
                body.append(f'<g clip-path="url(#{cid})">{doc.text(X0 + pw, y, cmd, "mono", size, WHITE)}</g>')
                t += len(cmd) / cps + 0.35
            y += lh
        else:
            appear = t
            body.append(f'<g opacity="0">{runs(doc, X0, y, ln[1], "mono", size)}'
                        f'<animate attributeName="opacity" calcMode="discrete" values="0;1;0" keyTimes="0;{f4(appear / T)};{f4(0.975)}" dur="{T}s" repeatCount="indefinite"/></g>')
            t += 0.32
            y += lh
    doc.add("".join(body))
    # cursor
    cursor.sort()
    xs, ys, kts = [], [], []
    for tt, cx_, cy_ in cursor:
        kt = f4(tt / T)
        if kts and kts[-1] == kt:
            xs[-1], ys[-1] = f2(cx_ + 1), f2(cy_ - size * 0.8)
        else:
            kts.append(kt)
            xs.append(f2(cx_ + 1))
            ys.append(f2(cy_ - size * 0.8))
    kts = ["0"] + kts
    xs = [f2(X0 + pw + 1)] + xs
    ys = [f2(Y0 - size * 0.8)] + ys
    doc.css.append("@keyframes blink{0%,49%{opacity:1}50%,100%{opacity:0}}.blink{animation:blink 1s steps(1) infinite}")
    doc.add(f'<rect class="blink" width="{f2(cw * 0.6)}" height="{f2(size * 1.05)}" fill="{RED_HOT}" x="{xs[0]}" y="{ys[0]}">'
            f'<animate attributeName="x" calcMode="discrete" values="{";".join(xs)}" keyTimes="{";".join(kts)}" dur="{T}s" repeatCount="indefinite"/>'
            f'<animate attributeName="y" calcMode="discrete" values="{";".join(ys)}" keyTimes="{";".join(kts)}" dur="{T}s" repeatCount="indefinite"/></rect>')

    # ── right pane: model mesh monitor
    PX = 852
    doc.add(f'<rect x="{PX - 26}" y="53" width="1" height="{H - 53}" fill="{LINE}"/>')
    doc.add(f'<rect x="{PX - 25}" y="53" width="{W - PX + 25}" height="{H - 53}" fill="#070B14" fill-opacity=".7"/>')
    doc.add(doc.text(PX, 92, "zen route --watch", "monoB", 13, WHITE, ls=0.5))
    doc.add(doc.text(PX, 114, "MODEL MESH · COST-AWARE · FALLBACKS", "mono", 10, DIM, ls=1.4))
    provs = [("OpenAI", RED_HOT), ("Anthropic", WHITE), ("Google", BLUE_HOT), ("Meta", ICE), ("Mistral", RED_SOFT), ("Cohere", "#CBD5E1"), ("Hugging Face", GOLD)]
    rnd = random.Random(11)
    for k, (name, col) in enumerate(provs):
        yy = 150 + k * 44
        doc.add(doc.text(PX, yy, name, "mono", 13, "#CBD5E1"))
        doc.add(f'<rect x="{PX}" y="{yy + 9}" width="300" height="5" rx="2.5" fill="#111827"/>')
        kf = doc.uid("eq")
        a, b, c = rnd.uniform(.25, .5), rnd.uniform(.6, .95), rnd.uniform(.35, .7)
        dur = rnd.uniform(2.4, 4.2)
        doc.css.append(f"@keyframes {kf}{{0%,100%{{transform:scaleX({f2(a)})}}35%{{transform:scaleX({f2(b)})}}70%{{transform:scaleX({f2(c)})}}}}"
                       f".{kf}{{transform-box:fill-box;transform-origin:0 50%;animation:{kf} {f2(dur)}s ease-in-out infinite}}")
        doc.add(f'<rect class="{kf}" x="{PX}" y="{yy + 9}" width="300" height="5" rx="2.5" fill="{col}" fill-opacity=".85"/>')
        doc.add(f'<circle cx="{PX + 300}" cy="{yy - 5}" r="3.5" fill="{GREEN}"><animate attributeName="opacity" values="1;.3;1" dur="{f2(1.2 + k * 0.17)}s" repeatCount="indefinite"/></circle>')
    # packets flowing into router
    doc.add(glint_line(doc, PX, 468, 300, dur=3.2, h=1))
    doc.add(doc.text(PX, 498, "ROUTER", "monoB", 11, ICE, ls=2))
    doc.add(doc.text(PX + 300, 498, "text · code · image · video", "mono", 11, DIM, anchor="end"))
    doc.add(cl)
    doc.save("terminal.svg")


# ═══════════════════════════════════════════════════════════════ ECOSYSTEM MAP
ECO = {
    "build": RED_HOT, "operate": BLUE_HOT, "learn": GOLD, "create": ICE, "signal": "#A78BFA",
}
ECO_RINGS = [
    (205, 122, -150, [("ARSENAL", "build"), ("ZEN AI ARENA", "operate"), ("QUBIT", "operate"), ("AI PIONEER", "learn")]),
    (360, 200, -135, [("ZEN PEN", "create"), ("VIBE BOOK", "create"), ("AGENT VIZ", "create"), ("ZEN AVATAR", "create"),
                      ("ANALYTICS", "operate"), ("LIFE-IN-TIME", "create"), ("PERIODIC", "learn"), ("PHYSICS", "learn")]),
    (492, 268, -162, [("SOLUTIONS", "operate"), ("TOOLS", "build"), ("CHALLENGES", "learn"), ("LEADERBOARD", "signal"),
                      ("ZEN WEEKLY", "signal"), ("BLOG", "signal"), ("MEDIA HUB", "signal"), ("GROUPS", "learn"),
                      ("DISCORD", "signal"), ("TELEGRAM", "signal")]),
]


def build_ecosystem():
    W, H = 1200, 700
    CX, CY = 600, 352
    doc = Doc(W, H, "The ZEN ecosystem — a live system map",
              "Animated constellation of the ZEN AI Co. ecosystem around the ZZ core: Arsenal, ZEN AI Arena, Qubit and "
              "AI Pioneer on the inner ring; creative and intelligence tools on the middle ring; community and media on the outer ring.")
    op, cl = card_frame(doc, W, H, 26)
    doc.add(op)
    doc.add(aurora(doc, W, H, [(CX, CY, 520, BLUE, 0.20, 0, 0, 20), (CX - 200, CY + 200, 360, RED, 0.14, 40, -20, 17)]))
    doc.add(BACKDROP_END, starfield(doc, W, H, 120, seed=5))
    doc.css.append("@keyframes dash{to{stroke-dashoffset:-240}}.rd{animation:dash 30s linear infinite}.rd2{animation:dash 45s linear infinite reverse}")
    nodes = []
    for ri, (rx, ry, a0, items) in enumerate(ECO_RINGS):
        cls = "rd" if ri % 2 == 0 else "rd2"
        doc.add(f'<ellipse class="{cls}" cx="{CX}" cy="{CY}" rx="{rx}" ry="{ry}" stroke="{ICE}" stroke-opacity="{f2(0.28 - ri * 0.05)}" stroke-dasharray="{("2 6", "1 5", "6 10")[ri]}"/>')
        step = 360 / len(items)
        for k, (name, cat) in enumerate(items):
            ang = math.radians(a0 + k * step)
            nx, ny = CX + rx * math.cos(ang), CY + ry * math.sin(ang)
            nodes.append((ri, name, cat, nx, ny, ang))
    # spokes + packets
    for idx, (ri, name, cat, nx, ny, ang) in enumerate(nodes):
        col = ECO[cat]
        doc.add(f'<path d="M{CX} {CY}L{f2(nx)} {f2(ny)}" stroke="{col}" stroke-opacity="{f2(0.16 - ri * 0.03)}" stroke-width="1"/>')
        inward = idx % 3 == 1
        d = f"M{f2(nx)} {f2(ny)}L{CX} {CY}" if inward else f"M{CX} {CY}L{f2(nx)} {f2(ny)}"
        dur = 2.2 + ri * 0.9
        begin = (idx * 0.37) % dur
        doc.add(f'<circle r="{2.6 - ri * 0.4}" fill="{col}"><animateMotion path="{d}" dur="{f2(dur)}s" begin="-{f2(begin)}s" repeatCount="indefinite"/>'
                f'<animate attributeName="opacity" values="0;1;1;0" keyTimes="0;.15;.8;1" dur="{f2(dur)}s" begin="-{f2(begin)}s" repeatCount="indefinite"/></circle>')
    # nodes + labels
    for idx, (ri, name, cat, nx, ny, ang) in enumerate(nodes):
        col = ECO[cat]
        r = (9, 6.5, 5)[ri]
        doc.add(f'<circle cx="{f2(nx)}" cy="{f2(ny)}" r="{r + 7}" fill="{col}" fill-opacity=".10"/>')
        doc.add(f'<circle cx="{f2(nx)}" cy="{f2(ny)}" r="{r}" fill="#060A12" stroke="{col}" stroke-width="2"/>')
        doc.add(f'<circle cx="{f2(nx)}" cy="{f2(ny)}" r="{r * 0.45}" fill="{col}"><animate attributeName="opacity" values="1;.35;1" dur="{f2(1.8 + (idx % 5) * 0.3)}s" repeatCount="indefinite"/></circle>')
        c, s_ = math.cos(ang), math.sin(ang)
        font = ("display", "displayM", "displayM")[ri]
        size = (15, 12.5, 12)[ri]
        off = r + 12
        if abs(c) < 0.35:
            lx, ly, anchor = nx, ny + (off + size if s_ > 0 else -off), "middle"
        else:
            lx, ly, anchor = nx + (off if c > 0 else -off), ny + size * 0.36, ("start" if c > 0 else "end")
        doc.add(doc.text(lx, ly, name, font, size, WHITE if ri == 0 else "#CBD5E1", anchor=anchor, ls=1.1,
                         attrs='stroke="#04070D" stroke-width="5" paint-order="stroke" stroke-linejoin="round"'))
    # core
    doc.add(f'<circle cx="{CX}" cy="{CY}" r="92" fill="{BLUE}" fill-opacity=".07"/>')
    for rr, dash, dur, col, rev in ((78, "40 18", 14, RED_HOT, False), (88, "3 9", 22, ICE, True), (68, "120 310", 6, WHITE, False)):
        to = -360 if rev else 360
        doc.add(f'<circle cx="{CX}" cy="{CY}" r="{rr}" stroke="{col}" stroke-opacity=".55" stroke-width="1.5" stroke-dasharray="{dash}">'
                f'<animateTransform attributeName="transform" type="rotate" from="0 {CX} {CY}" to="{to} {CX} {CY}" dur="{dur}s" repeatCount="indefinite"/></circle>')
    doc.add(f'<circle cx="{CX}" cy="{CY}" r="58" fill="#070B14" stroke="{LINE}"/>')
    lh_ = 52
    doc.add(laser_logo(doc, CX - logo_width(lh_) / 2, CY - lh_ / 2 - 4, lh_, dur=8, draw_frac=0.4))
    doc.add(doc.text(CX, CY + 40, "ZEN CORE", "monoB", 9.5, ICE, anchor="middle", ls=2))
    # legend
    lx = 40
    for name, key in (("BUILD", "build"), ("OPERATE", "operate"), ("LEARN", "learn"), ("CREATE", "create"), ("COMMUNITY", "signal")):
        doc.add(f'<circle cx="{lx + 5}" cy="{H - 36}" r="5" fill="{ECO[key]}"/>')
        doc.add(doc.text(lx + 16, H - 32, name, "monoB", 11, MUTED, ls=1.6))
        lx += 26 + FB.width("monoB", name, 11, 1.6) + 12
    doc.add(doc.text(W - 40, H - 32, "22 SURFACES · 1 OPERATING SYSTEM", "monoB", 11, DIM, anchor="end", ls=1.6))
    doc.add(doc.text(40, 44, "SYSTEM MAP", "monoB", 12, ICE, ls=2.4))
    doc.add(doc.text(W - 40, 44, "ZENAI.WORLD · ARSENAL.WORLD", "mono", 12, DIM, anchor="end", ls=1.6))
    doc.add(cl)
    doc.save("ecosystem.svg")


# ═══════════════════════════════════════════════════════════════ PRODUCT TILES
def _glyph(doc: Doc, kind, cx, cy, col) -> str:
    if kind == "arsenal":
        return (f'<circle cx="{cx}" cy="{cy}" r="34" stroke="{col}" stroke-opacity=".5" stroke-dasharray="4 6">'
                f'<animateTransform attributeName="transform" type="rotate" from="0 {cx} {cy}" to="360 {cx} {cy}" dur="16s" repeatCount="indefinite"/></circle>'
                + laser_logo(doc, cx - logo_width(30) / 2, cy - 15, 30, dur=7, spark=True))
    if kind == "arena":
        return (f'<circle cx="{cx - 12}" cy="{cy}" r="20" stroke="{RED_HOT}" stroke-width="2"><animate attributeName="cx" values="{cx - 14};{cx - 8};{cx - 14}" dur="2.4s" repeatCount="indefinite"/></circle>'
                f'<circle cx="{cx + 12}" cy="{cy}" r="20" stroke="{BLUE_HOT}" stroke-width="2"><animate attributeName="cx" values="{cx + 14};{cx + 8};{cx + 14}" dur="2.4s" repeatCount="indefinite"/></circle>'
                f'<path d="M{cx - 4} {cy - 8}l8 8-8 8" stroke="#fff" stroke-width="2"/>'
                f'<circle cx="{cx}" cy="{cy}" r="34" stroke="{col}" stroke-opacity=".35"/>')
    if kind == "qubit":
        return (f'<circle cx="{cx}" cy="{cy}" r="30" stroke="{col}" stroke-opacity=".8" stroke-width="1.5"/>'
                f'<ellipse cx="{cx}" cy="{cy}" rx="30" ry="9" stroke="{col}" stroke-opacity=".45"/>'
                f'<g><animateTransform attributeName="transform" type="rotate" from="0 {cx} {cy}" to="360 {cx} {cy}" dur="6s" repeatCount="indefinite"/>'
                f'<path d="M{cx} {cy}L{cx + 16} {cy - 22}" stroke="{RED_HOT}" stroke-width="2.2"/><circle cx="{cx + 16}" cy="{cy - 22}" r="3.5" fill="{RED_HOT}"/></g>'
                f'<circle cx="{cx}" cy="{cy}" r="2.5" fill="#fff"/>')
    if kind == "pioneer":
        return (f'<circle cx="{cx}" cy="{cy}" r="32" stroke="{GOLD}" stroke-opacity=".35"/>'
                f'<g><animateTransform attributeName="transform" type="rotate" from="0 {cx} {cy}" to="360 {cx} {cy}" dur="5s" repeatCount="indefinite"/>'
                f'<circle cx="{cx}" cy="{cy - 32}" r="4" fill="{RED_HOT}"/></g>'
                f'<path d="{star_path(cx, cy, 18)}" fill="{GOLD}"><animate attributeName="opacity" values="1;.6;1" dur="2.4s" repeatCount="indefinite"/></path>')
    if kind == "solutions":
        out = []
        for k in range(3):
            y = cy + 12 - k * 12
            out.append(f'<path d="M{cx} {y - 10}L{cx + 26} {y}L{cx} {y + 10}L{cx - 26} {y}Z" fill="#070B14" stroke="{(BLUE_HOT, WHITE, RED_HOT)[k]}" stroke-width="1.6">'
                       f'<animateTransform attributeName="transform" type="translate" values="0 0;0 {-3 - k * 2};0 0" dur="3s" begin="{k * 0.25}s" repeatCount="indefinite"/></path>')
        return "".join(out)
    if kind == "tools":
        out = []
        for k in range(4):
            x = cx - 22 + (k % 2) * 24
            y = cy - 22 + (k // 2) * 24
            out.append(f'<rect x="{x}" y="{y}" width="20" height="20" rx="5" fill="{(RED_HOT, WHITE, BLUE_HOT, ICE)[k]}" fill-opacity=".15" stroke="{(RED_HOT, WHITE, BLUE_HOT, ICE)[k]}">'
                       f'<animate attributeName="fill-opacity" values=".12;.12;.85;.12;.12" keyTimes="0;{k * 0.22};{k * 0.22 + 0.1};{k * 0.22 + 0.2};1" dur="3.2s" repeatCount="indefinite"/></rect>')
        return "".join(out)
    return ""


TILES = [
    ("arsenal", "ARSENAL", "Agent + app builder platform", "arsenal.world", RED_HOT),
    ("arena", "ZEN AI ARENA", "Models + agents, side by side", "us.zenai.biz", BLUE_HOT),
    ("qubit", "QUBIT", "Business agent platform", "qubit.earth", ICE),
    ("pioneer", "AI PIONEER", "Youth AI literacy · Year 4", "zenai.world/ailiteracyyouth", GOLD),
    ("solutions", "ZEN SOLUTIONS", "Enterprise + business AI", "zenai.world/solutions", BLUE_HOT),
    ("tools", "ZEN TOOLS", "AI utilities for everyone", "zenai.world/tools", RED_HOT),
]


def build_tiles():
    for i, (kind, name, desc, url, col) in enumerate(TILES):
        W, H = 400, 168
        doc = Doc(W, H, f"{name.title()} — {desc}", f"Link tile for {name.title()}: {desc} ({url}).")
        op, cl = card_frame(doc, W, H, 20, bg="#060A12")
        doc.add(op)
        doc.add(aurora(doc, W, H, [(70, 84, 170, col, 0.22, 10, 0, 9 + i)]))
        doc.add(grid_bg(doc, W, H, 16, 0.045), BACKDROP_END)
        doc.add(_glyph(doc, kind, 72, 84, col))
        doc.add(doc.text(134, 70, name, "display", 19, WHITE, ls=0.8))
        doc.add(doc.text(134, 97, desc, "sans", 14.5, "#CBD5E1"))
        doc.add(doc.text(134, 128, url, "mono", 12, col, ls=0.4))
        # arrow nudge
        doc.css.append("@keyframes nudge{0%,100%{transform:translateX(0)}50%{transform:translateX(5px)}}.ng{animation:nudge 1.8s ease-in-out infinite}")
        doc.add(f'<g class="ng"><path d="M{W - 42} 30h14m0 0-6-6m6 6-6 6" stroke="{WHITE}" stroke-opacity=".8" stroke-width="1.8" stroke-linecap="round"/></g>')
        # scanning edge
        doc.add(f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="20" stroke="{col}" stroke-width="2" stroke-dasharray="90 {2 * (W + H) - 90}" stroke-opacity=".9">'
                f'<animate attributeName="stroke-dashoffset" values="0;{-2 * (W + H)}" dur="{6 + i * 0.5}s" repeatCount="indefinite"/></rect>')
        doc.add(cl)
        doc.save(f"tile-{kind}.svg")


# ═══════════════════════════════════════════════════════════════ AI PIONEER
def build_pioneer():
    W, H = 1200, 580
    doc = Doc(W, H, "AI Pioneer Program — the first youth AI literacy program in U.S. history",
              "AI Pioneer Program with Boys & Girls Clubs of Greater Washington: ages 11–18 build and deploy real AI systems. "
              "33,000+ youth, 42 states, Year 4 in 2027. Pipeline: prompt, agent, app, deploy, portfolio, credential.")
    op, cl = card_frame(doc, W, H, 26)
    doc.add(op)
    doc.add(aurora(doc, W, H, [(230, 250, 380, GOLD, 0.13, 20, 20, 18), (1000, 120, 420, BLUE, 0.2, -30, 10, 21), (700, 600, 380, RED, 0.16, 30, -20, 16)]))
    doc.add(grid_bg(doc, W, H, 32, 0.04), BACKDROP_END)
    # seal
    sx, sy = 232, 238
    doc.add(guilloche(sx, sy, 118, 150, rings=7, waves=36, amp=4, pts=260, color=GOLD, opacity=0.18, width=0.7))
    doc.add(f'<circle cx="{sx}" cy="{sy}" r="160" stroke="{GOLD}" stroke-opacity=".5"/>')
    doc.add(f'<circle cx="{sx}" cy="{sy}" r="112" fill="#0A0B10" stroke="{GOLD}" stroke-opacity=".7" stroke-width="1.5"/>')
    doc.add(ring_text(doc, sx, sy, 136, "FIRST YOUTH AI LITERACY PROGRAM IN U.S. HISTORY ◆ AI PIONEER ◆ ",
                      font="monoB", size=12.5, fill=GOLD, dur=48))
    doc.add(f'<path d="{star_path(sx, sy - 34, 26)}" fill="{GOLD}"/>')
    doc.add(doc.text(sx, sy + 20, "AI PIONEER", "display", 21, WHITE, anchor="middle", ls=1))
    doc.add(doc.text(sx, sy + 46, "YEAR 4 · 2027", "monoB", 13, RED_SOFT, anchor="middle", ls=2.4))
    doc.add(doc.text(sx, sy + 68, "AGES 11–18", "mono", 11, MUTED, anchor="middle", ls=2.4))
    # content
    X = 450
    doc.add(doc.text(X, 78, "BOYS & GIRLS CLUBS OF GREATER WASHINGTON  ×  ZEN AI CO.", "monoB", 12.5, RED_SOFT, ls=2))
    doc.add(doc.text(X, 132, "THE FIRST GENERATION", "display", 40, WHITE))
    gid = doc.uid("pg")
    doc.defs.append(f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" x1="{X}" x2="{X + 620}" spreadMethod="reflect">'
                    f'<stop offset="0" stop-color="{GOLD}"/><stop offset=".5" stop-color="#FFFFFF"/><stop offset="1" stop-color="{BLUE_HOT}"/>'
                    f'<animateTransform attributeName="gradientTransform" type="translate" values="0 0;620 0;0 0" dur="9s" repeatCount="indefinite"/></linearGradient>')
    doc.add(doc.text(X, 182, "OF AI BUILDERS.", "display", 40, f"url(#{gid})"))
    doc.add(doc.text(X, 226, "The first youth AI literacy program in United States history.", "sansB", 18.5, "#E2E8F0"))
    doc.add(doc.text(X, 252, "Young builders launch real, cloud-hosted agents, apps, tools and games", "sans", 17, MUTED))
    doc.add(doc.text(X, 276, "with OpenAI, Anthropic, Google, Cohere, Meta, Mistral and more.", "sans", 17, MUTED))
    doc.add(f'<rect x="{X}" y="300" width="3" height="40" rx="1.5" fill="{RED_HOT}"/>')
    doc.add(doc.text(X + 18, 327, "“If it’s not deployed, it’s not finished.”", "sansB", 21, WHITE))
    # stats
    stats = [("33,000+", "YOUTH REACHED"), ("42", "STATES"), ("11–18", "AGES"), ("YEAR 4", "IN 2027")]
    sx0 = X
    for k, (v, lbl) in enumerate(stats):
        doc.add(doc.text(sx0, 390, v, "display", 24, (WHITE, WHITE, WHITE, GOLD)[k]))
        doc.add(doc.text(sx0, 412, lbl, "monoB", 10.5, DIM, ls=2))
        sx0 += max(FB.width("display", v, 24), FB.width("monoB", lbl, 10.5, 2)) + 40
    # pipeline
    steps = ["PROMPT", "AGENT", "APP", "DEPLOY", "PORTFOLIO", "CREDENTIAL"]
    y = 494
    x0, x1 = 90, W - 90
    doc.add(f'<rect x="40" y="450" width="{W - 80}" height="96" rx="18" fill="#060A12" fill-opacity=".8" stroke="{LINE}"/>')
    doc.add(f'<path d="M{x0} {y}H{x1}" stroke="#1E293B" stroke-width="3" stroke-linecap="round"/>')
    pid = doc.uid("pl")
    doc.defs.append(f'<linearGradient id="{pid}" x1="0" x2="1"><stop offset="0" stop-color="{RED_HOT}"/><stop offset=".5" stop-color="#fff"/><stop offset="1" stop-color="{BLUE_HOT}"/></linearGradient>')
    T = 7.0
    doc.add(f'<path d="M{x0} {y}H{x1}" stroke="url(#{pid})" stroke-width="3" stroke-linecap="round" stroke-dasharray="{x1 - x0}" stroke-dashoffset="{x1 - x0}">'
            f'<animate attributeName="stroke-dashoffset" values="{x1 - x0};0;0;{x1 - x0}" keyTimes="0;.62;.92;1" dur="{T}s" repeatCount="indefinite"/></path>')
    doc.add(f'<circle cy="{y}" r="7" fill="#fff"><animate attributeName="cx" values="{x0};{x1};{x1};{x0}" keyTimes="0;.62;.92;1" dur="{T}s" repeatCount="indefinite"/>'
            f'<animate attributeName="opacity" values="1;1;0;0" keyTimes="0;.62;.66;1" dur="{T}s" repeatCount="indefinite"/></circle>')
    for k, st in enumerate(steps):
        nx = x0 + (x1 - x0) * k / (len(steps) - 1)
        tk = 0.62 * k / (len(steps) - 1)
        col = (RED_HOT, RED_SOFT, WHITE, ICE, BLUE_HOT, GOLD)[k]
        doc.add(f'<circle cx="{f2(nx)}" cy="{y}" r="11" fill="#070B14" stroke="#334155" stroke-width="2">'
                f'<animate attributeName="stroke" calcMode="discrete" values="#334155;{col};{col};#334155" keyTimes="0;{f4(tk)};.92;1" dur="{T}s" repeatCount="indefinite"/></circle>')
        doc.add(f'<circle cx="{f2(nx)}" cy="{y}" r="4.5" fill="{col}" opacity=".2">'
                f'<animate attributeName="opacity" calcMode="discrete" values=".2;1;1;.2" keyTimes="0;{f4(tk)};.92;1" dur="{T}s" repeatCount="indefinite"/></circle>')
        doc.add(doc.text(nx, y + 36, st, "monoB", 12, "#CBD5E1", anchor="middle", ls=2))
        doc.add(doc.text(nx, y - 22, f"0{k + 1}", "mono", 10, DIM, anchor="middle", ls=1))
    doc.add(cl)
    doc.save("pioneer.svg")


# ═══════════════════════════════════════════════════════════════ PRESIDENTIAL CREDENTIAL
def build_credential():
    W, H = 1200, 400
    doc = Doc(W, H, "2026 Presidential AI Challenge — recognized contributor",
              "Credential card: Alexander Leschik, recognized for contribution to the 2026 Presidential AI Challenge. "
              "Certificate text: In recognition of your contribution to the 2026 Presidential AI Challenge.")
    op, cl = card_frame(doc, W, H, 26, bg="#07070A")
    doc.add(op)
    doc.add(aurora(doc, W, H, [(260, 200, 360, GOLD, 0.14, 20, 10, 16), (1000, 200, 380, RED, 0.12, -20, 10, 19), (700, -40, 300, BLUE, 0.16, 20, 20, 22)]), BACKDROP_END)
    # guilloche band behind content
    doc.add('<g opacity=".9"><animateTransform attributeName="transform" type="rotate" from="0 1040 200" to="360 1040 200" dur="160s" repeatCount="indefinite"/>'
            + guilloche(1040, 200, 90, 190, rings=11, waves=40, amp=5, pts=300, color=GOLD, opacity=0.16, width=0.7) + "</g>")
    # certificate thumbnail (embedded)
    try:
        from PIL import Image
        im = Image.open(ROOT / "assets" / "presidential-ai-challenge-2026-certificate.png").convert("RGB")
        im.thumbnail((420, 420))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=86, optimize=True, progressive=True)
        cert = base64.b64encode(buf.getvalue()).decode()
        iw, ih = im.size
    except Exception as e:  # pragma: no cover
        print("  (certificate thumbnail skipped:", e, ")")
        cert, iw, ih = None, 420, 316
    tw_ = 330
    th_ = tw_ * ih / iw
    tx, ty = 64, (H - th_) / 2
    hid = doc.uid("holo")
    doc.defs.append(f'<linearGradient id="{hid}" x1="0" y1="0" x2="1" y2="1"><stop offset=".35" stop-color="#fff" stop-opacity="0"/>'
                    f'<stop offset=".5" stop-color="#fff" stop-opacity=".45"/><stop offset=".65" stop-color="#fff" stop-opacity="0"/></linearGradient>')
    ccl = doc.uid("cc")
    doc.defs.append(f'<clipPath id="{ccl}"><rect x="{tx}" y="{f2(ty)}" width="{tw_}" height="{f2(th_)}" rx="6"/></clipPath>')
    g = [f'<g transform="rotate(-3 {tx + tw_ / 2} {H / 2})">',
         f'<rect x="{tx - 10}" y="{f2(ty - 10)}" width="{tw_ + 20}" height="{f2(th_ + 20)}" rx="12" fill="#0B0B0F" stroke="{GOLD}" stroke-opacity=".6"/>']
    if cert:
        g.append(f'<image href="data:image/jpeg;base64,{cert}" x="{tx}" y="{f2(ty)}" width="{tw_}" height="{f2(th_)}" clip-path="url(#{ccl})" preserveAspectRatio="xMidYMid slice"/>')
    g.append(f'<g clip-path="url(#{ccl})"><rect x="{tx - tw_}" y="{f2(ty)}" width="{tw_}" height="{f2(th_)}" fill="url(#{hid})">'
             f'<animate attributeName="x" values="{tx - tw_};{tx + tw_};{tx + tw_}" keyTimes="0;.45;1" dur="5s" repeatCount="indefinite"/></rect></g>')
    g.append("</g>")
    doc.add("".join(g))
    # text
    X = 450
    doc.add(doc.text(X, 88, "RECOGNITION · 2026", "monoB", 13, GOLD, ls=3))
    fid = doc.uid("foil")
    doc.defs.append(f'<linearGradient id="{fid}" gradientUnits="userSpaceOnUse" x1="{X}" x2="{X + 520}" spreadMethod="reflect">'
                    f'<stop offset="0" stop-color="{GOLD_DEEP}"/><stop offset=".45" stop-color="#FFF4C7"/><stop offset=".55" stop-color="{GOLD}"/><stop offset="1" stop-color="{GOLD_DEEP}"/>'
                    f'<animateTransform attributeName="gradientTransform" type="translate" values="0 0;520 0;0 0" dur="7s" repeatCount="indefinite"/></linearGradient>')
    doc.add(doc.text(X, 146, "PRESIDENTIAL", "display", 44, f"url(#{fid})"))
    doc.add(doc.text(X, 198, "AI CHALLENGE", "display", 44, f"url(#{fid})"))
    p1, pw1 = pill(doc, X, 222, "RECOGNIZED CONTRIBUTOR", size=11.5, fg="#FDE68A", stroke=GOLD_DEEP, fill="#141004",
                   icon=lambda cx, cy: f'<path d="{star_path(cx, cy, 6.5)}" fill="{GOLD}"/>', ls=1.8)
    doc.add(p1)
    doc.add(doc.text(X + pw1 + 14, 243, "Alexander Leschik", "sansB", 17, WHITE))
    doc.add(doc.text(X, 300, "“In recognition of your contribution to the", "sans", 19, "#E2E8F0"))
    doc.add(doc.text(X, 326, "2026 Presidential AI Challenge.”", "sans", 19, "#E2E8F0"))
    doc.add(doc.text(X, 360, "ORIGINAL CERTIFICATE ON FILE IN THIS REPOSITORY ↗", "mono", 11, DIM, ls=1.8))
    # seal
    sx, sy = 1040, 200
    doc.add(f'<circle cx="{sx}" cy="{sy}" r="74" fill="#0B0A06" stroke="{GOLD}" stroke-width="1.5"/>')
    doc.add(ring_text(doc, sx, sy, 60, "PRESIDENTIAL AI CHALLENGE · 2026 · ", font="monoB", size=10, fill=GOLD, dur=30, reverse=True))
    doc.add(f'<circle cx="{sx}" cy="{sy}" r="44" stroke="{GOLD}" stroke-opacity=".5"/>')
    doc.add(f'<path d="{star_path(sx, sy - 4, 26)}" fill="{GOLD}"/>')
    doc.add(doc.text(sx, sy + 34, "2026", "monoB", 11, "#FDE68A", anchor="middle", ls=2))
    doc.add(cl)
    doc.save("credential.svg")


# ═══════════════════════════════════════════════════════════════ BUTTONS
BUTTONS = [
    ("arsenal", "ENTER ARSENAL", "arsenal.world", RED_HOT, "logo"),
    ("pioneer", "AI PIONEER", "youth AI literacy", BLUE_HOT, "star"),
    ("partner", "PARTNER WITH ZEN", "huxley@zenai.biz", WHITE, "mail"),
    ("portfolio", "PORTFOLIO", "alexleschik.com", ICE, "arrow"),
]


def build_buttons():
    for i, (slug, label, sub, col, icon) in enumerate(BUTTONS):
        W, H = 330, 84
        doc = Doc(W, H, f"{label.title()} — {sub}", f"Button: {label.title()} ({sub}).")
        cid = doc.uid("bc")
        doc.defs.append(f'<clipPath id="{cid}"><rect x="2" y="2" width="{W - 4}" height="{H - 4}" rx="{(H - 4) / 2}"/></clipPath>')
        bgid = doc.uid("bg")
        doc.defs.append(f'<linearGradient id="{bgid}" x1="0" x2="1"><stop offset="0" stop-color="#0B1020"/><stop offset="1" stop-color="#05070D"/></linearGradient>')
        doc.add(f'<g clip-path="url(#{cid})"><rect width="{W}" height="{H}" fill="url(#{bgid})"/>')
        doc.add(aurora(doc, W, H, [(50, 42, 120, col if col != WHITE else "#94A3B8", 0.35, 8, 0, 5 + i)]))
        shid = doc.uid("sh")
        doc.defs.append(f'<linearGradient id="{shid}" x1="0" x2="1"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset=".5" stop-color="#fff" stop-opacity=".16"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>')
        doc.add(f'<rect y="0" width="90" height="{H}" fill="url(#{shid})" transform="skewX(-20)"><animate attributeName="x" values="-160;{W + 60};{W + 60}" keyTimes="0;.4;1" dur="4.5s" begin="{i * 0.6}s" repeatCount="indefinite"/></rect>')
        doc.add("</g>")
        per = 2 * (W - H) + math.pi * (H - 4)
        doc.add(f'<rect x="2" y="2" width="{W - 4}" height="{H - 4}" rx="{(H - 4) / 2}" stroke="{col}" stroke-opacity=".35" stroke-width="1.5"/>')
        doc.add(f'<rect x="2" y="2" width="{W - 4}" height="{H - 4}" rx="{(H - 4) / 2}" stroke="{col}" stroke-width="2.5" stroke-linecap="round" stroke-dasharray="70 {f2(per - 70)}">'
                f'<animate attributeName="stroke-dashoffset" values="0;{f2(-per)}" dur="{3.6 + i * 0.4}s" repeatCount="indefinite"/></rect>')
        # icon well
        doc.add(f'<circle cx="42" cy="42" r="26" fill="#0A0F1C" stroke="{col}" stroke-opacity=".6"/>')
        if icon == "logo":
            doc.add(static_logo(42 - logo_width(22) / 2, 31, 22, WHITE))
        elif icon == "star":
            doc.add(f'<path d="{star_path(42, 43, 14)}" fill="{col}"/>')
        elif icon == "mail":
            doc.add('<rect x="29" y="33" width="26" height="18" rx="3" stroke="#fff" stroke-width="2"/><path d="M30 35l12 9 12-9" stroke="#fff" stroke-width="2" stroke-linejoin="round"/>')
        else:
            doc.add(f'<path d="M33 51L51 33M40 33h11v11" stroke="{col}" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>')
        fs = min(17.5, 17.5 * 186 / FB.width("display", label, 17.5, 0.6))
        doc.add(doc.text(84, 40, label, "display", fs, WHITE, ls=0.6))
        doc.add(doc.text(84, 62, sub, "mono", 12.5, col if col != WHITE else MUTED, ls=0.3))
        doc.css.append("@keyframes nudge{0%,100%{transform:translateX(0)}50%{transform:translateX(5px)}}.ng{animation:nudge 1.6s ease-in-out infinite}")
        doc.add(f'<g class="ng"><path d="M{W - 46} 42h14m0 0-6-6m6 6-6 6" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></g>')
        doc.save(f"btn-{slug}.svg")


# ═══════════════════════════════════════════════════════════════ SOCIAL PILLS
SOCIALS = [
    ("x", "X", "@ZEN_AGI", "x"),
    ("linkedin", "LinkedIn", "Alexander Leschik", None),
    ("linkedin-co", "LinkedIn", "ZEN AI Co.", None),
    ("youtube", "YouTube", "@ZENAIML", "youtube"),
    ("instagram", "Instagram", "@0xvvs1", "instagram"),
    ("tiktok", "TikTok", "@milennialai", "tiktok"),
    ("discord", "Discord", "ZEN community", "discord"),
    ("telegram", "Telegram", "@ZENOAI", "telegram"),
    ("linq", "Linq", "all links", None),
]


def _simple_icon(name: str) -> str | None:
    tgz = _fetch("https://registry.npmjs.org/simple-icons/-/simple-icons-16.33.0.tgz", CACHE / "simple-icons-16.33.0.tgz")
    import re
    with tarfile.open(tgz) as t:
        try:
            svg = t.extractfile(f"package/icons/{name}.svg").read().decode()
        except KeyError:
            return None
    m = re.search(r' d="([^"]+)"', svg)
    return m.group(1) if m else None


def build_socials():
    for i, (slug, name, handle, icon) in enumerate(SOCIALS):
        W, H = 280, 72
        doc = Doc(W, H, f"{name} — {handle}", f"Link to {name}: {handle}")
        cid = doc.uid("sc")
        doc.defs.append(f'<clipPath id="{cid}"><rect x="1.5" y="1.5" width="{W - 3}" height="{H - 3}" rx="18"/></clipPath>')
        col = (RED_HOT, WHITE, BLUE_HOT)[i % 3]
        doc.add(f'<g clip-path="url(#{cid})"><rect width="{W}" height="{H}" fill="#070B14"/>')
        doc.add(aurora(doc, W, H, [(36, 36, 90, col if col != WHITE else "#94A3B8", 0.3, 6, 0, 6 + i * 0.5)]))
        doc.add("</g>")
        per = 2 * (W + H)
        doc.add(f'<rect x="1.5" y="1.5" width="{W - 3}" height="{H - 3}" rx="18" stroke="{LINE}" stroke-width="1.5"/>')
        doc.add(f'<rect x="1.5" y="1.5" width="{W - 3}" height="{H - 3}" rx="18" stroke="{col}" stroke-width="2" stroke-dasharray="40 {per - 40}" stroke-opacity=".9">'
                f'<animate attributeName="stroke-dashoffset" values="{-i * 60};{-i * 60 - per}" dur="{5 + (i % 3) * 0.7}s" repeatCount="indefinite"/></rect>')
        doc.add('<rect x="14" y="14" width="44" height="44" rx="12" fill="#0C1220" stroke="#1F2A3F"/>')
        d = _simple_icon(icon) if icon else None
        if d:
            doc.add(f'<path transform="translate(24 24)" d="{d}" fill="#fff"/>')
        elif slug.startswith("linkedin"):
            doc.add('<rect x="24" y="24" width="24" height="24" rx="4" fill="#fff"/>')
            doc.add(doc.text(36, 43, "in", "sansB", 16, "#070B14", anchor="middle"))
        else:  # linq
            doc.add('<path d="M31 41l10-10m-6-2 3-3a5 5 0 017 7l-3 3m-8 8-3 3a5 5 0 01-7-7l3-3" stroke="#fff" stroke-width="2.4" stroke-linecap="round" transform="translate(2 1)"/>')
        doc.add(doc.text(74, 33, name.upper(), "monoB", 12, MUTED, ls=2))
        doc.add(doc.text(74, 55, handle, "monoB", 15, WHITE))
        doc.save(f"social-{slug}.svg")


# ═══════════════════════════════════════════════════════════════ FOOTER
def build_footer():
    W, H = 1200, 380
    doc = Doc(W, H, "Build what matters. Ship what works. Prove what changed.",
              "Closing banner: the ZEN ZZ monogram is laser-traced while the ZEN operating principles cycle.")
    op, cl = card_frame(doc, W, H, 28)
    doc.add(op)
    doc.add(aurora(doc, W, H, [(600, 140, 420, BLUE, 0.2, 0, 10, 18), (220, 380, 340, RED, 0.18, 30, -10, 15), (1000, 380, 340, BLUE, 0.14, -30, -10, 21)]))
    doc.add(grid_bg(doc, W, H, 30, 0.04))
    doc.add(BACKDROP_END, starfield(doc, W, H, 70, seed=21))
    cx, cy = 600, 118
    for k in range(3):
        doc.add(f'<circle cx="{cx}" cy="{cy}" r="60" stroke="{(RED_HOT, WHITE, BLUE_HOT)[k]}" stroke-width="1.2" opacity="0">'
                f'<animate attributeName="r" values="60;150" dur="4.5s" begin="{k * 1.5}s" repeatCount="indefinite"/>'
                f'<animate attributeName="opacity" values=".6;0" dur="4.5s" begin="{k * 1.5}s" repeatCount="indefinite"/></circle>')
    doc.add(f'<circle cx="{cx}" cy="{cy}" r="64" fill="#070B14" stroke="{LINE}"/>')
    lh = 62
    doc.add(laser_logo(doc, cx - logo_width(lh) / 2, cy - lh / 2, lh, dur=8, draw_frac=0.45))
    phrases = [("BUILD WHAT MATTERS.", RED_HOT), ("SHIP WHAT WORKS.", WHITE), ("PROVE WHAT CHANGED.", BLUE_HOT)]
    T = 9.0
    for k, (p, col) in enumerate(phrases):
        kf = doc.uid("ph")
        a = k / 3 * 100
        b = (k + 1) / 3 * 100
        doc.css.append(f"@keyframes {kf}{{0%{{opacity:0;transform:translateY(14px)}}"
                       f"{f2(max(a, 0.01))}%{{opacity:0;transform:translateY(14px)}}{f2(a + 4)}%{{opacity:1;transform:translateY(0)}}"
                       f"{f2(b - 4)}%{{opacity:1;transform:translateY(0)}}{f2(b)}%{{opacity:0;transform:translateY(-14px)}}100%{{opacity:0}}}}"
                       f".{kf}{{animation:{kf} {T}s infinite}}")
        doc.add(f'<g class="{kf}" opacity="0">{doc.text(W / 2, 258, p, "display", 44, WHITE, anchor="middle", ls=1)}'
                f'<rect x="{f2(W / 2 - 40)}" y="276" width="80" height="4" rx="2" fill="{col}"/></g>')
    doc.add(glint_line(doc, 80, 316, W - 160, dur=6, h=1))
    doc.add(doc.text(W / 2, 348, "ZENAI.WORLD  ·  ARSENAL.WORLD  ·  US.ZENAI.BIZ  ·  QUBIT.EARTH  ·  WASHINGTON, D.C.", "mono", 12.5, MUTED, anchor="middle", ls=2))
    doc.add(cl)
    doc.save("footer.svg")


BUILDERS = {
    "hero": build_hero, "sections": build_sections, "signal": build_signal, "terminal": build_terminal,
    "ecosystem": build_ecosystem, "tiles": build_tiles, "pioneer": build_pioneer, "credential": build_credential,
    "buttons": build_buttons, "socials": build_socials, "footer": build_footer,
}


def main():
    import sys
    global FB
    FB = FontBank()
    names = sys.argv[1:] or list(BUILDERS)
    print(f"building {len(names)} asset(s) into {OUT.relative_to(ROOT)}/")
    for n in names:
        BUILDERS[n]()


if __name__ == "__main__":
    main()
