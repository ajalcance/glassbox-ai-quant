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

drill-trip:  ## LIVE: open a real spread, verify everything, close it
	uv run python -m glassbox.drills round_trip
drill-flat:  ## LIVE: open a position, kill switch, verify supervisor flattens
	uv run python -m glassbox.drills flatten
drill-recon: ## induce a reconciliation divergence (places no orders)
	uv run python -m glassbox.drills reconcile
drill-clean: ## close everything and clear drill state
	uv run python -m glassbox.drills cleanup
preflight:   ## assert account preconditions
	uv run python -m glassbox.preflight

kill:        ## ENGAGE the kill switch — halts trading, supervisor flattens
	@touch KILL && echo "kill switch ENGAGED — supervisor will flatten and halt"
resume:      ## clear the kill switch
	@rm -f KILL && echo "kill switch cleared"

up:          ## start the full stack in docker
	docker compose up -d --build
down:        ## stop the stack
	docker compose down
logs:        ## follow all logs
	docker compose logs -f --tail=100
ps:          ## container status
	docker compose ps
