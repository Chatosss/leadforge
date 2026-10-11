"""Persistência da caixa manual unificada, isolada por canal e conta."""

from datetime import datetime, timezone
import hashlib
import re
import sqlite3
import threading

import config
import db

_INIT_LOCK = threading.Lock()
_INITIALIZED = False


def _agora():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _quando(valor):
    """Horário da mensagem no fuso local, em ISO — o formato que ordena certo."""
    if valor in (None, ""):
        return _agora()
    try:
        n = float(valor)
        if n > 1e11:          # milissegundos
            n /= 1000.0
        return datetime.fromtimestamp(n, timezone.utc).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    try:
        d = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.astimezone()
        return d.astimezone().isoformat(timespec="seconds")
    except ValueError:
        return _agora()


def _colunas(con, tabela):
    return {r[1] for r in con.execute(f"PRAGMA table_info({tabela})")}


def _migrar(con):
    """Colunas novas da v023 em bancos criados pela v021."""
    cols = _colunas(con, "chat_threads")
    try:
        if "has_inbound" not in cols:
            con.execute("ALTER TABLE chat_threads ADD COLUMN has_inbound INTEGER NOT NULL DEFAULT 0")
            # até a v022 só entrava conversa por mensagem recebida
            con.execute("""UPDATE chat_threads SET has_inbound=1 WHERE EXISTS (
                SELECT 1 FROM chat_messages m
                WHERE m.thread_id=chat_threads.id AND m.direction='in')""")
        mcols = _colunas(con, "chat_messages")
        for nome, tipo in (("media_file", "TEXT"), ("media_mime", "TEXT"),
                           ("media_name", "TEXT"), ("media_size", "INTEGER"),
                           ("media_falha", "TEXT")):
            if nome not in mcols:
                con.execute(f"ALTER TABLE chat_messages ADD COLUMN {nome} {tipo}")
        if "last_inbound_at" not in cols:
            con.execute("ALTER TABLE chat_threads ADD COLUMN last_inbound_at TEXT")
            con.execute("""UPDATE chat_threads SET last_inbound_at=(
                SELECT MAX(m.created_at) FROM chat_messages m
                WHERE m.thread_id=chat_threads.id AND m.direction='in')""")
    except sqlite3.OperationalError as e:
        # outro processo migrou no mesmo instante: a coluna já existe
        if "duplicate column" not in str(e).lower():
            raise


def init():
    global _INITIALIZED
    if _INITIALIZED:
        return
    with _INIT_LOCK:
        if _INITIALIZED:
            return
        with db.conn() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS chat_threads (
                id INTEGER PRIMARY KEY,
                channel TEXT NOT NULL CHECK (channel IN ('whatsapp','instagram')),
                account TEXT NOT NULL,
                peer_id TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                last_message_at TEXT NOT NULL,
                last_message_preview TEXT NOT NULL DEFAULT '',
                unread_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE(channel, account, peer_id)
            );
            CREATE INDEX IF NOT EXISTS idx_chat_threads_recent
                ON chat_threads(last_message_at DESC);
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY,
                thread_id INTEGER NOT NULL REFERENCES chat_threads(id) ON DELETE CASCADE,
                direction TEXT NOT NULL CHECK (direction IN ('in','out')),
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                delivery_state TEXT NOT NULL DEFAULT 'received',
                provider_id TEXT,
                UNIQUE(thread_id, provider_id)
            );
            CREATE INDEX IF NOT EXISTS idx_chat_messages_thread
                ON chat_messages(thread_id, created_at, id);
            CREATE INDEX IF NOT EXISTS idx_chat_messages_provider
                ON chat_messages(provider_id);
            """)
            _migrar(con)
        _INITIALIZED = True


def whatsapp_peer_valido(peer_id):
    """O contato do WhatsApp é uma pessoa? Grupo e canal ficam de fora do chat."""
    p = str(peer_id or "").strip()
    return PEER_MIN <= len(p) <= PEER_MAX


PEER_MIN, PEER_MAX = 8, 15
# A mesma regra em SQL, para esconder o que já foi gravado antes dela.
_SQL_PEER_WHATSAPP_VALIDO = (f"NOT (channel='whatsapp' AND length(peer_id) "
                             f"NOT BETWEEN {PEER_MIN} AND {PEER_MAX})")


def registrar(channel, account, peer_id, text, display_name="", provider_id=None,
              direction="in", delivery_state="received", quando=None,
              conta_nao_lida=True):
    """Grava UMA mensagem. Devolve {"thread_id", "nova", "primeira_entrada"} ou None."""
    channel = str(channel or "").lower()
    account = str(account or "padrão").strip()[:100] or "padrão"
    peer_id = str(peer_id or "").strip()[:200]
    text = str(text or "")[:10000]
    provider_id = str(provider_id)[:200] if provider_id not in (None, "") else None
    direction = "out" if direction == "out" else "in"
    if channel not in {"whatsapp", "instagram"} or not peer_id or not text.strip():
        return None
    if channel == "whatsapp" and not whatsapp_peer_valido(peer_id):
        return None
    momento = _quando(quando)
    nome = str(display_name or "")[:200]
    init()
    with db.conn() as con:
        con.execute("""INSERT INTO chat_threads
            (channel,account,peer_id,display_name,last_message_at,last_message_preview,unread_count)
            VALUES (?,?,?,?,?,?,0)
            ON CONFLICT(channel,account,peer_id) DO NOTHING""",
            (channel, account, peer_id, nome, momento, text[:240]))
        t = con.execute("""SELECT id, has_inbound FROM chat_threads
            WHERE channel=? AND account=? AND peer_id=?""",
            (channel, account, peer_id)).fetchone()
        thread_id = t["id"]
        # A trava contra repetição é o UNIQUE(thread_id, provider_id): conferir
        # antes e inserir depois deixava dois gravadores simultâneos passarem
        # pela conferência e o segundo estourar o UNIQUE.
        cur = con.execute("""INSERT OR IGNORE INTO chat_messages
            (thread_id,direction,text,created_at,delivery_state,provider_id)
            VALUES (?,?,?,?,?,?)""",
            (thread_id, direction, text, momento, delivery_state, provider_id))
        if cur.rowcount == 0:
            return {"thread_id": thread_id, "nova": False, "primeira_entrada": False}
        entrada = 1 if direction == "in" else 0
        con.execute("""UPDATE chat_threads SET
              display_name = CASE WHEN ?<>'' THEN ? ELSE display_name END,
              last_message_preview = CASE WHEN ?>=last_message_at THEN ? ELSE last_message_preview END,
              last_message_at = MAX(last_message_at, ?),
              unread_count = unread_count + ?,
              has_inbound = MAX(has_inbound, ?),
              last_inbound_at = CASE WHEN ?=1 AND (last_inbound_at IS NULL OR ?>last_inbound_at)
                                     THEN ? ELSE last_inbound_at END
            WHERE id=?""",
            (nome, nome, momento, text[:240], momento,
             1 if (entrada and conta_nao_lida) else 0, entrada,
             entrada, momento, momento, thread_id))
    return {"thread_id": thread_id, "nova": True,
            "primeira_entrada": bool(entrada and not t["has_inbound"])}


def record(channel, account, peer_id, text, display_name="", provider_id=None,
           direction="in", delivery_state="received", quando=None):
    """Compatível com a v021: grava e devolve só o id da conversa."""
    r = registrar(channel, account, peer_id, text, display_name, provider_id,
                  direction, delivery_state, quando)
    return r["thread_id"] if r else None


def nome_da_conversa(channel, account, peer_id):
    """O nome já guardado da conversa ('' se ainda não há), sem criar nada."""
    init()
    with db.conn() as con:
        r = con.execute("""SELECT display_name FROM chat_threads
            WHERE channel=? AND account=? AND peer_id=?""",
            (channel, str(account)[:100], str(peer_id)[:200])).fetchone()
    return r["display_name"] if r else ""


def trocar_texto(channel, account, provider_id, novo_texto):
    """Edição ou exclusão feita pelo contato depois de a mensagem ter chegado."""
    if not provider_id or not str(novo_texto or "").strip():
        return 0
    init()
    with db.conn() as con:
        cur = con.execute("""UPDATE chat_messages SET text=?
            WHERE provider_id=? AND thread_id IN (
              SELECT id FROM chat_threads WHERE channel=? AND account=?)""",
            (str(novo_texto)[:10000], str(provider_id)[:200], channel, str(account)[:100]))
        return cur.rowcount


def list_threads(channel="", account="", search="", so_nao_lidas=False, manter_id=None):
    """
    Conversas da caixa. `so_nao_lidas` mostra só quem tem mensagem nova — mais
    `manter_id`, a conversa aberta na tela: abrir marca como lida, e sem isso ela
    sumiria da lista no instante em que é aberta.
    """
    init()
    # só conversa em que o contato escreveu: a abordagem sem resposta fica
    # gravada (aparece quando ele responder), mas não ocupa a lista
    sql = "SELECT * FROM chat_threads WHERE has_inbound=1 AND " + _SQL_PEER_WHATSAPP_VALIDO
    args = []
    if so_nao_lidas:
        sql += " AND (unread_count>0 OR id=?)"; args.append(int(manter_id or 0))
    if channel in {"whatsapp", "instagram"}:
        sql += " AND channel=?"; args.append(channel)
    if account:
        sql += " AND account=?"; args.append(account[:100])
    if search:
        sql += " AND (display_name LIKE ? OR peer_id LIKE ?)"
        args.extend([f"%{search[:100]}%"] * 2)
    sql += " ORDER BY last_message_at DESC,id DESC LIMIT 500"
    with db.conn() as con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]


def thread(thread_id):
    init()
    with db.conn() as con:
        r = con.execute("SELECT * FROM chat_threads WHERE id=?", (thread_id,)).fetchone()
        return dict(r) if r else None


def history(thread_id, limit=200):
    init()
    with db.conn() as con:
        con.execute("UPDATE chat_threads SET unread_count=0 WHERE id=? AND unread_count>0", (thread_id,))
        rows = con.execute("""SELECT id,direction,text,created_at,delivery_state,
                   provider_id,media_file,media_mime,media_name,media_falha
            FROM chat_messages WHERE thread_id=?
            ORDER BY created_at DESC, id DESC LIMIT ?""",
            (thread_id, max(1, min(limit, 500)))).fetchall()
        saida = []
        for r in reversed(rows):
            m = dict(r)
            m["media"] = _descrever_midia(m)
            if m["media"] and not m.get("media_file") and m.get("media_falha") in FALHAS_DEFINITIVAS:
                m["media"]["indisponivel"] = {"sumiu": "o WhatsApp não tem mais este arquivo",
                                              "empate": "duas mídias no mesmo instante: não dá para saber qual é esta",
                                              "grande": "arquivo acima de 25 MB"}[m["media_falha"]]
            for k in ("provider_id", "media_file", "media_mime", "media_name", "media_falha"):
                m.pop(k, None)
            saida.append(m)
        return saida


# ── Mídia (v024): foto, áudio, vídeo e documento do WhatsApp ─────────────────
# O arquivo mora em data/chat_midia/<sha256>.<ext> e a mensagem guarda só o
# nome.
MIDIA_DIR = config.DATA_DIR / "chat_midia"
MIDIA_MAX_BYTES = 25 * 1024 * 1024

# rótulo do bot -> tipo de mídia (o mesmo ROTULO_MIDIA do server.js)
_ROTULO_TIPO = {"imagem": "imagem", "figurinha": "figurinha", "áudio": "audio",
                "vídeo": "video", "documento": "documento"}
_RE_ROTULO = re.compile(r"^\[(imagem|figurinha|áudio|vídeo|documento)\](?:\s+(.*))?$", re.S)

# O que pode ser mostrado no próprio painel. Fora disto o arquivo só baixa
# (application/octet-stream): SVG e HTML mandados por um contato executariam
# script no painel.
MIME_NA_TELA = {
    "image/jpeg", "image/png", "image/webp", "image/gif",
    "audio/ogg", "audio/mpeg", "audio/mp4", "audio/aac", "audio/wav", "audio/webm", "audio/amr",
    "video/mp4", "video/webm", "video/3gpp",
    "application/pdf",
}
_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
        "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/aac": ".aac",
        "audio/wav": ".wav", "audio/webm": ".weba", "audio/amr": ".amr", "video/mp4": ".mp4",
        "video/webm": ".webm", "video/3gpp": ".3gp", "application/pdf": ".pdf"}


def mime_limpo(mime):
    """'audio/ogg; codecs=opus' -> 'audio/ogg'."""
    return str(mime or "").split(";", 1)[0].strip().lower()


def _tipo_do_mime(mime, padrao="documento"):
    m = mime_limpo(mime)
    if m.startswith("image/"):
        return "imagem"
    if m.startswith("audio/"):
        return "audio"
    if m.startswith("video/"):
        return "video"
    return padrao


def _descrever_midia(m):
    """O que o painel precisa para desenhar a mídia de uma mensagem (ou None)."""
    rot = _RE_ROTULO.match(str(m.get("text") or "").strip())
    # só o rótulo, sem arquivo: o painel busca no WhatsApp ao pedir (pelo id
    # ou, nas gravadas sem id, pelo horário)
    if not m.get("media_file") and not rot:
        return None
    tipo = (_tipo_do_mime(m.get("media_mime"), _ROTULO_TIPO.get(rot.group(1), "documento") if rot else "documento")
            if m.get("media_file") else _ROTULO_TIPO[rot.group(1)])
    if rot and rot.group(1) == "figurinha":
        tipo = "figurinha"
    nome = m.get("media_name") or (rot.group(2) if rot and tipo == "documento" else "") or ""
    return {"tipo": tipo, "nome": str(nome)[:200], "baixada": bool(m.get("media_file")),
            "url": f"/api/chat/mensagens/{m['id']}/midia"}


def anexar_midia(channel, account, provider_id, mime, nome, dados):
    """
    Guarda o arquivo de uma mensagem já gravada (achada pelo provider_id). Devolve o id
    da mensagem, ou None se ela não existe ou o arquivo é inválido.
    """
    if not provider_id or not dados or len(dados) > MIDIA_MAX_BYTES:
        return None
    init()
    with db.conn() as con:
        r = con.execute("""SELECT m.id FROM chat_messages m JOIN chat_threads t ON t.id=m.thread_id
            WHERE m.provider_id=? AND t.channel=? AND t.account=?""",
            (str(provider_id)[:200], channel, str(account)[:100])).fetchone()
    if not r:
        return None
    return gravar_midia_da_mensagem(r["id"], mime, nome, dados)


def gravar_midia_da_mensagem(msg_id, mime, nome, dados, provider_id=None):
    if not dados or len(dados) > MIDIA_MAX_BYTES:
        return None
    mime = mime_limpo(mime)
    MIDIA_DIR.mkdir(parents=True, exist_ok=True)
    arq = hashlib.sha256(dados).hexdigest() + _EXT.get(mime, ".bin")
    caminho = MIDIA_DIR / arq
    if not caminho.exists():
        tmp = caminho.with_suffix(caminho.suffix + ".tmp")
        tmp.write_bytes(dados)
        tmp.replace(caminho)
    nome = nome_seguro(nome)
    with db.conn() as con:
        con.execute("""UPDATE chat_messages SET media_file=?, media_mime=?, media_name=?, media_size=?
            WHERE id=?""", (arq, mime, nome, len(dados), msg_id))
        if provider_id:
            # mensagem gravada sem id (até a v024): ganha o id agora; se outra
            # cópia da mesma mensagem já o tem, o UNIQUE recusa e fica como está
            try:
                con.execute("UPDATE chat_messages SET provider_id=? WHERE id=? AND provider_id IS NULL",
                            (str(provider_id)[:200], msg_id))
            except sqlite3.IntegrityError:
                pass
    return msg_id


# Caracteres que invertem/escondem o texto: "fatura\u202efdp.exe" aparece como
# "faturaexe.pdf" na tela e no download.
_INVISIVEIS = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
# Lista do que PODE (a lista do que não pode sempre tem
# buraco:.cpl,.msc,.url,.iso,.docm...). Fora dela o nome ganha ".bloqueado" e
# não abre com dois cliques.
_EXTENSOES_OK = (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".mp3", ".ogg", ".opus",
                 ".m4a", ".aac", ".wav", ".amr", ".mp4", ".mov", ".3gp", ".webm", ".doc", ".docx",
                 ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".txt", ".csv", ".zip", ".rar")


def nome_seguro(nome):
    """Nome de arquivo vindo do contato, pronto para a tela e o download."""
    n = _INVISIVEIS.sub("", str(nome or ""))
    n = re.sub(r"[\x00-\x1f\\/:*?\"<>|]+", "_", n).strip(" .")[:200]
    if n and not n.lower().endswith(_EXTENSOES_OK):
        n += ".bloqueado"
    return n


# Falha ao buscar no WhatsApp: "sumiu"/"empate"/"grande" é para sempre; o resto
# (conta desconectada, bot reiniciando, rede) tenta de novo em 10 min.
ESPERA_FALHA_S = 600
FALHAS_DEFINITIVAS = ("sumiu", "empate", "grande")


def marcar_falha_midia(msg_id, motivo):
    init()
    valor = motivo if motivo in FALHAS_DEFINITIVAS else _agora()
    with db.conn() as con:
        con.execute("UPDATE chat_messages SET media_falha=? WHERE id=?", (valor, msg_id))


def pode_buscar_midia(info):
    f = info.get("media_falha")
    if not f:
        return True
    if f in FALHAS_DEFINITIVAS:
        return False
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(f)).total_seconds() > ESPERA_FALHA_S
    except ValueError:
        return True


def ids_de_midia_da_conversa(thread_id):
    """provider_id das mensagens desta conversa que já têm arquivo (não se repetem)."""
    init()
    with db.conn() as con:
        return [r[0] for r in con.execute(
            "SELECT provider_id FROM chat_messages WHERE thread_id=? AND provider_id IS NOT NULL "
            "AND media_file IS NOT NULL", (thread_id,))]


def indice_no_instante(info):
    """
    Posição desta mensagem entre as da conversa com o MESMO horário, direção e tipo (0,
    1, 2...). Num álbum todas as fotos têm o mesmo horário: é a ordem que diz qual foto
    é de qual linha.
    """
    tipo = tipo_do_rotulo(info.get("text"))
    init()
    with db.conn() as con:
        linhas = con.execute(
            "SELECT id, text FROM chat_messages WHERE thread_id=? AND created_at=? AND direction=? "
            "ORDER BY id", (info["thread_id"], info["created_at"], info["direction"])).fetchall()
    mesmas = [r["id"] for r in linhas if tipo_do_rotulo(r["text"]) == tipo]
    return mesmas.index(info["id"]) if info["id"] in mesmas else 0


def tipo_do_rotulo(texto):
    rot = _RE_ROTULO.match(str(texto or "").strip())
    return _ROTULO_TIPO.get(rot.group(1)) if rot else None


def midia_da_mensagem(msg_id):
    """Dados da mensagem para servir (ou buscar) a mídia dela."""
    init()
    with db.conn() as con:
        r = con.execute("""SELECT m.id, m.thread_id, m.text, m.provider_id, m.media_file, m.media_mime,
                   m.media_name, m.media_falha, m.direction, m.created_at, t.channel, t.account, t.peer_id
            FROM chat_messages m JOIN chat_threads t ON t.id=m.thread_id WHERE m.id=?""",
            (msg_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    if d["media_file"]:
        caminho = MIDIA_DIR / d["media_file"]
        # o nome vem do banco, mas confere: só arquivo DESTA pasta
        d["caminho"] = caminho if caminho.parent == MIDIA_DIR and caminho.exists() else None
    return d
