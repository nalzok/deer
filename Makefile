SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
MAKEFLAGS = --warn-undefined-variables

.venv:
	python3.11 -m venv .venv
	.venv/bin/python3.11 -m pip install --upgrade pip setuptools wheel build pip-tools
	touch .venv

requirements.txt: .venv requirements.in
	.venv/bin/python3.11 -m piptools compile \
		--pip-args="--find-links https://storage.googleapis.com/jax-releases/jax_cuda_releases.html --extra-index-url https://download.pytorch.org/whl/cu118"
	.venv/bin/python3.11 -m piptools sync
