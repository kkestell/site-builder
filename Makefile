.PHONY: build rebuild clean deploy serve watch default

build:
	pdm run python src/builder/build.py

rebuild:
	pdm run python src/builder/build.py -f

clean:
	rm -rf dist

deploy: clean build
	rsync -avz dist/* kkestell_kestell@ssh.nyc1.nearlyfreespeech.net:/home/public/

serve:
	pdm run python src/builder/serve.py

watch:
	pdm run python src/builder/watch.py

default: build
