"""H4 eval: heuristic vs local-LLM unit roles against eval/roles_gt.json.
usage: python -m eval.h4_roles [model.gguf] [timeout_s]"""
import glob, json, sys, time
from engine.analyze import analyze
from engine import llm_roles

KEY = {"headline", "hero", "logo", "cta"}
GT = {k: v for k, v in json.load(open("eval/roles_gt.json")).items() if not k.startswith("_")}


def score(pred_by_design):
    n = ok = kn = kok = 0
    for did, gt in GT.items():
        for uid, r in gt.items():
            p = pred_by_design[did].get(uid)
            n, ok = n + 1, ok + (p == r)
            if r in KEY or p in KEY:
                kn, kok = kn + 1, kok + (p == r)
    return ok / n, kok / kn, n, kn


def main():
    model = sys.argv[1] if len(sys.argv) > 1 else llm_roles.DEFAULT_MODEL
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 120
    heur, llm, lat = {}, {}, {}
    designs = {json.load(open(f))["design_id"]: json.load(open(f))["source"] for f in glob.glob("eval/corpus/designs/*.json")}
    t = time.monotonic(); llm_roles._get_llm(model); load = time.monotonic() - t
    for did in GT:
        d = analyze(designs[did])
        heur[did] = {u.uid: u.role for u in llm_roles.all_units(d)}
        t = time.monotonic()
        llm[did] = {k: (r, 1.0) for k, r in (llm_roles.label_roles(d, designs[did], timeout, model) or {}).items()}
        lat[did] = time.monotonic() - t
        if not llm[did]:
            print("FAIL/None", did)
        for uid, r in GT[did].items():
            h, (p, c) = heur[did].get(uid), llm[did].get(uid, ("None", 0))
            if h != r or p != r:
                print(f"  {did:24s} {uid:18s} gt={r:8s} heur={h:8s} llm={p} ({c:.2f})")
    llm_only = {d: {u: rc[0] for u, rc in v.items()} | {u: r for u, r in heur[d].items() if r == "missing"} for d, v in llm.items()}
    print(f"\nmodel {model}  load {load:.1f}s  latency mean {sum(lat.values())/len(lat):.1f}s max {max(lat.values()):.1f}s")
    print("heuristic      all=%.3f key=%.3f (n=%d, key n=%d)" % score(heur))
    print("llm            all=%.3f key=%.3f" % score(llm_only)[:2])


if __name__ == "__main__":
    main()
