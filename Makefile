.PHONY: install extract transform serve web all refresh full-rebuild clean test lint help

help:
	@echo "Common targets:"
	@echo "  make install        Install Python + dbt deps"
	@echo "  make extract        Run extract_load/ → DuckDB raw tables"
	@echo "  make transform      Run dbt models (stg → int → marts), incremental where supported"
	@echo "  make serve          Build slim JSONs into web/public/"
	@echo "  make web            Run Next.js dev server"
	@echo "  make all            extract + transform + serve"
	@echo "  make refresh        Same as 'all' but with a single log file (intended for cron)"
	@echo "  make full-rebuild   --full-refresh dbt; use after schema/logic changes"
	@echo "  make test           Python + dbt tests"
	@echo "  make clean          Drop dbt target/, keep warehouse"

install:
	python3 -m pip install -e .
	cd transform && dbt deps

extract:
	python3 -m extract_load.extract_markets
	python3 -m extract_load.extract_signatures
	python3 -m extract_load.extract_transactions
	python3 -m extract_load.extract_token_metadata
	python3 -m extract_load.extract_prices
	python3 -m extract_load.extract_positions
	python3 -m extract_load.extract_pool_state
	python3 -m extract_load.extract_holders

transform:
	cd transform && DBT_PROFILES_DIR=. dbt build

full-rebuild:
	cd transform && DBT_PROFILES_DIR=. dbt build --full-refresh

serve:
	python3 -m serve.build_web_data

web:
	cd web && npm run dev

all: extract transform serve

# Cron-friendly: timestamped log line at start, one file accumulates history.
# Uses relative paths to avoid Make $(dir) issues with spaces in absolute paths.
refresh:
	@mkdir -p data/logs
	@echo "" >> data/logs/refresh.log
	@echo "================================================================" >> data/logs/refresh.log
	@echo "[$$(date -u +%FT%TZ)] refresh started" >> data/logs/refresh.log
	@$(MAKE) -s all >> data/logs/refresh.log 2>&1 && \
		echo "[$$(date -u +%FT%TZ)] refresh done" >> data/logs/refresh.log || \
		echo "[$$(date -u +%FT%TZ)] refresh FAILED — see tail of log" >> data/logs/refresh.log

test:
	pytest tests/
	cd transform && dbt test

clean:
	rm -rf transform/target transform/dbt_packages transform/logs
