#!/usr/bin/env python3
"""
Genera il dataset compatto dei prezzi carburante per l'app Gasolio Low Cost.

Scarica gli open data MIMIT (anagrafica impianti attivi + prezzi alle 8), li pulisce
e scrive un unico file di testo compresso con gzip, pensato per essere servito come
file statico da una CDN (es. GitHub Pages) e scaricato dall'app una volta al giorno.

Le regole di pulizia riprendono quelle documentate dal progetto Prezzi Carburante
di dstmrk (https://github.com/dstmrk/prezzi-carburante, licenza MIT):
- i nomi impianto possono contenere il separatore '|': i campi in eccesso finiscono nel nome;
- il formato MIMIT non usa il quoting: le virgolette restano testo;
- la codifica viene riconosciuta automaticamente (UTF-8, altrimenti Latin-1);
- si scartano i prezzi fuori da 0,6×–2× la mediana nazionale del carburante;
- si scartano i prezzi comunicati più di 15 giorni prima (fuso di Roma).

Formato di output (UTF-8, una riga per record, campi separati da '|'):
    #carburanti|1|<data estrazione MIMIT>|<generato il, ISO 8601 UTC>
    S|id|bandiera|nome|indirizzo|comune|provincia|latitudine|longitudine
    P|id|carburante|self(1/0)|prezzo|data comunicazione gg/mm/aaaa
Le righe S precedono le righe P. Per ogni impianto, carburante e modalità
(self/servito) c'è al più un prezzo: il più basso.

Uso: python3 build_dataset.py <cartella di output>
"""

import gzip
import json
import statistics
import sys
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

FORMAT_VERSION = 1
BASE_URL = "https://www.mimit.gov.it/images/exportCSV/"
STATIONS_URL = BASE_URL + "anagrafica_impianti_attivi.csv"
PRICES_URL = BASE_URL + "prezzo_alle_8.csv"
USER_AGENT = "GasolioLowCost-dataset/1.0 (+https://github.com/dstmrk/prezzi-carburante)"

ROME = ZoneInfo("Europe/Rome")
MAX_PRICE_AGE_DAYS = 15
MEDIAN_MIN_FACTOR = 0.6
MEDIAN_MAX_FACTOR = 2.0
# Limiti larghi del territorio italiano, per scartare coordinate assenti o errate.
LAT_RANGE = (35.0, 47.5)
LON_RANGE = (6.0, 19.0)

OUTPUT_NAME = f"carburanti-v{FORMAT_VERSION}.txt.gz"


def download(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def data_rows(text: str):
    """Restituisce (data di estrazione, righe dopo l'intestazione 'idImpianto|...')."""
    lines = text.splitlines()
    extraction = None
    for i, line in enumerate(lines):
        if line.startswith("Estrazione del"):
            extraction = line.removeprefix("Estrazione del").strip()
        elif line.startswith("idImpianto"):
            return extraction, [row for row in lines[i + 1:] if row.strip()]
    return extraction, []


def clean(value: str) -> str:
    # '|' è il separatore del formato di output: non può comparire nei campi.
    return " ".join(value.replace("|", " ").split())


def to_float(value: str):
    try:
        return float(value.strip().replace(",", "."))
    except ValueError:
        return None


def parse_stations(text: str) -> dict:
    """Restituisce {id: riga S del formato di output}."""
    _, rows = data_rows(text)
    stations = {}
    for line in rows:
        f = line.split("|")
        if len(f) < 10:
            continue
        n = len(f)
        try:
            station_id = int(f[0].strip())
        except ValueError:
            continue
        lat, lon = to_float(f[n - 2]), to_float(f[n - 1])
        if lat is None or lon is None:
            continue
        if not (LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]):
            continue
        # Campi in eccesso dovuti a '|' nel nome: i primi 4 e gli ultimi 5 sono fissi.
        name = clean(" ".join(f[4:n - 5]))
        address, municipality, province = (clean(v) for v in f[n - 5:n - 2])
        stations[station_id] = (
            f"S|{station_id}|{clean(f[2])}|{name}|{address}|{municipality}|{province}|{lat:.6f}|{lon:.6f}"
        )
    return stations


def parse_prices(text: str, stations: dict, now: datetime):
    """Restituisce (data estrazione, {(id, carburante, self): (prezzo, data)}, statistiche)."""
    extraction, rows = data_rows(text)
    oldest = now - timedelta(days=MAX_PRICE_AGE_DAYS)
    candidates = []
    by_fuel = defaultdict(list)
    stats = Counter()
    for line in rows:
        f = line.split("|")
        if len(f) < 5:
            stats["malformed"] += 1
            continue
        try:
            station_id = int(f[0].strip())
        except ValueError:
            stats["malformed"] += 1
            continue
        if station_id not in stations:
            stats["unknown_station"] += 1
            continue
        fuel = clean(f[1]).lower()
        price = to_float(f[2])
        if not fuel or price is None or price <= 0:
            stats["malformed"] += 1
            continue
        try:
            date = datetime.strptime(f[4].strip(), "%d/%m/%Y %H:%M:%S").replace(tzinfo=ROME)
        except ValueError:
            stats["bad_date"] += 1
            continue
        if date < oldest:
            stats["stale"] += 1
            continue
        candidates.append((station_id, fuel, f[3].strip() == "1", price, date))
        by_fuel[fuel].append(price)

    bounds = {}
    for fuel, fuel_prices in by_fuel.items():
        median = statistics.median(fuel_prices)
        bounds[fuel] = (MEDIAN_MIN_FACTOR * median, MEDIAN_MAX_FACTOR * median)
    best = {}
    for station_id, fuel, is_self, price, date in candidates:
        low, high = bounds[fuel]
        if not (low <= price <= high):
            stats["outlier"] += 1
            continue
        key = (station_id, fuel, is_self)
        if key not in best or price < best[key][0]:
            best[key] = (price, date)
    return extraction, best, stats


def build(output_dir: Path) -> dict:
    now = datetime.now(ROME)
    # I due file sono indipendenti: scaricarli in parallelo dimezza l'attesa sul server MIMIT.
    with ThreadPoolExecutor(max_workers=2) as pool:
        stations_text, prices_text = pool.map(download, (STATIONS_URL, PRICES_URL))
    stations = parse_stations(stations_text)
    extraction, prices, stats = parse_prices(prices_text, stations, now)
    if len(stations) < 10_000 or len(prices) < 20_000:
        # Protezione contro file MIMIT vuoti o troncati: meglio lasciare online il dataset precedente.
        raise SystemExit(f"Dataset sospetto: {len(stations)} impianti, {len(prices)} prezzi")

    with_prices = {station_id for station_id, _, _ in prices}
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [f"#carburanti|{FORMAT_VERSION}|{extraction or ''}|{generated}"]
    lines.extend(stations[station_id] for station_id in sorted(with_prices))
    for (station_id, fuel, is_self), (price, date) in sorted(prices.items()):
        lines.append(f"P|{station_id}|{fuel}|{int(is_self)}|{price:.3f}|{date:%d/%m/%Y}")

    output_dir.mkdir(parents=True, exist_ok=True)
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    # mtime=0: a parità di contenuto il file compresso è identico byte per byte.
    (output_dir / OUTPUT_NAME).write_bytes(gzip.compress(payload, compresslevel=9, mtime=0))

    meta = {
        "format": FORMAT_VERSION,
        "file": OUTPUT_NAME,
        "extraction": extraction,
        "generated": generated,
        "stations": len(with_prices),
        "prices": len(prices),
        "discarded": dict(stats),
        "source": "MIMIT – Osservatorio prezzi carburanti (open data)",
        "credits": "Regole di pulizia da Prezzi Carburante di dstmrk, https://github.com/dstmrk/prezzi-carburante (MIT)",
    }
    (output_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return meta


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(json.dumps(build(Path(sys.argv[1])), ensure_ascii=False, indent=2))
