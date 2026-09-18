#!/usr/bin/env bash
# Construit IAConso.app : bundle autonome (interpréteur + dépendances + sources),
# sans icône dans le Dock, juste un élément dans la barre des menus.
#
# Le bundle est un venv dont la racine est Contents/, avec une copie du binaire
# Python dans Contents/MacOS/ : c'est ce qui permet à macOS d'identifier le
# processus comme IAConso.app (nom, lancement au démarrage, `quit app`).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="${1:-$ROOT/build/IAConso.app}"
# Emplacement où le bundle tournera : son chemin est cuit dans l'Info.plist, parce que
# Launch Services démarre l'interpréteur avec `argv=['']` et que, dans ce cas, il ne retrouve
# pas seul les paquets du venv. `make install` passe donc /Applications ici.
FINAL="${2:-$APP}"
VERSION="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$ROOT/src/ia_conso/__init__.py")"
PYTHON="${PYTHON:-}"
for candidate in "$PYTHON" /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 "$(command -v python3 || true)"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then PYTHON="$candidate"; break; fi
done
[ -n "$PYTHON" ] || { echo "python3 introuvable" >&2; exit 1; }
# Le repli sur le python3 du PATH tombe sur celui d'Apple, trop vieux pour pyobjc et non
# framework : PyObjC y construirait une app incapable de tenir un élément de barre. On refuse
# plutôt que de livrer un bundle qui échoue au lancement.
"$PYTHON" - <<'CHECK' || { echo "→ installe Python 3.12+ (brew install python@3.13)" >&2; exit 1; }
import os, sys
ok = sys.version_info >= (3, 12)
framework = os.path.exists(os.path.join(sys.base_prefix, "Resources", "Python.app"))
if not ok:
    print(f"python trop ancien : {sys.version.split()[0]}, il faut 3.12 ou plus", file=sys.stderr)
if ok and not framework:
    print("cet interpréteur n'est pas une installation framework", file=sys.stderr)
sys.exit(0 if ok and framework else 1)
CHECK

echo "→ $APP (python: $PYTHON, version: $VERSION)"
case "$APP" in
    *.app) ;;
    *) echo "cible refusée : « $APP » n'est pas un bundle .app" >&2; exit 1 ;;
esac
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

"$PYTHON" -m venv "$APP/Contents"
"$APP/Contents/bin/python" -m pip install --quiet --upgrade pip
"$APP/Contents/bin/python" -m pip install --quiet -r "$ROOT/requirements.txt"

# Vrai interpréteur : bin/python3.x d'un build framework n'est qu'un stub qui se
# ré-exécute via Resources/Python.app, ce qui ferait perdre l'identité du bundle.
REAL_PYTHON="$("$APP/Contents/bin/python" -c '
import os, sys
app = os.path.join(sys.base_prefix, "Resources", "Python.app", "Contents", "MacOS", "Python")
print(app if os.path.exists(app) else os.path.realpath(sys._base_executable))')"
cp "$REAL_PYTHON" "$APP/Contents/MacOS/IAConso"
chmod +x "$APP/Contents/MacOS/IAConso"

# Cette copie porte encore la signature de Python. Launch Services refuse de lancer un bundle
# dont l'exécutable principal s'annonce sous une autre identité que la sienne — erreur -54.
SIGNING="$(mktemp -t iaconso-exe)"
cp "$APP/Contents/MacOS/IAConso" "$SIGNING"
codesign --force --sign - --identifier fr.jsebire.ia-conso "$SIGNING" 2>/dev/null
cp "$SIGNING" "$APP/Contents/MacOS/IAConso"
rm -f "$SIGNING"
chmod +x "$APP/Contents/MacOS/IAConso"

SITE="$(echo "$APP"/Contents/lib/python*/site-packages)"
[ -d "$SITE" ] || { echo "site-packages introuvable dans le bundle" >&2; exit 1; }
FINAL_SITE="$FINAL/${SITE#$APP/}"

cp -R "$ROOT/src/ia_conso" "$APP/Contents/Resources/ia_conso"
find "$APP/Contents/Resources/ia_conso" -name '__pycache__' -type d -exec rm -rf {} +

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>IA-Conso</string>
  <key>CFBundleDisplayName</key><string>IA-Conso</string>
  <key>CFBundleIdentifier</key><string>fr.jsebire.ia-conso</string>
  <key>CFBundleExecutable</key><string>IAConso</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSEnvironment</key><dict>
    <key>PYTHONPATH</key><string>$FINAL_SITE</string>
    <key>PYTHONDONTWRITEBYTECODE</key><string>1</string>
  </dict>
</dict>
</plist>
PLIST

cp "$ROOT/scripts/sitecustomize.py" "$SITE/sitecustomize.py"

ident="$(codesign -dv "$APP/Contents/MacOS/IAConso" 2>&1 | sed -n 's/^Identifier=//p')"
[ "$ident" = "fr.jsebire.ia-conso" ] || { echo "exécutable signé « $ident » au lieu de fr.jsebire.ia-conso" >&2; exit 1; }
file -b "$APP/Contents/MacOS/IAConso" | grep -q "Mach-O" \
    || { echo "l'exécutable principal doit être l'interpréteur, pas un script" >&2; exit 1; }
[ -f "$SITE/sitecustomize.py" ] || { echo "amorce sitecustomize.py absente du bundle" >&2; exit 1; }
plutil -extract LSEnvironment.PYTHONPATH raw "$APP/Contents/Info.plist" 2>/dev/null \
    | grep -q "site-packages" || { echo "PYTHONPATH absent de l'Info.plist" >&2; exit 1; }

rm -f "$APP/Contents/.gitignore"
echo "✓ $APP"
