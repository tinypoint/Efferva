.PHONY: check wheel wheel-docker wheel-smoke docker-example opensandbox-e2e

check:
	uv run ruff check .
	uv run ruff format --check .
	cargo check --workspace

wheel:
	./scripts/build-wheel.sh

wheel-docker:
	./scripts/build-docker-wheel.sh

wheel-smoke: wheel-docker
	./scripts/wheel-smoke.sh $$(find dist/docker -maxdepth 1 -name 'efferva-*.whl' -print -quit)

docker-example:
	docker build \
		--file examples/basic-local-docker/Dockerfile \
		--tag efferva-basic-local-docker:local \
		examples/basic-local-docker

opensandbox-e2e: wheel-docker
	./scripts/opensandbox-e2e.sh
