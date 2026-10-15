"""Resize + render a batch of (design, WxH) pairs INSIDE the scoring container.

Linux text metrics are 1-2% wider than macOS and flip the engine's pick on ~12% of pairs, so
anything that decides what ships is measured here, the way the organisers run it.

Host side:   from eval.in_container import render_in_docker
             render_in_docker([("l2_right_aligned", 320, 100), ...], folder)
Container:   python /app/in_container.py /app/out/_jobs.json /app/out   (what the host call runs)

The working tree's engine/ is mounted over the image's, so no rebuild per iteration; the image
(`docker build --platform linux/amd64 -t chameleon-engine .`) supplies the renderer and fonts.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

IMAGE = "chameleon-engine"
ROOT = Path(__file__).resolve().parents[1]


def render_in_docker(pairs: list[tuple[str, int, int]], folder: Path, workers: int = 4) -> list[tuple[str, int, int, str, str]]:
    """Run the pairs in the amd64 container; returns (design, W, H, source_png, output_png) like
    vision_bench._render. Pairs whose output PNG already exists are skipped (judge cache stays valid)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    todo = [{"design": d, "w": W, "h": H} for d, W, H in pairs if not (folder / f"{d}@{W}x{H}.png").is_file()]
    if todo:
        (folder / "_jobs.json").write_text(json.dumps(todo))
        cmd = ["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
               "-v", f"{ROOT / 'engine'}:/app/engine:ro",
               "-v", f"{ROOT / 'eval' / 'corpus' / 'designs'}:/app/designs:ro",
               "-v", f"{ROOT / 'eval' / 'in_container.py'}:/app/in_container.py:ro",
               "-v", f"{folder.resolve()}:/app/out",
               "--entrypoint", "python", IMAGE, "/app/in_container.py", "/app/out/_jobs.json", "/app/out", str(workers)]
        subprocess.run(cmd, check=True)
    return [(d, W, H, str(folder / f"{d}.png"), str(folder / f"{d}@{W}x{H}.png")) for d, W, H in pairs]


# ---- inside the container -------------------------------------------------------------------

def _one(job: dict) -> str:
    from engine.resize import resize
    from renderer.converter.convert import render_design_to_png
    out = Path(sys.argv[2])
    d, W, H = job["design"], int(job["w"]), int(job["h"])
    src = json.loads((Path("/app/designs") / f"{d}.json").read_text())["source"]
    if not (out / f"{d}.png").is_file():
        render_design_to_png(src, "brand_kit/assets", str(out / f"{d}.png"))
    res = resize(src, {"width": W, "height": H})
    (out / f"{d}@{W}x{H}.json").write_text(json.dumps(res))
    render_design_to_png(res, "brand_kit/assets", str(out / f"{d}@{W}x{H}.png"))
    return f"{d}@{W}x{H}"


def _main() -> int:
    from multiprocessing import Pool
    jobs = json.loads(Path(sys.argv[1]).read_text())
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    with Pool(workers) as p:
        for name in p.imap_unordered(_one, jobs):
            print(name, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
