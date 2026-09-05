from collections import deque
from multiprocessing import Pool
import json
import os
import zipfile
from datetime import datetime

import requests

from meta.common import (
    upstream_path,
    ensure_upstream_dir,
    transform_maven_key,
    default_session,
)
from meta.common.bmclapi import (
    BMCLAPI_FABRIC_META_URL,
    BMCLAPI_MAVEN_URL,
    BMCLAPI_REQUEST_TIMEOUT_SECONDS,
)
from meta.common.fabric import (
    JARS_DIR,
    INSTALLER_INFO_DIR,
    META_DIR,
    DATETIME_FORMAT_HTTP,
)
from meta.model.fabric import FabricJarInfo

UPSTREAM_DIR = upstream_path()

ensure_upstream_dir(JARS_DIR)
ensure_upstream_dir(INSTALLER_INFO_DIR)
ensure_upstream_dir(META_DIR)

sess = default_session()

OFFICIAL_FABRIC_META_URL = "https://meta.fabricmc.net"
OFFICIAL_FABRIC_MAVEN_URL = "https://maven.fabricmc.net/"


def get_maven_url(maven_key, server, ext):
    parts = maven_key.split(":", 3)
    maven_ver_url = (
        server + parts[0].replace(".", "/") + "/" + parts[1] + "/" + parts[2] + "/"
    )
    maven_url = maven_ver_url + parts[1] + "-" + parts[2] + ext
    return maven_url


def get_json_file(path, url, fallback_url=None):
    candidates = [url]
    if fallback_url is not None and fallback_url not in candidates:
        candidates.append(fallback_url)

    last_error = None
    for candidate in candidates:
        try:
            r = sess.get(candidate, timeout=BMCLAPI_REQUEST_TIMEOUT_SECONDS)
            r.raise_for_status()
            version_json = r.json()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(version_json, f, sort_keys=True, indent=4)
            return version_json
        except (requests.RequestException, ValueError) as error:
            last_error = error
            if candidate != candidates[-1]:
                print(f"BMCLAPI Fabric request failed, trying official: {candidate}")

    assert last_error is not None
    raise last_error


def head_file(url):
    r = sess.head(url, timeout=BMCLAPI_REQUEST_TIMEOUT_SECONDS)
    r.raise_for_status()
    return r.headers


def get_binary_file(path, url):
    r = sess.get(url, timeout=BMCLAPI_REQUEST_TIMEOUT_SECONDS)
    r.raise_for_status()
    with open(path, "wb") as f:
        for chunk in r.iter_content(chunk_size=128):
            f.write(chunk)


def compute_jar_file(path, url, fallback_url=None):
    # These two approaches should result in the same metadata, except for the timestamp which might be a few minutes
    # off for the fallback method
    selected_url = url
    headers = None
    try:
        # Let's not download a Jar file if we don't need to.
        headers = head_file(url)
    except requests.RequestException as error:
        if fallback_url is None:
            print(f"Falling back to downloading jar for {url}")
        else:
            print(f"BMCLAPI Fabric JAR unavailable, trying official: {url}")
            selected_url = fallback_url
            try:
                headers = head_file(fallback_url)
            except requests.RequestException:
                headers = None

    if headers is not None and "Last-Modified" in headers:
        tstamp = datetime.strptime(headers["Last-Modified"], DATETIME_FORMAT_HTTP)
    else:
        # Just in case something changes in the future
        print(f"Falling back to downloading jar for {selected_url}")

        jar_path = path + ".jar"
        try:
            get_binary_file(jar_path, selected_url)
        except requests.RequestException:
            if fallback_url is None or selected_url == fallback_url:
                raise
            selected_url = fallback_url
            get_binary_file(jar_path, selected_url)
        tstamp = datetime.fromtimestamp(0)
        with zipfile.ZipFile(jar_path) as jar:
            allinfo = jar.infolist()
            for info in allinfo:
                tstamp_new = datetime(*info.date_time)
            if tstamp_new > tstamp:
                tstamp = tstamp_new

    existing_info_path = path + ".json"
    if os.path.isfile(existing_info_path):
        existing_info = FabricJarInfo.parse_file(existing_info_path)
        if existing_info.release_time is not None:
            tstamp = existing_info.release_time

    data = FabricJarInfo(
        release_time=tstamp,
        maven_url=(
            BMCLAPI_MAVEN_URL
            if selected_url == url
            else OFFICIAL_FABRIC_MAVEN_URL
        ),
    )
    data.write(path + ".json")


def compute_jar_file_concurrent(it):
    print(f"Processing {it['version']} ")
    jar_maven_url = get_maven_url(it["maven"], BMCLAPI_MAVEN_URL, ".jar")
    official_jar_maven_url = get_maven_url(
        it["maven"], OFFICIAL_FABRIC_MAVEN_URL, ".jar"
    )
    compute_jar_file(
        os.path.join(UPSTREAM_DIR, JARS_DIR, transform_maven_key(it["maven"])),
        jar_maven_url,
        official_jar_maven_url,
    )
    print(f"Processing {it['version']} Done")


def get_json_file_concurrent(it):
    print(f"Downloading JAR info for loader {it['version']} ")
    maven_url = get_maven_url(it["maven"], BMCLAPI_MAVEN_URL, ".json")
    official_maven_url = get_maven_url(
        it["maven"], OFFICIAL_FABRIC_MAVEN_URL, ".json"
    )
    get_json_file(
        os.path.join(UPSTREAM_DIR, INSTALLER_INFO_DIR, f"{it['version']}.json"),
        maven_url,
        official_maven_url,
    )
    print(f"Downloading JAR info for loader {it['version']} Done")


def main():
    # get the version list for each component we are interested in
    for component in ["intermediary", "loader"]:
        index = get_json_file(
            os.path.join(UPSTREAM_DIR, META_DIR, f"{component}.json"),
            f"{BMCLAPI_FABRIC_META_URL}/v2/versions/{component}",
            f"{OFFICIAL_FABRIC_META_URL}/v2/versions/{component}",
        )
        with Pool(None) as pool:
            deque(pool.imap_unordered(compute_jar_file_concurrent, index, 32), 0)

    # for each loader, download installer JSON file from maven
    with open(
        os.path.join(UPSTREAM_DIR, META_DIR, "loader.json"), "r", encoding="utf-8"
    ) as loaderVersionIndexFile:
        loader_version_index = json.load(loaderVersionIndexFile)
        with Pool(None) as pool:
            deque(
                pool.imap_unordered(get_json_file_concurrent, loader_version_index, 32),
                0,
            )


if __name__ == "__main__":
    main()
