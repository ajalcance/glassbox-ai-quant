.PHONY: help test lint check run dry supervisor dashboard train up down logs ps kill resume

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n", $$1, $$2}'

test:        ## run the test suite
	uv run pytest -q
lint:        ## lint and format
	uv run ruff check --fix . && uv run ruff format .
check: lint test  ## lint then test

dry:         ## run the trader against live data, placing no orders
	uv run python -m glassbox.runner --dry-run
run:         ## run the trader LIVE on the paper account
	uv run python -m glassbox.runner
supervisor:  ## run the guards and kill switch (separate process)
	uv run python -m glassbox.supervisor.run
dashboard:   ## serve the dashboard on :8847
	uv run python -m glassbox.dashboard.app
train:       ## fit the volatility model and refit the meta-labeler
	uv run python -m glassbox.ml.train
report:      ## write today's report now, ignoring the schedule
	uv run python -m glassbox.scheduler --force nightly_report
scheduler:   ## run the job scheduler (report + model refit)
	uv run python -m glassbox.scheduler

drill-sim:   ## full lifecycle with a simulated fill (works market-closed)
	uv run python -m glassbox.drills simulate
drill-trip:  ## LIVE: open a real spread, verify everything, close it
	uv run python -m glassbox.drills round_trip
drill-flat:  ## LIVE: open a position, kill switch, verify supervisor flattens
	uv run python -m glassbox.drills flatten
drill-recon: ## induce a reconciliation divergence (places no orders)
	uv run python -m glassbox.drills reconcile
drill-clean: ## close everything and clear drill state
	uv run python -m glassbox.drills cleanup
score:       ## score analyst estimates whose horizon has elapsed
	uv run python -m glassbox.scheduler --force resolve_predictions
calibrate:   ## analyst bias plus observed edge-ratio and VRP distributions
	uv run python -m glassbox.calibrate
preflight:   ## assert account preconditions
	uv run python -m glassbox.preflight

# The sentinel lives at data/KILL: host processes read the repo's data/, and
# the compose stack shares one `state` volume at /app/data — so the exec below
# reaches every container at once. Both paths are written so the stop works
# whether the stack runs on the host, in docker, or both.
kill:        ## ENGAGE the kill switch — halts trading, supervisor flattens
	@mkdir -p data && touch data/KILL
	@docker compose exec -T supervisor touch /app/data/KILL 2>/dev/null || true
	@echo "kill switch ENGAGED — supervisor will flatten and halt"
resume:      ## clear the kill switch
	@rm -f data/KILL KILL
	@docker compose exec -T supervisor rm -f /app/data/KILL 2>/dev/null || true
	@echo "kill switch cleared"

up:          ## start the full stack in docker
	docker compose up -d --build
down:        ## stop the stack
	docker compose down
logs:        ## follow all logs
	docker compose logs -f --tail=100
ps:          ## container status
	docker compose ps
