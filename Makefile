.PHONY: wheel image docker-up docker-down

wheel:
	./scripts/build-docker-wheel.sh

image: wheel
	docker build \
		--file examples/basic-local-docker/Dockerfile \
		--tag efferva-basic-local-docker:local \
		.

docker-up: image
	docker compose \
		--file examples/basic-local-docker/compose.yaml \
		up

docker-down:
	docker compose \
		--file examples/basic-local-docker/compose.yaml \
		down
