# Local build PKGBUILD - from the repo root run:  makepkg -si
# (Builds and installs the working tree. For the AUR -git package see
#  packaging/PKGBUILD.)
pkgname=gnomecast
pkgver=2.1.0
pkgrel=1
pkgdesc="A native Linux GUI for casting local files to Chromecast devices"
arch=('any')
url="https://github.com/winterdeaf/gnomecast"
license=('GPL-3.0-or-later')
depends=('ffmpeg' 'gtk3' 'python-gobject' 'python-pychromecast')
optdepends=('python-dbus: inhibit the screensaver while casting')
makedepends=('python-build' 'python-installer' 'python-wheel' 'python-setuptools')
checkdepends=('python-pytest')
provides=("$pkgname")
conflicts=("$pkgname" "$pkgname-git")

build() {
	# Copy the working tree into the build dir so the repo stays clean.
	local src="$srcdir/$pkgname"
	rm -rf "$src"
	mkdir -p "$src"
	cp -rt "$src" \
		"$startdir/gnomecast" \
		"$startdir/tests" \
		"$startdir/pyproject.toml" \
		"$startdir/README.md" \
		"$startdir/LICENSE" \
		"$startdir/gnomecast.desktop" \
		"$startdir/icons"
	cd "$src"
	python -m build --wheel --no-isolation
}

check() {
	cd "$srcdir/$pkgname"
	pytest -q
}

package() {
	cd "$srcdir/$pkgname"
	python -m installer --destdir="$pkgdir" dist/*.whl
}