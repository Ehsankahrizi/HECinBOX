"""S3 / S3-compatible cloud storage helpers for HECinBOX.

Lets the pipeline load a HEC-RAS model from a cloud bucket and upload
the finished run back to the cloud - ideal when HECinBOX runs on an
AWS / Google Cloud server instead of a personal laptop.

Authentication is **environment-based only** - boto3's default
credential chain is used, in this order of preference:

  * ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``
    (plus ``AWS_SESSION_TOKEN`` for temporary credentials), or
  * an EC2 / ECS IAM role / instance profile - no keys needed at all.

``AWS_DEFAULT_REGION`` selects the region.  An optional
``S3_ENDPOINT_URL`` points at any S3-compatible service - Cloudflare
R2, MinIO, Wasabi, Backblaze B2, the GCS XML API, etc.

No credentials are ever entered through the web UI - they are supplied
to the container at ``docker run`` time or inherited from the host's
cloud identity.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse


class CloudError(RuntimeError):
    """Any cloud-storage failure - bad URI, missing creds, network."""


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """Split ``s3://bucket/some/prefix`` into ``('bucket', 'some/prefix')``."""
    uri = (uri or "").strip()
    if not uri:
        raise CloudError("Empty S3 URI.")
    parsed = urlparse(uri)
    if parsed.scheme not in ("s3", "s3a"):
        raise CloudError(
            f"URI must start with 's3://' - got '{uri}'."
        )
    bucket = parsed.netloc
    if not bucket:
        raise CloudError(f"No bucket name in URI '{uri}'.")
    key = parsed.path.lstrip("/")
    return bucket, key


def _client():
    """Build a boto3 S3 client from environment / IAM credentials.

    Falls back to **anonymous (unsigned)** access when no credentials are
    available anywhere, or when ``S3_ANONYMOUS`` is set truthy - so a
    **public** sample-model bucket can be read with zero credentials.
    Private buckets still require credentials as before.
    """
    try:
        import boto3
        from botocore import UNSIGNED
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover
        raise CloudError(
            "boto3 is not installed - cloud features are unavailable."
        ) from exc

    kwargs: dict = {}
    endpoint = os.environ.get("S3_ENDPOINT_URL", "").strip()
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    region = os.environ.get("AWS_DEFAULT_REGION", "").strip()
    if region:
        kwargs["region_name"] = region

    force_anon = os.environ.get("S3_ANONYMOUS", "").strip().lower() in (
        "1", "true", "yes", "on"
    )
    try:
        has_creds = boto3.session.Session().get_credentials() is not None
    except Exception:
        has_creds = False
    if force_anon or not has_creds:
        # Public-bucket mode: sign requests as anonymous so no keys are
        # needed (mirrors the AORC fetcher's anon=True S3 access).
        kwargs["config"] = Config(signature_version=UNSIGNED)

    try:
        return boto3.client("s3", **kwargs)
    except Exception as exc:
        raise CloudError(f"Could not create the S3 client: {exc}") from exc


def _iter_objects(client, bucket: str, prefix: str):
    """Yield every object key under a prefix (handles pagination)."""
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            yield obj["Key"]


def list_s3_model_prefixes(base_uri: str) -> list:
    """List the immediate sub-folders under an S3 base URI.

    Each sub-folder is treated as one model.  Returns a sorted list of
    ``{"name": "BaselineModel", "uri": "s3://bucket/prefix/BaselineModel/"}``.
    Used by the demo-mode model picker.
    """
    bucket, prefix = parse_s3_uri(base_uri)
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    client = _client()
    out: list = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(
        Bucket=bucket, Prefix=prefix, Delimiter="/"
    ):
        for cp in page.get("CommonPrefixes", []):
            full = cp["Prefix"]
            name = full[len(prefix):].rstrip("/")
            if name:
                out.append({"name": name, "uri": f"s3://{bucket}/{full}"})
    return sorted(out, key=lambda d: d["name"].lower())


def download_prefix(uri: str, local_dir: Path) -> int:
    """Download every object under an S3 prefix into ``local_dir``.

    The relative directory layout below the prefix is preserved.
    Returns the number of files downloaded; raises :class:`CloudError`
    if the prefix is empty or unreadable.
    """
    bucket, prefix = parse_s3_uri(uri)
    if prefix and not prefix.endswith("/"):
        prefix += "/"  # treat the prefix as a folder

    client = _client()
    local_dir = Path(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    try:
        for key in _iter_objects(client, bucket, prefix):
            if key.endswith("/"):
                continue  # directory placeholder object
            rel = key[len(prefix):] if prefix else key
            if not rel:
                continue
            dest = local_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(dest))
            count += 1
    except CloudError:
        raise
    except Exception as exc:
        raise CloudError(f"Failed downloading '{uri}': {exc}") from exc

    if count == 0:
        raise CloudError(
            f"No objects found under '{uri}'. Check the bucket name, "
            f"the path, and that your credentials grant read access."
        )
    return count


def upload_dir(local_dir: Path, uri: str) -> int:
    """Upload every file under ``local_dir`` to an S3 prefix.

    Hidden files (names starting with ``.``) are skipped so internal
    job-state markers are not published.  Returns the file count.
    """
    bucket, prefix = parse_s3_uri(uri)
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    client = _client()
    local_dir = Path(local_dir)
    files = [
        p for p in local_dir.rglob("*")
        if p.is_file()
        and not any(part.startswith(".") for part in p.relative_to(local_dir).parts)
    ]
    try:
        for path in files:
            rel = path.relative_to(local_dir).as_posix()
            client.upload_file(str(path), bucket, prefix + rel)
    except Exception as exc:
        raise CloudError(f"Failed uploading to '{uri}': {exc}") from exc
    return len(files)


def download_file(uri: str, local_path: Path) -> None:
    """Download a single S3 object to ``local_path``."""
    bucket, key = parse_s3_uri(uri)
    if not key or key.endswith("/"):
        raise CloudError(f"'{uri}' does not point at a file object.")
    client = _client()
    local_path = Path(local_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        client.download_file(bucket, key, str(local_path))
    except Exception as exc:
        raise CloudError(f"Failed downloading '{uri}': {exc}") from exc


def object_exists(uri: str) -> bool:
    """Return True if the object - or anything under the prefix - exists."""
    bucket, key = parse_s3_uri(uri)
    client = _client()
    try:
        if key and not key.endswith("/"):
            try:
                client.head_object(Bucket=bucket, Key=key)
                return True
            except Exception:
                pass  # not an exact object - fall through to a listing
        resp = client.list_objects_v2(
            Bucket=bucket, Prefix=key, MaxKeys=1
        )
        return resp.get("KeyCount", 0) > 0
    except Exception as exc:
        raise CloudError(f"Could not check '{uri}': {exc}") from exc
