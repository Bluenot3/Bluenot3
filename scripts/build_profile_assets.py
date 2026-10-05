#!/usr/bin/env python3
"""Build the animated SVG assets behind the Bluenot3 profile README.

Every asset is a single self-contained SVG so it survives GitHub's <img> sandbox:
  * fonts are subset to the exact glyphs used and embedded as WOFF2 data URIs
  * the ZEN "ZZ" monogram is inline vector geometry (traced from the master PNG)
  * all motion is CSS keyframes or SMIL, no scripts, no external requests
  * no blur filters on anything that animates, so repaint stays cheap

Wide assets are built twice: `name.svg` for desktop and `name-m.svg`, a portrait layout the
README serves to phones through <picture><source media="(max-width: 600px)">, so type stays
legible on a ~330px-wide README column.

    pip install fonttools brotli pillow
    python3 scripts/build_profile_assets.py            # everything
    python3 scripts/build_profile_assets.py hero tiles # just some builders

Fonts (SIL OFL) and the world-atlas topology are downloaded once into .cache/.
"""
from __future__ import annotations

import base64
import io
import json
import math
import re
import shutil
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

# ─────────────────────────────────────────────────────────────── tokens
INK = "#07080A"
INK2 = "#0D0F13"
LINE = "#1A1E25"
LINE2 = "#2A3039"
WHITE = "#F5F7FA"
TEXT2 = "#B7BFCB"
MUTED = "#8C95A3"
DIM = "#5E6674"
RED = "#FF4A3D"
RED_SOFT = "#FF9A90"
BLUE = "#3D6DF2"

# ─────────────────────────────────────────────────────────────── fonts
FONT_SOURCES = {
    "inter": "ofl/inter/Inter%5Bopsz,wght%5D.ttf",
    "jbmono": "ofl/jetbrainsmono/JetBrainsMono%5Bwght%5D.ttf",
    "iserif": "ofl/instrumentserif/InstrumentSerif-Italic.ttf",
}
# key -> (source, axes, css family name)
FONTS = {
    "disp": ("inter", {"wght": 640, "opsz": 32}, "ZDisp"),
    "sans": ("inter", {"wght": 400, "opsz": 16}, "ZSans"),
    "sansM": ("inter", {"wght": 500, "opsz": 16}, "ZSansM"),
    "sansS": ("inter", {"wght": 600, "opsz": 20}, "ZSansS"),
    "mono": ("jbmono", {"wght": 460}, "ZMono"),
    "monoB": ("jbmono", {"wght": 640}, "ZMonoB"),
    "serif": ("iserif", {}, "ZSerif"),
}
_SANS = "Inter,'Segoe UI',Helvetica,Arial,sans-serif"
_MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
FALLBACK = {"disp": _SANS, "sans": _SANS, "sansM": _SANS, "sansS": _SANS,
            "mono": _MONO, "monoB": _MONO, "serif": "'Instrument Serif',Georgia,'Times New Roman',serif"}


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
            tag = "-".join(f"{k}{v}" for k, v in sorted(axes.items())) or "static"
            static = CACHE / "fonts" / f"{src}-{tag}.ttf"
            if not static.exists():
                raw = _fetch("https://raw.githubusercontent.com/google/fonts/main/" + FONT_SOURCES[src],
                             CACHE / "fonts" / f"{src}-source.ttf")
                if axes:
                    instancer.instantiateVariableFont(TTFont(raw), axes).save(static)
                else:
                    shutil.copy(raw, static)
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


def track(font: str, size: float) -> float:
    """Default letter-spacing: display type is set tight, mono labels open."""
    return {"disp": -0.035, "sansS": -0.012, "sansM": -0.006, "sans": 0.0,
            "mono": 0.0, "monoB": 0.0, "serif": 0.004}[font] * size


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

    def text(self, x, y, s, font="sans", size=16, fill=WHITE, anchor="start", ls=None,
             opacity=None, cls="", attrs="") -> str:
        self.used[font].update(s)
        ls = track(font, size) if ls is None else ls
        o = f' opacity="{f2(opacity)}"' if opacity is not None else ""
        a = f' text-anchor="{anchor}"' if anchor != "start" else ""
        l = f' letter-spacing="{f2(ls)}"' if ls else ""
        c = f"f-{font} {cls}".strip()
        return (f'<text x="{f2(x)}" y="{f2(y)}" class="{c}" font-size="{f2(size)}" fill="{fill}"'
                f'{a}{l}{o} {attrs}>{esc(s)}</text>')

    def runs(self, x, y, parts, anchor="start", cls="", attrs="") -> str:
        """One line made of styled runs: parts = [(text, fill, font, size)]."""
        spans = []
        for t, fill, font, size in parts:
            self.used[font].update(t)
            ls = track(font, size)
            l = f' letter-spacing="{f2(ls)}"' if ls else ""
            spans.append(f'<tspan class="f-{font}" font-size="{f2(size)}" fill="{fill}"{l}>{esc(t)}</tspan>')
        a = f' text-anchor="{anchor}"' if anchor != "start" else ""
        c = f' class="{cls}"' if cls else ""
        return f'<text x="{f2(x)}" y="{f2(y)}"{a}{c} {attrs} xml:space="preserve">{"".join(spans)}</text>'

    def need(self, font: str, chars: str):
        self.used[font].update(chars)

    def render(self) -> str:
        faces = []
        for key, chars in sorted(self.used.items()):
            if not chars:
                continue
            fam = FONTS[key][2]
            faces.append(
                f"@font-face{{font-family:{fam};font-display:swap;src:url(data:font/woff2;base64,{FB.woff2(key, chars)}) format('woff2');}}"
                f".f-{key}{{font-family:{fam},{FALLBACK[key]};}}"
            )
        css = "\n".join(faces + list(dict.fromkeys(self.css)) + [
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
        print(f"  {name:<30} {len(data) / 1024:7.1f} KB")


def width(font, text, size):
    return FB.width(font, text, size, track(font, size))


def runs_width(parts):
    return sum(width(f, t, s) for t, _, f, s in parts)


def fit(font, text, size, maxw):
    w = width(font, text, size)
    return size if w <= maxw else size * maxw / w


def wrap(font, text, size, maxw) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        t = f"{cur} {word}".strip()
        if cur and width(font, t, size) > maxw:
            lines.append(cur)
            cur = word
        else:
            cur = t
    return lines + ([cur] if cur else [])


def rise(doc: Doc, delay: float, dist: float = 18, dur: float = 1.1) -> str:
    """One-shot entrance (plays when the image loads), as a class name."""
    k = f"rz{int(dist)}"
    doc.css.append(f"@keyframes {k}{{from{{opacity:0;transform:translateY({f2(dist)}px)}}to{{opacity:1;transform:none}}}}")
    c = f"r{int(delay * 100)}{k}"
    doc.css.append(f".{c}{{animation:{k} {f2(dur)}s cubic-bezier(.16,.84,.24,1) {f2(delay)}s both}}")
    return c


# ─────────────────────────────────────────────────────────────── ZZ monogram
# Traced from the 1250px master and snapped so every diagonal is exactly 45°.
LOGO_D = ("M162 157V467H281L376 372H251V262H587L162 687V888L250 800V748L736 262H888"
          "L69 1083H1081V776H965L862 879H988V986H651L1081 556V355L990 446V508L512 986"
          "H352L1181 157Z")
LOGO_BOX = (69, 157, 1181, 1083)  # x0, y0, x1, y1
LOGO_W = LOGO_BOX[2] - LOGO_BOX[0]
LOGO_H = LOGO_BOX[3] - LOGO_BOX[1]


def logo_path_length() -> float:
    pts, x, y, cmd, i = [], 0.0, 0.0, None, 0
    toks = re.findall(r"[MLHVZ]|-?\d+\.?\d*", LOGO_D)
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


def laser_logo(doc: Doc, x: float, y: float, h: float, *, dur: float = 14.0, fill: str = WHITE,
               draw_frac: float = 0.16, trace: str = "#FFFFFF") -> str:
    """The ZZ monogram: a laser traces the outline, the solid mark resolves, then it holds."""
    tf, s = logo_transform(x, y, h)
    pid = doc.uid("logo")
    doc.defs.append(f'<path id="{pid}" d="{LOGO_D}"/>')
    L, k1 = LOGO_LEN, draw_frac
    k2 = min(draw_frac + 0.06, 0.9)
    sw = 1.4 / s
    p = [f'<g transform="{tf}">',
         f'<use href="#{pid}" fill="{fill}"><animate attributeName="opacity" values="0;0;1;1;0" '
         f'keyTimes="0;{f2(k1 * 0.75)};{f2(k2)};.97;1" dur="{dur}s" repeatCount="indefinite"/></use>']
    for wdt, col, op in ((sw * 4, RED, 0.22), (sw, trace, 1)):
        p.append(
            f'<use href="#{pid}" stroke="{col}" stroke-width="{f2(wdt)}" stroke-opacity="{op}" stroke-linejoin="round" '
            f'stroke-dasharray="{f2(L)}" stroke-dashoffset="{f2(L)}">'
            f'<animate attributeName="stroke-dashoffset" values="{f2(L)};0;0;0" keyTimes="0;{f2(k1)};.97;1" dur="{dur}s" repeatCount="indefinite"/>'
            f'<animate attributeName="opacity" values="1;1;0;0" keyTimes="0;{f2(k2)};{f2(k2 + 0.04)};1" dur="{dur}s" repeatCount="indefinite"/></use>')
    p.append(f'<g><circle r="{f2(7 / s)}" fill="{RED}" opacity=".35"/><circle r="{f2(2.4 / s)}" fill="{trace}"/>'
             f'<animateMotion dur="{dur}s" repeatCount="indefinite" keyPoints="0;1;1;1" keyTimes="0;{f2(k1)};.99;1" calcMode="linear">'
             f'<mpath href="#{pid}"/></animateMotion>'
             f'<animate attributeName="opacity" values="1;1;0;0" keyTimes="0;{f2(k1)};{f2(k1 + 0.01)};1" dur="{dur}s" repeatCount="indefinite"/></g>')
    p.append("</g>")
    return "".join(p)


def static_logo(x: float, y: float, h: float, fill: str = WHITE) -> str:
    tf, _ = logo_transform(x, y, h)
    return f'<path transform="{tf}" d="{LOGO_D}" fill="{fill}"/>'


# ─────────────────────────────────────────────────────────────── world data
def load_world():
    topo_path = CACHE / "countries-110m.json"
    if not topo_path.exists():
        tgz = _fetch("https://registry.npmjs.org/world-atlas/-/world-atlas-2.0.2.tgz", CACHE / "world-atlas-2.0.2.tgz")
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
    inside, j = False, len(poly) - 1
    for i in range(len(poly)):
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
def globe(doc: Doc, cx: float, cy: float, R: float, *, tilt: float = 22, period: float = 90,
          step: float = 2.8, face_lon: float = -77.04, lat_range=(-56, 78), label_scale: float = 1.0,
          arcs: bool = True, clip_h=None) -> str:
    """A rotating dotted Earth in pure SVG.

    Each latitude ring is one path of zero-length round-capped segments on the unit circle, placed
    with translate+scale so the circle projects to that ring's ellipse. Rotating the path (one
    animateTransform per ring) moves every dot at the right angular speed, non-scaling-stroke keeps
    dots round, and a per-ring clip at the true horizon hides the far side. The United States is
    lit; Washington, D.C. carries a beacon whose label counter-rotates to stay upright.
    """
    countries = load_world()
    plan = ArcPlan(tilt=tilt, period=period, face_lon=face_lon) if arcs else None
    a = math.radians(tilt)
    offset = -face_lon
    front = []
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
            lam = math.radians(lon + offset)
            p = f"M{f4(math.sin(lam))} {f4(math.cos(lam))}h.002"
            if c:
                buckets["us" if c == "840" else "ld " + region(lat, lon)].append(p)
            elif abs(((lon + 180) % 30) - 15) > 15 - 180 / n:
                buckets["gd"].append(p)
            elif i % 2 == 0 and k % 2 == 0:
                buckets["oc"].append(p)
        rid = doc.uid("rg")
        tv = max(-1.2, t)
        doc.defs.append(f'<clipPath id="{rid}"><rect x="-1.3" y="{f4(tv)}" width="2.6" height="{f4(1.3 - tv)}"/></clipPath>')
        inner = [f'<g><animateTransform attributeName="transform" type="rotate" from="0" to="-360" dur="{period}s" repeatCount="indefinite"/>']
        for kind in sorted(buckets, key=lambda k: ("oc", "gd", "ld", "us").index(k.split()[0])):
            if buckets[kind]:
                cls = kind if not plan else " ".join([kind.split()[0]] + [f"L{r}" for r in kind.split()[1:]] + (["Lus"] if kind == "us" else []))
                inner.append(f'<path class="{cls}" d="{"".join(buckets[kind])}"/>')
        inner.append("</g>")
        front.append(f'<g transform="translate({f2(cx)} {f2(cy0)}) scale({f4(r)} {f4(b)})" clip-path="url(#{rid})">{"".join(inner)}</g>')

    doc.css.append(
        ".oc,.gd,.ld,.us,.bc{vector-effect:non-scaling-stroke;stroke-linecap:round;fill:none}"
        ".oc{stroke:#34446A;stroke-width:1.15;stroke-opacity:.62}"
        ".gd{stroke:#5069A0;stroke-width:1.1;stroke-opacity:.5}"
        ".ld{stroke:#C4CEDD;stroke-width:2.35;stroke-opacity:.9}"
        f".us{{stroke:{WHITE};stroke-width:2.55}}"
    )
    if plan:
        doc.css.append(plan.lights_css())

    # Washington, D.C. beacon
    lat = 38.9
    phi = math.radians(lat)
    r = R * math.cos(phi)
    b = r * math.sin(a)
    cy0 = cy - R * math.sin(phi) * math.cos(a)
    t = -math.tan(phi) * math.tan(a)
    lam = math.radians(face_lon + offset)
    px, py = math.sin(lam), math.cos(lam)
    bid = doc.uid("bc")
    doc.defs.append(f'<clipPath id="{bid}"><rect x="-1.6" y="{f4(t)}" width="3.2" height="{f4(1.6 - t)}"/></clipPath>')
    ks = label_scale
    halo = f'stroke="{INK}" stroke-width="{f2(4 * ks)}" stroke-opacity=".9" paint-order="stroke" stroke-linejoin="round"'
    lbl = (f'<g transform="scale({f4(1 / r)} {f4(1 / b)})"><animate attributeName="opacity" values="1;1;0;0;1;1" '
           f'keyTimes="0;.14;.18;.82;.86;1" dur="{period}s" repeatCount="indefinite"/>'
           f'<path d="M0 0L{f2(16 * ks)} {f2(-22 * ks)}H{f2(30 * ks)}" stroke="{WHITE}" stroke-opacity=".55" stroke-width="1"/>'
           + doc.text(36 * ks, -18 * ks, "Washington, D.C.", "sansM", 13 * ks, WHITE, attrs=halo)
           + doc.text(36 * ks, -3 * ks, "38.90° N  77.04° W", "mono", 9.5 * ks, MUTED, attrs=halo)
           + "</g>")
    beacon = (
        f'<g transform="translate({f2(cx)} {f2(cy0)}) scale({f4(r)} {f4(b)})" clip-path="url(#{bid})">'
        f'<g><animateTransform attributeName="transform" type="rotate" from="0" to="-360" dur="{period}s" repeatCount="indefinite"/>'
        f'<path class="bc" d="M{f4(px)} {f4(py)}h.002" stroke="{RED}" stroke-width="{f2(6 * ks)}" stroke-opacity=".9">'
        f'<animate attributeName="stroke-width" values="{f2(6 * ks)};{f2(28 * ks)}" dur="2.4s" repeatCount="indefinite"/>'
        f'<animate attributeName="stroke-opacity" values=".6;0" dur="2.4s" repeatCount="indefinite"/></path>'
        f'<path class="bc" d="M{f4(px)} {f4(py)}h.002" stroke="{RED}" stroke-width="{f2(6.5 * ks)}"/>'
        f'<g transform="translate({f4(px)} {f4(py)})"><g>'
        f'<animateTransform attributeName="transform" type="rotate" from="0" to="360" dur="{period}s" repeatCount="indefinite"/>'
        f"{lbl}</g></g></g></g>"
    )

    gid = doc.uid("gl")
    doc.defs.append(
        f'<radialGradient id="{gid}b" cx="40%" cy="30%" r="78%"><stop offset="0" stop-color="#0F1A30"/>'
        f'<stop offset=".62" stop-color="#080D18"/><stop offset="1" stop-color="#05070C"/></radialGradient>'
        f'<radialGradient id="{gid}a" cx="50%" cy="50%" r="50%"><stop offset=".8" stop-color="{BLUE}" stop-opacity="0"/>'
        f'<stop offset=".862" stop-color="{BLUE}" stop-opacity=".22"/><stop offset=".92" stop-color="{BLUE}" stop-opacity=".07"/>'
        f'<stop offset="1" stop-color="{BLUE}" stop-opacity="0"/></radialGradient>'
        f'<radialGradient id="{gid}l" cx="50%" cy="50%" r="50%"><stop offset=".7" stop-color="{INK}" stop-opacity="0"/>'
        f'<stop offset=".94" stop-color="{INK}" stop-opacity=".38"/><stop offset="1" stop-color="{INK}" stop-opacity=".7"/></radialGradient>'
        f'<radialGradient id="{gid}s" cx="34%" cy="24%" r="42%"><stop offset="0" stop-color="#C9D8FF" stop-opacity=".12"/>'
        f'<stop offset="1" stop-color="#C9D8FF" stop-opacity="0"/></radialGradient>'
        f'<linearGradient id="{gid}r" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#DCE6FF" stop-opacity=".55"/>'
        f'<stop offset=".5" stop-color="#DCE6FF" stop-opacity=".12"/><stop offset="1" stop-color="#DCE6FF" stop-opacity=".03"/></linearGradient>'
    )
    c = f'cx="{f2(cx)}" cy="{f2(cy)}"'
    return (f'<circle {c} r="{f2(R / 0.862)}" fill="url(#{gid}a)"/>'
            f'<circle {c} r="{f2(R)}" fill="url(#{gid}b)"/>'
            + "".join(front)
            + f'<circle {c} r="{f2(R)}" fill="url(#{gid}l)"/><circle {c} r="{f2(R)}" fill="url(#{gid}s)"/>'
            f'<circle {c} r="{f2(R)}" stroke="url(#{gid}r)" stroke-width="1.2"/>'
            + (globe_arcs(doc, cx, cy, R, plan=plan, ks=label_scale, clip_h=clip_h) if plan else "")
            + beacon)


HUBS = {
    "dc": (38.90, -77.04), "chi": (41.88, -87.63), "atl": (33.75, -84.39), "bos": (42.36, -71.06),
    "mia": (25.76, -80.19), "tor": (43.65, -79.38), "msp": (44.98, -93.27), "aus": (30.27, -97.74),
    "den": (39.74, -104.99), "mex": (19.43, -99.13), "la": (34.05, -118.24), "sf": (37.77, -122.42),
    "sea": (47.61, -122.33), "anc": (61.22, -149.90), "hnl": (21.31, -157.86), "tyo": (35.68, 139.69),
    "sel": (37.57, 126.98), "sha": (31.23, 121.47), "hkg": (22.32, 114.17), "sin": (1.35, 103.82),
    "syd": (-33.87, 151.21), "del": (28.61, 77.21), "bom": (19.08, 72.88), "dxb": (25.20, 55.27),
    "cai": (30.04, 31.24), "ist": (41.01, 28.98), "nbo": (-1.29, 36.82), "jnb": (-26.20, 28.05),
    "los": (6.52, 3.38), "ber": (52.52, 13.40), "par": (48.86, 2.35), "mad": (40.42, -3.70),
    "lon": (51.51, -0.13), "gru": (-23.55, -46.63), "bue": (-34.60, -58.38),
}
# The network spreads westward in step with the rotation: D.C. lights the country, hops cross the
# Pacific, run through Asia, the Middle East, Africa and Europe, and London closes the loop on D.C.
ARC_WAVE_1 = ["chi", "atl", "bos", "mia", "tor", "msp", "aus", "den", "mex", "la", "sf", "sea"]
ARC_HOPS = [
    ("sea", "anc"), ("la", "hnl"), ("sf", "hnl"), ("anc", "tyo"), ("hnl", "tyo"), ("hnl", "syd"),
    ("tyo", "sel"), ("tyo", "sha"), ("sha", "hkg"), ("hkg", "sin"), ("syd", "sin"), ("hkg", "del"),
    ("sin", "bom"), ("del", "dxb"), ("bom", "dxb"), ("dxb", "cai"), ("dxb", "nbo"), ("cai", "ist"),
    ("nbo", "jnb"), ("nbo", "los"), ("ist", "ber"), ("ber", "par"), ("par", "lon"), ("par", "mad"),
    ("los", "gru"), ("gru", "bue"), ("lon", "dc"), ("mad", "mia"),
]


ARC_FLIGHTS = {"s": 1.6, "m": 2.1, "l": 2.7}  # flight times (s), quantized so arcs share CSS timelines


def region(lat, lon) -> str:
    """Coarse continent for a point: which part of the map lights up when the network arrives."""
    if lon < -25:
        return "na" if lat > 12 else "sa"
    if lon >= 110 and lat < -10:
        return "oc"
    if lon < 60 and lat >= 36 and not (lon > 26 and lat < 42 and lon > 35):
        return "eu"
    if lon < 52 and lat < 36:
        return "af"
    return "as"


class ArcPlan:
    """When each hop launches and lands. Pure geometry: depends on tilt, period and facing only."""

    def __init__(self, *, tilt, period, face_lon):
        self.a, self.P, self.off = math.radians(tilt), float(period), -face_lon
        self.T_END = self.P - 1.0  # everything has faded by here; D.C. relaunches at t = 0
        self.hops, self.landed = [], {"dc": 0.0}
        for i, (src, dst) in enumerate([("dc", k) for k in ARC_WAVE_1] + ARC_HOPS):
            if src not in self.landed:
                continue
            U0, U1 = self.unit(src), self.unit(dst)
            w = math.acos(max(-1.0, min(1.0, sum(p * q for p, q in zip(U0, U1)))))
            hop = {"src": src, "dst": dst, "w": w, "lift": min(0.04 + 0.30 * w, 0.24), "U0": U0, "U1": U1}
            hop["q"] = min(ARC_FLIGHTS, key=lambda q: abs(ARC_FLIGHTS[q] - (1.3 + 1.1 * w)))
            hop["dur"] = ARC_FLIGHTS[hop["q"]]
            if src == "dc":
                t0 = 0.6 + i * 0.62
            else:  # earliest moment after the source is lit when the whole flight is in view
                t0 = self.landed[src] + 0.4
                while t0 + hop["dur"] < self.T_END - 4 and not (self.vis(hop, t0) >= 0.9 and self.vis(hop, t0 + hop["dur"]) >= 0.9):
                    t0 += 0.25
                if t0 + hop["dur"] >= self.T_END - 4:
                    print(f"  (arc {src}->{dst} skipped: never fully in view)")
                    continue
            hop["t0"], hop["tl"] = t0, t0 + hop["dur"]
            self.landed.setdefault(dst, hop["tl"])
            self.hops.append(hop)
        self.reach = {}
        for k, t in self.landed.items():
            r = "us" if k == "dc" else region(*HUBS[k])
            self.reach[r] = min(self.reach.get(r, self.P), t)
        self.reach["us"] = min(self.reach.get("us", 2.0), 2.0)

    def unit(self, key):
        lat, lon = HUBS[key]
        p, l = math.radians(lat), math.radians(lon + self.off)
        return (math.cos(p) * math.sin(l), math.sin(p), math.cos(p) * math.cos(l))

    def turn(self, V, t):
        al = 2 * math.pi * t / self.P
        X, Y, Z = V
        return (X * math.cos(al) + Z * math.sin(al), Y, Z * math.cos(al) - X * math.sin(al))

    def depth(self, V):
        X, Y, Z = V
        return Z * math.cos(self.a) + Y * math.sin(self.a)

    def point(self, hop, s):
        w, U0, U1 = hop["w"], hop["U0"], hop["U1"]
        V = [(math.sin((1 - s) * w) * p + math.sin(s * w) * q) / math.sin(w) for p, q in zip(U0, U1)]
        e = 1 + hop["lift"] * math.sin(math.pi * s)
        return tuple(c * e for c in V)

    def vis(self, hop, t):
        ground = tuple(c / (1 + hop["lift"]) for c in self.point(hop, 0.5))
        d = min(self.depth(self.turn(V, t)) for V in (hop["U0"], hop["U1"], ground))
        return max(0.0, min(1.0, (d - 0.15) / 0.2))

    def fade(self, t):
        return max(0.0, min(1.0, (self.T_END - 0.5 - t) / 3.0))

    def life(self, hop, t):  # a landed hop glows for a while, then hands the story on
        return max(0.0, min(1.0, (hop["tl"] + 18 - t) / 3))

    def lights_css(self) -> str:
        """Land dims at the start of each rotation and each region lights up when the network reaches it."""
        css = []
        for r, t in sorted(self.reach.items()):
            a, b = t / self.P * 100, min(t + 2.5, self.T_END - 4) / self.P * 100
            lo = ".42" if r != "us" else ".5"
            css.append(f"@keyframes L{r}{{0%,{f2(a)}%{{stroke-opacity:{lo}}}{f2(b)}%,{f2((self.T_END - 3.5) / self.P * 100)}%{{stroke-opacity:1}}"
                       f"{f2(self.T_END / self.P * 100)}%,100%{{stroke-opacity:{lo}}}}}.L{r}{{animation:L{r} {self.P}s linear infinite}}")
        return "".join(css)


def globe_arcs(doc: Doc, cx, cy, R, *, plan: ArcPlan, ks=1.0, clip_h=None) -> str:
    """Render the plan: arcs pinned to the spinning globe, drawn with a comet head, landing pings,
    a slow flow of traffic once landed, and a fade at the limb and at the end of each rotation.

    Each hop is an elevated great circle, sampled every 10 degrees of rotation into a cubic Bezier
    through four projected points; SMIL interpolates the `d` between samples. `clip_h` drops hops
    that never cross the visible card.
    """
    P, a = plan.P, plan.a

    def proj(V):
        X, Y, Z = V
        return cx + R * X, cy - R * (Y * math.cos(a) - Z * math.sin(a))

    def kt(t):
        return f"{t / P:.4f}".rstrip("0").rstrip(".")

    def pc(t):
        return f"{t / P * 100:.2f}%"

    ease = "animation-timing-function:cubic-bezier(.45,0,.2,1)"
    css = [f".aT,.aH,.aF{{stroke-linecap:round}}.aT{{stroke:{RED};stroke-opacity:.8;stroke-width:{f2(1.4 * ks)};stroke-dasharray:1 1}}"
           f".aH{{stroke:#fff;stroke-width:{f2(2.8 * ks)};stroke-dasharray:.06 2;opacity:0}}"
           f".aF{{stroke:#fff;stroke-width:{f2(1.8 * ks)};stroke-dasharray:.035 .3;opacity:0}}"
           f".aD{{fill:{RED}}}.aP{{stroke:#fff;stroke-width:{f2(1.2 * ks)};opacity:0;transform-box:fill-box;transform-origin:center}}"
           "@keyframes aF{to{stroke-dashoffset:-.335}}"]
    for q, D in ARC_FLIGHTS.items():
        tl = f"{P}s infinite both;animation-delay:var(--d)"
        css.append(
            f".aT.{q}{{animation:aT{q} {tl}}}@keyframes aT{q}{{0%{{stroke-dashoffset:1;{ease}}}{pc(D)},100%{{stroke-dashoffset:0}}}}"
            f".aH.{q}{{animation:aH{q} {tl}}}@keyframes aH{q}{{0%{{stroke-dashoffset:.06;opacity:1;{ease}}}{pc(D)}{{stroke-dashoffset:-1;opacity:1}}{pc(D + 0.1)},100%{{stroke-dashoffset:-1;opacity:0}}}}"
            f".aD.{q}{{animation:aD{q} {tl}}}@keyframes aD{q}{{0%,{pc(D - 0.02)}{{opacity:0}}{pc(D + 0.15)},100%{{opacity:1}}}}"
            f".aP.{q}{{animation:aP{q} {tl}}}@keyframes aP{q}{{0%,{pc(D - 0.02)}{{opacity:0;transform:scale(.15)}}{pc(D)}{{opacity:.95;transform:scale(.15)}}{pc(D + 1.4)},100%{{opacity:0;transform:scale(1)}}}}"
            f".aF.{q}{{animation:aF 3.2s linear infinite,aG{q} {P}s infinite both;animation-delay:0s,var(--d)}}"
            f"@keyframes aG{q}{{0%,{pc(D + 0.3)}{{opacity:0}}{pc(D + 1.2)},100%{{opacity:.75}}}}")
    doc.css.append("".join(css))
    out = []
    loop = f'dur="{P}s" repeatCount="indefinite"'
    for hop in plan.hops:
        t0, tl = hop["t0"], hop["tl"]
        pts = [plan.point(hop, s) for s in (0, 1 / 3, 2 / 3, 1)]
        ts, t = [], t0
        while t <= plan.T_END:
            ts.append(t)
            if t > tl and plan.vis(hop, t) * plan.fade(t) * plan.life(hop, t) == 0:
                break
            t += P / 36
        shapes, ops, ends = [], [], []
        onscreen = False
        for t in ts:
            p0, p1, p2, p3 = (proj(plan.turn(V, t)) for V in pts)
            c1 = [(-5 * p0[k] + 18 * p1[k] - 9 * p2[k] + 2 * p3[k]) / 6 for k in (0, 1)]
            c2 = [(2 * p0[k] - 9 * p1[k] + 18 * p2[k] - 5 * p3[k]) / 6 for k in (0, 1)]
            shapes.append(f"M{p0[0]:.0f} {p0[1]:.0f}C{c1[0]:.0f} {c1[1]:.0f} {c2[0]:.0f} {c2[1]:.0f} {p3[0]:.0f} {p3[1]:.0f}")
            o = plan.vis(hop, t) * plan.fade(t) * plan.life(hop, t)
            ops.append(f2(o))
            ends.append(f"{p3[0]:.0f} {p3[1]:.0f}")
            if o > 0 and (clip_h is None or min(p0[1], p1[1], p2[1], p3[1]) < clip_h - 8):
                onscreen = True
        if not onscreen:
            continue
        ops[-1] = "0"
        keys = ";".join(["0", kt(t0 - 0.05)] + [kt(t) for t in ts] + ["1"])

        def seq(vals, first, last):
            return ";".join([first, first] + vals + [last])

        pid, q = doc.uid("arc"), hop["q"]
        doc.defs.append(f'<path id="{pid}" pathLength="1" d="{shapes[0]}">'
                        f'<animate attributeName="d" values="{seq(shapes, shapes[0], shapes[-1])}" keyTimes="{keys}" {loop}/></path>')
        out.append(
            f'<g opacity="0" style="--d:{t0:.2f}s"><animate attributeName="opacity" values="{seq(ops, "0", "0")}" keyTimes="{keys}" {loop}/>'
            f'<use href="#{pid}" class="aT {q}"/><use href="#{pid}" class="aH {q}"/><use href="#{pid}" class="aF {q}"/>'
            f'<g transform="translate({ends[0]})"><animateTransform attributeName="transform" type="translate" '
            f'values="{seq(ends, ends[0], ends[-1])}" keyTimes="{keys}" {loop}/>'
            f'<circle class="aD {q}" r="{f2(2.6 * ks)}"/><circle class="aP {q}" r="{f2(14 * ks)}"/></g></g>')
    print(f"  ({len(out)} arcs drawn, {len(plan.landed)} hubs lit, regions {', '.join(f'{k}@{v:.0f}s' for k, v in sorted(plan.reach.items(), key=lambda kv: kv[1]))})")
    return "".join(out)


# ─────────────────────────────────────────────────────────────── shared pieces
BACKDROP_END = "</g>"


def frame(doc: Doc, w, h, rx=28, bg=INK) -> tuple[str, str]:
    """Rounded card with a light-catching top edge. Returns (open, close).

    `open` paints the background and opens a clip group for the static backdrop only; callers end
    it with BACKDROP_END before adding animated content (clipping SMIL-animated content makes
    Chrome's CPU rasterizer drop 256px tiles).
    """
    cid = doc.uid("cf")
    doc.defs.append(
        f'<clipPath id="{cid}"><rect width="{w}" height="{h}" rx="{rx}"/></clipPath>'
        f'<linearGradient id="{cid}e" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#FFFFFF" stop-opacity=".17"/>'
        f'<stop offset=".3" stop-color="#FFFFFF" stop-opacity=".08"/><stop offset="1" stop-color="#FFFFFF" stop-opacity=".055"/></linearGradient>'
    )
    op = f'<rect width="{w}" height="{h}" rx="{rx}" fill="{bg}"/><g clip-path="url(#{cid})">'
    cl = f'<rect x=".5" y=".5" width="{w - 1}" height="{h - 1}" rx="{f2(rx - 0.5)}" stroke="url(#{cid}e)"/>'
    return op, cl


def glow(doc: Doc, x, y, r, color, op) -> str:
    gid = doc.uid("gw")
    doc.defs.append(f'<radialGradient id="{gid}"><stop offset="0" stop-color="{color}" stop-opacity="{op}"/>'
                    f'<stop offset=".5" stop-color="{color}" stop-opacity="{f2(op * 0.3)}"/>'
                    f'<stop offset="1" stop-color="{color}" stop-opacity="0"/></radialGradient>')
    return f'<circle cx="{f2(x)}" cy="{f2(y)}" r="{f2(r)}" fill="url(#{gid})"/>'


def dots_bg(doc: Doc, w, h, cx, cy, r, gap=22, op=0.16) -> str:
    """Fine dot grid fading out from (cx, cy)."""
    pid, mid = doc.uid("dp"), doc.uid("dm")
    doc.defs.append(
        f'<pattern id="{pid}" width="{gap}" height="{gap}" patternUnits="userSpaceOnUse">'
        f'<circle cx="{gap / 2}" cy="{gap / 2}" r=".9" fill="#FFFFFF"/></pattern>'
        f'<radialGradient id="{mid}g" gradientUnits="userSpaceOnUse" cx="{f2(cx)}" cy="{f2(cy)}" r="{f2(r)}">'
        f'<stop offset="0" stop-color="#fff" stop-opacity="{op}"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient>'
        f'<mask id="{mid}"><rect width="{w}" height="{h}" fill="url(#{mid}g)"/></mask>')
    return f'<rect width="{w}" height="{h}" fill="url(#{pid})" mask="url(#{mid})"/>'


def arrow_ne(x, y, s, color, sw=1.6) -> str:
    return (f'<path d="M{f2(x)} {f2(y + s)}L{f2(x + s)} {f2(y)}M{f2(x + s * 0.3)} {f2(y)}H{f2(x + s)}V{f2(y + s * 0.7)}" '
            f'stroke="{color}" stroke-width="{f2(sw)}" stroke-linecap="round" stroke-linejoin="round"/>')


def arrow_e(x, y, s, color, sw=2.0) -> str:
    return (f'<path d="M{f2(x)} {f2(y)}H{f2(x + s)}M{f2(x + s * 0.6)} {f2(y - s * 0.4)}L{f2(x + s)} {f2(y)}L{f2(x + s * 0.6)} {f2(y + s * 0.4)}" '
            f'stroke="{color}" stroke-width="{f2(sw)}" stroke-linecap="round" stroke-linejoin="round"/>')


def both(fn):
    """Build the desktop asset and its phone variant."""
    def run():
        fn(False)
        fn(True)
    return run


def suffix(m: bool) -> str:
    return "-m" if m else ""


# ═══════════════════════════════════════════════════════════════ HERO
@both
def build_hero(m: bool):
    W, H = (720, 980) if m else (1200, 600)
    doc = Doc(W, H, "Alex Leschik — Founder, ZEN AI Co.",
              "Alex Leschik, founder of ZEN AI Co. in Washington, D.C. Building Arsenal, an agentic AI platform, "
              "and the first youth AI literacy program in U.S. history. On a rotating dotted Earth, flight arcs launch from "
              "Washington, D.C., cross the country, then spread around the world, lighting up each region as they arrive.")
    op, cl = frame(doc, W, H, 32 if m else 28)
    gcx, gcy, R = (360, 974, 322) if m else (892, 300, 226)
    doc.add(op,
            glow(doc, gcx, gcy, R * 1.8, BLUE, .15),
            glow(doc, 0, H * 0.55, 560 if m else 480, RED, .05),
            dots_bg(doc, W, H, W * 0.42 if not m else W / 2, 260 if m else 300, 560 if m else 640, op=.11),
            BACKDROP_END)
    doc.add(globe(doc, gcx, gcy, R, lat_range=(-2, 78) if m else (-56, 78), label_scale=1.5 if m else 1.0,
                  clip_h=H if m else None))

    if not m:
        X, lh = 64, 28
        doc.add(laser_logo(doc, X, 52, lh))
        doc.add(doc.text(X + logo_width(lh) + 14, 73.5, "ZEN AI Co.", "sansS", 19, WHITE))
        doc.add(doc.text(W - 64, 72, "zenai.world", "mono", 13, MUTED, anchor="end"))
        doc.add(f'<g class="{rise(doc, .05, 10)}"><circle cx="{X + 4}" cy="182" r="4" fill="{RED}"/>'
                + doc.text(X + 18, 186.5, "FOUNDER  ·  WASHINGTON, D.C.", "mono", 13, MUTED, ls=2.2) + "</g>")
        ns = 110
        doc.add(f'<g class="{rise(doc, .14)}">{doc.text(X - 5, 290, "Alex", "disp", ns, WHITE)}</g>')
        doc.add(f'<g class="{rise(doc, .26)}">{doc.text(X - 5, 392, "Leschik", "disp", ns, WHITE)}</g>')
        s = 21
        l1 = [("Building ", TEXT2, "sans", s), ("Arsenal", WHITE, "sansM", s), (", an agentic AI platform, and the", TEXT2, "sans", s)]
        l2 = [("first", WHITE, "serif", s * 1.24), (" youth AI literacy program in U.S. history.", TEXT2, "sans", s)]
        c = rise(doc, .38, 12)
        doc.add(f'<g class="{c}">{doc.runs(X, 450, l1)}{doc.runs(X, 482, l2)}</g>')
        facts = [("PLATFORM", "Arsenal"), ("PROGRAM", "AI Pioneer · Year 4"), ("RECOGNITION", "Presidential AI Challenge")]
        x, g = X, []
        for k, (lab, val) in enumerate(facts):
            if k:
                g.append(f'<rect x="{f2(x - 22)}" y="520" width="1" height="42" fill="{LINE2}"/>')
            g.append(doc.text(x, 532, lab, "mono", 11, DIM, ls=1.8))
            g.append(doc.text(x, 556, val, "sansM", 15.5, WHITE))
            x += max(width("sansM", val, 15.5), FB.width("mono", lab, 11, 1.8)) + 44
        doc.add(f'<g class="{rise(doc, .5, 8)}">{"".join(g)}</g>')
    else:
        X, lh = 56, 38
        doc.add(laser_logo(doc, X, 58, lh))
        doc.add(doc.text(X + logo_width(lh) + 16, 87.5, "ZEN AI Co.", "sansS", 27, WHITE))
        doc.add(f'<g class="{rise(doc, .05, 10)}"><circle cx="{X + 6}" cy="190" r="6" fill="{RED}"/>'
                + doc.text(X + 24, 196.5, "FOUNDER  ·  WASHINGTON, D.C.", "mono", 19, MUTED, ls=2.6) + "</g>")
        ns = 122
        doc.add(f'<g class="{rise(doc, .14)}">{doc.text(X - 6, 318, "Alex", "disp", ns, WHITE)}</g>')
        doc.add(f'<g class="{rise(doc, .26)}">{doc.text(X - 6, 432, "Leschik", "disp", ns, WHITE)}</g>')
        s = 28
        lines = [
            [("Building ", TEXT2, "sans", s), ("Arsenal", WHITE, "sansM", s), (", an agentic AI", TEXT2, "sans", s)],
            [("platform, and the ", TEXT2, "sans", s), ("first", WHITE, "serif", s * 1.24), (" youth AI", TEXT2, "sans", s)],
            [("literacy program in U.S. history.", TEXT2, "sans", s)],
        ]
        c = rise(doc, .38, 12)
        doc.add(f'<g class="{c}">' + "".join(doc.runs(X, 506 + i * 40, ln) for i, ln in enumerate(lines)) + "</g>")
    doc.add(cl)
    doc.save(f"hero{suffix(m)}.svg")


# ═══════════════════════════════════════════════════════════════ SECTION HEADERS
SECTIONS = [
    ("01", "Impact"),
    ("02", "Platform"),
    ("03", "AI Pioneer Program"),
    ("04", "Recognition"),
    ("05", "Work together"),
    ("06", "Connect"),
]


def build_sections():
    for num, title in SECTIONS:
        for m in (False, True):
            W, H = (720, 104) if m else (1200, 88)
            doc = Doc(W, H, f"{num} — {title}", f"Section {num}: {title}")
            op, cl = frame(doc, W, H, 22 if m else 20)
            doc.add(op, BACKDROP_END)
            ns, ts = (20, 40) if m else (14.5, 30)
            cy = H / 2
            doc.add(doc.text(36, cy + ns * 0.365, num, "monoB", ns, RED))
            tx = 36 + FB.width("monoB", num, ns) + (22 if m else 18)
            doc.add(doc.text(tx, cy + ts * 0.364, title, "disp", ts, WHITE))
            x0, x1 = tx + width("disp", title, ts) + 28, W - 36
            if x1 - x0 > 48:
                L = x1 - x0
                doc.css.append(f"@keyframes dw{{from{{stroke-dashoffset:{f2(L)}}}}}"
                               f".dw{{stroke-dasharray:{f2(L)};animation:dw 1.4s cubic-bezier(.6,0,.2,1) .2s both}}")
                gid, cid, gw = doc.uid("gl"), doc.uid("gc"), (90 if m else 130)
                doc.defs.append(
                    f'<linearGradient id="{gid}"><stop offset="0" stop-color="#fff" stop-opacity="0"/>'
                    f'<stop offset=".7" stop-color="#fff" stop-opacity=".9"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>'
                    f'<clipPath id="{cid}"><rect x="{f2(x0)}" y="{f2(cy - 4)}" width="{f2(L)}" height="8"/></clipPath>')
                run = 'dur="7s" begin="1.8s" repeatCount="indefinite"'
                doc.add(f'<path class="dw" d="M{f2(x0)} {f2(cy)}H{f2(x1)}" stroke="{LINE2}" stroke-width="1.2"/>'
                        f'<g clip-path="url(#{cid})"><rect x="{f2(x0 - gw)}" y="{f2(cy - 1)}" width="{gw}" height="2" fill="url(#{gid})">'
                        f'<animate attributeName="x" values="{f2(x0 - gw)};{f2(x1)};{f2(x1)}" keyTimes="0;.4;1" {run}/></rect></g>'
                        f'<circle cx="{f2(x1)}" cy="{f2(cy)}" r="{2.5 if m else 2}" fill="{DIM}">'
                        f'<animate attributeName="fill" values="{DIM};{DIM};{RED};{RED};{DIM}" keyTimes="0;.38;.41;.55;1" {run}/>'
                        f'<animate attributeName="r" values="{2.5 if m else 2};{2.5 if m else 2};{4 if m else 3.2};{2.5 if m else 2}" keyTimes="0;.38;.42;1" {run}/></circle>')
            doc.add(cl)
            doc.save(f"section-{num}{suffix(m)}.svg")


# ═══════════════════════════════════════════════════════════════ IMPACT / ODOMETERS
def odometer(doc: Doc, x, y, text, *, size, font="disp", fill=WHITE, T=16.0, start=0.3,
             stagger=0.14, roll=1.9) -> tuple[str, float]:
    """Digits roll in like a mechanical counter, hold, then the loop resets while faded out."""
    lh = size * 1.15
    ls = track(font, size)
    parts, cx, col = [], x, 0
    for ch in text:
        adv = FB.advance(font, ch) * size + ls
        if ch.isdigit():
            target = int(ch)
            seq = [str(k % 10) for k in range(10 + target + 1)]
            final = (len(seq) - 1) * lh
            kf = doc.uid("od")
            s0 = (start + col * stagger) / T * 100
            s1 = (start + col * stagger + roll) / T * 100
            doc.css.append(
                f"@keyframes {kf}{{0%,{f2(s0)}%{{transform:translateY(0);animation-timing-function:cubic-bezier(.16,.84,.24,1)}}"
                f"{f2(s1)}%,100%{{transform:translateY(-{f2(final)}px)}}}}.{kf}{{animation:{kf} {T}s infinite}}")
            doc.need(font, "0123456789")
            tsp = "".join(f'<tspan x="{f2(cx + adv / 2)}" y="{f2(y + k * lh)}">{d}</tspan>' for k, d in enumerate(seq))
            parts.append(f'<g class="{kf}"><text class="f-{font}" font-size="{f2(size)}" fill="{fill}" text-anchor="middle">{tsp}</text></g>')
            col += 1
        else:
            parts.append(doc.text(cx, y, ch, font, size, fill))
        cx += adv
    cid = doc.uid("odc")
    doc.defs.append(f'<clipPath id="{cid}"><rect x="{f2(x - 8)}" y="{f2(y - size * 0.98)}" width="{f2(cx - x + 16)}" height="{f2(size * 1.24)}"/></clipPath>')
    doc.css.append(f"@keyframes odf{{0%{{opacity:0}}2%,95%{{opacity:1}}100%{{opacity:0}}}}.odf{{animation:odf {T}s infinite}}")
    return f'<g clip-path="url(#{cid})"><g class="odf">{"".join(parts)}</g></g>', cx - x


STATS = [  # (label, digits, suffix, desktop caption lines, phone caption lines)
    ("HISTORY", "1", "st", ["Youth AI literacy program", "in U.S. history"],
     ["Youth AI literacy", "program in U.S. history"]),
    ("REACH", "33", "K+", ["Young people reached,", "ages 11–18"], ["Young people reached,", "ages 11–18"]),
    ("FOOTPRINT", "42", "", ["States reached", "and expanding"], ["States reached", "and expanding"]),
    ("MOMENTUM", "4", "th", ["Year with Boys & Girls Clubs", "of Greater Washington, 2027"],
     ["Year with Boys & Girls", "Clubs of Greater", "Washington, 2027"]),
]


def _stat_viz(doc: Doc, i, x, y, w, m, start, T=16.0) -> str:
    """Small chart under each number, timed to the odometer loop."""
    doc.css.append(f"@keyframes lt{{0%{{opacity:.16}}3%,92%{{opacity:1}}97%,100%{{opacity:.16}}}}"
                   f".lt{{opacity:.16;animation:lt {T}s infinite both}}")
    s = 1.3 if m else 1.0

    def dl(t):
        return f'style="animation-delay:{f2(t)}s"'

    out = []
    if i == 0:  # the origin point everything runs out from
        gid = doc.uid("og")
        doc.defs.append(f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" x1="{f2(x)}" x2="{f2(x + w)}">'
                        f'<stop offset="0" stop-color="{RED}"/><stop offset="1" stop-color="{RED}" stop-opacity="0"/></linearGradient>')
        doc.css.append(f"@keyframes ln{{0%{{stroke-dashoffset:1;opacity:1}}16%,92%{{stroke-dashoffset:0;opacity:1}}97%,100%{{stroke-dashoffset:0;opacity:0}}}}"
                       f".ln{{stroke-dasharray:1 1;animation:ln {T}s cubic-bezier(.3,.7,.2,1) infinite both}}")
        line = f'd="M{f2(x)} {f2(y)}H{f2(x + w)}" stroke-width="{f2(2 * s)}" stroke-linecap="round"'
        out.append(f'<path {line} stroke="{LINE}"/><path class="ln" {dl(start)} pathLength="1" {line} stroke="url(#{gid})"/>')
        out.append(f'<circle cx="{f2(x)}" cy="{f2(y)}" r="{f2(4.5 * s)}" stroke="{RED}" stroke-width="{f2(1.4 * s)}" opacity="0">'
                   f'<animate attributeName="r" values="{f2(4.5 * s)};{f2(17 * s)}" dur="2.4s" repeatCount="indefinite"/>'
                   f'<animate attributeName="opacity" values=".8;0" dur="2.4s" repeatCount="indefinite"/></circle>'
                   f'<circle cx="{f2(x)}" cy="{f2(y)}" r="{f2(4.5 * s)}" fill="{RED}"/>')
    elif i == 1:  # one tick per thousand
        n = 33
        step = w / n
        tw, h = step * 0.42, 14 * s
        for j in range(n):
            out.append(f'<rect class="lt" {dl(start + 0.2 + j * 0.04)} x="{f2(x + j * step)}" y="{f2(y - h / 2)}" '
                       f'width="{f2(tw)}" height="{f2(h)}" rx="{f2(tw / 2)}" fill="{RED if j == n - 1 else WHITE}"/>')
    elif i == 2:  # 42 of 50 states
        cols = 25
        step = w / cols
        sq, gap = step * 0.6, 3.2 * s
        for j in range(50):
            cx_, cy_ = x + (j % cols) * step, y - sq - gap / 2 + (j // cols) * (sq + gap)
            box = f'x="{f2(cx_)}" y="{f2(cy_)}" width="{f2(sq)}" height="{f2(sq)}" rx="{f2(sq * 0.28)}"'
            if j < 42:
                out.append(f'<rect class="lt" {dl(start + 0.2 + j * 0.03)} {box} fill="{RED if j == 41 else WHITE}"/>')
            else:
                out.append(f'<rect {box} fill="{LINE2}"/>')
    else:  # four consecutive years
        gap, h = 8 * s, 6 * s
        sw_ = (w - 3 * gap) / 4
        for j in range(4):
            sx = x + j * (sw_ + gap)
            out.append(f'<rect class="lt" {dl(start + 0.3 + j * 0.35)} x="{f2(sx)}" y="{f2(y - h / 2)}" width="{f2(sw_)}" '
                       f'height="{f2(h)}" rx="{f2(h / 2)}" fill="{RED if j == 3 else WHITE}"/>')
            out.append(doc.text(sx, y + 20 * s, f"’{24 + j}", "mono", 11 * s, TEXT2 if j == 3 else DIM))
    return "".join(out)


@both
def build_signal(m: bool):
    W, H = (720, 744) if m else (1200, 318)
    doc = Doc(W, H, "Impact — ZEN AI Co. by the numbers",
              "1st youth AI literacy program in U.S. history · 33,000+ young people reached, ages 11–18 · "
              "42 states · 4th year with Boys & Girls Clubs of Greater Washington in 2027.")
    op, cl = frame(doc, W, H, 26 if m else 24)
    doc.add(op, glow(doc, 0, 0, 560, RED, .07), glow(doc, W, H, 640, BLUE, .09), BACKDROP_END)
    pad = 28
    cols = 2 if m else 4
    cw = (W - 2 * pad) / cols
    ch = (H - 2 * pad) / (2 if m else 1)
    if m:
        doc.add(f'<path d="M{W / 2} {pad + 24}V{H - pad - 24}M{pad + 24} {H / 2}H{W - pad - 24}" stroke="{LINE}" stroke-width="1.5"/>')
    else:
        doc.add("".join(f'<path d="M{f2(pad + k * cw)} {pad + 18}V{H - pad - 18}" stroke="{LINE}" stroke-width="1.5"/>' for k in (1, 2, 3)))
    ls_, ns, cs, clh = (18, 96, 23, 31) if m else (12, 76, 16, 23)
    for i, (lab, num, suf, cap_d, cap_m) in enumerate(STATS):
        r, c = divmod(i, cols)
        x0, y0 = pad + c * cw, pad + r * ch
        ix = x0 + (34 if m else 32)
        ly, ny, cy = (y0 + 60, y0 + 166, y0 + 214) if m else (y0 + 44, y0 + 136, y0 + 178)
        doc.add(f'<circle cx="{f2(ix + 3)}" cy="{f2(ly - ls_ * 0.36)}" r="{3.5 if m else 3}" fill="{RED}"/>')
        doc.add(doc.text(ix + (16 if m else 13), ly, lab, "mono", ls_, MUTED, ls=2.2))
        od, w = odometer(doc, ix - 2, ny, num, size=ns, start=0.3 + i * 0.18)
        doc.add(od)
        x = ix - 2 + w
        ss = ns * 0.42
        sup_y = ny - 0.727 * ns + 0.74 * ss
        if suf == "K+":
            doc.add(doc.text(x, ny, "K", "disp", ns, WHITE))
            doc.add(doc.text(x + width("disp", "K", ns) + ns * 0.03, sup_y, "+", "disp", ss, RED))
        elif suf:
            doc.add(doc.text(x + ns * 0.03, sup_y, suf, "disp", ss, MUTED))
        for k, line in enumerate(cap_m if m else cap_d):
            doc.add(doc.text(ix, cy + k * clh, line, "sans", cs, TEXT2))
        vy, vw = (y0 + 312, cw - 62) if m else (y0 + 242, cw - 58)
        doc.add(_stat_viz(doc, i, ix, vy, vw, m, 0.3 + i * 0.18))
    doc.add(cl)
    doc.save(f"signal{suffix(m)}.svg")


# ═══════════════════════════════════════════════════════════════ ECOSYSTEM MAP
ECO_INNER = ["Arsenal", "ZEN AI Arena", "Qubit", "AI Pioneer"]
ECO_MID = ["ZEN Pen", "Vibe Book", "Agent Viz", "ZEN Avatar", "Analytics", "Life-in-Time", "Periodic", "Physics"]
ECO_OUTER = ["Solutions", "Tools", "Challenges", "Leaderboard", "ZEN Weekly", "Blog", "Media Hub", "Groups",
             "Discord", "Telegram"]


@both
def build_ecosystem(m: bool):
    W, H = (720, 800) if m else (1200, 600)
    CX, CY = (360, 376) if m else (600, 292)
    doc = Doc(W, H, "The ZEN ecosystem — system map",
              "Arsenal, ZEN AI Arena, Qubit and AI Pioneer orbit the ZEN core; ZEN Pen, Vibe Book, Agent Viz, ZEN Avatar, "
              "Analytics, Life-in-Time, Periodic and Physics form the studio ring; Solutions, Tools, Challenges, "
              "Leaderboard, ZEN Weekly, Blog, Media Hub, Groups, Discord and Telegram form the community ring.")
    op, cl = frame(doc, W, H, 26)
    doc.add(op, glow(doc, CX, CY, 520, BLUE, .14), dots_bg(doc, W, H, CX, CY, 560, op=.09), BACKDROP_END)
    rings = ([(150, 150, -150, ECO_INNER), (256, 256, -135, ECO_MID), (334, 334, -162, ECO_OUTER)] if m else
             [(206, 112, -150, ECO_INNER), (352, 186, -135, ECO_MID), (500, 240, -162, ECO_OUTER)])
    doc.css.append("@keyframes rdd{to{stroke-dashoffset:-480}}.rdd{animation:rdd 90s linear infinite}")
    nodes = []
    for ri, (rx, ry, a0, items) in enumerate(rings):
        doc.add(f'<ellipse cx="{CX}" cy="{CY}" rx="{rx}" ry="{ry}" stroke="#FFFFFF" stroke-opacity="{(.11, .08, .06)[ri]}"/>')
        if ri == 1:
            doc.add(f'<ellipse class="rdd" cx="{CX}" cy="{CY}" rx="{rx}" ry="{ry}" stroke="#FFFFFF" stroke-opacity=".28" stroke-dasharray="1.5 14" stroke-linecap="round" stroke-width="1.6"/>')
        for k, name in enumerate(items):
            ang = math.radians(a0 + k * 360 / len(items))
            nodes.append((ri, name, CX + rx * math.cos(ang), CY + ry * math.sin(ang), ang))
    # radar sweep: a fading wedge circles the core; every node pings as the beam crosses it
    k_ = rings[-1][1] / rings[-1][0]
    Rs = rings[-1][0] + (14 if m else 26)
    TS = 10.0
    wg = doc.uid("wg")
    doc.defs.append(f'<linearGradient id="{wg}" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="{f2(-0.34 * Rs)}">'
                    f'<stop offset="0" stop-color="#FFFFFF" stop-opacity=".13"/><stop offset=".35" stop-color="#FFFFFF" stop-opacity=".05"/>'
                    f'<stop offset="1" stop-color="#FFFFFF" stop-opacity="0"/></linearGradient>')
    sl = [f'<path d="M0 0H{Rs}A{Rs} {Rs} 0 0 0 0 {-Rs}Z" fill="url(#{wg})"/>',
          f'<path d="M0 0H{Rs}" stroke="#FFFFFF" stroke-opacity=".4" vector-effect="non-scaling-stroke"/>']
    doc.add(f'<g transform="translate({CX} {CY}) scale(1 {f4(k_)})"><g>'
            f'<animateTransform attributeName="transform" type="rotate" from="0" to="360" dur="{TS}s" repeatCount="indefinite"/>'
            f'{"".join(sl)}</g></g>')
    # spokes + packets to the four platforms
    for k, (ri, name, nx, ny, ang) in enumerate([n for n in nodes if n[0] == 0]):
        doc.add(f'<path d="M{CX} {CY}L{f2(nx)} {f2(ny)}" stroke="#FFFFFF" stroke-opacity=".10"/>')
        d = f"M{CX} {CY}L{f2(nx)} {f2(ny)}" if k % 2 == 0 else f"M{f2(nx)} {f2(ny)}L{CX} {CY}"
        dur = 3.4
        doc.add(f'<circle r="{3.6 if m else 2.6}" fill="{RED if k % 2 == 0 else WHITE}">'
                f'<animateMotion path="{d}" dur="{dur}s" begin="-{f2(k * 0.85)}s" repeatCount="indefinite"/>'
                f'<animate attributeName="opacity" values="0;1;1;0" keyTimes="0;.2;.8;1" dur="{dur}s" begin="-{f2(k * 0.85)}s" repeatCount="indefinite"/></circle>')
    halo = f'stroke="{INK}" stroke-width="{6 if m else 5}" stroke-opacity=".92" paint-order="stroke" stroke-linejoin="round"'
    for idx, (ri, name, nx, ny, ang) in enumerate(nodes):
        th = math.degrees(math.atan2((ny - CY) / k_, nx - CX)) % 360
        pr = (15, 11, 8)[ri] * (1.35 if m else 1)
        run = f'dur="{TS}s" begin="{f2(th / 360 * TS)}s" repeatCount="indefinite"'
        doc.add(f'<circle cx="{f2(nx)}" cy="{f2(ny)}" r="{f2(pr)}" stroke="{RED if ri == 0 else WHITE}" stroke-width="{1.8 if m else 1.4}" opacity="0">'
                f'<animate attributeName="opacity" values=".85;0;0" keyTimes="0;.16;1" {run}/>'
                f'<animate attributeName="r" values="{f2(pr * 0.35)};{f2(pr * 1.7)};{f2(pr * 1.7)}" keyTimes="0;.16;1" {run}/></circle>')
        if ri == 0:
            doc.add(f'<circle cx="{f2(nx)}" cy="{f2(ny)}" r="{18 if m else 14}" fill="#FFFFFF" fill-opacity=".05"/>'
                    f'<circle cx="{f2(nx)}" cy="{f2(ny)}" r="{8 if m else 6}" fill="{INK}" stroke="{WHITE}" stroke-width="2"/>'
                    f'<circle cx="{f2(nx)}" cy="{f2(ny)}" r="{3 if m else 2.4}" fill="{RED}">'
                    f'<animate attributeName="opacity" values="1;.35;1" dur="{f2(2.2 + idx * 0.3)}s" repeatCount="indefinite"/></circle>')
        elif ri == 1:
            doc.add(f'<circle cx="{f2(nx)}" cy="{f2(ny)}" r="{5.5 if m else 4}" fill="{INK}" stroke="{TEXT2}" stroke-width="1.6"/>')
        else:
            doc.add(f'<circle cx="{f2(nx)}" cy="{f2(ny)}" r="{4.5 if m else 3.2}" fill="{DIM}"/>')
        if m and ri == 2:
            continue
        font, size, col = [("sansS", 17, WHITE), ("sansM", 14.5, TEXT2), ("sans", 13.5, MUTED)][ri]
        if m:
            font, size, col = [("sansS", 26, WHITE), ("sansM", 21, TEXT2)][ri]
            lx, ly, anchor = nx, ny + (size + (16 if ri == 0 else 12)), "middle"
        else:
            c, s_ = math.cos(ang), math.sin(ang)
            off = (12, 10, 9)[ri]
            if abs(c) < 0.35:
                lx, ly, anchor = nx, ny + (off + size * 0.95 if s_ > 0 else -off - 2), "middle"
            else:
                lx, ly, anchor = nx + (off if c > 0 else -off), ny + size * 0.36, ("start" if c > 0 else "end")
        doc.add(doc.text(lx, ly, name, font, size, col, anchor=anchor, attrs=halo))
    # core
    cr = 66 if m else 58
    doc.add(f'<circle cx="{CX}" cy="{CY}" r="{cr + 12}" stroke="#FFFFFF" stroke-opacity=".08"/>'
            f'<circle cx="{CX}" cy="{CY}" r="{cr + 12}" stroke="{RED}" stroke-width="1.6" stroke-linecap="round" stroke-dasharray="54 {f2(2 * math.pi * (cr + 12) - 54)}">'
            f'<animateTransform attributeName="transform" type="rotate" from="0 {CX} {CY}" to="360 {CX} {CY}" dur="12s" repeatCount="indefinite"/></circle>'
            f'<circle cx="{CX}" cy="{CY}" r="{cr}" fill="{INK2}" stroke="{LINE2}"/>')
    lh_ = 48 if m else 42
    doc.add(laser_logo(doc, CX - logo_width(lh_) / 2, CY - lh_ / 2, lh_, dur=12, draw_frac=0.2))
    if m:
        doc.add(doc.text(W / 2, H - 34, "+ 10 more across community, media and research", "sans", 21, MUTED, anchor="middle"))
    else:
        x = 40
        for name, col in (("Platforms", WHITE), ("Studio & labs", TEXT2), ("Community & media", DIM)):
            doc.add(f'<circle cx="{x + 4}" cy="{H - 36}" r="4" fill="{col}"/>')
            doc.add(doc.text(x + 16, H - 31.5, name, "sans", 13, MUTED))
            x += 16 + width("sans", name, 13) + 26
        doc.add(doc.text(W - 40, H - 32, "22 PRODUCTS & CHANNELS", "mono", 11, DIM, anchor="end", ls=1.8))
    doc.add(cl)
    doc.save(f"ecosystem{suffix(m)}.svg")


# ═══════════════════════════════════════════════════════════════ PRODUCT TILES
def _glyph(doc: Doc, kind, cx, cy) -> str:
    loop = 'repeatCount="indefinite"'
    if kind == "arsenal":
        return laser_logo(doc, cx - logo_width(32) / 2, cy - 16, 32, dur=9, draw_frac=0.24)
    if kind == "arena":  # two models drift together and apart
        return (f'<circle cx="{cx - 8}" cy="{cy}" r="15" stroke="{WHITE}" stroke-width="2.2">'
                f'<animate attributeName="cx" values="{cx - 8};{cx - 13};{cx - 8}" dur="3.6s" calcMode="spline" keySplines=".45 0 .55 1;.45 0 .55 1" {loop}/></circle>'
                f'<circle cx="{cx + 8}" cy="{cy}" r="15" stroke="{RED}" stroke-width="2.2">'
                f'<animate attributeName="cx" values="{cx + 8};{cx + 13};{cx + 8}" dur="3.6s" calcMode="spline" keySplines=".45 0 .55 1;.45 0 .55 1" {loop}/></circle>')
    if kind == "qubit":  # the state vector precesses
        return (f'<circle cx="{cx}" cy="{cy}" r="19" stroke="{WHITE}" stroke-width="2"/>'
                f'<ellipse cx="{cx}" cy="{cy}" rx="19" ry="6.5" stroke="{MUTED}" stroke-width="1.4"/>'
                f'<g><animateTransform attributeName="transform" type="rotate" from="0 {cx} {cy}" to="360 {cx} {cy}" dur="6s" {loop}/>'
                f'<path d="M{cx} {cy}L{cx + 10} {cy - 13}" stroke="{RED}" stroke-width="2.2" stroke-linecap="round"/>'
                f'<circle cx="{cx + 10}" cy="{cy - 13}" r="3.4" fill="{RED}"/></g><circle cx="{cx}" cy="{cy}" r="2.2" fill="{WHITE}"/>')
    if kind == "pioneer":  # the trajectory draws, then the endpoint lands
        return (f'<path pathLength="1" stroke-dasharray="1 1" d="M{cx - 19} {cy + 14}L{cx - 6} {cy + 1}L{cx + 3} {cy + 8}L{cx + 17} {cy - 10}" '
                f'stroke="{WHITE}" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">'
                f'<animate attributeName="stroke-dashoffset" values="1;0;0;0" keyTimes="0;.35;.9;1" dur="4s" {loop}/>'
                f'<animate attributeName="opacity" values="1;1;1;0" keyTimes="0;.35;.9;1" dur="4s" {loop}/></path>'
                f'<circle cx="{cx + 17}" cy="{cy - 10}" r="4" fill="{RED}" opacity="0">'
                f'<animate attributeName="opacity" values="0;0;1;1;0" keyTimes="0;.33;.38;.9;1" dur="4s" {loop}/></circle>'
                f'<circle cx="{cx + 17}" cy="{cy - 10}" r="4" stroke="{RED}" stroke-width="1.5" opacity="0">'
                f'<animate attributeName="opacity" values="0;0;.9;0;0" keyTimes="0;.36;.38;.6;1" dur="4s" {loop}/>'
                f'<animate attributeName="r" values="4;4;4;13;13" keyTimes="0;.36;.38;.6;1" dur="4s" {loop}/></circle>')
    if kind == "solutions":  # layers breathe apart in sequence
        out = []
        for k, col in enumerate((DIM, MUTED, RED)):
            y = cy + 10 - k * 10
            out.append(f'<path d="M{cx} {y - 9}L{cx + 21} {y}L{cx} {y + 9}L{cx - 21} {y}Z" fill="{INK2}" stroke="{col}" stroke-width="2" stroke-linejoin="round">'
                       f'<animateTransform attributeName="transform" type="translate" values="0 0;0 {-2 - 2 * k};0 0" dur="3s" begin="{k * 0.25}s" '
                       f'calcMode="spline" keySplines=".45 0 .55 1;.45 0 .55 1" {loop}/></path>')
        return "".join(out)
    if kind == "tools":  # the active tool steps around the grid
        cells = [(cx - 16 + (k % 2) * 18, cy - 16 + (k // 2) * 18) for k in range(4)]
        out = [f'<rect x="{x + 1}" y="{y + 1}" width="12" height="12" rx="3.5" stroke="{WHITE}" stroke-width="2"/>' for x, y in cells]
        order = [1, 3, 2, 0]
        xs = ";".join(str(cells[k][0]) for k in order + order[:1])
        ys = ";".join(str(cells[k][1]) for k in order + order[:1])
        spl = ";".join([".6 0 .2 1"] * 4)
        out.append(f'<rect x="{cells[1][0]}" y="{cells[1][1]}" width="14" height="14" rx="4" fill="{RED}">'
                   f'<animate attributeName="x" values="{xs}" dur="4.8s" calcMode="spline" keySplines="{spl}" {loop}/>'
                   f'<animate attributeName="y" values="{ys}" dur="4.8s" calcMode="spline" keySplines="{spl}" {loop}/></rect>')
        return "".join(out)
    return ""


TILES = [
    ("arsenal", "Arsenal", "Agent + app builder", "arsenal.world"),
    ("arena", "ZEN AI Arena", "Models + agents", "us.zenai.biz"),
    ("qubit", "Qubit", "Business agent platform", "qubit.earth"),
    ("pioneer", "AI Pioneer", "Youth AI literacy", "zenai.world/ailiteracyyouth"),
    ("solutions", "ZEN Solutions", "Enterprise + business AI", "zenai.world/solutions"),
    ("tools", "ZEN Tools", "AI utilities", "zenai.world/tools"),
]


def build_tiles():
    for kind, name, desc, url in TILES:
        W, H = 540, 152
        doc = Doc(W, H, f"{name} — {desc}", f"Link to {name}: {desc} ({url}).")
        op, cl = frame(doc, W, H, 26, bg=INK)
        doc.add(op, glow(doc, 72, 76, 160, RED if kind in ("arsenal", "pioneer") else BLUE, .07), BACKDROP_END)
        doc.add(f'<rect x="26" y="28" width="96" height="96" rx="24" fill="{INK2}" stroke="{LINE2}"/>')
        doc.add(_glyph(doc, kind, 74, 76))
        doc.add(doc.text(150, 70, name, "sansS", fit("sansS", name, 36, 320), WHITE))
        doc.add(doc.text(150, 110, desc, "sans", fit("sans", desc, 26, 340), MUTED))
        doc.add(arrow_ne(W - 54, 32, 15, DIM, 2.2))
        doc.add(cl)
        doc.save(f"tile-{kind}.svg")


def shimmer(doc: Doc, x0, x1, dur=7.0, delay=1.0) -> str:
    """Fill for display text: white, with a faint warm light that sweeps across it."""
    gid, w = doc.uid("sm"), x1 - x0
    doc.defs.append(
        f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" x1="{f2(x0)}" x2="{f2(x1)}">'
        f'<stop offset="0" stop-color="{WHITE}"/><stop offset=".4" stop-color="{WHITE}"/><stop offset=".5" stop-color="#FFB3A9"/>'
        f'<stop offset=".6" stop-color="{WHITE}"/><stop offset="1" stop-color="{WHITE}"/>'
        f'<animateTransform attributeName="gradientTransform" type="translate" values="{f2(-w)} 0;{f2(w)} 0;{f2(w)} 0" '
        f'keyTimes="0;.42;1" dur="{dur}s" begin="{delay}s" repeatCount="indefinite"/></linearGradient>')
    return f"url(#{gid})"


# ═══════════════════════════════════════════════════════════════ AI PIONEER
PIONEER_STEPS = ["Prompt", "Agent", "App", "Deploy", "Portfolio", "Credential"]


@both
def build_pioneer(m: bool):
    body = ("Ages 11–18. Students build agents, apps and games with frontier models, deploy them to the cloud, "
            "and leave with a public portfolio and a portable credential.")
    if m:
        lines = wrap("sans", body, 27, 608)
        qy = 448 + len(lines) * 39 + 44
        y0, gap = qy + 196, 52
        y1 = y0 + gap * (len(PIONEER_STEPS) - 1)
        W, H = 720, int(y1 + 64)
    else:
        W, H = 1200, 520
    doc = Doc(W, H, "AI Pioneer Program — the first generation of AI builders",
              "AI Pioneer Program with Boys & Girls Clubs of Greater Washington. Ages 11–18 build agents, apps and games "
              "with frontier models, deploy them, and leave with a public portfolio and a portable credential. "
              "Program principle: if it's not deployed, it's not finished. Pipeline: prompt, agent, app, deploy, portfolio, credential.")
    op, cl = frame(doc, W, H, 26)
    doc.add(op, glow(doc, 0, H, 620, RED, .10), glow(doc, W, 0, 560, BLUE, .09), BACKDROP_END)
    T = 9.0
    if not m:
        X = 64
        doc.add(doc.text(X, 84, "AI PIONEER PROGRAM  ·  WITH BOYS & GIRLS CLUBS OF GREATER WASHINGTON", "mono", 12, MUTED, ls=2))
        doc.add(doc.text(X - 2, 160, "The first generation", "disp", 56, WHITE))
        x_ab = X - 2 + width("disp", "of ", 56)
        sh = shimmer(doc, x_ab, x_ab + width("serif", "AI builders.", 68))
        doc.add(doc.runs(X - 2, 226, [("of ", WHITE, "disp", 56), ("AI builders.", sh, "serif", 68)]))
        for k, line in enumerate(wrap("sans", body, 19, 580)):
            doc.add(doc.text(X, 284 + k * 29, line, "sans", 19, TEXT2))
        qx = 760
        doc.add(f'<rect x="{qx - 36}" y="118" width="1" height="214" fill="{LINE2}"/>')
        doc.add(doc.text(qx, 176, "“If it’s not deployed,", "serif", 40, WHITE))
        doc.add(doc.text(qx, 224, "it’s not finished.”", "serif", 40, WHITE))
        doc.add(doc.text(qx, 268, "PROGRAM PRINCIPLE", "mono", 11, DIM, ls=2))
        # pipeline
        y, x0, x1 = 432, 96, W - 96
        L = x1 - x0
        doc.add(f'<path d="M{x0} {y}H{x1}" stroke="{LINE2}" stroke-width="2" stroke-linecap="round"/>')
        pid = doc.uid("pl")
        doc.defs.append(f'<linearGradient id="{pid}" gradientUnits="userSpaceOnUse" x1="{x0}" x2="{x1}"><stop offset="0" stop-color="{RED}"/><stop offset="1" stop-color="{WHITE}"/></linearGradient>')
        doc.add(f'<path d="M{x0} {y}H{x1}" stroke="url(#{pid})" stroke-width="2" stroke-linecap="round" stroke-dasharray="{L}" stroke-dashoffset="{L}">'
                f'<animate attributeName="stroke-dashoffset" values="{L};0;0;{L}" keyTimes="0;.62;.92;1" dur="{T}s" repeatCount="indefinite"/></path>')
        for k, st in enumerate(PIONEER_STEPS):
            nx = x0 + L * k / (len(PIONEER_STEPS) - 1)
            tk = 0.62 * k / (len(PIONEER_STEPS) - 1)
            doc.add(f'<circle cx="{f2(nx)}" cy="{y}" r="7.5" fill="{INK}" stroke="{LINE2}" stroke-width="2">'
                    f'<animate attributeName="stroke" calcMode="discrete" values="{LINE2};{WHITE};{WHITE};{LINE2}" keyTimes="0;{f4(tk)};.92;1" dur="{T}s" repeatCount="indefinite"/></circle>')
            doc.add(doc.text(nx, y + 38, st, "sansM", 16, TEXT2, anchor="middle"))
            doc.add(doc.text(nx, y - 22, f"0{k + 1}", "mono", 11, DIM, anchor="middle", ls=1))
        doc.add(f'<g><circle r="11" fill="{RED}" opacity=".3"/><circle r="4.5" fill="#fff"/>'
                f'<animateTransform attributeName="transform" type="translate" values="{x0} {y};{x1} {y};{x1} {y};{x0} {y}" keyTimes="0;.62;.92;1" dur="{T}s" repeatCount="indefinite"/>'
                f'<animate attributeName="opacity" values="1;1;0;0" keyTimes="0;.62;.66;1" dur="{T}s" repeatCount="indefinite"/></g>')
    else:
        X = 56
        doc.add(doc.text(X, 84, "AI PIONEER PROGRAM", "mono", 18, MUTED, ls=2.4))
        doc.add(doc.text(X, 114, "WITH BOYS & GIRLS CLUBS OF GREATER WASHINGTON", "mono", 18, MUTED,
                         ls=min(2.4, (608 - FB.width("mono", "WITH BOYS & GIRLS CLUBS OF GREATER WASHINGTON", 18)) / 44)))
        doc.add(doc.text(X - 3, 214, "The first", "disp", 68, WHITE))
        doc.add(doc.text(X - 3, 292, "generation of", "disp", 68, WHITE))
        doc.add(doc.text(X - 3, 378, "AI builders.", "serif", 84, shimmer(doc, X - 3, X - 3 + width("serif", "AI builders.", 84))))
        for k, line in enumerate(lines):
            doc.add(doc.text(X, 448 + k * 39, line, "sans", 27, TEXT2))
        doc.add(f'<rect x="{X}" y="{qy}" width="3" height="98" rx="1.5" fill="{RED}"/>')
        doc.add(doc.text(X + 26, qy + 40, "“If it’s not deployed,", "serif", 42, WHITE))
        doc.add(doc.text(X + 26, qy + 88, "it’s not finished.”", "serif", 42, WHITE))
        doc.add(doc.text(X + 26, qy + 128, "PROGRAM PRINCIPLE", "mono", 17, DIM, ls=2.4))
        # vertical pipeline
        L = y1 - y0
        lx = X + 20
        doc.add(f'<path d="M{lx} {y0}V{y1}" stroke="{LINE2}" stroke-width="2.4" stroke-linecap="round"/>')
        pid = doc.uid("pl")
        doc.defs.append(f'<linearGradient id="{pid}" gradientUnits="userSpaceOnUse" x1="0" y1="{y0}" x2="0" y2="{y1}"><stop offset="0" stop-color="{RED}"/><stop offset="1" stop-color="{WHITE}"/></linearGradient>')
        doc.add(f'<path d="M{lx} {y0}V{y1}" stroke="url(#{pid})" stroke-width="2.4" stroke-linecap="round" stroke-dasharray="{L}" stroke-dashoffset="{L}">'
                f'<animate attributeName="stroke-dashoffset" values="{L};0;0;{L}" keyTimes="0;.62;.92;1" dur="{T}s" repeatCount="indefinite"/></path>')
        for k, st in enumerate(PIONEER_STEPS):
            ny = y0 + gap * k
            tk = 0.62 * k / (len(PIONEER_STEPS) - 1)
            doc.add(f'<circle cx="{lx}" cy="{ny}" r="9.5" fill="{INK}" stroke="{LINE2}" stroke-width="2.4">'
                    f'<animate attributeName="stroke" calcMode="discrete" values="{LINE2};{WHITE};{WHITE};{LINE2}" keyTimes="0;{f4(tk)};.92;1" dur="{T}s" repeatCount="indefinite"/></circle>')
            doc.add(doc.text(lx + 36, ny + 9.5, st, "sansM", 27, TEXT2))
            doc.add(doc.text(W - 56, ny + 6.5, f"0{k + 1}", "mono", 18, DIM, anchor="end", ls=1))
        doc.add(f'<g><circle r="14" fill="{RED}" opacity=".3"/><circle r="6" fill="#fff"/>'
                f'<animateTransform attributeName="transform" type="translate" values="{lx} {y0};{lx} {y1};{lx} {y1};{lx} {y0}" keyTimes="0;.62;.92;1" dur="{T}s" repeatCount="indefinite"/>'
                f'<animate attributeName="opacity" values="1;1;0;0" keyTimes="0;.62;.66;1" dur="{T}s" repeatCount="indefinite"/></g>')
    doc.add(cl)
    doc.save(f"pioneer{suffix(m)}.svg")


# ═══════════════════════════════════════════════════════════════ PRESIDENTIAL AI CHALLENGE
def _certificate(doc: Doc, tx, ty, tw, rot=-2.0) -> tuple[str, float]:
    from PIL import Image
    im = Image.open(ROOT / "assets" / "presidential-ai-challenge-2026-certificate.png").convert("RGB")
    im.thumbnail((int(tw * 1.6), int(tw * 1.6)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=86, optimize=True, progressive=True)
    data = base64.b64encode(buf.getvalue()).decode()
    th = tw * im.size[1] / im.size[0]
    sid, ccl = doc.uid("sh"), doc.uid("cc")
    doc.defs.append(f'<linearGradient id="{sid}" x1="0" y1="0" x2="1" y2="1"><stop offset=".38" stop-color="#fff" stop-opacity="0"/>'
                    f'<stop offset=".5" stop-color="#fff" stop-opacity=".32"/><stop offset=".62" stop-color="#fff" stop-opacity="0"/></linearGradient>'
                    f'<clipPath id="{ccl}"><rect x="{f2(tx)}" y="{f2(ty)}" width="{f2(tw)}" height="{f2(th)}" rx="4"/></clipPath>')
    pad = 10
    return (f'<g transform="rotate({rot} {f2(tx + tw / 2)} {f2(ty + th / 2)})">'
            f'<rect x="{f2(tx - pad)}" y="{f2(ty - pad)}" width="{f2(tw + 2 * pad)}" height="{f2(th + 2 * pad)}" rx="14" fill="{INK2}" stroke="{LINE2}"/>'
            f'<image href="data:image/jpeg;base64,{data}" x="{f2(tx)}" y="{f2(ty)}" width="{f2(tw)}" height="{f2(th)}" clip-path="url(#{ccl})" preserveAspectRatio="xMidYMid slice"/>'
            f'<g clip-path="url(#{ccl})"><rect x="{f2(tx - tw)}" y="{f2(ty)}" width="{f2(tw)}" height="{f2(th)}" fill="url(#{sid})">'
            f'<animate attributeName="x" values="{f2(tx - tw)};{f2(tx + tw)};{f2(tx + tw)}" keyTimes="0;.32;1" dur="8s" repeatCount="indefinite"/></rect></g>'
            f'</g>'), th


@both
def build_credential(m: bool):
    quote = "“In recognition of your contribution to the 2026 Presidential AI Challenge.”"
    if m:
        qlines = wrap("serif", quote, 33, 608)
        H = int(56 + 520 * 464 / 600 + 66 + 186 + 52 + len(qlines) * 42 + 18 + 52)
        W = 720
    else:
        W, H = 1200, 340
    doc = Doc(W, H, "2026 Presidential AI Challenge — recognized contributor",
              "Alexander Leschik, recognized for contribution to the 2026 Presidential AI Challenge. "
              "Certificate text: In recognition of your contribution to the 2026 Presidential AI Challenge.")
    op, cl = frame(doc, W, H, 26)
    doc.add(op, glow(doc, 0, H / 2, 520, BLUE, .08), glow(doc, W, H, 520, RED, .07), BACKDROP_END)
    link = "VIEW THE ORIGINAL CERTIFICATE"
    if not m:
        doc.add(_certificate(doc, 74, (H - 296 * 464 / 600) / 2, 296)[0])
        X = 452
        doc.add(doc.text(X, 92, "RECOGNITION  ·  2026", "mono", 12, MUTED, ls=2.2))
        doc.add(doc.text(X - 2, 150, "Presidential AI Challenge", "disp", 46, WHITE))
        doc.add(doc.runs(X, 190, [("Recognized contributor", WHITE, "sansM", 18), ("  —  Alexander Leschik", TEXT2, "sans", 18)]))
        doc.add(doc.text(X, 240, "“In recognition of your contribution to the", "serif", 26, TEXT2))
        doc.add(doc.text(X, 272, "2026 Presidential AI Challenge.”", "serif", 26, TEXT2))
        doc.add(doc.text(X, 306, link, "mono", 11.5, MUTED, ls=1.8))
        doc.add(arrow_ne(X + FB.width("mono", link, 11.5, 1.8) + 10, 297, 9, MUTED, 1.5))
    else:
        cert, th = _certificate(doc, 100, 56, 520)
        doc.add(cert)
        X, y = 56, 56 + th + 66
        doc.add(doc.text(X, y, "RECOGNITION  ·  2026", "mono", 18, MUTED, ls=2.6))
        doc.add(doc.text(X - 3, y + 70, "Presidential", "disp", 58, WHITE))
        doc.add(doc.text(X - 3, y + 132, "AI Challenge", "disp", 58, WHITE))
        y += 186
        doc.add(doc.runs(X, y, [("Recognized contributor", WHITE, "sansM", 25), (" — Alexander Leschik", TEXT2, "sans", 25)]))
        y += 52
        for k, line in enumerate(qlines):
            doc.add(doc.text(X, y + k * 42, line, "serif", 33, TEXT2))
        y += len(qlines) * 42 + 18
        doc.add(doc.text(X, y, link, "mono", 17, MUTED, ls=2))
        doc.add(arrow_ne(X + FB.width("mono", link, 17, 2) + 14, y - 13, 13, MUTED, 2))
    doc.add(cl)
    doc.save(f"credential{suffix(m)}.svg")


# ═══════════════════════════════════════════════════════════════ BUTTONS
BUTTONS = [
    ("arsenal", "Arsenal", "Enter Arsenal — arsenal.world", True),
    ("pioneer", "AI Pioneer", "AI Pioneer Program — youth AI literacy", False),
    ("partner", "Partner", "Partner with ZEN — huxley@zenai.biz", False),
    ("portfolio", "Portfolio", "Portfolio — alexleschik.com", False),
]


def build_buttons():
    for slug, label, title, primary in BUTTONS:
        W, H = 320, 96
        doc = Doc(W, H, title, f"Button: {title}")
        r = H / 2
        i = [b[0] for b in BUTTONS].index(slug)
        if primary:
            doc.add(f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="{r - 1}" fill="{WHITE}" stroke="#D6DBE2" stroke-width="1.5"/>')
            lh = 26
            doc.add(laser_logo(doc, 36, r - lh / 2, lh, fill=INK, trace=INK, dur=8, draw_frac=0.26))
            tx, fg, ac = 36 + logo_width(lh) + 14, INK, INK
        else:
            cid, gid = doc.uid("bc"), doc.uid("bs")
            doc.defs.append(f'<clipPath id="{cid}"><rect x="2" y="2" width="{W - 4}" height="{H - 4}" rx="{r - 2}"/></clipPath>'
                            f'<linearGradient id="{gid}" x1="0" x2="1"><stop offset="0" stop-color="#fff" stop-opacity="0"/>'
                            f'<stop offset=".5" stop-color="#fff" stop-opacity=".13"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>')
            doc.add(f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="{r - 1}" fill="#0F1115" stroke="{LINE2}" stroke-width="1.5"/>'
                    f'<g clip-path="url(#{cid})"><rect x="-150" y="0" width="90" height="{H}" fill="url(#{gid})" transform="skewX(-18)">'
                    f'<animate attributeName="x" values="-150;{W + 60};{W + 60}" keyTimes="0;.32;1" dur="6s" begin="{f2(0.8 + i * 0.5)}s" repeatCount="indefinite"/></rect></g>')
            tx, fg, ac = 38, WHITE, TEXT2
        doc.add(doc.text(tx, r + 30 * 0.364, label, "sansS", 30, fg))
        doc.css.append("@keyframes aw{0%,62%{transform:translateX(0);opacity:1}74%{transform:translateX(16px);opacity:0}"
                       "75%{transform:translateX(-16px);opacity:0}88%,100%{transform:translateX(0);opacity:1}}"
                       ".aw{animation:aw 4.2s cubic-bezier(.5,0,.2,1) infinite}")
        doc.add(f'<g class="aw" style="animation-delay:{f2(1.2 + i * 0.3)}s">{arrow_e(W - 62, r, 22, ac, 2.6)}</g>')
        doc.save(f"btn-{slug}.svg")


# ═══════════════════════════════════════════════════════════════ SOCIAL LINKS
SOCIALS = [
    ("x", "@ZEN_AGI", "X", "x"),
    ("linkedin", "Alex Leschik", "LinkedIn", None),
    ("linkedin-co", "ZEN AI Co.", "LinkedIn", None),
    ("youtube", "@ZENAIML", "YouTube", "youtube"),
    ("instagram", "@0xvvs1", "Instagram", "instagram"),
    ("tiktok", "@milennialai", "TikTok", "tiktok"),
    ("discord", "Discord", "Discord", "discord"),
    ("telegram", "@ZENOAI", "Telegram", "telegram"),
    ("linq", "All links", "Linq", None),
]


def _simple_icon(name: str) -> str | None:
    tgz = _fetch("https://registry.npmjs.org/simple-icons/-/simple-icons-16.33.0.tgz", CACHE / "simple-icons-16.33.0.tgz")
    with tarfile.open(tgz) as t:
        try:
            svg = t.extractfile(f"package/icons/{name}.svg").read().decode()
        except KeyError:
            return None
    m = re.search(r' d="([^"]+)"', svg)
    return m.group(1) if m else None


def build_socials():
    for slug, label, network, icon in SOCIALS:
        W, H = 300, 80
        doc = Doc(W, H, f"{network} — {label}", f"Link to {network}: {label}")
        doc.add(f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="{H / 2 - 1}" fill="#0F1115" stroke="{LINE2}" stroke-width="1.5"/>')
        cx, cy = 42, H / 2
        d = _simple_icon(icon) if icon else None
        if d:
            s = 1.0
            doc.add(f'<path transform="translate({f2(cx - 12 * s)} {f2(cy - 12 * s)}) scale({s})" d="{d}" fill="{WHITE}"/>')
        elif slug.startswith("linkedin"):
            doc.add(f'<rect x="{cx - 12}" y="{cy - 12}" width="24" height="24" rx="5" fill="{WHITE}"/>')
            doc.add(doc.text(cx, cy + 6, "in", "sansS", 16, "#0F1115", anchor="middle", ls=0))
        else:
            doc.add(f'<g transform="translate({cx - 12} {cy - 12})" stroke="{WHITE}" stroke-width="2.4" stroke-linecap="round">'
                    '<path d="M10 14a4.5 4.5 0 006.4 0l3.2-3.2a4.5 4.5 0 00-6.4-6.4l-1.2 1.2"/>'
                    '<path d="M14 10a4.5 4.5 0 00-6.4 0l-3.2 3.2a4.5 4.5 0 006.4 6.4l1.2-1.2"/></g>')
        doc.add(doc.text(72, cy + 26 * 0.364, label, "sansM", fit("sansM", label, 26, 200), WHITE))
        doc.save(f"social-{slug}.svg")


# ═══════════════════════════════════════════════════════════════ FOOTER
@both
def build_footer(m: bool):
    W, H = (720, 340) if m else (1200, 216)
    doc = Doc(W, H, "ZEN AI Co. — Washington, D.C.",
              "Sign-off: the ZEN ZZ monogram is laser-traced above ZEN AI Co., with zenai.world, arsenal.world and qubit.earth.")
    op, cl = frame(doc, W, H, 26)
    doc.add(op, glow(doc, W / 2, 0, 420 if m else 360, BLUE, .12), glow(doc, W / 2, H, 380, RED, .06), BACKDROP_END)
    lh = 60 if m else 44
    ly = 54 if m else 40
    for k in range(3):
        doc.add(f'<circle cx="{W / 2}" cy="{f2(ly + lh / 2)}" r="{f2(lh * 0.9)}" stroke="#FFFFFF" stroke-width="1" opacity="0">'
                f'<animate attributeName="r" values="{f2(lh * 0.9)};{f2(lh * 2.8)}" dur="4.5s" begin="{k * 1.5}s" repeatCount="indefinite"/>'
                f'<animate attributeName="opacity" values=".28;0" dur="4.5s" begin="{k * 1.5}s" repeatCount="indefinite"/></circle>')
    doc.add(laser_logo(doc, W / 2 - logo_width(lh) / 2, ly, lh))
    if m:
        doc.add(doc.text(W / 2, 176, "ZEN AI Co.", "sansS", 30, WHITE, anchor="middle"))
        doc.add(doc.text(W / 2, 236, "zenai.world  ·  arsenal.world", "mono", 19, MUTED, anchor="middle", ls=1))
        doc.add(doc.text(W / 2, 270, "qubit.earth  ·  Washington, D.C.", "mono", 19, MUTED, anchor="middle", ls=1))
    else:
        doc.add(doc.text(W / 2, 130, "ZEN AI Co.", "sansS", 20, WHITE, anchor="middle"))
        doc.add(doc.text(W / 2, 168, "zenai.world  ·  arsenal.world  ·  qubit.earth  ·  Washington, D.C.", "mono", 12.5, MUTED, anchor="middle", ls=1.2))
    doc.add(cl)
    doc.save(f"footer{suffix(m)}.svg")


BUILDERS = {
    "hero": build_hero, "sections": build_sections, "signal": build_signal, "ecosystem": build_ecosystem,
    "tiles": build_tiles, "pioneer": build_pioneer, "credential": build_credential, "buttons": build_buttons,
    "socials": build_socials, "footer": build_footer,
}


def main():
    import sys
    global FB
    FB = FontBank()
    names = sys.argv[1:] or list(BUILDERS)
    print(f"building {len(names)} asset group(s) into {OUT.relative_to(ROOT)}/")
    for n in names:
        BUILDERS[n]()


if __name__ == "__main__":
    main()
