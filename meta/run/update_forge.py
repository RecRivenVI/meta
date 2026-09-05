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
from urllib.parse import quote

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
    BMCLAPI_FORGE_API_URL,
    BMCLAPI_MAVEN_URL,
    BMCLAPI_REQUEST_TIMEOUT_SECONDS,
    route_download_url,
)
from meta.common.forge import (
    JARS_DIR,
    INSTALLER_INFO_DIR,
    INSTALLER_MANIFEST_DIR,
    VERSION_MANIFEST_DIR,
    FILE_MANIFEST_DIR,
    BAD_VERSIONS,
    LEGACYINFO_FILE,
)
from meta.model.forge import (
    ForgeFile,
    ForgeEntry,
    ForgeMCVersionInfo,
    ForgeLegacyInfoList,
    DerivedForgeIndex,
    ForgeVersion,
    ForgeInstallerProfile,
    ForgeInstallerProfileV2,
    InstallerInfo,
    ForgeLegacyInfo,
)
from meta.common.http import download_binary_file
from meta.model.mojang import MojangVersion

UPSTREAM_DIR = upstream_path()

ensure_upstream_dir(JARS_DIR)
ensure_upstream_dir(INSTALLER_INFO_DIR)
ensure_upstream_dir(INSTALLER_MANIFEST_DIR)
ensure_upstream_dir(VERSION_MANIFEST_DIR)
ensure_upstream_dir(FILE_MANIFEST_DIR)

LEGACYINFO_PATH = os.path.join(UPSTREAM_DIR, LEGACYINFO_FILE)

sess = default_session()


BMCL_FORGE_MAVEN_METADATA_URL = (
    f"{BMCLAPI_MAVEN_URL}net/minecraftforge/forge/maven-metadata.json"
)
OFFICIAL_FORGE_MAVEN_METADATA_URL = (
    "https://files.minecraftforge.net/net/minecraftforge/forge/maven-metadata.json"
)
BMCL_FORGE_PROMOTIONS_URL = (
    f"{BMCLAPI_MAVEN_URL}net/minecraftforge/forge/promotions_slim.json"
)
OFFICIAL_FORGE_PROMOTIONS_URL = (
    "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
)

bmcl_forge_builds = {}


def get_json_with_fallback(preferred_url, fallback_url):
    preferred = None
    try:
        response = sess.get(
            preferred_url, timeout=BMCLAPI_REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        preferred = response.json()
    except (requests.RequestException, ValueError) as error:
        eprint(f"BMCLAPI request failed, trying official Forge source: {error}")

    try:
        response = sess.get(
            fallback_url, timeout=BMCLAPI_REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        fallback = response.json()
    except (requests.RequestException, ValueError):
        if preferred is not None:
            return preferred
        raise

    if preferred is not None and preferred == fallback:
        return preferred

    if preferred is not None:
        eprint(
            "BMCLAPI Forge index differs from the official index; "
            "keeping the official version semantics"
        )
    return fallback


def get_bmcl_forge_builds(mc_version):
    if mc_version in bmcl_forge_builds:
        return bmcl_forge_builds[mc_version]

    url = f"{BMCLAPI_FORGE_API_URL}/minecraft/{quote(mc_version, safe='')}"
    response = sess.get(url, timeout=BMCLAPI_REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    builds = response.json()
    if not isinstance(builds, list):
        raise ValueError(f"Unexpected BMCLAPI Forge build list for {mc_version}")
    bmcl_forge_builds[mc_version] = builds
    return builds


def get_single_forge_files_manifest(longversion, mc_version=None):
    print(f"Getting Forge manifest for {longversion}")
    path_thing = UPSTREAM_DIR + "/forge/files_manifests/%s.json" % longversion
    files_manifest_file = Path(path_thing)
    from_file = False
    files_json = None

    mc_version = mc_version or longversion.split("-", 1)[0]
    try:
        builds = get_bmcl_forge_builds(mc_version)
        build = None
        for candidate in builds:
            branch = candidate.get("branch")
            candidate_longversion = "%s-%s" % (
                candidate.get("mcversion"),
                candidate.get("version"),
            )
            if branch:
                candidate_longversion += "-%s" % branch
            if candidate_longversion == longversion:
                build = candidate
                break

        if build is None:
            raise ValueError(f"BMCLAPI has no Forge build {longversion}")

        classifiers = {}
        for file in build.get("files", []):
            classifier = file.get("category")
            extension = file.get("format")
            file_hash = file.get("hash")
            if classifier and extension and file_hash:
                classifiers.setdefault(classifier, {})[extension] = file_hash
        if not classifiers:
            raise ValueError(f"BMCLAPI has no Forge files for {longversion}")

        # Adapt BMCLAPI's build response to the existing Forge meta.json shape.
        files_json = {"classifiers": classifiers}
    except (requests.RequestException, ValueError, KeyError) as error:
        eprint(f"BMCLAPI Forge manifest unavailable for {longversion}: {error}")
        if files_manifest_file.is_file():
            with open(path_thing, "r") as f:
                files_json = json.load(f)
            from_file = True
        else:
            file_url = (
                "https://files.minecraftforge.net/net/minecraftforge/forge/%s/meta.json"
                % longversion
            )
            r = sess.get(file_url, timeout=BMCLAPI_REQUEST_TIMEOUT_SECONDS)
            r.raise_for_status()
            files_json = r.json()

    ret_dict = dict()

    for classifier, extensionObj in files_json.get("classifiers").items():
        assert type(classifier) == str
        assert type(extensionObj) == dict

        # assert len(extensionObj.items()) == 1
        index = 0
        count = 0
        while index < len(extensionObj.items()):
            mutable_copy = copy.deepcopy(extensionObj)
            extension, hashtype = mutable_copy.popitem()
            if not type(classifier) == str:
                pprint(classifier)
                pprint(extensionObj)
            if not type(hashtype) == str:
                pprint(classifier)
                pprint(extensionObj)
                print(
                    "%s: Skipping missing hash for extension %s:"
                    % (longversion, extension)
                )
                index += 1
                continue
            assert type(classifier) == str
            processed_hash = re.sub(r"\W", "", hashtype)
            if len(processed_hash) not in (32, 40):
                print(
                    "%s: Skipping invalid hash for extension %s:"
                    % (longversion, extension)
                )
                pprint(extensionObj)
                index += 1
                continue

            file_obj = ForgeFile(
                classifier=classifier, hash=processed_hash, extension=extension
            )
            if count == 0:
                ret_dict[classifier] = file_obj
                index += 1
                count += 1
            else:
                print(
                    "%s: Multiple objects detected for classifier %s:"
                    % (longversion, classifier)
                )
                pprint(extensionObj)
                assert False

    if not from_file:
        with open(path_thing, "w", encoding="utf-8") as f:
            json.dump(files_json, f, sort_keys=True, indent=4)

    return ret_dict


def select_forge_download_url(url):
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
                eprint(f"BMCLAPI Forge checksum unavailable, trying official: {error}")

        try:
            response = sess.head(candidate, timeout=BMCLAPI_REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            return candidate, None
        except requests.RequestException:
            pass

    return url, None


def process_forge_version(version, jar_path):
    installer_info_path = (
        UPSTREAM_DIR + "/forge/installer_info/%s.json" % version.long_version
    )
    profile_path = (
        UPSTREAM_DIR + "/forge/installer_manifests/%s.json" % version.long_version
    )
    version_file_path = (
        UPSTREAM_DIR + "/forge/version_manifests/%s.json" % version.long_version
    )

    sha1_file = jar_path + ".sha1"
    fileSha1 = get_file_sha1_from_file(jar_path, sha1_file)
    download_url, new_sha1 = select_forge_download_url(version.url())
    if new_sha1 is not None and fileSha1 != new_sha1:
        remove_files([jar_path, profile_path, installer_info_path, sha1_file])

    installer_refresh_required = not os.path.isfile(profile_path) or not os.path.isfile(
        installer_info_path
    )

    if installer_refresh_required:
        # grab the installer if it's not there
        if not os.path.isfile(jar_path):
            eprint("Downloading %s" % download_url)
            download_binary_file(
                sess,
                jar_path,
                download_url,
                timeout=BMCLAPI_REQUEST_TIMEOUT_SECONDS,
            )
            if new_sha1 is None:
                download_url, new_sha1 = select_forge_download_url(version.url())
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

                    try:
                        # Process: does it parse?
                        MojangVersion.parse_raw(version_data)
                    except Exception as e:
                        e.add_note(f"version_data: {version_data}")
                        raise e

                    with open(version_file_path, "wb") as versionJsonFile:
                        versionJsonFile.write(version_data)
                        versionJsonFile.close()

            with jar.open("install_profile.json") as profile_zip_entry:
                install_profile_data = profile_zip_entry.read()

                # Process: does it parse?
                is_parsable = False
                exception = None
                try:
                    ForgeInstallerProfile.parse_raw(install_profile_data)
                    is_parsable = True
                except ValidationError as err:
                    exception = err
                try:
                    ForgeInstallerProfileV2.parse_raw(install_profile_data)
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
    # get the remote version list fragments
    main_json = get_json_with_fallback(
        BMCL_FORGE_MAVEN_METADATA_URL,
        OFFICIAL_FORGE_MAVEN_METADATA_URL,
    )
    assert type(main_json) == dict

    promotions_json = get_json_with_fallback(
        BMCL_FORGE_PROMOTIONS_URL,
        OFFICIAL_FORGE_PROMOTIONS_URL,
    )
    assert type(promotions_json) == dict

    promoted_key_expression = re.compile(
        "(?P<mc>[^-]+)-(?P<promotion>(latest)|(recommended))(-(?P<branch>[a-zA-Z0-9\\.]+))?"
    )

    recommended_set = set()

    new_index = DerivedForgeIndex()

    # FIXME: does not fully validate that the file has not changed format
    # NOTE: For some insane reason, the format of the versions here is special. It having a branch at the end means it
    #           affects that particular branch.
    #       We don't care about Forge having branches.
    #       Therefore we only use the short version part for later identification and filter out the branch-specific
    #           promotions (among other errors).
    print("Processing promotions:")
    for promoKey, shortversion in promotions_json.get("promos").items():
        match = promoted_key_expression.match(promoKey)
        if not match:
            print("Skipping promotion %s, the key did not parse:" % promoKey)
            pprint(promoKey)
            assert match
        if not match.group("mc"):
            print(
                "Skipping promotion %s, because it has no Minecraft version." % promoKey
            )
            continue
        if match.group("branch"):
            print("Skipping promotion %s, because it on a branch only." % promoKey)
            continue
        elif match.group("promotion") == "recommended":
            recommended_set.add(shortversion)
            print("%s added to recommended set" % shortversion)
        elif match.group("promotion") == "latest":
            pass
        else:
            assert False

    version_expression = re.compile(
        "^(?P<mc>[0-9a-zA-Z_\\.]+)-(?P<ver>[0-9\\.]+\\.(?P<build>[0-9]+))(-(?P<branch>[a-zA-Z0-9\\.]+))?$"
    )

    print("")
    print("Processing versions:")
    for mc_version, value in main_json.items():
        assert type(mc_version) == str
        assert type(value) == list
        for long_version in value:
            assert type(long_version) == str
            match = version_expression.match(long_version)
            if not match:
                pprint(long_version)
                assert match
            assert match.group("mc") == mc_version

            files = get_single_forge_files_manifest(long_version, mc_version)

            build = int(match.group("build"))
            version = match.group("ver")
            branch = match.group("branch")

            is_recommended = version in recommended_set

            entry = ForgeEntry(
                long_version=long_version,
                mc_version=mc_version,
                version=version,
                build=build,
                branch=branch,
                # NOTE: we add this later after the fact. The forge promotions file lies about these.
                latest=False,
                recommended=is_recommended,
                files=files,
            )
            new_index.versions[long_version] = entry
            if not new_index.by_mc_version:
                new_index.by_mc_version = dict()
            if mc_version not in new_index.by_mc_version:
                new_index.by_mc_version.setdefault(mc_version, ForgeMCVersionInfo())
            new_index.by_mc_version[mc_version].versions.append(long_version)
            # NOTE: we add this later after the fact. The forge promotions file lies about these.
            # if entry.latest:
            # new_index.by_mc_version[mc_version].latest = long_version
            if entry.recommended:
                new_index.by_mc_version[mc_version].recommended = long_version

    print("")
    print("Post processing promotions and adding missing 'latest':")
    for mc_version, info in new_index.by_mc_version.items():
        latest_version = info.versions[-1]
        info.latest = latest_version
        new_index.versions[latest_version].latest = True
        print("Added %s as latest for %s" % (latest_version, mc_version))

    print("")
    print("Dumping index files...")

    with open(UPSTREAM_DIR + "/forge/maven-metadata.json", "w", encoding="utf-8") as f:
        json.dump(main_json, f, sort_keys=True, indent=4)

    with open(UPSTREAM_DIR + "/forge/promotions_slim.json", "w", encoding="utf-8") as f:
        json.dump(promotions_json, f, sort_keys=True, indent=4)

    new_index.write(UPSTREAM_DIR + "/forge/derived_index.json")

    legacy_info_list = ForgeLegacyInfoList()

    print("Grabbing installers and dumping installer profiles...")
    # get the installer jars - if needed - and get the installer profiles out of them
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = []
        for key, entry in new_index.versions.items():
            eprint("Updating Forge %s" % key)
            if entry.mc_version is None:
                eprint("Skipping %d with invalid MC version" % entry.build)
                continue

            version = ForgeVersion(entry)
            if version.url() is None:
                eprint("Skipping %d with no valid files" % version.build)
                continue
            if version.long_version in BAD_VERSIONS:
                eprint(f"Skipping bad version {version.long_version}")
                continue

            jar_path = os.path.join(UPSTREAM_DIR, JARS_DIR, version.filename())

            if version.uses_installer():
                futures.append(
                    executor.submit(process_forge_version, version, jar_path)
                )
            else:
                # ignore the two versions without install manifests and jar mod class files
                # TODO: fix those versions?
                if version.mc_version_sane == "1.6.1":
                    continue

                # only gather legacy info if it's missing
                if not os.path.isfile(LEGACYINFO_PATH):
                    # grab the jar/zip if it's not there
                    if not os.path.isfile(jar_path):
                        download_url, _ = select_forge_download_url(version.url())
                        download_binary_file(
                            sess,
                            jar_path,
                            download_url,
                            timeout=BMCLAPI_REQUEST_TIMEOUT_SECONDS,
                        )
                    # find the latest timestamp in the zip file
                    tstamp = datetime.fromtimestamp(0)
                    with zipfile.ZipFile(jar_path) as jar:
                        for info in jar.infolist():
                            tstamp_new = datetime(*info.date_time)
                            if tstamp_new > tstamp:
                                tstamp = tstamp_new
                    legacy_info = ForgeLegacyInfo()
                    legacy_info.release_time = tstamp
                    legacy_info.sha1 = file_hash(jar_path, hashlib.sha1)
                    legacy_info.sha256 = file_hash(jar_path, hashlib.sha256)
                    legacy_info.size = os.path.getsize(jar_path)
                    legacy_info_list.number[key] = legacy_info
        for f in futures:
            f.result()

    # only write legacy info if it's missing
    if not os.path.isfile(LEGACYINFO_PATH):
        legacy_info_list.write(LEGACYINFO_PATH)


if __name__ == "__main__":
    main()
