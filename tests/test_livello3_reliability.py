import asyncio
from datetime import datetime, timedelta, timezone
import sys
import os

sys.path.insert(0, ".")
from city_sdk.city import Message, City, LEDGER

import booking_centre.agent.agent as bc


class MockCity:
    def __init__(self, agent_id="agent:geyjmp26tajh"):
        self.agent_id = agent_id
        self.sent_messages: list[tuple[str, str]] = []
        self._history: list[Message] = []
        self._peers: list[object] = []

    async def history(self, need_id=None):
        return list(self._history)

    async def say(self, to: str, text: str):
        self.sent_messages.append((to, text))
        msg = Message(
            type="chat",
            sender=self.agent_id,
            msg_id=f"msg-sent-{len(self.sent_messages)}",
            recipient=to,
            body={"text": text},
            sent_at=datetime.now(timezone.utc).isoformat(),
        )
        self._history.append(msg)
        return True

    async def find(self, service: str):
        return list(self._peers)


class MockPeer:
    def __init__(self, agent_id, capabilities=(), role="facility", district=6):
        self.agent_id = agent_id
        self.capabilities = capabilities
        self.role = role
        self.district = district


# ---------------------------------------------------------------------------------
# TEST 1: VIS-FIS, priorità D, italiano, con centinaia di slot (es. 429 slot su più pagine)
# Il Centro deve paginare sul Ledger, trovare lo slot e proporlo al cittadino (risolve 000207).
# ---------------------------------------------------------------------------------
async def test_1_vis_fis_priority_d_many_slots():
    print("Executing Test 1: VIS-FIS, priorità D, centinaia di slot distribuiti su più pagine...")
    city = MockCity()
    need_id = "N-2026-11-02-890922000207"
    citizen = "citizen:W01QQQ-22-000207"
    now_dt = datetime(2026, 11, 21, 1, 14, 0, tzinfo=timezone.utc)
    now_str = now_dt.isoformat()

    # In history, GP has issued prescription
    rx_num = "prescription_number-N-2026-11-02-890922000207"
    # Page 1 has slots that are either urgent reserve (09:00) or inside lead time (under 4h)
    p1_slots = (
        "slots page=1/5\n"
        f"slot_id=s-p1-1 doctor_id=asst-ortica-h1 facility=agent:h1 procedure=VIS-FIS at={(now_dt + timedelta(hours=1)).isoformat()} urgent_reserve=true\n"
        f"slot_id=s-p1-2 doctor_id=asst-ortica-h2 facility=agent:h1 procedure=VIS-FIS at={(now_dt + timedelta(hours=2)).isoformat()} free=0\n"
    )
    # Page 2 has valid slots for priority D (tomorrow at 14:30)
    slot_valid_at = (now_dt + timedelta(days=2, hours=4)).isoformat()
    p2_slots = (
        "slots page=2/5\n"
        f"slot_id=s-p2-valid doctor_id=asst-ortica-h1 facility=agent:h1 procedure=VIS-FIS at={slot_valid_at} day=wed free=1\n"
    )

    city._history = [
        Message(type="chat", sender=citizen, msg_id="m1", need_id=need_id, body={"text": "ho un forte mal di schiena"}),
        Message(type="chat", sender="agent:ega0tzcy6za8", msg_id="m2", need_id=need_id, recipient=citizen, body={"text": f"prescription prescription_number={rx_num} procedure=VIS-FIS priority=D"}),
        Message(type="chat", sender=LEDGER, msg_id="m3", need_id=need_id, body={"text": f"agenda agenda_now={now_str}"}),
        Message(type="chat", sender=LEDGER, msg_id="m4", need_id=need_id, body={"text": f"prescription prescription_number={rx_num} procedure=VIS-FIS priority=D state=issued"}),
        Message(type="chat", sender=LEDGER, msg_id="m5", need_id=need_id, body={"text": p1_slots}),
    ]

    # Citizen writes to booking centre asking to book
    citizen_msg = Message(
        type="chat",
        sender=citizen,
        msg_id="m6",
        need_id=need_id,
        recipient=city.agent_id,
        body={"text": f"Buongiorno, vorrei prenotare la visita fisiatrica per il mal di schiena prescription_number={rx_num}"},
        sent_at=now_str,
    )
    city._history.append(citizen_msg)

    # When booking centre asks for page 2, simulate Ledger responding with page 2
    orig_say = city.say
    async def mock_say(to, text):
        res = await orig_say(to, text)
        if to == LEDGER and "page=2" in text:
            msg_p2 = Message(type="chat", sender=LEDGER, msg_id="ledger-p2", need_id=need_id, body={"text": p2_slots})
            city._history.append(msg_p2)
        return res
    city.say = mock_say

    await bc.handle("booking_centre", city, citizen_msg, city._history)

    # Check: Citizen MUST receive an offer for s-p2-valid!
    citizen_offers = [text for to, text in city.sent_messages if to == citizen and "offer" in text]
    assert len(citizen_offers) >= 1, f"Test 1 FAILED: Citizen received NO offer! Sent: {city.sent_messages}"
    assert "s-p2-valid" in citizen_offers[0], f"Test 1 FAILED: Expected slot s-p2-valid, got {citizen_offers[0]}"
    print("  PASS: VIS-FIS priority D successfully paged and offered valid slot!")


# ---------------------------------------------------------------------------------
# TEST 2: VIS-CARD, priorità B, 38 slot validi: niente lista d'attesa prematura
# Partner contattati ed offerta valida inoltrata al cittadino (risolve 000215).
# ---------------------------------------------------------------------------------
async def test_2_vis_card_priority_b_38_slots():
    print("Executing Test 2: VIS-CARD, priorità B, 38 slot validi (no lista d'attesa prematura)...")
    city = MockCity()
    need_id = "N-2026-11-02-890922000215"
    citizen = "citizen:W01QQQ-22-000215"
    now_dt = datetime(2026, 11, 21, 1, 20, 0, tzinfo=timezone.utc)
    now_str = now_dt.isoformat()
    partner = "agent:qtjne8symjas"
    rx_num = "prescription_number-N-2026-11-02-890922000215"

    city._peers = [
        MockPeer(agent_id=partner, capabilities=("specialist_care.cardiology",), role="facility", district=6)
    ]

    city._history = [
        Message(type="chat", sender=citizen, msg_id="m1", need_id=need_id, body={"text": "palpitazioni"}),
        Message(type="chat", sender="agent:ega0tzcy6za8", msg_id="m2", need_id=need_id, recipient=citizen, body={"text": f"prescription prescription_number={rx_num} procedure=VIS-CARD priority=B"}),
        Message(type="chat", sender=LEDGER, msg_id="m3", need_id=need_id, body={"text": f"agenda agenda_now={now_str}"}),
        Message(type="chat", sender=LEDGER, msg_id="m4", need_id=need_id, body={"text": f"prescription prescription_number={rx_num} procedure=VIS-CARD priority=B state=issued"}),
        Message(type="chat", sender=LEDGER, msg_id="m5", need_id=need_id, body={"text": "slots page=1/1\n"}),
    ]

    # Citizen writes
    c_msg = Message(
        type="chat",
        sender=citizen,
        msg_id="m6",
        need_id=need_id,
        recipient=city.agent_id,
        body={"text": f"Vorrei prenotare la visita cardiologica prescription_number={rx_num}"},
        sent_at=now_str,
    )
    city._history.append(c_msg)
    await bc.handle("booking_centre", city, c_msg, city._history)

    # Verify: search note sent, need.request sent to partner, NO waiting list!
    wl_offers = [text for to, text in city.sent_messages if "waiting_list=true" in text]
    assert len(wl_offers) == 0, f"Test 2 FAILED: premature waiting list sent: {wl_offers}"

    # Partner answers with valid slot
    slot_at = (now_dt + timedelta(days=1, hours=2)).isoformat()
    partner_msg = Message(
        type="chat",
        sender=partner,
        msg_id="m7",
        need_id=need_id,
        recipient=city.agent_id,
        body={"text": f"offer offer_id=of-card-38 slot_id=s-card-38 doctor_id=asst-lorenteggio-s1 facility={partner} procedure=VIS-CARD agenda_date={slot_at} free=1"},
        sent_at=(now_dt + timedelta(seconds=5)).isoformat(),
    )
    city._history.append(partner_msg)
    await bc.handle("booking_centre", city, partner_msg, city._history)

    # Citizen must receive the offer
    citizen_offers = [text for to, text in city.sent_messages if to == citizen and "offer_id=of-card-38" in text]
    assert len(citizen_offers) == 1, f"Test 2 FAILED: citizen did not receive partner offer: {city.sent_messages}"
    print("  PASS: VIS-CARD 38 slots forwarded without premature waiting list!")


# ---------------------------------------------------------------------------------
# TEST 3: VIS-CARD, priorità B, 79 slot: messaggio aggiuntivo del cittadino
# Proporre uno slot valido anche dopo un nuovo messaggio del cittadino (risolve 000197).
# ---------------------------------------------------------------------------------
async def test_3_vis_card_priority_b_79_slots_rewrite():
    print("Executing Test 3: VIS-CARD 79 slots, citizen rewrite during pending search...")
    city = MockCity()
    need_id = "N-2026-11-02-890922000197"
    citizen = "citizen:W01QQQ-22-000197"
    now_dt = datetime(2026, 11, 21, 1, 0, 0, tzinfo=timezone.utc)
    now_str = now_dt.isoformat()
    partner = "agent:qtjne8symjas"
    rx_num = "prescription_number-N-2026-11-02-890922000197"

    city._history = [
        Message(type="chat", sender=citizen, msg_id="m1", need_id=need_id, body={"text": "battiti accelerati"}),
        Message(type="chat", sender="agent:ega0tzcy6za8", msg_id="m2", need_id=need_id, recipient=citizen, body={"text": f"prescription prescription_number={rx_num} procedure=VIS-CARD priority=B"}),
        Message(type="chat", sender=LEDGER, msg_id="m3", need_id=need_id, body={"text": f"agenda agenda_now={now_str}"}),
        Message(type="chat", sender=LEDGER, msg_id="m4", need_id=need_id, body={"text": f"prescription prescription_number={rx_num} procedure=VIS-CARD priority=B state=issued"}),
        Message(type="chat", sender=LEDGER, msg_id="m5", need_id=need_id, body={"text": "slots page=1/1\n"}),
        Message(type="chat", sender=city.agent_id, msg_id="m6", need_id=need_id, recipient=partner, body={"text": f"need.request service_type=specialist_care.cardiology procedure=VIS-CARD priority=B request_id={need_id}"}),
        Message(type="chat", sender=city.agent_id, msg_id="m7", need_id=need_id, recipient=citizen, body={"text": bc._BC_SEARCHING_IT}),
    ]

    # Citizen rewrites 20 seconds later
    rewrite_msg = Message(
        type="chat",
        sender=citizen,
        msg_id="m8",
        need_id=need_id,
        recipient=city.agent_id,
        body={"text": "Salve, spero di non finire in lista d'attesa. Ci sono novità?"},
        sent_at=(now_dt + timedelta(seconds=20)).isoformat(),
    )
    city._history.append(rewrite_msg)
    await bc.handle("booking_centre", city, rewrite_msg, city._history)

    # Must NOT have sent waiting list!
    wl = [text for to, text in city.sent_messages if "waiting_list=true" in text]
    assert len(wl) == 0, f"Test 3 FAILED: sent premature waiting list after rewrite: {wl}"

    # Partner sends slot
    slot_at = (now_dt + timedelta(days=1, hours=3)).isoformat()
    partner_msg = Message(
        type="chat",
        sender=partner,
        msg_id="m9",
        need_id=need_id,
        recipient=city.agent_id,
        body={"text": f"offer offer_id=of-card-79 slot_id=s-card-79 doctor_id=asst-lorenteggio-s1 facility={partner} procedure=VIS-CARD agenda_date={slot_at} free=1"},
        sent_at=(now_dt + timedelta(seconds=25)).isoformat(),
    )
    city._history.append(partner_msg)
    await bc.handle("booking_centre", city, partner_msg, city._history)

    citizen_offers = [text for to, text in city.sent_messages if to == citizen and "offer_id=of-card-79" in text]
    assert len(citizen_offers) == 1, f"Test 3 FAILED: citizen did not get offer: {city.sent_messages}"
    print("  PASS: VIS-CARD 79 slots offered after citizen rewrite!")


# ---------------------------------------------------------------------------------
# TEST 4: VIS-FIS, priorità B, inglese, zero slot (effective_size=0)
# Alternativa corretta (waiting list) senza prenotazione fittizia (preserva 0001160004).
# ---------------------------------------------------------------------------------
async def test_4_vis_fis_en_zero_slots_waiting_list():
    print("Executing Test 4: VIS-FIS B English, zero slots -> waiting list alternative...")
    city = MockCity()
    need_id = "N-2026-11-02-8909220001160004"
    citizen = "citizen:W01QQQ-22-0001160004"
    now_dt = datetime(2026, 11, 21, 10, 0, 0, tzinfo=timezone.utc)
    now_str = now_dt.isoformat()
    partner1 = "agent:p1"
    rx_num = "prescription_number-1160004"

    city._history = [
        Message(type="chat", sender=citizen, msg_id="m1", need_id=need_id, body={"text": "I need English physiatry appointment lang=en"}),
        Message(type="chat", sender="agent:ega0tzcy6za8", msg_id="m2", need_id=need_id, recipient=citizen, body={"text": f"prescription prescription_number={rx_num} procedure=VIS-FIS priority=B"}),
        Message(type="chat", sender=LEDGER, msg_id="m3", need_id=need_id, body={"text": f"agenda agenda_now={now_str}"}),
        Message(type="chat", sender=LEDGER, msg_id="m4", need_id=need_id, body={"text": f"prescription prescription_number={rx_num} procedure=VIS-FIS priority=B state=issued"}),
        Message(type="chat", sender=LEDGER, msg_id="m5", need_id=need_id, body={"text": "slots page=1/1\n"}),
        Message(type="chat", sender=city.agent_id, msg_id="m6", need_id=need_id, recipient=partner1, body={"text": f"need.request service_type=hospital.physiatry procedure=VIS-FIS priority=B request_id={need_id}.en lang=en"}),
        Message(type="chat", sender=city.agent_id, msg_id="m7", need_id=need_id, recipient=citizen, body={"text": bc._BC_SEARCHING_EN}),
    ]

    # Partner replies: no availability
    p_cancel = Message(
        type="chat",
        sender=partner1,
        msg_id="m8",
        need_id=need_id,
        recipient=city.agent_id,
        body={"text": f"cancel reason=no_availability request_id={need_id}.en"},
    )
    city._history.append(p_cancel)
    await bc.handle("booking_centre", city, p_cancel, city._history)

    # When all partners answer no availability, citizen gets exactly 1 waiting list offer
    wl = [text for to, text in city.sent_messages if to == citizen and "waiting_list=true" in text]
    assert len(wl) == 1, f"Test 4 FAILED: expected 1 waiting list offer, got {wl}"
    print("  PASS: Zero slots correctly resulted in formal waiting list alternative!")


# ---------------------------------------------------------------------------------
# TEST 5: Due eventi simultanei sullo stesso need_id: nessun avviso duplicato
# ---------------------------------------------------------------------------------
async def test_5_concurrency_no_duplicate_search_note():
    print("Executing Test 5: Simultaneous events on same need_id -> no duplicate search notes...")
    city = MockCity()
    need_id = "N-test-concurrent"
    citizen = "citizen:c-conc"
    now_dt = datetime(2026, 11, 21, 10, 0, 0, tzinfo=timezone.utc)
    now_str = now_dt.isoformat()
    rx_num = "prescription_number-conc"

    city._peers = [
        MockPeer(agent_id="agent:peer1", capabilities=("specialist_care.cardiology",), role="facility")
    ]

    city._history = [
        Message(type="chat", sender=citizen, msg_id="m1", need_id=need_id, body={"text": f"visita cardiologica prescription_number={rx_num}"}),
        Message(type="chat", sender="agent:ega0tzcy6za8", msg_id="m2", need_id=need_id, recipient=citizen, body={"text": f"prescription prescription_number={rx_num} procedure=VIS-CARD priority=B"}),
        Message(type="chat", sender=LEDGER, msg_id="m3", need_id=need_id, body={"text": f"agenda agenda_now={now_str}"}),
        Message(type="chat", sender=LEDGER, msg_id="m4", need_id=need_id, body={"text": f"prescription prescription_number={rx_num} procedure=VIS-CARD priority=B state=issued"}),
        Message(type="chat", sender=LEDGER, msg_id="m5", need_id=need_id, body={"text": "slots page=1/1\n"}),
    ]

    msg_ev1 = Message(type="chat", sender=citizen, msg_id="ev1", need_id=need_id, recipient=city.agent_id, body={"text": "Vorrei info"})
    msg_ev2 = Message(type="chat", sender=citizen, msg_id="ev2", need_id=need_id, recipient=city.agent_id, body={"text": "Ci sono novità?"})

    # Launch both events concurrently
    await asyncio.gather(
        bc.handle("booking_centre", city, msg_ev1, city._history),
        bc.handle("booking_centre", city, msg_ev2, city._history),
    )

    search_notes = [text for to, text in city.sent_messages if to == citizen and text == bc._BC_SEARCHING_IT]
    assert len(search_notes) == 1, f"Test 5 FAILED: search note duplicated! Sent: {len(search_notes)} times"
    print("  PASS: Search notice sent exactly once despite concurrent turns!")


# ---------------------------------------------------------------------------------
# TEST 6: Risposta del Ledger ritardata: nessuna perdita silenziosa del caso
# ---------------------------------------------------------------------------------
async def test_6_ledger_delayed_recovery():
    print("Executing Test 6: Delayed ledger response -> recover from GP prescription in chat...")
    city = MockCity()
    need_id = "N-test-delayed-ledger"
    citizen = "citizen:c-delayed"
    now_dt = datetime(2026, 11, 21, 10, 0, 0, tzinfo=timezone.utc)
    now_str = now_dt.isoformat()
    rx_num = "prescription_number-delayed"

    # In chat, GP has prescribed, but Ledger has NOT replied to prescription yet
    city._history = [
        Message(type="chat", sender=citizen, msg_id="m1", need_id=need_id, body={"text": "mal di schiena"}),
        Message(type="chat", sender="agent:ega0tzcy6za8", msg_id="m2", need_id=need_id, recipient=citizen, body={"text": f"prescription prescription_number={rx_num} procedure=VIS-FIS priority=D"}),
    ]

    citizen_msg = Message(
        type="chat",
        sender=citizen,
        msg_id="m3",
        need_id=need_id,
        recipient=city.agent_id,
        body={"text": "Vorrei prenotare la visita per la schiena"},
        sent_at=now_str,
    )
    city._history.append(citizen_msg)

    # Booking centre should query Ledger for prescription, agenda and world without returning silently
    await bc.handle("booking_centre", city, citizen_msg, city._history)

    ledger_queries = [text for to, text in city.sent_messages if to == LEDGER]
    assert len(ledger_queries) >= 1, f"Test 6 FAILED: No ledger queries sent! Sent: {city.sent_messages}"
    assert any("world procedure=VIS-FIS" in q for q in ledger_queries) or any(f"prescription_number={rx_num}" in q for q in ledger_queries), f"Test 6 FAILED: recovery not initiated: {ledger_queries}"
    print("  PASS: Delayed ledger response handled without silent drop!")


# ---------------------------------------------------------------------------------
# TEST 7: Risposta di una ASST arrivata dopo una precedente ricerca
# L'offerta deve poter essere valutata.
# ---------------------------------------------------------------------------------
async def test_7_late_partner_offer_evaluated():
    print("Executing Test 7: Late partner offer arrived -> correctly evaluated and forwarded...")
    city = MockCity()
    need_id = "N-test-late-offer"
    citizen = "citizen:c-late"
    now_dt = datetime(2026, 11, 21, 10, 0, 0, tzinfo=timezone.utc)
    now_str = now_dt.isoformat()
    partner = "agent:qtjne8symjas"
    rx_num = "prescription_number-late"

    city._history = [
        Message(type="chat", sender=citizen, msg_id="m1", need_id=need_id, body={"text": "visita cardiologica"}),
        Message(type="chat", sender="agent:ega0tzcy6za8", msg_id="m2", need_id=need_id, recipient=citizen, body={"text": f"prescription prescription_number={rx_num} procedure=VIS-CARD priority=B"}),
        Message(type="chat", sender=LEDGER, msg_id="m3", need_id=need_id, body={"text": f"agenda agenda_now={now_str}"}),
        Message(type="chat", sender=LEDGER, msg_id="m4", need_id=need_id, body={"text": f"prescription prescription_number={rx_num} procedure=VIS-CARD priority=B state=issued"}),
        Message(type="chat", sender=city.agent_id, msg_id="m5", need_id=need_id, recipient=partner, body={"text": f"need.request service_type=specialist_care.cardiology procedure=VIS-CARD priority=B request_id={need_id}"}),
        Message(type="chat", sender=city.agent_id, msg_id="m6", need_id=need_id, recipient=citizen, body={"text": bc._BC_SEARCHING_IT}),
    ]

    # Partner arrives late with offer
    slot_at = (now_dt + timedelta(days=1, hours=2)).isoformat()
    offer_msg = Message(
        type="chat",
        sender=partner,
        msg_id="m7",
        need_id=need_id,
        recipient=city.agent_id,
        body={"text": f"offer offer_id=of-late slot_id=s-late doctor_id=asst-lorenteggio-s1 facility={partner} procedure=VIS-CARD agenda_date={slot_at} free=1"},
    )
    city._history.append(offer_msg)
    await bc.handle("booking_centre", city, offer_msg, city._history)

    offers = [text for to, text in city.sent_messages if to == citizen and "offer_id=of-late" in text]
    assert len(offers) == 1, f"Test 7 FAILED: late offer not forwarded to citizen: {city.sent_messages}"
    print("  PASS: Late partner offer successfully forwarded!")


# ---------------------------------------------------------------------------------
# TEST 8: Offerta rifiutata dal cittadino: nuova ricerca quando consentita
# ---------------------------------------------------------------------------------
async def test_8_offer_rejected_new_search():
    print("Executing Test 8: Offer rejected by citizen -> new options or search allowed...")
    city = MockCity()
    need_id = "N-test-rejected"
    citizen = "citizen:c-rej"
    now_dt = datetime(2026, 11, 21, 10, 0, 0, tzinfo=timezone.utc)
    now_str = now_dt.isoformat()
    rx_num = "prescription_number-rej"
    partner = "agent:qtjne8symjas"

    city._history = [
        Message(type="chat", sender=citizen, msg_id="m1", need_id=need_id, body={"text": "visita cardiologica"}),
        Message(type="chat", sender="agent:ega0tzcy6za8", msg_id="m2", need_id=need_id, recipient=citizen, body={"text": f"prescription prescription_number={rx_num} procedure=VIS-CARD priority=B"}),
        Message(type="chat", sender=LEDGER, msg_id="m3", need_id=need_id, body={"text": f"agenda agenda_now={now_str}"}),
        Message(type="chat", sender=LEDGER, msg_id="m4", need_id=need_id, body={"text": f"prescription prescription_number={rx_num} procedure=VIS-CARD priority=B state=issued"}),
        Message(type="chat", sender=city.agent_id, msg_id="m5", need_id=need_id, recipient=citizen, body={"text": "offer offer_id=of-1 slot_id=s-1"}),
    ]

    # Citizen rejects offer
    rej_msg = Message(
        type="chat",
        sender=citizen,
        msg_id="m6",
        need_id=need_id,
        recipient=city.agent_id,
        body={"text": "cancel offer_id=of-1 reason=declined Non posso quel giorno"},
    )
    city._history.append(rej_msg)

    # Active offer check should now be false (not pending)
    assert bc._bc_offer_pending(city._history, city.agent_id, citizen, {"s-1"}) is False
    print("  PASS: Rejected offer no longer blocks further offers!")


# ---------------------------------------------------------------------------------
# TEST 9: Slot annullato dalla struttura: recupero corretto
# ---------------------------------------------------------------------------------
async def test_9_slot_cancelled_by_facility():
    print("Executing Test 9: Slot cancelled by partner facility -> cancellation forwarded to citizen...")
    city = MockCity()
    need_id = "N-test-cancel-fac"
    citizen = "citizen:c-fac"
    partner = "agent:qtjne8symjas"

    city._history = [
        Message(type="chat", sender=citizen, msg_id="m1", need_id=need_id, body={"text": "prenota"}),
        Message(type="chat", sender=city.agent_id, msg_id="m2", need_id=need_id, recipient=citizen, body={
            "text": f"offer offer_id=of-partner-1 slot_id=s-1 facility={partner} procedure=VIS-CARD"
        }),
    ]

    fac_cancel = Message(
        type="chat",
        sender=partner,
        msg_id="m3",
        need_id=need_id,
        recipient=city.agent_id,
        body={"text": "cancel offer_id=of-partner-1 reason=slot_taken"},
    )
    city._history.append(fac_cancel)

    await bc.handle("booking_centre", city, fac_cancel, city._history)

    # Citizen must be notified of cancellation
    c_cancels = [text for to, text in city.sent_messages if to == citizen and "cancel offer_id=of-partner-1" in text]
    assert len(c_cancels) == 1, f"Test 9 FAILED: citizen not notified of cancelled slot: {city.sent_messages}"
    print("  PASS: Partner cancellation correctly relayed to citizen!")


# ---------------------------------------------------------------------------------
# TEST 10: Ricetta scaduta: nessuna prenotazione non valida
# ---------------------------------------------------------------------------------
async def test_10_rx_expired():
    print("Executing Test 10: Expired prescription -> redirect to GP renewal, no booking...")
    city = MockCity()
    need_id = "N-test-expired"
    citizen = "citizen:c-exp"
    now_dt = datetime(2026, 11, 21, 10, 0, 0, tzinfo=timezone.utc)
    now_str = now_dt.isoformat()
    rx_num = "prescription_number-exp"

    city._history = [
        Message(type="chat", sender=citizen, msg_id="m1", need_id=need_id, body={"text": f"visita prescription_number={rx_num}"}),
        Message(type="chat", sender=LEDGER, msg_id="m2", need_id=need_id, body={"text": f"agenda agenda_now={now_str}"}),
        Message(type="chat", sender=LEDGER, msg_id="m3", need_id=need_id, body={"text": f"prescription prescription_number={rx_num} procedure=VIS-CARD priority=B state=expired"}),
    ]

    c_msg = Message(type="chat", sender=citizen, msg_id="m4", need_id=need_id, recipient=city.agent_id, body={"text": f"prescription_number={rx_num}"})
    city._history.append(c_msg)

    await bc.handle("booking_centre", city, c_msg, city._history)

    # Must explain renewal to citizen, NO offers
    offers = [text for to, text in city.sent_messages if "offer" in text]
    assert len(offers) == 0, f"Test 10 FAILED: offer sent on expired prescription! {offers}"
    renewals = [text for to, text in city.sent_messages if to == citizen and ("rinnov" in text.lower() or "scadut" in text.lower() or "medico di base" in text.lower())]
    assert len(renewals) >= 1, f"Test 10 FAILED: citizen not instructed to renew: {city.sent_messages}"
    print("  PASS: Expired prescription redirected to GP!")


# ---------------------------------------------------------------------------------
# TEST 11: Riserva urgente: nessun utilizzo per priorità B o D
# ---------------------------------------------------------------------------------
def test_11_urgent_reserve_not_used_for_b_or_d():
    print("Executing Test 11: Urgent reserve (09:00) excluded for priority B and D...")
    now_dt = datetime(2026, 11, 21, 7, 0, 0, tzinfo=timezone.utc)
    lines = [
        f"slot_id=s-u-0900 doctor_id=asst-ortica-h1 procedure=VIS-FIS at={(now_dt + timedelta(hours=2)).isoformat()} day=mon free=1 urgent_reserve=true",
        f"slot_id=s-ord-1030 doctor_id=asst-ortica-h1 procedure=VIS-FIS at={(now_dt + timedelta(hours=5)).isoformat()} day=mon free=1",
    ]

    chosen_b = bc.fitting(lines, now=now_dt, priority="B", lang="it")
    assert all("0900" not in s["slot_id"] for s in chosen_b), f"Test 11 FAILED: priority B got 09:00 slot: {chosen_b}"
    assert len(chosen_b) == 1 and "1030" in chosen_b[0]["slot_id"]

    chosen_d = bc.fitting(lines, now=now_dt, priority="D", lang="it")
    assert all("0900" not in s["slot_id"] for s in chosen_d), f"Test 11 FAILED: priority D got 09:00 slot: {chosen_d}"
    assert len(chosen_d) == 1 and "1030" in chosen_d[0]["slot_id"]

    # Priority U can use urgent reserve
    chosen_u = bc.fitting(lines, now=now_dt, priority="U", lang="it")
    assert any("0900" in s["slot_id"] for s in chosen_u)
    print("  PASS: Urgent reserve strictly reserved for priority U!")


# ---------------------------------------------------------------------------------
# TEST 12: Riavvio dell'agente: nessuna duplicazione e nessun blocco
# ---------------------------------------------------------------------------------
async def test_12_agent_restart_reconstructed_state():
    print("Executing Test 12: Agent restart with empty memory -> reconstruct state from chat...")
    # Simulate restart by clearing _SENT and _GATES
    bc._SENT.clear()
    bc._GATES.clear()

    city = MockCity()
    need_id = "N-test-restart"
    citizen = "citizen:c-restart"
    now_dt = datetime(2026, 11, 21, 10, 0, 0, tzinfo=timezone.utc)
    now_str = now_dt.isoformat()
    rx_num = "prescription_number-restart"
    partner = "agent:qtjne8symjas"

    # History already has search note and need.request sent in previous lifetime
    city._history = [
        Message(type="chat", sender=citizen, msg_id="m1", need_id=need_id, body={"text": f"visita cardiologica prescription_number={rx_num}"}),
        Message(type="chat", sender="agent:ega0tzcy6za8", msg_id="m2", need_id=need_id, recipient=citizen, body={"text": f"prescription prescription_number={rx_num} procedure=VIS-CARD priority=B"}),
        Message(type="chat", sender=LEDGER, msg_id="m3", need_id=need_id, body={"text": f"agenda agenda_now={now_str}"}),
        Message(type="chat", sender=LEDGER, msg_id="m4", need_id=need_id, body={"text": f"prescription prescription_number={rx_num} procedure=VIS-CARD priority=B state=issued"}),
        Message(type="chat", sender=LEDGER, msg_id="m5", need_id=need_id, body={"text": "slots page=1/1\n"}),
        Message(type="chat", sender=city.agent_id, msg_id="m6", need_id=need_id, recipient=partner, body={"text": f"need.request service_type=specialist_care.cardiology procedure=VIS-CARD priority=B request_id={need_id}"}),
        Message(type="chat", sender=city.agent_id, msg_id="m7", need_id=need_id, recipient=citizen, body={"text": bc._BC_SEARCHING_IT}),
    ]

    # New message from citizen arrives after restart
    new_msg = Message(
        type="chat",
        sender=citizen,
        msg_id="m8",
        need_id=need_id,
        recipient=city.agent_id,
        body={"text": "Ci sono aggiornamenti?"},
        sent_at=(now_dt + timedelta(seconds=10)).isoformat(),
    )
    city._history.append(new_msg)

    await bc.handle("booking_centre", city, new_msg, city._history)

    # Reconstructed state must NOT re-send search notice nor prematurely send waiting list!
    search_notes = [text for to, text in city.sent_messages if to == citizen and text == bc._BC_SEARCHING_IT]
    assert len(search_notes) == 0, f"Test 12 FAILED: search notice resent on restart! {city.sent_messages}"
    wl = [text for to, text in city.sent_messages if "waiting_list=true" in text]
    assert len(wl) == 0, f"Test 12 FAILED: premature waiting list sent on restart! {city.sent_messages}"
    print("  PASS: Agent restart handled cleanly with reconstructed state!")


async def run_all():
    print("=================================================================")
    print("MACH HACKATHON — SUITE DI TEST LIVELLO 3 RELIABILITY (12 TEST)")
    print("=================================================================")
    await test_1_vis_fis_priority_d_many_slots()
    await test_2_vis_card_priority_b_38_slots()
    await test_3_vis_card_priority_b_79_slots_rewrite()
    await test_4_vis_fis_en_zero_slots_waiting_list()
    await test_5_concurrency_no_duplicate_search_note()
    await test_6_ledger_delayed_recovery()
    await test_7_late_partner_offer_evaluated()
    await test_8_offer_rejected_new_search()
    await test_9_slot_cancelled_by_facility()
    await test_10_rx_expired()
    test_11_urgent_reserve_not_used_for_b_or_d()
    await test_12_agent_restart_reconstructed_state()
    print("=================================================================")
    print("TUTTI I 12 TEST OBBLIGATORI SONO PASSATI CON SUCCESSO! 🎉")
    print("=================================================================")


if __name__ == "__main__":
    asyncio.run(run_all())
