@echo off
rem ============================================================================
rem  parar-bot.bat - derruba os tres servicos (5010, 5011, 5012).
rem
rem  Nao apaga nada e nao mexe no banco: so encerra os processos que estao
rem  ouvindo nessas portas. Depois de parar, o "INICIAR BOT.bat" sobe tudo de
rem  novo ja com o codigo atual.
rem ============================================================================

chcp 65001 >nul
title LeadForge - parando
cd /d "%~dp0"

echo.
echo  Parando dashboard, bot de WhatsApp e bot de DM...
echo.

python -X utf8 subir.py --parar

echo.
pause
