.PHONY: build test-samples test-samples-warning

IMAGE_TAG ?= kubepolicy:local

build:
	docker build -t $(IMAGE_TAG) .

test-samples: build
	IMAGE_TAG=$(IMAGE_TAG) SEVERITY=error POST_COMMENT=false \
	./scripts/run_local.sh "samples/**/*.yaml"

test-samples-warning: build
	IMAGE_TAG=$(IMAGE_TAG) SEVERITY=warning POST_COMMENT=false \
	./scripts/run_local.sh "samples/**/*.yaml"

