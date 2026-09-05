import json
import os

import requests

from meta.common import upstream_path, ensure_upstream_dir, default_session
from meta.common.bmclapi import (
    BMCLAPI_LITELOADER_MAVEN_URL,
    BMCLAPI_REQUEST_TIMEOUT_SECONDS,
    is_bmclapi_url,
)
from meta.common.liteloader import VERSIONS_FILE, BASE_DIR
from meta.model.liteloader import LiteloaderIndex

UPSTREAM_DIR = upstream_path()

ensure_upstream_dir(BASE_DIR)

sess = default_session()


def main():
    # get the remote version list
    official_url = "http://dl.liteloader.com/versions/versions.json"
    mirror_url = BMCLAPI_LITELOADER_MAVEN_URL + "versions.json"
    last_error = None
    for source_url in (mirror_url, official_url):
        try:
            r = sess.get(source_url, timeout=BMCLAPI_REQUEST_TIMEOUT_SECONDS)
            r.raise_for_status()

            # make sure it's JSON and that we understand the schema
            main_json = r.json()
            remote_versions = LiteloaderIndex.parse_obj(main_json)
            parsed = remote_versions.json()
            original = json.dumps(main_json, sort_keys=True, indent=4)
            assert parsed == original
            remote_versions.bmclapi = is_bmclapi_url(source_url)
            break
        except (requests.RequestException, ValueError, AssertionError) as error:
            last_error = error
            if source_url == mirror_url:
                print(f"BMCLAPI LiteLoader request failed, trying official: {error}")
    else:
        assert last_error is not None
        raise last_error

    print("Successfully parsed index")
    print(f"Last updated {remote_versions.meta.updated}")

    # save the json it to file
    remote_versions.write(os.path.join(UPSTREAM_DIR, VERSIONS_FILE))


if __name__ == "__main__":
    main()
