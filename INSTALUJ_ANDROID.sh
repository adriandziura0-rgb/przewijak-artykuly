#!/data/data/com.termux/files/usr/bin/bash
set -e
cd "$(dirname "$0")"
echo
echo "========================================================="
echo " PRZEWIJAK ARTYKULY V15.0 ETAP 1 MULTISOURCE - ANDROID / TERMUX"
echo "========================================================="
echo
if ! command -v python >/dev/null 2>&1; then
  echo "Instaluje Python..."
  pkg install -y python
fi
python -m pip install -r requirements.txt

echo
if ! command -v termux-saf-managedir >/dev/null 2>&1; then
  echo "Instaluje pakiet polecen Termux:API potrzebny do systemowego wyboru folderu..."
  if ! pkg install -y termux-api; then
    echo "UWAGA: nie udalo sie zainstalowac pakietu termux-api."
    echo "Program ruszy, ale pelny systemowy wybor karty SD bedzie niedostepny."
  fi
fi

echo
if [ ! -e "$HOME/storage/downloads" ]; then
  echo "Dla zwyklego dostepu do Pobrane wykonaj jednorazowo: termux-setup-storage"
fi

echo
if command -v termux-saf-managedir >/dev/null 2>&1; then
  echo "SAF: polecenia Termux:API sa zainstalowane."
  if command -v timeout >/dev/null 2>&1 && timeout 12s termux-saf-dirs >/dev/null 2>&1; then
    echo "SAF: aplikacja Termux:API odpowiada - systemowy wybor folderu jest gotowy."
  else
    echo "UWAGA: polecenia sa, ale aplikacja Termux:API nie odpowiedziala."
    echo "Do systemowego wyboru folderu na SD potrzebna jest aplikacja Termux:API"
    echo "z tego samego zrodla/podpisu co uzywany Termux."
  fi
  echo "Przewijak najpierw sprobuje starszego bezposredniego /storage/UUID, jesli Android nadal go udostepnia."
else
  echo "SAF: brak polecen Termux:API - zostanie awaryjny picker zwyklych katalogow."
fi

echo
echo "GOTOWE. Uruchom: bash START_ANDROID.sh"
echo "Panel: http://127.0.0.1:8765"
