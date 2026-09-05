import os
from typing import Any
from urllib.parse import urlparse, urlunparse


BMCLAPI_BASE_URL = os.environ.get(
    "BMCLAPI_BASE_URL", "https://bmclapi2.bangbang93.com"
).rstrip("/")
BMCLAPI_REQUEST_TIMEOUT_SECONDS = 30
BMCLAPI_MAVEN_URL = f"{BMCLAPI_BASE_URL}/maven/"
BMCLAPI_FABRIC_META_URL = f"{BMCLAPI_BASE_URL}/fabric-meta"
BMCLAPI_FORGE_API_URL = f"{BMCLAPI_BASE_URL}/forge"
BMCLAPI_NEOFORGE_META_URL = f"{BMCLAPI_BASE_URL}/neoforge/meta"
BMCLAPI_MOJANG_VERSION_MANIFEST_URL = (
    f"{BMCLAPI_BASE_URL}/mc/game/version_manifest_v2.json"
)
BMCLAPI_MOJANG_JAVA_URL = (
    f"{BMCLAPI_BASE_URL}/v1/products/java-runtime/"
    "2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json"
)


_MOJANG_METADATA_HOSTS = {
    "launchermeta.mojang.com",
    "piston-meta.mojang.com",
    "piston-data.mojang.com",
    "launcher.mojang.com",
}
_MAVEN_HOSTS = {
    "libraries.minecraft.net",
    "maven.fabricmc.net",
    "maven.minecraftforge.net",
    "files.minecraftforge.net",
    "maven.neoforged.net",
    # The generator supplies patched Log4j artifacts from Maven Central.
    "repo1.maven.org",
}


def _replace_origin(url: str, path: str) -> str:
    parsed = urlparse(url)
    mirror = urlparse(BMCLAPI_BASE_URL)
    return urlunparse(
        (
            mirror.scheme,
            mirror.netloc,
            path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def _maven_path(host: str, path: str) -> str | None:
    if host == "maven.neoforged.net":
        if path == "/releases":
            path = ""
        elif path.startswith("/releases/"):
            path = path[len("/releases") :]
        else:
            return None
    elif host == "repo1.maven.org":
        if path == "/maven2":
            path = ""
        elif path.startswith("/maven2/"):
            path = path[len("/maven2") :]
        else:
            return None
    elif host == "files.minecraftforge.net":
        if path == "/maven":
            path = ""
        elif path.startswith("/maven/"):
            path = path[len("/maven") :]

    if not path:
        return "/maven/"
    if not path.startswith("/"):
        path = "/" + path
    return "/maven" + path


def route_download_url(url: str) -> str:
    """Route only known BMCLAPI-compatible download locations.

    Assets object URLs are deliberately not included: their base is selected
    by the PrismLauncher client and is outside this metadata repository.
    """

    if not url:
        return url

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if host in _MOJANG_METADATA_HOSTS:
        return _replace_origin(url, parsed.path)

    if host in _MAVEN_HOSTS:
        path = _maven_path(host, parsed.path)
        if path is not None:
            return _replace_origin(url, path)

    return url


def _route_library(library: Any) -> None:
    if library is None:
        return

    if getattr(library, "url", None):
        library.url = route_download_url(library.url)

    downloads = getattr(library, "downloads", None)
    if downloads is None:
        return

    artifact = getattr(downloads, "artifact", None)
    if artifact is not None:
        artifact.url = route_download_url(artifact.url)

    classifiers = getattr(downloads, "classifiers", None)
    if classifiers:
        for classifier in classifiers.values():
            classifier.url = route_download_url(classifier.url)


def route_meta_version_urls(version: Any) -> Any:
    """Apply the BMCLAPI routes to URL-bearing Prism model fields."""

    asset_index = getattr(version, "asset_index", None)
    if asset_index is not None:
        asset_index.url = route_download_url(asset_index.url)

    for field in (
        "main_jar",
        "libraries",
        "maven_files",
        "jar_mods",
        "java_agents",
    ):
        value = getattr(version, field, None)
        if isinstance(value, list):
            for library in value:
                _route_library(library)
        else:
            _route_library(value)

    logging = getattr(version, "logging", None)
    if logging is not None and getattr(logging, "file", None) is not None:
        logging.file.url = route_download_url(logging.file.url)

    return version
