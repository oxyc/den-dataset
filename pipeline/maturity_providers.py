"""Registry describing every maturity authority and its acquisition boundary."""
import dataclasses
import os

from . import (artifacts, maturity_italy, maturity_kavi, maturity_kmrb, maturity_norway, maturity_public,
               maturity_quebec, maturity_singapore, maturity_taiwan)


@dataclasses.dataclass(frozen=True)
class Provider:
    authority: str
    artifact: object
    records: object
    source_url: str
    manifest_entry: object
    fetch: object = None
    acquisition: str = "remote"
    required: bool = False
    configured: object = lambda: True


def _manifest(authority, dataset, page, licence):
    def entry(snapshot, scope):
        payload = snapshot["payload"]
        return {"authority": authority, "dataset": dataset, "sourceUrl": snapshot["sourceUrl"],
                "landingPage": page, "reuseBasis": licence, "retrievedAt": snapshot["retrievedAt"],
                "sha256": maturity_public.digest(payload), "bytes": len(payload), "scope": scope,
                **({"fallbackAfter": snapshot["fallbackAfter"]}
                   if snapshot.get("fallbackAfter") else {})}
    return entry


PROVIDERS = (
    Provider("Medietilsynet", artifacts.MATURITY_NORWAY, maturity_norway.records,
             maturity_norway.URL, maturity_norway.manifest_entry, fetch=maturity_norway.fetch,
             required=True),
    Provider("CNC", artifacts.MATURITY_CNC, maturity_public.cnc_records, maturity_public.CNC_PAGE,
             _manifest("CNC", "Visas d'exploitation 1945–2025", maturity_public.CNC_PAGE,
                       maturity_public.CNC_LICENSE), fetch=maturity_public.fetch_cnc),
    Provider("ClassInd", artifacts.MATURITY_BRAZIL, maturity_public.brazil_records,
             maturity_public.BRAZIL_PAGE,
             _manifest("ClassInd", "Obras Audiovisuais", maturity_public.BRAZIL_PAGE,
                       maturity_public.BRAZIL_LICENSE), fetch=maturity_public.fetch_brazil),
    Provider("KMRB", artifacts.MATURITY_KMRB, maturity_kmrb.records,
             maturity_kmrb.URL, maturity_kmrb.manifest_entry, fetch=maturity_kmrb.fetch,
             configured=lambda: bool(os.environ.get("KMRB_API_KEY"))),
    Provider("BAMID", artifacts.MATURITY_TAIWAN, maturity_taiwan.records,
             maturity_taiwan.INDEX, maturity_taiwan.manifest_entry, fetch=maturity_taiwan.fetch),
    Provider("IMDA", artifacts.MATURITY_SINGAPORE, maturity_singapore.records,
             maturity_singapore.API, maturity_singapore.manifest_entry,
             fetch=maturity_singapore.fetch),
    Provider(maturity_quebec.AUTHORITY, artifacts.MATURITY_QUEBEC, maturity_quebec.records,
             maturity_quebec.API, maturity_quebec.manifest_entry, fetch=maturity_quebec.fetch),
    Provider(maturity_italy.AUTHORITY, artifacts.MATURITY_ITALY, maturity_italy.records,
             maturity_italy.INDEX, maturity_italy.manifest_entry, fetch=maturity_italy.fetch),
    Provider("KAVI", artifacts.MATURITY_KAVI, maturity_kavi.records,
             maturity_kavi.API, maturity_kavi.manifest_entry, acquisition="local"),
)


BY_AUTHORITY = {provider.authority: provider for provider in PROVIDERS}
REMOTE = tuple(provider for provider in PROVIDERS if provider.acquisition == "remote")
