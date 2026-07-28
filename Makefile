.PHONY: check docker-runtime docker-example docker-up docker-down docker-e2e kind-up kind-up-cliproxy kind-smoke kind-e2e kind-down kind-port-forward

check:
	uv run ruff check .
	uv run ruff format --check .
	cargo check --workspace

docker-runtime:
	docker build \
		--file docker/Dockerfile \
		--target runtime \
		--tag agentframe-runtime:local \
		..

docker-example: docker-runtime
	docker build \
		--file examples/basic-product/Dockerfile \
		--tag agentframe-basic-product:local \
		examples/basic-product

docker-up:
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
