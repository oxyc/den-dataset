"""Italian DGCA film classifications from the ministry's complete yearly CSV exports."""
import csv
import hashlib
import io
import re
import zipfile

from lib import http
from .contract import StageError

AUTHORITY = "DGCA-Italy"
HOST = "cinema.cultura.gov.it"
INDEX_PATH = "/database-nuova-classificazione/"
INDEX = f"https://{HOST}{INDEX_PATH}"
DOWNLOAD_PREFIX = "/tempcsv/dbclassificazione/DGCA-DB-classificazione-"
LEGAL = "https://cinema.cultura.gov.it/note-legali/"
LICENSE = "https://creativecommons.org/licenses/by/3.0/"
FIRST_YEAR = 2021
MIN_YEARS = 6
MIN_ROWS = 4500
HEADER = ("ID;TITOLO;TITOLO_ORIGINALE;RICHIEDENTE;PRODUZIONE;REGIA;DISTRIBUZIONE;"
          "NAZIONALITA;FORMATO;TIPO_EDIZIONE;MINUTAGGIO;CLASSIFICAZIONE;"
          "NUMERO_CLASSIFICAZIONE;SIGLA_SETTORE;DATA_CLASSIFICAZIONE;ID_DOMANDA_COL;"
          "VIOLENZA;SESSO;USO DI ARMI;LINGUAGGIO E TURPILOQUIO;"
          "USO DI SOSTANZE STUPEFACENTI O ALCOL;;CONTENUTI_SENSIBILI;")
HEADERS = tuple(HEADER.split(";"))
RATINGS = {
    "Opera per tutti": ("T", 0),
    "Opera non adatta ai minori di anni 6": ("6", 6),
    "Opera non adatta ai minori di anni 10": ("10", 10),
    "Opera vietata ai minori di anni 14": ("14", 14),
    "Opera vietata ai minori di anni 18": ("18", 18),
}


def _text(payload):
    """The export mixes UTF-8 sequences with legacy single-byte Italian characters."""
    decoded = payload.decode("utf-8", "surrogateescape")
    return "".join(chr(ord(char) - 0xDC00) if 0xDC80 <= ord(char) <= 0xDCFF else char
                   for char in decoded)


def _clean(value):
    value = " ".join(str(value or "").strip().split())
    if "Ã" in value:
        try:
            value = value.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return value


def _rating(value):
    label = _clean(value).split(".", 1)[0]
    return RATINGS.get(label, (label, None))


def age(value):
    return _rating(value)[1]


def _rows(payload, name):
    lines = _text(payload).splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.startswith("ID;TITOLO;"))
    except StopIteration:
        raise StageError(f"maturity: DGCA export {name!r} has no header") from None
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])), delimiter=";")
    if tuple(reader.fieldnames or ()) != HEADERS:
        raise StageError(f"maturity: DGCA columns changed in {name!r}: {reader.fieldnames!r}")
    yield from reader


def records(payload):
    """Emit factual certificates; absent production year forces reviewed-ID matching."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
        names = sorted(name for name in archive.namelist() if name.endswith(".csv"))
    except zipfile.BadZipFile:
        raise StageError("maturity: DGCA snapshot is not a readable ZIP") from None
    if len(names) < MIN_YEARS:
        raise StageError(f"maturity: DGCA snapshot has only {len(names)} yearly CSV files")
    rows = [(name, row) for name in names for row in _rows(archive.read(name), name)]
    if len(rows) < MIN_ROWS:
        raise StageError(f"maturity: DGCA exports shrank to {len(rows)} rows")
    seen = set()
    for name, row in rows:
        authority_id = _clean(row["NUMERO_CLASSIFICAZIONE"])
        if not re.fullmatch(r"C\d{14}", authority_id):
            raise StageError(f"maturity: DGCA export {name!r} has an invalid classification number")
        if authority_id in seen:
            raise StageError(f"maturity: DGCA duplicate classification number {authority_id}")
        seen.add(authority_id)
        titles = list(dict.fromkeys(title for key in ("TITOLO", "TITOLO_ORIGINALE")
                                    if (title := _clean(row[key]))))
        raw, minimum = _rating(row["CLASSIFICAZIONE"])
        runtime = _clean(row["MINUTAGGIO"])
        yield {"authority": AUTHORITY, "authorityId": authority_id, "titles": titles,
               "directors": _clean(row["REGIA"]), "year": None, "raw": raw, "age": minimum,
               "assessed": _clean(row["DATA_CLASSIFICAZIONE"]),
               "runtimeMinutes": int(runtime) if runtime.isdigit() else None,
               "matchedBy": "exact-title+year+director"}


def _zip(files):
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in sorted(files):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    return target.getvalue()


def fetch(request=None):
    request = request or http.request
    index = request(HOST, INDEX_PATH, headers={"Accept": "text/html"})
    years = sorted({int(year) for year in re.findall(rb'data-anno="(\d{4})"', index)})
    expected = list(range(FIRST_YEAR, max(years, default=0) + 1))
    if len(years) < MIN_YEARS or years != expected:
        raise StageError(f"maturity: DGCA index advertised unexpected years {years!r}")
    files = []
    for year in years:
        path = f"{DOWNLOAD_PREFIX}{year}.csv"
        files.append((f"{year}.csv", request(HOST, path, headers={"Accept": "*/*"})))
    snapshot = _zip(files)
    list(records(snapshot))
    return snapshot, INDEX


def manifest_entry(snapshot, scope):
    payload = snapshot["payload"]
    return {"authority": AUTHORITY, "dataset": "Database classificazione opere",
            "sourceUrl": snapshot["sourceUrl"], "landingPage": INDEX,
            "reuseBasis": LEGAL, "licence": LICENSE, "retrievedAt": snapshot["retrievedAt"],
            "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload), "scope": scope,
            **({"fallbackAfter": snapshot["fallbackAfter"]}
               if snapshot.get("fallbackAfter") else {})}
