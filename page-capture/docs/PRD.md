# PRD — page-capture 0.1.0

## Objetivo
Ferramenta pessoal (utilizador único, Windows 11) para testar um conteúdo e-learning e guardar prova
visual de cada página: abre o conteúdo no browser, captura cada página nova e avança sozinha.

## Decisões
- Script local Python + Playwright (não extensão, não serviço).
- Login manual; credenciais nunca guardadas; sessão em perfil local persistente.
- Controlo de avançar: heurística + override `--next-selector` + modo assistido.

## Requisitos funcionais (critérios de aceitação)
1. Login manual: browser com janela; espera ENTER antes de capturar (`--no-prompt` salta).
2. Captura `NNNN.png` em `capturas/AAAA-MM-DD_HHMMSS/` (pasta nova por execução, nunca sobrescreve),
   só quando a página mudou acima do limiar e estabilizou. Páginas repetidas (quase iguais a
   qualquer página já gravada) nunca são gravadas; 5 repetidas seguidas terminam a captura.
3. Avanço automático em todos os frames (iframes SCORM incluídos); ignora Anterior/Voltar/Sair/Menu.
   `--next-selector` substitui a heurística.
4. Modo assistido: sem controlo válido ou após 2 cliques sem mudança, o utilizador avança à mão;
   captura quando mudar e retoma o automático. Sem mudança durante `--manual-timeout` → termina.
5. `index.csv` com flush por linha: n, file, timestamp_utc, url, trigger (start|click|manual|update),
   control, changed_fraction.
6. Ctrl+C termina limpo e mantém o gravado. Segue sempre a janela mais recente (popups do LMS).

## Arquitetura
- `pagecapture/scoring.py` — heurística pura (tokens, score, rank).
- `pagecapture/imagediff.py` — miniatura cinzenta 400 px; fração de píxeis com diferença > 32.
- `pagecapture/runner.py` — recolha JS por frame, clique top-3, deteção de mudança + estabilização,
  `Recorder` (PNG + CSV; rejeita páginas repetidas antes de gravar).
- `capture.py` — CLI, arranque do browser, códigos de saída 0/1/2.
- `pagecapture/webui.py` + `ui.html` + `capture_ui.py` — interface web local (127.0.0.1, stdlib
  http.server, token por sessão, verificação de Host); o Playwright corre numa thread dedicada.
- `pagecapture/validators.py` — validação partilhada CLI/interface.

## Parâmetros
Mudança ≥ 0.003 (polling 0,4 s); estabilização: diferença consecutiva < 0.001 (máx. 6 s);
repetida: < 0.001 face a qualquer página gravada (não é gravada); score mínimo 6.

## Riscos
- Clique errado num layout atípico (mitigação: `-v`, `--next-selector`, ensaio com `--max-pages`).
- Clicar "Enviar/Submit" fica registado no LMS.
- Margem do limiar: mudanças só de texto podem ficar perto de 0,3%.
- Seguir popups do LMS não está coberto por E2E.
- Proteções anti-automação podem bloquear o Playwright.

## Fora de âmbito
Responder a quizzes, vídeo/áudio, contornar DRM/anti-bot, gerar PDF.
