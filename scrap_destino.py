# -*- coding: utf-8 -*-
"""scrap_destino.py — Manda os leads de uma busca para um quadro."""

from typing import Dict, List, Optional

import db

POLITICAS = ("ignorar", "substituir", "duplicar")


def init():
    """Liga cada lead à busca que o encontrou (idempotente)."""
    with db.conn() as con:
        try:
            con.execute("ALTER TABLE leads ADD COLUMN scrap_id INTEGER")
        except Exception:
            pass
        try:
            con.execute("CREATE INDEX IF NOT EXISTS ix_leads_scrap ON leads(scrap_id)")
        except Exception:
            pass


def resumo(sid: int) -> Dict:
    """O que esta busca rendeu, e onde esses leads estão agora."""
    init()
    with db.conn() as con:
        linhas = [dict(r) for r in con.execute(
            "SELECT l.id, l.nome, l.quadro_id, q.nome AS quadro_nome "
            "FROM leads l LEFT JOIN kanban_quadros q ON q.id = l.quadro_id "
            "WHERE l.scrap_id = ?", (int(sid),))]

    sem_quadro = [l for l in linhas if not l["quadro_id"]]
    por_quadro: Dict[str, int] = {}
    for l in linhas:
        if l["quadro_id"]:
            por_quadro[l["quadro_nome"] or f"#{l['quadro_id']}"] = \
                por_quadro.get(l["quadro_nome"] or f"#{l['quadro_id']}", 0) + 1

    return {
        "total": len(linhas),
        "sem_quadro": len(sem_quadro),
        "por_quadro": por_quadro,
        "amostra": [l["nome"] for l in linhas[:6]],
    }


def analisar(sid: int, quadro_id: int) -> Dict:
    """O que aconteceria ao mandar esta busca para este quadro — SEM mexer em nada."""
    init()
    quadro_id = int(quadro_id)
    with db.conn() as con:
        linhas = [dict(r) for r in con.execute(
            "SELECT l.id, l.nome, l.quadro_id, q.nome AS quadro_nome "
            "FROM leads l LEFT JOIN kanban_quadros q ON q.id = l.quadro_id "
            "WHERE l.scrap_id = ?", (int(sid),))]

    novos, ja_no_destino, repetidos = [], [], []
    for l in linhas:
        if not l["quadro_id"]:
            novos.append(l)
        elif int(l["quadro_id"]) == quadro_id:
            ja_no_destino.append(l)
        else:
            repetidos.append(l)

    origens: Dict[str, int] = {}
    for l in repetidos:
        nome = l["quadro_nome"] or f"quadro #{l['quadro_id']}"
        origens[nome] = origens.get(nome, 0) + 1

    return {
        "total": len(linhas),
        "novos": len(novos),
        "ja_no_destino": len(ja_no_destino),
        "repetidos": len(repetidos),
        "origens": origens,
        "exemplos_repetidos": [
            {"nome": l["nome"], "quadro": l["quadro_nome"] or f"#{l['quadro_id']}"}
            for l in repetidos[:5]],
        "precisa_escolher": bool(repetidos),
    }


def enviar(sid: int, quadro_id: int, politica: str = "ignorar") -> Dict:
    """Aplica o envio. `politica` só vale para os repetidos."""
    init()
    if politica not in POLITICAS:
        return {"ok": False, "erro": f"política inválida: {politica}"}

    quadro_id = int(quadro_id)
    with db.conn() as con:
        existe = con.execute("SELECT 1 FROM kanban_quadros WHERE id=?",
                             (quadro_id,)).fetchone()
    if not existe:
        return {"ok": False, "erro": "quadro não existe"}

    info = analisar(sid, quadro_id)

    with db.conn() as con:
        # 1) os inéditos entram sempre, qualquer que seja a política
        cur = con.execute(
            "UPDATE leads SET quadro_id=?, updated_at=datetime('now','localtime') "
            "WHERE scrap_id=? AND (quadro_id IS NULL OR quadro_id=0)",
            (quadro_id, int(sid)))
        movidos = cur.rowcount

        duplicados = 0
        substituidos = 0

        if politica == "substituir":
            cur = con.execute(
                "UPDATE leads SET quadro_id=?, updated_at=datetime('now','localtime') "
                "WHERE scrap_id=? AND quadro_id NOT IN (0, ?) AND quadro_id IS NOT NULL",
                (quadro_id, int(sid), quadro_id))
            substituidos = cur.rowcount

        elif politica == "duplicar":
            repetidos = [dict(r) for r in con.execute(
                "SELECT * FROM leads WHERE scrap_id=? AND quadro_id NOT IN (0, ?) "
                "AND quadro_id IS NOT NULL", (int(sid), quadro_id))]
            colunas = [d[1] for d in con.execute("PRAGMA table_info(leads)")]
            copiaveis = [c for c in colunas if c != "id"]
            for orig in repetidos:
                valores = []
                for c in copiaveis:
                    if c == "quadro_id":
                        valores.append(quadro_id)
                    elif c == "maps_url":
                        # a coluna é UNIQUE: a cópia precisa de uma chave própria
                        valores.append(f"{orig['maps_url']}#q{quadro_id}")
                    elif c == "kanban":
                        valores.append("lista")   # a cópia começa do início do funil
                    else:
                        valores.append(orig.get(c))
                marcas = ",".join("?" * len(copiaveis))
                try:
                    con.execute(
                        f"INSERT INTO leads ({','.join(copiaveis)}) VALUES ({marcas})",
                        valores)
                    duplicados += 1
                except Exception:
                    # já existia uma cópia deste lugar neste quadro
                    continue

    return {"ok": True, "quadro_id": quadro_id, "politica": politica,
            "movidos": movidos, "substituidos": substituidos,
            "duplicados": duplicados,
            "ignorados": info["repetidos"] if politica == "ignorar" else 0,
            "ja_estavam": info["ja_no_destino"]}


def descartar(sid: int) -> Dict:
    """Joga fora os leads desta busca que NÃO foram para quadro nenhum."""
    init()
    import dashboard_db as ddb

    with db.conn() as con:
        alvos = [r["id"] for r in con.execute(
            "SELECT id FROM leads WHERE scrap_id=? AND COALESCE(quadro_id,0)=0",
            (int(sid),))]
        em_quadro = con.execute(
            "SELECT COUNT(*) FROM leads WHERE scrap_id=? AND COALESCE(quadro_id,0)>0",
            (int(sid),)).fetchone()[0]

    # o lead de teste responde por rotas e pela bateria inteira; se ele cair
    # numa coleta por acidente, sai da lista em vez de derrubar a operação
    protegidos = [i for i in alvos if i in ddb.LEADS_PROTEGIDOS]
    alvos = [i for i in alvos if i not in ddb.LEADS_PROTEGIDOS]

    if not alvos:
        return {"ok": True, "excluidos": 0, "mantidos": em_quadro,
                "protegidos": len(protegidos)}

    with db.conn() as con:
        antes = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    # `origem='scrap-live'` é barrado por um gatilho do próprio SQLite: a trava
    # precisa ser aberta explicitamente, e é o que este `with` declara
    with db.exclusao_liberada():
        with db.conn() as con:
            con.executemany("DELETE FROM leads WHERE id=?", [(i,) for i in alvos])
    with db.conn() as con:
        depois = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]

    return {"ok": True, "excluidos": antes - depois, "mantidos": em_quadro,
            "protegidos": len(protegidos)}


def vincular_orfaos(sid: int) -> int:
    """Adota os leads sem quadro que ainda não têm busca ligada."""
    init()
    with db.conn() as con:
        cur = con.execute(
            "UPDATE leads SET scrap_id=? WHERE scrap_id IS NULL "
            "AND (quadro_id IS NULL OR quadro_id=0)", (int(sid),))
        return cur.rowcount
