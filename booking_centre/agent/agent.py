"""Care-protocol example. ``city init`` does not copy this file.

The scaffold brain is ``templates/agent/agent/agent.py``. This one is the full care
protocol (prescribe, offer, hold, commit, check-in, report), one inbound message per
step. The role is ``role`` in ``card.yaml``. Ledger writes are chat to ``city:ledger``;
citizens read the act word and ``key=value`` tokens (ADR-110).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from city_sdk.city import LEDGER, CapError, City, Message, Need, OutOfScope, clinical, understood

ROLES = ("booking_centre", "gp", "hospital", "specialist_care", "family_counseling")
_MAX_PAGE = 8
# Four agenda hours is about half a minute of real time at 480x, so a slot is still
# ahead when the citizen decides. One hour was already past by then.
_LEAD = timedelta(hours=4)
# Doctor key (the last segment of doctor_id) -> languages, gender, days. ``*`` is the ASST's
# rare language. The Ledger's slot line names the doctor; it does not repeat this table.
_DOCTORS: dict[str, tuple[frozenset[str], str, frozenset[str]]] = {
    "h1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed"})),
    "h2": (frozenset({"it"}), "m", frozenset({"wed", "thu", "fri"})),
    "h3": (frozenset({"it", "*"}), "m", frozenset({"tue", "thu"})),
    "s1": (frozenset({"it", "en"}), "m", frozenset({"mon", "wed", "fri"})),
    "s2": (frozenset({"it"}), "f", frozenset({"tue", "thu"})),
    "c1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed", "thu", "fri"})),
}
_PROTOCOLS: dict[str, tuple[str, str]] = {
    "SYM-BACK": ("VIS-FIS", "B"),
    "SYM-KNEE": ("VIS-FIS", "D"),
    "SYM-PALPITATIONS": ("VIS-CARD", "B"),
    "SYM-FOLLOWUP": ("VIS-CARD", "P"),
}
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("SYM-BACK", ("mal di schiena", "back pain")),
    ("SYM-KNEE", ("ginocchio", "knee")),
    ("SYM-PALPITATIONS", ("cuore che batte", "heart racing")),
    ("SYM-FOLLOWUP", ("controllo del cuore", "routine heart check")),
)
_RED_FLAGS = (
    "perdita di forza alle gambe",
    "loss of strength in the legs",
    "dolore al petto",
    "chest pain",
)
_DEADLINE_DAYS = {"B": 2, "D": 5}
_RX = re.compile(r"^prescription_number-[A-Z0-9-]{4,40}$")
_LOG = logging.getLogger("care")
# The Relay accepts a ledger reply and then the agent rejects the push, so the reply sits in
# the case history and never opens a turn. Wait briefly and read it back from history.
_PULL_TRIES = 24
_PULL_PAUSE = 0.25
# Needs whose sent offers this process remembers; the oldest is forgotten first.
_SENT_NEEDS = 4096


def role_of() -> str:
    """The role this process serves, from ``CITY_ROLE`` or the card next to the brain."""

    forced = os.environ.get("CITY_ROLE", "").strip()
    if forced in ROLES:
        return forced
    path = os.environ.get("CITY_CARD", "")
    if not path:
        return ""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    return _card_role(text)


async def main(need: Need, city: City) -> None:
    message = city.inbound
    if message is None:
        return
    text = message.text or need.summary
    if clinical(text):
        raise OutOfScope()
    case = await city.history(need_id=message.need_id)
    await handle(role_of(), city, message, case)


def _asks_address(message: Message) -> bool:
    """True when this turn is a ``data.request`` that names the field ``address``."""

    text = message.text
    if text:
        named = False
        asked = False
        for token in text.split():
            bare = token.strip(".,;:!?()\"'")
            if bare == "data.request":
                named = True
            key, eq, value = bare.partition("=")
            if eq and key == "fields":
                names = {part.strip() for part in value.split(",") if part.strip()}
                asked = asked or "address" in names
        return named and asked
    if message.type != "data.request":
        return False
    raw = message.body.get("fields")
    if isinstance(raw, list):
        return "address" in raw
    if isinstance(raw, str):
        return "address" in {part.strip() for part in raw.replace(",", " ").split() if part.strip()}
    return False


def _own_street(city: City) -> str:
    """This agent's door: the city value, else the street shipped beside the brain."""

    street = getattr(city, "address", "")
    if isinstance(street, str) and street.strip():
        return street.strip()
    env = os.environ.get("CITY_ADDRESS", "").strip()
    if env:
        return env
    path = Path(__file__).with_name("door.txt")
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


async def handle(role: str, city: City, message: Message, chat: list[Message]) -> None:
    street = _own_street(city)
    if street and _asks_address(message):
        await city.say(message.sender, f"address={street}")
        return
    if role in _FAC_PROCEDURES and _fac_partner_request(message):
        await _fac_answer_partner(city, message, chat, role)
        return
    if role == "gp":
        await _gp(city, message, chat)
    elif role == "booking_centre":
        await _desk(city, message, chat, hold_self=False, fixed=None)
    elif role == "hospital":
        await _facility(city, message, chat, reports=False, hold_self=False)
    elif role == "specialist_care":
        await _facility(city, message, chat, reports=True, hold_self=False)
    elif role == "family_counseling":
        await _family(city, message, chat)


# --- shared ------------------------------------------------------------------------------------


def _card_role(text: str) -> str:
    role = ""
    types: list[str] = []
    in_types = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line[0] in " \t":
            if in_types:
                item = line.strip().removeprefix("-").strip().strip("'\"")
                if item:
                    types.append(item)
            continue
        in_types = False
        key, _, value = line.partition(":")
        value = value.strip().strip("'\"")
        if key.strip() == "role":
            role = value
        elif (
            key.strip() in {"capabilities", "service_types"}
            and value.startswith("[")
            and value.endswith("]")
        ):
            types = [part.strip().strip("'\"") for part in value[1:-1].split(",") if part.strip()]
        elif key.strip() in {"capabilities", "service_types"}:
            in_types = True
    if role in ROLES:
        return role
    for family in ("specialist_care", "family_counseling", "hospital", "booking_centre", "gp"):
        if any(item == family or item.startswith(family + ".") for item in types):
            return family
    return "hospital" if role == "facility" else ""


def words(text: str) -> set[str]:
    return {token.strip(".,;:!?()\"'").lower() for token in text.split() if token.strip()}


def first_word(text: str) -> str:
    parts = text.split()
    return parts[0].strip(".,;:!?()\"'").lower() if parts else ""


def fields_of(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for token in text.replace(",", " ").split():
        key, eq, value = token.partition("=")
        if not eq or not key or not value:
            continue
        value = value.strip(".,;:!?")
        if key not in found and value:
            found[key] = value
    return found


def _when(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _when_loose(value: str) -> datetime | None:
    """Come ``_when``, ma una data ISO senza fuso (formato MACH-1.1/1.2 tra squadre) vale
    come ora di agenda UTC, la stessa scala del Ledger."""
    at = _when(value)
    if at is None and value and "T" in value:
        at = _when(value + "+00:00")
    return at


def _citizen(chat: list[Message]) -> str | None:
    for row in chat:
        if row.sender.startswith("citizen:"):
            return row.sender
    for row in chat:
        fields = fields_of(row.text)
        for key in ("patient", "citizen"):
            party = fields.get(key, "")
            if party.startswith("citizen:"):
                return party
    return None


def _we(chat: list[Message], agent: str, token: str, *, to: str, word: str) -> bool:
    for row in chat:
        if row.sender != agent or row.recipient != to:
            continue
        if token in row.text.split() and word in words(row.text):
            return True
    return False


def _exact(chat: list[Message], agent: str, text: str) -> bool:
    return any(row.sender == agent and row.text.strip() == text for row in chat)


async def _say(city: City, to: str, text: str) -> bool:
    try:
        await city.say(to, text)
    except CapError:
        return False
    return True


def _latest(chat: list[Message], head: str, token: str) -> Message | None:
    found: Message | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == head and token in row.text.split():
            found = row
    return found


async def _until(
    city: City,
    need_id: str | None,
    chat: list[Message],
    ready: Callable[[list[Message]], bool],
) -> list[Message]:
    """Re-read the case until ``ready`` or the short wait ends. The push may never arrive."""

    fresh = chat
    for attempt in range(_PULL_TRIES):
        try:
            fresh = await city.history(need_id=need_id)
        except CapError:
            fresh = chat
        if ready(fresh):
            return fresh
        if attempt + 1 < _PULL_TRIES:
            await asyncio.sleep(_PULL_PAUSE)
    return fresh


async def _await_line(
    city: City,
    need_id: str | None,
    chat: list[Message],
    head: str,
    token: str,
    *,
    op: str,
) -> tuple[Message, list[Message]] | None:
    """The ledger line for ``token``, or a refusal of ``op`` that was not already in ``chat``."""

    known = {row.msg_id for row in chat}
    fresh = chat
    for attempt in range(_PULL_TRIES):
        try:
            fresh = await city.history(need_id=need_id)
        except CapError:
            fresh = chat
        hit = _latest(fresh, head, token)
        refusal = None
        for row in fresh:
            if row.msg_id in known or row.sender != LEDGER:
                continue
            if first_word(row.text) == "refused" and _refuses(row.text, op):
                refusal = row
        if refusal is not None and (hit is None or fresh.index(refusal) > fresh.index(hit)):
            _LOG.info("care.ledger.refused", extra={"op": op, "code": _ledger_code(refusal.text)})
            return refusal, fresh
        # Una riga gia' letta prima di questo tentativo puo' essere di un tentativo vecchio:
        # si preferisce la risposta nuova e si usa la vecchia solo a fine attesa.
        if hit is not None and (hit.msg_id not in known or attempt + 1 >= _PULL_TRIES):
            return hit, fresh
        if attempt + 1 < _PULL_TRIES:
            await asyncio.sleep(_PULL_PAUSE)
    return None


def _refuses(text: str, op: str) -> bool:
    """Rifiuto del Ledger per ``op``: parola dell'operazione o token ``op=<op>``."""
    return op in words(text) or f"op={op}" in text.split()


def _ledger_code(text: str) -> str:
    fields = fields_of(text)
    return (fields.get("code") or fields.get("reason") or "")[:40]


def _has_agenda(chat: list[Message]) -> bool:
    agenda = _ledger(chat, "agenda")
    return _when("" if agenda is None else agenda.get("agenda_now", "")) is not None


def _rx_reply(chat: list[Message]) -> dict[str, str] | None:
    """Risposta del Ledger sulla ricetta ATTUALE del cittadino (dopo un rinnovo cambia numero)."""
    number = _number(chat)
    rows = [row for row in chat if row.sender == LEDGER and first_word(row.text) == "prescription"]
    if not number:
        return fields_of(rows[-1].text) if rows else None
    matching = [row for row in rows if f"prescription_number={number}" in row.text.split()]
    if matching:
        return fields_of(matching[-1].text)
    if any("prescription_number=" in row.text for row in rows):
        return None  # il Ledger ha risposto solo per ricette precedenti: va chiesta questa
    return fields_of(rows[-1].text) if rows else None


def _has_procedure(chat: list[Message]) -> bool:
    reply = _rx_reply(chat)
    return reply is not None and bool(reply.get("procedure"))


def _rx(need_id: str | None, msg_id: str) -> str:
    raw = (need_id or msg_id or "CASE").upper()
    suffix = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in raw).strip("-")
    if len(suffix) < 4:
        suffix = (suffix + "0000")[:4]
    number = "prescription_number-" + suffix[:40]
    return number if _RX.fullmatch(number) else "prescription_number-CASE"


# Lingua rara del medico h3 di ogni ASST (``*`` nella tabella _DOCTORS). La nostra (ortica)
# viene dal registry della console; le altre dalla tabella concordata su Discord tra le squadre.
_RARE_BY_ASST = {
    "ortica": "wo",
    "giambellino": "ka",
    "toros": "ro",
    "nord": "ur",
    "gorla": "sq",
    "brera": "fa",
    "precotto": "ro",
    "corvetto": "fr",
    "isola": "tl",
    "greco": "si",
    "bicocca": "ti",
    "lambrate": "ja",
    "forlanini": "de",
}


def _asst_of(doctor_id: str) -> str:
    """``asst-nord-h3`` -> ``nord``; stringa vuota se il formato non è riconosciuto."""
    parts = doctor_id.split("-")
    return "-".join(parts[1:-1]) if len(parts) >= 3 and parts[0] == "asst" else ""


def _speaks(doctor_id: str, lang: str, hint: str = "") -> bool:
    spec = _DOCTORS.get(doctor_id.rsplit("-", 1)[-1])
    if spec is not None:
        langs = spec[0]
        if lang in langs:
            return True
        if "*" in langs and lang not in {"it", "en"}:
            rare = _RARE_BY_ASST.get(_asst_of(doctor_id))
            if rare is not None and rare == lang:
                return True
            if hint and lang in hint.replace(",", "/").split("/"):
                return True
    if hint:
        hint_langs = {l.strip() for l in hint.replace(",", "/").split("/") if l.strip()}
        if lang in hint_langs:
            return True
    if lang == "it":
        return True
    return False


def _is_urgent_reserve(slot: dict[str, str], at: datetime | None = None) -> bool:
    val = slot.get("urgent_reserve", "").lower()
    if val in ("true", "1", "yes"):
        return True
    if slot.get("reserve", "").upper() == "U":
        return True
    slot_id = slot.get("slot_id", "")
    if slot_id.endswith("-0900") or "-09:00" in slot_id or "-0900-" in slot_id:
        return True
    if at is not None and at.hour == 9 and at.minute == 0:
        return True
    at_str = slot.get("at") or slot.get("agenda_date") or ""
    if "T09:00" in at_str:
        return True
    return False


def fitting(
    lines: list[str],
    *,
    now: datetime,
    priority: str | None,
    lang: str,
    gender: str | None = None,
    facility: str | None = None,
    procedure: str | None = None,
) -> list[dict[str, str]]:
    """Slots a citizen can accept: future, free, in the priority window, spoken language."""

    if priority == "U":
        horizon: datetime | None = now.replace(hour=23, minute=59, second=59, microsecond=0)
        earliest = now + timedelta(minutes=15)
    elif priority == "P":
        horizon = None
        earliest = now + _LEAD
    else:
        horizon = now + timedelta(days=_DEADLINE_DAYS.get(priority or "", 14))
        earliest = now + _LEAD
    chosen: list[tuple[datetime, dict[str, str]]] = []
    for line in lines:
        slot = fields_of(line)
        slot_id = slot.get("slot_id")
        doctor = slot.get("doctor_id")
        at = _when(slot.get("at", ""))
        if not slot_id or not doctor or at is None:
            continue
        if procedure and slot.get("procedure") not in (None, procedure):
            continue
        if facility and slot.get("facility") != facility:
            continue
        free = slot.get("free")
        if free is not None and free.isdigit() and int(free) <= 0:
            continue
        reserve = _is_urgent_reserve(slot, at)
        if reserve and priority != "U":
            continue
        if not earliest < at or (horizon is not None and at >= horizon):
            continue
        if not _speaks(doctor, lang):
            continue
        spec = _DOCTORS.get(doctor.rsplit("-", 1)[-1])
        if spec is None:
            continue
        day = slot.get("day", "")
        if day and day not in spec[2]:
            continue
        if gender and spec[1] != gender:
            continue
        chosen.append((at, slot))
    chosen.sort(key=lambda item: (item[0], item[1].get("slot_id", "")))
    return [slot for _, slot in chosen]


# --- medico di base ----------------------------------------------------------------------------


def _emergency(message: Message) -> bool:
    if understood(message).body.get("is_emergency") is True:
        return True
    lowered = message.text.lower()
    return any(flag in lowered for flag in _RED_FLAGS)


def _symptom(message: Message) -> str | None:
    code = understood(message).body.get("symptom_code")
    if isinstance(code, str) and code in _PROTOCOLS:
        return code
    lowered = message.text.lower()
    for name, phrases in _KEYWORDS:
        if any(phrase in lowered for phrase in phrases):
            return name
    return None


def _our_rx(chat: list[Message], agent: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for row in chat:
        if row.sender == agent and "prescribe" in words(row.text):
            found = fields_of(row.text)
    return found


async def _gp(city: City, message: Message, chat: list[Message]) -> None:
    agent = city.agent_id
    if message.sender == LEDGER:
        await _announce(city, message, chat, agent)
        return
    if not message.sender.startswith("citizen:"):
        return
    if _emergency(message):
        if not _we(chat, agent, "escalate.emergency", to=message.sender, word="escalate.emergency"):
            await _say(city, message.sender, "escalate.emergency reason=red_flag")
        return
    code = _symptom(message)
    if code is None:
        return
    procedure, priority = _PROTOCOLS[code]
    number = _rx(message.need_id, message.msg_id)
    token = f"prescription_number={number}"
    # The citizen's reply window closes before a ledger round trip comes back, so the
    # prescription is said in this same turn. The ledger write is what the judge reads.
    if not _we(chat, agent, token, to=LEDGER, word="prescribe"):
        if not await _say(
            city,
            LEDGER,
            (
                f"prescribe patient={message.sender} procedure={procedure} priority={priority} "
                f"symptom_code={code} {token}"
            ),
        ):
            return
    if _we(chat, agent, token, to=message.sender, word="prescription"):
        return
    await _say(
        city,
        message.sender,
        f"prescription {token} procedure={procedure} priority={priority}",
    )


async def _announce(city: City, message: Message, chat: list[Message], agent: str) -> None:
    head = first_word(message.text)
    if head == "refused" and "DUPLICATE" not in message.text:
        return
    if head not in {"prescribed", "refused"}:
        return
    fields = fields_of(message.text)
    ours = _our_rx(chat, agent)
    number = fields.get("prescription_number") or ours.get("prescription_number")
    citizen = fields.get("patient") or _citizen(chat)
    if not number or not citizen:
        return
    if _we(chat, agent, f"prescription_number={number}", to=citizen, word="prescription"):
        return
    parts = ["prescription", f"prescription_number={number}"]
    procedure = fields.get("procedure") or ours.get("procedure")
    priority = fields.get("priority") or ours.get("priority")
    if procedure:
        parts.append(f"procedure={procedure}")
    if priority:
        parts.append(f"priority={priority}")
    await _say(city, citizen, " ".join(parts))


# --- centro prenotazioni and consultorio -------------------------------------------------------


def _ledger(chat: list[Message], head: str) -> dict[str, str] | None:
    found: dict[str, str] | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == head:
            found = fields_of(row.text)
    return found


def _latest_slots(chat: list[Message]) -> str | None:
    found: str | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == "slots":
            found = row.text
    return found


def _page(text: str) -> tuple[int, int]:
    raw = fields_of(text.splitlines()[0] if text else "").get("page", "")
    left, _, right = raw.partition("/")
    if left.isdigit() and right.isdigit():
        return int(left), int(right)
    return 1, 1


def _asked(chat: list[Message], agent: str, procedure: str, page: int) -> bool:
    token = f"page={page}"
    proc = f"procedure={procedure}"
    for row in chat:
        if row.sender == agent and token in row.text.split() and proc in row.text.split():
            return True
    return False


def _offered(chat: list[Message], agent: str) -> set[str]:
    found: set[str] = set()
    for row in chat:
        if row.sender != agent:
            continue
        for line in row.text.splitlines():
            if "offer" not in words(line):
                continue
            slot = fields_of(line).get("slot_id")
            if slot:
                found.add(slot)
    return found


def _withdrawn(chat: list[Message], agent: str) -> set[str]:
    """Slots we cancelled. The citizen drops the rest, so those are offered again."""

    by_offer: dict[str, str] = {}
    for row in chat:
        if row.sender != agent:
            continue
        for line in row.text.splitlines():
            if "offer" not in words(line):
                continue
            fields = fields_of(line)
            offer_id = fields.get("offer_id")
            slot = fields.get("slot_id")
            if offer_id and slot:
                by_offer[offer_id] = slot
    dropped: set[str] = set()
    for row in chat:
        if "cancel" not in words(row.text):
            continue
        slot = by_offer.get(fields_of(row.text).get("offer_id", ""))
        if slot:
            dropped.add(slot)
    return dropped


def _rejected_doctors(chat: list[Message], agent: str) -> set[str]:
    doctors = set()
    for row in chat:
        if row.sender.startswith("citizen:") and "cancel" in words(row.text):
            fields = fields_of(row.text)
            if fields.get("reason") == "lang":
                offer = _our_offer(chat, agent, fields.get("offer_id", ""))
                if offer.get("doctor_id"):
                    doctors.add(offer["doctor_id"])
    return doctors


def _number(chat: list[Message]) -> str | None:
    for row in reversed(chat):
        if row.sender.startswith("citizen:"):
            number = fields_of(row.text).get("prescription_number")
            if number:
                return number
    return None


def _lang(chat: list[Message]) -> str:
    for row in chat:
        if row.sender.startswith("citizen:"):
            lang = fields_of(row.text).get("lang", "")
            if lang:
                return lang
    for row in chat:
        if row.sender.startswith("citizen:") and re.search(
            r"\b(?:english[- ]speaking|speaks? english|parl[ai] inglese)\b", row.text, re.I
        ):
            return "en"
    return "it"


def _gender(chat: list[Message]) -> str | None:
    for row in chat:
        if not row.sender.startswith("citizen:"):
            continue
        raw = fields_of(row.text).get("gender_preference") or fields_of(row.text).get("gender")
        if raw in {"m", "f"}:
            return raw
    return None


# --- comodità: giorni, orario e distretto del cittadino ------------------------------------
# Prima si tengono solo gli slot validi (fitting / controlli del partner), poi si ordinano per
# le preferenze dette dal cittadino. I giorni sono una preferenza (S_pref): mettono prima gli
# slot giusti ma non escludono gli altri. Un orario detto come vincolo («posso solo dopo le
# 16») esclude gli slot fuori fascia; detto come preferenza («meglio il pomeriggio») li ordina.

_DAY_NAMES: dict[str, tuple[str, ...]] = {
    "mon": ("lunedì", "lunedi", "monday"),
    "tue": ("martedì", "martedi", "tuesday"),
    "wed": ("mercoledì", "mercoledi", "wednesday"),
    "thu": ("giovedì", "giovedi", "thursday"),
    "fri": ("venerdì", "venerdi", "friday"),
    "sat": ("sabato", "saturday"),
    "sun": ("domenica", "sunday"),
}
_NEGATION = r"(?:non|not|no|tranne|except|escluso|esclusi)\W+(?:\w+\W+){0,3}"
_HARD_WORDS = re.compile(
    r"\b(?:solo|soltanto|unicamente|esclusivamente|only|non posso|cannot|can't|can not)\b"
)


def _citizen_texts(chat: list[Message]) -> list[str]:
    return [row.text for row in chat if row.sender.startswith("citizen:")]


def _days_pref(chat: list[Message]) -> set[str]:
    """Giorni preferiti: token ``days=mon/wed`` oppure nomi dei giorni nel testo del cittadino."""
    for text in _citizen_texts(chat):
        raw = fields_of(text).get("days", "")
        if raw:
            got = {part.strip().lower()[:3] for part in re.split(r"[/,;|]", raw) if part.strip()}
            got &= set(_DAY_NAMES)
            if got:
                return got
    found: set[str] = set()
    for text in _citizen_texts(chat):
        low = text.lower()
        for code, names in _DAY_NAMES.items():
            for name in names:
                if re.search(rf"\b{name}\b", low) and not re.search(rf"\b{_NEGATION}{name}\b", low):
                    found.add(code)
    return found


def _hour(raw: str, minutes: str | None, pm: str | None) -> int:
    hour = int(raw)
    if pm and hour < 12:
        hour += 12
    elif hour < 8:
        # «dopo le 4» in un contesto di visite vuol dire le 16.
        hour += 12
    return min(hour, 24) * 60 + (int(minutes) if minutes else 0)


def _time_pref(chat: list[Message]) -> tuple[int, int, bool] | None:
    """Fascia oraria detta dal cittadino: (da_minuto, a_minuto, vincolante)."""
    hhmm = r"(?:ore\s*)?(\d{1,2})(?:[:.](\d{2}))?\s*(pm|p\.m\.)?"
    for text in reversed(_citizen_texts(chat)):
        low = text.lower()
        patterns = (
            (rf"\b(?:dopo\s+le|dalle|after)\s+{hhmm}", "from"),
            (rf"\b(?:prima\s+delle|entro\s+le|before)\s+{hhmm}", "to"),
            (r"\b(?:pomeriggio|afternoon|evening|sera)\b", "pm"),
            (r"\b(?:mattina|mattino|mattinata|morning)\b", "am"),
        )
        for pattern, kind in patterns:
            match = re.search(pattern, low)
            if match is None:
                continue
            before = low[max(0, match.start() - 30): match.start()]
            if re.search(rf"\b{_NEGATION}$", before + " "):
                continue
            hard = _HARD_WORDS.search(before) is not None
            if kind == "from":
                return (_hour(match.group(1), match.group(2), match.group(3)), 24 * 60, hard)
            if kind == "to":
                return (0, _hour(match.group(1), match.group(2), match.group(3)), hard)
            if kind == "pm":
                return (13 * 60, 24 * 60, hard)
            return (0, 13 * 60, hard)
    return None


def _district(chat: list[Message]) -> str:
    for text in _citizen_texts(chat):
        fields = fields_of(text)
        value = fields.get("area.district") or fields.get("district") or ""
        if value:
            return value
    return ""


def _slot_time(slot: dict[str, str]) -> datetime | None:
    return _when(slot.get("at") or slot.get("agenda_date") or "")


def _comfort_miss(
    slot: dict[str, str], days: set[str], window: tuple[int, int, bool] | None
) -> tuple[int, int]:
    """(fuori fascia oraria, fuori giorni preferiti): 0 = rispettato."""
    at = _slot_time(slot)
    day = (slot.get("day") or slot.get("agenda_day") or "").lower()
    if not day and at is not None:
        day = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[at.weekday()]
    day_miss = 1 if days and day and day not in days else 0
    time_miss = 0
    if window is not None and at is not None:
        minute = at.hour * 60 + at.minute
        time_miss = 0 if window[0] <= minute < window[1] else 1
    return (time_miss, day_miss)


def _bc_by_comfort(slots: list[dict[str, str]], chat: list[Message]) -> list[dict[str, str]]:
    """Slot validi già in ordine di data -> esclude i fuori fascia vincolanti, poi ordina."""
    days = _days_pref(chat)
    window = _time_pref(chat)
    if not days and window is None:
        return slots
    if window is not None and window[2]:
        slots = [slot for slot in slots if _comfort_miss(slot, set(), window)[0] == 0]
    return sorted(slots, key=lambda slot: _comfort_miss(slot, days, window))


def _bc_comfort_note(english: bool, slots: list[dict[str, str]], chat: list[Message]) -> str:
    """Frase che spiega l'ordine (niente parole-atto, niente dati clinici)."""
    days = _days_pref(chat)
    window = _time_pref(chat)
    if not slots or (not days and window is None):
        return ""
    what_it = " e ".join(x for x in ("nei giorni" if days else "", "nella fascia oraria" if window else "") if x)
    what_en = " and ".join(x for x in ("on the days" if days else "", "at the times" if window else "") if x)
    if _comfort_miss(slots[0], days, window) == (0, 0):
        if english:
            return f"I put first the option {what_en} you mentioned."
        return f"Ho messo per prima l'opzione {what_it} che ha indicato."
    if english:
        return f"I found nothing {what_en} you mentioned, so these are the earliest times available."
    return f"Non ho trovato posti {what_it} che ha indicato: queste sono le prime date disponibili."


def _offer_line(slot: dict[str, str], number: str | None, priority: str | None) -> str:
    parts = [
        "offer",
        f"offer_id=of-{slot['slot_id']}",
        f"slot_id={slot['slot_id']}",
        f"doctor_id={slot['doctor_id']}",
        f"facility={slot['facility']}",
        f"procedure={slot['procedure']}",
        f"agenda_date={slot['at']}",
        f"agenda_day={slot['day']}",
        f"expiry={slot['at']}",
    ]
    if _is_urgent_reserve(slot):
        parts.append("urgent_reserve=true")
    if priority:
        parts.append(f"priority={priority}")
    if number:
        parts.append(f"prescription_number={number}")
    return " ".join(parts)


def _bc_offers_courtesy(english: bool, procedure: str, slots: list[dict[str, str]]) -> str:
    if not slots:
        return ""
    proc_it = "cardiologica" if "CARD" in procedure else ("ginecologica" if "GIN" in procedure else "fisiatrica")
    proc_en = "cardiology" if "CARD" in procedure else ("gynecology" if "GIN" in procedure else "physiatry")
    days_it = {"mon": "lunedì", "tue": "martedì", "wed": "mercoledì", "thu": "giovedì", "fri": "venerdì"}
    days_en = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday"}

    parts = []
    for s in slots:
        d = s.get("day") or s.get("agenda_day", "")
        d_name = (days_en if english else days_it).get(d.lower(), d)
        at_val = s.get("at") or s.get("agenda_date") or ""
        time_part = at_val[11:16] if "T" in at_val and len(at_val) >= 16 else ""
        if english:
            parts.append(f"{d_name}{' at ' + time_part if time_part else ''}")
        else:
            parts.append(f"{d_name}{' alle ore ' + time_part if time_part else ''}")

    joined = ", ".join(parts)
    if english:
        if len(slots) == 1:
            return f"Here is an available appointment for your {proc_en} visit on {joined}. Please reply with your confirmation if you wish to proceed."
        return f"Here are the available appointments for your {proc_en} visit: {joined}. Please reply with your confirmation indicating your preferred slot."
    if len(slots) == 1:
        return f"Le propongo questa disponibilità per la visita {proc_it} per {joined}. Mi risponda con la sua conferma se desidera procedere."
    return f"Le propongo le seguenti disponibilità per la visita {proc_it}: {joined}. Mi risponda con la sua conferma indicando l'opzione preferita."


def _bc_offer_courtesy(english: bool, procedure: str, day: str, at_str: str) -> str:
    return _bc_offers_courtesy(english, procedure, [{"day": day, "at": at_str}])


def _our_offer(chat: list[Message], agent: str, offer_id: str) -> dict[str, str]:
    token = f"offer_id={offer_id}"
    for row in chat:
        if row.sender != agent:
            continue
        for line in row.text.splitlines():
            if token in line.split() and "offer" in words(line):
                return fields_of(line)
    return {}


class _Gate:
    """One need's lock and how many turns hold it or wait for it."""

    __slots__ = ("lock", "turns")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.turns = 0


class _Sent:
    """Offers this process sent for one need, which the case history may not show yet.

    ``offers`` holds every slot id (and waiting-list offer id) sent. A cancel makes the
    citizen drop all offers, so ``again`` holds only what was re-sent for ``trigger``.
    """

    __slots__ = ("offers", "trigger", "again", "retried", "waiting_list")

    def __init__(self) -> None:
        self.offers: set[str] = set()
        self.retried: set[str] = set()
        self.trigger = ""
        self.again: set[str] = set()
        self.waiting_list = False


# Every Ledger or citizen message opens its own turn, and turns of one need run at the same
# time. Each reads the case before the others' offers are in it, so offers wait on this.
# An entry lives only while a turn holds or waits on its lock.
_GATES: dict[str, _Gate] = {}
_SENT: OrderedDict[str, _Sent] = OrderedDict()


def _sent(key: str) -> _Sent:
    entry = _SENT.get(key)
    if entry is None:
        entry = _SENT[key] = _Sent()
        while len(_SENT) > _SENT_NEEDS:
            _SENT.popitem(last=False)
    else:
        _SENT.move_to_end(key)
    return entry


async def _desk(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    hold_self: bool,
    fixed: str | None,
) -> None:
    if message.sender == city.agent_id:
        return

    # Solo il centro prenotazioni usa la collaborazione tra ASST.
    # Il consultorio mantiene il percorso diretto della baseline.
    if fixed is None and not hold_self:
        if _ledger(chat, "committed") is not None and "booking.receipt" not in words(message.text):
            return
        if message.sender.startswith("agent:") and _bc_offer_lines(message.text):
            # MACH-1.1: "cancel reason=slot_taken offer_id=..." seguito dalle offerte rimaste,
            # nello stesso messaggio: prima si ritira l'offerta persa, poi si leggono le altre.
            withdrawn = await _bc_partner_cancel_lines(city, message, chat)
            # I cancel appena mandati entrano nel thread: l'offerta ritirata non blocca le nuove.
            await _bc_external_offer(city, message, [*chat, *withdrawn])
            return
        if (
            message.sender.startswith("agent:")
            and "cancel" in words(message.text)
            and fields_of(message.text).get("offer_id", "").lower() in {"", "none", "null", "-"}
            and not _bc_offer_of_booking(chat, message.sender, fields_of(message.text).get("booking_id", ""))
        ):
            # cancel reason=no_availability request_id=...: il partner non ha posto.
            req_id = fields_of(message.text).get("request_id") or _bc_latest_request_id(chat, city.agent_id)
            _LOG.info("care.partner.no_availability", extra={"partner": message.sender, "request_id": req_id})
            status = _bc_partner_status([*chat, message], city.agent_id, req_id)
            pending = [p for p, s in status.items() if s == "pending"]
            citizen = _citizen(chat)
            if status and not pending and citizen and _bc_open_partner_offers(chat, city.agent_id, citizen) == 0:
                offer_id = f"wl-{message.need_id or req_id}"
                if not _we(chat, city.agent_id, f"offer_id={offer_id}", to=citizen, word="offer"):
                    note = _BC_WAITLIST_EN if _lang(chat) != "it" else _BC_WAITLIST_IT
                    if await _say(city, citizen, f"offer offer_id={offer_id} waiting_list=true\n{note}"):
                        _sent(message.need_id or req_id).waiting_list = True
            return
        if message.sender.startswith("citizen:"):
            if first_word(message.text) == "confirm":
                if await _bc_external_confirm(city, message, chat):
                    return
            elif "cancel" not in words(message.text) and await _bc_cross_reply(city, message, chat):
                return

    kind = words(message.text)
    if "confirm" in kind and (
        message.sender.startswith("citizen:") or message.sender.startswith("agent:")
    ):
        await _on_confirm(city, message, chat, hold_self=hold_self)
        return
    if "booking.receipt" in kind and message.sender.startswith("agent:"):
        await _forward_receipt(city, message, chat)
        return
    if "cancel" in kind and message.sender.startswith("agent:"):
        await _partner_cancel(city, message, chat, fixed=fixed)
        return
    if message.sender == LEDGER or message.sender.startswith("citizen:"):
        is_cancel = message.sender.startswith("citizen:") and "cancel" in kind
        # Il catalogo chiesto dopo un cancel puo' arrivare in un turno a parte: senza questo
        # il turno vedrebbe gli slot gia' offerti come presi e il cittadino resterebbe senza
        # alternative (percorsi 1670002 / 1670003).
        if message.sender == LEDGER and fixed is None and _awaiting_replacement(chat, city.agent_id):
            trigger = _replacement_trigger(chat, city.agent_id)
            if trigger is not None:
                await _advance(city, trigger, chat, fixed=fixed, replacing=True)
                return
        await _advance(city, message, chat, fixed=fixed, replacing=is_cancel)


def _awaiting_replacement(chat: list[Message], agent: str) -> bool:
    """L'ultimo nostro messaggio al cittadino è un cancel, senza offerte dopo."""
    citizen = _citizen(chat)
    if not citizen:
        return False
    for row in reversed(chat):
        if row.sender == agent and row.recipient == citizen:
            head = first_word(row.text)
            if head == "offer" or head == "booking.receipt":
                return False
            if head == "cancel":
                return True
    return False


def _replacement_trigger(chat: list[Message], agent: str) -> Message | None:
    """Il cancel (della struttura o del cittadino) che ha fatto perdere un nostro slot."""
    for row in reversed(chat):
        if row.sender == agent or row.sender == LEDGER or "cancel" not in words(row.text):
            continue
        offer_id = fields_of(row.text).get("offer_id", "")
        if offer_id and _our_offer(chat, agent, offer_id):
            return row
    return None


def _late_page(chat: list[Message], agent: str, page: int) -> Message | None:
    """Pagina del catalogo arrivata dopo il nostro ultimo cancel al cittadino (turno a parte)."""
    citizen = _citizen(chat)
    start = -1
    for i, row in enumerate(chat):
        if row.sender == agent and row.recipient == citizen and first_word(row.text) == "cancel":
            start = i
    if start < 0:
        return None
    for row in reversed(chat[start + 1:]):
        if row.sender == LEDGER and first_word(row.text) == "slots" and _page(row.text)[0] == page:
            return row
    return None


async def _advance(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    fixed: str | None,
    replacing: bool,
) -> None:
    key = message.need_id or message.msg_id
    gate = _GATES.get(key)
    if gate is None:
        gate = _GATES[key] = _Gate()
    gate.turns += 1
    try:
        async with gate.lock:
            await _advance_alone(city, message, chat, key, fixed=fixed, replacing=replacing)
    finally:
        gate.turns -= 1
        if gate.turns == 0 and _GATES.get(key) is gate:
            del _GATES[key]


async def _advance_alone(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
    *,
    fixed: str | None,
    replacing: bool,
) -> None:
    agent = city.agent_id
    citizen = _citizen(chat)
    if citizen is None:
        return
    retry = replacing and fixed is None
    sent = _sent(key)
    if retry:
        try:
            chat = await city.history(need_id=message.need_id)
        except CapError:
            return
        if _ledger(chat, "committed") is not None:
            return
        if all(row.msg_id != message.msg_id for row in chat):
            chat = [*chat, message]
        fields = fields_of(message.text)
        offer_id = fields.get("offer_id", "")
        lost = _our_offer(chat, agent, offer_id)
        if not lost or message.sender not in (citizen, lost.get("facility")):
            return
        if offer_id in sent.retried or any(
            row.sender == agent and first_word(row.text) == "need.request"
            and fields_of(row.text).get("request_id") == f"{key}.{offer_id}"
            for row in chat
        ):
            return
    reply = _rx_reply(chat)
    priority = None if reply is None else reply.get("priority")
    procedure = fixed or (None if reply is None else reply.get("procedure"))
    number = _number(chat)
    agenda = _ledger(chat, "agenda")
    now = _when_loose("" if agenda is None else agenda.get("agenda_now", ""))
    world = _latest_slots(chat)
    # Ask, then read the reply from history: the push of a ledger message does not open a turn.
    for _pass in range(3):
        reply = _rx_reply(chat)
        priority = None if reply is None else reply.get("priority")
        if priority == "P":
            offer_id = f"wl-{message.need_id or message.msg_id}"
            token = f"offer_id={offer_id}"
            sent = _sent(key)
            if offer_id in sent.offers:
                return
            if not _we(chat, agent, token, to=citizen, word="offer"):
                lang = _lang(chat)
                wl_courtesy = (
                    "Your prescription has no rush (P priority). I have added your request to our waiting list to offer the earliest available slot."
                    if lang != "it"
                    else "La ricetta ha priorità non urgente (P). Ho registrato la sua richiesta nella lista d'attesa per proporle il primo posto disponibile."
                )
                if await _say(city, citizen, f"offer offer_id={offer_id} waiting_list=true\n{wl_courtesy}"):
                    sent.offers.add(offer_id)
            return
        procedure = fixed or (None if reply is None else reply.get("procedure"))
        number = _number(chat)
        if fixed is None and number and not procedure:
            if _pass == 2:
                return
            if not any(
                row.sender == agent and f"prescription_number={number}" in row.text.split()
                for row in chat
            ):
                await _say(city, LEDGER, f"prescription prescription_number={number}")
            if not _exact(chat, agent, "agenda"):
                await _say(city, LEDGER, "agenda")
            chat = await _until(
                city,
                message.need_id,
                chat,
                lambda rows: _has_procedure(rows) and _has_agenda(rows),
            )
            continue
        if not procedure:
            return
        state = None if reply is None else reply.get("state")
        if fixed is None and _rx_expired(chat, reply, None):
            # Ricetta scaduta (stato o rifiuto del Ledger): si rimanda al medico di base.
            await _bc_ask_renewal(city, chat, citizen, number)
            return
        if fixed is None and state not in (None, "issued"):
            return
        if fixed is None and not _exact(chat, agent, "rules"):
            # Regole in vigore (P1: ricette valide 2 giorni di agenda), lette una volta.
            await _say(city, LEDGER, "rules")
        agenda = _ledger(chat, "agenda")
        now = _when_loose("" if agenda is None else agenda.get("agenda_now", ""))
        world = _latest_slots(chat)
        if now is not None and (world is not None or retry):
            break
        if _pass == 2:
            return
        if now is None and not _exact(chat, agent, "agenda"):
            await _say(city, LEDGER, "agenda")
        if world is None and not _asked(chat, agent, procedure, 1):
            await _say(city, LEDGER, f"world procedure={procedure} page=1")
        chat = await _until(
            city,
            message.need_id,
            chat,
            lambda rows: _has_agenda(rows) and _latest_slots(rows) is not None,
        )
    else:
        return
    if now is None or (world is None and not retry):
        return
    if fixed is None and _rx_expired(chat, _rx_reply(chat), now):
        # Regola P1: la ricetta ha piu' di 2 giorni di agenda. Prenotarla costerebbe -1.
        await _bc_ask_renewal(city, chat, citizen, _number(chat))
        return
    retry_page = 1
    rejected_doctors = _rejected_doctors(chat, agent) if fixed is None else set()
    # A cancel drops every offer the citizen was holding, so the ones still free are sent again,
    # once per cancel: a second turn for the same cancel or refusal does not repeat them.
    sent = _sent(key)
    if replacing:
        trigger = fields_of(message.text).get("offer_id") or message.msg_id
        if sent.trigger != trigger:
            sent.trigger = trigger
            sent.again = set()
        taken = _withdrawn(chat, agent) | sent.again
    else:
        taken = _offered(chat, agent) | sent.offers
    while True:
        late = _late_page(chat, agent, retry_page) if retry else None
        if late is not None:
            world = late.text
        elif retry:
            seen = {row.msg_id for row in chat}
            if not await _say(city, LEDGER, f"world procedure={procedure} page={retry_page}"):
                return
            def fresh_page(rows: list[Message]) -> Message | None:
                return next((row for row in reversed(rows) if row.msg_id not in seen
                             and row.sender == LEDGER and first_word(row.text) == "slots"
                             and _page(row.text)[0] == retry_page), None)

            chat = await _until(city, message.need_id, chat, lambda rows: fresh_page(rows) is not None)
            page_reply = fresh_page(chat)
            if page_reply is None or _ledger(chat, "committed") is not None:
                return
            world = page_reply.text
        chosen = [
            slot
            for slot in fitting(
                [line for line in world.splitlines() if "slot_id=" in line],
                now=now,
                priority=priority,
                lang=_lang(chat),
                gender=_gender(chat),
                facility=agent if fixed == "CONS-GIN" else None,
                procedure=procedure,
            )
            if slot.get("slot_id") not in taken and slot.get("doctor_id") not in rejected_doctors
        ]
        chosen = _bc_by_comfort(chosen, chat)
        if not retry or chosen or retry_page >= min(_page(world)[1], _MAX_PAGE):
            break
        retry_page += 1
    if chosen:
        lang = _lang(chat)
        slots_to_offer = chosen[:3]
        act_lines = [_offer_line(s, number, priority) for s in slots_to_offer]
        courtesy = _bc_offers_courtesy(lang != "it", procedure, slots_to_offer)
        note = _bc_comfort_note(lang != "it", slots_to_offer, chat)
        if note:
            courtesy = f"{courtesy} {note}".strip()
        full_text = "\n".join(act_lines) + (f"\n{courtesy}" if courtesy else "")
        if await _say(city, citizen, full_text):
            if retry:
                sent.retried.add(offer_id)
            for s in slots_to_offer:
                sent.offers.add(s["slot_id"])
                if replacing:
                    sent.again.add(s["slot_id"])
        return
    if taken and not replacing:
        return
    page, pages = _page(world)
    if page < pages and page < _MAX_PAGE:
        # Comportamento base del template: si chiede la pagina successiva e la si
        # elabora al turno in cui arriva la risposta del Ledger.
        if not _asked(chat, agent, procedure, page + 1):
            await _say(city, LEDGER, f"world procedure={procedure} page={page + 1}")
        return

    # Solo dopo l'ultima pagina locale: si cercano gli agenti di altre ASST.
    if fixed is None:
        prev_crits = _bc_sent_criteria(chat, city.agent_id)
        days = _days_pref(chat)
        days_str = "/".join(d for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun") if d in days) if days else ""
        current_crit = (
            procedure,
            priority or "",
            _lang(chat),
            _gender(chat) or "",
            days_str,
        )
        criteria_updated = bool(prev_crits and current_crit not in prev_crits)

        if not retry and not criteria_updated:
            if await _bc_waiting_list_fallback(city, message, chat, citizen, key, procedure, priority):
                return

        await _bc_discover(city, message, chat, key, procedure, priority,
                           retry_offer=offer_id if retry else "")


async def _on_confirm(
    city: City, message: Message, chat: list[Message], *, hold_self: bool
) -> None:
    agent = city.agent_id
    fields = fields_of(message.text)
    offer_id = fields.get("offer_id", "")
    consent = fields.get("consent_ref") or fields.get("cross_asst_consent_ref", "")
    offer = _our_offer(chat, agent, offer_id) if offer_id else {}
    slot = fields.get("slot_id") or offer.get("slot_id", "")
    facility = fields.get("facility") or offer.get("facility", "")
    if _fac_is_partner_confirm(message, agent):
        problem = _fac_confirm_problem(chat, agent, message, slot, consent)
        if problem:
            if not _we(chat, agent, f"offer_id={offer_id}", to=message.sender, word="cancel"):
                tag = f" request_id={fields['request_id']}" if fields.get("request_id") else ""
                await _say(city, message.sender, f"cancel offer_id={offer_id} reason={problem}{tag}")
            return
    if not slot or not consent:
        return
    citizen = fields.get("citizen") or (
        message.sender if message.sender.startswith("citizen:") else ""
    )
    number = fields.get("prescription_number") or offer.get("prescription_number")
    book_here = hold_self or message.sender.startswith("agent:") or facility == agent
    if book_here:
        if not _we(chat, agent, f"slot_id={slot}", to=LEDGER, word="hold"):
            parts = ["hold", f"slot_id={slot}"]
            if citizen:
                parts.append(f"citizen={citizen}")
            if number:
                parts.append(f"prescription_number={number}")
            parts.append(f"consent_ref={consent}")
            if not await _say(city, LEDGER, " ".join(parts)):
                return
        await _finish_booking(city, message, chat, slot, hold_self=hold_self)
        return
    if not facility or not citizen:
        return
    if _we(chat, agent, f"slot_id={slot}", to=facility, word="confirm"):
        return
    parts = [
        "confirm",
        f"offer_id={offer_id or offer.get('offer_id', '')}",
        f"slot_id={slot}",
        f"citizen={citizen}",
        f"consent_ref={consent}",
        f"facility={facility}",
    ]
    if number:
        parts.append(f"prescription_number={number}")
    await _say(city, facility, " ".join(parts))


async def _forward_receipt(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    booking_id = fields.get("booking_id", "")
    ledger_ref = fields.get("ledger_ref", "")
    citizen = _citizen(chat)
    if not booking_id or not ledger_ref or not citizen:
        return
    token = f"booking_id={booking_id}"
    if _we(chat, city.agent_id, token, to=citizen, word="booking.receipt"):
        return
    # MACH-1.2 (fiducia): una ricevuta da chi non ha ricevuto un nostro confirm è una
    # ricevuta fantasma; girarla al cittadino costerebbe -2 anche a noi.
    if message.sender not in _OUR_AGENTS and not any(
        row.sender == city.agent_id and row.recipient == message.sender and "confirm" in words(row.text)
        for row in chat
    ):
        return
    await _say(city, citizen, f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref}")


def _bc_offer_of_booking(chat: list[Message], partner: str, booking_id: str) -> str:
    """MACH-1.2 livello 6: un cancel può portare solo booking_id. Si risale all'offer_id dalla
    ricevuta che lo stesso partner ci aveva mandato (o dal confirm che gli avevamo girato)."""
    if not booking_id or booking_id.lower() in {"none", "null", "-"}:
        return ""
    token = f"booking_id={booking_id}"
    for row in reversed(chat):
        if row.sender == partner and token in row.text.split() and "booking.receipt" in words(row.text):
            fields = fields_of(row.text)
            if fields.get("offer_id"):
                return fields["offer_id"]
            slot = fields.get("slot_id", "")
            for sent in reversed(chat):
                if slot and sent.recipient == partner and f"slot_id={slot}" in sent.text.split():
                    found = fields_of(sent.text).get("offer_id", "")
                    if found:
                        return found
    # Ricevuta senza offer_id né slot_id: l'ultimo confirm girato a quel partner.
    for row in reversed(chat):
        if row.recipient == partner and "confirm" in words(row.text):
            return fields_of(row.text).get("offer_id", "")
    return ""


async def _partner_cancel(
    city: City, message: Message, chat: list[Message], *, fixed: str | None
) -> None:
    fields = fields_of(message.text)
    offer_id = fields.get("offer_id", "")
    if offer_id.lower() in {"", "none", "null", "-"}:
        offer_id = _bc_offer_of_booking(chat, message.sender, fields.get("booking_id", ""))
    citizen = _citizen(chat)
    expired = fields.get("reason") == "prescription_expired"
    reason = "prescription_expired" if expired else "facility_cancelled"
    _LOG.info(
        "care.partner.cancel",
        extra={"reason": fields.get("reason", "")[:40], "ledger_code": fields.get("ledger_code", "")[:40]},
    )
    if fixed is None and _our_offer(chat, city.agent_id, offer_id).get("facility") != message.sender:
        return
    if (
        offer_id
        and citizen
        and not _we(chat, city.agent_id, f"offer_id={offer_id}", to=citizen, word="cancel")
    ):
        await _say(city, citizen, f"cancel offer_id={offer_id} reason={reason}")
    if expired and fixed is None:
        # Il Ledger ha rifiutato il commit per ricetta scaduta (P1): niente altre offerte,
        # il cittadino passa dal medico di base per il rinnovo.
        await _bc_ask_renewal(city, chat, citizen, _number(chat))
        return
    await _advance(city, message, chat, fixed=fixed, replacing=True)




# --- centro prenotazioni: collaborazione inter-ASST ---------------------------------------------
# I messaggi need.request ai peer contengono solo criteri di ricerca, mai dati personali.
# Le operazioni hold/commit restano di competenza della facility destinataria.

_BC_SERVICE_TYPES = {
    "VIS-FIS": "hospital.physiatry",
    "VIS-CARD": "specialist_care.cardiology",
}


# Gli agent_id veri della città non contengono il team (es. agent:geyjmp26tajh), quindi
# un partner si riconosce come "non uno dei nostri". Sono gli id scritti in card.yaml dal
# primo deploy e restano gli stessi nei deploy successivi: aggiornarli solo se cambiano lì.
_OUR_AGENTS = frozenset(
    {
        "agent:cgj1c2mf1dx0",  # specialist
        "agent:vs1bs4485dcn",  # hospital
        "agent:ega0tzcy6za8",  # gp
        "agent:geyjmp26tajh",  # booking_centre
        "agent:d0kkschyjrdq",  # family_counseling
    }
)


def _bc_is_partner(city: City, agent_id: str) -> bool:
    """Un agente di un'altra ASST: id valido, diverso da noi e da ogni nostro agente."""
    return (
        agent_id.startswith("agent:")
        and len(agent_id) > len("agent:")
        and agent_id != city.agent_id
        and agent_id not in _OUR_AGENTS
    )


def _bc_partner_offer(city: City, chat: list[Message], offer: dict[str, str]) -> bool:
    """Un'offerta nostra che inoltra uno slot di un partner che NOI abbiamo interrogato.

    Le offerte costruite dagli slot del Ledger (anche di strutture di altre ASST) restano
    sul percorso base del template, che non va intercettato.
    """
    facility = offer.get("facility", "")
    return _bc_is_partner(city, facility) and _bc_asked_partner(
        chat, city.agent_id, facility, offer.get("procedure", "")
    )


def _bc_asked_partner(chat: list[Message], agent: str, partner: str, procedure: str) -> bool:
    """Autorizzazione a considerare una risposta: quel partner fu interrogato per la procedura."""
    return any(
        row.sender == agent and row.recipient == partner
        and first_word(row.text) == "need.request"
        and fields_of(row.text).get("procedure") == procedure
        for row in chat
    )


def _bc_extract_criteria(text: str) -> tuple[str, str, str, str, str]:
    fields = fields_of(text)
    return (
        fields.get("procedure", ""),
        fields.get("priority", ""),
        fields.get("lang", "it"),
        fields.get("gender_preference") or fields.get("gender", ""),
        fields.get("days", ""),
    )


def _bc_sent_criteria(chat: list[Message], agent: str) -> list[tuple[str, str, str, str, str]]:
    """Criteri distinti (procedure, priority, lang, gender, days) gia' inviati in need.request."""
    seen: list[tuple[str, str, str, str, str]] = []
    for row in chat:
        if row.sender == agent and first_word(row.text) == "need.request":
            c = _bc_extract_criteria(row.text)
            if c not in seen:
                seen.append(c)
    return seen


def _bc_latest_request_id(chat: list[Message], agent: str) -> str:
    """request_id dell'ultimo need.request inviato a qualsiasi partner."""
    for row in reversed(chat):
        if row.sender == agent and first_word(row.text) == "need.request":
            return fields_of(row.text).get("request_id", "")
    return ""


def _bc_partner_status(chat: list[Message], agent: str, req_id: str) -> dict[str, str]:
    """Mappa partner_id -> 'pending' | 'no_availability' | 'offered' per il request_id specificato."""
    status: dict[str, str] = {}
    for i, row in enumerate(chat):
        if row.sender == agent and first_word(row.text) == "need.request":
            if fields_of(row.text).get("request_id") == req_id:
                partner = row.recipient
                if partner not in status:
                    status[partner] = "pending"
                for reply in chat[i + 1:]:
                    if reply.sender == partner and reply.recipient == agent:
                        w = words(reply.text)
                        if _bc_offer_lines(reply.text):
                            status[partner] = "offered"
                            break
                        elif "cancel" in w:
                            status[partner] = "no_availability"
                            break
    return status


def _bc_search_start_time(chat: list[Message], agent: str, req_id: str) -> datetime | None:
    for row in chat:
        if row.sender == agent and first_word(row.text) == "need.request":
            if fields_of(row.text).get("request_id") == req_id:
                if hasattr(row, "sent_at") and row.sent_at:
                    return _when_loose(row.sent_at)
    return None


def _message_time(message: Message) -> datetime | None:
    if hasattr(message, "sent_at") and message.sent_at:
        return _when_loose(message.sent_at)
    return None


async def _bc_discover(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
    procedure: str,
    priority: str | None,
    *,
    retry_offer: str = "",
) -> None:
    """Fallback dopo l'esaurimento del catalogo locale, senza trasmettere dati del cittadino."""
    service = _BC_SERVICE_TYPES.get(procedure)
    if not service or priority in (None, "P") or not _number(chat):
        return

    lang = _lang(chat)
    gender = _gender(chat)
    days = _days_pref(chat)
    days_str = "/".join(d for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun") if d in days) if days else ""
    current_crit = (
        procedure,
        priority or "",
        lang or "it",
        gender or "",
        days_str,
    )
    prev_crits = _bc_sent_criteria(chat, city.agent_id)
    criteria_updated = bool(prev_crits and current_crit not in prev_crits)

    # Non aprire nuove ricerche se abbiamo gia' proposto uno slot valido e i criteri non sono cambiati.
    if not retry_offer and not criteria_updated and (_offered(chat, city.agent_id) or _sent(key).offers):
        return
    try:
        peers = await city.find(service)
    except CapError:
        return

    # Criteri di ricerca (non dati personali): lingua e genere preferiti aiutano il partner
    # a proporre il medico giusto invece di slot che il cittadino rifiuterebbe.
    criteria = ""
    if lang and lang != "it":
        criteria += f" lang={lang}"
    if gender:
        criteria += f" gender_preference={gender}"
    if days_str:
        criteria += f" days={days_str}"
    district = _district(chat)

    rev = len(prev_crits) + 1 if criteria_updated else (len(prev_crits) or 1)
    if retry_offer:
        request_id = f"{key}.{retry_offer}"
    elif criteria_updated:
        request_id = f"{key}.{lang}" if (lang and lang != "it") else f"{key}.v{rev}"
    elif lang and lang != "it" and not prev_crits:
        request_id = f"{key}.{lang}"
    else:
        request_id = key

    contacted = 0
    # MACH-1.1: con una lingua rara si scrive PRIMA all'ASST che la parla, poi ai partner che
    # hanno pubblicato i loro id (fidati), infine agli altri trovati sull'Index.
    peers = sorted(
        peers,
        key=lambda peer: (
            _bc_peer_rank(getattr(peer, "agent_id", ""), lang),
            0 if district and str(getattr(peer, "district", "")) == district else 1,
        ),
    )
    for peer in peers:
        peer_id = getattr(peer, "agent_id", "")
        if not _bc_is_partner(city, peer_id):
            continue
        capabilities = getattr(peer, "capabilities", ())
        if capabilities and service not in capabilities:
            continue
        # Contro gli impostori (contesto, sez. 6): sull'Index una struttura ha role=facility.
        role = getattr(peer, "role", "")
        if role and role != "facility":
            continue
        if _we(chat, city.agent_id, f"request_id={request_id}", to=peer_id, word="need.request"):
            continue
        request = (
            f"need.request service_type={service} procedure={procedure} "
            f"priority={priority} request_id={request_id}{criteria}"
        )
        if await _say(city, peer_id, request):
            if retry_offer:
                _sent(key).retried.add(retry_offer)
            contacted += 1
        # Con una lingua diversa dall'italiano il medico giusto sta in una sola ASST (es. urdu ->
        # Nord): si chiede a tutti i partner trovati, altrimenti si resta a 12.
        if contacted >= (_BC_MAX_PARTNERS_LANG if (lang and lang != "it") or gender else 12):
            break

    # Il cittadino non resta senza risposta mentre aspettiamo i partner.
    citizen = _citizen(chat)
    if contacted and citizen:
        note = _BC_SEARCHING_EN if lang and lang != "it" else _BC_SEARCHING_IT
        if not any(
            row.sender == city.agent_id and row.recipient == citizen and row.text == note
            for row in chat
        ):
            await _say(city, citizen, note)


_BC_MAX_PARTNERS_LANG = 40

# Id degli agenti pubblicati dalle squadre su Discord (MACH-1.1, "Fiducia"). Sono dichiarazioni
# non verificate: servono solo a decidere l'ORDINE in cui chiedere, non a fidarsi dei dati.
# Il controllo vero resta: si accettano offerte solo da chi abbiamo interrogato noi e con
# facility uguale al mittente.
_PUBLISHED_IDS: dict[str, frozenset[str]] = {
    "lorenteggio": frozenset({"agent:qtjne8symjas", "agent:w2qsptc9qqhg", "agent:tmt0bcv0st1f", "agent:n9bf9y6670hp", "agent:mcppmrbjvxag"}),
    "giambellino": frozenset({"agent:cdc7x6m21nj6", "agent:c32jyk0cyan7", "agent:h6r1v45esk77", "agent:hnpe58epeyh1", "agent:bzs050xacs1m"}),
    "lambrate": frozenset({"agent:gdrf9b9kxxss", "agent:c2ewegr2ea3v", "agent:nanpr5mgm2py", "agent:pky3vqyqbepa", "agent:atp772whjkgd"}),
    "nord": frozenset({"agent:psxp8bh1feaa", "agent:wwxaphfb5pfh", "agent:gpr1zqe343gb", "agent:zand0f57ec1j", "agent:ccjzqm7yekwx"}),
    "greco": frozenset({"agent:h3n6kfd18r3g", "agent:bmrcsmqq6at2", "agent:xtj1dk9x3qej", "agent:q18es2d82f8r", "agent:pxvd2kk2yx5j"}),
    "bicocca": frozenset({"agent:ez8cb7j167p2", "agent:rng5wcrzvhsj", "agent:bb01ggxymm05", "agent:sh35cbsy287d", "agent:nkh372rd16yq"}),
    "bovisa": frozenset({"agent:vgtmj5nqfkxj", "agent:h82xpx3rkn9e", "agent:gxq3vzgt3h5t", "agent:tty8erdj6t41", "agent:txar5994gbkj"}),
    "isola": frozenset({"agent:etpwrfmfa8hm", "agent:mkfzagfe8jtt"}),
    "precotto": frozenset({"agent:ser7es7thgwf", "agent:n4yjr7kjbmrx", "agent:s1twcdzg3296", "agent:f2s88vwd3jnf", "agent:nf9pqsgzrwm0"}),
    "corvetto": frozenset({"agent:yzcs4p44ep39", "agent:hkrtm1gzjb24", "agent:x0vr9rbwpesr", "agent:h8425ac7btd8", "agent:p0n57t7vtpq0"}),
}
_PUBLISHED_ALL = frozenset().union(*_PUBLISHED_IDS.values())


def _bc_peer_rank(agent_id: str, lang: str) -> int:
    """0 = ASST che parla la lingua rara richiesta, 1 = id pubblicato, 2 = altri."""
    if lang and lang not in {"it", "en"}:
        for asst, rare in _RARE_BY_ASST.items():
            if rare == lang and agent_id in _PUBLISHED_IDS.get(asst, frozenset()):
                return 0
    return 1 if agent_id in _PUBLISHED_ALL else 2


async def _bc_partner_cancel_lines(
    city: City, message: Message, chat: list[Message]
) -> list[Message]:
    """Righe ``cancel offer_id=...`` di un partner su offerte che abbiamo girato al cittadino.
    Restituisce i cancel mandati, come righe del thread."""
    citizen = _citizen(chat)
    sent: list[Message] = []
    if not citizen:
        return sent
    for line in message.text.splitlines():
        if first_word(line) != "cancel":
            continue
        fields = fields_of(line)
        offer_id = fields.get("offer_id", "")
        if offer_id.lower() in {"", "none", "null", "-"}:
            offer_id = _bc_offer_of_booking(chat, message.sender, fields.get("booking_id", ""))
        offer = _our_offer(chat, city.agent_id, offer_id) if offer_id else {}
        if not offer or offer.get("facility") != message.sender:
            continue
        if _we(chat, city.agent_id, f"offer_id={offer_id}", to=citizen, word="cancel"):
            continue
        reason = fields.get("reason", "facility_cancelled")
        text = f"cancel offer_id={offer_id} reason={reason}"
        if await _say(city, citizen, text):
            sent.append(
                Message(
                    type="chat", sender=city.agent_id, msg_id=f"local-cancel-{offer_id}",
                    need_id=message.need_id, body={"text": text}, recipient=citizen,
                )
            )
    return sent

_BC_WAITLIST_IT = (
    "Nessuna ASST mi ha proposto per ora un medico adatto alle sue preferenze entro la scadenza: "
    "l'ho inserita in lista d'attesa e le scrivo appena si libera un posto adatto."
)
_BC_WAITLIST_EN = (
    "No ASST has proposed a doctor that fits your preferences before the deadline so far: "
    "I have put you on the waiting list and will write to you as soon as a suitable time is free."
)


async def _bc_waiting_list_fallback(
    city: City,
    message: Message,
    chat: list[Message],
    citizen: str,
    key: str,
    procedure: str,
    priority: str | None,
) -> bool:
    """Propone la lista d'attesa una sola volta quando:
    - tutti i partner contattati hanno risposto senza disponibilita' o con offerte non valide;
    - oppure scade la finestra di attesa limitata (2 minuti) per partner ancora pendenti.
    Un messaggio del cittadino da solo non anticipa la lista d'attesa se la ricerca e' ancora pendente.
    """
    agent = city.agent_id
    if message.sender != citizen:
        return False
    if _bc_open_partner_offers(chat, agent, citizen) > 0:
        return False
    offer_id = f"wl-{message.need_id or key}"
    if _we(chat, agent, f"offer_id={offer_id}", to=citizen, word="offer"):
        return False
    if _offered(chat, agent) or _sent(key).offers:
        return False

    req_id = _bc_latest_request_id(chat, agent)
    if not req_id:
        return False

    # Se i criteri sono aggiornati, la ricerca va riaperta invece di proporre la lista d'attesa
    prev_crits = _bc_sent_criteria(chat, agent)
    days = _days_pref(chat)
    days_str = "/".join(d for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun") if d in days) if days else ""
    current_crit = (
        procedure,
        priority or "",
        _lang(chat),
        _gender(chat) or "",
        days_str,
    )
    if prev_crits and current_crit not in prev_crits:
        return False

    status = _bc_partner_status(chat, agent, req_id)
    if not status:
        return False

    pending = [p for p, s in status.items() if s == "pending"]
    if pending:
        # Finestra di attesa limitata compatibile con il percorso (120 s)
        t_start = _bc_search_start_time(chat, agent, req_id)
        t_now = _message_time(message)
        if t_start and t_now:
            elapsed = (t_now - t_start).total_seconds()
            if elapsed < 120:
                return False
        else:
            return False

    note = _BC_WAITLIST_EN if _lang(chat) != "it" else _BC_WAITLIST_IT
    if await _say(city, citizen, f"offer offer_id={offer_id} waiting_list=true\n{note}"):
        _sent(key).waiting_list = True
        return True
    return False


_BC_SEARCHING_IT = (
    "Nella nostra ASST non ci sono posti adatti alle sue preferenze entro la scadenza della "
    "ricetta: sto chiedendo alle altre ASST e le invio le loro proposte appena arrivano."
)
_BC_SEARCHING_EN = (
    "Our ASST has no free time that fits your preferences before your prescription deadline: "
    "I am asking the other ASSTs and will send you their options as soon as they arrive."
)


def _bc_fits_citizen(chat: list[Message], doctor_id: str, hint: str = "") -> bool:
    """Lingua e genere del medico dal registry (stessa tabella degli slot locali)."""
    if not _speaks(doctor_id, _lang(chat), hint):
        return False
    gender = _gender(chat)
    if gender:
        spec = _DOCTORS.get(doctor_id.rsplit("-", 1)[-1])
        if spec is None or spec[1] != gender:
            return False
    return True


def _bc_latest_consent_request(chat: list[Message], agent: str, citizen: str) -> int:
    """Posizione dell'ultima richiesta esplicita di consenso cross_asst."""
    index = -1
    for i, row in enumerate(chat):
        if (
            row.sender == agent and row.recipient == citizen
            and first_word(row.text) == "consent"
            and fields_of(row.text).get("purpose") == "cross_asst"
        ):
            index = i
    return index


def _bc_cross_ref(chat: list[Message], agent: str, citizen: str) -> str:
    """Accetta solo il consent_ref ricevuto dopo un consenso cross_asst richiesto."""
    request_index = _bc_latest_consent_request(chat, agent, citizen)
    if request_index < 0:
        return ""
    for row in chat[request_index + 1:]:
        if row.sender != citizen:
            continue
        data = fields_of(row.text)
        if (
            first_word(row.text) == "consent"
            and data.get("consent_ref")
            and data.get("purpose", "cross_asst") == "cross_asst"
        ):
            return data["consent_ref"]
    return ""


def _bc_recent_confirmation(chat: list[Message], citizen: str, before: int | None = None) -> Message | None:
    rows = chat if before is None else chat[:before]
    for row in reversed(rows):
        if row.sender == citizen and first_word(row.text) == "confirm":
            return row
    return None


async def _bc_forward_confirm(city: City, confirmation: Message, chat: list[Message]) -> None:
    """Invia dati identificativi SOLO dopo un consenso cross-ASST valido."""
    citizen = confirmation.sender
    offer_id = fields_of(confirmation.text).get("offer_id", "")
    offer = _our_offer(chat, city.agent_id, offer_id) if offer_id else {}
    facility = offer.get("facility", "")
    slot = offer.get("slot_id", "")
    consent = _bc_cross_ref(chat, city.agent_id, citizen)
    if not (offer_id and slot and consent and _bc_is_partner(city, facility)):
        return
    # Evita di utilizzare offerte mai ricevute dalla facility dichiarata.
    if not _bc_asked_partner(chat, city.agent_id, facility, offer.get("procedure", "")):
        return
    if _we(chat, city.agent_id, f"slot_id={slot}", to=facility, word="confirm"):
        return
    # MACH-1.1: consent_ref e' il consenso cross_asst; cross_asst_consent_ref (nome MACH-1)
    # con lo stesso valore per i partner fermi alla versione precedente.
    parts = [
        "confirm", f"offer_id={offer_id}", f"slot_id={slot}",
        f"consent_ref={consent}", f"cross_asst_consent_ref={consent}",
    ]
    request_id = _bc_request_id_to(chat, city.agent_id, facility)
    if request_id:
        parts.append(f"request_id={request_id}")
    number = offer.get("prescription_number") or _number(chat)
    if number:
        parts.append(f"prescription_number={number}")
    parts += [f"citizen={citizen}", f"facility={facility}"]
    await _say(city, facility, " ".join(parts))


def _bc_request_id_to(chat: list[Message], agent: str, partner: str) -> str:
    """request_id dell'ultima need.request mandata a quel partner."""
    for row in reversed(chat):
        if row.sender == agent and row.recipient == partner and first_word(row.text) == "need.request":
            return fields_of(row.text).get("request_id", "")
    return ""


async def _bc_external_confirm(city: City, message: Message, chat: list[Message]) -> bool:
    offer_id = fields_of(message.text).get("offer_id", "")
    offer = _our_offer(chat, city.agent_id, offer_id) if offer_id else {}
    if not _bc_partner_offer(city, chat, offer):
        return False  # Lascia invariata la gestione del confirm del template.

    citizen = message.sender
    if _bc_cross_ref(chat, city.agent_id, citizen):
        await _bc_forward_confirm(city, message, chat)
        return True

    if _bc_latest_consent_request(chat, city.agent_id, citizen) < 0:
        await _say(
            city, citizen,
            "consent purpose=cross_asst Posso condividere con un'altra ASST "
            "i dati necessari alla prenotazione?",
        )
    return True


async def _bc_cross_reply(city: City, message: Message, chat: list[Message]) -> bool:
    """Riprende il confirm sospeso dopo il consenso, o cancella l'offerta in caso di no."""
    citizen = message.sender
    request_index = _bc_latest_consent_request(chat, city.agent_id, citizen)
    if request_index < 0:
        return False
    # Non consumare normali need.request che non sono risposte alla richiesta di consenso.
    after = chat[request_index + 1:]
    if not any(row.msg_id == message.msg_id for row in after):
        return False
    confirmation = _bc_recent_confirmation(chat, citizen, before=request_index)
    if confirmation is None:
        return False
    offer_id = fields_of(confirmation.text).get("offer_id", "")
    offer = _our_offer(chat, city.agent_id, offer_id)
    if not _bc_partner_offer(city, chat, offer):
        return False

    if _bc_cross_ref(chat, city.agent_id, citizen):
        await _bc_forward_confirm(city, confirmation, chat)
    elif not _we(chat, city.agent_id, f"offer_id={offer_id}", to=citizen, word="cancel"):
        # Il rifiuto in linguaggio naturale non fornisce consent_ref.
        await _say(city, citizen, f"cancel offer_id={offer_id} reason=cross_asst_consent_declined")
    return True


# --- ricetta scaduta (regola P1): rinnovo dal medico di base -------------------------------
# Il Ledger non documenta il formato esatto: si riconosce la scadenza da piu' segnali.
_RX_VALID_DAYS = 2
_RX_ISSUED_KEYS = ("issued_at", "issued", "agenda_issued", "issue_date", "created_at", "agenda_date")
_RX_EXPIRY_KEYS = ("expires_at", "valid_until", "expiry", "expires")


def _p1_active(chat: list[Message]) -> bool:
    return any(
        row.sender == LEDGER and first_word(row.text) == "rules" and "P1" in row.text.replace("=", " ").split()
        for row in chat
    ) or any(row.sender == LEDGER and "rule_id=P1" in row.text for row in chat)


def _rx_expired(chat: list[Message], rx: dict[str, str] | None, now: datetime | None) -> bool:
    """La ricetta corrente risulta scaduta per il Ledger o per la regola P1."""
    if not rx:
        return False
    number = rx.get("prescription_number", "")
    state = rx.get("state", "").lower()
    if state in {"expired", "scaduta", "invalid"}:
        return True
    if rx.get("valid", "").lower() in {"false", "no", "0"}:
        return True
    # Rifiuto PRESCRIPTION_EXPIRED: conta solo se riguarda questa ricetta (numero nel testo) o
    # se arriva dopo l'ultima lettura di questa ricetta, mai per una ricetta precedente.
    seen_rx = False
    for row in chat:
        if row.sender != LEDGER:
            continue
        if first_word(row.text) == "prescription" and f"prescription_number={number}" in row.text.split():
            seen_rx = True
            continue
        if "PRESCRIPTION_EXPIRED" not in row.text:
            continue
        if number and number in row.text:
            return True
        if seen_rx and "prescription_number-" not in row.text:
            return True
    if now is None:
        return False
    for key in _RX_EXPIRY_KEYS:
        until = _when(rx.get(key, ""))
        if until is not None and now >= until:
            return True
    if _p1_active(chat):
        for key in _RX_ISSUED_KEYS:
            issued = _when(rx.get(key, ""))
            if issued is not None:
                return now >= issued + timedelta(days=_RX_VALID_DAYS)
    return False


_BC_RENEW_IT = (
    "La sua ricetta risulta scaduta, quindi non posso prenotarla: torni dal medico di base "
    "indicando il numero di ricetta per il rinnovo, poi mi riscriva con la ricetta nuova e le "
    "propongo subito gli orari."
)
_BC_RENEW_EN = (
    "Your prescription has expired, so I cannot book it: please go back to your GP with the "
    "prescription number to renew it, then write to me with the new prescription and I will "
    "propose the available times right away."
)


async def _bc_ask_renewal(
    city: City, chat: list[Message], citizen: str | None, number: str | None
) -> None:
    """data.request purpose=prescription_renewal (formato del kit), una sola volta per ricetta."""
    if not citizen:
        return
    token = f"prescription_number={number}" if number else "purpose=prescription_renewal"
    for row in chat:
        if (
            row.sender == city.agent_id and row.recipient == citizen
            and "data.request" in words(row.text) and "purpose=prescription_renewal" in row.text
            and token in row.text.split()
        ):
            return
    note = _BC_RENEW_EN if _lang(chat) != "it" else _BC_RENEW_IT
    act = "data.request purpose=prescription_renewal fields=prescription_number"
    if number:
        act += f" prescription_number={number}"
    await _say(city, citizen, f"{act}\n{note}")


def _cancel_reason(chat: list[Message]) -> str:
    """Motivo (MACH-1.1) dell'ultimo rifiuto del Ledger, con ledger_code quando serve."""
    refusals = [row for row in chat if row.sender == LEDGER and first_word(row.text) == "refused"]
    if not refusals:
        return "facility_cancelled"
    text = refusals[-1].text
    if "PRESCRIPTION_EXPIRED" in text:
        return "prescription_expired"
    if "CONSENT_MISSING" in text:
        return "consent_missing"
    fields = fields_of(text)
    code = fields.get("code") or fields.get("reason") or ""
    if code in {"HELD", "ALREADY_HELD", "TAKEN", "SLOT_TAKEN", "CONFLICT"}:
        return f"slot_taken ledger_code={code}"
    if code == "NO_CAPACITY" or "NO_CAPACITY" in text:
        return "no_capacity ledger_code=NO_CAPACITY"
    return f"hold_refused ledger_code={code}" if code else "hold_refused"


def _fac_is_partner_confirm(message: Message, agent: str) -> bool:
    return (
        message.sender.startswith("agent:")
        and message.sender != agent
        and message.sender not in _OUR_AGENTS
    )


def _fac_confirm_problem(
    chat: list[Message], agent: str, message: Message, slot: str, consent: str
) -> str:
    """MACH-1.1: senza consenso niente hold; confirm su un'offerta mai mandata a quel partner."""
    if not consent:
        return "consent_missing"
    offered = [
        fields_of(line)
        for row in chat
        if row.sender == agent and row.recipient == message.sender
        for line in row.text.splitlines()
        if first_word(line) == "offer"
    ]
    if offered and slot not in {info.get("slot_id") for info in offered}:
        return "not_offered"
    return ""


def _fac_receipt_extra(chat: list[Message], agent: str, slot: str, target: str) -> str:
    """Campi MACH-1.1 della ricevuta verso un centro prenotazioni partner."""
    if not target.startswith("agent:") or target in _OUR_AGENTS:
        return ""
    info: dict[str, str] = {}
    for row in chat:
        for line in row.text.splitlines():
            fields = fields_of(line)
            if fields.get("slot_id") != slot:
                continue
            if row.sender == agent and first_word(line) == "offer":
                info.update({k: v for k, v in fields.items() if k in {"offer_id", "doctor_id", "agenda_date", "request_id"}})
            if row.sender == target and first_word(line) == "confirm":
                info.update({k: v for k, v in fields.items() if k in {"offer_id", "request_id"}})
    parts = [f"offer_id={info['offer_id']}"] if info.get("offer_id") else []
    parts.append(f"slot_id={slot}")
    for key in ("request_id", "doctor_id", "agenda_date"):
        if info.get(key):
            parts.append(f"{key}={info[key]}")
    parts.append(f"facility={agent}")
    return " " + " ".join(parts)


_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# Offerte dei partner ancora in piedi davanti al cittadino: oltre questo numero non se ne
# inoltrano altre finché il cittadino non ne rifiuta qualcuna (niente raffiche di offerte).
_BC_MAX_OPEN_PARTNER_OFFERS = 3


def _bc_offer_lines(text: str) -> list[dict[str, str]]:
    """Formato comune tra ASST: un solo messaggio, una riga ``offer`` per slot."""
    rows = [fields_of(line) for line in text.splitlines() if first_word(line) == "offer"]
    if not rows and first_word(text) == "offer":
        rows = [fields_of(text)]
    return rows


def _bc_is_partner_id(agent_id: str) -> bool:
    return agent_id.startswith("agent:") and agent_id not in _OUR_AGENTS


def _bc_open_partner_offers(chat: list[Message], agent: str, citizen: str) -> int:
    """Offerte di partner proposte al cittadino e non ancora rifiutate (anche multi-riga)."""
    cancelled = {
        fields_of(row.text).get("offer_id", "")
        for row in chat
        if row.sender == citizen and "cancel" in words(row.text)
    }
    count = 0
    for row in chat:
        if row.sender != agent or row.recipient != citizen:
            continue
        for info in _bc_offer_lines(row.text):
            if info.get("offer_id") not in cancelled and _bc_is_partner_id(info.get("facility", "")):
                count += 1
    return count


def _bc_offer_pending(
    chat: list[Message], agent: str, citizen: str, sent_offers: set[str]
) -> bool:
    """True se c'e' gia' un'offerta di slot attiva proposta al cittadino e non ancora confermata/annullata."""
    real_sent = {s for s in sent_offers if not s.startswith("wl-")}
    if real_sent and not any(row.sender == citizen and "cancel" in words(row.text) for row in chat):
        return True
    for row in reversed(chat):
        if row.sender == citizen:
            return False
        if row.sender == agent and row.recipient == citizen:
            lines = _bc_offer_lines(row.text)
            if any(info.get("slot_id") for info in lines):
                return True
    return False


async def _bc_external_offer(city: City, message: Message, chat: list[Message]) -> None:
    """Inoltra offerte effettive di un partner interrogato, senza inventare attributi."""
    if not _bc_is_partner(city, message.sender):
        return
    citizen = _citizen(chat)
    if not citizen:
        return
    sent = _sent(message.need_id or message.msg_id)
    # A cancellation clears the batch displayed to the citizen.
    start = 0
    for i, row in enumerate(chat):
        if row.sender in (citizen, city.agent_id) and "cancel" in words(row.text):
            start = i + 1
    active = chat[start:]
    if _bc_offer_pending(active, city.agent_id, citizen, sent.again if start else sent.offers):
        return
    room = _BC_MAX_OPEN_PARTNER_OFFERS - _bc_open_partner_offers(active, city.agent_id, citizen)
    lines = []
    rejected = _rejected_doctors(chat, city.agent_id)
    withdrawn = _withdrawn(chat, city.agent_id)
    for info in _bc_offer_lines(message.text):
        if info.get("doctor_id") in rejected or info.get("slot_id") in withdrawn:
            continue
        line = _bc_partner_offer_line(city, message, chat, info)
        if line is not None:
            lines.append(line)
    days = _days_pref(chat)
    window = _time_pref(chat)

    def comfort(item: tuple[datetime, str, str]) -> tuple[int, int]:
        offer = fields_of(item[2].splitlines()[0])
        return _comfort_miss(offer, days, window)

    if window is not None and window[2]:
        lines = [item for item in lines if comfort(item)[0] == 0]
    lines.sort(key=lambda item: (comfort(item), item[0]))
    for _, slot_id, text in lines[: max(room, 0)]:
        if await _say(city, citizen, text):
            sent.offers.add(slot_id)


def _bc_partner_offer_line(
    city: City, message: Message, chat: list[Message], info: dict[str, str]
) -> tuple[datetime, str, str] | None:
    """Una riga ``offer`` di un partner, validata; ``None`` se va scartata."""
    procedure = info.get("procedure", "")
    offer_id = info.get("offer_id", "")
    slot_id = info.get("slot_id", "")
    doctor_id = info.get("doctor_id", "")
    facility = info.get("facility") or message.sender

    if not _bc_asked_partner(chat, city.agent_id, message.sender, procedure):
        _LOG.info("care.partner.discarded", extra={"reason": "partner_not_asked", "partner": message.sender, "procedure": procedure})
        return None
    rx = _rx_reply(chat)
    agenda = _ledger(chat, "agenda")
    citizen = _citizen(chat)
    now = _when_loose((agenda or {}).get("agenda_now", ""))
    at_text = info.get("agenda_date") or info.get("at", "")
    at = _when_loose(at_text)
    if not rx or not citizen or not now or not at:
        _LOG.info("care.partner.discarded", extra={"reason": "missing_context", "offer_id": offer_id, "has_rx": bool(rx), "has_citizen": bool(citizen), "has_now": bool(now), "has_at": bool(at)})
        return None
    if rx.get("procedure") != procedure or rx.get("state", "issued") != "issued":
        _LOG.info("care.partner.discarded", extra={"reason": "rx_mismatch_or_state", "offer_id": offer_id, "rx_proc": rx.get("procedure"), "offer_proc": procedure, "rx_state": rx.get("state")})
        return None
    if info.get("facility") and info["facility"] != message.sender:
        _LOG.info("care.partner.discarded", extra={"reason": "facility_mismatch", "offer_id": offer_id, "facility": info.get("facility"), "sender": message.sender})
        return None
    if not offer_id or not slot_id or not doctor_id:
        _LOG.info("care.partner.discarded", extra={"reason": "missing_required_fields", "offer_id": offer_id, "slot_id": slot_id, "doctor_id": doctor_id})
        return None
    # agenda_day ed expiry sono facoltativi nel formato comune: si ricavano dalla data.
    day = info.get("agenda_day") or _WEEKDAYS[at.weekday()]
    expiry_text = info.get("expiry") or at_text

    priority = rx.get("priority", "")
    lead = timedelta(minutes=15) if priority == "U" else _LEAD
    if at <= now + lead:
        _LOG.info("care.partner.discarded", extra={"reason": "slot_in_lead_or_past", "offer_id": offer_id, "at": at_text, "now": now.isoformat()})
        return None
    if priority == "U" and at.date() != now.date():
        _LOG.info("care.partner.discarded", extra={"reason": "priority_u_not_today", "offer_id": offer_id, "at": at_text, "now": now.isoformat()})
        return None
    if priority in _DEADLINE_DAYS and at >= now + timedelta(days=_DEADLINE_DAYS[priority]):
        _LOG.info("care.partner.discarded", extra={"reason": "priority_deadline_exceeded", "offer_id": offer_id, "at": at_text, "priority": priority})
        return None
    if _is_urgent_reserve(info, at) and priority != "U":
        _LOG.info("care.partner.discarded", extra={"reason": "urgent_reserve_non_u", "offer_id": offer_id, "slot_id": slot_id})
        return None
    if info.get("free", "1") == "0":
        _LOG.info("care.partner.discarded", extra={"reason": "slot_not_free", "offer_id": offer_id})
        return None
    expiry = _when_loose(expiry_text)
    if expiry is None or expiry <= now:
        _LOG.info("care.partner.discarded", extra={"reason": "offer_expired", "offer_id": offer_id, "expiry": expiry_text})
        return None
    # MACH-1.2: offerta "rilassata" (rompe un vincolo). La carrozzina non si rilassa.
    relaxed = info.get("relaxed", "").lower()
    if relaxed == "wheelchair":
        _LOG.info("care.partner.discarded", extra={"reason": "wheelchair_cannot_relax", "offer_id": offer_id})
        return None

    # Mai inoltrare un medico che non parla la lingua del cittadino (o del genere sbagliato):
    # il cittadino lo rifiuterebbe e il percorso resterebbe aperto. doctor_lang del partner
    # serve solo come indizio per le lingue rare e non viene mai scritto al cittadino.
    if not _bc_fits_citizen(chat, doctor_id, info.get("doctor_lang", "")):
        _LOG.info("care.partner.discarded", extra={"reason": "doctor_not_fitting", "offer_id": offer_id, "doctor_id": doctor_id, "lang": _lang(chat)})
        return None

    if _our_offer(chat, city.agent_id, offer_id):
        _LOG.info("care.partner.discarded", extra={"reason": "offer_already_made", "offer_id": offer_id})
        return None
    if _we(chat, city.agent_id, f"slot_id={slot_id}", to=citizen, word="offer"):
        _LOG.info("care.partner.discarded", extra={"reason": "slot_already_offered", "slot_id": slot_id})
        return None

    fields = [
        "offer", f"offer_id={offer_id}", f"slot_id={slot_id}",
        f"doctor_id={doctor_id}", f"facility={facility}",
        f"procedure={procedure}", f"agenda_date={at.isoformat()}",
        f"agenda_day={day}", f"expiry={expiry.isoformat()}",
        f"priority={priority}",
    ]
    if _is_urgent_reserve(info, at):
        fields.append("urgent_reserve=true")
    number = _number(chat)
    if number:
        fields.append(f"prescription_number={number}")
    act = " ".join(fields)
    lang = _lang(chat)
    courtesy = _bc_offer_courtesy(
        lang != "it",
        procedure,
        day,
        at_text,
    )
    if relaxed:
        courtesy = f"{courtesy} {_bc_relaxed_note(lang != 'it', relaxed)}".strip()
    full_text = f"{act}\n{courtesy}" if courtesy else act
    return at, info["slot_id"], full_text


def _bc_relaxed_note(english: bool, relaxed: str) -> str:
    """Cosa cambia rispetto alle preferenze, in parole semplici (relaxed= non va al cittadino)."""
    if relaxed == "days":
        what_it, what_en = "in un giorno diverso da quelli indicati", "on a different day from the ones you mentioned"
    elif relaxed in {"after", "before", "time"}:
        what_it, what_en = "in un orario diverso da quello indicato", "at a different time from the one you mentioned"
    else:
        what_it, what_en = "con una delle sue preferenze non rispettata", "with one of your preferences not met"
    if english:
        return f"With all your preferences there is no free time: this is the closest one, {what_en}. Is it fine for you?"
    return f"Con tutte le sue preferenze non c'è posto: questo è il più vicino, {what_it}. Va bene lo stesso?"


# --- strutture: risposta alle richieste dei partner (formato comune tra ASST) ---------------
# need.request da un centro prenotazioni di un'altra ASST -> UN solo messaggio, una riga offer
# per slot libero nostro (dal Ledger), oppure cancel reason=... request_id=...

_FAC_PROCEDURES = {
    "hospital": ("VIS-FIS",),
    "specialist_care": ("VIS-CARD", "CTRL-CARD"),
    "family_counseling": ("CONS-GIN",),
}
_FAC_WHEELCHAIR = {"hospital"}  # dal registry della console: solo l'ospedale è accessibile
_FAC_MAX_LINES = 5
_FAC_ACK = "Ricevuto, verifico la disponibilità."


def _fac_doctor_lang(doctor_id: str) -> str:
    spec = _DOCTORS.get(doctor_id.rsplit("-", 1)[-1])
    if spec is None:
        return ""
    rare = _RARE_BY_ASST.get(_asst_of(doctor_id), "")
    langs = [rare if lang == "*" else lang for lang in sorted(spec[0], key=lambda x: (x != "it", x))]
    return "/".join(lang for lang in langs if lang)


def _fac_partner_request(message: Message) -> bool:
    return (
        message.sender.startswith("agent:")
        and message.sender not in _OUR_AGENTS
        and first_word(message.text) == "need.request"
    )


async def _fac_answer_partner(city: City, message: Message, chat: list[Message], role: str) -> None:
    """Risponde in un solo messaggio: righe offer con i nostri slot liberi, o cancel."""
    partner = message.sender
    info = fields_of(message.text)
    request_id = info.get("request_id") or message.need_id or message.msg_id
    tag = f"request_id={request_id}"
    if _we(chat, city.agent_id, tag, to=partner, word="offer") or _we(
        chat, city.agent_id, tag, to=partner, word="cancel"
    ):
        # MACH-1.2 idempotenza: need.request ripetuta -> la stessa risposta, nessuno slot nuovo.
        previous = [
            row for row in chat
            if row.sender == city.agent_id and row.recipient == partner
            and tag in row.text.split() and ("offer" in words(row.text) or "cancel" in words(row.text))
        ]
        resent = sum(1 for row in chat if row.sender == partner and tag in row.text.split()
                     and first_word(row.text) == "need.request")
        if previous and resent > len(previous):
            await _say(city, partner, previous[-1].text)
        return
    procedure = info.get("procedure", "")
    priority = info.get("priority") or None
    if procedure not in _FAC_PROCEDURES.get(role, ()):
        await _say(city, partner, f"cancel reason=procedure_not_offered {tag}")
        return
    # MACH-1.1: prima risposta entro 10 s; riga senza atto ne' token, ignorata dai parser.
    if not _exact(chat, city.agent_id, _FAC_ACK):
        await _say(city, partner, _FAC_ACK)
    asked = f"world procedure={procedure} facility={city.agent_id} page=1"
    if not _exact(chat, city.agent_id, "agenda"):
        await _say(city, LEDGER, "agenda")
    if not _exact(chat, city.agent_id, asked):
        await _say(city, LEDGER, asked)
    fresh = await _until(
        city, message.need_id, chat,
        lambda rows: _has_agenda(rows) and _latest_slots(rows) is not None,
    )
    agenda = _ledger(fresh, "agenda")
    now = _when("" if agenda is None else agenda.get("agenda_now", ""))
    world = _latest_slots(fresh)
    if now is None or world is None:
        await _say(city, partner, f"cancel reason=facility_unavailable {tag}")
        return
    lang = info.get("lang") or "it"  # senza lingua richiesta: tutti i medici parlano italiano
    gender = info.get("gender_preference") or info.get("gender")
    if info.get("wheelchair", "").lower() in {"true", "1", "yes"} and role not in _FAC_WHEELCHAIR:
        # Struttura non accessibile: la carrozzina non si rilassa.
        await _say(city, partner, f"cancel reason=no_availability {tag}")
        return
    candidates = fitting(
        [line for line in world.splitlines() if "slot_id=" in line],
        now=now,
        priority=priority,
        lang=lang,
        gender=gender if gender in {"m", "f"} else None,
        facility=city.agent_id,
        procedure=procedure,
    )
    deadline = _when_loose(info.get("deadline", ""))
    if deadline is not None:
        candidates = [s for s in candidates if (_when(s.get("at", "")) or deadline) <= deadline]
    breaks = {s["slot_id"]: _fac_breaks(s, info) for s in candidates}
    slots = [s for s in candidates if not breaks[s["slot_id"]]][:_FAC_MAX_LINES]
    head: list[str] = []
    if not slots:
        # MACH-1.2: nessuno slot con tutti i vincoli -> cancel e sotto fino a 3 offerte che ne
        # rompono uno solo, marcate relaxed=. Decide il centro prenotazioni.
        slots = [s for s in candidates if len(breaks[s["slot_id"]]) == 1][:3]
        if not slots:
            await _say(city, partner, f"cancel reason=no_availability {tag}")
            return
        head = [f"cancel reason=no_availability {tag}"]
    rows = list(head)
    for slot in slots:
        doctor = slot["doctor_id"]
        spec = _DOCTORS.get(doctor.rsplit("-", 1)[-1])
        parts = [
            "offer", f"offer_id=of-{slot['slot_id']}", f"slot_id={slot['slot_id']}",
            f"doctor_id={doctor}", f"facility={city.agent_id}", f"procedure={procedure}",
            f"agenda_date={slot['at']}", f"agenda_day={slot.get('day', '')}",
            f"expiry={slot['at']}",
        ]
        doctor_lang = _fac_doctor_lang(doctor)
        if doctor_lang:
            parts.append(f"doctor_lang={doctor_lang}")
        if spec is not None:
            parts.append(f"doctor_gender={spec[1]}")
        if priority:
            parts.append(f"priority={priority}")
        if _is_urgent_reserve(slot, _when(slot.get("at", ""))):
            parts.append("urgent_reserve=true reserve=U")
        parts.append(tag)
        if role in _FAC_WHEELCHAIR:
            parts.append("wheelchair=true")
        if breaks.get(slot["slot_id"]):
            parts.append(f"relaxed={breaks[slot['slot_id']][0]}")
        rows.append(" ".join(parts))
    if head:
        rows.append("Con tutti i vincoli non c'è posto, questi sono i più vicini")
    while len(rows) > 1 and len("\n".join(rows)) > 1900:  # limite di 2000 caratteri per messaggio
        rows.pop()
    await _say(city, partner, "\n".join(rows))


def _fac_breaks(slot: dict[str, str], info: dict[str, str]) -> list[str]:
    """Vincoli MACH-1.2 (days, after, before) che lo slot non rispetta."""
    at = _when(slot.get("at", ""))
    out: list[str] = []
    days = {part.strip().lower()[:3] for part in re.split(r"[/,;|]", info.get("days", "")) if part.strip()}
    day = (slot.get("day") or "").lower()
    if not day and at is not None:
        day = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[at.weekday()]
    if days and day and day not in days:
        out.append("days")
    after = _when_loose(info.get("after", ""))
    before = _when_loose(info.get("before", ""))
    if at is not None and after is not None and at < after:
        out.append("after")
    if at is not None and before is not None and at > before:
        out.append("before")
    return out


# --- facility ----------------------------------------------------------------------------------


def _facility_turn(message: Message) -> bool:
    head = first_word(message.text)
    kind = words(message.text)
    if message.sender == LEDGER and head in {"held", "committed", "checked_in", "reported"}:
        return True
    if message.sender == LEDGER and head == "refused" and _refuses(message.text, "hold"):
        return True
    if "checkin" in kind and message.sender.startswith("citizen:"):
        return True
    return "confirm" in kind and (
        message.sender.startswith("agent:") or message.sender.startswith("citizen:")
    )


async def _family(city: City, message: Message, chat: list[Message]) -> None:
    if _facility_turn(message):
        await _facility(city, message, chat, reports=False, hold_self=True)
        return
    await _desk(city, message, chat, hold_self=True, fixed="CONS-GIN")


async def _facility(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    reports: bool,
    hold_self: bool,
) -> None:
    head = first_word(message.text)
    kind = words(message.text)
    if message.sender == LEDGER and head == "held":
        await _on_held(city, message, chat)
    elif message.sender == LEDGER and head == "committed":
        await _on_committed(city, message, chat)
    elif message.sender == LEDGER and head == "refused" and _refuses(message.text, "hold"):
        await _on_refused(city, message, chat, hold_self=hold_self)
    elif message.sender == LEDGER and head == "checked_in":
        await _on_checked_in(city, message, chat, reports=reports)
    elif message.sender == LEDGER and head == "reported":
        await _on_reported(city, message, chat)
    elif "checkin" in kind and message.sender.startswith("citizen:"):
        await _on_checkin(city, message, chat, reports=reports)
    elif "confirm" in kind:
        await _on_confirm(city, message, chat, hold_self=hold_self)


def _booking_of(chat: list[Message], agent: str, slot_id: str) -> tuple[str, str, str | None]:
    """Who asked us to book this slot, the consent, and the prescription, if we have them."""

    token = f"slot_id={slot_id}"
    sender = ""
    consent = ""
    number: str | None = None
    for row in chat:
        if token not in row.text.split():
            continue
        fields = fields_of(row.text)
        if "confirm" in words(row.text) and row.sender not in (LEDGER, agent):
            sender = row.sender
            consent = fields.get("consent_ref") or fields.get("cross_asst_consent_ref") or consent
            number = fields.get("prescription_number") or number
        if row.sender == agent and "hold" in words(row.text):
            consent = fields.get("consent_ref", consent)
            number = fields.get("prescription_number") or number
            sender = sender or fields.get("citizen", "")
    return sender, consent, number


def _hold_slot(chat: list[Message], agent: str, hold_id: str) -> str:
    token = f"hold_id={hold_id}"
    for row in chat:
        if token in row.text.split() and (row.sender == agent or row.sender == LEDGER):
            slot = fields_of(row.text).get("slot_id", "")
            if slot:
                return slot
    return ""


def _our_commit(chat: list[Message], agent: str, hold_id: str) -> bool:
    """A commit we actually sent. The ledger's held line also contains the word commit."""

    token = f"hold_id={hold_id}"
    for row in chat:
        if row.sender != agent or row.recipient != LEDGER:
            continue
        if row.text.strip().startswith("commit ") and token in row.text.split():
            return True
    return False


def _latest_hold(chat: list[Message], agent: str) -> tuple[str, str | None]:
    consent = ""
    number: str | None = None
    for row in chat:
        if row.sender != agent or not row.text.strip().startswith("hold "):
            continue
        fields = fields_of(row.text)
        consent = fields.get("consent_ref", consent)
        number = fields.get("prescription_number") or number
    return consent, number


async def _finish_booking(
    city: City, message: Message, chat: list[Message], slot: str, *, hold_self: bool
) -> None:
    """Hold is in the mailbox. Read the held line and commit before this turn ends."""

    found = await _await_line(city, message.need_id, chat, "held", f"slot_id={slot}", op="hold")
    if found is None:
        _LOG.info("care.held.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = found
    if first_word(reply.text) == "refused":
        await _on_refused(city, reply, fresh, hold_self=hold_self)
        return
    await _on_held(city, reply, fresh)
    hold_id = fields_of(reply.text).get("hold_id", "")
    if not hold_id:
        return
    done = await _await_line(
        city, message.need_id, fresh, "committed", f"booking_id={hold_id}", op="commit"
    )
    if done is None:
        _LOG.info("care.commit.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = done
    if first_word(reply.text) == "committed":
        await _on_committed(city, reply, fresh)
        return
    if first_word(reply.text) == "refused":
        await _on_refused(city, reply, fresh, hold_self=hold_self)


async def _on_held(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    hold_id = fields.get("hold_id", "")
    slot = fields.get("slot_id", "")
    if not hold_id or not slot:
        _LOG.info("care.held.skip", extra={"head": first_word(message.text)[:40]})
        return
    if _our_commit(chat, city.agent_id, hold_id):
        return
    _, consent, number = _booking_of(chat, city.agent_id, slot)
    if not consent:
        consent, held_number = _latest_hold(chat, city.agent_id)
        number = number or held_number
    need = fields.get("need_id", "")
    if not consent and need.startswith("N-"):
        # The citizen's booking consent is cons-<need_id>-booking. The held line does not
        # echo the real ref, and the hold that carried it may not be in this page of history.
        consent = f"cons-{need}-booking"
    if not consent:
        _LOG.info("care.held.noconsent", extra={"need_id": need[:48]})
        return
    parts = [
        "commit",
        f"hold_id={hold_id}",
        f"consent_ref={consent}",
        f"fence={fields.get('fence', '1')}",
    ]
    if number:
        parts.append(f"prescription_number={number}")
    await _say(city, LEDGER, " ".join(parts))


async def _on_committed(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    booking_id = fields.get("booking_id", "")
    ledger_ref = fields.get("ledger_ref", "")
    if not booking_id or not ledger_ref:
        return
    slot = fields.get("slot_id") or _hold_slot(chat, city.agent_id, fields.get("hold_id", ""))
    sender, _, _ = _booking_of(chat, city.agent_id, slot) if slot else ("", "", None)
    target = sender or _citizen(chat)
    if not target:
        return
    token = f"booking_id={booking_id}"
    if _we(chat, city.agent_id, token, to=target, word="booking.receipt"):
        return
    extra = _fac_receipt_extra(chat, city.agent_id, slot, target) if slot else ""
    await _say(city, target, f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref}{extra}")


async def _on_refused(
    city: City, message: Message, chat: list[Message], *, hold_self: bool
) -> None:
    agent = city.agent_id
    slot = ""
    for row in chat:
        if row.sender == agent and row.recipient == LEDGER and "hold" in words(row.text):
            slot = fields_of(row.text).get("slot_id", slot)
    if not slot:
        return
    target, _, _ = _booking_of(chat, agent, slot)
    offer_id = ""
    for row in chat:
        if f"slot_id={slot}" in row.text.split():
            offer_id = fields_of(row.text).get("offer_id", offer_id)
    if (
        target
        and offer_id
        and not _we(chat, agent, f"offer_id={offer_id}", to=target, word="cancel")
    ):
        tag = ""
        if target.startswith("agent:") and target not in _OUR_AGENTS:
            rid = _fac_receipt_extra(chat, agent, slot, target)
            rid_fields = fields_of(rid)
            tag = f" request_id={rid_fields['request_id']}" if rid_fields.get("request_id") else ""
        await _say(city, target, f"cancel offer_id={offer_id} reason={_cancel_reason(chat)}{tag}")
    if hold_self:
        await _advance(city, message, chat, fixed="CONS-GIN", replacing=True)


async def _on_checkin(city: City, message: Message, chat: list[Message], *, reports: bool) -> None:
    booking_id = fields_of(message.text).get("booking_id", "")
    citizen = message.sender if message.sender.startswith("citizen:") else _citizen(chat)
    if not booking_id or not citizen:
        return
    token = f"booking_id={booking_id}"
    if not _we(chat, city.agent_id, token, to=LEDGER, word="checkin"):
        if not await _say(city, LEDGER, f"checkin booking_id={booking_id} citizen={citizen}"):
            return
    found = await _await_line(city, message.need_id, chat, "checked_in", token, op="checkin")
    if found is None:
        _LOG.info("care.checkin.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = found
    if first_word(reply.text) == "checked_in":
        await _on_checked_in(city, reply, fresh, reports=reports)


async def _on_checked_in(
    city: City, message: Message, chat: list[Message], *, reports: bool
) -> None:
    booking_id = fields_of(message.text).get("booking_id", "")
    citizen = _citizen(chat)
    if not booking_id:
        return
    token = f"booking_id={booking_id}"
    if reports and not _we(chat, city.agent_id, token, to=LEDGER, word="report"):
        await _say(city, LEDGER, f"report booking_id={booking_id} outcome_code=VISIT")
    if citizen and not _we(chat, city.agent_id, token, to=citizen, word="checkin"):
        await _say(city, citizen, f"checkin booking_id={booking_id}")
    if not reports:
        return
    found = await _await_line(city, message.need_id, chat, "reported", token, op="report")
    if found is None:
        _LOG.info("care.report.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = found
    if first_word(reply.text) == "reported":
        await _on_reported(city, reply, fresh)


async def _on_reported(city: City, message: Message, chat: list[Message]) -> None:
    booking_id = fields_of(message.text).get("booking_id", "")
    citizen = _citizen(chat)
    if not booking_id or not citizen:
        return
    token = f"booking_id={booking_id}"
    if _we(chat, city.agent_id, token, to=citizen, word="report"):
        return
    await _say(city, citizen, f"report booking_id={booking_id} outcome_code=VISIT")
