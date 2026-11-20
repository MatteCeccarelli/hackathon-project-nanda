---
name: hot-fix
description: Correzione rapida di un agente ASST direttamente su main (bug, penalità vista in Journeys, regola cambiata). Usala quando il team scrive /hot-fix o chiede di "sistemare al volo", "correggere subito" un agente già in gara.
---

# /hot-fix — correzione rapida su main

Per correzioni piccole e urgenti a un agente già in servizio. Per lavoro nuovo o più ampio usa `/feature`.

Prima di iniziare leggi le regole comuni in [../_shared/regole-team.md](../_shared/regole-team.md): valgono per ogni passo.

## Passi

1. **Vai su main.**
   ```bash
   git switch main
   ```
   Se ci sono modifiche non committate, **fermati**: mostra `git status` e chiedi al team cosa farne. Non scartarle e non fare stash senza conferma.

2. **Aggiorna main.**
   ```bash
   git pull --ff-only origin main
   ```
   Se il pull non è fast-forward o ci sono **conflitti**: fermati, spiega quali file sono in conflitto e cosa cambia da una parte e dall'altra, e **chiedi come risolverli**. Non scegliere tu.

3. **Leggi il contesto** `docs/MACH_contesto_team.md`, in particolare: 1B (pilastri e criteri), 5B.4 (rubrica), 6 (penalità), 9 (log LIVE), 12 (registro dei cambiamenti). Poi confronta il problema descritto con il contesto e **chiedi** al team se qualcosa non è chiaro o è incongruente (per esempio: la correzione richiesta va contro una regola della rubrica, o contraddice una scelta registrata in sezione 12). Se è tutto chiaro, dillo in una riga e prosegui.

4. **Risolvi il problema** con la modifica più piccola possibile, nel file `agent/agent.py` dell'agente coinvolto. Rispetta i vincoli in `_shared/regole-team.md` (atto e token invariati, niente clinico, Ledger prima del cittadino, LF). Poi verifica:
   - compilazione: `~/.local/share/uv/tools/city-cli/bin/python -I -m py_compile <agente>/agent/agent.py`
   - prova locale con `city run` sul caso che ha causato il problema (vedi regole comuni).
   - `city card validate <agente>`

5. **Commit su main** (solo i file toccati, mai `.DS_Store` o `__pycache__`), con un messaggio che dice problema e soluzione. **Non fare ancora push.**

6. **Riassunto e richiesta di deploy.** Scrivi al team:
   - **Problema**: cosa succedeva, su quale agente, quale riga di rubrica / penalità era in gioco.
   - **Causa**.
   - **Soluzione**: cosa hai cambiato e perché è la modifica minima.
   - **Test fatti** e cosa non è stato possibile testare in locale.
   - **Rischio** se si deploya e se non si deploya.

   Poi **chiedi esplicitamente se puoi fare push e deploy**. Fermati e aspetta la risposta.

7. **Solo dopo un sì esplicito:**
   ```bash
   git pull --ff-only origin main   # se nel frattempo è cambiato qualcosa: conflitti → fermati e chiedi
   git push origin main
   ```
   Poi deploy dell'agente come da regole comuni (sezione "Deploy"), verifica con `city status <agente>`, commit e push del bump di versione in `card.yaml`.

8. **Aggiorna il contesto**: aggiungi una riga alla **sezione 12** di `docs/MACH_contesto_team.md` (Tipo = `hot-fix`), e se serve una riga alla sezione 9 (log LIVE) o a "Regole e penalità scoperte". Linguaggio non tecnico, con il **perché**. Commit e push su main. Mostra la riga al team perché la rilegga.
