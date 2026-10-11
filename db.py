"""db.py — Banco de dados PRÓPRIO do projeto (data/leadforge.db)."""

import sqlite3
import json
from contextlib import contextmanager
from typing import Dict, List, Optional

import config

# Ciclo de vida do lead no funil (Módulo E / CRM).
STATUS_FLUXO = [
    "sourced",         # veio do scraper
    "qualified",       # passou pelo scorer
    "contacted",       # primeiro toque enviado
    "replied",         # respondeu
    "diagnosed_sent",  # diagnóstico enviado
    "proposed",        # proposta enviada
    "won",
    "lost",
]


@contextmanager
def conn():
    """Conexão isolada ao banco do projeto, com commit/rollback automático."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.LEADFORGE_DB, timeout=10, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _instalar_trava_exclusao(con):
    """Impede, no próprio banco, que lead real seja apagado."""
    con.executescript("""
        CREATE TABLE IF NOT EXISTS trava_exclusao (
            id       INTEGER PRIMARY KEY CHECK (id = 1),
            liberado INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO trava_exclusao (id, liberado) VALUES (1, 0);

        CREATE TRIGGER IF NOT EXISTS trava_exclusao_lead_real
        BEFORE DELETE ON leads
        FOR EACH ROW
        WHEN (OLD.origem = 'scrap-live' OR OLD.id = 633)
         AND COALESCE((SELECT liberado FROM trava_exclusao WHERE id = 1), 0) = 0
        BEGIN
            SELECT RAISE(ABORT, 'lead real protegido: exclusao recusada pela trava do banco. Para tirar da prospeccao sem perder o dado use kanban=ignorado ou pausado=1. Exclusao deliberada: with db.exclusao_liberada().');
        END;
    """)


@contextmanager
def exclusao_liberada():
    """Abre a trava de exclusão pelo tempo de um bloco, e fecha mesmo se der erro."""
    with conn() as con:
        con.execute("UPDATE trava_exclusao SET liberado = 1 WHERE id = 1")
    try:
        yield
    finally:
        with conn() as con:
            con.execute("UPDATE trava_exclusao SET liberado = 0 WHERE id = 1")


def init_db():
    """Cria as tabelas do projeto se não existirem."""
    with conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS leads (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                maps_url              TEXT UNIQUE NOT NULL,
                nome                  TEXT DEFAULT '',
                categoria             TEXT DEFAULT '',
                nicho                 TEXT DEFAULT 'padrao',
                avaliacao_num         REAL,
                total_avaliacoes_num  INTEGER DEFAULT 0,
                tem_site              INTEGER DEFAULT 0,
                tem_instagram         INTEGER DEFAULT 0,
                instagram             TEXT DEFAULT '',
                telefone              TEXT DEFAULT '',
                website               TEXT DEFAULT '',
                endereco              TEXT DEFAULT '',
                posicao               INTEGER,
                patrocinado           INTEGER DEFAULT 0,
                score                 REAL,
                score_breakdown       TEXT DEFAULT '',
                status                TEXT DEFAULT 'sourced',
                created_at            TEXT DEFAULT (datetime('now','localtime')),
                updated_at            TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE INDEX IF NOT EXISTS idx_leads_score  ON leads(score DESC);
            CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
            CREATE INDEX IF NOT EXISTS idx_leads_nicho  ON leads(nicho);

            CREATE TABLE IF NOT EXISTS diagnosticos (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id           INTEGER REFERENCES leads(id) ON DELETE CASCADE,
                problemas         TEXT DEFAULT '',
                roi_reais         INTEGER DEFAULT 0,
                concorrente_nome  TEXT DEFAULT '',
                concorrente_nota  REAL,
                texto             TEXT DEFAULT '',
                created_at        TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_diag_lead ON diagnosticos(lead_id);

            CREATE TABLE IF NOT EXISTS propostas (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id     INTEGER REFERENCES leads(id) ON DELETE CASCADE,
                nivel       INTEGER,
                nivel_nome  TEXT DEFAULT '',
                preco       INTEGER,
                texto       TEXT DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_prop_lead ON propostas(lead_id);

            CREATE TABLE IF NOT EXISTS touches (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id        INTEGER REFERENCES leads(id) ON DELETE CASCADE,
                canal          TEXT,       -- instagram | whatsapp | email
                etapa          TEXT,       -- dia0, dia2, ...
                status         TEXT DEFAULT 'scheduled',  -- scheduled|sent|skipped|failed
                scheduled_for  TEXT,
                sent_at        TEXT,
                detalhe        TEXT DEFAULT '',
                created_at     TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_touch_lead ON touches(lead_id);
            CREATE INDEX IF NOT EXISTS idx_touch_status ON touches(status);
        """)
        _instalar_trava_exclusao(con)


# ── LEADS ─────────────────────────────────────────────────────────────────────

_LEAD_UPSERT_FIELDS = [
    "maps_url", "nome", "categoria", "nicho", "avaliacao_num",
    "total_avaliacoes_num", "tem_site", "tem_instagram", "instagram",
    "telefone", "website", "endereco", "posicao",
]


def upsert_lead(data: Dict) -> int:
    """Insere/atualiza um lead pelo maps_url. Retorna o id do lead."""
    row = {k: data.get(k) for k in _LEAD_UPSERT_FIELDS}
    with conn() as con:
        con.execute("""
            INSERT INTO leads
                (maps_url, nome, categoria, nicho, avaliacao_num, total_avaliacoes_num,
                 tem_site, tem_instagram, instagram, telefone, website, endereco, posicao)
            VALUES
                (:maps_url, :nome, :categoria, :nicho, :avaliacao_num, :total_avaliacoes_num,
                 :tem_site, :tem_instagram, :instagram, :telefone, :website, :endereco, :posicao)
            ON CONFLICT(maps_url) DO UPDATE SET
                nome=COALESCE(NULLIF(excluded.nome,''), nome),
                categoria=COALESCE(NULLIF(excluded.categoria,''), categoria),
                nicho=COALESCE(NULLIF(excluded.nicho,''), nicho),
                avaliacao_num=COALESCE(excluded.avaliacao_num, avaliacao_num),
                total_avaliacoes_num=CASE
                    WHEN COALESCE(excluded.total_avaliacoes_num,0) > 0
                    THEN excluded.total_avaliacoes_num
                    ELSE total_avaliacoes_num END,
                instagram=COALESCE(NULLIF(excluded.instagram,''), instagram),
                telefone=COALESCE(NULLIF(excluded.telefone,''), telefone),
                website=COALESCE(NULLIF(excluded.website,''), website),
                endereco=COALESCE(NULLIF(excluded.endereco,''), endereco),
                -- derivados do valor que sobreviveu à mesclagem, não da leitura crua
                tem_site=CASE WHEN COALESCE(NULLIF(excluded.website,''), website, '') <> ''
                              THEN 1 ELSE 0 END,
                tem_instagram=CASE WHEN COALESCE(NULLIF(excluded.instagram,''), instagram, '') <> ''
                                   THEN 1 ELSE 0 END,
                posicao=COALESCE(excluded.posicao, posicao),
                updated_at=datetime('now','localtime')
        """, row)
        r = con.execute("SELECT id FROM leads WHERE maps_url=?", (row["maps_url"],)).fetchone()
    return r["id"]


def set_score(lead_id: int, score: float, breakdown: Dict):
    with conn() as con:
        con.execute(
            "UPDATE leads SET score=?, score_breakdown=?, updated_at=datetime('now','localtime') "
            "WHERE id=?",
            (score, json.dumps(breakdown, ensure_ascii=False), lead_id),
        )


def set_status(lead_id: int, status: str):
    if status not in STATUS_FLUXO:
        raise ValueError(f"status inválido: {status}")
    with conn() as con:
        con.execute(
            "UPDATE leads SET status=?, updated_at=datetime('now','localtime') WHERE id=?",
            (status, lead_id),
        )


def get_lead(lead_id: int) -> Optional[Dict]:
    with conn() as con:
        r = con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    return dict(r) if r else None


def all_leads() -> List[Dict]:
    with conn() as con:
        rows = con.execute("SELECT * FROM leads").fetchall()
    return [dict(r) for r in rows]


def leads_by_score(limit: int = 30, status: Optional[str] = None) -> List[Dict]:
    q = "SELECT * FROM leads WHERE score IS NOT NULL"
    params: list = []
    if status:
        q += " AND status=?"
        params.append(status)
    q += " ORDER BY score DESC LIMIT ?"
    params.append(limit)
    with conn() as con:
        rows = con.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def forca_referencia(lead: Dict) -> float:
    """Quão FORTE esta clínica é como referência da região (0 a 100)."""
    import math
    nota = lead.get("avaliacao_num")
    total = lead.get("total_avaliacoes_num") or 0
    pos = lead.get("posicao")

    if nota is None or total <= 0:
        return 0.0  # sem reputação comprovada, não serve como referência

    f_nota = max(0.0, (nota - 3.0) / 2.0)                       # 3.0 -> 0 ; 5.0 -> 1
    f_vol = min(1.0, math.log10(total + 1) / math.log10(301))   # ~300 avaliações satura
    f_pos = max(0.0, 1 - ((pos or 30) - 1) / 29) if pos else 0.3
    f_pres = ((1 if lead.get("tem_site") else 0) + (1 if lead.get("tem_instagram") else 0)) / 2
    return round(100 * (0.38 * f_nota + 0.32 * f_vol + 0.20 * f_pos + 0.10 * f_pres), 1)


def leaders_by_nicho(nicho: str, exclude_lead_id: int, limit: int = 2) -> List[Dict]:
    """A MELHOR clínica da região no mesmo nicho, para servir de referência."""
    # `origem` filtra lead de teste: o 633 ('teste-rota') estava sendo escolhido
    # como referência de leads reais, o que colocaria uma clínica INEXISTENTE, com
    # nota e avaliações inventadas, dentro de um e-mail para prospect real.
    with conn() as con:
        colunas = {c[1] for c in con.execute("PRAGMA table_info(leads)")}
        alvo = con.execute("SELECT * FROM leads WHERE id=?", (exclude_lead_id,)).fetchone()
        alvo = dict(alvo) if alvo else {}

        recorte, args = "", [nicho, exclude_lead_id]
        if "cidade" in colunas:
            if (alvo.get("cidade") or "").strip():
                recorte = " AND COALESCE(cidade,'')=? "
                args.append(alvo["cidade"])
            elif (alvo.get("uf") or "").strip():
                # sem cidade apurada, a UF ainda separa os dois mercados
                recorte = " AND COALESCE(uf,'')=? "
                args.append(alvo["uf"])

        rows = con.execute(f"""
            SELECT * FROM leads
            WHERE nicho=? AND id<>? AND COALESCE(patrocinado,0)=0
              AND COALESCE(origem,'') NOT LIKE 'teste%'
              {recorte}
        """, args).fetchall()
    cands = [dict(r) for r in rows]
    if not cands:
        return []
    for c in cands:
        c["_forca"] = forca_referencia(c)
    # Decisão do usuário: a referência é sempre a MELHOR POSICIONADA da cidade no
    # nicho, a caminho do 1º lugar.
    cands.sort(key=lambda c: ((c.get("posicao") is None),
                              c.get("posicao") or 999,
                              -c["_forca"]))
    return cands[:limit]


# ── DIAGNÓSTICOS / PROPOSTAS ──────────────────────────────────────────────────

def save_diagnostico(lead_id: int, problemas: List[str], roi_reais: int,
                     concorrente_nome: str, concorrente_nota, texto: str) -> int:
    with conn() as con:
        cur = con.execute("""
            INSERT INTO diagnosticos
                (lead_id, problemas, roi_reais, concorrente_nome, concorrente_nota, texto)
            VALUES (?,?,?,?,?,?)
        """, (lead_id, json.dumps(problemas, ensure_ascii=False), roi_reais,
              concorrente_nome, concorrente_nota, texto))
        return cur.lastrowid


def get_diagnostico(lead_id: int) -> Optional[Dict]:
    with conn() as con:
        r = con.execute(
            "SELECT * FROM diagnosticos WHERE lead_id=? ORDER BY id DESC LIMIT 1",
            (lead_id,),
        ).fetchone()
    return dict(r) if r else None


def save_proposta(lead_id: int, nivel: int, nivel_nome: str, preco: int, texto: str) -> int:
    with conn() as con:
        cur = con.execute("""
            INSERT INTO propostas (lead_id, nivel, nivel_nome, preco, texto)
            VALUES (?,?,?,?,?)
        """, (lead_id, nivel, nivel_nome, preco, texto))
        return cur.lastrowid


def get_proposta(lead_id: int) -> Optional[Dict]:
    with conn() as con:
        r = con.execute(
            "SELECT * FROM propostas WHERE lead_id=? ORDER BY id DESC LIMIT 1",
            (lead_id,),
        ).fetchone()
    return dict(r) if r else None


# ── TOUCHES (cadência / CRM) ──────────────────────────────────────────────────

def schedule_touch(lead_id: int, canal: str, etapa: str, scheduled_for: str,
                   detalhe: str = "") -> int:
    with conn() as con:
        cur = con.execute("""
            INSERT INTO touches (lead_id, canal, etapa, status, scheduled_for, detalhe)
            VALUES (?,?,?,'scheduled',?,?)
        """, (lead_id, canal, etapa, scheduled_for, detalhe))
        return cur.lastrowid


def mark_touch(touch_id: int, status: str, detalhe: Optional[str] = None):
    with conn() as con:
        if detalhe is None:
            con.execute(
                "UPDATE touches SET status=?, sent_at=datetime('now','localtime') WHERE id=?",
                (status, touch_id),
            )
        else:
            con.execute(
                "UPDATE touches SET status=?, sent_at=datetime('now','localtime'), detalhe=? "
                "WHERE id=?",
                (status, detalhe, touch_id),
            )


def touches_for(lead_id: int) -> List[Dict]:
    with conn() as con:
        rows = con.execute(
            "SELECT * FROM touches WHERE lead_id=? ORDER BY scheduled_for", (lead_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def funnel_counts() -> Dict[str, int]:
    with conn() as con:
        rows = con.execute(
            "SELECT status, COUNT(*) c FROM leads GROUP BY status"
        ).fetchall()
    return {r["status"]: r["c"] for r in rows}


if __name__ == "__main__":
    init_db()
    print(f"✅ Banco do projeto inicializado em: {config.LEADFORGE_DB}")
    print("Tabelas: leads, diagnosticos, propostas, touches")
    print("Funil atual:", funnel_counts())
