.PHONY: check wheel wheel-docker wheel-smoke docker-runtime docker-example docker-up docker-down docker-e2e kind-up kind-up-cliproxy kind-smoke kind-e2e kind-down kind-port-forward

check:
	uv run ruff check .
	uv run ruff format --check .
	cargo check --workspace

wheel:
	./scripts/build-wheel.sh

wheel-docker:
	./scripts/build-docker-wheel.sh

wheel-smoke: wheel-docker
	./scripts/wheel-smoke.sh $$(find dist/docker -maxdepth 1 -name 'agentframe-*.whl' -print -quit)

docker-runtime: wheel-docker
	docker build \
		--file docker/Dockerfile \
		--target runtime \
		--tag agentframe-runtime:local \
		..

docker-example: wheel-docker
	docker build \
		--file examples/basic-product/Dockerfile \
		--tag agentframe-basic-product:local \
		.

docker-up: wheel-docker
	@docker network inspect agentframe >/dev/null 2>&1 || docker network create agentframe
	docker compose up --build --detach
	docker compose ps

docker-down:
	docker compose down

docker-e2e:
	./scripts/docker-e2e.sh

kind-up:
	./scripts/kind-up.sh

kind-up-cliproxy:
	./scripts/kind-up-cliproxy.sh

kind-smoke:
	./scripts/kind-smoke.sh

kind-e2e:
	./scripts/kind-e2e.sh

kind-down:
	kind delete cluster --name "$${AGENTFRAME_KIND_CLUSTER:-agentframe}"

kind-port-forward:
	kubectl --context "kind-$${AGENTFRAME_KIND_CLUSTER:-agentframe}" \
		--namespace agentframe port-forward service/agentframe 8080:80
