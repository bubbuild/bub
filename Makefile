.DEFAULT_GOAL := help

.PHONY: help install check vulture test clean-build build publish build-and-publish docs-test docs docs-preview

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z0-9_-]+:.*## / {printf "%-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Sync Python deps, install website deps, and install prek hooks
	@echo "==> Syncing Python dependencies with uv"
	uv sync
	@echo "==> Installing website dependencies with pnpm"
	pnpm --dir website install --frozen-lockfile
	@echo "==> Installing prek hooks"
	uv run prek install

check: ## Validate lockfile, run prek across the repo, and type-check src
	@echo "==> Verifying uv.lock matches pyproject.toml"
	uv lock --locked
	@echo "==> Running prek hooks"
	uv run prek run -a
	@echo "==> Running mypy on src"
	uv run mypy src

vulture: ## Run the optional unused-code check
	@echo "==> Running vulture via prek"
	uv run prek run vulture --hook-stage manual --all-files

test: ## Run pytest with doctests enabled
	@echo "==> Running pytest with doctests"
	uv run python -m pytest --doctest-modules

clean-build: ## Remove local build artifacts
	@echo "==> Removing dist/"
	rm -rf dist

build: clean-build ## Build source and wheel distributions
	@echo "==> Building package distributions"
	uv build

publish: ## Publish the contents of dist/ with uv
	@echo "==> Publishing dist/ with uv"
	uv publish

build-and-publish: build publish ## Build distributions, then publish them

docs-test: ## Build the website/docs bundle
	@echo "==> Building website/"
	BUB_ASTRO_IMAGE_MODE=build pnpm --dir website build

docs: ## Start the website/docs development server
	@echo "==> Starting website dev server"
	BUB_ASTRO_IMAGE_MODE=dev pnpm --dir website dev --host

docs-preview: ## Preview the production website/docs build
	@echo "==> Starting website preview server"
	BUB_ASTRO_IMAGE_MODE=build pnpm --dir website preview --ip 0.0.0.0
