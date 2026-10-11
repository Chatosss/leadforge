"""analise_ia.py — A IA como ANALISTA do dossiê (não como redatora de slogan)."""

import json
import re
import sys

import coleta_profunda
import copy_ai
import db
import ia_hook

SISTEMA = """Você é um analista de presença digital para clínicas de saúde no Brasil.
Recebe um DOSSIÊ com dados coletados por robô e produz uma análise honesta.

REGRAS:
- Trabalhe SOMENTE com os dados do dossiê. Não invente número, cidade, serviço,
  horário, preço ou promoção. Campo ausente ou nulo significa "não sabemos".
- Cruze as fontes. Exemplo de raciocínio esperado: se a busca na web mostra um
  Instagram ativo mas o dossiê do Maps diz instagram_vinculado=false, a conclusão
  correta NÃO é "a clínica não tem Instagram", e sim "tem Instagram, mas ele não
  está conectado ao perfil do Google, então quem acha a clínica no Maps não chega
  até ele".
- RESULTADOS DA BUSCA: cada item em "resultados" tem "e_desta_clinica". Só use como
  fato sobre a clínica os itens com e_desta_clinica=true. Os demais são ruído da
  página de busca (outras empresas) e devem ser ignorados por completo.
- CIDADE: só cite uma cidade se o campo "cidade_uf_encontrada" existir no dossiê.
  Se ele não existir, escreva "na sua região" e NUNCA afirme ou deduza uma cidade,
  mesmo que apareça no título de algum resultado.
- NÃO prometa retorno, lucro, payback ou multiplicação de investimento. Fale do
  valor que a clínica DEIXA DE RECEBER e do fato de que ele soma enquanto nada muda.
  Reconheça que a correção leva tempo.
- Seja concreto e sóbrio. Sem hype, sem superlativo, sem emoji.
- PROIBIDO usar travessão longo (— ou –). Use ponto, vírgula ou dois-pontos.
- Escreva em português do Brasil, na segunda pessoa (você / sua clínica).

Responda SOMENTE com um JSON válido, sem cercas de código, neste formato exato:
{"leitura_geral": "...", "pontos_fortes": ["..."],
 "falhas": [{"titulo": "...", "explicacao": "...", "impacto": "..."}],
 "achados_da_busca": "...", "prioridade_1": "...", "abertura_relatorio": "..."}"""

_PEDIDO = """Analise o dossiê abaixo e produza o JSON.

Orientações de tamanho:
- leitura_geral: 2 a 3 frases sobre o que um paciente encontra hoje.
- pontos_fortes: 1 a 3 itens curtos (só o que os dados sustentam).
- falhas: exatamente 3 itens.
  "explicacao": até 30 palavras, o MECANISMO (o que o paciente vive na prática).
  "impacto": até 25 palavras, a CONSEQUÊNCIA CONCRETA para o negócio da clínica.
  Escreva o efeito real, não uma generalidade. Bom: "o paciente liga para a clínica
  que aparece antes e agenda lá; você nem fica sabendo que ele existiu". Ruim:
  "prejudica a captação de pacientes".
- achados_da_busca: 1 a 2 frases sobre o que a busca na web revelou e o Maps não mostra.
  Se a busca não trouxe nada, escreva "".
- prioridade_1: 1 frase dizendo o primeiro ajuste a fazer e por quê.
- abertura_relatorio: 2 frases para abrir o documento entregue ao dono da clínica.

DOSSIÊ:
"""


def _extrair_json(txt: str) -> dict:
    if not txt:
        return {}
    t = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return {}
    return {}


def _limpar(obj):
    """Aplica a limpeza de travessão recursivamente."""
    if isinstance(obj, str):
        return copy_ai.limpar_travessao(obj)
    if isinstance(obj, list):
        return [_limpar(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _limpar(v) for k, v in obj.items()}
    return obj


def analisar(lead_id: int, dossie: dict | None = None, coletar_agora: bool = True) -> dict:
    """Roda a análise da clínica. Retorna a análise estruturada (ou {} se falhar)."""
    if not ia_hook.IA_ATIVA:
        return {}
    if dossie is None:
        lead = db.get_lead(lead_id)
        bruto = (lead or {}).get("dossie_json")
        if bruto and not coletar_agora:
            try:
                dossie = json.loads(bruto)
            except Exception:
                dossie = None
        if dossie is None:
            dossie = coleta_profunda.coletar(lead_id)
    if not dossie.get("ok"):
        return {}

    payload = json.dumps(dossie, ensure_ascii=False, indent=1)[:7000]
    try:
        bruto = ia_hook._chamar_llm(
            [{"role": "system", "content": SISTEMA},
             {"role": "user", "content": _PEDIDO + payload}],
            temperatura=0.4, max_tokens=1400)
    except Exception as e:
        print(f"[analise_ia] falhou: {e}")
        return {}

    analise = _limpar(_extrair_json(bruto))
    if analise:
        try:
            with db.conn() as con:
                con.execute("UPDATE leads SET analise_json=? WHERE id=?",
                            (json.dumps(analise, ensure_ascii=False), lead_id))
        except Exception:
            pass
    return analise


def carregar(lead_id: int) -> dict:
    """Lê a última análise salva do lead."""
    lead = db.get_lead(lead_id)
    try:
        return json.loads((lead or {}).get("analise_json") or "{}")
    except Exception:
        return {}


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    lid = int(sys.argv[1]) if len(sys.argv) > 1 else 547
    print("Coletando dossiê (bot)...")
    d = coleta_profunda.coletar(lid)
    print(coleta_profunda.resumo_legivel(d))
    print("\nAnalisando com IA...\n")
    a = analisar(lid, d)
    print(json.dumps(a, ensure_ascii=False, indent=2))
