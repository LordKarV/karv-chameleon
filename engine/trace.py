"""Step-by-step HTML trace of one resize: what the engine saw, proposed, scored and
chose. Self-contained pages (images inlined), one per task, plus an index.

    python -m engine.trace input/chameleon_sample_level2_core.json            # its own target
    python -m engine.trace eval/corpus/designs/l2_trio_products.json --target 1280x720 --target 728x90
    python -m engine.trace input/*.json --out trace/                          # several files
"""
from __future__ import annotations

import argparse
import base64
import html
import io
import json
import math
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from engine.analyze import analyze
from engine.energy import energy
from engine.relations import background_focal, _decor_host, _units
from engine.resize import candidates, finalize, prior, resize_with_info, SAME_ASPECT

ROLE_COLORS = {"headline": "#d6336c", "subhead": "#ae3ec9", "body": "#7048e8", "legal": "#868e96",
               "hero": "#f08c00", "image": "#e8590c", "logo": "#1c7ed6", "cta": "#2f9e44",
               "badge": "#0ca678", "button": "#66a80f", "ribbon": "#e03131", "panel": "#495057",
               "line": "#adb5bd", "missing": "#c92a2a", "decor": "#74c0fc"}
MAX_THUMBS = 12
TERM_HELP = {
    "overlap": "new collisions beyond those already in the source", "decor": "decoration over text",
    "read": "text below its readable size", "size": "timid or oversized elements vs the canvas",
    "hier": "headline no longer the largest text", "space": "content span vs the source's span",
    "bal": "visual mass off-centre", "oob": "boxes outside the canvas", "bare": "uncovered canvas",
    "backdrop": "text/button now on a different panel colour", "concentric": "things centred together drifted apart",
    "focal": "hero/headline left the background glow", "proportion": "size ratios between key elements drifted",
    "deadzone": "largest empty hole bigger than the source's", "edge": "visible content touching the canvas edge",
    "proximity": "neighbours crammed together", "lines": "headline exploded into short lines",
}


def _render(design: dict, max_side: int = 900) -> Image.Image:
    from renderer.converter.convert import render_design_to_png
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "r.png"
        render_design_to_png(design, "brand_kit/assets", p, max_size=max_side)
        return Image.open(p).convert("RGB")


def _b64(im: Image.Image, side: int = 420) -> str:
    im = im.copy()
    im.thumbnail((side, side))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _overlay(im: Image.Image, W: float, H: float, boxes, points=()) -> Image.Image:
    """Draw labelled role boxes (in design px) over a render."""
    im = im.copy()
    sx, sy = im.width / W, im.height / H
    dr = ImageDraw.Draw(im)
    for label, role, r in boxes:
        c = ROLE_COLORS.get(role, "#212529")
        dr.rectangle([r[0] * sx, r[1] * sy, r[2] * sx, r[3] * sy], outline=c, width=3)
        dr.rectangle([r[0] * sx, r[1] * sy, r[0] * sx + 7 * len(label) + 6, r[1] * sy + 14], fill=c)
        dr.text((r[0] * sx + 3, r[1] * sy + 1), label, fill="white")
    for label, (x, y) in points:
        dr.ellipse([x * sx - 7, y * sy - 7, x * sx + 7, y * sy + 7], outline="#ffd43b", width=3)
        dr.text((x * sx + 9, y * sy - 6), label, fill="#ffd43b")
    return im


def _table(head, rows) -> str:
    h = "".join(f"<th>{html.escape(str(x))}</th>" for x in head)
    b = "".join("<tr>" + "".join(f"<td>{x}</td>" for x in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>"


def _r(r):
    return f"{round(r[0])}, {round(r[1])} → {round(r[2])}, {round(r[3])}"


CSS = """
:root{--bg:#f7f5f2;--card:#fff;--ink:#1f1b24;--mute:#6b6675;--line:#e3ddd5}
@media (prefers-color-scheme: dark){:root{--bg:#17151b;--card:#211e26;--ink:#ece8f2;--mute:#a49fae;--line:#38333f}}
body{margin:0;font:14px/1.5 -apple-system,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink)}
nav{position:fixed;top:0;left:0;bottom:0;width:210px;padding:18px 14px;border-right:1px solid var(--line);background:var(--card);overflow:auto}
nav a{display:block;color:var(--ink);text-decoration:none;padding:5px 8px;border-radius:6px}nav a:hover{background:var(--bg)}
main{margin-left:240px;padding:24px 32px;max-width:1300px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 22px;margin:0 0 22px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:18px;margin:0 0 10px}h3{font-size:14px;margin:16px 0 6px}
.mute{color:var(--mute)}.row{display:flex;gap:18px;flex-wrap:wrap;align-items:flex-start}
img{border:1px solid var(--line);border-radius:6px;max-width:100%}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin:6px 0}th,td{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}
th{color:var(--mute);font-weight:600}.win{background:rgba(47,158,68,.14)}.pill{display:inline-block;padding:1px 8px;border-radius:10px;color:#fff;font-size:11px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}.card{border:1px solid var(--line);border-radius:8px;padding:8px}
main.index a{display:block;padding:6px 0;color:var(--ink)}
@media (max-width:800px){nav{display:none}main{margin-left:0;padding:16px}}
"""


def _sec_input(source, d, W, H, src_im):
    rows = [[html.escape(e["id"]), e["type"], round(e["x"]), round(e["y"]), round(e["width"]), round(e["height"]),
             e.get("z_index", 0), html.escape(str(e.get("group_id") or "")),
             html.escape(str(e.get("content", e.get("asset_id", e.get("shape_type", ""))))[:60])]
            for e in source["elements"]]
    return ("input", "1 · Input", f"""<p class=mute>Source {d.W:.0f}×{d.H:.0f} → target {W}×{H}
        (aspect {d.W / d.H:.2f} → {W / H:.2f}). Element ids are never used for decisions.</p>
        <div class=row><img src="{_b64(src_im, 520)}"><div style="flex:1;min-width:360px">
        {_table(["id", "type", "x", "y", "w", "h", "z", "group", "content / asset"], rows)}</div></div>""")


def _sec_analyze(d, src_im):
    boxes = [(u.uid[:18], u.role, u.rect) for u in d.units if not u.host]
    boxes += [(u.uid[:14] + "*", "decor" if u.role == "decor" else u.role, u.rect) for u in d.units if u.host]
    boxes += [(e["id"][:18], "decor", (e["x"], e["y"], e["x"] + e["width"], e["y"] + e["height"])) for e in d.decor]
    f0 = background_focal(d, d.W, d.H)
    ov = _overlay(src_im, d.W, d.H, boxes, [("glow", f0)] if f0 else [])
    urows = []
    for u in d.units:
        why = []
        if u.kind == "container":
            why.append(f"holds {len(u.children)}: " + ", ".join(k.uid for k in u.children))
        if u.host:
            why.append(f"pinned to {u.host}")
        if u.corner:
            why.append(f"anchored to corner {u.corner}")
        pill = f"<span class=pill style='background:{ROLE_COLORS.get(u.role, '#212529')}'>{u.role}</span>"
        urows.append([html.escape(u.uid), u.kind, pill, f"{u.importance:.2f}", _r(u.rect), html.escape("; ".join(why))])
    order = " → ".join("[" + ", ".join(html.escape(u.uid) for u in r) + "]" for r in d.rows)
    hosts = []
    for e in d.decor:
        h = _decor_host(e, _units(d))
        hosts.append([html.escape(e["id"]), html.escape(h.uid) if h else "— (canvas edge / proportional)"])
    glow = f"({f0[0]:.0f}, {f0[1]:.0f})" if f0 else "none"
    return ("analyze", "2 · Understand the design", f"""
        <p class=mute>Background: {", ".join(e["id"] for e in d.background) or "none"} ·
        decorations: {", ".join(e["id"] for e in d.decor) or "none"} · background glow: {glow}.
        Boxes are coloured by role; * marks pinned stickers/shadows.</p>
        <div class=row><img src="{_b64(ov, 560)}"><div style="flex:1;min-width:380px">
        <h3>Units (what moves together) and roles</h3>{_table(["unit", "kind", "role", "importance", "source box", "notes"], urows)}
        <h3>Reading order (XY-cut)</h3><p>{order}</p>
        <h3>Decoration anchors</h3>{_table(["decoration", "follows"], hosts)}</div></div>""")


def _sec_candidates(source, d, W, H):
    same = abs(math.log((W / H) / (d.W / d.H))) < SAME_ASPECT
    scored = []
    for name, p in candidates(d, source, W, H):
        try:
            e, terms = energy(d, p, W, H)
            scored.append((e + prior(name), name, p, terms))
        except Exception as exc:  # shown, not hidden
            scored.append((float("inf"), name, p, {"error": str(exc)}))
    scored.sort(key=lambda t: t[0])
    keys = [k for k in TERM_HELP if any(isinstance(t[3].get(k), float) and t[3].get(k) for t in scored)]
    rows = []
    for e, name, p, terms in scored:
        cells = [f"{e:.2f}" if math.isfinite(e) else "error", html.escape(name), f"{prior(name):.2f}"]
        cells += [f"{terms.get(k, 0):.2f}" if terms.get(k) else "" for k in keys]
        rows.append(cells)
    table = _table(["energy", "candidate", "prior"] + keys, rows).replace("<tr><td>", "<tr class=win><td>", 1)
    thumbs, seen = [], set()
    for e, name, p, terms in scored:
        fam = name.split()[0] + (" grid" if "grid" in name else "")
        if fam in seen or not math.isfinite(e):
            continue
        seen.add(fam)
        im = _render(finalize(source, p, W, H), 700)
        worst = sorted((k for k in terms if isinstance(terms[k], float) and terms[k] > 0), key=lambda k: -terms[k])[:3]
        top = ", ".join(f"{k} {terms[k]:.2f}" for k in worst)
        thumbs.append(f"<div class=card><img src='{_b64(im, 360)}'><div><b>{html.escape(name)}</b> · E={e:.2f}</div>"
                      f"<div class=mute>{html.escape(top or 'no penalties')}</div></div>")
        if len(thumbs) >= MAX_THUMBS:
            break
    legend = "".join(f"<li><b>{k}</b>: {v}</li>" for k, v in TERM_HELP.items() if k in keys)
    note = " The target has the same shape as the source, so the engine returns the uniformly scaled original." if same else ""
    return ("candidates", "3 · Candidates and scores", f"""
        <p class=mute>{len(scored)} candidates; lower energy is better; the prior is the small cost of departing
        from the source's structure.{note}</p>
        <h3>Best of each template</h3><div class=grid>{"".join(thumbs)}</div>
        <h3>All candidates (winner highlighted)</h3>{table}<h3>What the terms mean</h3><ul>{legend}</ul>""")


def trace_one(source: dict, W: int, H: int, title: str) -> str:
    d = analyze(source)
    src_im = _render(source)
    secs = [_sec_input(source, d, W, H, src_im), _sec_analyze(d, src_im), _sec_candidates(source, d, W, H)]
    out, info = resize_with_info(source, {"width": W, "height": H})
    if info.get("insight"):
        secs.insert(2, ("insight", "2b · Insight (model review)", "<p class=mute>Optional model review of the "
                        "analysis; every correction is validated before use.</p><ul>"
                        + "".join(f"<li>{html.escape(x)}</li>" for x in info["insight"]) + "</ul>"))
    out_im = _render(out)
    prows = []
    for r in info.get("propose", []):
        prows.append([r["round"], "<br>".join(html.escape(x) for x in r["defects"][:8]),
                      "<br>".join(f"{e:.2f} · {html.escape(n)}" for e, n in r["tried"]),
                      html.escape(str(r["accepted"] or "none (no edit improved the score)")),
                      f"{r['energy_before']:.2f} → {r.get('energy_after', r['energy_before']):.2f}"])
    secs.append(("propose", "4 · Propose (touch-up)", "<p class=mute>A critic lists concrete defects in the chosen "
                 "layout; each maps to typed edits (nudge, pull in, re-centre, shrink); an edit is kept only if the "
                 "full score improves.</p>" + (_table(["round", "defects found", "edits tried (energy)", "accepted",
                                                        "energy"], prows) if prows else "<p>No defects found.</p>")))
    if info.get("refine"):
        rr = []
        for r in info["refine"]:
            rr.append([r.get("round", ""), "<br>".join(html.escape(str(x)) for x in r.get("why", [])),
                       "<br>".join(html.escape(str(x)) for x in r.get("applied", r.get("edits", []))),
                       html.escape(str(r.get("votes", ""))), html.escape(r.get("result", "")), r.get("latency_s", "")])
        secs.append(("refine", "4b · Refine (local model)", "<p class=mute>A local vision model proposes JSON edits; "
                     "the engine applies them exactly and keeps them only if hard checks pass and the model prefers "
                     "the result in both orders.</p>" + _table(["round", "model's reasons", "edits", "votes",
                                                                 "result", "seconds"], rr)))
    secs.append(("decision", "5 · Decision", f"""
        <p>Chosen: <b>{html.escape(info["chosen"])}</b> (energy {info.get("energy", 0):.2f}).</p>
        <div class=row><div><h3>Original</h3><img src="{_b64(src_im, 480)}"></div>
        <div><h3>Result</h3><img src="{_b64(out_im, 480)}"></div></div>"""))
    srcd = {e["id"]: e for e in source["elements"]}
    orows = []
    for e in out["elements"]:
        s = srcd[e["id"]]
        fs = f"{s['font_size']:.0f} → {e['font_size']:.0f}" if e["type"] == "text" else ""
        orows.append([html.escape(e["id"]), f"{s['x']:.0f}, {s['y']:.0f} → {e['x']:.0f}, {e['y']:.0f}",
                      f"{s['width']:.0f}×{s['height']:.0f} → {e['width']:.0f}×{e['height']:.0f}", fs])
    secs.append(("output", "6 · Output geometry", f"""<p class=mute>Only position, size, font size and letter
        spacing change; every other field is copied from the source.</p>
        {_table(["element", "position", "size", "font"], orows)}
        <details><summary>Output JSON</summary><pre>{html.escape(json.dumps(out, indent=1))}</pre></details>"""))
    nav = "".join(f"<a href='#{a}'>{t}</a>" for a, t, _ in secs)
    body = "".join(f"<section id='{a}'><h2>{t}</h2>{c}</section>" for a, t, c in secs)
    return (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>Resize trace</title><style>{CSS}</style></head><body><nav><b>{html.escape(title)}</b>"
            f"<p class=mute>{W}×{H}</p>{nav}<p><a href='index.html'>← all traces</a></p></nav>"
            f"<main><h1>{html.escape(title)} → {W}×{H}</h1>{body}</main></body></html>")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--target", action="append", default=[], help="WxH; repeatable; default: the task's own target")
    ap.add_argument("--out", default="trace")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    links = []
    for f in a.files:
        task = json.load(open(f))
        targets = [tuple(map(int, t.split("x"))) for t in a.target] or \
                  [(task["target_canvas"]["width"], task["target_canvas"]["height"])]
        for W, H in targets:
            name = f"{Path(f).stem}__{W}x{H}"
            (out / f"{name}.html").write_text(trace_one(task["source"], W, H, Path(f).stem))
            print("wrote", out / f"{name}.html")
    links = [f"<a href='{p.name}'>{html.escape(p.stem)}</a>" for p in sorted(out.glob("*.html")) if p.name != "index.html"]
    (out / "index.html").write_text(f"<!doctype html><meta charset=utf-8><title>Resize traces</title><style>{CSS}</style>"
                                    f"<main class=index style='margin:0 auto'><h1>Resize traces</h1>{''.join(links)}</main>")


if __name__ == "__main__":
    main()
