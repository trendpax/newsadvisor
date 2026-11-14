"""Colector público del registro compartido de «Reacciones pasadas» de News Advisor (TrendPax).

Lo ejecuta GitHub Actions cada hora. Mide, con las velas de biquote, cómo se movieron los pares en la ventana de reacción
histórica de cada noticia de alto impacto ya publicada (la misma ventana y los mismos instrumentos que el programa) y
actualiza reacciones.json en la rama «datos». Solo publica resultados derivados por publicación (pips), nunca velas, ni
títulos ni consensos de terceros: cada serie se identifica con un hash corto de su título normalizado, que el programa
calcula igual desde su propio calendario.

Uso: python colector/reacciones.py RUTA/reacciones.json
Sin dependencias (solo la biblioteca estándar). Sin secretos. Sale con código distinto de 0 sin escribir nada si algo no
es válido.
"""
import hashlib
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reglas_ventana import _emparejar, es_crudo, indicador, ventana  # noqa: E402

UA = "TrendPax-NewsAdvisor-Colector/1.0 (+https://trendpax.com)"
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
BQ = "https://biquote.io/api"
FUENTE = "Market data by biquote"
DIVISAS = ["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"]
PAISES = {"USD": "US", "EUR": "EU", "GBP": "GB", "JPY": "JP", "CHF": "CH", "CAD": "CA", "AUD": "AU", "NZD": "NZ"}
MAYORES = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD"]
ORO, CRUDO = "XAUUSD", "USOIL"
PIP = {p: (0.01 if p.endswith("JPY") else 0.0001) for p in MAYORES}
PIP.update({ORO: 0.1, CRUDO: 0.01})
BQ_MULT = {"none": 1.0, "thousands": 1.0, "millions": 1e3, "billions": 1e6, "trillions": 1e9}
MARGEN = timedelta(minutes=2)
CON_1M = timedelta(hours=4, minutes=30)
LIMITE = timedelta(hours=23)
MAX_REGISTROS = 5000
MAX_BYTES = 2_000_000
MAX_DESCARGA = 5_000_000
HEX12 = re.compile(r"[0-9a-f]{12}")


# ---------------------------------------------------------------- utilidades
def iso(t):
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def de_iso(s):
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read(MAX_DESCARGA))


def serie(divisa, titulo):
    """Hash corto de la serie: el programa lo calcula igual (reacciones.serie)."""
    norm = re.sub(r"[^a-z0-9/]+", " ", titulo.lower()).strip()
    return hashlib.sha256(f"{divisa}|{norm}".encode()).hexdigest()[:12]


def instrumentos(divisa):
    if divisa == "OIL":
        return [CRUDO]
    pares = [p for p in MAYORES if divisa in (p[:3], p[3:])]
    return pares + ([ORO] if divisa == "USD" else [])


# ---------------------------------------------------------------- agenda (feed de Forex Factory, sin publicarlo)
def bloques(feed):
    """Noticias de alto impacto (y el crudo de la EIA como OIL) agrupadas por divisa y hora, como en el programa."""
    out = {}
    for e in feed:
        crudo = e.get("country") == "USD" and es_crudo(e.get("title", ""))
        if (e.get("impact") != "High" and not crudo) or e.get("country") not in DIVISAS:
            continue
        t = de_iso(e["date"]).astimezone(timezone.utc)
        div = "OIL" if crudo else e["country"]
        bid = f"{t:%Y%m%d-%H%M}-{div}"
        b = out.setdefault(bid, {"id": bid, "divisa": div, "utc": t, "eventos": []})
        b["eventos"].append({"titulo": e["title"]})
    for b in out.values():
        b["eventos"].sort(key=lambda x: -(indicador(x["titulo"]) or (0, 0, 0, 0))[3])
    return out


# ---------------------------------------------------------------- medición (igual que el programa)
def barras(par, intervalo):
    d = http_json(f"{BQ}/{par}/ohlc?interval={intervalo}&limit=1000")
    return {de_iso(b["openTime"]): {k: float(b[k]) for k in ("open", "high", "low", "close")}
            for b in d.get("bars", []) if not b.get("isOpen")}


def medir(velas, b, paso):
    v = ventana(b)

    def suelo(x):
        m = paso.seconds // 60
        return x.replace(second=0, microsecond=0) - timedelta(minutes=x.minute % m)

    t0, ini, fin = suelo(b["utc"]), suelo(v["entrada"]), suelo(v["salida"]) - paso
    if any(x not in velas for x in (t0, ini, fin)):
        return None
    tramo = [x for k, x in velas.items() if t0 <= k <= fin]
    p = PIP
    return lambda par: {"pips_ventana": round((velas[fin]["close"] - velas[ini]["open"]) / p[par], 1),
                        "pips_total": round((velas[fin]["close"] - velas[t0]["open"]) / p[par], 1),
                        "rango_pips": round((max(x["high"] for x in tramo) - min(x["low"] for x in tramo)) / p[par], 1),
                        "res": f"{paso.seconds // 60}m"}


def valor_bq(v, mult):
    if v is None:
        return None
    return v * BQ_MULT.get(mult or "none", 1.0) if mult not in (None, "none") else v


def datos_publicados(b):
    """{hash de serie: dato publicado según biquote} para los eventos del bloque que ya lo tienen."""
    q = urllib.parse.urlencode({"from": iso(b["utc"] - timedelta(minutes=1)), "to": iso(b["utc"] + timedelta(minutes=1)),
                                "countries": PAISES["USD" if b["divisa"] == "OIL" else b["divisa"]], "limit": 100})
    cands = []
    for imp in ("high", "medium"):
        cands += http_json(f"{BQ}/calendar?{q}&importance={imp}")
    out = {}
    for e in b["eventos"]:
        c = _emparejar(e, cands)
        if c and c.get("actual") is not None:
            v = valor_bq(c["actual"], c.get("multiplier"))
            if isinstance(v, (int, float)) and math.isfinite(v):
                out[serie(b["divisa"], e["titulo"])] = round(float(v), 6)
    return out


# ---------------------------------------------------------------- validación (la misma que aplica el programa)
def valido(doc):
    if not isinstance(doc, dict) or doc.get("v") != 1 or not isinstance(doc.get("registros"), dict):
        return False
    if len(doc["registros"]) > MAX_REGISTROS:
        return False
    for bid, r in doc["registros"].items():
        if not re.fullmatch(r"\d{8}-\d{4}-(USD|EUR|GBP|JPY|CHF|CAD|AUD|NZD|OIL)", bid) or not isinstance(r, dict):
            return False
        try:
            de_iso(r["utc"])
        except (KeyError, ValueError, TypeError, AttributeError):
            return False
        cl = r.get("claves")
        if not isinstance(cl, list) or not 1 <= len(cl) <= 10 or not all(isinstance(c, str) and HEX12.fullmatch(c) for c in cl):
            return False
        pares = r.get("pares")
        if not isinstance(pares, dict) or not pares or not set(pares) <= set(PIP):
            return False
        for m in pares.values():
            if not isinstance(m, dict) or m.get("res") not in ("1m", "5m"):
                return False
            for k in ("pips_ventana", "pips_total", "rango_pips"):
                x = m.get(k)
                if not isinstance(x, (int, float)) or not math.isfinite(x) or abs(x) > 100000:
                    return False
        act = r.get("actual", {})
        if not isinstance(act, dict) or not all(HEX12.fullmatch(k) and isinstance(x, (int, float)) and math.isfinite(x)
                                                for k, x in act.items()):
            return False
    return True


def main(ruta, ahora=None, feed=None, descargar=barras, publicados=datos_publicados):
    t = ahora or datetime.now(timezone.utc)
    doc = {"v": 1, "fuente": FUENTE, "generado": iso(t), "registros": {}}
    if os.path.exists(ruta):
        with open(ruta, encoding="utf-8") as f:
            previo = json.load(f)
        if not valido(previo):
            raise SystemExit("el reacciones.json actual no es válido: no se toca")
        doc["registros"] = previo["registros"]
    if feed is None:
        try:
            feed = http_json(FF_URL)
        except Exception as ex:   # p. ej., 429: se reintenta en la ejecución siguiente, sin publicar nada
            print(f"sin calendario ({ex}): nada que hacer", file=sys.stderr)
            return None
    pend = [b for b in bloques(feed).values()
            if ventana(b)["salida"] + MARGEN <= t <= b["utc"] + LIMITE
            and (b["id"] not in doc["registros"] or not doc["registros"][b["id"]].get("actual"))]
    cache, nuevos, cambios = {}, 0, 0
    for b in pend:
        r = doc["registros"].get(b["id"])
        if r is None:
            intervalo, paso = ("1m", timedelta(minutes=1)) if t - b["utc"] <= CON_1M else ("5m", timedelta(minutes=5))
            pares = {}
            for par in instrumentos(b["divisa"]):
                if (par, intervalo) not in cache:
                    try:
                        cache[(par, intervalo)] = descargar(par, intervalo)
                    except Exception as ex:   # sin datos: se reintenta en la ejecución siguiente
                        print(f"aviso: {par} {intervalo}: {ex}", file=sys.stderr)
                        cache[(par, intervalo)] = {}
                m = medir(cache[(par, intervalo)], b, paso)
                if m:
                    pares[par] = m(par)
            if not pares:
                continue
            v = ventana(b)
            r = {"divisa": b["divisa"], "utc": iso(b["utc"]),
                 "claves": sorted({serie(b["divisa"], e["titulo"]) for e in b["eventos"]})[:10],
                 "ventana": {"ini": iso(v["entrada"]), "fin": iso(v["salida"]), "min": v["min"]},
                 "pares": pares, "actual": {}}
            nuevos += 1
        try:
            act = publicados(b)
            if act and act != r.get("actual"):
                r["actual"] = act
                cambios += 1
        except Exception as ex:
            print(f"aviso: dato publicado {b['id']}: {ex}", file=sys.stderr)
        doc["registros"][b["id"]] = r
    if not nuevos and not cambios and os.path.exists(ruta):
        print(f"sin cambios: {len(pend)} pendientes, {len(doc['registros'])} registros")
        return None
    # límites: los registros más antiguos salen primero
    regs = sorted(doc["registros"].items(), key=lambda kv: kv[1]["utc"])
    while len(regs) > MAX_REGISTROS:
        regs.pop(0)
    doc["registros"] = dict(regs)
    texto = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    while len(texto.encode()) > MAX_BYTES and doc["registros"]:
        doc["registros"].pop(next(iter(doc["registros"])))
        texto = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if not valido(json.loads(texto)):
        raise SystemExit("el resultado no es válido: no se publica")
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(texto)
    os.replace(tmp, ruta)
    print(f"ok: {len(pend)} pendientes, {nuevos} nuevas, {len(doc['registros'])} registros, {len(texto)} bytes")
    return doc


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("uso: python colector/reacciones.py RUTA/reacciones.json")
    main(sys.argv[1])
