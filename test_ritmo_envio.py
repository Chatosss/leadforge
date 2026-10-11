"""test_ritmo_envio.py — O espaçamento entre um lead e o próximo existe de verdade."""
import sys
from datetime import datetime

import config
import db
import esteira_wa as ew
from senders import whatsapp_sender

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Leads que existem na base. Nada é enviado: `enviar_lote` vira espião.
def _cobaias(n=3):
    import db as _db
    with _db.conn() as con:
        return [r[0] for r in con.execute(
            "SELECT id FROM leads ORDER BY id LIMIT ?", (n,))]


LEADS = _cobaias(3)
ETAPAS = ["wa1_abertura", "wa3_isca", "wa4_encerramento"]

# 10h de uma quarta: dentro da janela (8h-20h), para o teste não depender da
# hora em que alguém o roda
AGORA = datetime(2026, 8, 12, 10, 0, 0)


def main():
    falhas = 0

    # ── 1 e 2) a esteira manda um lote só, com a etapa de cada lead ──────────
    chamadas = []

    def espiao_lote(itens, etapa="dia0"):
        chamadas.append({"itens": list(itens), "etapa": etapa})
        return {"ok": True, "enviados": len(itens), "bloqueados": []}

    fila_original = ew.fila
    montar_original = ew.montar_mensagem
    enviar_original = whatsapp_sender.enviar_lote
    # o que já saiu hoje não pode decidir este teste: com o teto do dia gasto,
    # `rodar` volta antes do laço e a medição vira zero por motivo errado
    hoje_original = ew.enviados_hoje
    ew.enviados_hoje = lambda: 0
    # idem as vagas das contas: com o limite de hoje gasto, `rodar` volta antes
    import limites
    vagas_original = limites.vagas_hoje
    limites.vagas_hoje = lambda canal: 999
    try:
        ew.fila = lambda agora=None: [
            {"id": lid, "nome": f"lead {lid}", "proxima": et}
            for lid, et in zip(LEADS, ETAPAS)]
        ew.montar_mensagem = lambda lead, etapa: f"mensagem de {etapa}"
        whatsapp_sender.enviar_lote = espiao_lote

        saida = ew.rodar(agora=AGORA, verbose=False)
    finally:
        ew.fila = fila_original
        ew.montar_mensagem = montar_original
        whatsapp_sender.enviar_lote = enviar_original
        ew.enviados_hoje = hoje_original
        limites.vagas_hoje = vagas_original

    ok = len(chamadas) == 1
    falhas += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FALHOU'} {len(LEADS)} leads vencidos -> "
          f"{len(chamadas)} lote(s) (esperado 1; um por lead mata o intervalo)")

    itens = chamadas[0]["itens"] if chamadas else []
    ok = len(itens) == len(LEADS)
    falhas += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FALHOU'} o lote levou {len(itens)} alvos "
          f"(esperado {len(LEADS)})")

    etapas_enviadas = [i.get("etapa") for i in itens]
    ok = etapas_enviadas == ETAPAS
    falhas += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FALHOU'} etapa por item: {etapas_enviadas} "
          f"(esperado {ETAPAS})")

    ok = len(saida.get("enviados") or []) == len(LEADS) and not saida.get("pulados")
    falhas += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FALHOU'} saída: {len(saida.get('enviados') or [])} "
          f"enviados, {len(saida.get('pulados') or [])} pulados")

    # ── 3) a faixa de intervalo vai no payload ──────────────────────────────
    payloads = []

    def espiao_post(rota, corpo):
        payloads.append({"rota": rota, "corpo": corpo})
        return {"ok": True}

    post_original = whatsapp_sender._post
    registrar_original = whatsapp_sender._registrar
    log_original = whatsapp_sender._log_experimento
    status_original = whatsapp_sender.status
    try:
        whatsapp_sender._post = espiao_post
        whatsapp_sender._registrar = lambda *a, **k: None
        whatsapp_sender._log_experimento = lambda *a, **k: None
        # bot dado como pronto e repetição liberada: aqui se mede o PAYLOAD, e as duas
        # travas (bot fora, mensagem repetida) barrariam antes dele `conta` entrou na
        # assinatura quando o status passou a distinguir "nenhuma conectada" de "varias
        # conectadas"
        whatsapp_sender.status = lambda conta="": {"no_ar": True, "pronto": True,
                                          "numero": "0", "erro": ""}
        # uma conta (fora do cadastro: não reserva vaga no banco) com vaga, para
        # o teste não depender do limite de hoje das contas reais
        remetentes_original = whatsapp_sender._remetentes_disponiveis
        whatsapp_sender._remetentes_disponiveis = lambda conta="": [
            {"conta": "5500000000000", "restam": 10, "id_no_bot": "5500000000000"}]
        whatsapp_sender.enviar_lote(
            [{"lead_id": 633, "mensagem": "oi", "etapa": "wa2_reforco"}],
            etapa="", permitir_repeticao=True)
    finally:
        whatsapp_sender._remetentes_disponiveis = remetentes_original
        whatsapp_sender._post = post_original
        whatsapp_sender._registrar = registrar_original
        whatsapp_sender._log_experimento = log_original
        whatsapp_sender.status = status_original

    corpo = payloads[0]["corpo"] if payloads else {}
    intervalo = corpo.get("intervalo") or {}
    ok = (intervalo.get("min") == config.ENVIO_DELAY_MIN
          and intervalo.get("max") == config.ENVIO_DELAY_MAX)
    falhas += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FALHOU'} payload leva intervalo "
          f"{intervalo or '(nenhum)'} (esperado "
          f"{{'min': {config.ENVIO_DELAY_MIN}, 'max': {config.ENVIO_DELAY_MAX}}})")

    alvo = (corpo.get("alvos") or [{}])[0]
    ok = alvo.get("etapa") == "wa2_reforco"
    falhas += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FALHOU'} etapa do item vence a do lote: "
          f"{alvo.get('etapa')!r} (esperado 'wa2_reforco')")

    # a faixa tem que ser uma faixa: intervalo fixo é padrão, e padrão é o que
    # o antispam procura
    ok = config.ENVIO_DELAY_MAX > config.ENVIO_DELAY_MIN >= 0
    falhas += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FALHOU'} config: {config.ENVIO_DELAY_MIN}s a "
          f"{config.ENVIO_DELAY_MAX}s (máximo tem que ser maior que o mínimo)")

    print()
    total = 7
    print(f"RESULTADO: {total - falhas}/{total} passaram")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
