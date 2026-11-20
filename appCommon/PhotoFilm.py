"""
Photo-tool films for home PCB making, printed on transparency film with an ordinary printer.

Builds an A4 PDF at true 1:1 scale from Gerber/Excellon files (parsed with FlatCAM's own parsers):
  * copper films, NEGATIVE (for negative-acting dry-film photoresist) and/or POSITIVE (positive photoresist),
    optionally with drill-centre marks (copper removed at hole centres to guide the drill),
  * solder-mask films for UV-curable solder mask (openings black = not cured = washed off).

Mirroring follows the usual "printed side against the board" rule for Gerbers in top view:
top layers are mirrored, bottom layers are not.

Used by the FlatCAM Plus MCP bridge (POST /film) and from the command line:
    python -m appCommon.PhotoFilm --out films.pdf --outline Edge_Cuts.gbr \
        --film "B_Cu.gbr:copper:bottom:neg" --film "B_Cu.gbr:copper:bottom:pos" \
        --film "B_Mask.gbr:mask:bottom" --film "F_Mask.gbr:mask:top" --drill board.drl
"""

import argparse
import contextlib
import logging
import os
import sys
import types
from dataclasses import dataclass
from pathlib import Path

from shapely.affinity import scale as shp_scale, translate as shp_translate
from shapely.geometry import MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union

REPO_DIR = Path(__file__).resolve().parent.parent
MM = 72.0 / 25.4  # PDF points per mm


# ------------------------------------------------------------------------------------------------------------------
# Gerber / Excellon parsing with FlatCAM's parsers (no GUI)
# ------------------------------------------------------------------------------------------------------------------
def _flatcam_stub_app():
    if str(REPO_DIR) not in sys.path:
        sys.path.insert(0, str(REPO_DIR))
    from defaults import AppDefaults

    class _Sig:
        def emit(self, *a, **k):
            pass

        def __getitem__(self, k):
            return self

    class _Proc:
        def update_view_text(self, *a, **k):
            pass

        def new(self, *a, **k):
            return contextlib.nullcontext()

    opts = dict(AppDefaults.factory_defaults)
    app = types.SimpleNamespace(
        log=logging.getLogger("film"), options=opts, defaults=opts, inform=_Sig(), inform_shell=_Sig(),
        inform_no_echo=_Sig(), proc_container=_Proc(), abort_flag=False, app_units="MM", decimals=4,
        use_3d_engine=True, exc_areas=None, preprocessors={}, ui=None,
        plotcanvas=types.SimpleNamespace(new_shape_collection=lambda **k: None))
    app.dec_format = lambda v, d=4: round(float(v), d)
    return app


_APP = None


def _app():
    global _APP
    if _APP is None:
        logging.getLogger("film").setLevel(logging.ERROR)
        _APP = _flatcam_stub_app()
    return _APP


def load_gerber(path):
    app = _app()
    from appParsers.ParseGerber import Gerber
    g = Gerber(app)
    with open(os.devnull, "w") as devnull, contextlib.redirect_stdout(devnull):
        g.parse_file(str(path))
    geo = g.solid_geometry
    return unary_union(geo if isinstance(geo, (list, tuple)) else [geo]).buffer(0)


def load_outline(path):
    """Board outline polygon from an Edge_Cuts Gerber (the outline is drawn as a thin stroke)."""
    ring = load_gerber(path)
    polys = list(ring.geoms) if isinstance(ring, MultiPolygon) else [ring]
    biggest = max(polys, key=lambda p: Polygon(p.exterior).area)
    if biggest.interiors:
        # stroke ring: the board edge is the middle of the stroke; use the largest interior grown back by half
        inner = max((Polygon(i) for i in biggest.interiors), key=lambda p: p.area)
        outer = Polygon(biggest.exterior)
        half = (outer.area - inner.area) / max(outer.length + inner.length, 1e-9)
        return inner.buffer(half, join_style=2)
    return Polygon(biggest.exterior)


def load_drill_points(path):
    app = _app()
    from appParsers.ParseExcellon import Excellon
    e = Excellon(app)
    with open(os.devnull, "w") as devnull, contextlib.redirect_stdout(devnull):
        e.parse_file(str(path))
    pts, slots = [], []
    for tool in e.tools.values():
        pts += [(p.x, p.y) for p in tool.get("drills", [])]
        slots += [(s[0].x, s[0].y, s[1].x, s[1].y) for s in tool.get("slots", [])]
    return pts, slots


# ------------------------------------------------------------------------------------------------------------------
# film geometry
# ------------------------------------------------------------------------------------------------------------------
@dataclass
class Film:
    title: str
    note: str
    short: str
    geometry: object          # black areas, in board coordinates (mm)
    frame: object             # area the film covers (board outline + margin)


def make_copper_film(copper, frame, negative, drill_pts=(), drill_slots=(), mark_dia=0.4, title="", note="",
                     short=""):
    marks = []
    for x, y in drill_pts:
        marks.append(Point(x, y).buffer(mark_dia / 2, 32))
    for x1, y1, x2, y2 in drill_slots:
        from shapely.geometry import LineString
        marks.append(LineString([(x1, y1), (x2, y2)]).buffer(mark_dia / 2, 32))
    copper_kept = copper.difference(unary_union(marks)) if marks else copper
    black = frame.difference(copper_kept) if negative else copper_kept.intersection(frame)
    return Film(title, note, short or title, black, frame)


def make_mask_film(mask, frame, title="", note="", short=""):
    return Film(title, note, short or title, mask.intersection(frame), frame)


# ------------------------------------------------------------------------------------------------------------------
# PDF output
# ------------------------------------------------------------------------------------------------------------------
def _draw_geometry(c, geom, ox, oy, minx, miny, mirror, width):
    from reportlab.lib.colors import black
    polys = []
    if geom.is_empty:
        return
    for g in (geom.geoms if hasattr(geom, "geoms") else [geom]):
        if isinstance(g, Polygon):
            polys.append(g)
        elif isinstance(g, MultiPolygon):
            polys.extend(g.geoms)

    def tx(x, y):
        u = (x - minx)
        if mirror:
            u = width - u
        return (ox + u) * MM, (oy + (y - miny)) * MM

    p = c.beginPath()
    for poly in polys:
        for ring in [poly.exterior] + list(poly.interiors):
            pts = list(ring.coords)
            p.moveTo(*tx(*pts[0]))
            for pt in pts[1:]:
                p.lineTo(*tx(*pt))
            p.close()
    c.setFillColor(black)
    c.drawPath(p, fill=1, stroke=0, fillMode=0)  # fillMode 0 = even-odd


def _corner_marks(c, ox, oy, w, h, gap=1.0, length=3.0):
    c.setLineWidth(0.15 * MM)
    for (x, y, dx, dy) in ((ox, oy, -1, -1), (ox + w, oy, 1, -1), (ox, oy + h, -1, 1), (ox + w, oy + h, 1, 1)):
        c.line((x + dx * gap) * MM, y * MM, (x + dx * (gap + length)) * MM, y * MM)
        c.line(x * MM, (y + dy * gap) * MM, x * MM, (y + dy * (gap + length)) * MM)


def _scale_bar(c, x, y, length=50.0):
    c.setLineWidth(0.2 * MM)
    c.line(x * MM, y * MM, (x + length) * MM, y * MM)
    for i in range(0, int(length) + 1, 10):
        t = 2.0 if i % 50 == 0 else 1.2
        c.line((x + i) * MM, y * MM, (x + i) * MM, (y + t) * MM)
    c.setFont("Helvetica", 7)
    c.drawString((x + length + 2) * MM, (y - 0.8) * MM, "%d mm - measure this to check the print scale" % length)


def write_pdf(films, out_path, mirror_flags, board_name="", copies=2):
    """Lay the films out as a grid of cells (film x copies) on as few A4 pages as possible."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    page_w, page_h = A4[0] / MM, A4[1] / MM
    margin, mark = 10.0, 5.0          # page margin; space kept free around each film for the corner marks
    label_h, header_h = 7.0, 18.0
    c = canvas.Canvas(str(out_path), pagesize=A4)
    c.setTitle("%s photo films" % board_name)
    c.setAuthor("FlatCAM Plus MCP")

    def header():
        c.setFont("Helvetica-Bold", 10)
        c.drawString(margin * MM, (page_h - margin - 3) * MM, "%s - PCB photo films (1:1)" % board_name)
        c.setFont("Helvetica", 7)
        c.drawString(margin * MM, (page_h - margin - 7) * MM,
                     "Print at 100% / Actual size (no 'fit to page'). Printed side goes against the board. "
                     "Stack two copies for more density.")
        _scale_bar(c, margin, page_h - margin - 12)

    cells = [(f, m, k) for f, m in zip(films, mirror_flags) for k in range(copies)]
    cell_w = max(f.frame.bounds[2] - f.frame.bounds[0] for f in films) + 2 * mark
    cell_h = max(f.frame.bounds[3] - f.frame.bounds[1] for f in films) + 2 * mark + label_h
    legend_h = 4.0 + 3.5 * len(films)
    cols = max(1, int((page_w - 2 * margin) // cell_w))
    rows = max(1, int((page_h - 2 * margin - header_h - legend_h) // cell_h))

    def legend():
        y = margin + legend_h - 3
        c.setFont("Helvetica-Bold", 7)
        c.drawString(margin * MM, y * MM, "Films on this sheet:")
        c.setFont("Helvetica", 6.5)
        for i, (f, m) in enumerate(zip(films, mirror_flags)):
            y -= 3.5
            c.drawString(margin * MM, y * MM, "%d. %s - %s%s" % (i + 1, f.title, f.note,
                                                                 "  [MIRRORED]" if m else "  [not mirrored]"))

    per_page = cols * rows
    for start in range(0, len(cells), per_page):
        header()
        legend()
        for i, (film, mirror, k) in enumerate(cells[start:start + per_page]):
            r, col = divmod(i, cols)
            x0 = margin + col * cell_w
            y_top = page_h - margin - header_h - r * cell_h
            minx, miny, maxx, maxy = film.frame.bounds
            w, h = maxx - minx, maxy - miny
            idx = films.index(film) + 1
            c.setFont("Helvetica-Bold", 7)
            c.drawString((x0 + mark) * MM, (y_top - 3) * MM, "%d. %s" % (idx, film.short))
            c.setFont("Helvetica", 6)
            c.drawString((x0 + mark) * MM, (y_top - 6) * MM,
                         "copy %d - %s" % (k + 1, "MIRRORED" if mirror else "not mirrored"))
            ox, oy = x0 + mark, y_top - label_h - mark - h
            _draw_geometry(c, film.geometry, ox, oy, minx, miny, mirror, w)
            _corner_marks(c, ox, oy, w, h)
        c.showPage()
    c.save()


# ------------------------------------------------------------------------------------------------------------------
# high-level helper
# ------------------------------------------------------------------------------------------------------------------
SIDE_MIRROR = {"top": True, "bottom": False}


def build_films(specs, outline_path=None, drill_path=None, margin=2.0, mark_dia=0.4):
    """specs: list of (gerber_path, kind 'copper'|'mask', side 'top'|'bottom', polarity 'neg'|'pos')."""
    layers = {}
    for path, *_ in specs:
        if path not in layers:
            layers[path] = load_gerber(path)
    if outline_path:
        board = load_outline(outline_path)
    else:
        board = box(*unary_union(list(layers.values())).bounds)
    frame = box(*board.buffer(margin, join_style=2).bounds)
    drill_pts, drill_slots = load_drill_points(drill_path) if drill_path else ([], [])

    films, mirrors = [], []
    for path, kind, side, polarity in specs:
        name = Path(path).stem
        mirror = SIDE_MIRROR[side]
        if kind == "copper":
            neg = polarity == "neg"
            title = "%s - %s copper - %s" % (name, side, "NEGATIVE" if neg else "POSITIVE")
            note = ("For negative-acting dry-film photoresist: copper areas clear, exposed resist stays."
                    if neg else "For positive photoresist: copper areas black, exposed resist washes off.")
            if drill_pts or drill_slots:
                note += " Drill centres marked (%.1f mm)." % mark_dia
            short = "%s copper %s" % (side.capitalize(), "NEGATIVE" if neg else "POSITIVE")
            films.append(make_copper_film(layers[path], frame, neg, drill_pts, drill_slots, mark_dia, title, note,
                                          short))
        else:
            title = "%s - %s solder mask - UV mask" % (name, side)
            note = "For UV-curable solder mask: pad openings black (not cured, washed off), rest clear."
            films.append(make_mask_film(layers[path], frame, title, note, "%s solder mask (UV)" % side.capitalize()))
        mirrors.append(mirror)
    return films, mirrors, board


def export_films(specs, out_path, outline_path=None, drill_path=None, name="", copies=2, mark_dia=0.4):
    """Build films and write the PDF. specs: list of dicts or tuples (gerber, kind, side, polarity).
    Returns a summary dict."""
    norm = []
    for sp in specs:
        if isinstance(sp, dict):
            sp = (sp["gerber"], sp.get("kind", "copper"), sp.get("side", "bottom"), sp.get("polarity", "neg"))
        path, kind, side, pol = sp
        if kind not in ("copper", "mask") or side not in ("top", "bottom") or pol not in ("neg", "pos"):
            raise ValueError("bad film spec %r: kind copper|mask, side top|bottom, polarity neg|pos" % (sp,))
        if not Path(path).exists():
            raise FileNotFoundError(path)
        norm.append((str(path), kind, side, pol))
    if not norm:
        raise ValueError("no films requested")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    films, mirrors, board = build_films(norm, outline_path, drill_path, mark_dia=mark_dia)
    write_pdf(films, out_path, mirrors, board_name=name or Path(norm[0][0]).stem.split("-")[0], copies=copies)
    bx = board.bounds
    return {"pdf": str(out_path.resolve()), "board_mm": [round(bx[2] - bx[0], 2), round(bx[3] - bx[1], 2)],
            "films": [{"title": f.title, "mirrored": m} for f, m in zip(films, mirrors)], "copies": copies}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--film", action="append", required=True,
                    help="gerber:copper|mask:top|bottom[:neg|pos]  (copper polarity default neg)")
    ap.add_argument("--outline")
    ap.add_argument("--drill")
    ap.add_argument("--name", default="")
    ap.add_argument("--copies", type=int, default=2)
    ap.add_argument("--mark-dia", type=float, default=0.4)
    a = ap.parse_args()
    specs = []
    for f in a.film:
        bits = f.split(":")
        if len(bits) >= 2 and len(bits[0]) == 1:  # Windows drive letter ("D:\...")
            bits = [bits[0] + ":" + bits[1]] + bits[2:]
        specs.append((bits[0], bits[1], bits[2], bits[3] if len(bits) > 3 else "neg"))
    res = export_films(specs, a.out, a.outline, a.drill, a.name, a.copies, a.mark_dia)
    print("Board %.2f x %.2f mm; %d films written to %s" % (res["board_mm"][0], res["board_mm"][1],
                                                          len(res["films"]), res["pdf"]))


if __name__ == "__main__":
    main()
