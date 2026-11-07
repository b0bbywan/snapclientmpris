# Dev / CI helpers. `snapclientmpris/__init__.py` is the version source of
# truth; this Makefile drives lint / test / build / deb and keeps the
# debian/changelog in sync with the Python version, no logic duplicated
# in the workflow YAML.

PYTHON  ?= python3
VERSION := $(PYTHON) scripts/version.py

.PHONY: version deb-version check-tag sync-deb check-arch sync-arch \
        lint lint-ruff lint-mypy test build sdist deb arch clean

# --- version helpers ---------------------------------------------------

version:
	@$(VERSION)

deb-version:
	@$(VERSION) --debian

# Fail if the git tag doesn't match __init__.py (vX prefix optional).
# CI invokes this with TAG=$GITHUB_REF_NAME on tag pushes to catch a drift
# between the manual __init__.py bump and the tag.
TAG ?= $(GITHUB_REF_NAME)
check-tag:
	@$(VERSION) --check-tag '$(TAG)'

# Bump debian/changelog to match deb-version. Idempotent — noop if already
# in sync. Needs `devscripts` (dch) and `dpkg-dev` (dpkg-parsechangelog).
sync-deb:
	@deb=$$($(VERSION) --debian); \
	cl=$$(dpkg-parsechangelog -S Version); \
	if [ "$$deb" != "$$cl" ]; then \
		dch -b --newversion "$$deb" --distribution unstable \
			--urgency medium "Release $$deb"; \
	fi

# Arch PKGBUILD pkgver must match __init__.py. sync-arch also resets pkgrel.
ARCH_DIR    := packaging/arch
ARCH_PKGVER  = sed -n 's/^pkgver=//p' $(ARCH_DIR)/PKGBUILD

check-arch:
	@v=$$($(VERSION)); a=$$($(ARCH_PKGVER)); \
	if [ "$$v" != "$$a" ]; then \
		echo "PKGBUILD pkgver $$a != __init__.py $$v (run make sync-arch)" >&2; exit 1; \
	fi

sync-arch:
	@v=$$($(VERSION)); a=$$($(ARCH_PKGVER)); \
	if [ "$$v" != "$$a" ]; then \
		sed -i "s/^pkgver=.*/pkgver=$$v/; s/^pkgrel=.*/pkgrel=1/" $(ARCH_DIR)/PKGBUILD; \
	fi

# --- dev workflow ------------------------------------------------------

lint: lint-ruff lint-mypy

lint-ruff:
	ruff check snapclientmpris/ tests/

lint-mypy:
	mypy snapclientmpris/

test:
	pytest -q

build:
	$(PYTHON) -m build

# Source distribution only (the tar.gz attached to the GitHub release).
sdist:
	$(PYTHON) -m build --sdist

# Builds the .deb via dpkg-buildpackage. Requires a Debian toolchain
# (debhelper, dh-python, devscripts, etc.) — not available on Fedora;
# use a Debian container for local builds. Note: this target does NOT
# call `sync-deb`; call it first manually for a release build so the
# changelog matches __init__.py.
deb:
	dpkg-buildpackage -b -us -uc

# Builds the Arch package from a local sdist of the working tree, not
# PyPI's. Needs makepkg, so run it as non-root in an Arch container.
# CI passes MAKEPKG_FLAGS=--nodeps since snapcast is AUR-only.
arch:
	$(PYTHON) -m build --sdist --no-isolation --outdir $(ARCH_DIR)
	cd $(ARCH_DIR) && makepkg -f $(MAKEPKG_FLAGS)

clean:
	rm -rf build/ dist/ *.egg-info snapclientmpris.egg-info
	rm -rf $(ARCH_DIR)/src $(ARCH_DIR)/pkg $(ARCH_DIR)/*.tar.gz $(ARCH_DIR)/*.pkg.tar.*
