"""Bulk public-authority maturity datasets from France and Brazil.

Only identity and numeric classification facts leave these adapters. Descriptions, content warnings and
other prose are deliberately ignored. Both parsers require their published identity columns and fail
closed when the upstream schema changes.
"""
import csv
import hashlib
import io
import re
import unicodedata
import urllib.parse

from lib import http
from .contract import StageError

CNC_DATASET = "liste-des-visas-dexploitations-cinematographiques-delivres-de-1945-a-2025"
CNC_RESOURCE = "1c5075ec-7ce1-49cb-ab89-94f507812daf"
CNC_PAGE = f"https://www.data.gouv.fr/datasets/{CNC_DATASET}"
CNC_LICENSE = "https://www.etalab.gouv.fr/licence-ouverte-open-licence/"

BRAZIL_DATASET = "5138a6ca-8009-4ffb-b95a-052f76d62a33"
BRAZIL_RESOURCE = "527d195f-c60a-4e8a-8d36-178486ccb480"
BRAZIL_PAGE = f"https://dados.mj.gov.br/dataset/{BRAZIL_DATASET}"
BRAZIL_LICENSE = "https://creativecommons.org/licenses/by/4.0/"


def _text(payload):
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise StageError("maturity: authority CSV is neither UTF-8 nor Windows-1252")


def _key(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(c for c in value.casefold() if c.isalnum())


def _rows(payload, required, authority):
    text = _text(payload)
    first = text.splitlines()[0] if text.splitlines() else ""
    delimiter = ";" if first.count(";") > first.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    fields = {_key(name): name for name in (reader.fieldnames or ())}
    missing = sorted(name for name in required if _key(name) not in fields)
    if missing:
        raise StageError(f"maturity: {authority} columns changed; missing {missing!r}")
    for number, row in enumerate(reader, 2):
        yield number, {_key(name): value for name, value in row.items()}


def _integer(value):
    match = re.search(r"\b\d{1,4}\b", str(value or ""))
    return int(match.group(0)) if match else None


def cnc_age(value):
    raw = str(value or "").strip()
    folded = _key(raw)
    ages = [int(found) for found in re.findall(r"interdit\s*-?\s*(\d{1,2})\s*ans", raw, re.I)]
    if "interditmineurs" in folded:
        ages.append(18)
    if ages:
        return max(age for age in ages if 0 <= age <= 18)
    return 0 if folded == "toutpublic" else None


def cnc_records(payload):
    required = ("Titre", "Réalisation", "Date", "N° de visa", "Décision")
    for number, row in _rows(payload, required, "CNC"):
        title, director = row[_key("Titre")], row[_key("Réalisation")]
        visa, decision = row[_key("N° de visa")], row[_key("Décision")]
        yield {"authority": "CNC", "authorityId": f"visa:{visa.strip()}" if visa.strip() else f"row:{number}",
               "titles": [title] if title.strip() else [], "directors": director,
               "year": _integer(row[_key("Date")]), "raw": decision, "age": cnc_age(decision),
               "matchedBy": "exact-title+year+director"}


def brazil_age(value):
    folded = _key(value)
    if folded in {"livre", "livreparatodosospublicos"}:
        return 0
    age = _integer(value)
    return age if age in {10, 12, 14, 16, 18} else None


def brazil_records(payload):
    required = ("Código", "Titulo no Brasil", "Titulo original", "Ano de produção", "Diretores",
                "Classificação Atribuída")
    for number, row in _rows(payload, required, "ClassInd"):
        code = row[_key("Código")].strip()
        titles = [row[_key(name)].strip() for name in ("Titulo no Brasil", "Titulo original")
                  if row[_key(name)].strip()]
        rating = row[_key("Classificação Atribuída")]
        yield {"authority": "ClassInd", "authorityId": f"classind:{code}" if code else f"row:{number}",
               "titles": titles, "directors": row[_key("Diretores")],
               "year": _integer(row[_key("Ano de produção")]), "raw": rating,
               "age": brazil_age(rating), "matchedBy": "exact-title+year+director"}


def _download(host, url):
    parts = urllib.parse.urlsplit(url or "")
    if parts.scheme != "https" or parts.hostname not in {host, "static.data.gouv.fr"}:
        raise StageError(f"maturity: {host} did not advertise the expected HTTPS resource")
    path = parts.path + (("?" + parts.query) if parts.query else "")
    return http.request(parts.netloc, path, headers={"Accept": "text/csv"}), url


def fetch_cnc():
    metadata = http.get_json("www.data.gouv.fr", f"/api/1/datasets/{CNC_DATASET}/")
    resources = {row.get("id"): row for row in metadata.get("resources") or ()}
    return _download("www.data.gouv.fr", (resources.get(CNC_RESOURCE) or {}).get("url"))


def fetch_brazil():
    metadata = http.get_json("dados.mj.gov.br", "/api/3/action/package_show", {"id": BRAZIL_DATASET})
    resources = {row.get("id"): row for row in (metadata.get("result") or {}).get("resources") or ()}
    return _download("dados.mj.gov.br", (resources.get(BRAZIL_RESOURCE) or {}).get("url"))


def digest(payload):
    return hashlib.sha256(payload).hexdigest()
