.PHONY: collect report serve dashboard dashboard-down refresh clean ensure-ci-obs help investigate digest failure-patterns export-context jira-report ci-report bug-bash-report agentready

PYTHON ?= python3
CLI = $(PYTHON) cli.py
EXPORTER_PID_FILE = data/.exporter.pid

# Path to the openshift-ci-observability checkout.
# Override via env, local.mk, or: make collect CI_OBS_DIR=/path/to/repo
CI_OBS_DIR ?= $(HOME)/git/openshift-ci-observability

-include local.mk

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

CI_OBS_CONTAINERS := ci-obs-victoriametrics ci-obs-victorialogs ci-obs-scraper-watch ci-obs-scraper-backfill ci-obs-grafana

ensure-ci-obs: ## Ensure CI Observability stack (all containers) is running
	@if [ ! -f "$(CI_OBS_DIR)/podman-compose.yml" ]; then \
		echo "CI Observability: stack not found at $(CI_OBS_DIR)"; \
		echo "  CI efficiency data will be skipped."; \
		echo "  To enable: clone openshift-ci-observability and set CI_OBS_DIR, or start it manually."; \
	else \
		missing=""; \
		for c in $(CI_OBS_CONTAINERS); do \
			if ! podman ps --format '{{.Names}}' 2>/dev/null | grep -q "^$$c$$"; then \
				missing="$$missing $$c"; \
			fi; \
		done; \
		if [ -z "$$missing" ]; then \
			echo "CI Observability: all containers running"; \
		else \
			echo "CI Observability: missing containers:$$missing"; \
			echo "Restarting CI Observability stack from $(CI_OBS_DIR)..."; \
			$(MAKE) -C "$(CI_OBS_DIR)" restart; \
			echo "Waiting for VictoriaMetrics to be ready..."; \
			for i in 1 2 3 4 5; do \
				curl -sf http://localhost:8428/health >/dev/null 2>&1 && break; \
				sleep 2; \
			done; \
		fi; \
	fi

collect: ensure-ci-obs ## Clone/fetch repos and collect all data from git history (FORCE=true to re-collect)
	$(CLI) collect $(if $(FORCE),--force,)

report: ## Print an engineering metrics summary to the terminal
	$(CLI) report

investigate: ## Generate failure investigation report (PR=<number> or latest failed)
	$(CLI) investigate $(if $(PR),--pr $(PR),)

digest: ## Generate weekly CI health digest (WEEKS=<n> for lookback, default 1)
	$(CLI) digest $(if $(WEEKS),--weeks $(WEEKS),)

failure-patterns: ## Analyze recurring failure patterns (DAYS=<n> for lookback, default 30)
	$(CLI) failure-patterns $(if $(DAYS),--days $(DAYS),)

export-context: ## Export structured JSON for AI agents (PR=<number> or codebase-wide)
	$(CLI) export-context $(if $(PR),--pr $(PR),) $(if $(DAYS),--days $(DAYS),) $(if $(OUTPUT),-o $(OUTPUT),)

jira-report: ## Analyze a JIRA collection (COLLECTION=name, JSON=1 for JSON output)
	$(CLI) jira-report $(COLLECTION) $(if $(JSON),--json-output,)

ci-report: ## Generate HTML CI health report with charts (OUTPUT=path)
	$(CLI) ci-report $(if $(OUTPUT),-o $(OUTPUT),)

bug-bash-report: ## Generate HTML deep-analysis report for the AI Bug Bash (OUTPUT=path)
	$(PYTHON) generate_bug_bash_report.py $(if $(OUTPUT),$(OUTPUT),)

agentready: ## Run AgentReady assessments on repos mapped to JIRA projects (COLLECTION=name)
	$(CLI) agentready $(if $(COLLECTION),--collection $(COLLECTION),)

serve: ## Start the Prometheus metrics exporter on :9090 (foreground)
	$(CLI) serve

dashboard: ## Start Prometheus + Grafana + exporter (all in containers)
	@# Stop any leftover host-side exporter from older setups
	@if [ -f $(EXPORTER_PID_FILE) ]; then \
		kill $$(cat $(EXPORTER_PID_FILE)) 2>/dev/null || true; \
		rm -f $(EXPORTER_PID_FILE); \
	fi
	docker compose -f dashboard/docker-compose.yml up -d
	@echo ""
	@echo "  Grafana:    http://localhost:3001  (admin/admin)"
	@echo "  Prometheus: http://localhost:9091"
	@echo ""
	@echo "  Run 'make dashboard-down' to stop everything."
	@echo "  Run 'make refresh' after 'make collect' to update dashboards."

dashboard-down: ## Stop the Docker Compose stack
	docker compose -f dashboard/docker-compose.yml down

refresh: ensure-ci-obs ## Collect fresh data and restart the exporter so dashboards update immediately
	$(CLI) collect
	docker compose -f dashboard/docker-compose.yml restart exporter
	@sleep 3
	@echo ""
	@echo "  Data collected and exporter restarted."
	@echo "  Dashboards will reflect new data within ~60s (next Prometheus scrape)."

clean: ## Remove cached data (clones + sqlite)
	rm -rf data/
