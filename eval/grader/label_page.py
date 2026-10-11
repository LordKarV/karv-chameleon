"""Blind human-labelling page for the grader (docs/GRADER_PLAN.md §5.1 "Human labels").

    python -m eval.grader.label_page [--n 120] [--seed 5] [-j 6]   # -> eval/grader_labels/index.html
    python -m eval.grader.label_page --relabel labels.json          # -> eval/grader_labels/relabel.html

Items mix engine outputs (~50%), identity/corrupt.CONTROLS (~20%) and visible
corrupt.DEFECTS (~30%), shuffled. The page holds only opaque item ids and images; the
answer key (design, target, kind, defect, grader verdict) goes to manifest.json, so the
labeller is blind. Images are cached in items.json so --relabel needs no re-render.
Score the labels with `python -m eval.grader.agree`.
"""
from __future__ import annotations

import argparse
import io
import json
import random
from multiprocessing import Pool
from pathlib import Path

from PIL import Image

from eval.blind_ab import b64
from eval.grader import corrupt, forensics
from eval.grader.__main__ import grade

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "eval" / "grader_labels"
# PNG/JSON-only defects a human can't see; the hard rules already prove them
INVISIBLE = {"content_changed", "font_changed", "color_changed", "asset_changed", "id_duplicated", "wrong_canvas",
             "png_tampered", "element_dropped"}
VISIBLE = [d for d in corrupt.DEFECTS if d not in INVISIBLE]
CONTROLS = ["identity", *corrupt.CONTROLS]
RELABEL_N = 30


def _jpeg(a) -> str:
    buf = io.BytesIO()
    Image.fromarray(a).save(buf, "PNG")
    return b64(buf, 720)       # blind_ab.b64 takes anything PIL can open


def _one(job):
    from engine.resize import resize      # labelling tool only; the grader never imports the engine
    iid, stem, kind, names, target = job
    src = json.loads((ROOT / "eval" / "corpus" / "designs" / f"{stem}.json").read_text())["source"]
    out, name = None, None
    if kind == "identity":
        out, target = src, {"width": src["canvas"]["width"], "height": src["canvas"]["height"]}
    else:
        base = resize(src, target)
        fns = {**corrupt.CONTROLS, **{k: f for k, (f, _) in corrupt.DEFECTS.items()}}
        for n in names:                   # first corruption that applies to this pair
            out, name = fns[n](src, base), n
            if out is not None:
                break
        if out is None:
            out, name, kind = base, None, "engine"
    row = {"id": iid, "design": stem, "target": f"{target['width']}x{target['height']}", "kind": kind, "defect": name}
    try:
        g = grade(src, out, target)
        row.update(verdict=g["verdict"], overall=g["overall"],
                   hard_fail=sorted({r["id"] for r in g["hard"] if r["status"] == "FAIL"}),
                   fired=sorted(k for k, c in g["criteria"].items() if c["flags"]),
                   flags=[f"{r['id']}:{r['element'] or ''} {r['msg']}" for r in g["hard"] if r["status"] == "FAIL"]
                   + [f"{f['criterion']}: {f['msg']}" for c in g["criteria"].values() for f in c["flags"]
                      if f["severity"] == "major"])
    except Exception as ex:   # noqa: BLE001 - a grader crash is recorded, the item stays labellable
        row.update(verdict="ERROR", overall=0, hard_fail=[], fired=[], flags=[f"grader error: {ex}"])
    return row, _jpeg(forensics.render_png(out))


def _src(stem):
    src = json.loads((ROOT / "eval" / "corpus" / "designs" / f"{stem}.json").read_text())["source"]
    return stem, _jpeg(forensics.render_png(src))


def _jobs(n: int, rng: random.Random):
    targets = json.loads((ROOT / "eval" / "targets.json").read_text())
    designs = sorted(p.stem for p in (ROOT / "eval" / "corpus" / "designs").glob("*.json"))
    n_eng, n_ctl = round(0.5 * n), round(0.2 * n)
    kinds = ["engine"] * n_eng + ["control"] * n_ctl + ["defect"] * (n - n_eng - n_ctl)
    jobs, ids = [], set()
    for i, k in enumerate(kinds):
        iid = f"{rng.getrandbits(32):08x}"
        while iid in ids:
            iid = f"{rng.getrandbits(32):08x}"
        ids.add(iid)
        t = rng.choice(targets)
        ctl = CONTROLS[i % len(CONTROLS)]
        kind = "identity" if k == "control" and ctl == "identity" else k
        names = [] if k == "engine" else [ctl] if k == "control" else rng.sample(VISIBLE, len(VISIBLE))
        jobs.append((iid, rng.choice(designs), kind, names, {"width": t["width"], "height": t["height"]}))
    rng.shuffle(jobs)
    return jobs


def write_page(items: list, srcs: dict, path: Path, key: str, title: str):
    keys = {d: f"s{i}" for i, d in enumerate(sorted(srcs))}   # opaque: the page never names the design
    page = [{"id": it["id"], "src": keys[it["design"]], "target": it["target"].replace("x", " × "), "img": it["img"]}
            for it in items]
    html = (TEMPLATE.replace("__ITEMS__", json.dumps(page)).replace("__SRC__", json.dumps({keys[d]: v for d, v in srcs.items()}))
            .replace("__KEY__", key).replace("__TITLE__", title))
    path.write_text(html)
    print(f"wrote {path} ({len(items)} items, {path.stat().st_size / 1e6:.1f} MB)")


def relabel(labels_file: Path, seed: int):
    cache = json.loads((OUT / "items.json").read_text())
    labels = json.loads(labels_file.read_text())
    done = [it for it in cache["items"] if (labels.get(it["id"]) or {}).get("verdict")]
    rng = random.Random(seed + 1)
    pick = rng.sample(done, min(RELABEL_N, len(done)))
    write_page(pick, cache["srcs"], OUT / "relabel.html", f"grader_relabel_v1_{seed}", "Re-label (blind)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.grader.label_page")
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("-j", type=int, default=6)
    ap.add_argument("--relabel", type=Path, help="labels JSON exported from index.html")
    a = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    if a.relabel:
        relabel(a.relabel, a.seed)
        return 0
    jobs = _jobs(a.n, random.Random(a.seed))
    with Pool(a.j) as p:
        res = p.map(_one, jobs)
        srcs = dict(p.map(_src, sorted({j[1] for j in jobs})))
    manifest = {r["id"]: r for r, _ in res}
    items = [{"id": r["id"], "design": r["design"], "target": r["target"], "img": img} for r, img in res]
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1))
    (OUT / "items.json").write_text(json.dumps({"items": items, "srcs": srcs}))
    write_page(items, srcs, OUT / "index.html", f"grader_labels_v2_{a.seed}", "Label resizes (blind)")
    mix = {}
    for r, _ in res:
        k = r["kind"] if r["kind"] != "defect" else f"defect:{r['defect']}"
        mix[k] = mix.get(k, 0) + 1
    print("mix:", dict(sorted(mix.items())))
    print("grader:", {v: sum(r["verdict"] == v for r, _ in res) for v in ("PASS", "REVIEW", "FAIL", "ERROR")})
    return 0


TEMPLATE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title>
<style>
:root{--bg:#f6f4f0;--card:#fff;--ink:#1f1b24;--mute:#6b6675;--line:#e2dcd3;--acc:#c2552d;--ok:#2f9e44;--rev:#d08700;--bad:#c92a2a}
@media (prefers-color-scheme:dark){:root{--bg:#17151b;--card:#211e26;--ink:#ece8f2;--mute:#a49fae;--line:#38333f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,Helvetica,Arial,sans-serif}
main{max-width:1400px;margin:0 auto;padding:12px 16px 40px}
h1{font-size:18px;margin:0}.mute{color:var(--mute)}.top{display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
.bar{height:5px;background:var(--line);border-radius:3px;margin:8px 0 10px;overflow:hidden}.bar i{display:block;height:100%;background:var(--acc)}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.pane{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px;text-align:center}
.pane img{max-width:100%;max-height:58vh;border:1px solid var(--line);border-radius:4px;object-fit:contain}
.lab{font-weight:700;margin-bottom:4px}
.ctl{display:flex;gap:18px;flex-wrap:wrap;margin-top:12px;align-items:flex-start}
.grp{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
button{font:inherit;padding:7px 12px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button.on{color:#fff}.v-PASS.on{background:var(--ok);border-color:var(--ok)}.v-REVIEW.on{background:var(--rev);border-color:var(--rev)}
.v-FAIL.on{background:var(--bad);border-color:var(--bad)}.r.on{background:var(--acc);border-color:var(--acc)}
label.tag{display:inline-flex;gap:5px;align-items:center;padding:5px 9px;border:1px solid var(--line);border-radius:8px;background:var(--card);cursor:pointer}
kbd{font:11px ui-monospace,monospace;border:1px solid var(--line);border-radius:4px;padding:0 4px;color:var(--mute)}
@media (max-width:700px){.pair{grid-template-columns:1fr}}
</style></head><body><main>
<div class="top"><h1>__TITLE__</h1><span class="mute" id="prog"></span>
<span class="mute">Is the resize (right) a finished, correct version of the original (left)?</span></div>
<div class="bar"><i id="pbar"></i></div>
<div class="pair"><div class="pane"><div class="lab">Original</div><img id="src" alt="original design"></div>
<div class="pane"><div class="lab">Resize to <span id="tgt"></span></div><img id="out" alt="resized design"></div></div>
<div class="ctl">
 <div class="grp" id="verd"></div>
 <div class="grp" id="tags"></div>
 <div class="grp" id="rate"><span class="mute">Overall (optional):</span></div>
 <div class="grp"><button id="prev"><kbd>←</kbd> Prev</button><button id="next">Next <kbd>→</kbd></button>
  <button id="exp">Export JSON</button></div>
</div>
<p class="mute">Tick tags first (letter keys), then <kbd>1</kbd> PASS / <kbd>2</kbd> REVIEW / <kbd>3</kbd> FAIL saves and moves on.
Rating: <kbd>z</kbd>..<kbd>b</kbd> = 1..5. Labels stay in this browser until you export.</p>
</main>
<script>
const ITEMS = __ITEMS__, SRC = __SRC__, KEY = "__KEY__";
const VERDICTS = ["PASS", "REVIEW", "FAIL"];
const TAGS = [["overlap", "q", "overlap / collision"], ["text", "w", "unreadable or too-small text"],
  ["lost", "e", "element lost or shrunk"], ["hierarchy", "r", "hierarchy wrong"], ["apart", "t", "belonged together, came apart"],
  ["space", "y", "bad empty space / cramped"], ["distorted", "u", "distorted image"], ["other", "i", "other"]];
const RATE_KEYS = ["z", "x", "c", "v", "b"];
let labels = {}; try { labels = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(labels)); } catch (e) {} };
const $ = id => document.getElementById(id);
let i = ITEMS.findIndex(it => !(labels[it.id] || {}).verdict); if (i < 0) i = 0;
const cur = () => labels[ITEMS[i].id] || (labels[ITEMS[i].id] = {verdict: null, tags: [], rating: null});

$("verd").innerHTML = VERDICTS.map((v, k) => `<button class="v-${v}" data-v="${v}"><kbd>${k + 1}</kbd> ${v}</button>`).join("");
$("tags").innerHTML = TAGS.map(([t, k, l]) => `<label class="tag"><input type="checkbox" data-t="${t}"><kbd>${k}</kbd> ${l}</label>`).join("");
$("rate").innerHTML += [1, 2, 3, 4, 5].map(r => `<button class="r" data-r="${r}"><kbd>${RATE_KEYS[r - 1]}</kbd> ${r}</button>`).join("");

function go(d) { i = Math.max(0, Math.min(ITEMS.length - 1, i + d)); render(); }
function verdict(v) { cur().verdict = v; save(); if (i < ITEMS.length - 1) go(1); else render(); }
function tag(t) { const c = cur(); c.tags = c.tags.includes(t) ? c.tags.filter(x => x !== t) : [...c.tags, t]; save(); render(); }
function rate(r) { const c = cur(); c.rating = c.rating === r ? null : r; save(); render(); }

function render() {
  const it = ITEMS[i], l = labels[it.id] || {tags: []};
  const done = ITEMS.filter(x => (labels[x.id] || {}).verdict).length;
  $("prog").textContent = `item ${i + 1} / ${ITEMS.length} · ${done} labelled`;
  $("pbar").style.width = `${100 * done / ITEMS.length}%`;
  $("src").src = SRC[it.src]; $("out").src = it.img; $("tgt").textContent = it.target;
  document.querySelectorAll("[data-v]").forEach(b => b.classList.toggle("on", b.dataset.v === l.verdict));
  document.querySelectorAll("[data-t]").forEach(c => { c.checked = l.tags.includes(c.dataset.t); });
  document.querySelectorAll("[data-r]").forEach(b => b.classList.toggle("on", +b.dataset.r === l.rating));
  const nx = ITEMS[i + 1]; if (nx) { new Image().src = nx.img; }
}

function exportJson() {
  const out = {}; for (const it of ITEMS) if ((labels[it.id] || {}).verdict) out[it.id] = labels[it.id];
  const s = JSON.stringify(out, null, 1);
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([s], {type: "application/json"})); a.download = `${KEY}.json`; a.click();
  if (navigator.clipboard) navigator.clipboard.writeText(s).catch(() => {});
}

document.addEventListener("click", e => {
  const b = e.target.closest("[data-v],[data-r]"); if (b) { b.dataset.v ? verdict(b.dataset.v) : rate(+b.dataset.r); b.blur(); }
});
document.addEventListener("change", e => { if (e.target.dataset.t) { tag(e.target.dataset.t); e.target.blur(); } });
$("prev").onclick = () => go(-1); $("next").onclick = () => go(1); $("exp").onclick = exportJson;
document.addEventListener("keydown", e => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.toLowerCase(), t = TAGS.find(x => x[1] === k);
  if (k >= "1" && k <= "3") verdict(VERDICTS[+k - 1]);
  else if (t) tag(t[0]);
  else if (RATE_KEYS.includes(k)) rate(RATE_KEYS.indexOf(k) + 1);
  else if (k === "arrowleft") go(-1);
  else if (k === "arrowright" || k === "enter") go(1);
  else return;
  e.preventDefault();
});
render();
</script></body></html>"""

if __name__ == "__main__":
    raise SystemExit(main())
