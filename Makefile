.PHONY: collect build all clean

all: collect build

collect:
	@echo "=== Collecting commit data ==="
	bash scripts/collect.sh

build:
	@echo "=== Building dashboard ==="
	python3 scripts/build.py

clean:
	rm -f data/raw_commits.tsv index.html
