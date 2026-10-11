"""bridge.py — Módulo D (Prospectar / Ponte multicanal)."""

import argparse
import importlib.util
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import config
import db
import abordagem
import diagnostico as diag_mod
import identidade
import proposta as prop_mod
from senders import email_sender, instagram_sender, whatsapp_sender

try:
    import requests
except ImportError:
    requests = None


# ── Extração de contatos a partir do lead ─────────────────────────────────────

def ig_username(lead) -> str | None:
    ig = (lead.get("instagram") or "").strip()
    if not ig:
        return None
    ig = re.sub(r"^https?://", "", ig)
    if "instagram.com" in ig:
        ig = ig.split("instagram.com/")[-1]
    ig = ig.strip("/@ ").split("/")[0].split("?")[0]
    return ig or None


def email_lead(lead):
    """
    Retorna (email, origem). origem: 'real' | 'derivado' | 'ausente'. O scraper NÃO
    coleta e-mail; na v1 derivamos contato@<dominio> do site como PLACEHOLDER (nunca
    enviado em dry-run). Quando houver e-mail real (enriquecimento ou import), é só
    preencher e a origem passa a 'real'.
    """
    real = (lead.get("email") or "").strip() if lead.get("email") else ""
    if real:
        return real, "real"
    site = (lead.get("website") or "").strip()
    if not site:
        return None, "ausente"
    dom = re.sub(r"^https?://", "", site).split("/")[0].replace("www.", "")
    return f"contato@{dom}", "derivado"


def telefone_lead(lead):
    tel = (lead.get("telefone") or "").strip()
    return re.sub(r"[^\d+]", "", tel) or None


# ── Mensagens por canal ───────────────────────────────────────────────────────

def abordagem_ig(lead, diag) -> str:
    """DM curto e pessoal (não é o diagnóstico inteiro — só o gancho)."""
    problemas = diag["problemas"][:2]
    if problemas:
        pts = " e ".join(p["titulo"].lower() for p in problemas)
        gancho = f"vi 2 pontos que podem estar te custando pacientes ({pts})"
    else:
        gancho = "vi alguns pontos na presença digital que dá pra melhorar rápido"
    return (
        f"Oi! Passando aqui rapidinho 👋 Dei uma olhada na presença digital da "
        f"{lead.get('nome')} no Google e {gancho}. "
        f"Fiz um mini-diagnóstico com números, quanto isso representa em R$/mês. "
        f"Quer que eu te mande sem compromisso?"
    )


def corpo_email(lead, diag, prop) -> str:
    """Corpo único por lead: diagnóstico completo + proposta resumida."""
    partes = [diag["texto"], "", "* * *", "", prop["texto"], config.ASSINATURA_EMAIL]
    return "\n".join(partes)


# ── Cadência (touches) ────────────────────────────────────────────────────────

def agendar_cadencia(lead_id: int, base: date | None = None):
    base = base or date.today()
    existentes = {(t["canal"], t["etapa"]) for t in db.touches_for(lead_id)}
    for offset, canal, etapa in config.CADENCIA:
        if (canal, etapa) in existentes:
            continue
        quando = (base + timedelta(days=offset)).isoformat()
        db.schedule_touch(lead_id, canal, etapa, quando)


# ── Instagram: chama a cópia do bot ───────────────────────────────────────────

def enviar_lote_ig(alvos: list) -> dict:
    if not alvos:
        return {"status": "vazio", "total": 0}
    if requests is None:
        return {"status": "erro", "mensagem": "biblioteca 'requests' não instalada"}
    url = f"{config.DM_BOT_URL}/api/enviar_lote"
    try:
        r = requests.post(url, json={"nicho": "clinicas", "alvos": alvos}, timeout=10)
        return {"status": "ok" if r.ok else "erro", "http": r.status_code,
                "resposta": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text}
    except Exception as e:
        return {"status": "erro", "mensagem": str(e)}


# ── E-mail: encena prospects na cópia do mail bot ─────────────────────────────

def _load_mail_db():
    path = config.BASE_DIR / "senders" / "mail_bot" / "core" / "database.py"
    spec = importlib.util.spec_from_file_location("mail_core_database", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def encenar_email(itens: list) -> dict:
    """
    itens: [{email, empresa, dominio, corpo}]. Cria lista + template wrapper {{corpo}} e
    insere os prospects (status pending) prontos para o mail bot enviar.
    """
    if not itens:
        return {"status": "vazio", "total": 0}
    mdb = _load_mail_db()
    mdb.init_db()
    conn = mdb.get_conn()
    try:
        nome_lista = f"Prospecção — {date.today().isoformat()}"
        list_id = conn.execute("INSERT INTO lists(name) VALUES(?)", (nome_lista,)).lastrowid
        conn.execute(
            "INSERT INTO templates(name,subject,body) VALUES(?,?,?)",
            (f"Diagnóstico LeadForge — {date.today().isoformat()}",
             "{{empresa}}: 3 pontos que podem estar custando pacientes",
             "{{corpo}}"),
        )
        for it in itens:
            conn.execute(
                "INSERT INTO prospects(list_id,email,company,domain,corpo,status) "
                "VALUES(?,?,?,?,?, 'pending')",
                (list_id, it["email"], it["empresa"], it["dominio"], it["corpo"]),
            )
        conn.commit()
        return {"status": "ok", "total": len(itens), "lista": nome_lista,
                "db": str(mdb.DB_PATH)}
    finally:
        conn.close()


# ── Orquestração ──────────────────────────────────────────────────────────────

def _dados_lead(lead):
    """Garante diagnóstico e proposta do lead (gera se faltar) e devolve dicts ricos."""
    diag = {
        "problemas": diag_mod.detectar_problemas(lead)[:3],
        "texto": (db.get_diagnostico(lead["id"]) or {}).get("texto"),
    }
    if not diag["texto"]:
        d = diag_mod.gerar_diagnostico(lead)
        diag["texto"] = d["texto"]
    prop = db.get_proposta(lead["id"]) or prop_mod.gerar_proposta(lead)
    return diag, {"texto": prop["texto"], "nivel": prop["nivel"],
                  "nivel_nome": prop.get("nivel_nome", ""), "preco": prop["preco"]}


# Qual toque cada canal usa quando a ponte roda sem `--etapa`: SÓ os toques do
# DIA 0 da cadência.
ETAPA_PADRAO = {canal: etapa for dia, canal, etapa in config.CADENCIA if dia == 0}


def assunto_email(lead) -> str:
    return f"{lead.get('nome')}: 3 pontos que podem estar custando pacientes"


def ja_respondeu(lead_id: int) -> bool:
    """O lead já respondeu alguma coisa?"""
    try:
        import conversa_ia
        conversa_ia.init()
        lead = db.get_lead(lead_id) or {}
        # O vínculo real é pelo TELEFONE, não pelo lead_id: a conversa pode ter começado
        # antes de o lead existir, ou de um número que o cadastro ainda não tinha, e aí
        # o lead_id fica nulo.
        alvos = {re.sub(r"\D", "", (lead.get(c) or ""))[-8:]
                 for c in ("telefone", "whatsapp")}
        alvos = {a for a in alvos if len(a) == 8}

        with db.conn() as con:
            if con.execute("SELECT 1 FROM conversas WHERE autor='lead' AND lead_id=? "
                           "LIMIT 1", (lead_id,)).fetchone():
                return True
            if not alvos:
                return False
            for (numero,) in con.execute(
                    "SELECT DISTINCT numero FROM conversas WHERE autor='lead'"):
                if re.sub(r"\D", "", numero or "")[-8:] in alvos:
                    return True
        return False
    except Exception:
        return False


def _laudo(lead_id: int, recruzar: bool) -> dict:
    """
    Laudo de identidade do lead. O cruzamento é automático: se não houver laudo, ou se o
    salvo estiver vencido (config.IDENTIDADE_VALIDADE_DIAS), ele roda aqui mesmo, por
    lead. `recruzar` força refazer mesmo com laudo válido.
    """
    return identidade.garantir(lead_id, forcar=recruzar) or {}


def _selo(laudo: dict, canal: str) -> str:
    """Uma linha legível do estado do canal para o dry-run."""
    if not laudo.get("ok"):
        return "cruzamento falhou: " + str(laudo.get("erro", "motivo desconhecido"))
    cand = (laudo.get("escolhido") or {}).get(canal)
    if not cand:
        return "sem candidato"
    estado = "LIBERADO" if (laudo.get("liberado") or {}).get(canal) else "bloqueado"
    return f"{cand['rotulo']} — {cand['confianca']}/100, {cand['veredito']}, {estado}"


def run(enviar: bool = False, top_n: int = 30, verbose: bool = True,
        recruzar: bool = False, canais=("whatsapp", "instagram", "email"),
        lead_ids=None, etapas=None):
    """
    `lead_ids` restringe a rota a leads específicos. Sem ele, a ponte pega os melhores
    por score, o que em modo --enviar significa disparar para estabelecimentos reais.
    Sempre passe lead_ids quando estiver testando.
    """
    etapas = etapas or dict(ETAPA_PADRAO)
    db.init_db()
    identidade.init()
    if lead_ids:
        leads = [l for l in (db.get_lead(i) for i in lead_ids) if l]
    else:
        leads = db.leads_by_score(limit=top_n, status="qualified")
        if not leads:
            # talvez já tenham avançado de status; pega por score mesmo assim
            leads = db.leads_by_score(limit=top_n)
    if not leads:
        print("⚠ Nenhum lead. Rode: python seed_amostra.py && python scorer.py")
        return {}

    if verbose:
        modo = "ENVIO REAL" if enviar else "DRY-RUN (nada é enviado)"
        print(f"╔{'═'*68}╗")
        print(f"║ PONTE MULTICANAL — {modo:<47}║")
        print(f"╚{'═'*68}╝\n")

    fila = {"whatsapp": [], "instagram": [], "email": []}
    liberados = {"whatsapp": 0, "instagram": 0, "email": 0}
    bloqueados = {"whatsapp": 0, "instagram": 0, "email": 0}

    for lead in leads:
        diag, prop = _dados_lead(lead)
        agendar_cadencia(lead["id"])
        # quem já respondeu sai da régua e fica com o atendente ou com você
        if ja_respondeu(lead["id"]):
            if verbose:
                print(f"── {lead['nome']}  JÁ RESPONDEU, fora da cadência "
                      f"(assumido pelo atendente ou por você)\n")
            for canal in canais:
                bloqueados[canal] += 1
            continue

        # Estabelecimento que já aparece entre as primeiras não tem perda por posição, e
        # os toques dos dias 2, 4 e 7 são construídos EM CIMA desse número.
        if not diag_mod.perda_apurada(lead)[3]:
            fora = [c for c in canais
                    if etapas.get(c, "dia0") in ("dia2", "dia4", "dia7_followup")]
            if fora:
                if verbose:
                    print(f"── {lead['nome']}  posição {lead.get('posicao')}, sem perda "
                          f"apurada: fora dos toques de valor ({', '.join(fora)})\n")
                for canal in fora:
                    bloqueados[canal] += 1
                canais_lead = tuple(c for c in canais if c not in fora)
                if not canais_lead:
                    continue
            else:
                canais_lead = tuple(canais)
        else:
            canais_lead = tuple(canais)

        laudo = _laudo(lead["id"], recruzar)

        if verbose:
            print(f"── {lead['nome']}  (score {lead['score']}, {lead['nicho']}) "
                  f"→ Nível {prop['nivel']} {prop['nivel_nome']} R$ {diag_mod._br(prop['preco'])}")

        for canal, icone in (("whatsapp", "💬"), ("instagram", "📸"), ("email", "✉️ ")):
            if canal not in canais_lead:
                continue
            etapa = etapas.get(canal)
            if not etapa:
                # canal sem toque no dia 0 e sem --etapa: não há mensagem certa
                # a mandar, e escolher uma por conta própria é como este código
                # já mandou despedida no lugar de abertura
                if verbose:
                    print(f"   {icone} {canal:<10} sem etapa definida para hoje, "
                          f"use --etapa para incluir este canal")
                continue
            selo = _selo(laudo, canal)
            if verbose:
                # a etapa vai impressa: sem ela não dá para ver, olhando o
                # dry-run, que o WhatsApp está prestes a sair com o toque do
                # dia 2 (o do valor em R$) como PRIMEIRO contato
                print(f"   {icone} {canal:<10} [{etapa:<13}] {selo}")
            if (laudo.get("liberado") or {}).get(canal):
                liberados[canal] += 1
                # A copy vem de abordagem.py, que tem PAPEL por toque.
                peca = abordagem.gerar(lead["id"], canal, etapa)
                marca = {"experimento": peca.get("experimento", ""),
                         "variante": peca.get("variante", "")}
                if canal == "email":
                    fila[canal].append({"lead_id": lead["id"],
                                        "assunto": peca.get("assunto") or assunto_email(lead),
                                        "corpo": peca["texto"], **marca})
                else:
                    fila[canal].append({"lead_id": lead["id"], "mensagem": peca["texto"],
                                        **marca})
            else:
                bloqueados[canal] += 1
        if verbose:
            print()

    resumo = {"leads": len(leads), "liberados": liberados, "bloqueados": bloqueados}

    if verbose:
        print("Resumo por canal (liberado / bloqueado pela trava de identidade):")
        for canal in canais:
            print(f"  {canal:<10} {liberados[canal]:>3} liberado(s)  "
                  f"{bloqueados[canal]:>3} bloqueado(s)")
        print()

    if not enviar:
        if verbose:
            print("DRY-RUN: cadência agendada em `touches`. Nada foi enviado.")
            print("Para disparar de verdade: python bridge.py --enviar")
        return resumo

    # ── ENVIO REAL ────────────────────────────────────────────────────────────
    # A trava roda de novo dentro de cada sender: mesmo que algo passe daqui,
    # nada sai sem o veredito exigido pelo canal.
    resultados = {}

    if "whatsapp" in canais and fila["whatsapp"]:
        est = whatsapp_sender.status()
        if not est["pronto"]:
            print("→ WhatsApp: bot NÃO conectado. Pulei o canal.")
            resultados["whatsapp"] = {"ok": False, "erro": "bot não conectado"}
        else:
            print(f"→ WhatsApp: enviando {len(fila['whatsapp'])} mensagem(ns) "
                  f"pela conta {est['numero']}...")
            resultados["whatsapp"] = whatsapp_sender.enviar_lote(fila["whatsapp"], etapa=etapas.get("whatsapp", "dia0"))
            print(f"   {resultados['whatsapp']}")

    if "instagram" in canais and fila["instagram"]:
        est = instagram_sender.status()
        if not est["no_ar"]:
            print(f"→ Instagram: bot de DM fora do ar em {est['url']}. Pulei o canal.")
            resultados["instagram"] = {"ok": False, "erro": "bot de DM fora do ar"}
        else:
            print(f"→ Instagram: enviando {len(fila['instagram'])} DM(s)...")
            # espera a confirmação: a ponte é processo de lote e termina logo,
            # o que mataria a thread de confirmação e deixaria o toque preso
            # em `queued` mesmo com a DM entregue
            resultados["instagram"] = instagram_sender.enviar_lote(
                fila["instagram"], etapa=etapas.get("instagram", "dia0"),
                bloquear_ate_confirmar=True)
            print(f"   {resultados['instagram']}")

    if "email" in canais and fila["email"]:
        est = email_sender.status()
        if not est["pronto"]:
            print("→ E-mail: canal não configurado. Pulei.")
            for p in est["pendencias"]:
                print(f"   falta: {p}")
            resultados["email"] = {"ok": False, "erro": "canal não configurado"}
        else:
            print(f"→ E-mail: enviando {len(fila['email'])} mensagem(ns), "
                  f"rodízio de {len(est['contas'])} conta(s), "
                  f"{est['capacidade_hoje']} de capacidade hoje...")
            resultados["email"] = email_sender.enviar_lote(fila["email"], etapa=etapas.get("email", "dia4"))
            print(f"   {resultados['email']}")

    resumo["envio"] = resultados
    return resumo


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Ponte multicanal (Módulo D)")
    ap.add_argument("--enviar", action="store_true",
                    help="dispara de verdade (padrão: dry-run)")
    ap.add_argument("--top", type=int, default=30, help="quantos leads processar")
    ap.add_argument("--recruzar", action="store_true",
                    help="refaz o cruzamento de identidade mesmo com laudo válido")
    ap.add_argument("--canais", default="whatsapp,instagram,email",
                    help="canais a usar, separados por vírgula")
    ap.add_argument("--leads", default="",
                    help="ids específicos, separados por vírgula (obrigatório em teste)")
    ap.add_argument("--etapa", default="",
                    help="força a MESMA etapa em todos os canais (ex.: dia0). "
                         "Sem isso, cada canal usa o primeiro toque dele na "
                         "CADENCIA, que hoje é instagram=dia0, whatsapp=dia2 e "
                         "email=dia4, ou seja, o WhatsApp já sai com o valor em R$")
    args = ap.parse_args()
    ids = [int(i) for i in args.leads.split(",") if i.strip().isdigit()] or None
    canais = tuple(c.strip() for c in args.canais.split(",") if c.strip())
    etapas = None
    if args.etapa:
        if args.etapa not in abordagem.PAPEIS:
            ap.error(f"etapa desconhecida: {args.etapa}. "
                     f"Use uma de: {', '.join(abordagem.PAPEIS)}")
        etapas = {c: args.etapa for c in canais}
    run(enviar=args.enviar, top_n=args.top, recruzar=args.recruzar, lead_ids=ids,
        canais=canais, etapas=etapas)
