# page-capture

Abre um conteúdo web/e-learning (LMS/SCORM, com login e iframes) num browser, grava um screenshot de
cada página nova e clica automaticamente no controlo de avançar. Ferramenta pessoal, utilizador único.

## Instalação (Windows 11, PowerShell)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python -m playwright install chromium
```

Requisitos: Python ≥ 3.10. Dependências fixadas: playwright 1.65.2, pillow 13.0.0
(dev: pytest 9.2.0, ruff 0.17.6).

## Interface no browser (recomendado)

Duplo clique em `ui.bat`, ou:

```powershell
.\.venv\Scripts\python.exe capture_ui.py
```

Abre `http://127.0.0.1:8765/` no browser predefinido:

1. Preencha o URL (e o browser: Edge por omissão) e carregue em **1. Abrir browser**.
2. Faça login na janela que abriu e navegue até à primeira página a capturar.
3. Carregue em **2. Login feito — começar captura**. **Parar** termina em segurança.
4. O registo e a galeria das capturas atualizam-se sozinhos; capturas anteriores ficam na lista.

Opções do servidor: `--port`, `--out`, `--profile`, `--no-open`. A interface só escuta em
`127.0.0.1` e exige um token por sessão para qualquer ação (proteção contra outros sites abertos
no mesmo browser). Para sair: Ctrl+C na janela da consola.

## Utilização (linha de comandos)

```powershell
python capture.py https://lms.exemplo.pt/curso --max-pages 10 -v
```

1. O browser abre no URL. Faça login manualmente (as credenciais nunca são guardadas; a sessão fica
   no perfil local `.browser-profile/`).
2. Abra a primeira página a capturar e carregue ENTER na consola.
3. A ferramenta captura, clica em "avançar", espera que a página mude e estabilize, e repete.
4. Se não encontrar o controlo (ou 2 cliques seguidos não mudarem nada) entra em **modo assistido**:
   avance à mão no browser; a captura retoma o automático. Sem mudança durante `--manual-timeout`
   segundos, termina.
5. Ctrl+C termina em segurança; o que já foi gravado fica.

Resultado: `capturas/AAAA-MM-DD_HHMMSS/` com `0001.png`, `0002.png`, … e `index.csv`
(`n, file, timestamp_utc, url, trigger, control, changed_fraction, duplicate_of`).

### Opções

| Opção | Default | Descrição |
|---|---|---|
| `--out` | `capturas` | pasta base (uma subpasta nova por execução) |
| `--profile` | `.browser-profile` | perfil do browser (cookies de sessão) |
| `--channel chrome\|msedge` | — | usar Chrome/Edge instalado |
| `--viewport LxA` | `1366x768` | tamanho fixo da página; `janela` usa o tamanho real da janela (sem emulação) |
| `--next-selector CSS` | — | seletor do botão avançar (substitui a heurística) |
| `--max-pages` | `1000` | limite de páginas |
| `--change-threshold` | `0.003` | fração mínima de píxeis alterados |
| `--change-timeout` | `15` | segundos à espera de mudança após clique |
| `--manual-timeout` | `300` | segundos à espera em modo assistido |
| `--full-page` | — | screenshot da página inteira |
| `--headless` | — | sem janela (não permite login manual) |
| `--no-sandbox` | — | desliga a sandbox do browser (ligada por omissão; usar só se o browser não abrir) |
| `--no-prompt` | — | não esperar ENTER |
| `-v` | — | registo detalhado (candidatos e pontuações) |

Códigos de saída: `0` ok (inclui Ctrl+C), `1` erro de execução, `2` o browser não abre
(o argparse também usa `2` para argumentos inválidos).

### Navegação limitada (ex. Storyline)

Se o conteúdo mostrar um aviso como «A navegação é limitada» (o slide ainda não terminou), a
ferramenta fecha o aviso (OK), não o grava, espera 5 s e volta a tentar. Se o bloqueio durar mais do
que `--manual-timeout`, passa a modo assistido.

### Afinação

- Clique no controlo errado → ver o candidato no log `-v`; usar `--next-selector` ou ajustar termos
  em `pagecapture/scoring.py` (com teste em `tests/test_scoring.py`).
- Páginas falhadas (mudança pequena) → `--change-threshold 0.002`.
- Capturas a mais por vídeo/animação → subir `--change-threshold`.

## Testes

```powershell
ruff check .
pytest            # unit + E2E (servidor HTTP local, browser headless) + interface web
pytest -m "not e2e"
```

Variável opcional só para os testes: `PC_BROWSER_EXECUTABLE` (caminho de um Chromium quando não foi
instalado com `python -m playwright install chromium`).

## Segurança e âmbito

- Usar um utilizador de teste, não uma conta de formando real.
- Nunca versionar `.browser-profile/` (cookies) nem `capturas/` (conteúdo possivelmente proprietário).
- Atenção: a heurística não clica em "Enviar/Submit" de propósito, mas um layout atípico pode levar a
  um clique errado que fica registado no LMS.
- Fora de âmbito: responder a quizzes, gravar vídeo/áudio, contornar DRM/anti-bot, gerar PDF.
