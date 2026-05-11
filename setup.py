import os
import re

from setuptools import setup

# Project description
description = "Snapcast MPRIS bridge"

# Use README.md as long description if available; otherwise, fallback to the description
if os.path.exists("readme.md"):
    with open("readme.md", encoding="utf-8") as fh:
        long_description = fh.read()
else:
    long_description = description

# Single source of truth: debian/changelog. The CI release step bumps
# the changelog from the git tag, and this picks up the same value so
# `pip show snapclientmpris` and the .deb stay in sync. Debian-flavor
# prereleases (1.1.0~rc.1 / ~beta.N / ~alpha.N) are normalized to
# PEP 440 because setuptools rejects '~' in versions.
_PRE_TAG = {"rc": "rc", "beta": "b", "alpha": "a"}


def _read_version() -> str:
    path = os.path.join(os.path.dirname(__file__), "debian", "changelog")
    try:
        with open(path, encoding="utf-8") as f:
            first = f.readline()
    except OSError:
        return "0.0.0"
    m = re.match(r"^\S+\s+\(([^)]+)\)", first)
    if not m:
        return "0.0.0"
    deb = m.group(1)
    deb = re.sub(r"-\d+$", "", deb)  # strip Debian revision if any
    return re.sub(
        r"~(rc|beta|alpha)\.?(\d+)",
        lambda mo: _PRE_TAG[mo.group(1)] + mo.group(2),
        deb,
    )


setup(
    name="snapclientmpris",
    version=_read_version(),
    author="Mathieu Réquillart",
    author_email="mathieu.requillart@gmail.com",
    description=description,
    long_description=long_description,
    long_description_content_type="text/markdown" if os.path.exists("readme.md") else "text/plain",
    url="https://github.com/b0bbywan/snapclientmpris",
    license="MIT",
    packages=["snapclientmpris"],
    package_dir={"snapclientmpris": "snapclientmpris"},
    entry_points={
        "console_scripts": [
            "snapclientmpris=snapclientmpris.cli:main",
        ]
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Operating System :: POSIX :: Linux",
    ],
    python_requires=">=3.11",
    install_requires=[
        "snapcast>=2.3",
        "dbus-fast>=2.0",
        "zeroconf>=0.28",
    ],
    extras_require={
        "dev": ["pytest", "flake8"],
    },
    include_package_data=True,
    zip_safe=False,
)
