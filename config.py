"""config.py — Configuração central do LeadForge."""

import os
import re
import sys
from pathlib import Path

# ── Console UTF-8 (Windows) ───────────────────────────────────────────────────
# O terminal do Windows usa cp1252 e quebra ao imprimir emoji/alguns caracteres.
# Reconfigura stdout/stderr para UTF-8 (no-op se já for UTF-8 ou não suportado).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── Caminhos ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUT_DIR = BASE_DIR / "out"

# Banco próprio do projeto (NUNCA os bancos de produção).
def _carregar_env():
    """Lê o.env da raiz para os.environ (sem sobrescrever o que já veio do sistema)."""
    env = Path(__file__).resolve().parent / ".env"
    if not env.exists():
        return
    for linha in env.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if linha and not linha.startswith("#") and "=" in linha:
            k, v = linha.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_carregar_env()

LEADFORGE_DB = Path(os.environ.get("LEADFORGE_DB") or (DATA_DIR / "leadforge.db"))

# Banco de ORIGEM do scraper (produção) — lido SEMPRE em modo somente-leitura.
# Regra Zero: jamais abrir para escrita.
MAPS_DB_ORIGEM = Path(os.environ.get("MAPS_DB_ORIGEM") or Path.home() / "maps_scraper" / "maps_data.db")

# Banco do bot de e-mail (a CÓPIA cria o seu próprio; ver senders/mail_bot).
MAIL_BOT_DB = Path.home() / ".pcz" / "pcz.db"

CSV_SAIDA = OUT_DIR / "leads_qualificados.csv"

# ── Bots (cópias) ─────────────────────────────────────────────────────────────
# 127.0.0.1, não "localhost": o bot escuta só IPv4 (host 0.0.0.0) e, no Windows,
# "localhost" tenta::1 primeiro e espera ~2s pela recusa antes de cair no IPv4 —
# cada consulta ao bot custava 2s a mais.
DM_BOT_URL = "http://127.0.0.1:5022"          # cópia do bot de DM Instagram

# ── Sessões do Instagram: um perfil de navegador POR CONTA ───────────────────
# Fonte única do caminho, de propósito.
_LOCAL_APPDATA = Path(os.environ.get("LOCALAPPDATA") or Path.home())
IG_SESSOES_DIR = _LOCAL_APPDATA / "clinica_prospector" / "ig_sessions"

# Perfil único de antes da separação por conta. Continua sendo lido para quem
# ainda não logou conta nenhuma no formato novo.
IG_PERFIL_LEGADO = _LOCAL_APPDATA / "clinica_prospector" / "ig_session"

_RE_IG_CONTA = re.compile(r"[^a-z0-9._-]")


def ig_conta_normalizada(conta: str) -> str:
    """`@Minha.Conta ` → `minha.conta`. Vazio se não sobrar nada."""
    c = _RE_IG_CONTA.sub("", (conta or "").strip().lstrip("@").lower())
    # `..` sobrevive ao filtro de caracteres e viraria a pasta de CIMA de
    # IG_SESSOES_DIR. Nome só de pontos não é conta de Instagram nenhuma.
    return "" if not c.strip(".") else c


def ig_perfil(conta: str = "") -> Path:
    """Pasta do perfil de Chromium de UMA conta do Instagram."""
    c = ig_conta_normalizada(conta)
    return (IG_SESSOES_DIR / c) if c else IG_PERFIL_LEGADO


# ── Histórico de DM de OUTRAS pastas desta máquina ───────────────────────────
#
# Quem já recebeu DM por uma destas pastas não recebe a abordagem de novo (ver
# `historico_ig.py`). São as versões anteriores do bot de DM e a instalação de
# distribuição, que mandaram DM pelas MESMAS contas do Instagram — foi o
# histórico do `dminstagram` que o bot atual ignorava, e 85 perfis receberam a
# abordagem duas vezes. Só leitura: as pastas do `dminstagram` estão sob a
# REGRA ZERO. Pasta que não existir é ignorada.
IG_HISTORICO_RAIZES = [BASE_DIR.parent / nome for nome in (
    "Prospecção/dminstagram",
    "Prospecção v01/dminstagram",
    "LeadForge Distribuicao",
    "clinica_prospector",
)]

# ── Nichos ────────────────────────────────────────────────────────────────────
# Palavras-chave na categoria do Maps que mapeiam para um nicho interno.
NICHO_KEYWORDS = {
    "odontologia": ["dentista", "odonto", "dental", "ortodont", "implant"],
    "estetica":    ["estética", "estetica", "harmoniz", "botox", "skin", "beleza",
                    "dermato"],
    "fisioterapia": ["fisio", "pilates", "rpg", "quiro"],
    "veterinaria":  ["veterin", "pet", "animal"],
}
NICHO_PADRAO = "padrao"

# ── Módulo A: pesos de score (0–100) ──────────────────────────────────────────
# Cada componente contribui com no máximo o valor abaixo. A soma dos máximos = 100.
# "peso alto" ~25, "peso médio" ~15. Ajuste livremente (o scorer normaliza para 100).
PESOS_SCORE = {
    "posicao":                  25,   # ALTO  — quanto pior posicionada, maior a oportunidade
    "nota_baixa":               15,   # MÉDIO — nota do Google abaixo de 4.5
    "sem_site":                 12,   # MÉDIO — não tem website
    "sem_instagram":            12,   # MÉDIO — não tem Instagram
    "volume_avaliacoes":        16,   # MÉDIO — clínica ativa vale mais (curva com teto)
    "avaliacoes_sem_resposta":  20,   # ALTO  — reviews recentes sem resposta (quando disponível)
}

# Parâmetros da curva de qualificação (regra do "lead ideal").
NOTA_ALVO = 4.5            # acima disso, não há dor de reputação
NOTA_PISO = 3.0           # abaixo disso, a nota já satura a penalização
VOLUME_ATIVO_MIN = 10      # menos que isso = clínica "morta", pontua pouco em volume
VOLUME_ATIVO_TETO = 300    # volume onde a pontuação de "clínica ativa" satura
POSICAO_TETO = 20          # posição a partir da qual a oportunidade satura

# Clínica "perfeita" (sem falha nenhuma) ou "fantasma" (sem presença nenhuma)
# recebem uma penalização final para não subirem no ranking de leads.
PENALIZA_PERFEITA = 0.45   # multiplica o score se não houver NENHUMA falha explorável
PENALIZA_FANTASMA = 0.40   # multiplica o score se a clínica é inativa (volume ~0)

# ── Módulo B: ROI ─────────────────────────────────────────────────────────────
TICKET_POR_NICHO = {
    "odontologia": 800,
    "estetica":    500,
    "fisioterapia": 300,
    "veterinaria":  250,
    "padrao":       400,
}
# Estimativa antiga (fallback): faixa fixa de pacientes/mês perdidos.
PACIENTES_PERDIDOS_MIN = 3
PACIENTES_PERDIDOS_MAX = 12

# ── Estimativa por CTR + volume de busca (estimativa.py) ─────────────────────
# Ativa a conta defensável: buscas × diferença de CTR × conversão × ticket.
USAR_ESTIMATIVA_CTR = True

# Quanto da população procura o serviço por mês (fração). Ajuste com dados seus
# ou do Keyword Planner. Ex.: 0.008 = 8 de cada 1.000 habitantes por mês.
TAXA_BUSCA_MENSAL = {
    "odontologia": 0.0080,
    "estetica":    0.0045,
    "fisioterapia": 0.0030,
    "veterinaria":  0.0040,
    "padrao":       0.0040,
}
# De quem clica no perfil, quantos viram paciente.
TAXA_CONVERSAO_CLIQUE = 0.12
# Posição que a clínica poderia ocupar com o trabalho feito (meta realista).
POSICAO_ALVO = 3
# Perda de conversão quando falta o canal no perfil do Google.
PENALIDADE_SEM_SITE = 0.20
PENALIDADE_SEM_INSTAGRAM = 0.10
# Volume assumido quando a cidade não é identificada.
BUSCAS_MES_PADRAO = 1200

# ── Simulações do documento do cliente (estimativas, ajustáveis) ──────────────
# Recuperação = quanto dos pacientes perdidos a solução reconquista ao longo do
# tempo (curva com rampa). Teto por nível: quanto mais completo, mais recupera.
SIM_MESES = 12
RECUP_VELOCIDADE = 4.0            # menor = rampa mais rápida (usado em 1-exp(-m/vel))
RECUP_TETO_NIVEL = {1: 0.60, 2: 0.85, 3: 0.95}   # teto de recuperação por nível

# ── Módulo C: faixas de preço (âncora) ────────────────────────────────────────
FAIXAS_PRECO = {
    1: {"nome": "Presença",  "min": 397,  "max": 597,  "mais_vendido": False},
    2: {"nome": "Conversão", "min": 797,  "max": 1197, "mais_vendido": True},
    3: {"nome": "Domínio",   "min": 1497, "max": 1997, "mais_vendido": False},
}

# O QUE CADA NÍVEL ENTREGA. Fonte única: a proposta em texto, o PDF e a copy
# leem daqui.
ENTREGAS_POR_NIVEL = {
    1: {
        "site": "uma página de vendas",
        "itens": [
            "Perfil do Google Meu Negócio otimizado e mantido",
            "Uma página de vendas, com os serviços e o botão de WhatsApp",
            "Gestão das avaliações: pedido ao paciente e resposta a todas",
        ],
    },
    2: {
        "site": "página de vendas + blog",
        "itens": [
            "Tudo do plano Presença",
            "Blog no site, com publicação de conteúdo sobre os procedimentos",
        ],
    },
    3: {
        "site": "site institucional + blog",
        "itens": [
            "Tudo do plano Conversão",
            "Site institucional completo, no lugar da página de vendas",
            "Automação de WhatsApp e Instagram: resposta na hora, agendamento "
            "e lembrete anti-falta",
        ],
    },
}

# A automação existe SÓ no nível 3. Fica como constante porque é a regra que a
# copy e o material mais erram: é a parte mais desejada e a mais fácil de
# prometer no plano errado.
NIVEL_COM_AUTOMACAO = 3

# Serviços fora dos planos, cobrados à parte.
SERVICOS_EXTRAS = [
    {
        "nome": "Gestão de tráfego",
        "preco": 450,
        "desc": "Campanhas pagas no Google e no Meta, criadas e acompanhadas mês "
                "a mês. Contratada à parte, não está incluída em nenhum plano.",
    },
]

# ── Módulo D: cadência multicanal ─────────────────────────────────────────────
# (dia_offset, canal, etapa) O primeiro toque é o PING: uma pergunta de
# identificação e mais nada.
CADENCIA = [
    (0,  "whatsapp",  "dia0_ping"),
    (2,  "whatsapp",  "dia0"),          # a abordagem, para quem não respondeu
    (4,  "email",     "dia4"),
    (7,  "email",     "dia7_followup"),
    (12, "instagram", "dia12_ultima"),
]

# ── Esteira de WhatsApp (a régua automática) ─────────────────────────────────
# Quatro toques, nos dias 0, 2, 5 e 9.
ESTEIRA_WHATSAPP = [
    {"etapa": "wa1_abertura", "dia": 0, "rotulo": "1 · Abertura",
     "uso": "Observação específica sobre a clínica + uma pergunta que abre "
            "conversa. Sem link, sem oferta, sem preço. É o toque que compra o "
            "direito ao segundo, não o que vende."},
    {"etapa": "wa2_reforco", "dia": 2, "rotulo": "2 · Reforço",
     "uso": "Reforço de valor com um dado ou prova, ainda leve. Explica por que "
            "aparecer bem na busca decide quem é considerado. Não repete a "
            "observação do toque 1 como se fosse novidade."},
    {"etapa": "wa3_isca", "dia": 5, "rotulo": "3 · Isca de valor",
     "uso": "Oferece algo grátis e concreto (o diagnóstico pronto da clínica). "
            "É a mensagem que mais converte lead frio em conversa, porque "
            "entrega valor antes de pedir qualquer coisa. Deixa a saída aberta."},
    {"etapa": "wa4_encerramento", "dia": 9, "rotulo": "4 · Encerramento",
     "uso": "Encerramento educado (break-up). Tira a pressão e é um dos toques "
            "que mais converte. Depois deste, a esteira PARA: o lead esfria e "
            "só é revisitado em 60 a 90 dias."},
]
ESTEIRA_ETAPAS = [e["etapa"] for e in ESTEIRA_WHATSAPP]

# Etapas antigas que valem como o toque 1, para leads que já receberam o
# primeiro contato antes desta esteira existir.
ESTEIRA_ETAPAS_LEGADAS = {"dia0_ping": "wa1_abertura", "dia0": "wa1_abertura"}

# Proteção do número, medida em conta real: mais que isto num número não
# aquecido é o caminho curto para o bloqueio pelo Meta.
# (sem teto geral por dia: vale o limite diário de cada conta, aba Contas)
ESTEIRA_MIN_HORAS_ENTRE_TOQUES = 20   # nunca 2 mensagens no mesmo dia p/ o mesmo contato
ESTEIRA_JANELA = (8, 20)          # só envia em horário comercial (hora local)

# ── Atendente automático (resposta do bot quando o lead responde) ────────────
# DESLIGADO por decisão do operador: o bot manda SÓ os toques da esteira, que
# são texto de template revisado.
ATENDENTE_AUTOMATICO = False

# ── Ritmo do envio em lote (segundos) ────────────────────────────────────────
#
# Faixa sorteada entre um envio e o SEGUINTE quando o lote tem vários leads.
# Aleatório de propósito: intervalo fixo é um padrão, e padrão é o que os
# antispam procuram. Vale para os três canais e esta é a fonte única —
# o e-mail lê daqui, e o bot de WhatsApp (Node, que não lê config.py) recebe a
# faixa no payload de `/api/enviar_lote`. Antes o bot tinha a própria faixa
# fixa no código (25s a 60s) e mudar aqui não mudava o WhatsApp.
#
# O Instagram fica de fora: quem espaça é o bot de DM, dentro de
# `dminstagram/`, que é pasta somente leitura pela REGRA ZERO.
ENVIO_DELAY_MIN = 30
ENVIO_DELAY_MAX = 90

# ── Identidade da SUA empresa (usada pela IA no WhatsApp e nos textos) ───────
# Preencha com os dados reais: sem isso a IA escreve "[seu nome]" nas mensagens.
EMPRESA_NOME = "LeadForge"
ATENDENTE_NOME = "Wallisson"
EMPRESA_CIDADE = "Arapiraca, AL"

# ── WhatsApp ─────────────────────────────────────────────────────────────────
WHATSAPP_BOT_URL = "http://127.0.0.1:5021"

# ── Cruzamento de identidade (identidade.py) ─────────────────────────────────
# Roda sozinho dentro da esteira de cada lead.
IDENTIDADE_VALIDADE_DIAS = 30
# Quantos sites candidatos da busca visitar por lead. Cada um custa requisições.
IDENTIDADE_MAX_SITES = 4

# ── Assinatura padrão (e-mail) ────────────────────────────────────────────────
# Sem travessão longo: é regra do projeto e o sanitizador não alcança texto fixo.
ASSINATURA_EMAIL = (
    "\n\n...\nEquipe LeadForge\n"
    "Captação • Confiança • Conversão\n"
)

# ── Scheduler / Automação ─────────────────────────────────────────────────────
SCHEDULER_PORT = 5020           # porta do dashboard (para referência)
BACKUP_SLOTS = 30               # slots rotativos de backup
BACKUP_HOUR = 2                 # hora do backup diario
FOLLOWUP_DELAY_MIN = 3          # intervalo entre follow-ups (minutos)
FOLLOWUP_HOUR_START = 8         # inicio do horario comercial
FOLLOWUP_HOUR_END = 18          # fim do horario comercial
ENRICHMENT_DAILY_LIMIT = 100    # leads enriquecidos por dia
RELATORIO_EMAIL = ""            # e-mail para relatorio semanal (vazio = salva em out/)
