"""pagespeed.py — Google PageSpeed Insights (nota + screenshot do site)."""

import base64
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date
from typing import Dict

import config
import db

_API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
_KEY = os.environ.get("PAGESPEED_API_KEY", "")
_TIMEOUT = 60
PS_DIR = config.DATA_DIR / "pagespeed"


def _slug(s: str) -> str:
    return re.sub(r"[^\w]+", "-", (s or "").lower()).strip("-") or "site"


def run_pagespeed(url: str, strategy: str = "mobile") -> Dict:
    if not url:
        return {"ok": False, "erro": "sem site"}
    if not url.startswith("http"):
        url = "http://" + url
    params = {"url": url, "strategy": strategy, "category": "performance"}
    if _KEY:
        params["key"] = _KEY
    full = f"{_API}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(full, headers={"User-Agent": "clinica_prospector"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "erro": f"PageSpeed indisponível: {e}"}

    lh = data.get("lighthouseResult", {})
    perf = (lh.get("categories", {}).get("performance", {}) or {}).get("score")
    score = round(perf * 100) if perf is not None else None
    audits = lh.get("audits", {})

    def disp(k):
        return (audits.get(k, {}) or {}).get("displayValue", "")

    metrics = {
        "LCP": disp("largest-contentful-paint"),
        "FCP": disp("first-contentful-paint"),
        "CLS": disp("cumulative-layout-shift"),
        "TBT": disp("total-blocking-time"),
        "Speed Index": disp("speed-index"),
    }
    # screenshot final (data URI base64)
    shot = (audits.get("final-screenshot", {}).get("details", {}) or {}).get("data", "")
    return {"ok": True, "score": score, "strategy": strategy, "metrics": metrics,
            "screenshot_data": shot, "url": url}


def pagespeed_lead(lead_id: int, strategy: str = "mobile") -> Dict:
    """Roda PSI para o site do lead, salva screenshot e guarda o resultado."""
    import dashboard_db as ddb
    lead = db.get_lead(lead_id)
    if not lead:
        return {"ok": False, "erro": "lead não encontrado"}
    site = lead.get("website", "")
    if not site:
        return {"ok": False, "erro": "lead sem site"}

    res = run_pagespeed(site, strategy)
    if not res.get("ok"):
        return res

    PS_DIR.mkdir(parents=True, exist_ok=True)
    shot_path = ""
    shot = res.pop("screenshot_data", "")
    if shot and shot.startswith("data:image"):
        try:
            b64 = shot.split(",", 1)[1]
            ext = "jpg" if "jpeg" in shot[:30] else "png"
            shot_path = str(PS_DIR / f"{_slug(lead['nome'])}_{strategy}.{ext}")
            with open(shot_path, "wb") as f:
                f.write(base64.b64decode(b64))
            ddb.add_anexo(lead_id, f"pagespeed_{strategy}.{ext}", shot_path)
        except Exception as e:
            print("erro ao salvar screenshot:", e)

    guardado = {**res, "screenshot_file": shot_path, "data": date.today().isoformat()}
    with db.conn() as con:
        con.execute("UPDATE leads SET pagespeed_json=? WHERE id=?",
                    (json.dumps(guardado, ensure_ascii=False), lead_id))
    ddb.add_nota(lead_id, f"PageSpeed ({strategy}): nota {res['score']}/100 · "
                          f"LCP {res['metrics'].get('LCP','?')}")
    return {"ok": True, "score": res["score"], "metrics": res["metrics"],
            "screenshot_file": shot_path, "strategy": strategy}


def dados_para_pdf(lead: Dict) -> Dict:
    """
    Lê o pagespeed_json do lead e devolve {score, metrics, screenshot_data_uri} p/ o
    PDF.
    """
    raw = lead.get("pagespeed_json")
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except Exception:
        return {}
    data_uri = ""
    f = d.get("screenshot_file")
    if f and os.path.exists(f):
        ext = "jpeg" if f.endswith("jpg") or f.endswith("jpeg") else "png"
        with open(f, "rb") as fh:
            data_uri = f"data:image/{ext};base64," + base64.b64encode(fh.read()).decode()
    return {"score": d.get("score"), "metrics": d.get("metrics", {}),
            "screenshot": data_uri, "strategy": d.get("strategy", "mobile")}


if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.google.com"
    r = run_pagespeed(url)
    r.pop("screenshot_data", None)
    print(json.dumps(r, ensure_ascii=False, indent=2))
