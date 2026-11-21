import asyncio
from datetime import datetime, timedelta, timezone
import sys
import os

sys.path.insert(0, ".")
from city_sdk.city import Message, City, LEDGER

# Import our unified agent
import booking_centre.agent.agent as agent_module


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

    async def llm(self, messages):
        return {"content": "Il Centro Prenotazioni le proporrà gli orari a breve."}


class MockPeer:
    def __init__(self, agent_id, capabilities=(), role="facility", district=6):
        self.agent_id = agent_id
        self.capabilities = capabilities
        self.role = role
        self.district = district


async def test_gp_symptom_and_prescription():
    print("Test GP: Palpitations -> VIS-CARD Priority B prescription...")
    city = MockCity(agent_id="agent:gp-test")
    citizen = "citizen:001"
    need_id = "N-001"
    msg = Message(
        type="chat", sender=citizen, msg_id="m1", need_id=need_id, recipient=city.agent_id,
        body={"text": "Buongiorno dottore, ho il cuore che batte fortissimo da stamattina"}
    )
    city._history = [msg]
    await agent_module.handle("gp", city, msg, city._history)

    sent = city.sent_messages
    ledger_prescribes = [m for to, m in sent if to == LEDGER and "prescribe" in m]
    citizen_prescribes = [m for to, m in sent if to == citizen and "prescription" in m]
    assert ledger_prescribes, "Ledger must receive prescribe"
    assert "procedure=VIS-CARD" in ledger_prescribes[0]
    assert "priority=B" in ledger_prescribes[0]
    assert citizen_prescribes, "Citizen must receive prescription"
    assert "procedure=VIS-CARD" in citizen_prescribes[0]
    assert "priority=B" in citizen_prescribes[0]
    print("  PASS: GP issued conforming prescription for palpitazioni!")


async def test_gp_red_flag_emergency():
    print("Test GP: Red Flag (loss of strength in legs) -> Emergency escalation...")
    city = MockCity(agent_id="agent:gp-test")
    citizen = "citizen:002"
    need_id = "N-002"
    msg = Message(
        type="chat", sender=citizen, msg_id="m1", need_id=need_id, recipient=city.agent_id,
        body={"text": "Ho un forte mal di schiena e sento perdita di forza alle gambe"}
    )
    city._history = [msg]
    await agent_module.handle("gp", city, msg, city._history)

    sent = city.sent_messages
    assert any("escalate.emergency reason=red_flag" in m for to, m in sent if to == citizen)
    print("  PASS: GP immediately escalated red flag emergency!")


async def test_gp_p1_renewal():
    print("Test GP: Expired prescription renewal (Rule P1)...")
    city = MockCity(agent_id="agent:gp-test")
    citizen = "citizen:003"
    need_id = "N-003"
    old_rx = "prescription_number-ORIGINAL-123"

    # History contains ledger confirmation of old prescription
    city._history = [
        Message(type="chat", sender=LEDGER, msg_id="m0", need_id=need_id, body={"text": f"prescription prescription_number={old_rx} procedure=VIS-FIS priority=B symptom_code=SYM-BACK state=expired"}),
        Message(type="chat", sender=citizen, msg_id="m1", need_id=need_id, recipient=city.agent_id, body={"text": f"Buongiorno, devo rinnovare la ricetta prescription_number={old_rx}"})
    ]
    await agent_module.handle("gp", city, city._history[-1], city._history)

    sent = city.sent_messages
    ledger_prescribes = [m for to, m in sent if to == LEDGER and "prescribe" in m]
    assert ledger_prescribes, "GP must prescribe renewal on Ledger"
    assert "-R1" in ledger_prescribes[0]
    assert "procedure=VIS-FIS" in ledger_prescribes[0]
    assert "priority=B" in ledger_prescribes[0]
    citizen_rx = [m for to, m in sent if to == citizen and "prescription" in m]
    assert citizen_rx, "GP must deliver renewed prescription to citizen"
    print("  PASS: GP renewed expired prescription with -R1 and protocol compliance!")


async def test_specialist_checkin_consent_and_followup():
    print("Test Specialist: Checkin, report_sharing consent, report archiving, and CTRL-CARD follow-up...")
    city = MockCity(agent_id="agent:spec-test")
    citizen = "citizen:004"
    need_id = "N-004"
    booking_id = "bk-spec-123"

    # Step 1: Citizen does checkin
    msg1 = Message(type="chat", sender=citizen, msg_id="m1", need_id=need_id, recipient=city.agent_id, body={"text": f"checkin booking_id={booking_id}"})
    city._history = [
        Message(type="chat", sender=citizen, msg_id="m0", need_id=need_id, body={"text": "visita cardiologica VIS-CARD"}),
        msg1
    ]

    # Mock Ledger replying to checkin
    orig_say = city.say
    async def mock_say(to, text):
        res = await orig_say(to, text)
        if to == LEDGER and "checkin" in text:
            city._history.append(Message(type="chat", sender=LEDGER, msg_id="l-ck", need_id=need_id, body={"text": f"checked_in booking_id={booking_id}"}))
        if to == LEDGER and "report" in text:
            city._history.append(Message(type="chat", sender=LEDGER, msg_id="l-rep", need_id=need_id, body={"text": f"reported booking_id={booking_id}"}))
        return res
    city.say = mock_say

    await agent_module.handle("specialist_care", city, msg1, city._history)

    sent = city.sent_messages
    # Verify checkin acknowledged to citizen
    assert any("checkin booking_id=" in m for to, m in sent if to == citizen)
    # Verify report_sharing consent requested
    assert any("consent purpose=report_sharing" in m for to, m in sent if to == citizen)
    # Verify follow-up prescribed on Ledger with follow_up_of
    ledger_prescribes = [m for to, m in sent if to == LEDGER and "prescribe" in m]
    assert ledger_prescribes, "Specialist must prescribe CTRL-CARD follow-up on Ledger"
    assert "procedure=CTRL-CARD" in ledger_prescribes[0]
    assert "priority=P" in ledger_prescribes[0]
    assert f"follow_up_of={booking_id}" in ledger_prescribes[0]
    # Verify report written to Ledger
    ledger_reports = [m for to, m in sent if to == LEDGER and "report" in m]
    assert ledger_reports, "Specialist must write report on Ledger"
    assert "outcome_code=VISIT" in ledger_reports[0]
    # Verify citizen received report and follow-up prescription
    citizen_reports = [m for to, m in sent if to == citizen and "report booking_id=" in m]
    assert citizen_reports, "Specialist must notify citizen of report and follow-up"
    assert "CTRL-CARD" in citizen_reports[0]
    print("  PASS: Specialist completed check-in, consent prompt, report, and CTRL-CARD follow-up!")


async def test_family_counseling_direct_access():
    print("Test Family Counseling: Direct access CONS-GIN without requiring prescription...")
    city = MockCity(agent_id="agent:fc-test")
    citizen = "citizen:005"
    need_id = "N-005"
    now_dt = datetime(2026, 11, 21, 10, 0, 0, tzinfo=timezone.utc)

    # Slots on ledger for CONS-GIN
    slots = f"slots page=1/1\nslot_id=s-fc-1 doctor_id=asst-ortica-c1 facility={city.agent_id} procedure=CONS-GIN at={(now_dt + timedelta(days=1)).isoformat()} free=1 day=tue\n"
    city._history = [
        Message(type="chat", sender=LEDGER, msg_id="m-ag", need_id=need_id, body={"text": f"agenda agenda_now={now_dt.isoformat()}"}),
        Message(type="chat", sender=LEDGER, msg_id="m-sl", need_id=need_id, body={"text": slots}),
        Message(type="chat", sender=citizen, msg_id="m1", need_id=need_id, recipient=city.agent_id, body={"text": "Buongiorno, vorrei prenotare una visita ginecologica di controllo"})
    ]

    await agent_module.handle("family_counseling", city, city._history[-1], city._history)

    sent = city.sent_messages
    offers = [m for to, m in sent if to == citizen and "offer" in m]
    assert offers, "Family counseling must offer CONS-GIN slot without requiring prescription"
    assert "CONS-GIN" in offers[0] or "slot_id=s-fc-1" in offers[0]
    print("  PASS: Family counseling offered direct access CONS-GIN with no prescription required!")


async def main_tests():
    print("=================================================================")
    print("MACH HACKATHON — COMPREHENSIVE MULTI-ROLE TEST SUITE (L1..L8)")
    print("=================================================================")
    await test_gp_symptom_and_prescription()
    await test_gp_red_flag_emergency()
    await test_gp_p1_renewal()
    await test_specialist_checkin_consent_and_followup()
    await test_family_counseling_direct_access()
    print("=================================================================")
    print("TUTTI I TEST AGGIUNTIVI SUI RUOLI SONO PASSATI CON SUCCESSO! 🎉")
    print("=================================================================")

if __name__ == "__main__":
    asyncio.run(main_tests())
