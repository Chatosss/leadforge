"""weekly_report.py — Relatorio semanal de KPIs do LeadForge."""

from datetime import datetime, timedelta
from typing import Dict

import db
import config


def gerar_relatorio() -> Dict:
    """Gera dados do relatorio semanal."""
    hoje = datetime.now()
    semana_passada = hoje - timedelta(days=7)

    with db.conn() as con:
        # Leads adicionados esta semana
        leads_novos = con.execute(
            "SELECT COUNT(*) FROM leads WHERE created_at >= ?",
            (semana_passada.isoformat(),)
        ).fetchone()[0]

        # Envios esta semana
        envios = con.execute(
            "SELECT COUNT(*) FROM touches WHERE status='sent' AND sent_at >= ?",
            (semana_passada.isoformat(),)
        ).fetchone()[0]

        # Replies esta semana
        replies = con.execute(
            "SELECT COUNT(*) FROM touches WHERE status='sent' AND detalhe LIKE 'reply%' AND sent_at >= ?",
            (semana_passada.isoformat(),)
        ).fetchone()[0]

        # Estado do Kanban
        kanban = {}
        for row in con.execute(
            "SELECT kanban, COUNT(*) as n FROM leads GROUP BY kanban"
        ).fetchall():
            kanban[row["kanban"]] = row["n"]

        # Pipeline valor total
        pipeline_value = con.execute(
            "SELECT COALESCE(SUM(preco), 0) FROM propostas "
            "WHERE lead_id IN (SELECT id FROM leads WHERE kanban IN ('conversa','reuniao','fechado'))"
        ).fetchone()[0]

        # Taxa de reply
        taxa_reply = (replies / envios * 100) if envios > 0 else 0

    return {
        "periodo": f"{semana_passada.strftime('%d/%m')} - {hoje.strftime('%d/%m/%Y')}",
        "leads_novos": leads_novos,
        "envios": envios,
        "replies": replies,
        "taxa_reply": round(taxa_reply, 1),
        "kanban": kanban,
        "pipeline_value": pipeline_value,
    }


def gerar_html(relatorio: Dict = None) -> str:
    """Gera HTML do relatorio."""
    r = relatorio or gerar_relatorio()

    kanban_html = ""
    for coluna, count in sorted(r["kanban"].items()):
        kanban_html += f"<tr><td>{coluna}</td><td>{count}</td></tr>"

    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <h2 style="color: #1a1a2e;">LeadForge - Relatorio Semanal</h2>
        <p><strong>Periodo:</strong> {r['periodo']}</p>

        <h3>KPIs</h3>
        <table style="width: 100%; border-collapse: collapse;">
            <tr style="background: #f0f0f0;"><td style="padding: 8px;"><strong>Leads novos</strong></td><td style="padding: 8px;">{r['leads_novos']}</td></tr>
            <tr><td style="padding: 8px;"><strong>Envios</strong></td><td style="padding: 8px;">{r['envios']}</td></tr>
            <tr style="background: #f0f0f0;"><td style="padding: 8px;"><strong>Replies</strong></td><td style="padding: 8px;">{r['replies']}</td></tr>
            <tr><td style="padding: 8px;"><strong>Taxa de reply</strong></td><td style="padding: 8px;">{r['taxa_reply']}%</td></tr>
            <tr style="background: #f0f0f0;"><td style="padding: 8px;"><strong>Pipeline</strong></td><td style="padding: 8px;">R$ {r['pipeline_value']:,}</td></tr>
        </table>

        <h3>Estado do Kanban</h3>
        <table style="width: 100%; border-collapse: collapse;">
            <tr style="background: #f0f0f0;"><th style="padding: 8px; text-align: left;">Coluna</th><th style="padding: 8px;">Leads</th></tr>
            {kanban_html}
        </table>

        <p style="color: #666; font-size: 12px; margin-top: 30px;">
            Gerado automaticamente pelo LeadForge em {datetime.now().strftime('%d/%m/%Y %H:%M')}
        </p>
    </body>
    </html>
    """
    return html


def enviar_relatorio(destinatario: str = None):
    """Envia relatorio por e-mail."""
    from config import ASSINATURA_EMAIL
    r = gerar_relatorio()
    html = gerar_html(r)

    if not destinatario:
        # Usa o e-mail do .env ou o do operador
        destinatario = getattr(config, "RELATORIO_EMAIL", None)
        if not destinatario:
            print("[weekly-report] sem destinatario configurado")
            return

    # TODO: integrar com email_sender para enviar o HTML
    # Por enquanto, salva em out/
    out_path = config.OUT_DIR / f"relatorio-{datetime.now().strftime('%Y%m%d')}.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"[weekly-report] relatorio salvo em {out_path}")
