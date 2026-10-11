# -*- coding: utf-8 -*-
"""historico_ig.py — quem JÁ recebeu DM nossa no Instagram, venha de onde vier."""

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import config

logger = logging.getLogger(__name__)

# Onde cada versão do bot guardou o seu rastreio, a partir da raiz de uma pasta.
# Todos os padrões valem para todas as raízes; o que não existir não aparece.
_PADROES = (
    "senders/dm_bot/perfis/*/rastreio_dms.json",          # o bot atual
    "senders/dm_bot_novo/perfis/*/rastreio_dms.json",     # a cópia do antigo
    "senders/dm_bot_novo/dist/*/_internal/perfis/*/rastreio_dms.json",
    "data/ig_perfis/*/rastreio_dms.json",
    "perfis/*/rastreio_dms.json",                         # o dminstagram antigo
    "dist/*/_internal/perfis/*/rastreio_dms.json",        # ... e o executável dele
)

# De quanto em quanto tempo conferir se algum arquivo mudou.
REVER_S = 30

_TRAVA = threading.Lock()
_CACHE: Dict = {"quando": 0.0, "assinatura": None, "arquivos": {}, "indice": {}}
_ANOTADOS: Dict[str, Dict] = {}
_JA_AVISEI: set = set()

_RE_URL = re.compile(r"^(https?://)?(www\.)?(instagram\.com|ig\.me/m)/", re.I)


def normalizar(username: str) -> str:
    """`@Fulano`, `fulano/` e `https://instagram.com/fulano/?x=1` viram `fulano`."""
    u = str(username or "").strip()
    u = _RE_URL.sub("", u)
    u = u.split("?")[0].split("#")[0].strip().strip("/").lstrip("@")
    return u.lower()


def _raizes() -> List[Path]:
    base = Path(config.BASE_DIR)
    saida = [base]
    for r in getattr(config, "IG_HISTORICO_RAIZES", None) or []:
        try:
            r = Path(r)
            if r.is_dir() and r.resolve() != base.resolve():
                saida.append(r)
        except OSError:
            continue
    return saida


def _arquivos() -> List[Path]:
    vistos, saida = set(), []
    for raiz in _raizes():
        for padrao in _PADROES:
            try:
                achados = list(raiz.glob(padrao))
            except OSError:
                continue
            for arq in achados:
                chave = os.path.normcase(str(arq.resolve()))
                if chave not in vistos:
                    vistos.add(chave)
                    saida.append(arq)
    return saida


def _rotulo(arq: Path) -> str:
    """De onde veio, legível: `Prospecção/dminstagram/perfis/Gestores`."""
    pasta = arq.parent
    for topo in (Path(config.BASE_DIR).parent,):
        try:
            return pasta.relative_to(topo).as_posix()
        except ValueError:
            pass
    return pasta.as_posix()


def _guardar(indice: Dict, username: str, data: str, origem: str):
    u = normalizar(username)
    if not u:
        return
    atual = indice.get(u)
    # Fica a PRIMEIRA vez: é ela que responde "desde quando essa pessoa
    # conhece a nossa abordagem".
    if atual is None or (data and (not atual["data"] or data < atual["data"])):
        indice[u] = {"data": data or "", "origem": origem}


def _ler_rastreio(arq: Path) -> Dict[str, Dict]:
    saida: Dict[str, Dict] = {}
    try:
        registro = json.loads(arq.read_text(encoding="utf-8"))
    except Exception as e:
        # Um arquivo quebrado não pode derrubar o resto do histórico, e nem
        # pode sumir calado: sem ele, quem está lá volta a ser "desconhecido".
        chave = str(arq)
        if chave not in _JA_AVISEI:
            _JA_AVISEI.add(chave)
            logger.warning("historico_ig: nao consegui ler %s (%s); os envios "
                           "registrados nele nao estao sendo considerados", arq, e)
        return saida
    if not isinstance(registro, dict):
        return saida
    origem = _rotulo(arq)
    for user, dado in registro.items():
        data = dado.get("data", "") if isinstance(dado, dict) else ""
        _guardar(saida, user, str(data or ""), origem)
    return saida


def _ler_banco(indice: Dict):
    """O registro do teto também sabe para quem saiu DM — e não depende de pasta."""
    try:
        import db
        with db.conn() as con:
            linhas = con.execute(
                "SELECT destino, MIN(sent_at) AS quando FROM envios_conta "
                "WHERE canal='instagram' AND COALESCE(destino,'') != '' "
                "GROUP BY destino").fetchall()
    except Exception:
        # Sem banco ou sem a tabela (instalação nova): os arquivos respondem.
        return
    for destino, quando in linhas:
        _guardar(indice, destino, str(quando or ""), "registro de envios (banco)")


def _atualizar():
    """Relê o que mudou. Chamado com a trava tomada."""
    agora = time.monotonic()
    if _CACHE["assinatura"] is not None and agora - _CACHE["quando"] < REVER_S:
        return
    _CACHE["quando"] = agora

    arquivos = _arquivos()
    assinatura = []
    for arq in arquivos:
        try:
            st = arq.stat()
            assinatura.append((str(arq), st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    assinatura = tuple(sorted(assinatura))

    if assinatura != _CACHE["assinatura"]:
        # Só relê o arquivo que mudou: o do nicho em uso muda a cada envio, o
        # do dminstagram antigo não muda nunca.
        anteriores = _CACHE["arquivos"]
        novos = {}
        for caminho, mtime, tamanho in assinatura:
            velho = anteriores.get(caminho)
            if velho and velho["mtime"] == mtime and velho["tamanho"] == tamanho:
                novos[caminho] = velho
            else:
                novos[caminho] = {"mtime": mtime, "tamanho": tamanho,
                                  "dados": _ler_rastreio(Path(caminho))}
        _CACHE["arquivos"] = novos
        _CACHE["assinatura"] = assinatura

    indice: Dict[str, Dict] = {}
    for info in _CACHE["arquivos"].values():
        for u, v in info["dados"].items():
            _guardar(indice, u, v["data"], v["origem"])
    _ler_banco(indice)
    _CACHE["indice"] = indice


def ja_abordado(username: str) -> Optional[Dict]:
    """
    Esse @ já recebeu DM nossa? Devolve `{"data", "origem"}` da primeira vez, ou None.
    """
    u = normalizar(username)
    if not u:
        return None
    try:
        with _TRAVA:
            if u in _ANOTADOS:
                return dict(_ANOTADOS[u])
            _atualizar()
            achado = _CACHE["indice"].get(u)
            return dict(achado) if achado else None
    except Exception as e:
        logger.warning("historico_ig: falha ao consultar @%s (%s)", u, e)
        return None


def anotar(username: str, data: str = "", origem: str = ""):
    """O bot acabou de enviar: vale já, sem esperar a próxima releitura."""
    u = normalizar(username)
    if not u:
        return
    with _TRAVA:
        if u not in _ANOTADOS:
            _ANOTADOS[u] = {"data": data or time.strftime("%Y-%m-%d %H:%M:%S"),
                            "origem": origem or "este bot"}


def resumo() -> Dict:
    """Quantos @ e de quantos arquivos — para o log de quem liga o bot."""
    with _TRAVA:
        _CACHE["quando"] = 0.0
        _atualizar()
        return {"perfis": len(_CACHE["indice"]),
                "arquivos": len(_CACHE["arquivos"])}


def _esquecer():
    """Zera o que está em memória. Para os testes."""
    with _TRAVA:
        _CACHE.update({"quando": 0.0, "assinatura": None, "arquivos": {},
                       "indice": {}})
        _ANOTADOS.clear()
        _JA_AVISEI.clear()
