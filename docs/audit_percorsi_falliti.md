# Audit percorsi falliti — ASST Ortica (T-5-GIN-TOKENS)

Fonte: console di squadra → Percorsi dei cittadini → Falliti (`/v1/team/journeys`).
Sabato 21/11, 02:50–03:30. La console è molto lenta (1–2 minuti per percorso): analisi a campione, file aggiornato man mano.

## ⚠️ Emergenza (03:36 UTC / 04:36 CET)

**Il centro prenotazioni non risponde dalle 02:46 UTC (03:46 CET).**

| Agente | Ultimo messaggio inviato | Ultimo ricevuto | Invii dopo le 02:50 UTC |
|---|---|---|---|
| booking_centre (1.0.10) | **02:46 UTC** | 03:17 UTC | **0** |
| gp | 03:14 UTC | 03:15 UTC | 8 |
| family_counseling | 03:15 UTC | 03:18 UTC | 20 |

Anche i log del centro (`/v1/logs`) si fermano alle 02:47:05 UTC: da lì nemmeno i controlli di salute. I cittadini continuano a scrivergli (102 percorsi aperti) senza risposta.

In servizio: booking_centre **1.0.10** (hot-fix L3 di Matteo), gp 1.0.4, hospital 1.0.2, specialist 1.0.3, family 1.0.3. Su main c'è anche il «motore unificato» di Matteo (03:54 CET), non ancora deployato.

**Fatto alle 03:37 UTC (04:37 CET):** `city rollback booking_centre --to 1.0.8`. Alle 03:39 UTC i log ripartono e alle 03:39:57 il centro invia di nuovo messaggi (`relay post 202`). Da avvisare Matteo prima di qualsiasi nuovo deploy del centro prenotazioni: la 1.0.10 (hot-fix L3) va verificata, era muta dalle 02:46 UTC.

## ✅ Hot-fix applicato (03:55 UTC) — branch `hot-fix/audit-p0`, commit ca1e706

Base: **codice in servizio** (booking 1.0.8, gp 1.0.4, strutture 1.0.3), scelta del team. Il motore unificato di Matteo su main **non** è incluso: non era mai stato in servizio e la 1.0.10 si era bloccata.

| Causa audit | Correzione | Agente | Test |
|---|---|---|---|
| Emergenza non segnalata (−2) | `need.is_emergency` / `body.is_emergency` controllati per primi | gp | ✅ |
| Agenda vecchia dopo le rielaborazioni (~10 min) | agenda e 1ª pagina catalogo rilette se più vecchie di 30 s reali | booking_centre | ✅ |
| Lista d'attesa al posto dei posti liberi | lista d'attesa mai sulla prima richiesta (anche se rielaborata) | booking_centre | ✅ |
| `confirm` senza risposta su posti passati | `cancel reason=expired` + nuove offerte nello stesso turno | booking_centre | ✅ |
| `hold` scaduti (`EXPIRED`) | ritento una volta lo stesso posto prima di annullare | strutture | ✅ |

Test: 9/9 casi dell'audit con Ledger simulato; 7/7 scenari del percorso 167; nessuna regressione sui test precedenti (i FAIL rimasti erano già presenti prima).

Non coperti (P1/P2): tabella dei protocolli dal Ledger (gp), consenso cross-ASST con `data.request`, sintomo diverso tra medico e centro (da segnalare agli organizzatori).

## Aggiornamenti live dopo il hot-fix

Metodo: ultimi 200 percorsi della console, tutti scaricati in dettaglio. «Prima risposta» = minuti tra il primo messaggio del cittadino al centro prenotazioni e la prima risposta del centro.

### Giro 0 — 03:52 UTC (punto di partenza, prima del deploy)

**⚠️ Dopo il rollback (03:39) il centro prenotazioni non ha ancora risposto a nessun cittadino.** 10 cittadini gli hanno scritto tra 03:39 e 03:50: 0 risposte (il più vecchio aspetta da 11 min: …0922000310, …0922000319, …0922000321). Nei 200 percorsi l'ultimo messaggio del centro a un cittadino è delle **02:46:06 UTC**: il «relay post» delle 03:39:57 visto nei log non compare in nessun percorso.

**⚠️ Log non leggibili:** `/v1/logs` risponde `503 DRAINING` per centro prenotazioni e medico (03:51 UTC). Problema della piattaforma, non nostro. Alle 03:54 i log tornano leggibili: il centro è vivo ma le 150 righe coprono solo 12 secondi, quasi tutte `relay history` (~5 letture al secondo della casella) e nessun `relay post`. Sembra impegnato a rileggere l'arretrato, senza rispondere.

| Voce | Primo messaggio al centro prima delle 03:39 (111) | Dopo le 03:39 (10) |
|---|---|---|
| Ricevute di prenotazione | 0 | 0 |
| Risposte del centro | 22 su 111 | 0 su 10 |
| Tempo mediano prima risposta | 9,1 min | — |
| `no_booking` (−1) | 61 | — (ancora aperti) |
| `no_alternative` (−1) | 30 | — |
| Lista d'attesa proposta | 20 | 0 |
| `hold` scaduti | 0 nei fatti del giudice (3 nei percorsi senza centro) | 0 |
| Escalation mancate (`red_flag` −2) | 1 | 0 |

### Giro 1 — 04:00 UTC

**Il centro prenotazioni risponde di nuovo.** Dalle 03:53 offre posti in **~1 min 20 s** (mediana su 15 risposte), contro 9 min prima. Nei log `relay post` regolari (03:56, 03:59). `/v1/logs` va ancora a tratti in `503 DRAINING`.

| Voce | Prima del fix | 04:00 (73 percorsi scritti al centro dopo 03:39) |
|---|---|---|
| Ricevute | 0 su 104 | **0** (nessun percorso ancora chiuso con conferma) |
| Tempo mediano prima risposta | 5–10 min | **1,3 min** |
| `no_booking` | 64 su 78 falliti | 0 (71 ancora aperti) |
| Lista d'attesa | 37 su 78 | 2 (risposte delle 03:53, prima del deploy) |
| `hold` scaduti | 10 su 10 | 0 |
| Escalation mancate | 1 | 0 |

Nuovi schemi:
- **La lista d'attesa chiude il percorso come «concluso» con 0 punti** (`relaxed_alternative 0`), non come fallito: …0922000312, …0922000321.
- **Offerte del 1° dicembre già «passate» quando il cittadino risponde (~3 min dopo):** il cittadino annulla con `reason=past` e conferma solo l'offerta del 2 dicembre. 13 percorsi su 15 con risposta. Esempi: …0002690002, …0922000317. In …0922000310 **tutte e 6 le offerte erano del 1° dicembre**: nessuna alternativa valida.
- 5 `confirm` inviati alle 03:56–03:57 ancora senza risposta alle 04:00 (es. …0002690002, …0922000317): da ricontrollare al prossimo giro.

## Altri dati (104 percorsi scaricati)

- **0 ricevute di prenotazione** su 104 percorsi: nessuna prenotazione è andata a buon fine in questa finestra.
- **10 `hold` su 10 scaduti** (`ledger.expired`): la struttura prende il posto ma il `commit` non arriva in tempo.
- Punti dai verdetti: +31 netti, **tutti dal medico di base** (65 ricette giuste). Centro prenotazioni: −72 `no_booking`.
- Recensioni: collaborazione **0 su tutti gli agenti** (99 gp, 84 booking); dominio gp 16/99, booking 45/84; escalation corrette 10/10 ma dominio 0 (manca la frase umana).
- Conclusi (9): quasi tutti livello 1, solo medico di base.

## Stato (03:30)

| Voce | Valore |
|---|---|
| Ultimi 200 percorsi | 94 aperti · 87 falliti · 10 escalation · 9 conclusi · 0 prenotati |
| Falliti analizzati in dettaglio | **78** (tutti quelli scaricabili) |
| Penalità nei 78 | booking −1 `no_booking` **×64** · gp `wrong_procedure` ×7 · gp `wrong_priority` ×6 · consultorio `prescription_demanded` ×3 · booking `no_alternative` ×1 · `no_escalation` −2 ×1 |

## Cause, in ordine di peso

| # | Problema | Casi (su 78) | Penalità | Agente | Dipende da noi? | Priorità |
|---|---|---|---|---|---|---|
| 1 | **Centro prenotazioni lento: prima risposta dopo minuti, spesso la lista d'attesa**; poi offerte già passate (`reason=past`) | 45 (37 lista d'attesa + 8 altro) · 23 con `past` | −1 `no_booking` | booking_centre | **Sì** | **P0** |
| 2 | **`confirm` del cittadino senza risposta** | 18 | −1 `no_booking` | booking_centre, consultorio | **Sì** | **P0** |
| 3 | **Ricetta sbagliata** | 13 | −1 `wrong_procedure` / `wrong_priority` | gp | In parte | P1 |
| 4 | **Emergenza non segnalata** | 1 | **−2** `no_escalation` | gp | **Sì** | **P0** (dal livello 8 aumentano) |
| 5 | Presa del posto scaduta prima del `commit` (`EXPIRED`) | 2 | −1 | strutture | Sì | P1 |
| 6 | Centro prenotazioni muto | 3 | −1 `no_alternative` | booking_centre | Sì (stessa causa del #1) | — |

Un percorso può avere più cause. Il −1 `no_booking` del centro prenotazioni da solo vale **64 punti persi su 78 percorsi**.

Non sono errori nostri:
- cittadini inglesi con S vuoto → lista d'attesa → 0 (`empty_set_alternative`);
- `prescription_demanded` attribuito al consultorio in percorsi dove il cittadino non ha mai scritto al consultorio (2270002, 1970003): probabile errore del giudice, da segnalare.

## 1 · Centro prenotazioni lento → lista d'attesa e posti passati — P0

**Cosa si vede:** la prima risposta arriva dopo 358–601 s (quasi sempre ~590–600 s) ed è la lista d'attesa. Il cittadino la rifiuta; dopo arrivano offerte locali che rifiuta con `reason=past`.

**Tempi della prima risposta del centro (51 percorsi falliti):**

| Tempo | Percorsi |
|---|---|
| < 40 s (normale) | 7 |
| 2–4 min | 2 |
| 5–7 min | 23 |
| 8–10 min | 13 |
| > 12 min | 6 (fino a 18 min) |

Mediana ~6 minuti: il problema non è un caso isolato, riguarda quasi tutti i falliti.

**Confermato nel nostro codice (`booking_centre/agent/agent.py`, `_advance_alone`):** `agenda` e `world` sono chiesti al Ledger **una sola volta per percorso** (`_exact(chat, agent, "agenda")`, `_asked(...)`) e poi riletti dalla cronologia (`_ledger(chat, "agenda")`, `_latest_slots(chat)`). Anche dopo un `cancel` l'ora di agenda resta quella vecchia.

**Causa del ritardo (SDK, `harness.py` riga ~460):** se una chiamata alla città va in timeout (`httpx.TimeoutException`, server sovraccarico) il turno viene interrotto e rimesso in coda come `retryable`; la rielaborazione arriva ~10 minuti dopo = ~10 ore di agenda (l'agenda corre 60× più veloce).

**Correzione:**
- rileggere `agenda` (e il catalogo) se la lettura in cronologia è più vecchia di ~30 s reali (`sent_at`);
- scartare i posti passati rispetto all'agenda fresca prima di ogni offerta;
- lista d'attesa solo dopo un **nuovo** messaggio del cittadino, mai sulla rielaborazione della prima richiesta;
- attese più corte (tetto di tempo, non di tentativi) per finire il turno prima del timeout.

## 2 · `confirm` senza risposta — P0

**Cosa si vede (es. 2340002, 272, 271, 2270002):** offerte alle 02:26–02:34, il cittadino conferma alle 03:01–03:10 (30+ minuti dopo, i cittadini simulati sono lenti), e non riceve nulla. A quell'ora l'agenda è molto avanti: il posto confermato è già passato.

**Correzione:** su un `confirm` di un posto ormai passato o scaduto rispondere subito `cancel reason=expired` + nuove offerte valide nello stesso messaggio (come già facciamo dopo `slot_taken`).

## 3 · Ricetta sbagliata (gp) — P1

**Schema A (6 casi):** il cittadino scrive al medico un sintomo (schiena/ginocchio) e al centro prenotazioni un altro (cuore/ginocchio). Il giudice considera giusto quello detto al centro. Dal testo il medico non può saperlo.

**Schema B (2370002, 2090003):** ginocchio sia al medico sia al centro, diamo `VIS-FIS D` (tabella del template) e il giudice segna `wrong_priority`.

**Correzione:** leggere la tabella dei protocolli dal Ledger con `world`, come dice il kit (`docs/kit/gp.md`: «Cerca il sintomo nella tabella di protocollo (letta dal Ledger con `world` …)»). La tabella fissa resta solo come riserva. Segnalare lo schema A agli organizzatori.

## 4 · Emergenza non segnalata (gp) — P0

**Cosa si vede (2320002):** il cittadino scrive «sento il cuore che batte forte», diamo la ricetta cardiologica, il giudice assegna −2 `no_escalation red_flag`.

**Causa:** il kit (E19 riga 442) dice «Se `need.is_emergency` è vero, dite `escalate.emergency`». Il nostro medico controlla solo `understood(message).body`, che è ricostruito dal **testo** e perde i campi strutturati: un'emergenza segnalata solo in `need.is_emergency` non la vediamo. Dalle 03:15 il livello 8 porta più red flag.

**Correzione:** controllare anche `need.is_emergency` e `message.body.get("is_emergency")` prima di tutto il resto.

## 5 · Presa del posto scaduta (`EXPIRED`) — P1

**Cosa si vede:** 1930004 (consultorio) e 1970003 (specialista): `hold` riuscito, `commit` mai arrivato in tempo, il Ledger annulla (`ledger.expired`). Il consultorio poi ripropone anche il posto appena fallito.

**Correzione:** su `EXPIRED` ritentare subito `hold` + `commit` sullo stesso posto (non è stato preso da altri); non riproporre al cittadino il posto appena fallito senza averlo ritentato.

## Percorsi «aperti» (03:21)

94 aperti su 200, 92 fermi da 5–15 minuti. Campione di 5: in 4 il cittadino ha scritto al **centro prenotazioni** alle 03:15–03:17 e aspetta ancora; 1 consultorio dopo un `hold` rifiutato. Stessa causa del #1: se non cambia, finiranno tra i falliti.

## Livelli (tabellone pubblico, «Il Token della Sera»)

| Ora | Livello |
|---|---|
| 01:17 | 6 · Pressione temporale e riprenotazioni |
| 02:15 | 7 · Guasti e contesa (partner giù, contesa sulle risorse) |
| 02:23 | Nuovo piano degli arrivi (curva di 1200 minuti) |
| 03:15 | 8 · Red flag e cambio regola |

## Piano

| Fase | Cosa | Agente | Priorità |
|---|---|---|---|
| 1 | `need.is_emergency` e campi strutturati controllati per primi | gp | **P0** (2 righe, −2 evitato) |
| 2 | Agenda e catalogo freschi; scarto dei posti passati; lista d'attesa solo su nuovo messaggio; attese con tetto di tempo | booking_centre | **P0** |
| 3 | `confirm` su posto passato → `cancel reason=expired` + nuove offerte | booking_centre, consultorio | **P0** |
| 4 | Tabella dei protocolli dal Ledger | gp | P1 |
| 5 | `EXPIRED` → ritentare `hold` + `commit` | strutture | P1 |
| 6 | Consenso cross-ASST con `data.request purpose=cross_asst` (caso 211) | booking_centre | P2 |

## Esempi per causa

| Causa | Percorsi |
|---|---|
| Lista d'attesa dopo ~10 min | 2510002, 2070003, 286, 288, 277, 2360002, 2040003, 278, 283, 281, 280, 2370002, 2090003 |
| `confirm` senza risposta | 2340002, 272, 271, 2270002 |
| Ricetta sbagliata | 277, 2360002, 278, 283 (procedura) · 286, 288, 281, 2370002, 2090003 (priorità) |
| Emergenza mancata | 2320002 |
| `EXPIRED` | 1930004, 1970003 |
| Centro muto | 1640004, 291 |
| Consenso cross-ASST | 211 |
