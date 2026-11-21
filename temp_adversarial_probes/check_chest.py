import re
pat_chest = re.compile(r"\b(?:dolore|fitt\w+|oppression\w+|stringimento|bruciore)\s+(?:\w+\s+){0,6}(?:al|nel|del)?\s*petto\b", re.I)
c1 = "sento un dolore molto acuto e continuo al petto"
c2 = "sento un dolore forte, acuto e continuo al petto"
print("c1 (no comma):", bool(pat_chest.search(c1)))
print("c2 (with comma):", bool(pat_chest.search(c2)))
