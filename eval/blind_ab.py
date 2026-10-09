"""Blind human A/B: engine vs model-generated layouts (from the H7 run).

    python -m eval.blind_ab            # -> eval/blind/index.html (self-contained)

Left/right is randomised per item (fixed seed) and labels are hidden; votes
are kept in the browser; the end screen reveals sides and agreement with the
strict judge, and a button copies the results as JSON to paste back."""
import base64
import io
import json
import random
from pathlib import Path

from PIL import Image

RUN = Path("eval/runs/h7")
OUT = Path("eval/blind")
MODEL_ORDER = ["claude0", "claude1", "gemini1"]   # strongest available per item; qwen excluded


def b64(path: Path, side: int = 640) -> str:
    im = Image.open(path).convert("RGB")
    im.thumbnail((side, side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def main():
    rows = json.load(open(RUN / "results.json"))
    rng = random.Random(7)
    items = []
    for r in rows:
        stem, j = r["stem"], r["judge"]
        model = next((m for m in MODEL_ORDER if m in j and (RUN / f"{stem}__{m}.png").is_file()), None)
        if model is None or "engine" not in j:
            continue
        judge_pref = "engine" if j["engine"] > j[model] else ("model" if j[model] > j["engine"] else "tie")
        engine_left = rng.random() < 0.5
        imgs = {"engine": b64(RUN / f"{stem}__engine.png"), "model": b64(RUN / f"{stem}__{model}.png")}
        design, target = stem.split("__", 1)
        items.append({"id": stem, "design": design, "target": target.replace("_", " ").strip(),
                      "src": b64(RUN / f"{stem}__src.png", 420),
                      "left": imgs["engine" if engine_left else "model"],
                      "right": imgs["model" if engine_left else "engine"],
                      "left_is": "engine" if engine_left else "model", "model": model,
                      "judge": {"engine": j["engine"], "model": j[model], "pref": judge_pref}})
    rng.shuffle(items)
    OUT.mkdir(parents=True, exist_ok=True)
    html = TEMPLATE.replace("__ITEMS__", json.dumps(items))
    (OUT / "index.html").write_text(html)
    print(f"wrote {OUT / 'index.html'} with {len(items)} items "
          f"({sum(i['model'].startswith('claude') for i in items)} Claude, {sum(i['model'] == 'gemini1' for i in items)} Gemini)")


TEMPLATE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Blind layout test</title>
<style>
:root{--bg:#f6f4f0;--card:#fff;--ink:#1f1b24;--mute:#6b6675;--line:#e2dcd3;--acc:#c2552d;--ok:#2f9e44}
@media (prefers-color-scheme:dark){:root{--bg:#17151b;--card:#211e26;--ink:#ece8f2;--mute:#a49fae;--line:#38333f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,Helvetica,Arial,sans-serif}
main{max-width:1200px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:20px;margin:0 0 4px}.mute{color:var(--mute)}
.bar{height:6px;background:var(--line);border-radius:3px;margin:12px 0 18px;overflow:hidden}.bar i{display:block;height:100%;background:var(--acc)}
.src{text-align:center;margin-bottom:14px}.src img{max-height:220px}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.opt{background:var(--card);border:2px solid var(--line);border-radius:12px;padding:10px;cursor:pointer;text-align:center}
.opt:hover{border-color:var(--acc)}.opt.sel{border-color:var(--ok)}
.opt img{max-width:100%;max-height:52vh;border-radius:6px;border:1px solid var(--line)}
.lab{font-weight:700;margin-bottom:6px}
.btns{display:flex;gap:10px;justify-content:center;margin:16px 0;flex-wrap:wrap}
button{font:inherit;padding:9px 16px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button.pri{background:var(--acc);border-color:var(--acc);color:#fff}
img{background:repeating-conic-gradient(#ccc 0 25%,#eee 0 50%) 0/16px 16px}
table{border-collapse:collapse;margin:12px 0}td,th{padding:5px 10px;border-bottom:1px solid var(--line);text-align:left}
@media (max-width:700px){.pair{grid-template-columns:1fr}}
</style></head><body><main id="app"></main>
<script>
const ITEMS = __ITEMS__;
const KEY = "blind_ab_votes_v1";
let votes = {}; try { votes = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(votes)); } catch (e) {} };
let i = ITEMS.findIndex(it => !(it.id in votes)); if (i < 0) i = ITEMS.length;
const app = document.getElementById("app");

function vote(v) { votes[ITEMS[i].id] = v; save(); i++; render(); }
function back() { if (i > 0) { i--; render(); } }

function render() {
  const done = Object.keys(votes).length;
  const pct = Math.round(100 * done / ITEMS.length);
  if (i >= ITEMS.length) return results();
  const it = ITEMS[i];
  app.innerHTML = `
    <h1>Which resize is better? <span class="mute">(${i + 1} / ${ITEMS.length})</span></h1>
    <p class="mute">Original on top. Judge each as a finished ad for <b>${it.target}</b>: collisions, readability,
      faithfulness to the original, and whether it looks designed. Keys: <b>1</b> = A, <b>2</b> = B, <b>3</b> = tie, <b>←</b> = back.</p>
    <div class="bar"><i style="width:${pct}%"></i></div>
    <div class="src"><div class="lab">Original</div><img src="${it.src}" alt="original design"></div>
    <div class="pair">
      <div class="opt" onclick="vote('A')"><div class="lab">A</div><img src="${it.left}" alt="option A"></div>
      <div class="opt" onclick="vote('B')"><div class="lab">B</div><img src="${it.right}" alt="option B"></div>
    </div>
    <div class="btns"><button onclick="back()">← Back</button><button onclick="vote('A')">A is better</button>
      <button onclick="vote('tie')">About the same</button><button onclick="vote('B')">B is better</button></div>`;
}

function results() {
  let human = {engine: 0, model: 0, tie: 0}, agree = 0, decided = 0, rows = "";
  const out = [];
  for (const it of ITEMS) {
    const v = votes[it.id]; if (!v) continue;
    const pick = v === "tie" ? "tie" : ((v === "A") === (it.left_is === "engine") ? "engine" : "model");
    human[pick]++;
    if (pick !== "tie" && it.judge.pref !== "tie") { decided++; if (pick === it.judge.pref) agree++; }
    out.push({id: it.id, model: it.model, human: pick, judge: it.judge.pref, judge_engine: it.judge.engine, judge_model: it.judge.model});
    rows += `<tr><td>${it.design}</td><td>${it.target}</td><td>${it.model}</td><td>${pick}</td><td>${it.judge.pref} (${it.judge.engine} vs ${it.judge.model})</td></tr>`;
  }
  const res = JSON.stringify({human, agree, decided, items: out});
  app.innerHTML = `<h1>Done. Here is what you picked</h1>
    <table><tr><th>You preferred the engine</th><td>${human.engine}</td></tr><tr><th>You preferred the model</th><td>${human.model}</td></tr>
    <tr><th>Ties</th><td>${human.tie}</td></tr><tr><th>Agreement with the strict judge (where both picked a side)</th><td>${agree} / ${decided}</td></tr></table>
    <div class="btns" style="justify-content:flex-start"><button class="pri" id="cp">Copy results to paste back</button>
      <button onclick="if(confirm('Clear all votes?')){votes={};save();i=0;render();}">Start over</button></div>
    <details><summary>Per item</summary><table><tr><th>design</th><th>target</th><th>model</th><th>you</th><th>judge</th></tr>${rows}</table></details>`;
  document.getElementById("cp").onclick = () => navigator.clipboard.writeText(res).then(() => alert("Copied"), () => prompt("Copy this:", res));
}

document.addEventListener("keydown", e => {
  if (i >= ITEMS.length) return;
  if (e.key === "1") vote("A"); else if (e.key === "2") vote("B"); else if (e.key === "3") vote("tie"); else if (e.key === "ArrowLeft") back();
});
render();
</script></body></html>"""

if __name__ == "__main__":
    main()
