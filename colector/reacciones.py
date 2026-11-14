"""Colector público del registro compartido de «Reacciones pasadas» de News Advisor (TrendPax).

Lo ejecuta GitHub Actions cada hora; el feed de Forex Factory se pide como mucho cada 3 h y, entre medias, se mide con
una agenda derivada guardada en el propio JSON (horas, ventanas y hashes de las series; sin títulos ni consensos). Mide,
con las velas de biquote, cómo se movieron los pares en la ventana de reacción histórica de cada noticia de alto impacto ya publicada (la misma ventana y los mismos instrumentos que el programa) y
actualiza reacciones.json en la rama «datos». Solo publica resultados derivados por publicación (pips), nunca velas, ni
títulos ni consensos de terceros: cada serie se identifica con un hash corto de su título normalizado, que el programa
calcula igual desde su propio calendario. También publica «equivalencias»: el id de serie del calendario de biquote de
cada serie ya registrada → su hash, para que el programa reconozca la misma serie en las semanas que Forex Factory aún no
cubre (allí los títulos vienen del proveedor y no coinciden). Sale de la consulta del dato publicado: sin peticiones extra.

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
ID_BQ = re.compile(r"[a-z0-9_]{1,20}:[0-9]{1,15}")
MAX_EQUIVALENCIAS = 2000
MAX_SIN_EQ = 3   # bloques ya registrados a los que, como mucho, se busca la equivalencia en cada ejecución
CADA_FF = timedelta(hours=3)   # norma del proyecto: el feed de Forex Factory, como mucho 1 petición cada 3 h
MAX_AGENDA = 200
BID = re.compile(r"\d{8}-\d{4}-(USD|EUR|GBP|JPY|CHF|CAD|AUD|NZD|OIL)")


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


def medir(velas, a, paso):
    """a: entrada de la agenda derivada (hora del dato y ventana)."""
    def suelo(x):
        m = paso.seconds // 60
        return x.replace(second=0, microsecond=0) - timedelta(minutes=x.minute % m)

    t0, ini, fin = suelo(de_iso(a["utc"])), suelo(de_iso(a["ini"])), suelo(de_iso(a["fin"])) - paso
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
    """({hash de serie: dato publicado según biquote}, {id de serie de biquote: hash de serie}) del bloque."""
    q = urllib.parse.urlencode({"from": iso(b["utc"] - timedelta(minutes=1)), "to": iso(b["utc"] + timedelta(minutes=1)),
                                "countries": PAISES["USD" if b["divisa"] == "OIL" else b["divisa"]], "limit": 100})
    cands = []
    for imp in ("high", "medium"):
        cands += http_json(f"{BQ}/calendar?{q}&importance={imp}")
    out, eq = {}, {}
    for e in b["eventos"]:
        c = _emparejar(e, cands)
        if c and isinstance(c.get("eventId"), str) and ID_BQ.fullmatch(c["eventId"]):
            eq[c["eventId"]] = serie(b["divisa"], e["titulo"])
        if c and c.get("actual") is not None:
            v = valor_bq(c["actual"], c.get("multiplier"))
            if isinstance(v, (int, float)) and math.isfinite(v):
                out[serie(b["divisa"], e["titulo"])] = round(float(v), 6)
    return out, eq


# ---------------------------------------------------------------- validación (la misma que aplica el programa)
def valido(doc):
    if not isinstance(doc, dict) or doc.get("v") != 1 or not isinstance(doc.get("registros"), dict):
        return False
    if len(doc["registros"]) > MAX_REGISTROS:
        return False
    eq = doc.get("equivalencias", {})
    if not isinstance(eq, dict) or len(eq) > MAX_EQUIVALENCIAS or not all(
            isinstance(k, str) and ID_BQ.fullmatch(k) and isinstance(x, str) and HEX12.fullmatch(x) for k, x in eq.items()):
        return False
    ag = doc.get("agenda", {})
    if not isinstance(ag, dict) or len(ag) > MAX_AGENDA:
        return False
    for bid, a in ag.items():
        try:
            if not BID.fullmatch(bid) or a["divisa"] != bid[14:] or not isinstance(a["min"], int):
                return False
            for k in ("utc", "ini", "fin"):
                de_iso(a[k])
        except (KeyError, ValueError, TypeError, AttributeError):
            return False
        cl = a.get("claves")
        if not isinstance(cl, list) or not 1 <= len(cl) <= 10 or not all(isinstance(c, str) and HEX12.fullmatch(c) for c in cl):
            return False
    if "ff" in doc:
        try:
            de_iso(doc["ff"])
        except (ValueError, TypeError, AttributeError):
            return False
    for bid, r in doc["registros"].items():
        if not BID.fullmatch(bid) or not isinstance(r, dict):
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


def derivada(b):
    """Lo que el colector necesita de un bloque para medirlo sin el calendario: hora, ventana y series (hashes)."""
    v = ventana(b)
    return {"divisa": b["divisa"], "utc": iso(b["utc"]), "ini": iso(v["entrada"]), "fin": iso(v["salida"]),
            "min": v["min"], "claves": sorted({serie(b["divisa"], e["titulo"]) for e in b["eventos"]})[:10]}


def main(ruta, ahora=None, feed=None, descargar=barras, publicados=datos_publicados):
    """Cada ejecución mide con la agenda derivada guardada; el feed de Forex Factory se pide como mucho cada 3 h (para
    renovar la agenda, el dato publicado y las equivalencias, que necesitan los títulos)."""
    t = ahora or datetime.now(timezone.utc)
    doc = {"v": 1, "fuente": FUENTE, "generado": iso(t), "registros": {}, "equivalencias": {}, "agenda": {}}
    previo = None
    if os.path.exists(ruta):
        with open(ruta, encoding="utf-8") as f:
            previo = json.load(f)
        if not valido(previo):
            raise SystemExit("el reacciones.json actual no es válido: no se toca")
        doc["registros"] = previo["registros"]
        doc["equivalencias"] = dict(previo.get("equivalencias", {}))
        doc["agenda"] = dict(previo.get("agenda", {}))
        if previo.get("ff"):
            doc["ff"] = previo["ff"]
    if feed is None and (not doc.get("ff") or t - de_iso(doc["ff"]) >= CADA_FF):
        try:
            feed = http_json(FF_URL)
        except Exception as ex:   # p. ej., 429: se sigue con la agenda guardada; se reintenta en la siguiente
            print(f"sin calendario ({ex})", file=sys.stderr)
            if not doc["agenda"]:
                return None
    bls = {}
    if feed is not None:
        bls = bloques(feed)
        doc["ff"] = iso(t)
        for bid, b in bls.items():
            doc["agenda"][bid] = derivada(b)
    # la agenda solo guarda lo que aún se puede medir
    ag = {bid: a for bid, a in sorted(doc["agenda"].items()) if de_iso(a["utc"]) + LIMITE >= t}
    doc["agenda"] = dict(list(ag.items())[-MAX_AGENDA:])
    pend = [bid for bid, a in doc["agenda"].items()
            if de_iso(a["fin"]) + MARGEN <= t <= de_iso(a["utc"]) + LIMITE
            and (bid not in doc["registros"] or not doc["registros"][bid].get("actual"))]
    con_eq = set(doc["equivalencias"].values())
    sin_eq = [b["id"] for b in bls.values() if b["id"] in doc["registros"] and b["id"] not in pend and b["utc"] <= t
              and any(serie(b["divisa"], e["titulo"]) not in con_eq for e in b["eventos"]
                      if (indicador(e["titulo"]) or ("texto",))[0] != "texto")]
    if sin_eq:   # rotando, para que un bloque que nunca empareja no tape a los demás
        i = t.hour % len(sin_eq)
        sin_eq = (sin_eq[i:] + sin_eq[:i])[:MAX_SIN_EQ]
    cache, nuevos, cambios = {}, 0, 0
    for bid in pend + sin_eq:
        r = doc["registros"].get(bid)
        if r is None:
            a = doc["agenda"][bid]
            intervalo, paso = (("1m", timedelta(minutes=1)) if t - de_iso(a["utc"]) <= CON_1M
                               else ("5m", timedelta(minutes=5)))
            pares = {}
            for par in instrumentos(a["divisa"]):
                if (par, intervalo) not in cache:
                    try:
                        cache[(par, intervalo)] = descargar(par, intervalo)
                    except Exception as ex:   # sin datos: se reintenta en la ejecución siguiente
                        print(f"aviso: {par} {intervalo}: {ex}", file=sys.stderr)
                        cache[(par, intervalo)] = {}
                m = medir(cache[(par, intervalo)], a, paso)
                if m:
                    pares[par] = m(par)
            if not pares:
                continue
            r = {"divisa": a["divisa"], "utc": a["utc"], "claves": a["claves"],
                 "ventana": {"ini": a["ini"], "fin": a["fin"], "min": a["min"]}, "pares": pares, "actual": {}}
            nuevos += 1
        if bid in bls:   # el dato publicado y las equivalencias necesitan los títulos (solo con el feed recién pedido)
            try:
                res = publicados(bls[bid])
                act, eq = res if isinstance(res, tuple) else (res, {})
                if act and act != r.get("actual"):
                    r["actual"] = act
                    cambios += 1
                for k, x in eq.items():
                    if doc["equivalencias"].get(k) != x and len(doc["equivalencias"]) < MAX_EQUIVALENCIAS:
                        doc["equivalencias"][k] = x
                        cambios += 1
            except Exception as ex:
                print(f"aviso: dato publicado {bid}: {ex}", file=sys.stderr)
        doc["registros"][bid] = r
    if (previo is not None and not nuevos and not cambios and doc.get("ff") == previo.get("ff")
            and doc["agenda"] == previo.get("agenda", {})):
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
