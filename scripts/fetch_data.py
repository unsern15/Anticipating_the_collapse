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
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "latest.json")


DEAD_HOSTS = set()


# stoxx.com sert une chaîne de certificats incomplète que Python refuse. Pour ce seul site
# (données publiques, lecture seule, valeurs contrôlées ensuite), on accepte sans vérifier.
INSECURE_HOSTS = {"www.stoxx.com"}


def get(url, timeout=25, tries=2):
    """Téléchargement avec une nouvelle tentative. Un site qui ne répond pas du tout
    est mis de côté pour le reste du passage, pour ne pas bloquer le robot."""
    host = urllib.parse.urlparse(url).netloc
    if host in DEAD_HOSTS:
        raise TimeoutError(f"{host} déjà injoignable ce soir")
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            ctx = None
            if host in INSECURE_HOSTS:
                import ssl
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (429, 500, 502, 503, 504):
                raise
        except Exception as e:  # noqa: BLE001
            last = e
            if isinstance(e, TimeoutError) or "timed out" in str(e):
                DEAD_HOSTS.add(host)
                raise
        time.sleep(5 * (i + 1))
    raise last


def first_ok(*fns):
    """Essaie plusieurs sources dans l'ordre, renvoie la première qui marche."""
    errs = []
    for fn in fns:
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            errs.append(f"{type(e).__name__}: {e}"[:90])
    raise RuntimeError(" | ".join(errs))


# ---------------------------------------------------------------- sources

def fred_series(series_id, start=None):
    """Série FRED. Avec la clé gratuite FRED_API_KEY (secret GitHub) on passe par l'API
    officielle, plus fiable ; sinon par le CSV public. Renvoie [(date, valeur)]."""
    start = start or (dt.date.today() - dt.timedelta(days=800)).isoformat()
    key = os.environ.get("FRED_API_KEY", "").strip()
    if key:
        d = json.loads(get("https://api.stlouisfed.org/fred/series/observations"
                           f"?series_id={series_id}&api_key={key}&file_type=json&observation_start={start}"))
        out = [(o["date"], float(o["value"])) for o in d.get("observations", []) if o.get("value") not in ("", ".")]
        if out:
            return out
        raise ValueError(f"FRED API {series_id} vide")
    txt = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}")
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


_YAHOO = {}


def _yahoo_session():
    """Yahoo limite les robots sans cookie : on récupère un cookie et un « crumb » une fois."""
    if "opener" in _YAHOO:
        return _YAHOO["opener"], _YAHOO["crumb"]
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", UA), ("Accept", "*/*"), ("Accept-Language", "en-US,en;q=0.9")]
    for u in ("https://fc.yahoo.com", "https://finance.yahoo.com/"):
        try:
            opener.open(u, timeout=20).read()
        except Exception:  # noqa: BLE001 - fc.yahoo.com renvoie souvent 404 mais pose le cookie
            pass
    crumb = opener.open("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=20).read().decode().strip()
    _YAHOO.update(opener=opener, crumb=crumb)
    return opener, crumb


def yahoo_closes(ticker, rng="10y"):
    """Clôtures quotidiennes d'un indice via le graphique Yahoo Finance. [(date, clôture)]."""
    q = urllib.request.quote(ticker)

    def with_crumb():
        opener, crumb = _yahoo_session()
        url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{q}?range={rng}&interval=1d"
               f"&crumb={urllib.request.quote(crumb)}")
        for i in range(3):
            try:
                return opener.open(url, timeout=25).read().decode()
            except urllib.error.HTTPError as e:
                if e.code != 429 or i == 2:
                    raise
                time.sleep(8 * (i + 1))

    data = json.loads(first_ok(
        with_crumb,
        lambda: get(f"https://query1.finance.yahoo.com/v8/finance/chart/{q}?range={rng}&interval=1d", tries=2),
    ))
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


def stooq_closes(symbol):
    """Clôtures quotidiennes depuis stooq.com (CSV gratuit). [(date, clôture)]."""
    txt = get(f"https://stooq.com/q/d/l/?s={urllib.request.quote(symbol)}&i=d", tries=2)
    out = []
    for row in csv.DictReader(io.StringIO(txt)):
        try:
            out.append((row["Date"], float(row["Close"])))
        except (KeyError, ValueError, TypeError):
            continue
    if len(out) < 30:
        raise ValueError(f"stooq {symbol} vide")
    return out


def twse_taiex(months=13):
    """Clôtures du Taiex depuis la bourse de Taïwan (un appel par mois). [(date, clôture)]."""
    out = []
    d = dt.date.today().replace(day=1)
    for _ in range(months):
        js = json.loads(get(f"https://www.twse.com.tw/exchangeReport/FMTQIK?response=json&date={d:%Y%m%d}"))
        fields = js.get("fields") or []
        col = next((i for i, f in enumerate(fields) if "指數" in f), 4)
        for row in js.get("data") or []:
            y, m, dd = row[0].split("/")
            out.append((dt.date(int(y) + 1911, int(m), int(dd)).isoformat(), float(row[col].replace(",", ""))))
        d = (d - dt.timedelta(days=1)).replace(day=1)
        time.sleep(2)
    out.sort()
    if len(out) < 30:
        raise ValueError("TWSE vide")
    return out


def naver_index(code="KOSPI"):
    """Clôtures quotidiennes d'un indice coréen via le graphique Naver (XML,
    lignes « date|ouverture|haut|bas|clôture|volume »). [(date, clôture)]."""
    xml = get(f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count=400&requestType=0")
    pts = []
    for raw in re.findall(r'data="([^"]+)"', xml):
        parts = raw.split("|")
        try:
            d = parts[0]
            pts.append((f"{d[:4]}-{d[4:6]}-{d[6:8]}", float(parts[4])))
        except (IndexError, ValueError):
            continue
    pts.sort()
    if len(pts) < 30:
        raise ValueError(f"Naver {code} vide")
    return pts


def treasury_10y():
    """Taux US à 10 ans depuis le site du Trésor américain. (date, valeur)."""
    y = dt.date.today().year
    url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
           f"daily-treasury-rates.csv/{y}/all?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv")
    rows = list(csv.DictReader(io.StringIO(get(url))))
    best = None
    for row in rows:
        try:
            d = dt.datetime.strptime(row["Date"], "%m/%d/%Y").date().isoformat()
            v = float(row["10 Yr"])
        except (KeyError, ValueError):
            continue
        if best is None or d > best[0]:
            best = (d, v)
    if not best:
        raise ValueError("Trésor US vide")
    return best


def eurostat_hicp_euro_area():
    """Inflation zone euro sur un an (HICP), Eurostat. Le code de la zone change quand un
    pays rejoint l'euro (EA20, puis EA21 avec la Bulgarie en 2026) : on garde le plus récent."""
    best = None
    for geo in ("EA21", "EA", "EA20"):
        try:
            url = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_manr"
                   f"?format=JSON&lang=EN&geo={geo}&coicop=CP00&sinceTimePeriod={dt.date.today().year - 1}-01")
            d = json.loads(get(url))
            idx = d["dimension"]["time"]["category"]["index"]
            vals = d.get("value", {})
            have = [k for k in idx if str(idx[k]) in vals]
            if not have:
                continue
            period = max(have)
            cand = (period, float(vals[str(idx[period])]), geo)
            if best is None or cand[0] > best[0]:
                best = cand
        except Exception:  # noqa: BLE001
            continue
    if not best:
        raise ValueError("Eurostat HICP vide")
    return best


def eurostat_minr():
    """Inflation zone euro sur un an, nouveau jeu Eurostat prc_hicp_minr (ECOICOP v2, 2026).
    Renvoie (période, valeur, source)."""
    best, seen = None, set()
    for geo in ("EA", "EA21"):
        try:
            d = json.loads(get("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_minr"
                               f"?format=JSON&lang=EN&geo={geo}&unit=RCH_A&lastTimePeriod=2", timeout=40))
        except Exception:  # noqa: BLE001
            continue
        ids, sizes, dims = d["id"], d["size"], d["dimension"]
        cdim = next((i for i in ids if i.startswith("coicop") or i.startswith("ecoicop")), None)
        if not cdim:
            continue
        cats = dims[cdim]["category"]["index"]
        seen.update(list(cats)[:8])
        code = next((c for c in ("TOTAL", "CP00", "CP00_V2", "_T", "00") if c in cats), None)
        if code is None:
            continue
        times = dims["time"]["category"]["index"]
        for t in sorted(times, reverse=True):
            pos = []
            for i in ids:
                cat = dims[i]["category"]["index"]
                key = code if i == cdim else (t if i == "time" else next(iter(cat)))
                pos.append(cat[key])
            flat = 0
            for p_, sz in zip(pos, sizes):
                flat = flat * sz + p_
            v = d.get("value", {}).get(str(flat))
            if v is not None:
                cand = (t, float(v), f"Eurostat {geo} {code}")
                if best is None or cand[0] > best[0]:
                    best = cand
                break
    if not best:
        raise ValueError(f"prc_hicp_minr : indice global introuvable (postes vus : {sorted(seen)})")
    return best


def abcbourse_recent(slug="STOXXp"):
    """Dernières clôtures affichées sur abcbourse (tableau « Cours historiques »). [(date, clôture)]."""
    html = get(f"https://www.abcbourse.com/cotation/{slug}")
    pts = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S):
        cells = [re.sub(r"<.*?>", "", c).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.S)]
        if len(cells) >= 6 and re.fullmatch(r"\d{2}/\d{2}/\d{4}", cells[0]):
            try:
                d = dt.datetime.strptime(cells[0], "%d/%m/%Y").date().isoformat()
                pts.append((d, float(cells[5].replace("\u202f", "").replace(" ", "").replace(",", "."))))
            except ValueError:
                continue
    pts.sort()
    if not pts:
        raise ValueError("abcbourse illisible")
    if not 200 < pts[-1][1] < 2000:
        raise ValueError(f"valeur abcbourse incohérente ({pts[-1][1]})")
    return pts


# Record de clôture connu avant que le robot ne garde son propre historique
# (relevé par Alexis sur Investing.com, août 2026). Le robot prend le plus grand
# entre ce chiffre et ce qu'il a vu lui-même : un nouveau record sera suivi tout seul.
STOXX_RECORD_SEED = (656.86, "2026-08")

HIST = os.path.join(os.path.dirname(__file__), "..", "data", "stoxx600_history.json")


def stoxx_with_memory(recent):
    """Ajoute les clôtures récentes à l'historique gardé dans le dépôt, renvoie tout."""
    try:
        with open(HIST, encoding="utf-8") as f:
            hist = dict(json.load(f))
    except (OSError, ValueError):
        hist = {}
    hist.update(dict(recent))
    pts = sorted(hist.items())
    with open(HIST, "w", encoding="utf-8") as f:
        json.dump(pts, f, indent=0)
    return pts


def stoxx_official(code="sxxp"):
    """Historique officiel STOXX (fichier texte public). [(date, valeur)]."""
    txt = get(f"https://www.stoxx.com/document/Indices/Current/HistoricalData/h_3{code}.txt")
    out = []
    for line in txt.splitlines():
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 3:
            continue
        try:
            d = dt.datetime.strptime(parts[0], "%d.%m.%Y").date().isoformat()
            out.append((d, float(parts[2])))
        except ValueError:
            continue
    out.sort()
    if len(out) < 30:
        raise ValueError("fichier STOXX illisible")
    last_d, last_v = out[-1]
    if not 200 < last_v < 2000:
        raise ValueError(f"valeur STOXX incohérente ({last_v})")
    if (dt.date.today() - dt.date.fromisoformat(last_d)).days > 7:
        raise ValueError(f"fichier STOXX périmé ({last_d})")
    return out


def ecb_euro_aaa_10y():
    """Taux 10 ans de la courbe zone euro notée AAA (BCE, quotidien). Très proche du Bund."""
    url = ("https://data-api.ecb.europa.eu/service/data/YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y"
           "?format=csvdata&lastNObservations=1")
    for row in csv.DictReader(io.StringIO(get(url))):
        return row["TIME_PERIOD"], float(row["OBS_VALUE"])
    raise ValueError("BCE courbe AAA vide")


def bls_cpi_yoy():
    """Inflation US sur 12 mois depuis le BLS (série CUUR0000SA0, sans clé). (période, valeur)."""
    y = dt.date.today().year
    body = json.dumps({"seriesid": ["CUUR0000SA0"], "startyear": str(y - 1), "endyear": str(y)}).encode()
    req = urllib.request.Request("https://api.bls.gov/publicAPI/v1/timeseries/data/", data=body,
                                 headers={"User-Agent": UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        d = json.loads(r.read().decode())
    pts = {}
    for o in d["Results"]["series"][0]["data"]:
        if o["period"].startswith("M") and o["period"] != "M13":
            try:
                pts[(int(o["year"]), int(o["period"][1:]))] = float(o["value"])
            except ValueError:
                continue  # mois non publié (ex. shutdown d'octobre 2025)
    last = max(pts)
    prev = (last[0] - 1, last[1])
    if prev not in pts:
        raise ValueError("BLS : pas de point il y a un an")
    return f"{last[0]}-{last[1]:02d}", round((pts[last] / pts[prev] - 1) * 100, 2)


def ecb_hicp_euro_area():
    """Inflation zone euro sur un an (HICP), BCE. Les codes ont changé début 2026
    (nouvelle nomenclature) : on prend tous les postes et on garde l'indice global."""
    url = ("https://data-api.ecb.europa.eu/service/data/ICP/M.U2.N..4.ANR"
           "?format=csvdata&lastNObservations=1&detail=dataonly")
    rows = list(csv.DictReader(io.StringIO(get(url, timeout=40))))
    if not rows:
        raise ValueError("BCE HICP vide")
    latest = max(r["TIME_PERIOD"] for r in rows)
    fresh = {r["ICP_ITEM"]: r for r in rows if r["TIME_PERIOD"] == latest}
    for item in ("000000", "TOTAL", "_T", "CP00", "00"):
        if item in fresh:
            return latest, float(fresh[item]["OBS_VALUE"]), f"BCE {item}"
    raise ValueError(f"indice global introuvable, postes {latest}: {sorted(fresh)[:12]}")


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
        t0 = time.time()
        try:
            values[key] = fn()
            print(f"  ok   {key} ({time.time() - t0:.0f}s): {values[key].get('value')} [{values[key].get('source')}]", flush=True)
        except Exception as e:  # noqa: BLE001 - on veut tout attraper, la page gère l'absence
            errors[key] = f"{type(e).__name__}: {e}"[:300]
            print(f"  ÉCHEC {key} ({time.time() - t0:.0f}s): {errors[key]}", flush=True)

    # ---- US
    def vix():
        def from_cboe():
            h = cboe_history("VIX")
            d = max(h)
            return {"value": round(h[d], 2), "date": d, "source": "Cboe VIX"}

        def from_fred():
            d, v = fred_series("VIXCLS")[-1]
            return {"value": round(v, 2), "date": d, "source": "FRED VIXCLS"}
        return first_ok(from_cboe, from_fred)

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
        def from_bls():
            d, v = bls_cpi_yoy()
            return {"value": v, "date": d, "source": "BLS, CPI sur 12 mois"}

        def from_fred():
            s = fred_series("CPIAUCSL")
            d, v = s[-1]
            return {"value": round((v / s[-13][1] - 1) * 100, 2), "date": d, "source": "FRED CPIAUCSL, sur 12 mois"}
        return first_ok(from_bls, from_fred)

    def us10y():
        def from_treasury():
            d, v = treasury_10y()
            return {"value": round(v, 2), "date": d, "source": "Trésor US"}

        def from_fred():
            d, v = fred_series("DGS10")[-1]
            return {"value": round(v, 2), "date": d, "source": "FRED DGS10"}
        return first_ok(from_treasury, from_fred)

    def buffett():
        """Indicateur Buffett version Fed : valeur des actions des entreprises non financières US
        (comptes Z.1, millions $) / PIB nominal (milliards $). Trimestriel, publié avec retard.
        Ce n'est pas la même base que la version Wilshire : on le juge par rapport à sa
        propre histoire depuis 1950 (moyenne, pic de 2000, rang)."""
        gdp = dict(fred_series("GDP", start="1950-01-01"))
        eq = fred_series("NCBEILQ027S", start="1950-01-01")
        ratio = [(d, v / 1000 / gdp[d] * 100) for d, v in eq if d in gdp and gdp[d]]
        if len(ratio) < 100:
            raise ValueError("historique Fed trop court")
        d, v = ratio[-1]
        hist = [x for _, x in ratio]
        mean = sum(hist) / len(hist)
        peak2000 = max(x for dd, x in ratio if "1999" <= dd[:4] <= "2000")
        rank = round(sum(1 for x in hist if x < v) / len(hist) * 100)
        return {"value": round(v, 1), "date": d, "source": "Fed Z.1 NCBEILQ027S / PIB, trimestriel",
                "mean": round(mean, 1), "peak2000": round(peak2000, 1), "rank": rank}

    def concentration():
        return {"value": slickcharts_top10(), "date": today.isoformat(), "source": "slickcharts.com"}

    run("vix", vix)
    run("vixTerm", vix_term)
    run("hyStruct", hy_level)
    run("hyAccel", hy_accel)
    run("usCpi", us_cpi)
    run("us10y", us10y)
    run("concentration", concentration)
    run("buffett", buffett)

    # ---- Europe
    def stoxx():
        src = {}

        def official():
            src["n"] = "STOXX officiel (SXXP)"
            return stoxx_official("sxxp")

        def yahoo():
            src["n"] = "Yahoo ^STOXX"
            return yahoo_closes("^STOXX", "10y")

        def recent():
            src["n"] = "abcbourse + historique du robot"
            src["partial"] = True
            return stoxx_with_memory(abcbourse_recent("STOXXp"))
        closes = first_ok(yahoo, official, recent)
        if not src.get("partial"):
            stoxx_with_memory(closes[-30:])  # garde l'historique à jour pour les jours sans source
        b = drawdown_block(closes, today)
        b["source"] = src["n"]
        if src.get("partial"):
            seed, seed_d = STOXX_RECORD_SEED
            if seed >= b["high"]:
                b["high"], b["highDate"] = seed, seed_d
                b["value"] = round((b["last"] / seed - 1) * 100, 2)
        return b

    def bund():
        def from_ecb():
            d, v = ecb_euro_aaa_10y()
            return {"value": round(v, 2), "date": d, "source": "BCE, taux 10 ans zone euro AAA"}

        def from_fred():
            d, v = fred_series("IRLTLT01DEM156N")[-1]
            return {"value": round(v, 2), "date": d, "source": "FRED Bund 10 ans (moyenne mensuelle)"}
        return first_ok(from_ecb, from_fred)

    def eu_cpi():
        def fresh(res):
            d = res[0]
            age = (dt.date.today() - dt.date.fromisoformat(d + "-01")).days
            if age > 120:
                raise ValueError(f"série arrêtée en {d}")
            return res
        d, v, src = first_ok(lambda: fresh(eurostat_minr()), lambda: fresh(ecb_hicp_euro_area()),
                             lambda: fresh(eurostat_hicp_euro_area()))
        return {"value": round(v, 2), "date": d, "source": f"HICP zone euro sur 12 mois ({src})"}

    run("stoxxDD", stoxx)
    run("bund10y", bund)
    run("euCpi", eu_cpi)

    # ---- Asie (plus haut sur 52 semaines)
    def asia(ticker, official, label):
        def f():
            src = {}

            def off():
                src["n"] = f"{label}, source officielle"
                return official()

            def yah():
                src["n"] = f"Yahoo {ticker}"
                return yahoo_closes(ticker, "2y")
            b = drawdown_block(first_ok(off, yah), today, window_days=365)
            b["source"] = src["n"]
            return b
        return f

    run("kospiDD", asia("^KS11", naver_index, "Kospi (Naver)"))
    run("taiexDD", asia("^TWII", twse_taiex, "Taiex (TWSE)"))

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
