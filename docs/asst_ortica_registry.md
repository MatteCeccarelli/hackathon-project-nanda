# ASST Ortica (Wave W01QQQ) — Specifiche e Registro Ufficiale

> Dati ufficiali della nostra ASST (`asst-ortica`, Wave `W01QQQ`). I nostri agenti leggono gli stessi dati chiedendo `world` al Ledger. Le ASST partner non compaiono qui.

- **ASST**: `asst-ortica`
- **Wave ID**: `W01QQQ`
- **Municipio**: `Municipio 6`
- **Agenti**: `booking_centre` (centro prenotazioni), `gp` (medico di base), `hospital` (ospedale), `specialist` (specialista), `family_counseling` (consultorio)

---

## 1. Strutture e Medici (3 Strutture, 6 Medici)

I cittadini controllano ogni offerta su questo registro: lingue, genere e giorni di lavoro del medico.

### 1.1 `hospital`
- **Ubicazione**: Municipio 6
- **Caratteristiche**: Accesso in carrozzina
- **Prestazioni**: `VIS-FIS`
- **Medici**:
  - `asst-ortica-h1`: fisiatria (`physiatry`) · Femmina (`f`) · Lingue: `it, en` · Giorni: `lun, mar, mer`
  - `asst-ortica-h2`: fisiatria (`physiatry`) · Maschio (`m`) · Lingue: `it` · Giorni: `mer, gio, ven`
  - `asst-ortica-h3`: fisiatria (`physiatry`) · Maschio (`m`) · Lingue: `it, wo` (Wolof) · Giorni: `mar, gio`

### 1.2 `specialist`
- **Ubicazione**: Municipio 6
- **Prestazioni**: `VIS-CARD`, `CTRL-CARD`
- **Medici**:
  - `asst-ortica-s1`: cardiologia (`cardiology`) · Maschio (`m`) · Lingue: `it, en` · Giorni: `lun, mer, ven`
  - `asst-ortica-s2`: cardiologia (`cardiology`) · Femmina (`f`) · Lingue: `it` · Giorni: `mar, gio`

### 1.3 `family_counseling`
- **Ubicazione**: Municipio 6
- **Prestazioni**: `CONS-GIN`
- **Medici**:
  - `asst-ortica-c1`: ginecologia (`gynecology`) · Femmina (`f`) · Lingue: `it, en` · Giorni: `lun, mar, mer, gio, ven`

---

## 2. Prestazioni e Modalità di Accesso

| Prestazione | Specialità | Accesso | Erogata da |
|---|---|---|---|
| `VIS-FIS` | physiatry | Con ricetta | `hospital` |
| `VIS-CARD` | cardiology | Con ricetta | `specialist` |
| `CTRL-CARD` | cardiology | Con ricetta | `specialist` |
| `CONS-GIN` | gynecology | Accesso Diretto | `family_counseling` |

---

## 3. Protocolli Clinici e Red Flags (GP / Medico di Base)

| Sintomo | Prestazione | Priorità | Tempistica | Red Flag (Escalate to Emergency) |
|---|---|---|---|---|
| `SYM-BACK` | `VIS-FIS` | **B** | Entro 2 giorni di agenda | *perdita di forza alle gambe*; *loss of strength in the legs* |
| `SYM-KNEE` | `VIS-FIS` | **D** | Entro 5 giorni di agenda | — |
| `SYM-PALPITATIONS` | `VIS-CARD` | **B** | Entro 2 giorni di agenda | *dolore al petto*; *chest pain* |
| `SYM-FOLLOWUP` | `VIS-CARD` | **P** | Più avanti (lista d'attesa) | — |

---

## 4. Agenda Completa (90 Slot Settimanali)

- **Fascia 09:00**: Riserva Urgente (solo priorità `U`).
- **Fasce 10:30, 12:00, 14:30, 16:00**: Ordinarie (priorità `B`, `D`, `P`).
- **Posti**: 1 per slot.

### Lunedì
| Ora | Prestazione | Medico | Struttura | Posti | Riserva |
|---|---|---|---|---|---|
| 09:00 | VIS-FIS | asst-ortica-h1 | hospital | 1 | Urgente |
| 09:00 | VIS-CARD | asst-ortica-s1 | specialist | 1 | Urgente |
| 09:00 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | Urgente |
| 10:30 | VIS-FIS | asst-ortica-h1 | hospital | 1 | — |
| 10:30 | VIS-CARD | asst-ortica-s1 | specialist | 1 | — |
| 10:30 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | — |
| 12:00 | VIS-FIS | asst-ortica-h1 | hospital | 1 | — |
| 12:00 | VIS-CARD | asst-ortica-s1 | specialist | 1 | — |
| 12:00 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | — |
| 14:30 | VIS-FIS | asst-ortica-h1 | hospital | 1 | — |
| 14:30 | VIS-CARD | asst-ortica-s1 | specialist | 1 | — |
| 14:30 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | — |
| 16:00 | VIS-FIS | asst-ortica-h1 | hospital | 1 | — |
| 16:00 | CTRL-CARD | asst-ortica-s1 | specialist | 1 | — |
| 16:00 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | — |

### Martedì
| Ora | Prestazione | Medico | Struttura | Posti | Riserva |
|---|---|---|---|---|---|
| 09:00 | VIS-FIS | asst-ortica-h1 | hospital | 1 | Urgente |
| 09:00 | VIS-FIS | asst-ortica-h3 | hospital | 1 | Urgente |
| 09:00 | VIS-CARD | asst-ortica-s2 | specialist | 1 | Urgente |
| 09:00 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | Urgente |
| 10:30 | VIS-FIS | asst-ortica-h1 | hospital | 1 | — |
| 10:30 | VIS-FIS | asst-ortica-h3 | hospital | 1 | — |
| 10:30 | VIS-CARD | asst-ortica-s2 | specialist | 1 | — |
| 10:30 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | — |
| 12:00 | VIS-FIS | asst-ortica-h1 | hospital | 1 | — |
| 12:00 | VIS-FIS | asst-ortica-h3 | hospital | 1 | — |
| 12:00 | VIS-CARD | asst-ortica-s2 | specialist | 1 | — |
| 12:00 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | — |
| 14:30 | VIS-FIS | asst-ortica-h1 | hospital | 1 | — |
| 14:30 | VIS-FIS | asst-ortica-h3 | hospital | 1 | — |
| 14:30 | VIS-CARD | asst-ortica-s2 | specialist | 1 | — |
| 14:30 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | — |
| 16:00 | VIS-FIS | asst-ortica-h1 | hospital | 1 | — |
| 16:00 | VIS-FIS | asst-ortica-h3 | hospital | 1 | — |
| 16:00 | CTRL-CARD | asst-ortica-s2 | specialist | 1 | — |
| 16:00 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | — |

### Mercoledì
| Ora | Prestazione | Medico | Struttura | Posti | Riserva |
|---|---|---|---|---|---|
| 09:00 | VIS-FIS | asst-ortica-h1 | hospital | 1 | Urgente |
| 09:00 | VIS-FIS | asst-ortica-h2 | hospital | 1 | Urgente |
| 09:00 | VIS-CARD | asst-ortica-s1 | specialist | 1 | Urgente |
| 09:00 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | Urgente |
| 10:30 | VIS-FIS | asst-ortica-h1 | hospital | 1 | — |
| 10:30 | VIS-FIS | asst-ortica-h2 | hospital | 1 | — |
| 10:30 | VIS-CARD | asst-ortica-s1 | specialist | 1 | — |
| 10:30 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | — |
| 12:00 | VIS-FIS | asst-ortica-h1 | hospital | 1 | — |
| 12:00 | VIS-FIS | asst-ortica-h2 | hospital | 1 | — |
| 12:00 | VIS-CARD | asst-ortica-s1 | specialist | 1 | — |
| 12:00 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | — |
| 14:30 | VIS-FIS | asst-ortica-h1 | hospital | 1 | — |
| 14:30 | VIS-FIS | asst-ortica-h2 | hospital | 1 | — |
| 14:30 | VIS-CARD | asst-ortica-s1 | specialist | 1 | — |
| 14:30 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | — |
| 16:00 | VIS-FIS | asst-ortica-h1 | hospital | 1 | — |
| 16:00 | VIS-FIS | asst-ortica-h2 | hospital | 1 | — |
| 16:00 | CTRL-CARD | asst-ortica-s1 | specialist | 1 | — |
| 16:00 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | — |

### Giovedì
| Ora | Prestazione | Medico | Struttura | Posti | Riserva |
|---|---|---|---|---|---|
| 09:00 | VIS-FIS | asst-ortica-h2 | hospital | 1 | Urgente |
| 09:00 | VIS-FIS | asst-ortica-h3 | hospital | 1 | Urgente |
| 09:00 | VIS-CARD | asst-ortica-s2 | specialist | 1 | Urgente |
| 09:00 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | Urgente |
| 10:30 | VIS-FIS | asst-ortica-h2 | hospital | 1 | — |
| 10:30 | VIS-FIS | asst-ortica-h3 | hospital | 1 | — |
| 10:30 | VIS-CARD | asst-ortica-s2 | specialist | 1 | — |
| 10:30 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | — |
| 12:00 | VIS-FIS | asst-ortica-h2 | hospital | 1 | — |
| 12:00 | VIS-FIS | asst-ortica-h3 | hospital | 1 | — |
| 12:00 | VIS-CARD | asst-ortica-s2 | specialist | 1 | — |
| 12:00 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | — |
| 14:30 | VIS-FIS | asst-ortica-h2 | hospital | 1 | — |
| 14:30 | VIS-FIS | asst-ortica-h3 | hospital | 1 | — |
| 14:30 | VIS-CARD | asst-ortica-s2 | specialist | 1 | — |
| 14:30 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | — |
| 16:00 | VIS-FIS | asst-ortica-h2 | hospital | 1 | — |
| 16:00 | VIS-FIS | asst-ortica-h3 | hospital | 1 | — |
| 16:00 | CTRL-CARD | asst-ortica-s2 | specialist | 1 | — |
| 16:00 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | — |

### Venerdì
| Ora | Prestazione | Medico | Struttura | Posti | Riserva |
|---|---|---|---|---|---|
| 09:00 | VIS-FIS | asst-ortica-h2 | hospital | 1 | Urgente |
| 09:00 | VIS-CARD | asst-ortica-s1 | specialist | 1 | Urgente |
| 09:00 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | Urgente |
| 10:30 | VIS-FIS | asst-ortica-h2 | hospital | 1 | — |
| 10:30 | VIS-CARD | asst-ortica-s1 | specialist | 1 | — |
| 10:30 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | — |
| 12:00 | VIS-FIS | asst-ortica-h2 | hospital | 1 | — |
| 12:00 | VIS-CARD | asst-ortica-s1 | specialist | 1 | — |
| 12:00 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | — |
| 14:30 | VIS-FIS | asst-ortica-h2 | hospital | 1 | — |
| 14:30 | VIS-CARD | asst-ortica-s1 | specialist | 1 | — |
| 14:30 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | — |
| 16:00 | VIS-FIS | asst-ortica-h2 | hospital | 1 | — |
| 16:00 | CTRL-CARD | asst-ortica-s1 | specialist | 1 | — |
| 16:00 | CONS-GIN | asst-ortica-c1 | family_counseling | 1 | — |
