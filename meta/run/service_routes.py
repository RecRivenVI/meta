import os


BMCLAPI_BASE_URL = os.environ.get(
    "BMCLAPI_BASE_URL", "https://bmclapi2.bangbang93.com"
).rstrip("/")
BMCLAPI_MAVEN_URL = f"{BMCLAPI_BASE_URL}/maven/"
OFFICIAL_FML_LIBS_URL = "https://files.prismlauncher.org/fmllibs/"


# The filenames and SHA-1 values are fixed by PrismLauncher's legacy FML
# mapping. A BMCLAPI URL is listed only when its current response was checked
# against the corresponding expected file hash.
FML_LIBRARIES = {
    "argo-2.25.jar": (
        "bb672829fde76cb163004752b86b0484bd0a7f4b",
        None,
    ),
    "guava-12.0.1.jar": (
        "b8e78b9af7bf45900e14c6f958486b6ca682195f",
        f"{BMCLAPI_MAVEN_URL}com/google/guava/guava/12.0.1/guava-12.0.1.jar",
    ),
    "asm-all-4.0.jar": (
        "98308890597acb64047f7e896638e0d98753ae82",
        None,
    ),
    "bcprov-jdk15on-147.jar": (
        "b6f5d9926b0afbde9f4dbe3db88c5247be7794bb",
        f"{BMCLAPI_MAVEN_URL}org/bouncycastle/bcprov-jdk15on/1.47/"
        "bcprov-jdk15on-1.47.jar",
    ),
    "argo-small-3.2.jar": (
        "58912ea2858d168c50781f956fa5b59f0f7c6b51",
        None,
    ),
    "guava-14.0-rc3.jar": (
        "931ae21fa8014c3ce686aaa621eae565fefb1a6a",
        f"{BMCLAPI_MAVEN_URL}com/google/guava/guava/14.0-rc3/"
        "guava-14.0-rc3.jar",
    ),
    "asm-all-4.1.jar": (
        "054986e962b88d8660ae4566475658469595ef58",
        f"{BMCLAPI_MAVEN_URL}org/ow2/asm/asm-all/4.1/asm-all-4.1.jar",
    ),
    "bcprov-jdk15on-148.jar": (
        "960dea7c9181ba0b17e8bab0c06a43f0a5f04e65",
        f"{BMCLAPI_MAVEN_URL}org/bouncycastle/bcprov-jdk15on/1.48/"
        "bcprov-jdk15on-1.48.jar",
    ),
    "deobfuscation_data_1.5.zip": (
        "5f7c142d53776f16304c0bbe10542014abad6af8",
        None,
    ),
    "deobfuscation_data_1.5.1.zip": (
        "22e221a0d89516c1f721d6cab056a7e37471d0a6",
        None,
    ),
    "deobfuscation_data_1.5.2.zip": (
        "446e55cd986582c70fcf12cb27bc00114c5adfd9",
        None,
    ),
    "scala-library.jar": (
        "458d046151ad179c85429ed7420ffb1eaf6ddf85",
        None,
    ),
}
