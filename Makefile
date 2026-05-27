PYTHON := python3
SRC    := src

.PHONY: install scrape gauge all docker-build docker-run

install:
	$(PYTHON) -m pip install -r requirements.txt
	$(PYTHON) -m playwright install chromium

scrape:
	$(PYTHON) -m src.scraper

gauge:
	$(PYTHON) -m src.gauge_reader

all: scrape gauge

docker-build:
	docker build -t tactacam-scraper .

docker-run:
	docker run --env-file .env tactacam-scraper
