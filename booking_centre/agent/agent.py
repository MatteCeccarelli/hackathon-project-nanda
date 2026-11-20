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
        if hit is not None:
            return hit, fresh
        for row in fresh:
            if row.msg_id in known or row.sender != LEDGER:
                continue
            if first_word(row.text) == "refused" and op in words(row.text):
                return row, fresh
        if attempt + 1 < _PULL_TRIES:
            await asyncio.sleep(_PULL_PAUSE)
    return None


def _has_agenda(chat: list[Message]) -> bool:
    agenda = _ledger(chat, "agenda")
    return _when("" if agenda is None else agenda.get("agenda_now", "")) is not None


def _has_procedure(chat: list[Message]) -> bool:
    reply = _ledger(chat, "prescription")
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
}


def _asst_of(doctor_id: str) -> str:
    """``asst-nord-h3`` -> ``nord``; stringa vuota se il formato non è riconosciuto."""
    parts = doctor_id.split("-")
    return "-".join(parts[1:-1]) if len(parts) >= 3 and parts[0] == "asst" else ""


def _speaks(doctor_id: str, lang: str, hint: str = "") -> bool:
    spec = _DOCTORS.get(doctor_id.rsplit("-", 1)[-1])
    if spec is None:
        return False
    langs = spec[0]
    if lang in langs:
        return True
    if "*" not in langs or lang in {"it", "en"}:
        return False
    # Lingua rara: vale solo se è quella dell'ASST del medico. Se l'ASST non è in tabella,
    # ci si appoggia all'indicazione del partner (doctor_lang), che non viene mai inoltrata.
    rare = _RARE_BY_ASST.get(_asst_of(doctor_id))
    if rare is not None:
        return rare == lang
    return lang in hint.replace(",", "/").split("/")


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
    elif priority == "P":
        horizon = None
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
        reserve = slot.get("urgent_reserve", "").lower() == "true"
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
    return "it"


def _gender(chat: list[Message]) -> str | None:
    for row in chat:
        if not row.sender.startswith("citizen:"):
            continue
        raw = fields_of(row.text).get("gender_preference") or fields_of(row.text).get("gender")
        if raw in {"m", "f"}:
            return raw
    return None


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

    __slots__ = ("offers", "trigger", "again")

    def __init__(self) -> None:
        self.offers: set[str] = set()
        self.trigger = ""
        self.again: set[str] = set()


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
        if message.sender.startswith("agent:") and _bc_offer_lines(message.text):
            await _bc_external_offer(city, message, chat)
            return
        if (
            message.sender.startswith("agent:")
            and "cancel" in words(message.text)
            and not fields_of(message.text).get("offer_id")
            and fields_of(message.text).get("request_id")
        ):
            # cancel reason=no_availability request_id=...: il partner non ha posto.
            # Nessuna offerta da ritirare: non si riapre la ricerca locale.
            return
        if message.sender.startswith("citizen:"):
            if first_word(message.text) == "confirm":
                if await _bc_external_confirm(city, message, chat):
                    return
            elif await _bc_cross_reply(city, message, chat):
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
        await _advance(city, message, chat, fixed=fixed, replacing=is_cancel)


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
    reply = _ledger(chat, "prescription")
    priority = None if reply is None else reply.get("priority")
    procedure = fixed or (None if reply is None else reply.get("procedure"))
    number = _number(chat)
    agenda = _ledger(chat, "agenda")
    now = _when("" if agenda is None else agenda.get("agenda_now", ""))
    world = _latest_slots(chat)
    # Ask, then read the reply from history: the push of a ledger message does not open a turn.
    for _pass in range(3):
        reply = _ledger(chat, "prescription")
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
                    if lang == "en"
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
        if fixed is None and state not in (None, "issued"):
            return
        agenda = _ledger(chat, "agenda")
        now = _when("" if agenda is None else agenda.get("agenda_now", ""))
        world = _latest_slots(chat)
        if now is not None and world is not None:
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
    if now is None or world is None:
        return
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
        if slot.get("slot_id") not in taken
    ]
    if chosen:
        lang = _lang(chat)
        slots_to_offer = chosen[:3]
        act_lines = [_offer_line(s, number, priority) for s in slots_to_offer]
        courtesy = _bc_offers_courtesy(lang == "en", procedure, slots_to_offer)
        full_text = "\n".join(act_lines) + (f"\n{courtesy}" if courtesy else "")
        if await _say(city, citizen, full_text):
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
        await _bc_discover(city, message, chat, key, procedure, priority)


async def _on_confirm(
    city: City, message: Message, chat: list[Message], *, hold_self: bool
) -> None:
    agent = city.agent_id
    fields = fields_of(message.text)
    offer_id = fields.get("offer_id", "")
    consent = fields.get("consent_ref", "")
    offer = _our_offer(chat, agent, offer_id) if offer_id else {}
    slot = fields.get("slot_id") or offer.get("slot_id", "")
    facility = fields.get("facility") or offer.get("facility", "")
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
    await _say(city, citizen, f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref}")


async def _partner_cancel(
    city: City, message: Message, chat: list[Message], *, fixed: str | None
) -> None:
    fields = fields_of(message.text)
    offer_id = fields.get("offer_id", "")
    citizen = _citizen(chat)
    if (
        offer_id
        and citizen
        and not _we(chat, city.agent_id, f"offer_id={offer_id}", to=citizen, word="cancel")
    ):
        await _say(city, citizen, f"cancel offer_id={offer_id} reason=facility_cancelled")
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


async def _bc_discover(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
    procedure: str,
    priority: str | None,
) -> None:
    """Fallback dopo l'esaurimento del catalogo locale, senza trasmettere dati del cittadino."""
    service = _BC_SERVICE_TYPES.get(procedure)
    if not service or priority in (None, "P") or not _number(chat):
        return
    # Non aprire nuove ricerche se abbiamo gia' proposto uno slot valido.
    if _offered(chat, city.agent_id) or _sent(key).offers:
        return
    try:
        peers = await city.find(service)
    except CapError:
        return

    # Criteri di ricerca (non dati personali): lingua e genere preferiti aiutano il partner
    # a proporre il medico giusto invece di slot che il cittadino rifiuterebbe.
    criteria = ""
    lang = _lang(chat)
    if lang and lang != "it":
        criteria += f" lang={lang}"
    gender = _gender(chat)
    if gender:
        criteria += f" gender_preference={gender}"

    contacted = 0
    for peer in peers:
        peer_id = getattr(peer, "agent_id", "")
        if not _bc_is_partner(city, peer_id):
            continue
        capabilities = getattr(peer, "capabilities", ())
        if capabilities and service not in capabilities:
            continue
        if _bc_asked_partner(chat, city.agent_id, peer_id, procedure):
            continue
        request = (
            f"need.request service_type={service} procedure={procedure} "
            f"priority={priority} request_id={key}{criteria}"
        )
        if await _say(city, peer_id, request):
            contacted += 1
        if contacted >= 12:
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
    parts = [
        "confirm", f"offer_id={offer_id}", f"slot_id={slot}",
        f"facility={facility}", f"citizen={citizen}",
        f"consent_ref={consent}",
    ]
    number = offer.get("prescription_number") or _number(chat)
    if number:
        parts.append(f"prescription_number={number}")
    await _say(city, facility, " ".join(parts))


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
    """True se c'e' gia' un'offerta attiva proposta al cittadino e non ancora confermata/annullata."""
    if sent_offers and not any(row.sender == citizen and "cancel" in words(row.text) for row in chat):
        return True
    for row in reversed(chat):
        if row.sender == citizen:
            return False
        if row.sender == agent and row.recipient == citizen and "offer" in words(row.text):
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
    if _bc_offer_pending(chat, city.agent_id, citizen, sent.offers):
        return
    room = _BC_MAX_OPEN_PARTNER_OFFERS - _bc_open_partner_offers(chat, city.agent_id, citizen)
    lines = []
    for info in _bc_offer_lines(message.text):
        line = _bc_partner_offer_line(city, message, chat, info)
        if line is not None:
            lines.append(line)
    lines.sort(key=lambda item: item[0])
    for _, slot_id, text in lines[: max(room, 0)]:
        if await _say(city, citizen, text):
            sent.offers.add(slot_id)


def _bc_partner_offer_line(
    city: City, message: Message, chat: list[Message], info: dict[str, str]
) -> tuple[datetime, str, str] | None:
    """Una riga ``offer`` di un partner, validata; ``None`` se va scartata."""
    procedure = info.get("procedure", "")
    if not _bc_asked_partner(chat, city.agent_id, message.sender, procedure):
        return None
    rx = _ledger(chat, "prescription")
    agenda = _ledger(chat, "agenda")
    citizen = _citizen(chat)
    now = _when((agenda or {}).get("agenda_now", ""))
    at_text = info.get("agenda_date") or info.get("at", "")
    at = _when(at_text)
    if not rx or not citizen or not now or not at:
        return None
    if rx.get("procedure") != procedure or rx.get("state", "issued") != "issued":
        return None
    if info.get("facility") != message.sender:
        return None
    if any(not info.get(field) for field in ("offer_id", "slot_id", "doctor_id")):
        return None
    # agenda_day ed expiry sono facoltativi nel formato comune: si ricavano dalla data.
    day = info.get("agenda_day") or _WEEKDAYS[at.weekday()]
    expiry_text = info.get("expiry") or at_text

    priority = rx.get("priority", "")
    if at <= now + _LEAD:
        return None
    if priority == "U" and at.date() != now.date():
        return None
    if priority in _DEADLINE_DAYS and at >= now + timedelta(days=_DEADLINE_DAYS[priority]):
        return None
    if info.get("urgent_reserve", "").lower() == "true" and priority != "U":
        return None
    if info.get("free", "1") == "0":
        return None
    expiry = _when(expiry_text)
    if expiry is None or expiry <= now:
        return None

    # Mai inoltrare un medico che non parla la lingua del cittadino (o del genere sbagliato):
    # il cittadino lo rifiuterebbe e il percorso resterebbe aperto. doctor_lang del partner
    # serve solo come indizio per le lingue rare e non viene mai scritto al cittadino.
    if not _bc_fits_citizen(chat, info["doctor_id"], info.get("doctor_lang", "")):
        return None

    offer_id = info["offer_id"]
    if _our_offer(chat, city.agent_id, offer_id):
        return None
    if _we(chat, city.agent_id, f"slot_id={info['slot_id']}", to=citizen, word="offer"):
        return None

    fields = [
        "offer", f"offer_id={offer_id}", f"slot_id={info['slot_id']}",
        f"doctor_id={info['doctor_id']}", f"facility={message.sender}",
        f"procedure={procedure}", f"agenda_date={at_text}",
        f"agenda_day={day}", f"expiry={expiry_text}",
        f"priority={priority}",
    ]
    number = _number(chat)
    if number:
        fields.append(f"prescription_number={number}")
    act = " ".join(fields)
    lang = _lang(chat)
    courtesy = _bc_offer_courtesy(
        lang == "en",
        procedure,
        day,
        at_text,
    )
    full_text = f"{act}\n{courtesy}" if courtesy else act
    return at, info["slot_id"], full_text


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
        return
    procedure = info.get("procedure", "")
    priority = info.get("priority") or None
    if procedure not in _FAC_PROCEDURES.get(role, ()):
        await _say(city, partner, f"cancel reason=no_availability {tag}")
        return
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
    slots = fitting(
        [line for line in world.splitlines() if "slot_id=" in line],
        now=now,
        priority=priority,
        lang=lang,
        gender=gender if gender in {"m", "f"} else None,
        facility=city.agent_id,
        procedure=procedure,
    )[:_FAC_MAX_LINES]
    if not slots:
        await _say(city, partner, f"cancel reason=no_availability {tag}")
        return
    rows = []
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
        parts.append(tag)
        if role in _FAC_WHEELCHAIR:
            parts.append("wheelchair=true")
        rows.append(" ".join(parts))
    while len(rows) > 1 and len("\n".join(rows)) > 1900:  # limite di 2000 caratteri per messaggio
        rows.pop()
    await _say(city, partner, "\n".join(rows))


# --- facility ----------------------------------------------------------------------------------


def _facility_turn(message: Message) -> bool:
    head = first_word(message.text)
    kind = words(message.text)
    if message.sender == LEDGER and head in {"held", "committed", "checked_in", "reported"}:
        return True
    if message.sender == LEDGER and head == "refused" and "hold" in kind:
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
    elif message.sender == LEDGER and head == "refused" and "hold" in kind:
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
            consent = fields.get("consent_ref", consent)
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
    await _say(city, target, f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref}")


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
        await _say(city, target, f"cancel offer_id={offer_id} reason=facility_cancelled")
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
