.PHONY: install extract transform serve web all clean test lint help

help:
	@echo "Common targets:"
	@echo "  make install     Install Python + dbt deps"
	@echo "  make extract     Run extract_load/ → DuckDB raw tables"
	@echo "  make transform   Run dbt models (stg → int → marts)"
	@echo "  make serve       Build slim JSONs into web/public/"
	@echo "  make web         Run Next.js dev server"
	@echo "  make all         extract + transform + serve"
	@echo "  make test        Python + dbt tests"
	@echo "  make clean       Drop dbt target/, keep warehouse"

install:
	python3 -m pip install -e .
	cd transform && dbt deps

extract:
	python3 -m extract_load.extract_markets
	python3 -m extract_load.extract_signatures
	python3 -m extract_load.extract_transactions
	python3 -m extract_load.extract_prices
	python3 -m extract_load.extract_holders

transform:
	cd transform && dbt build

serve:
	python3 -m serve.build_web_data

web:
	cd web && npm run dev

all: extract transform serve

test:
	pytest tests/
	cd transform && dbt test

clean:
	rm -rf transform/target transform/dbt_packages transform/logs
