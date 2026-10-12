"""Norwegian Medietilsynet film classifications from the official XLSX export."""
import hashlib
import io
import re
import unicodedata
import zipfile
import xml.etree.ElementTree as ET

from lib import http
from .contract import StageError

HOST = "www.medietilsynet.no"
PATH = "/contentassets/afe645f8781745e484a8f51d5e50a4ad/filmdatabasen.xlsx"
URL = f"https://{HOST}{PATH}"
PAGE = "https://www.medietilsynet.no/filmdatabasen/"
LICENSE = "https://lovdata.no/lov/2006-05-19-16/%C2%A77"
XML = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
HEADERS = ("Tittel", "Originaltittel", "Vurdert dato", "Aldersgrense", "Begrunnelse", "Distributør",
           "Regissør", "Prod.år", "Prod.land", "Lengde", "Handling", "Annen info")


def _normalized(value):
    if not isinstance(value, str):
        return None
    text = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    return text or None


def _column(reference):
    match = re.match(r"[A-Z]+", reference or "")
    if not match:
        raise ValueError(f"invalid XLSX cell reference {reference!r}")
    number = 0
    for letter in match.group():
        number = number * 26 + ord(letter) - 64
    return number - 1


def xlsx_rows(payload):
    """Yield workbook rows using only the stdlib; blank cells retain their declared column."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
        shared = []
        with archive.open("xl/sharedStrings.xml") as source:
            for _event, element in ET.iterparse(source, events=("end",)):
                if element.tag == XML + "si":
                    shared.append("".join(part.text or "" for part in element.iter(XML + "t")))
                    element.clear()
        with archive.open("xl/worksheets/sheet1.xml") as source:
            for _event, element in ET.iterparse(source, events=("end",)):
                if element.tag != XML + "row":
                    continue
                cells = {}
                for cell in element.findall(XML + "c"):
                    value = cell.find(XML + "v")
                    raw = "" if value is None else value.text or ""
                    cells[_column(cell.get("r"))] = shared[int(raw)] if cell.get("t") == "s" and raw else raw
                yield [cells.get(index) for index in range(max(cells, default=-1) + 1)]
                element.clear()
    except (KeyError, ValueError, zipfile.BadZipFile, ET.ParseError) as broken:
        raise StageError(f"maturity: Medietilsynet workbook is not a readable XLSX: {broken}") from None


def age(raw):
    text = _normalized(raw)
    if text == "tillatt for alle":
        return 0
    match = re.fullmatch(r"(\d{1,2}) år", text or "")
    if not match:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= 18 else None


def _integer(value):
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def records(payload):
    rows = iter(xlsx_rows(payload))
    header = next(rows, None)
    if header != list(HEADERS):
        raise StageError(f"maturity: Medietilsynet columns changed: {header!r}")
    for number, row in enumerate(rows, 2):
        row += [None] * (len(HEADERS) - len(row))
        title, original, assessed, rating, _reason, _distributor, director, year, country, runtime = row[:10]
        identity = "\x1f".join(str(value or "") for value in
                               (title, original, assessed, rating, director, year, runtime))
        yield {"authority": "Medietilsynet",
               "authorityId": "xlsx:" + hashlib.sha256(identity.encode()).hexdigest()[:20],
               "row": number, "titles": [value for value in (title, original) if _normalized(value)],
               "assessed": assessed, "raw": rating, "age": age(rating), "directors": director,
               "year": _integer(year), "country": country, "runtimeMinutes": _integer(runtime),
               "matchedBy": "exact-title+year+director"}


def fetch():
    return http.request(HOST, PATH,
                        headers={"Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"})


def manifest_entry(snapshot, scope):
    payload = snapshot["payload"]
    return {"authority": "Medietilsynet", "dataset": "Filmdatabasen 1913–2022",
            "sourceUrl": snapshot["sourceUrl"], "landingPage": PAGE, "reuseBasis": LICENSE,
            "retrievedAt": snapshot["retrievedAt"], "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload), "scope": scope,
            **({"fallbackAfter": snapshot["fallbackAfter"]} if snapshot.get("fallbackAfter") else {})}
