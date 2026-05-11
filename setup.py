import os

from setuptools import setup

# Project description
description = "Snapcast MPRIS bridge"

# Use README.md as long description if available; otherwise, fallback to the description
if os.path.exists("readme.md"):
    with open("readme.md", encoding="utf-8") as fh:
        long_description = fh.read()
else:
    long_description = description

setup(
    name="snapclientmpris",
    version="1.1.0",
    author="Mathieu Réquillart",
    author_email="mathieu.requillart@gmail.com",
    description=description,
    long_description=long_description,
    long_description_content_type="text/markdown" if os.path.exists("readme.md") else "text/plain",
    url="https://github.com/b0bbywan/snapclientmpris",
    packages=["snapclientmpris"],
    package_dir={"snapclientmpris": "snapclientmpris"},
    entry_points={
        "console_scripts": [
            "snapclientmpris=snapclientmpris.snapclientmpris:main",
        ]
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: POSIX :: Linux",
    ],
    python_requires=">=3.9",
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
