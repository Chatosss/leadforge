"""scorer.py — Módulo A (Qualificação). PRIORIDADE 1."""

import csv
from typing import Dict, Tuple

import config
import db


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _frac_posicao(posicao) -> float:
    """Pior posição => maior oportunidade. pos 1 -> 0.0; pos>=TETO -> 1.0."""
    if not posicao or posicao <= 1:
        return 0.0
    return _clamp((posicao - 1) / (config.POSICAO_TETO - 1))


def _frac_nota_baixa(nota) -> Tuple[float, bool]:
    """Nota < alvo => oportunidade de reputação. Sem nota => indisponível."""
    if nota is None:
        return 0.0, False
    frac = (config.NOTA_ALVO - nota) / (config.NOTA_ALVO - config.NOTA_PISO)
    return _clamp(frac), True


def _frac_volume(total) -> float:
    """Clínica ativa vale mais (curva com teto)."""
    return _clamp((total or 0) / config.VOLUME_ATIVO_TETO)


def score_lead(lead: Dict) -> Tuple[float, Dict]:
    """Retorna (score 0–100, breakdown detalhado)."""
    P = config.PESOS_SCORE
    nota = lead.get("avaliacao_num")
    total = lead.get("total_avaliacoes_num") or 0
    tem_site = bool(lead.get("tem_site"))
    tem_ig = bool(lead.get("tem_instagram"))
    posicao = lead.get("posicao")

    frac_nota, nota_disp = _frac_nota_baixa(nota)

    # (nome, fração 0–1, peso, disponível?)
    componentes = [
        ("posicao",                 _frac_posicao(posicao),        P["posicao"],                True),
        ("nota_baixa",              frac_nota,                     P["nota_baixa"],             nota_disp),
        ("sem_site",                1.0 if not tem_site else 0.0,  P["sem_site"],               True),
        ("sem_instagram",           1.0 if not tem_ig else 0.0,    P["sem_instagram"],          True),
        ("volume_avaliacoes",       _frac_volume(total),           P["volume_avaliacoes"],      True),
        # coletado? não. O scraper não traz status de resposta às avaliações.
        ("avaliacoes_sem_resposta", 0.0,                           P["avaliacoes_sem_resposta"], False),
    ]

    soma_pontos = 0.0
    soma_pesos_disp = 0.0
    detalhe = {}
    for nome, frac, peso, disp in componentes:
        pontos = frac * peso
        if disp:
            soma_pontos += pontos
            soma_pesos_disp += peso
        detalhe[nome] = {
            "frac": round(frac, 3),
            "peso": peso,
            "pontos": round(pontos, 2),
            "disponivel": disp,
        }

    raw = (soma_pontos / soma_pesos_disp * 100) if soma_pesos_disp else 0.0

    # ── Penalizações (regra do lead ideal) ────────────────────────────────────
    penalidades = []
    score = raw

    # Fantasma: clínica realmente inativa = pouco volume E SEM nota (sem reputação).
    # Se tem nota, tem movimento — não é fantasma (mesmo que o nº de avaliações não
    # tenha sido capturado). Evita punir injustamente clínica avaliada.
    if total < config.VOLUME_ATIVO_MIN and nota is None:
        score *= config.PENALIZA_FANTASMA
        penalidades.append(f"fantasma(sem nota e <{config.VOLUME_ATIVO_MIN} avaliações) x{config.PENALIZA_FANTASMA}")

    # Perfeita: nenhuma falha explorável -> pouco a vender.
    perfeita = (
        nota is not None and nota >= config.NOTA_ALVO
        and posicao and posicao <= 3
        and tem_site and tem_ig
    )
    if perfeita:
        score *= config.PENALIZA_PERFEITA
        penalidades.append(f"perfeita x{config.PENALIZA_PERFEITA}")

    score = round(_clamp(score, 0, 100), 1)

    breakdown = {
        "componentes": detalhe,
        "raw": round(raw, 1),
        "penalidades": penalidades,
        "final": score,
    }
    return score, breakdown


def run(export_csv: bool = True) -> int:
    """Pontua todos os leads, grava score e status=qualified, exporta CSV."""
    db.init_db()
    leads = db.all_leads()
    if not leads:
        print("⚠ Nenhum lead no banco. Rode ingest.py ou seed_amostra.py antes.")
        return 0

    scored = []
    for lead in leads:
        score, breakdown = score_lead(lead)
        db.set_score(lead["id"], score, breakdown)
        db.set_status(lead["id"], "qualified")
        lead["score"] = score
        scored.append(lead)

    scored.sort(key=lambda l: l["score"], reverse=True)

    if export_csv:
        _exportar_csv(scored)

    print(f"✅ {len(scored)} leads pontuados. CSV: {config.CSV_SAIDA}")
    print("\n🏆 TOP 10 leads (oportunidade):")
    print(f"{'#':>2}  {'score':>5}  {'pos':>3}  {'nota':>4}  {'aval':>5}  "
          f"{'site':>4} {'ig':>3}  nome")
    for i, l in enumerate(scored[:10], 1):
        nota = l["avaliacao_num"]
        print(f"{i:>2}  {l['score']:>5}  {str(l['posicao'] or '-'):>3}  "
              f"{(f'{nota:.1f}' if nota is not None else '  -'):>4}  "
              f"{l['total_avaliacoes_num']:>5}  "
              f"{'sim' if l['tem_site'] else 'NÃO':>4} "
              f"{'sim' if l['tem_instagram'] else 'NÃO':>3}  {l['nome']}")
    return len(scored)


def _exportar_csv(scored):
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.CSV_SAIDA, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["score", "posicao", "nome", "categoria", "nicho", "nota",
                    "total_avaliacoes", "tem_site", "tem_instagram", "telefone",
                    "instagram", "status"])
        for l in scored:
            w.writerow([
                l["score"], l["posicao"], l["nome"], l["categoria"], l["nicho"],
                l["avaliacao_num"] if l["avaliacao_num"] is not None else "",
                l["total_avaliacoes_num"],
                "sim" if l["tem_site"] else "nao",
                "sim" if l["tem_instagram"] else "nao",
                l["telefone"], l["instagram"], "qualified",
            ])


if __name__ == "__main__":
    run()
