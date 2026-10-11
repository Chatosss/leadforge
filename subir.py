"""subir.py — Sobe os três serviços do projeto com um comando só."""

import argparse
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import psutil

RAIZ = Path(__file__).resolve().parent
LOGS = RAIZ / "out" / "logs"

SERVICOS = [
    {"nome": "dashboard", "porta": 5020,
     "cmd": [sys.executable, "dashboard/app.py"],
     "url": "http://localhost:5020"},
    {"nome": "whatsapp", "porta": 5021,
     "cmd": ["node", "senders/whatsapp_bot/server.js"],
     "url": "http://127.0.0.1:5021"},
    {"nome": "dm", "porta": 5022,
     "cmd": [sys.executable, "senders/dm_bot/Servidor_N8N.py"],
     "url": "http://127.0.0.1:5022"},
]

# fontes que, se mudarem, tornam obsoleto o que já está rodando
FONTES = ["*.py", "dashboard/*.py", "senders/*.py",
          "senders/whatsapp_bot/*.js", "senders/dm_bot/*.py"]


def _linha(c="─", n=72):
    return c * n


# ── Inspeção ──────────────────────────────────────────────────────────────────

def dono_da_porta(porta: int):
    """O processo que está ouvindo nesta porta, ou None."""
    for c in psutil.net_connections(kind="inet"):
        if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == porta:
            if c.pid:
                try:
                    return psutil.Process(c.pid)
                except psutil.NoSuchProcess:
                    return None
    return None


def porta_responde(porta: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", porta)) == 0


def fonte_mais_recente() -> float:
    """mtime do arquivo de código mais novo do projeto."""
    ultimo = 0.0
    for padrao in FONTES:
        for f in RAIZ.glob(padrao):
            try:
                ultimo = max(ultimo, f.stat().st_mtime)
            except OSError:
                pass
    return ultimo


def _hora(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d/%m %H:%M:%S")


# ── Ações ─────────────────────────────────────────────────────────────────────

def whatsapp_conectado() -> bool:
    """A conta está pareada agora? Consulta o bot, sem depender do sender."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen("http://localhost:5021/api/status", timeout=5) as r:
            return bool(json.loads(r.read()).get("pronto"))
    except Exception:
        return False


def parar_servico(s) -> bool:
    p = dono_da_porta(s["porta"])
    if p is None:
        return False
    filhos = []
    try:
        filhos = p.children(recursive=True)
    except psutil.Error:
        pass
    for proc in filhos + [p]:
        try:
            proc.terminate()
        except psutil.Error:
            pass
    _, vivos = psutil.wait_procs(filhos + [p], timeout=6)
    for proc in vivos:                       # quem não saiu por bem
        try:
            proc.kill()
        except psutil.Error:
            pass
    return True


# ── Ciclo de vida amarrado à janela (modo vinculado) ─────────────────────────

def _criar_job():
    """
    Um Job Object do Windows que MATA tudo que estiver dentro dele quando o último
    handle é fechado, ou seja, quando este processo morre.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.POINTER(wintypes.ULONG)),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64)]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    # Permite que um processo PEÇA para sair do Job (CREATE_BREAKAWAY_FROM_JOB).
    JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0800
    JobObjectExtendedLimitInformation = 9

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                                             | JOB_OBJECT_LIMIT_BREAKAWAY_OK)
    ok = k32.SetInformationJobObject(
        job, JobObjectExtendedLimitInformation,
        ctypes.byref(info), ctypes.sizeof(info))
    if not ok:
        k32.CloseHandle(job)
        return None
    return job


def _por_no_job(job, pid) -> bool:
    if not job or sys.platform != "win32":
        return False
    import ctypes
    PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
    if not h:
        return False
    try:
        return bool(k32.AssignProcessToJobObject(job, h))
    finally:
        k32.CloseHandle(h)


def subir_servico(s, verbose=True, vinculado=False) -> str:
    """'ja_no_ar' | 'subiu' | 'falhou'."""
    dono = dono_da_porta(s["porta"])
    if dono is not None:
        if verbose:
            print(f"  {s['nome']:<10} já no ar  PID {dono.pid} "
                  f"desde {_hora(dono.create_time())}")
        return "ja_no_ar"

    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"{s['nome']}.log"
    # Solto (padrão): o processo sobrevive ao fim deste script, e o log é o
    # único lugar onde um erro de boot fica visível depois que o console some.
    # Vinculado: o processo é filho deste, entra no Job e morre junto.
    flags = 0
    if sys.platform == "win32" and not vinculado:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    with open(log, "a", encoding="utf-8") as saida:
        saida.write(f"\n===== subir.py {datetime.now():%d/%m/%Y %H:%M:%S} =====\n")
        saida.flush()
        try:
            proc = subprocess.Popen(s["cmd"], cwd=str(RAIZ), stdout=saida,
                                    stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL,
                                    creationflags=flags, close_fds=True)
        except FileNotFoundError as e:
            if verbose:
                print(f"  {s['nome']:<10} FALHOU: {e}")
            return "falhou"
    if vinculado:
        _por_no_job(_JOB[0], proc.pid)
        _FILHOS.append(proc)

    for _ in range(60):                       # até ~30s para abrir a porta
        if porta_responde(s["porta"]):
            dono = dono_da_porta(s["porta"])
            if verbose:
                pid = dono.pid if dono else "?"
                print(f"  {s['nome']:<10} subiu     PID {pid}  {s['url']}")
            return "subiu"
        time.sleep(0.5)

    if verbose:
        print(f"  {s['nome']:<10} FALHOU: porta {s['porta']} não abriu. "
              f"Veja {log.relative_to(RAIZ)}")
    return "falhou"


ESPERA_WHATSAPP = 150      # segundos


def _status_bruto() -> dict:
    """O /api/status do bot Node, que diz mais que o sender: temQr, iniciado."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen("http://localhost:5021/api/status", timeout=8) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def conectar_whatsapp(verbose=True) -> dict:
    """
    O servidor Node no ar não significa WhatsApp conectado: o cliente só sobe depois de
    POST /api/iniciar. Esta é a etapa que se esquece de fazer na mão.
    """
    sys.path.insert(0, str(RAIZ))
    from senders import whatsapp_sender

    st = whatsapp_sender.status()
    if st.get("pronto"):
        if verbose:
            print(f"  whatsapp   já conectado: {st.get('numero')}")
        return st

    import urllib.request
    try:
        req = urllib.request.Request("http://localhost:5021/api/iniciar",
                                     data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        if verbose:
            print(f"  whatsapp   não deu para chamar /api/iniciar: {e}")
        return {"pronto": False, "erro": str(e)}

    avisou_qr = False
    for i in range(ESPERA_WHATSAPP):
        st = whatsapp_sender.status()
        if st.get("pronto"):
            if verbose:
                print(f"  whatsapp   conectado: {st.get('numero')}")
            return st
        bruto = _status_bruto() if i % 5 == 0 else {}
        if bruto.get("temQr") and not avisou_qr:
            avisou_qr = True
            if verbose:
                print("  whatsapp   A SESSÃO EXPIROU: escaneie o QR em "
                      "http://localhost:5021")
        if verbose and i and i % 30 == 0:
            print(f"  whatsapp   ainda sincronizando... ({i}s)")
        time.sleep(1)

    bruto = _status_bruto()
    if verbose:
        if bruto.get("temQr"):
            print("  whatsapp   esperando o QR ser escaneado: "
                  "http://localhost:5021")
        elif bruto.get("iniciado"):
            print("  whatsapp   autenticado, ainda sincronizando as conversas.")
            print("             confira daqui a pouco com: python operar.py")
        else:
            print("  whatsapp   servidor no ar, cliente não iniciou. "
                  "Veja out/logs/whatsapp.log")
    return st


# ── Relatório ─────────────────────────────────────────────────────────────────

def imprimir_estado():
    novo = fonte_mais_recente()
    print(_linha("═"))
    print("  SERVIÇOS")
    print(_linha("═"))
    print(f"{'serviço':<11}{'porta':<7}{'PID':<8}{'no ar desde':<17}código")
    obsoletos = []
    for s in SERVICOS:
        p = dono_da_porta(s["porta"])
        if p is None:
            print(f"{s['nome']:<11}{s['porta']:<7}{'-':<8}{'FORA':<17}-")
            continue
        inicio = p.create_time()
        velho = novo > inicio
        if velho:
            obsoletos.append(s["nome"])
        print(f"{s['nome']:<11}{s['porta']:<7}{p.pid:<8}{_hora(inicio):<17}"
              f"{'DESATUALIZADO' if velho else 'ok'}")
    print(_linha())
    if obsoletos:
        print(f"código mais novo que o processo em: {', '.join(obsoletos)}")
        print(f"o fonte mais recente é de {_hora(novo)}. Servidor de longa")
        print("duração não recarrega código: rode  python subir.py --reiniciar")
    return obsoletos


_JOB = [None]          # handle do Job Object, criado no modo vinculado
_FILHOS = []           # Popen dos serviços que este processo iniciou


def _encerrar_filhos(verbose=True):
    """Derruba o que ESTE processo subiu. O Job já cobre o caso violento."""
    if not _FILHOS:
        return
    if verbose:
        print("\nencerrando os serviços...")
    for proc in _FILHOS:
        try:
            p = psutil.Process(proc.pid)
        except psutil.Error:
            continue
        alvos = []
        try:
            alvos = p.children(recursive=True)
        except psutil.Error:
            pass
        for x in alvos + [p]:
            try:
                x.terminate()
            except psutil.Error:
                pass
        psutil.wait_procs(alvos + [p], timeout=5)
        for x in alvos + [p]:
            try:
                if x.is_running():
                    x.kill()
            except psutil.Error:
                pass
    if verbose:
        print("serviços encerrados.")


_HANDLER_CONSOLE = [None]      # precisa ficar vivo: o Windows guarda o ponteiro


def _armar_handler_console(ao_fechar):
    """Pega o fechamento da JANELA (o X), que sinal de Python não vê."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    CTRL_C, CTRL_BREAK, CTRL_CLOSE, CTRL_LOGOFF, CTRL_SHUTDOWN = 0, 1, 2, 5, 6
    TIPO = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def _cb(evento):
        if evento in (CTRL_C, CTRL_BREAK, CTRL_CLOSE, CTRL_LOGOFF, CTRL_SHUTDOWN):
            ao_fechar()
            return True
        return False

    _HANDLER_CONSOLE[0] = TIPO(_cb)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return bool(k32.SetConsoleCtrlHandler(_HANDLER_CONSOLE[0], True))


def _vigiar_pai(pai_pid, ao_morrer):
    """Encerra quando o processo PAI morre."""
    import threading

    def _laco():
        while True:
            time.sleep(2)
            if not psutil.pid_exists(pai_pid):
                ao_morrer()
                return
    t = threading.Thread(target=_laco, daemon=True)
    t.start()
    return t


def rodar_vinculado(alvos, sem_conectar=False) -> int:
    """Sobe tudo como FILHO desta janela e fica segurando o processo."""
    import atexit
    import os
    import signal

    _JOB[0] = _criar_job()
    atexit.register(_encerrar_filhos, False)

    def _sinal(_sig, _frm):
        raise KeyboardInterrupt

    for nome in ("SIGINT", "SIGTERM", "SIGBREAK"):
        s = getattr(signal, nome, None)
        if s is not None:
            try:
                signal.signal(s, _sinal)
            except (ValueError, OSError):
                pass

    # Três gatilhos, porque cada um cobre uma forma diferente de a janela sumir:
    # sinal -> Ctrl+C e Ctrl+Break handler console -> o X da janela, logoff,
    # desligar o computador vigia do pai -> o cmd.exe morrer sozinho, deixando o
    # python de pé O terceiro veio de um teste em que matar só o cmd deixou os três
    # serviços rodando: o Job estava ancorado no python, e o python não tinha por
    # que morrer.
    def _desligar():
        _encerrar_filhos(False)
        os._exit(0)

    _armar_handler_console(_desligar)
    try:
        _vigiar_pai(os.getppid(), _desligar)
    except Exception:
        pass

    if _JOB[0] is None and sys.platform == "win32":
        print("aviso: não foi possível criar o Job do Windows. Fechar a janela")
        print("       no X pode deixar processo para trás; prefira Ctrl+C.\n")

    print("subindo (modo vinculado: fecham junto com esta janela):")
    resultados = {}
    for s in alvos:
        resultados[s["nome"]] = subir_servico(s, vinculado=True)
    print()

    externos = [n for n, r in resultados.items() if r == "ja_no_ar"]
    if externos:
        print(f"aviso: {', '.join(externos)} já estava(m) no ar por fora e NÃO")
        print("       será(ão) encerrado(s) por esta janela. Para amarrar tudo:")
        print("       python subir.py --parar  e rode este comando de novo.\n")

    if any(s["nome"] == "whatsapp" for s in alvos) and not sem_conectar:
        if resultados.get("whatsapp") != "falhou":
            print("conectando o cliente de WhatsApp:")
            conectar_whatsapp()
            print()

    imprimir_estado()
    print()
    print("=" * 72)
    print("  Os serviços estão presos a esta janela.")
    print("  FECHAR esta janela (ou Ctrl+C) derruba os três.")
    print("=" * 72)
    print()
    try:
        while True:
            time.sleep(2)
            mortos = [s["nome"] for s, p in zip(alvos, _FILHOS) if p.poll() is not None]
            if mortos and len(mortos) == len(_FILHOS):
                print(f"todos os serviços caíram ({', '.join(mortos)}). Saindo.")
                return 1
    except KeyboardInterrupt:
        _encerrar_filhos()
        return 0


def _estado_9router(abrir: bool) -> None:
    """Uma linha sobre o 9router — e, com `abrir`, abre se estiver fechado."""
    try:
        import ia_roteador
    except Exception as e:
        print(f"9router: não deu para conferir ({e})")
        return
    if not ia_roteador.configurado():
        print("9router: sem IA_MSG_KEY no .env (personalização por IA desligada)")
        return
    if not abrir:
        print(f"9router: {'no ar' if ia_roteador.no_ar() else 'FECHADO'} "
              f"({ia_roteador.URL})")
        return
    g = ia_roteador.garantir()
    rotulo = {"ja_estava": "no ar", "iniciado": "estava fechado, ABRI",
              "recente": "mandei abrir há pouco e ainda não subiu",
              "falhou": "FECHADO e não consegui abrir",
              "remoto": "fora desta máquina e sem resposta"}.get(g["acao"], g["acao"])
    print(f"9router: {rotulo} ({g['detalhe']})")
    print()


def main():
    ap = argparse.ArgumentParser(description="Sobe os serviços do projeto")
    ap.add_argument("--parar", action="store_true")
    ap.add_argument("--reiniciar", action="store_true")
    ap.add_argument("--estado", action="store_true", help="só mostra, não mexe")
    ap.add_argument("--apenas", default="", help="dashboard,whatsapp,dm")
    ap.add_argument("--sem-conectar", dest="sem_conectar", action="store_true",
                    help="não chama /api/iniciar no bot de WhatsApp")
    ap.add_argument("--forcar", action="store_true",
                    help="reinicia o WhatsApp mesmo conectado (perde o pareamento)")
    ap.add_argument("--vinculado", action="store_true",
                    help="serviços presos a esta janela: fechar derruba tudo")
    a = ap.parse_args()

    escolhidos = [s.strip() for s in a.apenas.split(",") if s.strip()]
    alvos = [s for s in SERVICOS if not escolhidos or s["nome"] in escolhidos]
    if escolhidos and not alvos:
        print(f"nome desconhecido. Use: {', '.join(s['nome'] for s in SERVICOS)}")
        return 1

    if a.estado:
        imprimir_estado()
        _estado_9router(abrir=False)
        return 0

    # O 9router é a IA das mensagens personalizadas. Pedido do operador: toda vez
    # que o bot iniciar, conferir se ele está aberto e, se não, abrir. Antes de
    # subir os serviços, para o bot de DM já encontrá-lo no ar.
    if not a.parar:
        _estado_9router(abrir=True)

    if a.vinculado:
        if a.reiniciar:
            print("parando o que estava no ar antes de amarrar:")
            for s in alvos:
                print(f"  {s['nome']:<10} "
                      f"{'parado' if parar_servico(s) else 'já estava fora'}")
            print()
        return rodar_vinculado(alvos, a.sem_conectar)

    if a.parar or a.reiniciar:
        print("parando:")
        for s in alvos:
            # Reiniciar o bot de WhatsApp CONECTADO custa um pareamento novo: o whatsapp-
            # web.js grava a autenticação aos poucos, e derrubar o processo antes disso
            # terminar perde a sessão.
            if (s["nome"] == "whatsapp" and a.reiniciar and not a.forcar
                    and whatsapp_conectado()):
                print(f"  {s['nome']:<10} CONECTADO, não vou reiniciar "
                      f"(reiniciar custa parear de novo)")
                print(f"             use --forcar se for mesmo necessário")
                continue
            print(f"  {s['nome']:<10} {'parado' if parar_servico(s) else 'já estava fora'}")
        if a.parar:
            print()
            imprimir_estado()
            return 0
        print()

    print("subindo:")
    resultados = {s["nome"]: subir_servico(s) for s in alvos}
    print()

    if any(s["nome"] == "whatsapp" for s in alvos) and not a.sem_conectar:
        if resultados.get("whatsapp") != "falhou":
            print("conectando o cliente de WhatsApp:")
            conectar_whatsapp()
            print()

    imprimir_estado()
    print()
    print("estado da operação:  python operar.py")
    return 0 if "falhou" not in resultados.values() else 1


if __name__ == "__main__":
    sys.exit(main())
