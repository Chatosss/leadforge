@echo off
rem ============================================================================
rem  iniciar-bot.bat - sobe o LeadForge inteiro com um clique.
rem
rem  MODO VINCULADO: os servicos ficam presos a ESTA janela. Fechar a janela
rem  (no X ou com Ctrl+C) derruba os tres. Quem garante isso e um Job Object do
rem  Windows criado pelo subir.py: o proprio sistema operacional mata a arvore,
rem  entao vale tambem se o processo for morto a forca.
rem
rem  Faz o que antes eram quatro passos em terminais diferentes:
rem    1. dashboard (5010), bot de WhatsApp (5011), bot de DM (5012) e scheduler
rem    2. o POST /api/iniciar, sem o qual o servidor Node sobe mas a conta de
rem       WhatsApp NAO conecta
rem    3. avisa se algum servico esta com codigo mais velho que os arquivos
rem    4. mostra o painel de operacao: modo de envio, canais e leads
rem
rem  Para deixar rodando em segundo plano (sobrevivendo ao fechar da janela),
rem  use:  python subir.py
rem ============================================================================

chcp 65001 >nul
title LeadForge - iniciando
cd /d "%~dp0"

echo.
echo  ================================================================
echo    LEADFORGE - MAQUINA DE PROSPECCAO
echo.
echo    Os servicos ficam presos a esta janela.
echo    FECHAR esta janela derruba o bot.
echo  ================================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo  [ERRO] Python nao encontrado no PATH.
    echo         Instale o Python ou abra o terminal onde ele funciona.
    echo.
    pause
    exit /b 1
)

rem  --reiniciar derruba o que estiver no ar por fora antes de amarrar, senao
rem  esses processos ficariam de pe depois que a janela fechasse
title LeadForge - no ar (nao feche para manter ligado)
python -X utf8 subir.py --vinculado --reiniciar

rem  Inicia o scheduler em background (follow-up, backups, enrichment)
echo.
echo  Iniciando scheduler...
start /B python -X utf8 scheduler.py

echo.
echo  ================================================================
echo    Bot encerrado. Pode fechar esta janela.
echo  ================================================================
echo.
pause
