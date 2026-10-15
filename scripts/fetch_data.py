#!/usr/bin/env python3
"""Collecte quotidienne des indicateurs du tableau de bord.

Lancé chaque soir par le robot GitHub (.github/workflows/update-data.yml).
Écrit data/latest.json, que la page index.html lit à l'ouverture.

Règle d'or : si une source ne répond pas, on n'invente rien. L'indicateur
passe dans "errors" et la page le laisse en saisie manuelle.
Uniquement la bibliothèque standard de Python, rien à installer.
"""

import csv
import datetime as dt
import io
import json
import os
import re
import sys
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "latest.json")


def get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------- sources

def fred_series(series_id):
    """Série FRED en CSV public (pas de clé). Renvoie [(date, valeur)] sans les trous."""
    txt = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}")
    rows = list(csv.reader(io.StringIO(txt)))
    out = []
    for row in rows[1:]:
        if len(row) < 2 or row[1] in ("", "."):
            continue
        try:
            out.append((row[0], float(row[1])))
        except ValueError:
            continue
    if not out:
        raise ValueError(f"FRED {series_id} vide")
    return out


def cboe_history(name):
    """Historique quotidien Cboe (VIX, VIX9D). Colonnes DATE, OPEN, HIGH, LOW, CLOSE."""
    txt = get(f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv")
    reader = csv.DictReader(io.StringIO(txt))
    out = {}
    for row in reader:
        row = {k.strip().upper(): v for k, v in row.items() if k}
        try:
            d = dt.datetime.strptime(row["DATE"].strip(), "%m/%d/%Y").date().isoformat()
            out[d] = float(row["CLOSE"])
        except (KeyError, ValueError):
            continue
    if not out:
        raise ValueError(f"Cboe {name} vide")
    return out


def yahoo_closes(ticker, rng="10y"):
    """Clôtures quotidiennes d'un indice via le graphique Yahoo Finance. [(date, clôture)]."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.request.quote(ticker)}?range={rng}&interval=1d"
    data = json.loads(get(url))
    res = data["chart"]["result"][0]
    ts = res["timestamp"]
    closes = res["indicators"]["quote"][0]["close"]
    out = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        out.append((dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat(), float(c)))
    if not out:
        raise ValueError(f"Yahoo {ticker} vide")
    return out


def ecb_hicp_euro_area():
    """Inflation zone euro sur un an (HICP), BCE. Renvoie (période, valeur)."""
    url = ("https://data-api.ecb.europa.eu/service/data/ICP/M.U2.N.000000.4.ANR"
           "?format=csvdata&lastNObservations=1")
    reader = csv.DictReader(io.StringIO(get(url)))
    for row in reader:
        return row["TIME_PERIOD"], float(row["OBS_VALUE"])
    raise ValueError("BCE HICP vide")


def slickcharts_top10():
    """Poids cumulé des 10 premières valeurs du S&P 500 (page slickcharts). Fragile."""
    html = get("https://www.slickcharts.com/sp500")
    body = html.split("<tbody>", 1)[1]
    rows = re.findall(r"<tr.*?</tr>", body, flags=re.S)[:10]
    weights = []
    for r in rows:
        cells = [re.sub(r"<.*?>", "", c).strip() for c in re.findall(r"<td.*?</td>", r, flags=re.S)]
        pct = [c for c in cells if c.endswith("%")]
        if pct:
            weights.append(float(pct[0].rstrip("%")))
    if len(weights) != 10:
        raise ValueError("tableau slickcharts illisible")
    total = sum(weights)
    if not 15 <= total <= 70:  # garde-fou : un chiffre absurde vaut mieux absent
        raise ValueError(f"somme top 10 incohérente ({total:.1f} %)")
    return round(total, 1)


# ---------------------------------------------------------------- calculs

def drawdown_block(closes, today, window_days=None):
    """Dernière clôture, plus haut (sur la fenêtre), écart en %, variation 1 mois, clôture hebdo."""
    if window_days:
        start = (dt.date.fromisoformat(closes[-1][0]) - dt.timedelta(days=window_days)).isoformat()
        win = [c for c in closes if c[0] >= start]
    else:
        win = closes
    last_d, last = closes[-1]
    high_d, high = max(win, key=lambda c: c[1])
    month_ago = (dt.date.fromisoformat(last_d) - dt.timedelta(days=30)).isoformat()
    past = [c for c in closes if c[0] <= month_ago]
    chg1m = round((last / past[-1][1] - 1) * 100, 2) if past else None

    # Clôture hebdomadaire : dernier jour coté de la dernière semaine terminée.
    weeks = {}
    for d, c in closes:
        y, w, _ = dt.date.fromisoformat(d).isocalendar()
        weeks[(y, w)] = (d, c)
    keys = sorted(weeks)
    cur = today.isocalendar()[:2]
    if keys and tuple(keys[-1]) == tuple(cur) and today.weekday() < 4:
        keys = keys[:-1]  # semaine en cours pas encore close (lundi à jeudi)
    wk_d, wk = weeks[keys[-1]] if keys else (None, None)

    return {
        "value": round((last / high - 1) * 100, 2),
        "date": last_d,
        "last": round(last, 2),
        "high": round(high, 2),
        "highDate": high_d,
        "chg1m": chg1m,
        "weeklyClose": round(wk, 2) if wk else None,
        "weeklyDate": wk_d,
    }


def main():
    today = dt.datetime.now(dt.timezone.utc).date()
    values, errors = {}, {}

    def run(key, fn):
        try:
            values[key] = fn()
        except Exception as e:  # noqa: BLE001 - on veut tout attraper, la page gère l'absence
            errors[key] = f"{type(e).__name__}: {e}"[:200]

    # ---- US
    vix_cache = {}

    def vix():
        s = fred_series("VIXCLS")
        vix_cache["fred"] = dict(s)
        d, v = s[-1]
        return {"value": round(v, 2), "date": d, "source": "FRED VIXCLS"}

    def vix_term():
        v9 = cboe_history("VIX9D")
        v30 = cboe_history("VIX")
        common = sorted(set(v9) & set(v30))
        d = common[-1]
        return {"value": round(v9[d] - v30[d], 2), "date": d, "source": "Cboe VIX9D - VIX"}

    hy_cache = {}

    def hy_level():
        s = fred_series("BAMLH0A0HYM2")
        hy_cache["s"] = s
        d, v = s[-1]
        return {"value": round(v, 2), "date": d, "source": "FRED BAMLH0A0HYM2"}

    def hy_accel():
        s = hy_cache.get("s") or fred_series("BAMLH0A0HYM2")
        if len(s) < 6:
            raise ValueError("historique trop court")
        d, v = s[-1]
        return {"value": round((v - s[-6][1]) * 100, 0), "date": d, "source": "FRED BAMLH0A0HYM2, 5 séances"}

    def us_cpi():
        s = fred_series("CPIAUCSL")
        d, v = s[-1]
        prev = s[-13][1]
        return {"value": round((v / prev - 1) * 100, 2), "date": d, "source": "FRED CPIAUCSL, sur 12 mois"}

    def us10y():
        d, v = fred_series("DGS10")[-1]
        return {"value": round(v, 2), "date": d, "source": "FRED DGS10"}

    def concentration():
        return {"value": slickcharts_top10(), "date": today.isoformat(), "source": "slickcharts.com"}

    run("vix", vix)
    run("vixTerm", vix_term)
    run("hyStruct", hy_level)
    run("hyAccel", hy_accel)
    run("usCpi", us_cpi)
    run("us10y", us10y)
    run("concentration", concentration)

    # ---- Europe
    def stoxx():
        b = drawdown_block(yahoo_closes("^STOXX", "10y"), today)
        b["source"] = "Yahoo ^STOXX (Stoxx Europe 600)"
        return b

    def bund():
        d, v = fred_series("IRLTLT01DEM156N")[-1]
        return {"value": round(v, 2), "date": d, "source": "FRED IRLTLT01DEM156N (moyenne mensuelle)"}

    def eu_cpi():
        d, v = ecb_hicp_euro_area()
        return {"value": round(v, 2), "date": d, "source": "BCE, HICP zone euro sur 12 mois"}

    run("stoxxDD", stoxx)
    run("bund10y", bund)
    run("euCpi", eu_cpi)

    # ---- Asie (plus haut sur 52 semaines)
    def asia(ticker, label):
        def f():
            b = drawdown_block(yahoo_closes(ticker, "2y"), today, window_days=365)
            b["source"] = f"Yahoo {ticker} ({label})"
            return b
        return f

    run("kospiDD", asia("^KS11", "Kospi"))
    run("taiexDD", asia("^TWII", "Taiex"))

    out = {
        "updated": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "values": values,
        "errors": errors,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"OK: {', '.join(sorted(values)) or 'aucun'}")
    if errors:
        print("Échecs :")
        for k, v in errors.items():
            print(f"  {k}: {v}")
    # On ne fait pas échouer le robot si quelques sources tombent :
    # seul un échec total est une vraie panne.
    return 0 if values else 1


if __name__ == "__main__":
    sys.exit(main())
