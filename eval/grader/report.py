"""Self-contained HTML report: verdict, hard rules, flags, source | output with the
flagged elements outlined in red (output) and their source position in blue."""
from __future__ import annotations

import base64
import html
import io
import json
from pathlib import Path

from PIL import Image, ImageDraw

from eval.grader import forensics

COLORS = {"PASS": "#1a7f37", "REVIEW": "#b58105", "FAIL": "#c62828", "ERROR": "#6a1b9a"}
THUMB = 520


def _img(design: dict, ids: set, color: str) -> str:
    try:
        a = forensics.render_png(design)
    except Exception:       # noqa: BLE001 - a report without a picture beats no report
        return ""
    im = Image.fromarray(a)
    s = im.width / design["canvas"]["width"]
    dr = ImageDraw.Draw(im)
    for e in design["elements"]:
        if e["id"] in ids:
            dr.rectangle([e["x"] * s, e["y"] * s, (e["x"] + e["width"]) * s, (e["y"] + e["height"]) * s],
                         outline=color, width=max(2, round(3 * im.width / 1000)))
    im.thumbnail((THUMB, THUMB))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return f'<img src="data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}">'


def _card(g: dict) -> str:
    v = g["verdict"]
    head = (f'<h2 id="{html.escape(g["task"])}"><span class="v" style="background:{COLORS[v]}">{v}</span> '
            f'{html.escape(g["task"])} <small>{g.get("target")} · {g.get("overall", 0)} · {g.get("seconds", "")}s</small></h2>')
    if v == "ERROR":
        return head + f"<pre>{html.escape(g.get('error', ''))}</pre>"
    ids = {r["element"] for r in g["hard"] if r["status"] == "FAIL" and r["element"]}
    ids |= {i for c in g["criteria"].values() for f in c["flags"] for i in f["ids"]}
    try:
        task = json.loads(Path(g["input"]).read_text())
        src, out = task.get("source", task), json.loads(Path(g["output"]).read_text())
        pics = f'<div class="pics"><figure>{_img(src, ids, "#1565c0")}<figcaption>source</figcaption></figure>' \
               f'<figure>{_img(out, ids, "#e53935")}<figcaption>output (flagged in red)</figcaption></figure></div>'
    except Exception:       # noqa: BLE001
        pics = ""
    rows = "".join(f'<tr class="{r["status"]}"><td>{r["id"]}</td><td>{r["status"]}</td>'
                   f'<td>{html.escape(str(r["element"] or ""))}</td><td>{html.escape(r["msg"])}</td></tr>'
                   for r in g["hard"] if r["status"] != "PASS")
    hard_tbl = f"<table><tr><th>rule</th><th></th><th>element</th><th>evidence</th></tr>{rows}</table>" if rows \
        else "<p>All hard rules pass.</p>"
    crit = "".join(f'<li><b>{k}</b> {c["score"]:.2f}' + "".join(
        f'<div class="f {f["severity"]}">{f["severity"]}: {html.escape(f["msg"])}</div>' for f in c["flags"]) + "</li>"
        for k, c in g["criteria"].items())
    return f'<section>{head}{pics}{hard_tbl}<ul>{crit}</ul></section>'


def write_html(grades: list, path: Path) -> None:
    idx = "".join(f'<a href="#{html.escape(g["task"])}" style="color:{COLORS[g["verdict"]]}">{g["verdict"]} '
                  f'{html.escape(g["task"])}</a><br>' for g in grades)
    css = """:root{--bg:#fff;--fg:#1d1d1f;--mut:#666;--line:#ddd}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#eee;--mut:#aaa;--line:#333}}
body{background:var(--bg);color:var(--fg);font:14px system-ui;margin:0 auto;max-width:1120px;padding:16px}
.v{color:#fff;padding:2px 8px;border-radius:4px;font-size:13px}small{color:var(--mut);font-weight:normal}
.pics{display:flex;gap:12px;flex-wrap:wrap}figure{margin:0}img{max-width:100%;border:1px solid var(--line)}
figcaption{color:var(--mut);font-size:12px}table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid var(--line);padding:4px;text-align:left}tr.FAIL td{color:#e53935}
.f{margin-left:16px;font-size:13px}.f.major{color:#e53935}.f.minor{color:var(--mut)}
section{border-top:2px solid var(--line);padding-top:8px;margin-top:16px}"""
    path.write_text(f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                    f"<title>Grader report</title><style>{css}</style><h1>Grader report</h1>{idx}"
                    + "".join(_card(g) for g in grades))
