"""dashboard_db.py — Extensão do banco do projeto (leadforge.db) para o Dashboard."""

import json
import pathlib
import re
from typing import Dict, List, Optional

import config
import db

# ── Colunas do Kanban (ordem = fluxo) ─────────────────────────────────────────
# (chave, rótulo, cor) `enviado` virou `contato_iniciado`: o rótulo "Enviado"
# ficaria ambíguo ao lado de "Material enviado", que é outra etapa.
KANBAN_SEED = [
    ("lista",            "Adicionados à lista", "#64748b", 1),
    ("material",         "Material pronto",     "#7c3aed", 0),
    ("entrar_contato",   "Entrar em contato",   "#0ea5e9", 1),
    ("contato_iniciado", "Contato iniciado",    "#2563eb", 1),
    ("problema",         "Problema",            "#dc2626", 1),
    ("respondeu",        "Respondeu",           "#16a34a", 1),
    ("material_enviado", "Material enviado",    "#6366f1", 0),
    ("conversa",         "Em conversa",         "#0891b2", 0),
    ("reuniao",          "Reunião marcada",     "#d97706", 0),
    ("nao_compareceu",   "Não compareceu",      "#b45309", 0),
    ("fechado",          "Fechado ✅",           "#16a34a", 0),
    ("ignorado",         "Ignorado",            "#94a3b8", 0),
    ("negado",           "Negado",              "#dc2626", 0),
]

# As cinco que o fluxo de envio exige. Fora destas, tudo é do operador.
KANBAN_OBRIGATORIAS = ("lista", "entrar_contato", "contato_iniciado",
                       "problema", "respondeu")


# ── Quadros (múltiplos funis) ─────────────────────────────────────────────────
# Um quadro é um funil independente: os leads da empresa A não aparecem, não
# entram na fila de envio nem contam nas estatísticas do quadro da empresa B.

QUADRO_PADRAO_NOME = "Principal"


def _seed_quadros():
    """Garante o quadro padrão e adota os leads que existiam antes dos quadros."""
    with db.conn() as con:
        n = con.execute("SELECT COUNT(*) FROM kanban_quadros").fetchone()[0]
        if not n:
            con.execute("INSERT INTO kanban_quadros(nome,cor,ordem,padrao) "
                        "VALUES(?,?,0,1)", (QUADRO_PADRAO_NOME, "#00ff88"))
        # o id do padrão não é necessariamente 1 (banco pode ter sido mexido)
        pid = con.execute("SELECT id FROM kanban_quadros WHERE padrao=1 "
                          "ORDER BY id LIMIT 1").fetchone()
        if pid:
            con.execute("UPDATE leads SET quadro_id=? WHERE quadro_id IS NULL", (pid[0],))


def quadro_padrao_id() -> int:
    with db.conn() as con:
        r = con.execute("SELECT id FROM kanban_quadros WHERE padrao=1 "
                        "ORDER BY id LIMIT 1").fetchone()
        if r:
            return r[0]
        r = con.execute("SELECT id FROM kanban_quadros ORDER BY id LIMIT 1").fetchone()
        return r[0] if r else 1


def quadros() -> List[Dict]:
    """Os quadros com a contagem de leads de cada um."""
    with db.conn() as con:
        return [dict(r) for r in con.execute("""
            SELECT q.id, q.nome, q.cor, q.ordem, q.padrao,
                   (SELECT COUNT(*) FROM leads l WHERE l.quadro_id = q.id) AS leads
            FROM kanban_quadros q ORDER BY q.ordem, q.id
        """)]


def quadro_ativo() -> int:
    """O quadro que o painel está mostrando — guardado no servidor, não no navegador."""
    raw = get_config("quadro_ativo")
    ids = {q["id"] for q in quadros()}
    try:
        qid = int(raw)
    except (TypeError, ValueError):
        qid = 0
    return qid if qid in ids else quadro_padrao_id()


def set_quadro_ativo(qid: int) -> Dict:
    if qid not in {q["id"] for q in quadros()}:
        return {"ok": False, "erro": "quadro não existe"}
    set_config("quadro_ativo", str(int(qid)))
    return {"ok": True, "quadro_id": int(qid)}


def quadro_criar(nome: str, cor: str = "#00ff88") -> Dict:
    nome = (nome or "").strip()
    if not nome:
        return {"ok": False, "erro": "Dê um nome ao quadro"}
    if len(nome) > 60:
        nome = nome[:60]
    with db.conn() as con:
        existe = con.execute("SELECT 1 FROM kanban_quadros WHERE lower(nome)=lower(?)",
                             (nome,)).fetchone()
        if existe:
            return {"ok": False, "erro": f'Já existe um quadro chamado "{nome}"'}
        ordem = con.execute("SELECT COALESCE(MAX(ordem),0)+1 FROM kanban_quadros").fetchone()[0]
        cur = con.execute("INSERT INTO kanban_quadros(nome,cor,ordem,padrao) VALUES(?,?,?,0)",
                          (nome, cor or "#00ff88", ordem))
        return {"ok": True, "id": cur.lastrowid, "nome": nome, "quadros": quadros()}


def quadro_renomear(qid: int, nome: str = None, cor: str = None) -> Dict:
    nome = (nome or "").strip()
    with db.conn() as con:
        if not con.execute("SELECT 1 FROM kanban_quadros WHERE id=?", (qid,)).fetchone():
            return {"ok": False, "erro": "quadro não existe"}
        if nome:
            dup = con.execute("SELECT 1 FROM kanban_quadros WHERE lower(nome)=lower(?) "
                              "AND id<>?", (nome, qid)).fetchone()
            if dup:
                return {"ok": False, "erro": f'Já existe um quadro chamado "{nome}"'}
            con.execute("UPDATE kanban_quadros SET nome=? WHERE id=?", (nome[:60], qid))
        if cor:
            con.execute("UPDATE kanban_quadros SET cor=? WHERE id=?", (cor, qid))
    return {"ok": True, "quadros": quadros()}


def quadro_excluir(qid: int, destino_id: int = None) -> Dict:
    """Apaga um quadro. Os leads NÃO são apagados junto — vão para outro quadro."""
    qs = {q["id"]: q for q in quadros()}
    if qid not in qs:
        return {"ok": False, "erro": "quadro não existe"}
    if qs[qid]["padrao"]:
        return {"ok": False, "erro": "O quadro padrão não pode ser excluído"}
    if len(qs) < 2:
        return {"ok": False, "erro": "É preciso ter ao menos um quadro"}

    destino = destino_id if destino_id in qs and destino_id != qid else quadro_padrao_id()
    with db.conn() as con:
        movidos = con.execute("SELECT COUNT(*) FROM leads WHERE quadro_id=?", (qid,)).fetchone()[0]
        con.execute("UPDATE leads SET quadro_id=? WHERE quadro_id=?", (destino, qid))
        con.execute("DELETE FROM kanban_quadros WHERE id=?", (qid,))
    if quadro_ativo() == qid:
        set_config("quadro_ativo", str(destino))
    return {"ok": True, "movidos": movidos, "destino": destino,
            "destino_nome": qs[destino]["nome"], "quadros": quadros()}


def kanban_cols() -> List[Dict]:
    """As colunas em vigor, na ordem da tela."""
    with db.conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT chave, rotulo, cor, ordem, sistema FROM kanban_cols "
            "ORDER BY ordem, id")]


def kanban_keys() -> List[str]:
    return [c["chave"] for c in kanban_cols()]

# Colunas de onde uma resposta do lead o move para "Respondeu". As demais ficam
# de fora de propósito: quem já está em "Reunião marcada" ou "Fechado" mandando
# mensagem não volta para o começo do funil, isso seria regressão do pipeline.
KANBAN_ANTES_DA_RESPOSTA = ("lista", "material", "entrar_contato", "contato_iniciado")

CANAIS = ["email", "whatsapp", "instagram"]


def init_dashboard():
    """Cria colunas/tabelas do dashboard (idempotente)."""
    db.init_db()
    with db.conn() as con:
        # migrações nas colunas de leads
        migr = [
            "ALTER TABLE leads ADD COLUMN kanban TEXT DEFAULT 'lista'",
            "ALTER TABLE leads ADD COLUMN email TEXT DEFAULT ''",
            "ALTER TABLE leads ADD COLUMN cnpj TEXT DEFAULT ''",
            "ALTER TABLE leads ADD COLUMN razao_social TEXT DEFAULT ''",
            "ALTER TABLE leads ADD COLUMN gov_json TEXT DEFAULT ''",
            "ALTER TABLE leads ADD COLUMN origem TEXT DEFAULT 'scraper'",
            "ALTER TABLE leads ADD COLUMN whatsapp TEXT DEFAULT ''",
            "ALTER TABLE leads ADD COLUMN tem_whatsapp INTEGER",
            "ALTER TABLE leads ADD COLUMN pagespeed_json TEXT DEFAULT ''",
            "ALTER TABLE leads ADD COLUMN site_apurado INTEGER DEFAULT 0",
            "ALTER TABLE leads ADD COLUMN kanban_ordem REAL DEFAULT 0",
            "ALTER TABLE leads ADD COLUMN patrocinado INTEGER DEFAULT 0",
            # cidade apurada por `cidade.py`.
            "ALTER TABLE leads ADD COLUMN cidade TEXT DEFAULT ''",
            "ALTER TABLE leads ADD COLUMN uf TEXT DEFAULT ''",
            "ALTER TABLE leads ADD COLUMN cidade_fonte TEXT DEFAULT ''",
            # a campanha sorteada para este lead no primeiro contato.
            "ALTER TABLE leads ADD COLUMN campanha_id INTEGER",
            # quadro a que o lead pertence; NULL vira o quadro padrão no _seed_quadros
            "ALTER TABLE leads ADD COLUMN quadro_id INTEGER",
        ]
        for sql in migr:
            try:
                con.execute(sql)
            except Exception:
                pass

        con.executescript("""
            CREATE TABLE IF NOT EXISTS lead_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER REFERENCES leads(id) ON DELETE CASCADE,
                texto TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_notes_lead ON lead_notes(lead_id);

            CREATE TABLE IF NOT EXISTS lead_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER REFERENCES leads(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                path TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_att_lead ON lead_attachments(lead_id);

            CREATE TABLE IF NOT EXISTS msg_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canal TEXT NOT NULL,           -- email | whatsapp | instagram
                nome TEXT NOT NULL,
                assunto TEXT DEFAULT '',
                corpo TEXT NOT NULL,
                etapa TEXT DEFAULT '',         -- etapa da esteira, '' = avulso
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS scrap_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                termo TEXT NOT NULL,
                localizacao TEXT DEFAULT '',
                status TEXT DEFAULT 'pendente', -- pendente | rodando | concluido | erro
                resultado INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS app_config (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            -- Quadros: um funil por empresa atendida. As COLUNAS são as mesmas
            -- para todos (o processo de venda não muda); o que separa é o
            -- conjunto de leads. Cada lead pertence a exatamente um quadro.
            CREATE TABLE IF NOT EXISTS kanban_quadros (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                nome       TEXT NOT NULL,
                cor        TEXT NOT NULL DEFAULT '#00ff88',
                ordem      INTEGER NOT NULL DEFAULT 0,
                padrao     INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
        """)
        # banco que já existia antes da esteira: a coluna entra por migração.
        # Fica DEPOIS do executescript de propósito — antes dele a tabela ainda
        # não existe num banco novo, o ALTER falha calado e a coluna some.
        try:
            con.execute("ALTER TABLE msg_templates ADD COLUMN etapa TEXT DEFAULT ''")
        except Exception:
            pass

        # ── Campanhas ──────────────────────────────────────────────────────── Uma
        # campanha é uma sequência inteira: abertura (dia 0) + reforços nos dias do
        # `config.ESTEIRA_WHATSAPP`.
        con.executescript("""
            CREATE TABLE IF NOT EXISTS kanban_cols (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                chave   TEXT NOT NULL UNIQUE,
                rotulo  TEXT NOT NULL,
                cor     TEXT NOT NULL DEFAULT '#64748b',
                ordem   INTEGER NOT NULL DEFAULT 0,
                sistema INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS campanhas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                canal TEXT NOT NULL DEFAULT 'whatsapp',
                ativa INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
        """)
        for sql in ("ALTER TABLE msg_templates ADD COLUMN campanha_id INTEGER",
                    "ALTER TABLE msg_templates ADD COLUMN ordem INTEGER DEFAULT 0",
                    # como esta busca deve ser feita: 'busca' e uma consulta so, 'estado' varre o
                    # estado inteiro area por area.
                    "ALTER TABLE scrap_queue ADD COLUMN modo TEXT DEFAULT 'busca'",
                    "ALTER TABLE scrap_queue ADD COLUMN orcamento INTEGER DEFAULT 30",
                    # onde a varredura parou: as areas ja visitadas e as que faltam, em JSON.
                    "ALTER TABLE scrap_queue ADD COLUMN progresso TEXT DEFAULT ''"):
            try:
                con.execute(sql)
            except Exception:
                pass
    _seed_quadros()
    _seed_kanban()
    _seed_defaults()
    _seed_esteira()
    _seed_campanhas()


def _seed_kanban():
    """Popula as colunas na primeira vez e REPÕE as de sistema que faltarem."""
    with db.conn() as con:
        # `material`, `material_enviado` e `ignorado` deixaram de ser de sistema: nenhum
        # código depende delas para enviar, e travá-las só impedia o operador de montar
        # o funil do jeito dele. O UPDATE realinha bases antigas.
        marks = ",".join("?" * len(KANBAN_OBRIGATORIAS))
        con.execute(f"UPDATE kanban_cols SET sistema=0 WHERE chave NOT IN ({marks})",
                    KANBAN_OBRIGATORIAS)
        con.execute(f"UPDATE kanban_cols SET sistema=1 WHERE chave IN ({marks})",
                    KANBAN_OBRIGATORIAS)

        existentes = {r["chave"] for r in con.execute(
            "SELECT chave FROM kanban_cols")}
        # Numa base já povoada, repõe só as OBRIGATÓRIAS. Repor todas ressuscitaria no
        # próximo boot a coluna que o operador acabou de apagar.
        primeira_vez = not existentes
        for i, (chave, rotulo, cor, sistema) in enumerate(KANBAN_SEED):
            if chave in existentes:
                continue
            if not primeira_vez and chave not in KANBAN_OBRIGATORIAS:
                continue
            # coluna nova numa base que já roda entra na posição da semente, e as criadas
            # pelo operador ficam depois (ordem 100+)
            con.execute("INSERT INTO kanban_cols(chave,rotulo,cor,ordem,sistema) "
                        "VALUES(?,?,?,?,?)", (chave, rotulo, cor, i, sistema))


def _seed_defaults():
    # ferramentas ativas por padrão
    if get_config("ferramentas") is None:
        set_config("ferramentas", json.dumps(
            {"email": True, "whatsapp": True, "instagram": True}))
    # templates iniciais por canal (se vazio)
    if not listar_templates():
        _seed_templates()


def _seed_templates():
    seeds = [
        ("instagram", "Abordagem padrão", "",
         "Oi! Passando aqui rapidinho 👋 Dei uma olhada na presença digital da {nome} no Google e "
         "vi alguns pontos que podem estar te custando pacientes. Fiz um mini-diagnóstico com "
         "números — quer que eu te mande sem compromisso?"),
        ("whatsapp", "Abordagem padrão", "",
         "Olá! Aqui é da equipe Presença Digital para Clínicas. Fizemos um raio-x rápido da {nome} "
         "e encontramos oportunidades de atrair mais pacientes. Posso te enviar o diagnóstico?"),
        ("email", "Diagnóstico", "{nome}: 3 pontos que podem estar custando pacientes",
         "Olá! Segue o diagnóstico da presença digital da {nome}.\n\n{corpo}\n\n"
         "Podemos conversar sem compromisso?"),
    ]
    for canal, nome, assunto, corpo in seeds:
        criar_template(canal, nome, assunto, corpo)


def _seed_esteira():
    """Cria um template por etapa da esteira, se ainda não existir."""
    if listar_templates("whatsapp"):
        return
    NL = chr(10)     # quebra de linha, montada aqui para o texto ficar legível
    textos = {
        "wa1_abertura":
            "Oi! Aqui é o {atendente}. Vi a {nome_curto} e fiz um teste rápido: "
            "busquei no Google como quem procura {servico} na região e a clínica "
            "de vocês não apareceu entre as primeiras." + NL +
            "Isso costuma ser um detalhe no perfil. Vocês cuidam disso "
            "internamente ou fica meio no automático?",
        "wa2_reforco":
            "Complementando, {nome_curto}: hoje a maioria dos pacientes escolhe "
            "buscando \"perto de mim\" no celular e clicando num dos primeiros "
            "que aparece com boas avaliações e foto." + NL +
            "Quem não aparece bem nessa busca simplesmente não entra na "
            "comparação. Foi isso que vi no perfil de vocês, e dá para corrigir "
            "sem gastar com anúncio.",
        "wa3_isca":
            "{nome_curto}, para facilitar: preparei um diagnóstico do perfil de "
            "vocês no Google mostrando os pontos que estão travando a busca e "
            "como a concorrência aparece." + NL +
            "Te mando aqui sem compromisso nenhum. Se fizer sentido a gente "
            "conversa, se não, você fica com o diagnóstico. Pode ser?",
        "wa4_encerramento":
            "Imagino que a correria aí esteja grande, então não vou insistir. "
            "Encerro por aqui." + NL +
            "Se em algum momento quiser ver os pacientes que hoje estão indo "
            "para clínicas que aparecem melhor no Google, é só me chamar que eu "
            "retomo na hora. Sucesso com a {nome_curto}!",
    }
    for e in config.ESTEIRA_WHATSAPP:
        if template_da_etapa(e["etapa"]):
            continue
        criar_template("whatsapp", e["rotulo"], "", textos[e["etapa"]], e["etapa"])


# ── Campanhas ─────────────────────────────────────────────────────────────────
# Campanha = a sequência inteira de um modelo de abordagem: abertura (dia 0) +
# reforços nos dias definidos em `config.ESTEIRA_WHATSAPP`.

def etapa_da_ordem(ordem: int) -> str:
    etapas = config.ESTEIRA_ETAPAS
    return etapas[ordem] if 0 <= ordem < len(etapas) else ""


def MAX_MENSAGENS_CAMPANHA() -> int:
    """Abertura + reforços. Vem do config: cada posição precisa de um dia."""
    return len(config.ESTEIRA_WHATSAPP)


def _seed_campanhas():
    """Converte os templates soltos que já existiam em campanhas, uma única vez."""
    with db.conn() as con:
        if con.execute("SELECT COUNT(*) FROM campanhas").fetchone()[0]:
            return
        avulsos = [dict(r) for r in con.execute(
            "SELECT * FROM msg_templates WHERE canal='whatsapp' "
            "AND COALESCE(etapa,'')='' AND campanha_id IS NULL ORDER BY id")]
        if not avulsos:
            return
        # os reforços de hoje: da 2ª posição em diante
        reforcos = []
        for ordem in range(1, MAX_MENSAGENS_CAMPANHA()):
            etapa = etapa_da_ordem(ordem)
            r = con.execute(
                "SELECT * FROM msg_templates WHERE etapa=? AND campanha_id IS NULL "
                "ORDER BY id LIMIT 1", (etapa,)).fetchone()
            if r:
                reforcos.append((ordem, etapa, dict(r)))

        for i, abertura in enumerate(avulsos):
            cur = con.execute(
                "INSERT INTO campanhas(nome,canal,ativa) VALUES(?,?,1)",
                (abertura.get("nome") or f"Campanha {i + 1}", "whatsapp"))
            cid = cur.lastrowid
            con.execute("UPDATE msg_templates SET campanha_id=?, ordem=0, etapa=? "
                        "WHERE id=?", (cid, etapa_da_ordem(0), abertura["id"]))
            for ordem, etapa, tpl in reforcos:
                if i == 0:
                    # a primeira campanha adota os originais
                    con.execute("UPDATE msg_templates SET campanha_id=?, ordem=? "
                                "WHERE id=?", (cid, ordem, tpl["id"]))
                else:
                    con.execute(
                        "INSERT INTO msg_templates(canal,nome,assunto,corpo,etapa,"
                        "campanha_id,ordem) VALUES(?,?,?,?,?,?,?)",
                        ("whatsapp", tpl.get("nome") or etapa, "",
                         tpl.get("corpo") or "", etapa, cid, ordem))


def listar_campanhas(canal: str = "whatsapp", so_ativas: bool = False) -> List[Dict]:
    """As campanhas, cada uma com as mensagens em ordem."""
    with db.conn() as con:
        sql = "SELECT * FROM campanhas WHERE canal=?"
        if so_ativas:
            sql += " AND ativa=1"
        camps = [dict(r) for r in con.execute(sql + " ORDER BY id", (canal,))]
        for c in camps:
            c["mensagens"] = [dict(r) for r in con.execute(
                "SELECT * FROM msg_templates WHERE campanha_id=? "
                "ORDER BY COALESCE(ordem,0), id", (c["id"],))]
            for m in c["mensagens"]:
                passo = next((e for e in config.ESTEIRA_WHATSAPP
                              if e["etapa"] == (m.get("etapa") or "")), None)
                m["dia"] = passo["dia"] if passo else None
                m["rotulo_passo"] = ("Abertura" if (m.get("ordem") or 0) == 0
                                     else f"Reforço {m.get('ordem')}")
            c["completa"] = bool(c["mensagens"]) and bool(
                (c["mensagens"][0].get("corpo") or "").strip())
    return camps


def obter_campanha(cid: int) -> Optional[Dict]:
    todas = listar_campanhas(canal="whatsapp")
    return next((c for c in todas if c["id"] == cid), None)


def criar_campanha(nome: str, canal: str = "whatsapp") -> int:
    nome = (nome or "").strip()
    if not nome:
        raise ValueError("a campanha precisa de um nome")
    with db.conn() as con:
        return con.execute("INSERT INTO campanhas(nome,canal,ativa) VALUES(?,?,1)",
                           (nome, canal)).lastrowid


def atualizar_campanha(cid: int, campos: Dict) -> bool:
    sets, params = [], []
    if "nome" in campos:
        nome = str(campos.get("nome") or "").strip()
        if not nome:
            raise ValueError("o nome da campanha não pode ficar vazio")
        sets.append("nome=?")
        params.append(nome)
    if "ativa" in campos:
        sets.append("ativa=?")
        params.append(1 if campos.get("ativa") else 0)
    if not sets:
        return False
    params.append(cid)
    with db.conn() as con:
        cur = con.execute(f"UPDATE campanhas SET {','.join(sets)} WHERE id=?", params)
        return cur.rowcount > 0


def excluir_campanha(cid: int) -> Dict:
    """Apaga a campanha e as mensagens dela."""
    with db.conn() as con:
        usos = con.execute("SELECT COUNT(*) FROM leads WHERE campanha_id=?",
                           (cid,)).fetchone()[0]
        if usos:
            return {"ok": False, "erro": f"{usos} lead(s) estão nesta campanha. "
                                         f"Desative em vez de excluir."}
        con.execute("DELETE FROM msg_templates WHERE campanha_id=?", (cid,))
        con.execute("DELETE FROM campanhas WHERE id=?", (cid,))
    return {"ok": True}


def template_da_campanha(cid: int, ordem: int) -> Optional[Dict]:
    with db.conn() as con:
        r = con.execute(
            "SELECT * FROM msg_templates WHERE campanha_id=? AND COALESCE(ordem,0)=? "
            "ORDER BY id LIMIT 1", (cid, ordem)).fetchone()
    return dict(r) if r else None


def campanhas_para_rodizio() -> List[Dict]:
    """As campanhas que podem receber leads novos: ativas E com abertura escrita."""
    return [c for c in listar_campanhas("whatsapp", so_ativas=True) if c["completa"]]


CHAVE_RODIZIO = "rodizio_ultima_campanha"


def uso_das_campanhas() -> Dict:
    """Quantos leads entraram em cada campanha. Só para mostrar o placar."""
    with db.conn() as con:
        return {r[0]: r[1] for r in con.execute(
            "SELECT campanha_id, COUNT(*) FROM leads WHERE campanha_id IS NOT NULL "
            "GROUP BY campanha_id")}


def ultima_campanha_usada() -> Optional[int]:
    bruto = get_config(CHAVE_RODIZIO)
    try:
        return int(bruto)
    except (TypeError, ValueError):
        return None


def registrar_campanha_usada(cid: int):
    set_config(CHAVE_RODIZIO, str(int(cid)))


def proxima_campanha(campanhas: List[Dict], ultima_id: Optional[int] = None) -> Dict:
    """A próxima da fila: a seguinte à última atribuída, em círculo pelo id."""
    ordenadas = sorted(campanhas, key=lambda c: c["id"])
    if ultima_id is None:
        ultima_id = ultima_campanha_usada()
    if ultima_id is not None:
        for c in ordenadas:
            if c["id"] > ultima_id:
                return c
    return ordenadas[0]


def definir_campanha_do_lead(lead_id: int, cid: int):
    with db.conn() as con:
        con.execute("UPDATE leads SET campanha_id=? WHERE id=?", (cid, lead_id))


# ── Config (key-value) ────────────────────────────────────────────────────────

def get_config(key: str) -> Optional[str]:
    with db.conn() as con:
        r = con.execute("SELECT value FROM app_config WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def set_config(key: str, value: str):
    with db.conn() as con:
        con.execute("INSERT OR REPLACE INTO app_config(key,value) VALUES(?,?)", (key, value))


def get_ferramentas() -> Dict:
    raw = get_config("ferramentas")
    return json.loads(raw) if raw else {"email": True, "whatsapp": True, "instagram": True}


# ── Leads ─────────────────────────────────────────────────────────────────────

def leads_full(quadro_id: int = None) -> List[Dict]:
    """Os leads de um quadro. `quadro_id=None` traz TODOS os quadros."""
    filtro, args = "", ()
    if quadro_id is not None:
        filtro, args = "WHERE l.quadro_id = ?", (int(quadro_id),)
    with db.conn() as con:
        rows = con.execute(f"""
            SELECT l.*,
                   (SELECT COUNT(*) FROM lead_notes n WHERE n.lead_id=l.id) AS n_notas,
                   (SELECT COUNT(*) FROM lead_attachments a WHERE a.lead_id=l.id) AS n_anexos,
                   (SELECT roi_reais FROM diagnosticos d WHERE d.lead_id=l.id ORDER BY d.id DESC LIMIT 1) AS roi,
                   (SELECT nivel_nome FROM propostas p WHERE p.lead_id=l.id ORDER BY p.id DESC LIMIT 1) AS nivel
            FROM leads l
            {filtro}
            ORDER BY (l.score IS NULL), l.score DESC
        """, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if not d.get("kanban"):
            d["kanban"] = "lista"
        out.append(d)
    return out


def lead_detalhe(lead_id: int) -> Optional[Dict]:
    lead = db.get_lead(lead_id)
    if not lead:
        return None
    lead["notas"] = notas_do_lead(lead_id)
    lead["anexos"] = anexos_do_lead(lead_id)
    lead["diagnostico"] = db.get_diagnostico(lead_id)
    lead["proposta"] = db.get_proposta(lead_id)
    if lead.get("gov_json"):
        try:
            lead["gov"] = json.loads(lead["gov_json"])
        except Exception:
            lead["gov"] = None
    return lead


class ColunaProtegida(Exception):
    """Não dá para apagar esta coluna."""


def _slug(texto: str) -> str:
    import unicodedata
    base = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-zA-Z0-9]+", "_", base).strip("_").lower()
    return base[:32]


def criar_coluna(rotulo: str, cor: str = "#64748b") -> Dict:
    """Cria uma coluna. A CHAVE é derivada do rótulo e não muda depois."""
    rotulo = (rotulo or "").strip()
    if not rotulo:
        raise ValueError("a coluna precisa de um nome")
    chave = _slug(rotulo)
    if not chave:
        raise ValueError("o nome precisa ter ao menos uma letra ou número")
    with db.conn() as con:
        if con.execute("SELECT 1 FROM kanban_cols WHERE chave=?", (chave,)).fetchone():
            raise ValueError(f"já existe uma coluna com a chave '{chave}'")
        ordem = (con.execute("SELECT COALESCE(MAX(ordem),0) FROM kanban_cols"
                             ).fetchone()[0] or 0) + 1
        con.execute("INSERT INTO kanban_cols(chave,rotulo,cor,ordem,sistema) "
                    "VALUES(?,?,?,?,0)", (chave, rotulo, cor or "#64748b", ordem))
    return {"chave": chave, "rotulo": rotulo, "cor": cor}


def atualizar_coluna(chave: str, campos: Dict) -> bool:
    """Renomeia ou troca a cor. A chave nunca muda: leads apontam para ela."""
    sets, params = [], []
    if "rotulo" in campos:
        rotulo = str(campos.get("rotulo") or "").strip()
        if not rotulo:
            raise ValueError("o nome da coluna não pode ficar vazio")
        sets.append("rotulo=?")
        params.append(rotulo)
    if "cor" in campos:
        sets.append("cor=?")
        params.append(str(campos.get("cor") or "#64748b"))
    if "ordem" in campos:
        sets.append("ordem=?")
        params.append(int(campos.get("ordem") or 0))
    if not sets:
        return False
    params.append(chave)
    with db.conn() as con:
        return con.execute(f"UPDATE kanban_cols SET {','.join(sets)} WHERE chave=?",
                           params).rowcount > 0


def reordenar_colunas(chaves: list) -> int:
    """Regrava a ordem das colunas na sequência recebida (arrastar na tela)."""
    chaves = [str(c) for c in (chaves or [])]
    if not chaves:
        raise ValueError("nenhuma coluna informada")
    with db.conn() as con:
        existentes = [r["chave"] for r in con.execute(
            "SELECT chave FROM kanban_cols ORDER BY ordem, id")]
        validas = [c for c in chaves if c in existentes]
        if not validas:
            raise ValueError("nenhuma das colunas informadas existe")
        resto = [c for c in existentes if c not in validas]
        for i, chave in enumerate(validas + resto):
            con.execute("UPDATE kanban_cols SET ordem=? WHERE chave=?", (i, chave))
        return len(validas)


def excluir_coluna(chave: str, destino: str = None) -> Dict:
    """Apaga a coluna. Os leads que estavam nela vão para `destino`."""
    with db.conn() as con:
        col = con.execute("SELECT * FROM kanban_cols WHERE chave=?", (chave,)).fetchone()
        if not col:
            return {"ok": False, "erro": "coluna não encontrada"}
        if chave in KANBAN_OBRIGATORIAS:
            raise ColunaProtegida(
                f"'{col['rotulo']}' é uma das cinco colunas que o fluxo de envio "
                f"usa pelo nome. Dá para renomear e trocar a cor, mas não apagar.")

        n = con.execute("SELECT COUNT(*) FROM leads WHERE kanban=?", (chave,)).fetchone()[0]
        if n:
            outras = [r["chave"] for r in con.execute(
                "SELECT chave FROM kanban_cols WHERE chave<>? ORDER BY ordem, id", (chave,))]
            alvo = destino if destino in outras else ("lista" if "lista" in outras
                                                      else (outras[0] if outras else None))
            if not alvo:
                raise ColunaProtegida("não há outra coluna para receber os leads")
            con.execute("UPDATE leads SET kanban=?, updated_at=datetime('now','localtime') "
                        "WHERE kanban=?", (alvo, chave))
        else:
            alvo = None
        con.execute("DELETE FROM kanban_cols WHERE chave=?", (chave,))
    return {"ok": True, "movidos": n, "destino": alvo}


def set_kanban_opcional(lead_id: int, kanban: str) -> bool:
    """Move o lead se a coluna existir; se o operador apagou aquela coluna, não move."""
    if kanban not in kanban_keys():
        return False
    set_kanban(lead_id, kanban)
    return True


def set_kanban(lead_id: int, kanban: str):
    if kanban not in kanban_keys():
        raise ValueError(f"coluna kanban inválida: {kanban}")
    with db.conn() as con:
        con.execute("UPDATE leads SET kanban=?, updated_at=datetime('now','localtime') WHERE id=?",
                    (kanban, lead_id))


def atualizar_lead(lead_id: int, campos: Dict):
    permitidos = {"nome", "categoria", "nicho", "telefone", "website", "instagram",
                  "email", "endereco", "cnpj", "razao_social"}
    # Nome vazio é recusado aqui, não só na tela: o nome é o que o primeiro toque
    # pergunta ("é da Clínica X?"), o que assina o PDF e o que vira o nome do
    # arquivo do material.
    if "nome" in campos:
        nome = str(campos.get("nome") or "").strip()
        if not nome:
            raise ValueError("o nome da clínica não pode ficar vazio")
        campos = {**campos, "nome": nome}
    sets, params = [], []
    for k, v in campos.items():
        if k in permitidos:
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return
    params.append(lead_id)
    with db.conn() as con:
        con.execute(f"UPDATE leads SET {','.join(sets)}, updated_at=datetime('now','localtime') "
                    f"WHERE id=?", params)


def inserir_manual(dados: Dict) -> int:
    """Insere um lead manualmente (origem=manual)."""
    import time
    maps_url = dados.get("maps_url") or f"manual://{int(time.time()*1000)}"
    lead = {
        "maps_url": maps_url,
        "nome": dados.get("nome", "Sem nome"),
        "categoria": dados.get("categoria", ""),
        "nicho": dados.get("nicho", config.NICHO_PADRAO),
        "avaliacao_num": dados.get("avaliacao_num"),
        "total_avaliacoes_num": dados.get("total_avaliacoes_num", 0),
        "tem_site": 1 if dados.get("website") else 0,
        "tem_instagram": 1 if dados.get("instagram") else 0,
        "instagram": dados.get("instagram", ""),
        "telefone": dados.get("telefone", ""),
        "website": dados.get("website", ""),
        "endereco": dados.get("endereco", ""),
        "posicao": dados.get("posicao"),
    }
    lead_id = db.upsert_lead(lead)
    campos = {"email": dados.get("email", ""), "cnpj": dados.get("cnpj", ""),
              "razao_social": dados.get("razao_social", "")}
    atualizar_lead(lead_id, campos)
    # entra no quadro pedido, ou no que está aberto na tela
    qid = dados.get("quadro_id") or quadro_ativo()
    with db.conn() as con:
        con.execute("UPDATE leads SET origem='manual', kanban='lista', quadro_id=? "
                    "WHERE id=?", (int(qid), lead_id))
    return lead_id


def mover_para_quadro(ids: list, quadro_id: int) -> int:
    """Move leads já existentes para outro quadro. Devolve quantos mudaram."""
    ids = [int(i) for i in (ids or [])]
    if not ids:
        return 0
    if quadro_id not in {q["id"] for q in quadros()}:
        raise ValueError("quadro não existe")
    marks = ",".join("?" * len(ids))
    with db.conn() as con:
        cur = con.execute(
            f"UPDATE leads SET quadro_id=?, updated_at=datetime('now','localtime') "
            f"WHERE id IN ({marks}) AND quadro_id<>?",
            [int(quadro_id)] + ids + [int(quadro_id)])
        return cur.rowcount


# ── Trava de exclusão ─────────────────────────────────────────────────────────
# Em 01/08 uma exclusão pelo dashboard apagou 94 dos 95 leads reais.
ORIGENS_PROTEGIDAS = ("scrap-live",)
LEADS_PROTEGIDOS = (633,)          # 'teste-rota': operar.py --teste depende dele


class LeadProtegido(Exception):
    """Exclusão recusada."""

    def __init__(self, mensagem, forcavel=True):
        super().__init__(mensagem)
        self.forcavel = forcavel


def _checar_intocaveis(con, ids):
    """Recusa mesmo com `forcar`."""
    achados = []
    for lid in ids:
        row = con.execute("SELECT id, nome FROM leads WHERE id=?", (lid,)).fetchone()
        if row is not None and row["id"] in LEADS_PROTEGIDOS:
            achados.append(f"{row['id']} {row['nome'][:40]}")
    if achados:
        raise LeadProtegido(
            "este é o lead de TESTE, e a exclusão dele é recusada mesmo com "
            "confirmação:\n  " + "\n  ".join(achados) +
            "\nA rota de teste (operar.py --teste) e a bateria de testes "
            "dependem dele. Tire-o da seleção e apague o resto.",
            forcavel=False)


def _checar_exclusao(con, ids):
    """Levanta LeadProtegido se algum id for lead real ou o lead de teste."""
    protegidos = []
    for (lid,) in [(i,) for i in ids]:
        row = con.execute("SELECT id, nome, origem FROM leads WHERE id=?", (lid,)).fetchone()
        if row is None:
            continue
        if row["id"] in LEADS_PROTEGIDOS or row["origem"] in ORIGENS_PROTEGIDAS:
            protegidos.append(f"{row['id']} ({row['origem']}) {row['nome'][:40]}")
    if protegidos:
        raise LeadProtegido(
            "exclusão recusada, estes leads são dado real de prospecção e levam "
            "identidade, laudo, diagnóstico, proposta, notas e toques junto no "
            "CASCADE:\n  " + "\n  ".join(protegidos) +
            "\nPara tirar da prospecção sem perder o dado, mova para a coluna "
            "'ignorado' ou marque pausado. Exclusão real exige forcar=True."
        )


def excluir_lead(lead_id: int, forcar: bool = False):
    with db.conn() as con:
        _checar_intocaveis(con, [lead_id])
        if not forcar:
            _checar_exclusao(con, [lead_id])
    if forcar:
        # a trava do BANCO (trigger) também precisa ser aberta: sem isto o
        # DELETE aborta no SQLite mesmo com o Python de acordo
        with db.exclusao_liberada():
            with db.conn() as con:
                con.execute("DELETE FROM leads WHERE id=?", (lead_id,))
        return
    with db.conn() as con:
        con.execute("DELETE FROM leads WHERE id=?", (lead_id,))


# ── Ordenação / ações em massa ────────────────────────────────────────────────

def reordenar(kanban: str, ids: list):
    """Grava a ordem manual dos cards numa coluna (não usado na 'lista')."""
    if kanban not in kanban_keys():
        raise ValueError("coluna inválida")
    with db.conn() as con:
        for i, lid in enumerate(ids):
            con.execute("UPDATE leads SET kanban=?, kanban_ordem=?, "
                        "updated_at=datetime('now','localtime') WHERE id=?", (kanban, i, lid))


def bulk_mover(ids: list, kanban: str):
    if kanban not in kanban_keys():
        raise ValueError("coluna inválida")
    with db.conn() as con:
        for lid in ids:
            con.execute("UPDATE leads SET kanban=?, updated_at=datetime('now','localtime') "
                        "WHERE id=?", (kanban, lid))


def bulk_excluir(ids: list, forcar: bool = False) -> int:
    """
    Exclusão em massa. Foi por aqui que os 94 leads reais foram embora de uma vez, então
    a checagem roda ANTES de qualquer DELETE: ou nenhum id da lista é protegido, ou nada
    é apagado. Falhar pela metade seria o pior dos dois mundos.
    """
    with db.conn() as con:
        _checar_intocaveis(con, ids)
        if not forcar:
            _checar_exclusao(con, ids)
        antes = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    if forcar:
        # abre também a trava do banco: o trigger recusaria o DELETE de
        # `scrap-live` mesmo com a checagem do Python satisfeita
        with db.exclusao_liberada():
            with db.conn() as con:
                con.executemany("DELETE FROM leads WHERE id=?", [(i,) for i in ids])
    else:
        with db.conn() as con:
            con.executemany("DELETE FROM leads WHERE id=?", [(i,) for i in ids])
    with db.conn() as con:
        depois = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    # o que sumiu de verdade, não o tamanho da lista pedida: id que não existia
    # mais não pode virar "excluído" na contagem da tela
    return antes - depois


def excluir_artificiais() -> int:
    """
    Remove os leads de SEED/demo (origem='scraper'), mantendo prospecção e manuais.
    """
    with db.conn() as con:
        n = con.execute("SELECT COUNT(*) FROM leads WHERE origem='scraper'").fetchone()[0]
        con.execute("DELETE FROM leads WHERE origem='scraper'")
    return n


# ── Notas ─────────────────────────────────────────────────────────────────────

def add_nota(lead_id: int, texto: str) -> int:
    with db.conn() as con:
        cur = con.execute("INSERT INTO lead_notes(lead_id,texto) VALUES(?,?)", (lead_id, texto))
        return cur.lastrowid


def notas_do_lead(lead_id: int) -> List[Dict]:
    with db.conn() as con:
        rows = con.execute("SELECT * FROM lead_notes WHERE lead_id=? ORDER BY id DESC",
                           (lead_id,)).fetchall()
    return [dict(r) for r in rows]


# ── Anexos ────────────────────────────────────────────────────────────────────

def add_anexo(lead_id: int, filename: str, path: str) -> int:
    with db.conn() as con:
        cur = con.execute("INSERT INTO lead_attachments(lead_id,filename,path) VALUES(?,?,?)",
                          (lead_id, filename, path))
        return cur.lastrowid


def anexos_do_lead(lead_id: int) -> List[Dict]:
    with db.conn() as con:
        rows = con.execute("SELECT * FROM lead_attachments WHERE lead_id=? ORDER BY id DESC",
                           (lead_id,)).fetchall()
    return [dict(r) for r in rows]


def anexo(att_id: int) -> Optional[Dict]:
    with db.conn() as con:
        r = con.execute("SELECT * FROM lead_attachments WHERE id=?", (att_id,)).fetchone()
    return dict(r) if r else None


def excluir_anexo(att_id: int, apagar_arquivo: bool = True) -> Dict:
    """Remove o anexo do lead. Devolve {ok, filename, arquivo_apagado}."""
    import os

    a = anexo(att_id)
    if not a:
        return {"ok": False, "erro": "anexo não encontrado"}

    with db.conn() as con:
        outros = con.execute(
            "SELECT COUNT(*) FROM lead_attachments WHERE path=? AND id<>?",
            (a["path"], att_id)).fetchone()[0]
        con.execute("DELETE FROM lead_attachments WHERE id=?", (att_id,))

    apagado = False
    if apagar_arquivo and not outros:
        try:
            caminho = pathlib.Path(a["path"])
            # trava de caminho: só apaga dentro das pastas do projeto. Sem isto,
            # um `path` adulterado no banco viraria remoção de arquivo arbitrário
            permitidas = [config.OUT_DIR.resolve(), (config.DATA_DIR / "attachments").resolve()]
            alvo = caminho.resolve()
            if any(str(alvo).startswith(str(d)) for d in permitidas) and alvo.exists():
                os.remove(alvo)
                apagado = True
        except Exception:
            pass

    return {"ok": True, "filename": a["filename"], "arquivo_apagado": apagado,
            "compartilhado": bool(outros)}


# ── Templates ─────────────────────────────────────────────────────────────────

def criar_template(canal: str, nome: str, assunto: str, corpo: str,
                   etapa: str = "", campanha_id: int = None,
                   ordem: int = None) -> int:
    """
    Cria um template. Dentro de uma campanha, a `etapa` é derivada da `ordem`: são a
    mesma coisa vista de dois lados (posição na sequência × passo da esteira), e deixar
    as duas entrarem soltas era o caminho para elas discordarem.
    """
    if campanha_id is not None and ordem is not None:
        etapa = etapa_da_ordem(int(ordem))
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO msg_templates(canal,nome,assunto,corpo,etapa,campanha_id,ordem) "
            "VALUES(?,?,?,?,?,?,?)",
            (canal, nome, assunto, corpo, etapa or "", campanha_id,
             0 if ordem is None else int(ordem)))
        return cur.lastrowid


def template_da_etapa(etapa: str) -> Optional[Dict]:
    """
    O template SOLTO de uma etapa da esteira: o caminho dos leads que não têm campanha
    (os anteriores ao modelo de campanhas).
    """
    with db.conn() as con:
        r = con.execute(
            "SELECT * FROM msg_templates WHERE etapa=? AND campanha_id IS NULL "
            "ORDER BY id LIMIT 1", (etapa,)).fetchone()
    return dict(r) if r else None


def listar_templates(canal: str = None) -> List[Dict]:
    with db.conn() as con:
        if canal:
            rows = con.execute("SELECT * FROM msg_templates WHERE canal=? ORDER BY id", (canal,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM msg_templates ORDER BY canal, id").fetchall()
    return [dict(r) for r in rows]


def obter_template(tid: int) -> Optional[Dict]:
    with db.conn() as con:
        r = con.execute("SELECT * FROM msg_templates WHERE id=?", (tid,)).fetchone()
    return dict(r) if r else None


def atualizar_template(tid: int, campos: Dict) -> bool:
    """Edita um template existente. Só os campos enviados mudam."""
    permitidos = {"canal", "nome", "assunto", "corpo", "etapa", "campanha_id", "ordem"}
    mudar = {k: v for k, v in (campos or {}).items() if k in permitidos}
    if not mudar:
        return False
    # dentro de campanha, a etapa segue a ordem — nunca o que veio na requisição
    if mudar.get("ordem") is not None and mudar.get("campanha_id") is not None:
        mudar["etapa"] = etapa_da_ordem(int(mudar["ordem"]))
    sets = ", ".join(f"{k}=?" for k in mudar)
    with db.conn() as con:
        cur = con.execute(f"UPDATE msg_templates SET {sets} WHERE id=?",
                          [*mudar.values(), tid])
    return cur.rowcount > 0


def excluir_template(tid: int):
    with db.conn() as con:
        con.execute("DELETE FROM msg_templates WHERE id=?", (tid,))


# ── Fila de scrap ─────────────────────────────────────────────────────────────

def add_scrap(termo: str, localizacao: str, modo: str = "busca",
              orcamento: int = 30) -> int:
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO scrap_queue(termo,localizacao,modo,orcamento) VALUES(?,?,?,?)",
            (termo, localizacao, modo if modo in ("busca", "estado") else "busca",
             max(1, int(orcamento or 30))))
        return cur.lastrowid


def listar_scrap() -> List[Dict]:
    """A fila de buscas."""
    import json
    saida = []
    with db.conn() as con:
        for r in con.execute("SELECT * FROM scrap_queue ORDER BY id DESC"):
            item = dict(r)
            bruto = item.pop("progresso", "") or ""
            item["tem_progresso"] = bool(bruto)
            item["areas_feitas"] = 0
            if bruto:
                try:
                    item["areas_feitas"] = int(json.loads(bruto).get("consultas", 0))
                except Exception:
                    pass
            saida.append(item)
    return saida


def set_scrap_status(sid: int, status: str, resultado: int = 0):
    with db.conn() as con:
        con.execute("UPDATE scrap_queue SET status=?, resultado=? WHERE id=?",
                    (status, resultado, sid))


# ── Parar e continuar uma coleta ──────────────────────────────────────────────
# A coleta roda em OUTRO processo (scrap_live.py), lancado pelo painel.

def pedir_parada(sid: int) -> Dict:
    """Pede que a coleta pare. Ela obedece na proxima rolagem."""
    with db.conn() as con:
        linha = con.execute("SELECT status FROM scrap_queue WHERE id=?",
                            (int(sid),)).fetchone()
        if linha is None:
            return {"ok": False, "erro": "busca nao encontrada"}
        if linha["status"] != "rodando":
            return {"ok": False, "erro": f"esta busca nao esta rodando "
                                         f"(status: {linha['status']})",
                    "status": linha["status"]}
        con.execute("UPDATE scrap_queue SET status='parando' WHERE id=?", (int(sid),))
    return {"ok": True, "status": "parando"}


def parada_pedida(sid: int) -> bool:
    """A coleta consulta isto entre as rolagens."""
    try:
        with db.conn() as con:
            linha = con.execute("SELECT status FROM scrap_queue WHERE id=?",
                                (int(sid),)).fetchone()
        return bool(linha) and linha["status"] == "parando"
    except Exception:
        # banco ocupado nao pode derrubar a coleta: na duvida, segue trabalhando
        return False


def salvar_progresso(sid: int, estado) -> None:
    """Grava o ponto de retomada (ou o apaga, com None)."""
    import json
    with db.conn() as con:
        con.execute("UPDATE scrap_queue SET progresso=? WHERE id=?",
                    (json.dumps(estado, ensure_ascii=False) if estado else "",
                     int(sid)))


def ler_progresso(sid: int) -> Optional[Dict]:
    """O ponto de retomada, se houver um."""
    import json
    with db.conn() as con:
        linha = con.execute("SELECT progresso FROM scrap_queue WHERE id=?",
                            (int(sid),)).fetchone()
    if not linha or not linha["progresso"]:
        return None
    try:
        return json.loads(linha["progresso"])
    except Exception:
        # progresso ilegivel nao pode impedir uma coleta nova: vale como se nao
        # houvesse ponto de retomada
        return None


def excluir_scrap(sid: int):
    with db.conn() as con:
        con.execute("DELETE FROM scrap_queue WHERE id=?", (sid,))


# ── Estatísticas ──────────────────────────────────────────────────────────────

def stats(quadro_id: int = None) -> Dict:
    leads = leads_full(quadro_id)
    por_col = {k: 0 for k in kanban_keys()}
    roi_total = 0
    for l in leads:
        por_col[l.get("kanban", "lista")] = por_col.get(l.get("kanban", "lista"), 0) + 1
        roi_total += l.get("roi") or 0
    return {
        "total": len(leads),
        "por_coluna": por_col,
        "fechados": por_col.get("fechado", 0),
        "em_negociacao": por_col.get("conversa", 0) + por_col.get("reuniao", 0),
        "roi_pipeline": roi_total,
    }


if __name__ == "__main__":
    init_dashboard()
    print("✅ Dashboard DB inicializado.")
    print("Colunas Kanban:", kanban_keys())
    print("Stats:", stats())
