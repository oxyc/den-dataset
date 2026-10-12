"""Taiwan BAMID film classifications from the official open-data yearly CSV archive."""
import csv
import hashlib
import io
import json
import re
import urllib.parse
import zipfile

from lib import http
from .contract import StageError

INDEX_HOST = "www.bamid.gov.tw"
INDEX_PATH = "/OpenData.aspx"
INDEX_PARAMS = {"SN": "E10C6A5C3B9BD8C8"}
INDEX = f"https://{INDEX_HOST}{INDEX_PATH}?SN={INDEX_PARAMS['SN']}"
FILE_HOST = "file.moc.gov.tw"
MIN_ARCHIVE_FILES = 10
PAGE = "https://data.gov.tw/en/datasets/59820"
LICENSE = "https://data.gov.tw/license"
HEADERS = ("年度", "分級證明字號", "級別", "中文片名", "原文片名", "國別", "語言", "出品公司", "映演時間")
LEGACY_HEADERS = ("年度", "分級證明字號", "級別", "中文片名/外文片名", "國別/語別", "出品公司", "映演時間")


def age(value):
    return {"普": 0, "護": 6, "輔": 12, "輔12": 12, "輔15": 15, "限": 18}.get(str(value or "").strip())


def _runtime(value):
    match = re.fullmatch(
        r"\s*(?:(\d+)\s*(?:小時|時))?\s*(?:(\d+)\s*(?:分鐘|分))?\s*(?:(\d+)\s*秒)?\s*",
        value or "")
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 60 + minutes + seconds // 60


def _titles(row, headers):
    if headers == HEADERS:
        candidates = [title for key in ("中文片名", "原文片名") if (title := row[key].strip())]
    else:
        # BAMID's 2015--2020 exports put the titles on separate lines in one column.
        candidates = [title.strip() for title in row["中文片名/外文片名"].splitlines() if title.strip()]
    return list(dict.fromkeys(candidates))


def _authority_id(year, certificate, titles):
    if not year or not certificate or not titles:
        return None
    # The export omits the certificate series, so its displayed number repeats for unrelated films.
    # A title correction intentionally changes this ID and forces the checked-in link to be reviewed.
    fingerprint = hashlib.sha256("\0".join(titles).encode()).hexdigest()[:16]
    return f"bamid:{year}:{certificate}:{fingerprint}"


def records(payload):
    """Emit certificate facts; absent production year/director forces reviewed-ID matching."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
        names = sorted(archive.namelist())
    except zipfile.BadZipFile:
        raise StageError("maturity: BAMID snapshot is not a readable ZIP") from None
    if not names:
        raise StageError("maturity: BAMID snapshot has no yearly CSV files")
    grouped = {}
    for name in names:
        try:
            rows = csv.DictReader(io.StringIO(archive.read(name).decode("utf-8-sig")))
        except (KeyError, UnicodeDecodeError):
            raise StageError(f"maturity: BAMID snapshot member {name!r} is unreadable") from None
        headers = tuple(rows.fieldnames or ())
        if headers not in (HEADERS, LEGACY_HEADERS):
            raise StageError(f"maturity: BAMID columns changed in {name!r}: {rows.fieldnames!r}")
        for row in rows:
            year = row["年度"].strip()
            certificate = row["分級證明字號"].strip()
            titles = _titles(row, headers)
            key = (year, certificate, tuple(titles))
            entry = grouped.setdefault(key, {"ratings": set(), "runtimes": set()})
            entry["ratings"].add(row["級別"].strip())
            entry["runtimes"].add(row["映演時間"].strip())
    for (year, certificate, title_key), entry in sorted(grouped.items()):
        ratings = entry["ratings"]
        runtimes = entry["runtimes"]
        rating = next(iter(ratings)) if len(ratings) == 1 else "|".join(sorted(ratings))
        yield {"authority": "BAMID", "authorityId": _authority_id(year, certificate, title_key),
               "titles": list(title_key),
               "directors": "", "year": None, "raw": rating,
               "age": age(rating) if len(ratings) == 1 else None,
               "runtimeMinutes": _runtime(next(iter(runtimes))) if len(runtimes) == 1 else None,
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
    index = request(INDEX_HOST, INDEX_PATH, INDEX_PARAMS, headers={"Accept": "application/json"})
    try:
        attachments = json.loads(index.decode("utf-8-sig"))[0]["相關檔案"]
    except (UnicodeDecodeError, ValueError, KeyError, IndexError, TypeError):
        raise StageError("maturity: BAMID open-data index changed") from None
    urls = re.findall(r"\((https://[^)]+\.csv)\)", attachments)
    if len(urls) < MIN_ARCHIVE_FILES:
        raise StageError(f"maturity: BAMID open-data index has only {len(urls)} yearly CSV files")
    files = []
    for url in urls:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != FILE_HOST:
            raise StageError("maturity: BAMID open-data index advertised an unexpected CSV host")
        params = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        payload = request(parsed.netloc, parsed.path, params,
                          headers={"Accept": "text/csv"})
        files.append((parsed.path.rsplit("/", 1)[-1], payload))
    snapshot = _zip(files)
    list(records(snapshot))
    return snapshot, INDEX


def manifest_entry(snapshot, scope):
    payload = snapshot["payload"]
    return {"authority": "BAMID", "dataset": "Film classifications, yearly certificate archive",
            "sourceUrl": snapshot["sourceUrl"], "landingPage": PAGE, "reuseBasis": LICENSE,
            "retrievedAt": snapshot["retrievedAt"], "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload), "scope": scope,
            **({"fallbackAfter": snapshot["fallbackAfter"]} if snapshot.get("fallbackAfter") else {})}
