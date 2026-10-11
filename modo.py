"""modo.py — Modo de operação e a lista branca de destinos."""

import os
import re
from typing import Dict, List

import config  # noqa: F401  (carrega o .env com os destinos de teste)

CHAVE = "modo_envio"
TESTE = "teste"
AO_VIVO = "ao_vivo"

# Contatos de teste (do.env). Em modo teste, só estes recebem.
def _lista_env(nome):
    return [x.strip() for x in os.environ.get(nome, "").split(",") if x.strip()]


DESTINOS_TESTE = {
    "whatsapp": _lista_env("MODO_TESTE_WHATSAPP"),
    "instagram": _lista_env("MODO_TESTE_INSTAGRAM"),
    "email": _lista_env("MODO_TESTE_EMAIL"),
}


def destinos_teste() -> Dict[str, List[str]]:
    """Os contatos que recebem em modo TESTE."""
    return DESTINOS_TESTE


def _norm(canal: str, valor: str) -> str:
    """Compara maçã com maçã: telefone por dígitos, @ e caixa fora do resto."""
    v = (valor or "").strip().lower()
    if canal == "whatsapp":
        # os 8 dígitos finais são o que identifica a pessoa, porque o WhatsApp
        # entrega sem o 9º dígito e nós enviamos com ele
        return re.sub(r"\D", "", v)[-8:]
    if canal == "instagram":
        return v.lstrip("@")
    return v


def atual() -> str:
    """O modo em vigor. Ambiente vence o banco; o padrão é sempre teste."""
    do_ambiente = (os.environ.get("MODO_ENVIO") or "").strip().lower()
    if do_ambiente in (TESTE, AO_VIVO):
        return do_ambiente
    try:
        import dashboard_db as ddb
        # o mesmo nome errado de `definir`, e aqui era pior: o AttributeError caía no
        # `except` abaixo e a função devolvia TESTE calada.
        ddb.init_dashboard()
        gravado = (ddb.get_config(CHAVE) or "").strip().lower()
        if gravado in (TESTE, AO_VIVO):
            return gravado
    except Exception:
        pass
    # Falha ao ler o modo cai em TESTE de propósito: o erro seguro é não enviar.
    return TESTE


def ao_vivo() -> bool:
    return atual() == AO_VIVO


def definir(novo: str) -> str:
    if novo not in (TESTE, AO_VIVO):
        raise ValueError(f"modo inválido: {novo}")
    import dashboard_db as ddb
    # `ddb.init()` não existe: a função chama-se `init_dashboard`.
    ddb.init_dashboard()
    ddb.set_config(CHAVE, novo)
    return novo


def permite(canal: str, valor: str) -> Dict:
    """Este destino pode receber agora? {"ok", "motivo"}."""
    _destinos = destinos_teste()
    if canal not in _destinos:
        return {"ok": False, "motivo": f"canal desconhecido: {canal}"}
    if ao_vivo():
        return {"ok": True, "motivo": "modo ao vivo"}

    alvo = _norm(canal, valor)
    liberados = [_norm(canal, v) for v in _destinos[canal]]
    if alvo and alvo in liberados:
        return {"ok": True, "motivo": "destino de teste"}
    return {"ok": False,
            "motivo": f"MODO TESTE: {canal} '{valor}' não está na lista de "
                      f"destinos de teste. Use 'python modo.py --ao-vivo' para "
                      f"liberar envio real."}


def resumo() -> str:
    m = atual()
    linhas = [f"modo de envio: {m.upper()}"]
    if m == TESTE:
        linhas.append("só estes destinos recebem:")
        for canal, valores in destinos_teste().items():
            linhas.append(f"  {canal:<10} {', '.join(valores)}")
    else:
        linhas.append("ATENÇÃO: envio liberado para destinos reais.")
    if os.environ.get("MODO_ENVIO"):
        linhas.append(f"(fixado pela variável de ambiente MODO_ENVIO)")
    return "\n".join(linhas)


if __name__ == "__main__":
    import sys
    args = [a.lower() for a in sys.argv[1:]]
    if "--teste" in args:
        definir(TESTE)
        print("modo alterado para TESTE.")
    elif "--ao-vivo" in args or "--ao_vivo" in args:
        print("Isto libera envio para CLÍNICAS REAIS. Não existe desfazer para")
        print("mensagem enviada.")
        if input("Digite AO VIVO para confirmar: ").strip().upper() == "AO VIVO":
            definir(AO_VIVO)
            print("modo alterado para AO VIVO.")
        else:
            print("cancelado, segue em teste.")
    print()
    print(resumo())
