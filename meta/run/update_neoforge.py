"""
Get the source files necessary for generating Forge versions
"""

import concurrent.futures
import copy
import hashlib
import json
import os
import re
import zipfile
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from pprint import pprint
import urllib.parse

from pydantic import ValidationError
import requests

from meta.common import (
    upstream_path,
    ensure_upstream_dir,
    default_session,
    remove_files,
    eprint,
    file_hash,
    get_file_sha1_from_file,
)
from meta.common.bmclapi import (
    BMCLAPI_NEOFORGE_META_URL,
    BMCLAPI_REQUEST_TIMEOUT_SECONDS,
    route_download_url,
)
from meta.common.http import download_binary_file
from meta.common.neoforge import (
    JARS_DIR,
    INSTALLER_INFO_DIR,
    INSTALLER_MANIFEST_DIR,
    VERSION_MANIFEST_DIR,
    FILE_MANIFEST_DIR,
)
from meta.model.neoforge import (
    NeoForgeFile,
    NeoForgeEntry,
    NeoForgeMCVersionInfo,
    DerivedNeoForgeIndex,
    NeoForgeVersion,
    NeoForgeInstallerProfileV2,
    InstallerInfo,
)
from meta.model.mojang import MojangVersion

UPSTREAM_DIR = upstream_path()

ensure_upstream_dir(JARS_DIR)
ensure_upstream_dir(INSTALLER_INFO_DIR)
ensure_upstream_dir(INSTALLER_MANIFEST_DIR)
ensure_upstream_dir(VERSION_MANIFEST_DIR)
ensure_upstream_dir(FILE_MANIFEST_DIR)

sess = default_session()


def find_nth(haystack, needle, n):
    start = haystack.find(needle)
    while start >= 0 and n > 1:
        start = haystack.find(needle, start + len(needle))
        n -= 1
    return start


def get_single_forge_files_manifest(longversion, artifact: str):
    print(f"Getting NeoForge manifest for {longversion}")
    path_thing = UPSTREAM_DIR + "/neoforge/files_manifests/%s.json" % longversion
    files_manifest_file = Path(path_thing)
    from_file = False
    bmcl_file_url = (
        f"{BMCLAPI_NEOFORGE_META_URL}/api/maven/details/releases/net/neoforged/"
        f"{artifact}/{urllib.parse.quote(longversion, safe='')}"
    )
    official_file_url = (
        f"https://maven.neoforged.net/api/maven/details/releases/net%2Fneoforged%2F"
        f"{artifact}%2F{urllib.parse.quote(longversion, safe='')}"
    )
    try:
        r = sess.get(bmcl_file_url, timeout=BMCLAPI_REQUEST_TIMEOUT_SECONDS)
        r.raise_for_status()
        files_json = r.json()
    except (requests.RequestException, ValueError) as error:
        eprint(
            f"BMCLAPI NeoForge manifest unavailable for {longversion}: {error}"
        )
        if files_manifest_file.is_file():
            with open(path_thing, "r") as f:
                files_json = json.load(f)
                from_file = True
        else:
            r = sess.get(official_file_url, timeout=BMCLAPI_REQUEST_TIMEOUT_SECONDS)
            r.raise_for_status()
            files_json = r.json()

    ret_dict = dict()

    for file in files_json.get("files"):
        assert type(file) == dict
        name = file["name"]
        prefix = f"{artifact}-{longversion}"
        assert name.startswith(
            prefix
        ), f"{longversion} classifier {name} doesn't start with {prefix}"
        file_name = name[len(prefix) :]
        if file_name.startswith("-"):
            file_name = file_name[1:]
        if file_name.startswith("."):
            continue

        classifier, ext = os.path.splitext(file_name)

        if ext in [".md5", ".sha1", ".sha256", ".sha512"]:
            continue

        # assert len(extensionObj.items()) == 1
        file_obj = NeoForgeFile(
            artifact=artifact, classifier=classifier, extension=ext[1:]
        )
        ret_dict[classifier] = file_obj

    if not from_file:
        Path(path_thing).parent.mkdir(parents=True, exist_ok=True)
        with open(path_thing, "w", encoding="utf-8") as f:
            json.dump(files_json, f, sort_keys=True, indent=4)

    return ret_dict


def select_neoforge_download_url(url):
    candidates = [route_download_url(url)]
    if url not in candidates:
        candidates.append(url)

    for candidate in candidates:
        try:
            response = sess.get(
                candidate + ".sha1", timeout=BMCLAPI_REQUEST_TIMEOUT_SECONDS
            )
            response.raise_for_status()
            return candidate, response.text.strip()
        except requests.RequestException as error:
            if candidate != candidates[-1]:
                eprint(
                    "BMCLAPI NeoForge checksum unavailable, "
                    f"trying official: {error}"
                )

        try:
            response = sess.head(candidate, timeout=BMCLAPI_REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            return candidate, None
        except requests.RequestException:
            pass

    return url, None


def get_neoforge_version_list(artifact):
    bmcl_url = (
        f"{BMCLAPI_NEOFORGE_META_URL}/api/maven/details/releases/net/neoforged/"
        f"{artifact}"
    )
    official_url = (
        f"https://maven.neoforged.net/api/maven/versions/releases/"
        f"net%2Fneoforged%2F{artifact}"
    )

    bmcl_versions = None
    try:
        response = sess.get(bmcl_url, timeout=BMCLAPI_REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        details = response.json()
        bmcl_versions = [
            entry["name"]
            for entry in details.get("files", [])
            if entry.get("type") == "DIRECTORY"
        ]
    except (requests.RequestException, ValueError, KeyError) as error:
        eprint(f"BMCLAPI NeoForge version list unavailable: {error}")

    try:
        response = sess.get(official_url, timeout=BMCLAPI_REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        official_versions = response.json()["versions"]
    except requests.RequestException:
        if bmcl_versions is not None:
            return bmcl_versions
        raise

    if bmcl_versions is None:
        return official_versions

    if set(bmcl_versions) == set(official_versions):
        # Keep the ordering produced by the existing official updater.
        return official_versions

    eprint(
        f"BMCLAPI NeoForge {artifact} version list differs from the official "
        "list; keeping the official version semantics"
    )
    return official_versions


def process_neoforge_version(key, entry):
    eprint("Updating NeoForge %s" % key)

    version = NeoForgeVersion(entry)
    if version.url() is None:
        eprint("Skipping %s with no valid files" % key)
        return
    if not version.uses_installer():
        eprint(f"version {version.long_version} does not use installer")
        return

    jar_path = os.path.join(UPSTREAM_DIR, JARS_DIR, version.filename())

    installer_info_path = (
        UPSTREAM_DIR + "/neoforge/installer_info/%s.json" % version.long_version
    )
    profile_path = (
        UPSTREAM_DIR + "/neoforge/installer_manifests/%s.json" % version.long_version
    )
    version_file_path = (
        UPSTREAM_DIR + "/neoforge/version_manifests/%s.json" % version.long_version
    )

    sha1_file = jar_path + ".sha1"
    fileSha1 = get_file_sha1_from_file(jar_path, sha1_file)
    download_url, new_sha1 = select_neoforge_download_url(version.url())
    if new_sha1 is not None and fileSha1 != new_sha1:
        remove_files([jar_path, profile_path, installer_info_path, sha1_file])

    installer_refresh_required = not os.path.isfile(profile_path) or not os.path.isfile(
        installer_info_path
    )

    if installer_refresh_required:
        # grab the installer if it's not there
        if not os.path.isfile(jar_path):
            eprint("Downloading %s" % download_url)
            try:
                Path(jar_path).parent.mkdir(parents=True, exist_ok=True)
                download_binary_file(
                    sess,
                    jar_path,
                    download_url,
                    timeout=BMCLAPI_REQUEST_TIMEOUT_SECONDS,
                )
            except Exception as e:
                eprint("Failed to download %s" % version.url())
                eprint("Error is %s" % e)
                return
            if new_sha1 is None:
                download_url, new_sha1 = select_neoforge_download_url(version.url())
            if new_sha1 is not None:  # this is in case the fetch failed
                with open(sha1_file, "w") as file:
                    file.write(new_sha1)

    eprint("Processing %s" % version.url())
    # harvestables from the installer
    if not os.path.isfile(profile_path):
        print(jar_path)
        with zipfile.ZipFile(jar_path) as jar:
            with suppress(KeyError):
                with jar.open("version.json") as profile_zip_entry:
                    version_data = profile_zip_entry.read()

                    # Process: does it parse?
                    MojangVersion.parse_raw(version_data)

                    Path(version_file_path).parent.mkdir(parents=True, exist_ok=True)
                    with open(version_file_path, "wb") as versionJsonFile:
                        versionJsonFile.write(version_data)
                        versionJsonFile.close()

            with jar.open("install_profile.json") as profile_zip_entry:
                install_profile_data = profile_zip_entry.read()

                # Process: does it parse?
                is_parsable = False
                exception = None
                try:
                    NeoForgeInstallerProfileV2.parse_raw(install_profile_data)
                    is_parsable = True
                except ValidationError as err:
                    exception = err

                if not is_parsable:
                    if version.is_supported():
                        raise exception
                    else:
                        eprint(
                            "Version %s is not supported and won't be generated later."
                            % version.long_version
                        )

                Path(profile_path).parent.mkdir(parents=True, exist_ok=True)
                with open(profile_path, "wb") as profileFile:
                    profileFile.write(install_profile_data)
                    profileFile.close()

    # installer info v1
    if not os.path.isfile(installer_info_path):
        installer_info = InstallerInfo()
        installer_info.sha1hash = file_hash(jar_path, hashlib.sha1)
        installer_info.sha256hash = file_hash(jar_path, hashlib.sha256)
        installer_info.size = os.path.getsize(jar_path)
        installer_info.write(installer_info_path)


def main():
    # get the 1.20.1 remote version list fragments
    main_json = get_neoforge_version_list("forge")
    assert type(main_json) == list

    # get the new remote version list fragments
    new_main_json = get_neoforge_version_list("neoforge")
    assert type(new_main_json) == list

    main_json += new_main_json

    new_index = DerivedNeoForgeIndex()

    # let's keep the regex here to remove the 1.20.1-
    version_expression = re.compile(
        r"^(?P<mc>[0-9a-zA-Z_\.]+)-(?P<ver>[0-9\.]+\.(?P<build>[0-9]+))(-(?P<branch>[a-zA-Z0-9\.]+))?$"
    )

    print("")
    print("Processing versions:")
    for long_version in main_json:
        assert type(long_version) == str

        legacyMatch = version_expression.match(long_version)
        if legacyMatch:
            version = legacyMatch.group("ver")
            artifact = "forge"
        else:
            version = long_version
            artifact = "neoforge"

        try:
            files = get_single_forge_files_manifest(long_version, artifact)
        except:
            continue

        # TODO: what *is* recommended?
        is_recommended = False

        entry = NeoForgeEntry(
            artifact=artifact,
            long_version=long_version,
            version=version,
            # NOTE: we add this later after the fact. The forge promotions file lies about these.
            latest=False,
            recommended=is_recommended,
            files=files,
        )
        new_index.versions[long_version] = entry

        if entry.recommended:
            new_index.recommended = long_version

    print("")
    print("Dumping index files...")

    with open(
        UPSTREAM_DIR + "/neoforge/maven-metadata.json", "w", encoding="utf-8"
    ) as f:
        json.dump(main_json, f, sort_keys=True, indent=4)

    new_index.write(UPSTREAM_DIR + "/neoforge/derived_index.json")

    print("Grabbing installers and dumping installer profiles...")
    # get the installer jars - if needed - and get the installer profiles out of them
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = [
            executor.submit(process_neoforge_version, key, entry)
            for key, entry in new_index.versions.items()
        ]
        for f in futures:
            f.result()


if __name__ == "__main__":
    main()
