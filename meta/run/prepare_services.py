import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests

from meta.common.bmclapi import (
    BMCLAPI_REQUEST_TIMEOUT_SECONDS,
    is_bmclapi_url,
    route_download_url,
)


def json_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, indent=4).encode("utf-8")


def write_json(path: Path, value) -> bytes:
    data = json_bytes(value)
    path.write_bytes(data)
    return data


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rewrite_manifest(source_url: str, service_base_url: str, manifest_key: str):
    if not is_bmclapi_url(source_url):
        return None

    manifest = None
    manifest_sources = []
    source = urlparse(source_url)
    if source.path.startswith("/v1/packages/"):
        manifest_sources.append(
            urlunparse(
                (
                    source.scheme,
                    "piston-meta.mojang.com",
                    source.path,
                    source.params,
                    source.query,
                    source.fragment,
                )
            )
        )
    manifest_sources.append(source_url)
    for manifest_source in manifest_sources:
        for attempt in range(2):
            try:
                response = requests.get(
                    manifest_source,
                    timeout=BMCLAPI_REQUEST_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                manifest = response.json()
                break
            except (requests.RequestException, ValueError):
                if attempt < 1:
                    time.sleep(attempt + 1)
        if manifest is not None:
            break
    if manifest is None:
        return None

    payload_routes = {}
    for path, entry in manifest.get("files", {}).items():
        if entry.get("type") != "file":
            continue
        raw = entry.get("downloads", {}).get("raw", {})
        original_url = raw.get("url")
        if not original_url:
            continue
        candidate_url = route_download_url(original_url)
        if candidate_url != original_url:
            token = hashlib.sha1(f"{manifest_key}:{path}".encode()).hexdigest()
            raw["url"] = f"{service_base_url}/java-runtime-payload/{token}"
            payload_routes[token] = {
                "bmclapi": candidate_url,
                "official": original_url,
            }

    if not payload_routes:
        return None

    data = json_bytes(manifest)
    return data, payload_routes


def rewrite_java_metadata(
    metadata_root: Path, service_root: Path, service_base_url: str
) -> tuple[int, int]:
    java_root = metadata_root / "net.minecraft.java"
    manifest_root = service_root / "java-runtime"
    manifest_root.mkdir(parents=True, exist_ok=True)

    service_base_url = service_base_url.rstrip("/")
    rewritten_by_source = {}
    payload_routes = {}
    rewritten_manifests = 0
    rewritten_payloads = 0

    for version_path in sorted(java_root.glob("java*.json")):
        version_data = json.loads(version_path.read_text(encoding="utf-8"))
        version_changed = False
        for runtime in version_data.get("runtimes", []):
            if (
                runtime.get("vendor") != "mojang"
                or runtime.get("downloadType") != "manifest"
            ):
                continue

            source_url = runtime.get("url")
            if not source_url or not is_bmclapi_url(source_url):
                continue

            if source_url not in rewritten_by_source:
                checksum = runtime.get("checksum", {}).get("hash")
                key = checksum or hashlib.sha1(source_url.encode()).hexdigest()
                result = rewrite_manifest(source_url, service_base_url, key)
                if result is None:
                    rewritten_by_source[source_url] = None
                else:
                    data, routes = result
                    manifest_path = manifest_root / f"{key}.json"
                    manifest_path.write_bytes(data)
                    payload_routes.update(routes)
                    rewritten_by_source[source_url] = (
                        f"{service_base_url}/java-runtime/{key}.json",
                        hashlib.sha1(data).hexdigest(),
                    )
                    rewritten_manifests += 1
                    rewritten_payloads += len(routes)

            rewritten = rewritten_by_source[source_url]
            if rewritten is None:
                continue

            local_url, manifest_sha1 = rewritten
            runtime["url"] = local_url
            runtime.setdefault("checksum", {})["hash"] = manifest_sha1
            version_changed = True

        if version_changed:
            write_json(version_path, version_data)

    java_index_path = java_root / "index.json"
    java_index = json.loads(java_index_path.read_text(encoding="utf-8"))
    for entry in java_index.get("versions", []):
        version_path = java_root / f"{entry['version']}.json"
        entry["sha256"] = sha256_file(version_path)
    write_json(java_index_path, java_index)

    root_index_path = metadata_root / "index.json"
    root_index = json.loads(root_index_path.read_text(encoding="utf-8"))
    java_component_sha256 = sha256_file(java_index_path)
    for package in root_index.get("packages", []):
        if package.get("uid") == "net.minecraft.java":
            package["sha256"] = java_component_sha256
            break
    write_json(root_index_path, root_index)

    write_json(service_root / "java-runtime-payloads.json", payload_routes)
    return rewritten_manifests, rewritten_payloads


def main():
    parser = argparse.ArgumentParser(
        description="Prepare a three-URL local Prism Metadata service root."
    )
    parser.add_argument("--metadata-root", type=Path, required=True)
    parser.add_argument("--service-root", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args()

    if not args.metadata_root.is_dir():
        parser.error(f"metadata root does not exist: {args.metadata_root}")
    if args.service_root.exists():
        parser.error(f"service root already exists: {args.service_root}")
    parsed_base_url = urlparse(args.base_url)
    if parsed_base_url.scheme not in ("http", "https") or not parsed_base_url.netloc:
        parser.error("base URL must be an absolute HTTP or HTTPS URL")

    args.service_root.mkdir(parents=True)
    service_metadata_root = args.service_root / "v1"
    shutil.copytree(args.metadata_root, service_metadata_root)
    rewritten = rewrite_java_metadata(
        service_metadata_root, args.service_root, args.base_url
    )
    print(
        "SERVICE_ROOT_READY "
        f"root={args.service_root} "
        f"java_manifests={rewritten[0]} "
        f"java_payload_routes={rewritten[1]}"
    )


if __name__ == "__main__":
    main()
