import re
pat = re.compile(r"\b(?:perdit\w+|mancanz\w+|senza|non\s+ho)\s+(?:\w+\s+){0,6}forz\w*", re.I)
s1 = "ho una perdita improvvisa e grave di forza alle gambe"
s2 = "ho una perdita improvvisa, fortissima, grave e totale di forza alle gambe"
s3 = "ho una perdita improvvisa fortissima grave e totale di forza alle gambe"
print("s1 (no comma):", bool(pat.search(s1)))
print("s2 (with comma):", bool(pat.search(s2)))
print("s3 (no comma, 5 words):", bool(pat.search(s3)))
