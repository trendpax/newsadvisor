"""GENERADO por build/generar_colector.py a partir de las reglas de News Advisor: no editar a mano.

Reglas fijas y públicas del programa: categoría de cada noticia, ventana de reacción histórica (inicio a T+5/10/15 min,
60 min, fuera del rollover de 23:45 a 01:00 del servidor) y emparejamiento con el calendario de biquote.
"""
import re
from datetime import timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def servidor(t):
    """Hora del servidor de los brókers de MT4/MT5 con horario de Nueva York + 7 h."""
    return t.astimezone(NY).replace(tzinfo=None) + timedelta(hours=7)


INDICADORES = [
    # petróleo (activo "OIL"): cambio semanal de existencias de crudo de la EIA, en miles de barriles
    ("crude oil inventories",        "energia",     -1, 3000.0, 1.0),
    ("crude oil stocks change",      "energia",     -1, 3000.0, 1.0),
    ("unemployment claims",          "empleo",      -1, 15.0,  0.6),
    ("jobless claims",               "empleo",      -1, 15.0,  0.6),
    ("claimant count",               "empleo",      -1, 15.0,  0.6),
    ("unemployment rate",            "empleo",      -1, 0.1,   0.7),
    ("non-farm employment change",   "empleo",      +1, 50.0,  1.0),
    ("nonfarm payrolls",             "empleo",      +1, 50.0,  1.0),
    ("adp non",                      "empleo",      +1, 40.0,  0.6),
    ("employment change q/q",        "empleo",      +1, 0.3,   1.0),   # Nueva Zelanda: en %, no en miles
    ("employment change",            "empleo",      +1, 20.0,  1.0),
    ("average hourly earnings",      "empleo",      +1, 0.1,   0.8),
    ("wage price index",             "inflacion",   +1, 0.1,   1.0),
    ("labor cost index",             "inflacion",   +1, 0.1,   0.8),
    ("trimmed mean cpi",             "inflacion",   +1, 0.1,   1.0),
    ("core pce",                     "inflacion",   +1, 0.1,   1.0),
    ("core cpi",                     "inflacion",   +1, 0.1,   1.0),
    ("cpi",                          "inflacion",   +1, 0.1,   0.7),
    ("ppi",                          "inflacion",   +1, 0.2,   0.4),
    ("federal funds rate",           "tipos",       +1, 0.25,  3.0),
    ("official cash rate",           "tipos",       +1, 0.25,  3.0),
    ("cash rate",                    "tipos",       +1, 0.25,  3.0),
    ("overnight rate",               "tipos",       +1, 0.25,  3.0),
    ("main refinancing rate",        "tipos",       +1, 0.25,  3.0),
    ("official bank rate",           "tipos",       +1, 0.25,  3.0),
    ("boj policy rate",              "tipos",       +1, 0.10,  3.0),
    ("snb policy rate",              "tipos",       +1, 0.25,  3.0),
    ("interest rate decision",       "tipos",       +1, 0.25,  3.0),
    ("gdp",                          "crecimiento", +1, 0.3,   1.0),
    ("core retail sales",            "crecimiento", +1, 0.4,   1.0),
    ("retail sales",                 "crecimiento", +1, 0.4,   0.8),
    ("ism services",                 "crecimiento", +1, 1.5,   1.0),
    ("ism non-manufacturing",        "crecimiento", +1, 1.5,   1.0),
    ("ism manufacturing",            "crecimiento", +1, 1.5,   1.0),
    ("pmi",                          "crecimiento", +1, 1.5,   0.7),
    ("consumer sentiment",           "crecimiento", +1, 2.0,   0.5),
    ("consumer confidence",          "crecimiento", +1, 3.0,   0.5),
    ("jolts",                        "empleo",      +1, 300.0, 0.6),
]


TEXTO = ["statement", "press conference", "speaks", "speech", "minutes", "testifies", "report", "budget",
         "conference", "summary", "outlook"]


ENTRADA_MIN = {"energia": 5, "empleo": 5, "inflacion": 5, "crecimiento": 5, "tipos": 10, "texto": 15, "otros": 10}


HORIZONTE_MIN = 60


ROLLOVER = ("23:45", "01:00")


def es_crudo(titulo):
    """¿Es el dato de inventarios de crudo de la EIA (no el de Cushing ni otros del mismo informe)?"""
    t = titulo.lower()
    return ("crude oil inventories" in t or "crude oil stocks change" in t) and "cushing" not in t


def indicador(titulo):
    t = titulo.lower()
    if "votes" in t:   # reparto de votos («7-0-2»): no es el tipo de interés
        return None
    for pat, cat, signo, escala, peso in INDICADORES:
        if pat in t:
            return cat, signo, escala, peso
    if any(w in t for w in TEXTO):
        return "texto", 0, None, 0
    return None


def categoria(bloque):
    mejor, peso = "otros", -1
    for e in bloque["eventos"]:
        ind = indicador(e["titulo"])
        if ind is None:
            continue
        if ind[0] == "texto":
            if mejor == "otros":
                mejor = "texto"
        elif ind[3] > peso:
            mejor, peso = ind[0], ind[3]
    return mejor


def en_rollover(t):
    hm = servidor(t).strftime("%H:%M")
    return hm >= ROLLOVER[0] or hm < ROLLOVER[1]


def ventana(bloque):
    cat = categoria(bloque)
    ent = bloque["utc"] + timedelta(minutes=ENTRADA_MIN.get(cat, 10))
    movida = False
    for _ in range(300):
        if not en_rollover(ent) and not en_rollover(ent + timedelta(minutes=HORIZONTE_MIN)):
            break
        ent += timedelta(minutes=5)
        movida = True
    return {"categoria": cat, "entrada": ent, "salida": ent + timedelta(minutes=HORIZONTE_MIN),
            "min": int((ent - bloque["utc"]).total_seconds() // 60), "rollover": movida}


SINONIMOS = [("non-farm employment change", "nonfarm payrolls"), ("unemployment claims", "initial jobless claims"),
             ("advance gdp", "gdp"), ("prelim gdp", "gdp"), ("final gdp", "gdp"), ("flash ", ""),
             ("ism services pmi", "ism non-manufacturing pmi"), ("cb consumer confidence", "cb consumer confidence index"),
             ("crude oil inventories", "eia crude oil stocks change")]


PERIODOS = {"m/m", "y/y", "q/q"}


def _norm(t):
    t = t.lower()
    for a, b in SINONIMOS:
        t = t.replace(a, b)
    return set(re.findall(r"[a-z0-9/]+", t))


def _emparejar(ev, cands):
    """El evento de biquote que corresponde a uno de Forex Factory (misma divisa y minuto)."""
    ind = indicador(ev["titulo"])
    if ind and ind[0] == "tipos":
        for c in cands:
            n = c["name"].lower()
            if "interest rate decision" in n and "deposit" not in n:
                return c
    if ind is None or ind[0] == "texto":
        return None
    a = _norm(ev["titulo"])
    mejor, puntos = None, 0.0
    for c in cands:
        if c.get("type") != "indicator":
            continue
        b = _norm(c["name"])
        if a & PERIODOS != b & PERIODOS:   # m/m no empareja con y/y ni con el nivel del índice
            continue
        j = len(a & b) / len(a | b)
        if j > puntos:
            mejor, puntos = c, j
    return mejor if puntos >= 0.5 else None
