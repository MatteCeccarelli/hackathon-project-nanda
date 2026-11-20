---
name: feature
description: Nuova funzionalità per uno o più agenti ASST su un branch dedicato, con revisione del team prima del merge in main; il deploy si fa a parte, su richiesta del team. Usala quando il team scrive /feature o chiede di "implementare", "aggiungere" un comportamento nuovo agli agenti.
---

# /feature — nuova funzionalità su branch

Per lavoro nuovo o non urgente. Per una correzione piccola e urgente usa `/hot-fix`.

Prima di iniziare leggi le regole comuni in [../_shared/regole-team.md](../_shared/regole-team.md): valgono per ogni passo.

## Passi

1. **Vai su main.**
   ```bash
   git switch main
   ```
   Se ci sono modifiche non committate, **fermati**: mostra `git status` e chiedi al team cosa farne.

2. **Aggiorna main.**
   ```bash
   git pull --ff-only origin main
   ```
   Se ci sono conflitti: fermati, spiegali e chiedi come risolverli.

3. **Crea il branch** con un nome breve e parlante:
   ```bash
   git switch -c feature/<agente>-<cosa>     # es. feature/booking-preferenze-lingua
   ```

4. **Leggi il contesto** `docs/MACH_contesto_team.md`, in particolare: 1B (pilastri e criteri), 5B.4 (rubrica), 6 (penalità), 7 (strategia e priorità), 12 (registro dei cambiamenti). Poi **chiedi** al team se qualcosa della feature non è chiaro o è incongruente con il contesto (per esempio: tocca un atto o un token letto dal giudice, rischia contenuti clinici, va contro una scelta in sezione 12, non porta punti secondo la rubrica). Se è tutto chiaro, dillo in una riga e prosegui.

5. **Implementa la feature** rispettando i vincoli in `_shared/regole-team.md`. Verifica:
   - compilazione: `~/.local/share/uv/tools/city-cli/bin/python -I -m py_compile <agente>/agent/agent.py`
   - prove locali con `city run` sui casi della feature e su almeno un caso "normale" che non deve cambiare (vedi regole comuni).
   - `city card validate <agente>`

   Committa sul branch (solo i file toccati) e fai push del branch:
   ```bash
   git push -u origin feature/<...>
   ```

6. **Fermati e riporta al team:**
   - cosa hai implementato, su quali agenti, con un esempio di messaggio prima/dopo;
   - quale riga della rubrica dovrebbe migliorare (e di quanto, come ipotesi);
   - test fatti e cosa non è testabile in locale;
   - rischi.

   Poi **chiedi se ci sono altre modifiche da fare o cose da implementare**. Se il team chiede modifiche, torna al passo 5. Non fare merge senza conferma esplicita.

7. **Dopo la conferma esplicita**, merge in main:
   ```bash
   git switch main
   git pull --ff-only origin main
   git merge --no-ff feature/<...>
   ```
   Se ci sono **conflitti**: risolvili tu solo se la soluzione è ovvia (per esempio `card.yaml` → tieni sempre `agent_id` e `version` più recenti, cioè quelli scritti dall'ultimo deploy). Altrimenti spiega il conflitto e chiedi. Dopo la risoluzione ricompila e ripeti `city card validate`.

   ```bash
   git push origin main
   git branch -d feature/<...>
   git push origin --delete feature/<...>
   ```

8. **Non fare il deploy.** Il team raccoglie più hot-fix e feature su main e chiede il deploy a parte, quando decide (vedi regole comuni, sezione "Deploy"). Nel riassunto finale indica quali agenti hanno modifiche su main non ancora deployate.

9. **Aggiorna il contesto**: aggiungi una riga alla **sezione 12** di `docs/MACH_contesto_team.md` (Tipo = `feature`, Versione deployata = `in main, da deployare`), e se serve aggiorna le sezioni 9 e 11 ("Note per il pitch"). Linguaggio non tecnico, con il **perché** e il pilastro (accessibilità, mobilità, ambiente) o criterio coperto. Commit e push su main. Mostra la riga al team perché la rilegga.
