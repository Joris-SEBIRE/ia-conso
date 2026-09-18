PYTHON ?= /opt/homebrew/bin/python3.13
VENV := .venv
APP := build/IAConso.app
INSTALLED := /Applications/IAConso.app

.PHONY: venv print run app install restart stop uninstall clean

venv: $(VENV)/bin/python

$(VENV)/bin/python:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m pip install --quiet --upgrade pip
	$(VENV)/bin/python -m pip install --quiet -r requirements.txt

print: venv          ## Affiche en texte ce que le menu contiendrait
	PYTHONPATH=src $(VENV)/bin/python -m ia_conso --print

run: venv            ## Lance depuis les sources (Ctrl-C pour arrêter)
	PYTHONPATH=src $(VENV)/bin/python -m ia_conso

app:                 ## Construit build/IAConso.app
	./scripts/build_app.sh $(APP)

install: stop        ## Installe dans /Applications et lance
	./scripts/build_app.sh $(APP) $(INSTALLED)
	rm -rf $(INSTALLED)
	ditto $(APP) $(INSTALLED)
	open $(INSTALLED)

restart: stop        ## Relance l'app installée
	open $(INSTALLED)

stop:                ## Arrête toute instance
	-@pkill -f 'IAConso.app/Contents/MacOS' >/dev/null 2>&1 || true
	-@pkill -f -- '-m ia_conso' >/dev/null 2>&1 || true

uninstall: stop     ## Retire l'app, le LaunchAgent et l'état local
	-@launchctl unload -w ~/Library/LaunchAgents/fr.jsebire.ia-conso.plist >/dev/null 2>&1 || true
	rm -rf $(INSTALLED) ~/Library/LaunchAgents/fr.jsebire.ia-conso.plist
	rm -rf ~/Library/Application\ Support/IAConso

clean:
	rm -rf build $(VENV) src/ia_conso/__pycache__
