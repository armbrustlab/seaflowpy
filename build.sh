#!/bin/bash
# Build PyPI pure Python wheel, Docker image, pyinstaller execs

# --------------------------------------------------------------------------- #
# Step 1
# Create pure python wheel and source tarball. Will be saved in ./dist
# --------------------------------------------------------------------------- #
python setup.py -q sdist
python setup.py -q bdist_wheel
[[ -d build ]] && rm -rf build

# --------------------------------------------------------------------------- #
# Step 2
# Test new wheel in a temporary virtual environment
# --------------------------------------------------------------------------- #
venvdir=$(mktemp -dt seaflowpy)
if [ -z "$venvdir" ]; then
    echo "Could not create virtualenv temp directory name" >&2
    exit 1
fi
echo "Creating virtualenv $venvdir" >&2
python -m venv "$venvdir"
source "$venvdir/bin/activate"
echo "Installing requirements.txt, pytest, seaflowpy from wheel" >&2
pip install -q -r requirements.txt
pip install -q pytest pyinstaller
pip install -q -f ./dist seaflowpy
git clean -fdx tests  # clean up test caches
pytest
pytestrc=$?
deactivate

if [ $pytestrc -ne 0 ]; then
    exit $pytestrc
fi

# --------------------------------------------------------------------------- #
# Step 3
# Build a docker image with wheel, tagged with current version string
# --------------------------------------------------------------------------- #
verstr=$(git describe --tags --dirty --always)
docker build -t seaflowpy:"$verstr" .
if [ $? -ne 0 ]; then
    echo "Error building Docker image" >&2
    exit $?
fi

# --------------------------------------------------------------------------- #
# Step 4
# Test the new docker image
# --------------------------------------------------------------------------- #
git clean -fdx tests  # remove test cache
docker run --rm -v $(pwd):/mnt seaflowpy:"$verstr" bash -c 'cd /mnt && pip3 install -q pytest && pytest --cache-clear'
if [ $? -ne 0 ]; then
    echo "Docker image failed tests" >&2
    exit $?
fi

# --------------------------------------------------------------------------- #
# Step 5
# Build pyinstaller executables. Linux target will be built in a temp docker
# container using wheel from step 1. MacOS target will be built in the temp
# virtual environment created in step 2.
# --------------------------------------------------------------------------- #
source "$venvdir/bin/activate"
cd pyinstaller || exit 1
./build_all.sh
deactivate

# --------------------------------------------------------------------------- #
# Cleanup tasks. Find build files to remove, and remove them.
# --------------------------------------------------------------------------- #
# git clean -fdn  # remove build files (dry run)
# git clean -fd   # remove build files

# --------------------------------------------------------------------------- #
# Misc docker tasks
# --------------------------------------------------------------------------- #
# Find docker images created with this script
# docker image ls --filter=reference='seaflowpy:*'

# Remove all iamges created with this script
# docker rmi $(docker image ls -q --filter=reference='seaflowpy:*')

# Tag the image created with this script and push to docker hub
# docker image tag seaflowpy:<version> account/seaflowpy:<version>
# docker push account/seaflowpy:<version>

# --------------------------------------------------------------------------- #
# Optional, upload wheel and source tarball to PyPI
# --------------------------------------------------------------------------- #
# Test against test PyPI repo
# twine upload -r https://test.pypi.org/legacy/ dist/seaflowpy-x.x.x*

# Create a virtualenv and test install from test.pypi.org
# python -m venv pypi-test
# pypi-test/bin/pip install -r requirements.txt
# pypi-test/bin/pip install -i https://testpypi.python.org/pypi seaflowpy
# pypi-test/bin/seaflowpy version

# Then upload to the real PyPI
# twine upload dist/seaflowpy-x.x.x*