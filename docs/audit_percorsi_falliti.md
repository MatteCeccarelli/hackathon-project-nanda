# Audit percorsi falliti — ASST Ortica (T-5-GIN-TOKENS)

Fonte: console di squadra → Percorsi dei cittadini → Falliti (`/v1/team/journeys`).
Sabato 21/11, 02:50–03:30. La console è molto lenta (1–2 minuti per percorso): analisi a campione, file aggiornato man mano.

## 🔎 Audit 2 — 04:30–04:45 UTC, 200 percorsi letti uno a uno (messaggi + verdetto)

Fonte: `/v1/team/journeys` + dettaglio di ogni percorso, log degli agenti, `city status`.
Stato alle 04:41 UTC: **95 aperti · 91 falliti · 9 urgenza · 5 conclusi · 0 prenotati**.

### Fatti nuovi (da sapere prima di tutto)

| # | Fatto | Prova |
|---|---|---|
| F1 | **In servizio ci sono le versioni precedenti**: booking **1.0.12**, gp **1.0.6**, family **1.0.6**, specialist **1.0.5**, hospital **1.0.4**. Le card in main dicono 1.0.13 / 1.0.7 / 1.0.7 / 1.0.6 / 1.0.5 → **i «turni corti» (0ccd713: attese 5 s, LLM 2 s, margine 8 h) NON sono in gara** | `city --json status <agente>` alle 04:38 |
| F2 | **Mercato fermo dalle ~04:32**: console «MERCATO Not started T+00:00:00», agenda ferma a «Gio 13:55», log degli agenti «DRAINING directory». Da allora nessun agente scrive ai cittadini | ultimo invio gp 04:32:02, fam 04:31:48, booking 04:32:22 |
| F3 | **Tutti i 95 falliti con verdetto hanno la stessa riga negativa: `citizen.booking_centre.wrong_outcome` reason `no_booking` (−1)**; il gp prende `protocol_match` +2 in 98 casi. Non ci sono altre penalità | `verdict.rows` |

### 1) Perché restano «aperti» (in coda)

| Tipo | Quanti | Causa | Priorità | Soluzione |
|---|---|---|---|---|
| A. Cittadino ha scritto al booking, **nessuna risposta** | 59 aperti (+36 già falliti) | Coda del booking: risposta p50 **113 s**, p90 **252 s**, max 642 s. Alle 04:26 arriva un'ondata di ~45 `need.request` insieme | **P0** | vedi B + deploy dei turni corti (F1) |
| B. **Il booking lavora su percorsi già chiusi** | nei log delle 04:37: **100% dei turni su percorsi falliti** (…0922000317, …0922000319, …0002530002, …0002620002, …0001640005) con `relay post` | Ogni turno aperto da una risposta del **Ledger** rientra in `_advance` → `_bc_fresh_clock` richiede `agenda` se la riga ha più di 30 s → sotto coda è sempre «vecchia» → nuova richiesta → nuova risposta → nuovo turno: **ciclo senza fine per ogni percorso toccato**, anche chiuso. In più le rielaborazioni `retryable` (timeout, +10 min). La capacità va ai percorsi morti, i nuovi aspettano | **P0** | 1) Turno aperto dal Ledger: **mai** chiamare `_bc_fresh_clock` né chiedere di nuovo `agenda`. 2) In `_desk`: se l'ultimo messaggio del cittadino ha più di 20 min reali → `return` (percorso chiuso o perso). 3) Valido per tutte le strutture (stesso codice) |
| C. Cittadino ha scritto al **gp**, nessuna risposta | 19 (1–13 min) | gp lento: p50 **224 s**, p90 248 s (ricetta + attesa Ledger + frase LLM) + mercato fermo (F2) | P1 | turni corti (F1 già in main): attesa Ledger 5 s, LLM 2 s |
| D. Cittadino ha scritto al **consultorio**, nessuna risposta | 11 (2–15 min) | family: p50 **569 s**, p90 **600 s** = schema «timeout → `retryable` → rielaborato dopo 10 min» | P0 | come B (stesso codice struttura) + turni corti |
| E. Burst 03:31 senza risposta | 33 (falliti) | arrivati mentre il booking 1.0.10 era muto (02:46–03:37, rollback) | — | nessuna (persi) |

Nota: dal `prescription` del gp alla `need.request` il cittadino aspetta **19–23 min reali** (= ~20 h di agenda). Non dipende da noi.

### 2) Perché le richieste accolte falliscono (95 `no_booking`)

| Causa | Quanti | Cosa succede | Priorità | Soluzione |
|---|---|---|---|---|
| **G1. Conferma valida, ma il booking risponde dopo 4–5 min → `cancel reason=expired`** | **45 conferme**: 26 chiuse con solo `cancel expired`, 19 con un'offerta nuova (4/12 10:30), poi fallite | Es. offerta 04:11, il cittadino conferma il 2/12 14:30 alle 04:16, il booking risponde alle **04:20–04:21**: nel frattempo l'agenda ha superato le 14:30 → `expired`. Tutti i casi: …02990002, …02840002, …01510007, …22000313 | **P0** | 1) Togliere la coda (sez. 1B). 2) Sul `confirm` **prima** `hold`/`commit` subito, **poi** il resto (niente rilettura del catalogo prima della presa). 3) Se `expired`: offrire **sempre** il primo posto libero entro la scadenza della priorità, non solo `cancel` |
| **G2. Offerte già «passate» quando il cittadino risponde** | **239 `cancel reason=past`** | Le offerte del booking 1.0.12 hanno margine 4 h; il cittadino risponde dopo ~5 min (= 5 h). Il 2/12 alle 04:16 tutti annullano 10:30 e 12:00 e confermano 14:30 | **P0** | margine 8 h (in main, **non deployato** F1); meglio margine = ritardo reale misurato × 60 + 3 h |
| **G3. Dopo i `cancel` del cittadino nessuna nuova offerta** | 13 percorsi (sequenza «offer×N → cancel×N» e poi niente) | Le alternative entro la scadenza sono finite o già offerte (`taken`), oppure il turno è perso nella coda | P1 | dopo l'ultimo `cancel` offrire il primo posto oltre la finestra con `empty_set_alternative` (già vale 0, non −1) oppure altra struttura |
| **G4. Preferenze ignorate** | 12 `cancel reason=gender`, 12 `reason=day` | Il comfort (giorni/genere) è in main ma non nella 1.0.12 | P2 | arriva con il deploy |
| **G5. Consultorio: `hold_refused` e conferme senza risposta** | 6 `hold_refused`, 3 conferme mai risposte | Stessa coda (p50 569 s); offerte duplicate (stesso `offer_id` due volte, …0002560003); offerte inviate prima del messaggio del cittadino | P1 | come 1B + idempotenza delle offerte |

### Piano (ordine)

1. **P0 — Rompere il ciclo Ledger** (booking + strutture): nessun `agenda` dai turni aperti dal Ledger; ignorare i percorsi con ultimo messaggio del cittadino > 20 min. Piccolo, locale, testabile con `city run`.
2. **P0 — Deploy di main** (turni corti + margine 8 h + comfort): oggi in gara c'è la versione precedente (F1). Verificare con `city --json status` dopo il deploy.
3. **P0 — `confirm` prima di tutto**: presa del posto subito; se scaduto, offerta sostitutiva sempre.
4. **P1 — Alternative dopo l'ultimo `cancel`** (G3).
5. Da chiarire con gli organizzatori: mercato fermo dalle 04:32 (F2) e orologio di agenda non lineare (salto notturno: alle 04:16 era ~2/12 13:00, alle 04:34 «Gio 3/12 13:55»).

### ✅ Hot-fix P0 fatto (commit a9da3da, in main locale, **non deployato**)

| Problema | Cosa cambia | Agenti |
|---|---|---|
| B. Ciclo Ledger | Il turno aperto da una risposta del Ledger si ferma se il cittadino tace da > 20 min, se ha già avuto risposta (salvo un nostro `cancel` senza offerte nuove) o se ha appena confermato. Niente più `agenda` dai turni del Ledger. Sostituzione dopo un `cancel` una sola volta per evento | booking, consultorio |
| G1. Confirm lento | Il `confirm` parte subito se il posto è chiaramente lontano (stima agenda ×60 + 1 h di margine); il giro di `agenda` resta solo per i posti vicini | booking, consultorio |

Test: compilazione ok, 5 casi della regola verificati (attesa prime offerte → lavora; già risposto / confermato / silenzio 25 min → si ferma; cancel senza offerte → lavora), card valide. `city run` locale uguale a prima (il Ledger locale non risponde ad `agenda`: flusso reale non testabile qui).
Restano: P0 deploy di main (F1), domande agli organizzatori (F2).

### ✅ Hot-fix P1/P2 fatto (in main, **non deployato**)

| Problema | Cosa cambia | Agenti |
|---|---|---|
| G3. Silenzio dopo l'ultimo `cancel` | Se non c'è più un posto locale adatto e nessuna altra ASST da interrogare, offerta di lista d'attesa una sola volta (`empty_set_alternative` 0 invece di `no_booking` −1) | booking |
| G5. Consultorio: offerte doppie | Coperto dal fix P0 (i turni del Ledger non rifanno offerte già inviate). `hold_refused` = posto preso da altri: resta | consultorio |
| G4. Preferenze genere/giorno (P2) | Nessun codice nuovo: il comfort è già in main, arriva con il deploy | booking |
| C. GP lento (P1) | Nessun codice nuovo: turni corti già in main, arrivano con il deploy | gp |

Test: compilazione, card valida, offerta di lista d'attesa provata con un finto `say` (una sola volta anche se richiamata). Non testabile in locale: ricerca esaurita con il Ledger reale.

### 🧪 Test locali prima del deploy (città finta con Ledger scriptato)

| Agente | Nuovo | Pre-fix (25bb418) | Note |
|---|---|---|---|
| booking | **11/11** | 6/11 | Pre-fix riproduce il ciclo: **60 richieste al Ledger in 30 turni** dopo le offerte; nuovo: 0. Confirm lontano senza giro di agenda; percorso fermo 25 min ignorato; ricerca esaurita → lista d'attesa |
| consultorio | 6/6 | 6/6 | Il ciclo non lo riguardava (non rilegge l'agenda); guadagno solo sui percorsi fermi |
| gp, ospedale, specialista | ok | — | `city run`: ricetta, emergenza, check-in senza errori; 5/5 card valide |

Non testabile in locale: coda e carico reali, più copie in parallelo, orologio dell'agenda della città.

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

### Giro 2 — 04:05 UTC

| Voce | Prima del fix | 04:05 (74 percorsi dopo 03:39) |
|---|---|---|
| Ricevute | 0 su 104 | **0** |
| Tempo mediano prima risposta | 5–10 min | 1,4 min (18 risposte) |
| Chiusi | — | 11: 3 conclusi (lista d'attesa, 0 punti) · 8 falliti |
| `no_booking` | 64 su 78 falliti | 2 (altri 6 falliti senza ancora il giudizio) |
| Lista d'attesa | 37 su 78 | 3 |
| `hold` scaduti | 10 su 10 | 0 (nessun `hold` arrivato al Ledger) |
| Escalation mancate | 1 | 0 |

Nuovi schemi:
- **⚠️ Il `confirm` riceve risposta dopo ~3,4 min, e la risposta è una nuova offerta, non la ricevuta.** I 5 `confirm` delle 03:57 (posti del 2 dicembre) hanno avuto alle 04:00–04:01 un'offerta del 3 dicembre: nel frattempo il posto confermato era passato. Nessun messaggio `cancel reason=expired` visibile prima della nuova offerta. Il cittadino rincorre posti che scadono: …0002690002, …0922000317, …0002910002.
- **⚠️ Dopo i `cancel reason=past` il centro non manda nuove offerte e il percorso fallisce** (`no_booking`): …0001640005 (4 offerte del 1° dicembre, tutte rifiutate alle 03:57, nessuna risposta), …0922000310 (6 rifiutate alle 03:59), …0002530002.
- Collega al «Problema aperto» qui sotto: la causa è la stessa (offerte troppo vicine a «adesso» rispetto ai tempi reali di risposta).

### Giro 3 — 04:10 UTC

**⚠️ Il centro prenotazioni non scrive ai cittadini dalle 04:01:00 UTC.** I log mostrano attività (`relay post`, molte `brain begin` con `deferred`), ma in nessuno dei 200 percorsi compare un suo messaggio dopo le 04:01. **47 richieste di prenotazione senza alcuna risposta da più di 10 minuti**: 45 arrivate a raffica alle 03:53 (…0002980002, …0002840002, …0001410007), più …0922000327 (03:56) e …0922000328 (03:58).

| Voce | Prima del fix | 04:10 (77 percorsi dopo 03:39) |
|---|---|---|
| Ricevute | 0 su 104 | **0** |
| Tempo mediano prima risposta | 5–10 min | 1,4 min, ma solo 18 su 77 hanno avuto risposta |
| Chiusi | — | 14: 3 conclusi (lista d'attesa, 0 punti) · 11 falliti |
| `no_booking` | 64 su 78 falliti | **9 su 11 falliti** (+1 `no_alternative`) |
| Lista d'attesa | 37 su 78 | 3 |
| `hold` scaduti | 10 su 10 | 0 (nessun `hold` arrivato al Ledger) |
| Escalation mancate | 1 | 0 |

- 9 falliti su 11 sono «offerte del 1° dicembre rifiutate come passate, poi silenzio»: …0922000318, …0922000319, …0002720002.
- I 5 cittadini che hanno ricevuto alle 04:00 la nuova offerta del 3 dicembre non hanno ancora risposto (…0002690002): il ciclo `confirm` → nuova offerta resta aperto.

### Giro 4 — 04:15 UTC

Correzione al giro 3: le 45 richieste «delle 03:53» ora risultano inviate alle ~04:09 (la console ha cambiato l'ora del messaggio, es. …0002980002: 03:53:32 → 04:09:33). Quindi non aspettavano da 16 minuti. Resta vero che il centro risponde a pochissimi.

**⚠️ Il centro prenotazioni è in coda.** Richieste ricevute e risposte, per fascia di 10 minuti:

| Fascia (UTC) | Richieste al centro | Con risposta | Tempo prima risposta |
|---|---|---|---|
| 03:40–03:50 | 4 | 2 | ~10,5 min |
| 03:50–04:00 | 16 | **16** | ~1,3 min |
| 04:00–04:10 | 55 | **3** | 1–8 min |
| 04:10–04:15 | 36 | **0** | — |

Messaggi del centro ai cittadini: 37 (03:50–04:00) → 18 (04:00–04:10) → 1 (dopo le 04:10, ultimo alle 04:10:28). `/v1/logs` del centro di nuovo `503 DRAINING` alle 04:15. Lo `city scale` delle 04:12 non si vede ancora nei percorsi.

| Voce | Prima del fix | 04:15 (111 percorsi dopo 03:39) |
|---|---|---|
| Ricevute | 0 su 104 | **0** |
| Tempo mediano prima risposta | 5–10 min | 1,4 min sulle 21 risposte; 90 richieste ancora senza risposta |
| Chiusi | — | 14 (invariato): 3 conclusi con lista d'attesa · 11 falliti |
| `no_booking` | 64 su 78 falliti | 9 su 11 |
| `hold` scaduti | 10 su 10 | 0 (nessun `confirm` arrivato a buon fine) |
| Escalation mancate | 1 | 0 |

- Il ciclo `confirm` → nuova offerta continua: i 5 cittadini dell'offerta del 3 dicembre (ricevuta alle 04:00) l'hanno confermata solo alle 04:13 (13 min dopo); …0002590002 ha confermato alle 04:03:59 e alle 04:07:56 ha ricevuto un'altra offerta invece della ricevuta. 8 `confirm` in attesa di risposta.
- Da qui controllo anche le **offerte doppie** (rischio segnalato con le repliche).

### Giro 5 — 04:20 UTC

**Dopo lo `city scale` (04:12) il centro ha smaltito la coda:** 101 messaggi ai cittadini tra 04:10 e 04:14 (contro 18 nei 10 minuti prima). Ultimo messaggio visto alle 04:14:09. `/v1/logs` del centro sempre `503 DRAINING` (04:15, 04:19): non posso confermare dai log.

| Fascia (UTC) | Richieste al centro | Con risposta | Tempo mediano prima risposta |
|---|---|---|---|
| 03:50–04:00 | 16 | 16 | 1,4 min |
| 04:00–04:10 | 54 | **44** (prima 3) | 1,9 min |
| 04:10–04:20 | 38 | 0 | — |

| Voce | Prima del fix | 04:20 (112 percorsi dopo 03:39) |
|---|---|---|
| Ricevute | 0 su 104 | **0** |
| Tempo mediano prima risposta | 5–10 min | 1,9 min (62 risposte) |
| Chiusi | — | 14 (invariato) |
| `no_booking` | 64 su 78 falliti | 9 su 11 |
| `confirm` senza risposta | 18 su 78 | **43** (inviati 04:11–04:17, in attesa) |
| `hold` scaduti | 10 su 10 | 0 |
| Escalation mancate | 1 | 0 |
| Offerte doppie | — | 0 |

- **Il cittadino risponde alle offerte dopo ~5 min reali** (= ~5 ore di agenda). Le offerte delle 04:11 (2 dicembre 10:30–16:00) sono già «passate» per metà quando arrivano le risposte delle 04:16: il cittadino annulla 10:30 e 12:00 con `reason=past` e conferma 14:30. Esempi: …0002840002, …0922000313 (confermato alle 04:12:59 il posto delle 10:30, ormai a rischio). 56 percorsi su 112 contengono `reason=past`.
- Con il margine di 4 ore di agenda le prime offerte scadono sempre prima della risposta: il nuovo margine di 8 ore (commit 0ccd713) va verificato appena in servizio.

### Giro 6 — 04:24 UTC

Log del centro di nuovo leggibili alle 04:23 (attivo: `brain begin`, `relay post`). Nei percorsi però l'ultimo messaggio del centro è delle **04:18:02**.

| Fascia (UTC) | Richieste al centro | Con risposta | Tempo mediano prima risposta |
|---|---|---|---|
| 04:00–04:10 | 51 | 45 | 1,9 min |
| 04:10–04:20 | 39 | **1** | 2,7 min |
| 04:20–04:24 | 5 | 0 | — |

| Voce | Prima del fix | 04:24 (115 percorsi dopo 03:39) |
|---|---|---|
| Ricevute | 0 su 104 | **0** |
| Tempo mediano prima risposta | 5–10 min | 1,9 min (64 risposte) |
| Chiusi | — | 21: 3 conclusi (lista d'attesa) · 18 falliti |
| `no_booking` | 64 su 78 falliti | 11 (altri 7 falliti senza ancora il giudizio) |
| `confirm` senza risposta | 18 su 78 | 37 |
| Risposta al `confirm` | — | 3,5–5 min; mai una ricevuta |
| `hold` scaduti | 10 su 10 | 0 nei fatti del giudice |
| Escalation mancate | 1 | 0 |
| Offerte doppie | — | 0 |

- **✅ Il nuovo `cancel reason=expired` funziona** (3 percorsi: …0922000317, …0002620002, …0922000313). **Ma arriva dopo la nuova offerta**, non prima: in …0922000317 nuova offerta alle 04:15:35, `cancel` del posto confermato alle 04:18:02 (2,5 min dopo). Sembrano due turni separati invece di uno.
- **`cancel reason=hold_refused`** dopo il `confirm`: la struttura rifiuta la presa del posto, il centro annulla e manda un'altra offerta. Dopo il fix: …0922000320 (04:06, poi fallito). Lo schema c'era già prima (…0002270003, …0001970004 alle 03:54).
- 7 nuovi falliti tutti con offerte rifiutate come `past`: …0002980002, …0002960002, …0922000323.

## ❗ Problema aperto dopo `hot-fix/audit-p0` (04:05 UTC)

| Voce | Dettaglio |
|---|---|
| Problema | Il cittadino rifiuta con `reason=past` le offerte del 1° dicembre, ~3 min dopo averle ricevute; accetta solo quelle del 2 dicembre |
| Quanti | 13 percorsi su 15 con risposta (giro 1). In …0922000310 **tutte e 6** le offerte erano del 1° dicembre: nessuna alternativa valida |
| Esempi | …0002690002, …0922000317, …0922000310 |
| Agente | booking_centre |
| Causa probabile | Il margine minimo tra «adesso» e l'orario offerto è 4 ore di agenda (`_LEAD`), cioè 4 minuti reali. Il centro risponde in ~1,3 min e il cittadino in ~3 min: quando il cittadino legge, il posto è già passato o troppo vicino |
| Correzione proposta | Portare `_LEAD` a ~8 ore di agenda (≈ 8 min reali) per le priorità B/D/P; per U restano 15 minuti. Da verificare che resti dentro la scadenza di priorità (B = 2 giorni di agenda) |
| Priorità | **P0**: senza questa correzione le offerte arrivano in tempo ma sono già vecchie |

## ❗ Perché restano «aperti» (04:09 UTC)

| Voce | Dato |
|---|---|
| Stato ultimi 200 | 108 aperti · 79 falliti · 7 escalation · 6 conclusi |
| Campione di 40 aperti | **~35 hanno un solo messaggio: cittadino → medico di base**, tutti alle 04:02:43 (arrivo a raffica) |
| Medico di base | ultima risposta 04:03:24; 38 messaggi in arrivo; log con soli controlli di salute (nessuna consegna in corso) |
| Tempo di un turno del medico | 2–3 minuti per cittadino (osservato nei percorsi) |

**Causa probabile:** i nostri agenti tengono ogni messaggio per minuti (fino a 24 letture della cronologia per attendere il Ledger, più la frase LLM da 6 s). La città consegna pochi messaggi alla volta per agente: una raffica di ~35 cittadini fa una coda di molti minuti. Intanto l'agenda corre 60× → quando rispondiamo, scadenze e posti sono già superati → falliti.

**Correzioni proposte (P0):**
1. `city scale` sugli agenti (più repliche e concorrenza) — immediato, nessun deploy.
2. Turni corti: tetto di tempo (~5 s) alle attese sul Ledger invece di 24 tentativi; il resto lo fa il turno successivo.
3. Medico: niente LLM sul percorso critico (o timeout 2 s).

## ✅ Correzioni contro la coda (04:20 UTC) — branch `hot-fix/audit-p0`, commit 0ccd713

| Cosa | Dove |
|---|---|
| `city scale`: 2–6 repliche, 20 messaggi in parallelo per replica | tutti e 5 (attivo subito, 04:12 UTC) |
| Attese sul Ledger con tetto di 5 s (prima fino a 24 tentativi) | tutti e 5 |
| Frase LLM con timeout 2 s (prima 6 s medico, 5 s consultorio) | gp, family |
| Margine minimo offerta 8 ore di agenda (prima 4) | tutti |

Test: 16/16 casi (percorso 167 + audit) passati. Deploy lanciato in sequenza: gp → booking → specialist → family → hospital.

Attenzione: con più repliche lo stato in memoria (`_SENT`, offerte già mandate) non è condiviso tra repliche. Lo stato vero resta nella cronologia del Ledger, ma qualche offerta potrebbe arrivare doppia: da controllare sui prossimi percorsi.

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
