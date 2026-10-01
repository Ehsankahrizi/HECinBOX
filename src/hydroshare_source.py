"""Load a HEC-RAS model from a HydroShare resource.

Tab 1 offers HydroShare as a model source next to a local folder and S3.
A user pastes a resource URL, DOI or ID; this module reads the
resource's metadata (title, authors, license, citation), finds the
folders inside it that hold a HEC-RAS project, and downloads the one the
user picks into a local folder the rest of the pipeline treats like any
other model.

Downloads and metadata go through CUAHSI's ``hsclient`` package.  The
file *listing* uses HydroShare's REST file endpoint instead:
``Resource.files(search_aggregations=True)`` parses every content
aggregation's metadata and raises on some published resources (the
Arroyo Seco resource fails on a shapefile's spatial-reference string),
while the REST listing returns every file with its size and never
parses metadata.

Public resources need no account.  Private ones are out of scope for
now: they would need a HydroShare sign-in, which this UI does not ask
for.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

import requests

HS_API = "https://www.hydroshare.org/hsapi"

_ID = r"[0-9a-f]{32}"
# Only accept an ID where HydroShare puts one: a landing-page or API path,
# a DOI suffix (10.4211/hs.<id>), or the bare ID.  A loose 32-hex search
# would also match the tracking GUIDs inside Outlook "safe links".
_ID_PATTERNS = (
    re.compile(rf"/resource/({_ID})", re.I),
    re.compile(rf"\bhs\.({_ID})\b", re.I),
    re.compile(rf"^\s*({_ID})\s*$", re.I),
)


def parse_resource_id(text: str) -> str | None:
    """The 32-character resource ID in a HydroShare URL, DOI or bare ID."""
    for pat in _ID_PATTERNS:
        m = pat.search(text or "")
        if m:
            return m.group(1).lower()
    return None


def resource_info(resource_id: str) -> dict:
    """Title, authors, license and citation of a public resource."""
    from hsclient import HydroShare

    md = HydroShare().resource(resource_id).metadata
    rights = md.rights
    return {
        "id": resource_id,
        "title": md.title,
        "authors": [c.name for c in (md.creators or []) if c.name],
        "license": (rights.statement if rights else "") or "",
        "license_url": str(rights.url) if rights and rights.url else "",
        "citation": md.citation or "",
        "url": str(md.url),
    }


def list_files(resource_id: str) -> list[dict]:
    """Every file in the resource as ``{"path", "size"}``.

    ``path`` is relative to the resource's content folder, the same form
    hsclient's ``file_download`` / ``folder_download`` take.
    """
    out: list[dict] = []
    url = f"{HS_API}/resource/{resource_id}/files/"
    while url:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        page = r.json()
        for f in page.get("results", []):
            path = f["url"].split("/data/contents/", 1)[-1]
            out.append({
                "path": requests.utils.unquote(path),
                "size": int(f.get("size") or 0),
            })
        url = page.get("next")
    return out


def find_model_folders(files: list[dict]) -> list[dict]:
    """Folders that hold a HEC-RAS project, best candidates first.

    A folder qualifies when it has ``<name>.prj`` next to a plan file
    ``<name>.pNN``; that pairing is what separates a HEC-RAS project
    file from the many shapefile ``.prj`` projections in a resource.
    ``size`` covers everything under the folder, since that is what a
    folder download fetches.
    """
    by_dir: dict[str, set[str]] = {}
    for f in files:
        p = PurePosixPath(f["path"])
        by_dir.setdefault(str(p.parent) if str(p.parent) != "." else "",
                          set()).add(p.name)

    found = []
    for folder, names in by_dir.items():
        lower = {n.lower() for n in names}
        projects = sorted(
            n[:-4] for n in names
            if n.lower().endswith(".prj")
            and any(re.fullmatch(re.escape(n[:-4].lower()) + r"\.p\d\d", x)
                    for x in lower)
        )
        if not projects:
            continue
        computed = [
            p for p in projects
            if any(re.fullmatch(re.escape(p.lower()) + r"\.p\d\d\.hdf", x)
                   for x in lower)
        ]
        prefix = folder + "/" if folder else ""
        size = sum(f["size"] for f in files
                   if not folder or f["path"].startswith(prefix))
        found.append({
            "folder": folder,
            "projects": projects,
            "computed": computed,
            "size": size,
        })
    # Computed models first (the pipeline needs a plan HDF), then by path.
    found.sort(key=lambda d: (not d["computed"], d["folder"]))
    return found


def download_model_folder(resource_id: str, folder: str, dest: Path,
                          info: dict | None = None) -> int:
    """Download one model folder of a resource into ``dest``.

    A sub-folder comes down as one zip through hsclient's
    ``folder_download``; a model at the resource root is fetched file by
    file, since the root cannot be zipped on its own.  ``dest`` is
    replaced.  Writes ``hydroshare_source.json`` (resource, folder,
    license, citation) beside the model so the provenance travels with
    it.  Returns the number of files written.
    """
    from hsclient import HydroShare

    res = HydroShare().resource(resource_id)
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    with tempfile.TemporaryDirectory() as tmp:
        if folder:
            zpath = Path(res.folder_download(folder, save_path=tmp))
            top = PurePosixPath(folder).name + "/"
            with zipfile.ZipFile(zpath) as zf:
                for m in zf.infolist():
                    if m.is_dir():
                        continue
                    rel = m.filename[len(top):] if m.filename.startswith(top) \
                        else m.filename
                    target = (dest / rel).resolve()
                    if not str(target).startswith(str(dest.resolve())):
                        continue  # zip-slip guard
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(m) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
        else:
            for f in list_files(resource_id):
                if "/" in f["path"]:
                    continue
                res.file_download(f["path"], save_path=str(dest))

    n = sum(1 for p in dest.rglob("*") if p.is_file())
    (dest / "hydroshare_source.json").write_text(json.dumps({
        **(info or {"id": resource_id}),
        "folder": folder,
    }, indent=2))
    return n
