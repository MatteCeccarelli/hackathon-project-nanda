import re
from city_sdk.city import Message
import gp.agent.agent as agent_module

test_cases = [
    "sento un dolore forte, continuo al petto",
    "ho una perdita improvvisa, totale di forza alle gambe",
    "sento un dolore molto acuto e continuo al petto",
    "ho una perdita improvvisa e grave di forza alle gambe",
    "ho una perdita improvvisa, fortissima, grave e totale di forza alle gambe",
    "dolore acuto, forte e continuo al petto",
]

for t in test_cases:
    msg = Message(type="chat", sender="citizen:c", recipient="agent:a", msg_id="m", body={"text": t})
    res = agent_module._emergency(msg)
    print(f"'{t}' -> emergency={res}")
