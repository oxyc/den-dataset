"""South Korea KMRB self-rating classifications from the official public-data API.

The API is paginated XML.  A snapshot is a deterministic ZIP whose members are the unmodified response
bodies, so the manifest can vouch for exactly what the authority returned.  Only identity and the final
rating leave this adapter; the supplied synopsis and content-warning prose deliberately do not.
"""
import io
import hashlib
import os
import zipfile
import xml.etree.ElementTree as ET

from lib import http
from .contract import StageError

HOST = "apis.data.go.kr"
PATH = "/B551008/irating_v1/ir_search"
URL = f"https://{HOST}{PATH}"
PAGE = "https://www.data.go.kr/data/15142196/openapi.do"
LICENSE = PAGE
ROWS_PER_PAGE = 1000
FIRST_DATE = "20230601"


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _children(element):
    return {_local(child.tag): child for child in element}


def _text(element, name):
    child = _children(element).get(name)
    return (child.text or "").strip() if child is not None else ""


def page(payload):
    """Parse one response page and require the paging envelope and documented item schema."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as broken:
        raise StageError(f"maturity: KMRB response is not XML: {broken}") from None
    elements = {_local(element.tag): element for element in root.iter()}
    header = elements.get("header")
    header = header if header is not None else ET.Element("header")
    code = _text(header, "resultCode")
    if code != "00":
        message = _text(header, "resultMsg")
        raise StageError(f"maturity: KMRB response refused the request: {code or 'missing code'} {message}".strip())
    body = elements.get("body")
    if body is None:
        raise StageError("maturity: KMRB response has no body")
    envelope = _children(body)
    try:
        page_number = int((envelope.get("pageNo").text or "").strip())
        page_size = int((envelope.get("numOfRows").text or "").strip())
        total = int((envelope.get("totalCount").text or "").strip())
    except (AttributeError, TypeError, ValueError):
        raise StageError("maturity: KMRB paging envelope changed") from None
    items = [element for element in root.iter() if _local(element.tag) == "item"]
    required = {"wrksNm", "oriwkTtl", "fbctnYr", "rtNo", "grdNm", "direNm"}
    if items:
        published = set().union(*(_children(item) for item in items))
        missing = sorted(required - published)
        if missing:
            raise StageError(f"maturity: KMRB item schema changed; missing {missing!r}")
    return page_number, page_size, total, items


def age(value):
    """Map only thresholds that are stable numeric ages in the current Den 0...18 contract."""
    folded = "".join(str(value or "").split())
    return {"전체관람가": 0, "전체이용가": 0,
            "12세이상관람가": 12, "12세관람가": 12,
            "15세이상관람가": 15, "15세관람가": 15}.get(folded)


def _record(item):
    values = {name: (child.text or "").strip() for name, child in _children(item).items()}
    titles = [values.get(name, "") for name in ("wrksNm", "oriwkTtl")]
    rating = values.get("grdNm", "")
    runtime = values.get("screHr", "")
    return {"authority": "KMRB",
            "authorityId": f"kmrb:{values['rtNo']}" if values.get("rtNo") else None,
            "titles": [title for title in titles if title], "directors": values.get("direNm", ""),
            "year": int(values["fbctnYr"]) if values.get("fbctnYr", "").isdigit() else None,
            "raw": rating, "age": age(rating),
            "runtimeMinutes": int(runtime) if runtime.isdigit() else None,
            "matchedBy": "exact-title+year+director"}


def records(payload):
    """Read a raw-page ZIP and emit factual rows, refusing gaps, duplicates, or a moving total."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
        names = sorted(archive.namelist())
    except zipfile.BadZipFile:
        raise StageError("maturity: KMRB snapshot is not a readable ZIP") from None
    if not names:
        raise StageError("maturity: KMRB snapshot has no response pages")
    expected_total = None
    seen_ids = set()
    count = 0
    for expected_page, name in enumerate(names, 1):
        number, _size, total, items = page(archive.read(name))
        if number != expected_page or name != f"page-{expected_page:06d}.xml":
            raise StageError("maturity: KMRB snapshot pages are not contiguous")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise StageError("maturity: KMRB result count changed while paging")
        for item in items:
            record = _record(item)
            authority_id = record["authorityId"]
            if authority_id and authority_id in seen_ids:
                raise StageError(f"maturity: KMRB repeated {authority_id}")
            if authority_id:
                seen_ids.add(authority_id)
            count += 1
            yield record
    if count != expected_total:
        raise StageError(f"maturity: KMRB snapshot has {count} rows but advertised {expected_total}")


def _zip(pages):
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for number, payload in enumerate(pages, 1):
            info = zipfile.ZipInfo(f"page-{number:06d}.xml", (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    return target.getvalue()


def fetch(api_key=None, request=http.request):
    """Fetch a complete stable view; a concurrent count change is refused rather than truncated."""
    key = api_key or os.environ.get("KMRB_API_KEY")
    if not key:
        raise StageError("maturity: KMRB_API_KEY is not configured")
    pages, expected_total, count, number = [], None, 0, 1
    while expected_total is None or count < expected_total:
        payload = request(HOST, PATH, {"serviceKey": key, "pageNo": number,
                                      "numOfRows": ROWS_PER_PAGE, "stDate": FIRST_DATE},
                          headers={"Accept": "application/xml"})
        actual, _size, total, items = page(payload)
        if actual != number:
            raise StageError(f"maturity: KMRB returned page {actual} for request {number}")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise StageError("maturity: KMRB result count changed while paging")
        if not items and count < expected_total:
            raise StageError("maturity: KMRB returned an empty page before the advertised end")
        pages.append(payload)
        count += len(items)
        number += 1
    snapshot = _zip(pages)
    # Parse the finished object too: this validates its framing and exact advertised row count before cache.
    list(records(snapshot))
    return snapshot, URL


def optional_snapshot(source_snapshot, stamp, store, fetcher=None):
    """Join the shared validated-cache policy only after this optional source has a key or test fetcher."""
    if fetcher is None and not os.environ.get("KMRB_API_KEY"):
        return None, None
    return source_snapshot(fetcher or fetch, records, "KMRB", URL, stamp, store)


def manifest_entry(snapshot, scope):
    payload = snapshot["payload"]
    return {"authority": "KMRB", "dataset": "Self-rating classifications since 2023-06",
            "sourceUrl": snapshot["sourceUrl"], "landingPage": PAGE, "reuseBasis": LICENSE,
            "retrievedAt": snapshot["retrievedAt"], "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload), "scope": scope,
            **({"fallbackAfter": snapshot["fallbackAfter"]} if snapshot.get("fallbackAfter") else {})}
