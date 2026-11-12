# Built by `sam build` (BuildMethod: makefile).
# Installs Lambda-compatible Linux wheels regardless of the OS you build on.
build-PillowLayer:
	python3 -m pip install -r requirements.txt \
		--platform manylinux_2_28_x86_64 \
		--implementation cp \
		--python-version 3.13 \
		--only-binary=:all: \
		--target "$(ARTIFACTS_DIR)/python"
