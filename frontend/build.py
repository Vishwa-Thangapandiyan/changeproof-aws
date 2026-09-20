"""Bake the engine's evidence bundle into frontend/index.html.

    python frontend/build.py [path/to/evidence.json]

The page reads no network and no backend: every number it shows is in the bundle.
"""
import json, sys
from pathlib import Path

here = Path(__file__).parent
evidence = Path(sys.argv[1]) if len(sys.argv) > 1 else here.parent / ".changeproof/experiments/EXP-DEMO-001/evidence.json"
data = json.loads(evidence.read_text(encoding="utf-8"))
folder = evidence.parent
backends = json.loads((folder / "backends.json").read_text()) if (folder / "backends.json").exists() else {"graph": "local", "explainer": "local"}
expl = next((f for f in (folder / f"explanation.{backends['explainer']}.txt", folder / "explanation.local.txt") if f.exists()), None)
extra = {"backends": backends, "explanation": expl.read_text(encoding="utf-8") if expl else ""}
html = (here / "template.html").read_text(encoding="utf-8").replace("__EVIDENCE__", json.dumps(data)).replace("__EXTRA__", json.dumps(extra))
(here / "index.html").write_text(html, encoding="utf-8")
print(f"wrote {here / 'index.html'} from {evidence.name} ({data['experimentId']})")
