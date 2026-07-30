.PHONY: wheel docker-example

wheel:
	./scripts/build-docker-wheel.sh

docker-example:
	docker build \
		--file examples/basic-local-docker/Dockerfile \
		--tag efferva-basic-local-docker:local \
		examples/basic-local-docker
