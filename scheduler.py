"""scheduler.py — Daemon de automacao do LeadForge."""

import logging
import os
import sys
import signal
from datetime import datetime
from pathlib import Path

try:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
except ImportError:
    print("ERRO: apscheduler nao instalado. Rode: pip install apscheduler")
    raise SystemExit(1)

import config

RAIZ = Path(__file__).resolve().parent
PID_FILE = RAIZ / "data" / "scheduler.pid"
LOG_DIR = RAIZ / "out" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

log = logging.getLogger("leadforge")


def _configurar_log():
    """Só quando este arquivo é o PROGRAMA, nunca ao ser importado."""
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / "scheduler.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)

# ── Constantes ────────────────────────────────────────────────────────────────
FOLLOWUP_MAX_PER_DAY = 20
HOUR_START = 8
HOUR_END = 18


# ── Helpers ───────────────────────────────────────────────────────────────────

def _business_hours() -> bool:
    now = datetime.now()
    return now.weekday() < 5 and HOUR_START <= now.hour < HOUR_END


def _write_pid():
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def _remove_pid():
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _pid_vivo(pid: int) -> bool:
    """O processo existe?"""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)          # em POSIX o sinal 0 é de fato só uma checagem
        return True
    except OSError:
        return False


def _is_running() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        return _pid_vivo(int(PID_FILE.read_text().strip()))
    except (OSError, ValueError):
        return False


# ── Jobs ──────────────────────────────────────────────────────────────────────

def job_follow_up():
    """Processa 1 follow-up vencido (e-mail, WhatsApp ou Instagram)."""
    if not _business_hours():
        return
    try:
        import follow_up
        r = follow_up.processar_proximo()
        acao = r.get("acao")
        if acao == "vazio":
            return
        if acao == "enviado":
            log.info(f"[follow-up] enviado para lead {r['lead_id']} ({r['canal']})")
        elif acao == "cancelado":
            log.info(f"[follow-up] lead {r['lead_id']} respondeu, regua cancelada")
        elif acao == "adiado":
            log.warning(f"[follow-up] lead {r.get('lead_id')} adiado: {r.get('erro')}")
        else:
            log.error(f"[follow-up] {acao} - lead {r.get('lead_id')}: {r.get('erro')}")
    except Exception as e:
        log.exception(f"[follow-up] erro: {e}")


def job_agendamentos():
    """Dispara os agendamentos de envio vencidos."""
    try:
        import agendamentos
        for r in agendamentos.rodar_pendentes():
            if r["ok"]:
                log.info(f"[agenda] {r['canal']}: {r['resposta']}")
            else:
                log.error(f"[agenda] {r['canal']} FALHOU: {r['resposta']}")
    except Exception as e:
        log.exception(f"[agenda] erro: {e}")


def job_reset_counters():
    """Reconcilia os contadores de envio."""
    try:
        import limites
        r = limites.sincronizar_email()
        if r.get("ok") and r.get("ajustadas"):
            log.info(f"[limites] {r['ajustadas']} conta(s) de e-mail ajustadas a janela de 24h")
        elif not r.get("ok"):
            log.warning(f"[limites] nao deu para sincronizar o e-mail: {r.get('erro')}")
    except Exception as e:
        log.error(f"[limites] erro na sincronizacao: {e}")

    try:
        import db
        with db.conn() as con:
            today = datetime.now().strftime("%Y-%m-%d")
            # Conta envios reais de hoje
            count = con.execute(
                "SELECT COUNT(*) FROM touches WHERE status='sent' AND date(sent_at)=?",
                (today,)
            ).fetchone()[0]
            # Atualiza app_config
            # colunas reais de app_config sao (key, value): com (chave, valor)
            # este job levantava OperationalError a cada 15 min, em silencio
            con.execute(
                "INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)",
                ("enviados_hoje", str(count))
            )
        log.info(f"[counters] enviados_hoje={count}")
    except Exception as e:
        log.error(f"[counters] erro: {e}")


def job_auto_start():
    """Auto-inicia o bot de WhatsApp se estiver configurado."""
    if not _business_hours():
        return
    try:
        from senders import whatsapp_sender
        st = whatsapp_sender.status()
        if not st.get("pronto"):
            log.info("[auto-start] WhatsApp desconectado, tentando reconectar...")
            import subir
            subir.conectar_whatsapp(verbose=False)
        else:
            log.debug("[auto-start] WhatsApp ok")
    except Exception as e:
        log.error(f"[auto-start] erro: {e}")


def job_email_replies():
    """Detecta replies de e-mail."""
    try:
        import reply_detector
        replies = reply_detector.detectar_replies_email()
        if replies:
            log.info(f"[email-replies] {len(replies)} reply(ies) detectado(s)")
    except Exception as e:
        log.error(f"[email-replies] erro: {e}")


def job_enrichment():
    """Enriquece leads com validacao MX e check de website."""
    try:
        import enrichment
        resultado = enrichment.enrich_leads(limite=100)
        if resultado.get("processados", 0) > 0:
            log.info(f"[enrichment] {resultado['processados']} leads enriquecidos")
    except Exception as e:
        log.error(f"[enrichment] erro: {e}")


def job_weekly_report():
    """Envia relatorio semanal de KPIs."""
    try:
        import weekly_report
        weekly_report.enviar_relatorio()
        log.info("[weekly-report] relatorio enviado")
    except Exception as e:
        log.error(f"[weekly-report] erro: {e}")


def job_daily_backup():
    """Backup diario do banco."""
    try:
        import backup_manager
        resultado = backup_manager.run_backup()
        log.info(f"[backup] slot {resultado.get('slot')}, {resultado.get('files_added', 0)} arquivos")
    except Exception as e:
        log.error(f"[backup] erro: {e}")


# ── Status ────────────────────────────────────────────────────────────────────

def status() -> dict:
    """Estado do scheduler para o dashboard."""
    rodando = _is_running()
    return {
        "rodando": rodando,
        "pid": int(PID_FILE.read_text().strip()) if rodando and PID_FILE.exists() else None,
        "jobs": [
            {"nome": "agendamentos", "intervalo": "1 min", "ativo": True},
            {"nome": "follow_up", "intervalo": "3 min", "ativo": _business_hours()},
            {"nome": "reset_counters", "intervalo": "15 min", "ativo": True},
            {"nome": "auto_start", "intervalo": "15 min", "ativo": _business_hours()},
            {"nome": "email_replies", "intervalo": "30 min", "ativo": True},
            {"nome": "enrichment", "intervalo": "diario 06:00", "ativo": True},
            {"nome": "weekly_report", "intervalo": "segunda 08:00", "ativo": True},
            {"nome": "daily_backup", "intervalo": "diario 02:00", "ativo": True},
        ],
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    _configurar_log()
    if "--status" in sys.argv:
        import json
        print(json.dumps(status(), indent=2, default=str))
        return

    if _is_running():
        print("Scheduler ja esta rodando. Use --status para ver o estado.")
        return

    _write_pid()

    def _shutdown(sig, frame):
        log.info("Scheduler encerrado.")
        _remove_pid()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    scheduler = BlockingScheduler(timezone="America/Sao_Paulo")

    # Follow-up: a cada 3 min, seg-sex 8-17h
    scheduler.add_job(
        job_follow_up,
        CronTrigger(
            day_of_week="mon-fri",
            hour=f"{HOUR_START}-{HOUR_END}",
            minute="*/3",
        ),
        id="follow_up",
        max_instances=1,
        misfire_grace_time=60,
    )

    # Agendamentos de envio: a cada minuto
    scheduler.add_job(
        job_agendamentos,
        IntervalTrigger(minutes=1),
        id="agendamentos",
        max_instances=1,
        misfire_grace_time=120,
    )

    # Reset de contadores: a cada 15 min
    scheduler.add_job(
        job_reset_counters,
        IntervalTrigger(minutes=15),
        id="reset_counters",
        max_instances=1,
    )

    # Auto-start de bots: a cada 15 min, seg-sex 8-17h
    scheduler.add_job(
        job_auto_start,
        CronTrigger(
            day_of_week="mon-fri",
            hour=f"{HOUR_START}-{HOUR_END}",
            minute="*/15",
        ),
        id="auto_start",
        max_instances=1,
    )

    # Deteccao de replies: a cada 30 min
    scheduler.add_job(
        job_email_replies,
        IntervalTrigger(minutes=30),
        id="email_replies",
        max_instances=1,
    )

    # Enrichment: diario as 06:00
    scheduler.add_job(
        job_enrichment,
        CronTrigger(hour=6, minute=0),
        id="enrichment",
        max_instances=1,
    )

    # Relatorio semanal: segunda as 08:00
    scheduler.add_job(
        job_weekly_report,
        CronTrigger(day_of_week="mon", hour=8, minute=0),
        id="weekly_report",
        max_instances=1,
    )

    # Backup diario: as 02:00
    scheduler.add_job(
        job_daily_backup,
        CronTrigger(hour=2, minute=0),
        id="daily_backup",
        max_instances=1,
    )

    log.info("LeadForge Scheduler iniciado com 7 jobs.")
    log.info(f"  PID: {os.getpid()}")
    log.info(f"  Follow-up: 3 min (seg-sex {HOUR_START}-{HOUR_END}h)")
    log.info(f"  Counters: 15 min")
    log.info(f"  Auto-start: 15 min (seg-sex {HOUR_START}-{HOUR_END}h)")
    log.info(f"  Email replies: 30 min")
    log.info(f"  Enrichment: diario 06:00")
    log.info(f"  Weekly report: segunda 08:00")
    log.info(f"  Backup: diario 02:00")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler encerrado.")
        _remove_pid()


if __name__ == "__main__":
    main()
