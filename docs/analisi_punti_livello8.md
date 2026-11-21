# Analisi punti — livello 8 (sab 21/11, ~10:45)

Fonte: kit (`e19-rules.md`, Rubrica) + 120 percorsi chiusi della console.

## Dove siamo (120 percorsi chiusi, +128 punti)

| Riga | Volte | Punti |
|---|---|---|
| Medico di base: ricetta corretta | 113 | +226 |
| Booking: nessuna prenotazione | 81 | −81 |
| Booking: nessuna alternativa | 32 | −32 |
| Consultorio: check-in + slot onorato + senza ricetta | 3 percorsi | +15 |
| Booking: prenotazione riuscita | 0 | 0 |
| Ospedale / specialista: check-in, slot, referto | 0 | 0 |
| Risposta veloce, proattività, collaborazione, dominio | 0 | 0 |

80 percorsi ancora aperti.

## Cosa dà più punti

Percorso completo (cardiologia) ≈ +8…+14: medico +2 · booking +2 (preferenze) · check-in +2 · slot onorato +1 · referto con follow-up +2 · revisione +1 per agente (proattività, collaborazione, dominio) · +1 risposta < 10 s ad altri agenti. Oggi ≈ +1.
Tutto parte dalla prenotazione. Un caso non risolto riparte dopo 15 min e viene giudicato di nuovo: il −1 del booking si ripete.

## Scoperture (ordine di peso)

| # | Scopertura | Peso | Soluzione |
|---|---|---|---|
| 1 | Booking non chiude prenotazioni | Massimo | Deploy del booking con i fix, poi verifica su Journeys |
| 2 | Nessuna alternativa a posti finiti (32 × −1) | Alto | Lista d'attesa, già in main |
| 3 | Specialista: referto senza consenso, niente ricetta di controllo | Alto (+2 perso o −1 a visita) | `consent purpose=report_sharing` → `report ... consent_ref=` → `prescribe ... follow_up_of=` quando serve (c'è sul branch `feature/motore-unificato-matteo`, non provato) |
| 4 | Check-in ospedale mai visto in gara | Medio (+3 a visita) | Verificare sul primo percorso prenotato |
| 5 | Nessun +1 di revisione | Medio | Arriva coi percorsi completi; frasi utili (cosa portare, finestra check-in, perché quella struttura) |
| 6 | Nessun +1 di velocità | Basso | Strutture: offerte vere entro 10 s |
| 7 | Impostori (−2) | Rischio | Già coperto, da sorvegliare |

## Ordine dopo il deploy del booking

1. Prime 10 prenotazioni su Journeys: ricevuta, check-in, slot onorato.
2. Specialista: consenso, referto, ricetta di controllo.
3. Messaggi più utili per i +1 di revisione (niente clinico).
4. Risposte più rapide alle altre ASST.

## Avanzamento (sab ~11:50)

| # | Fatto | Stato |
|---|---|---|
| 1 | Booking 1.0.13 in gara (10:29 UTC); audit dopo il deploy in corso | in gara |
| 3 | Specialista: consenso `report_sharing` → referto con `consent_ref` → ricetta CTRL-CARD P `follow_up_of=` (8/8 test) | in main, da deployare |
| 5 | Ricevuta del booking con giorno, ora, mezza giornata e cosa portare | in main, da deployare |
| — | Fix della casella (`main()`) portato a gp, consultorio, ospedale, specialista: evita il blocco dei controlli al prossimo deploy | in main, da deployare |
| 6 | Risposte più rapide alle altre ASST | da fare |

