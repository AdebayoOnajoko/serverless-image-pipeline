# Built by `sam build` (BuildMethod: makefile).
# The function has no third-party dependencies (Pillow comes from the layer,
# boto3 from the runtime), so the build just copies the source. Using a
# makefile means sam build does not need Python 3.13 installed locally.
build-ImageResizerFunction:
	cp app.py "$(ARTIFACTS_DIR)/"
