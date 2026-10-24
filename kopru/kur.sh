#!/bin/bash
# OBSKURA Baskı Köprüsü kurulumu (Mac). Yönetici şifresi istemez; yalnız bu kullanıcı için kurar.
# Kaldırmak için:  launchctl bootout gui/$(id -u)/com.obskura.kopru; rm ~/Library/LaunchAgents/com.obskura.kopru.plist
set -e
KAYNAK="https://burakisik88-ctrl.github.io/booth/kopru"
DIZIN="$HOME/OBSKURA-Baski"
PLIST="$HOME/Library/LaunchAgents/com.obskura.kopru.plist"
ETIKET="com.obskura.kopru"
echo "== OBSKURA baskı köprüsü kuruluyor =="
mkdir -p "$DIZIN/isler" "$HOME/Library/LaunchAgents"
curl -fsSL "$KAYNAK/kopru.py?v=$(date +%s)" -o "$DIZIN/kopru.py.yeni"
grep -q "OBSKURA Baskı Köprüsü" "$DIZIN/kopru.py.yeni" || { echo "HATA: köprü dosyası indirilemedi"; exit 1; }
mv "$DIZIN/kopru.py.yeni" "$DIZIN/kopru.py"
PY="$(/usr/bin/python3 -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
[ -x "$PY" ] || { echo "HATA: python3 bulunamadı"; exit 1; }
echo "python: $PY ($("$PY" -c 'import sys;print(sys.version.split()[0])'))"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$ETIKET</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIZIN/kopru.py</string></array>
  <key>WorkingDirectory</key><string>$DIZIN</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIZIN/launchd.log</string>
  <key>StandardErrorPath</key><string>$DIZIN/launchd.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/$ETIKET" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$ETIKET" 2>/dev/null || true
for i in 1 2 3 4 5 6 7 8; do
  sleep 1
  curl -fs "http://127.0.0.1:8740/durum?taze" -o "$DIZIN/durum.json" 2>/dev/null && break
done
[ -s "$DIZIN/durum.json" ] || { echo "HATA: köprü başlamadı. Son satırlar:"; tail -n 15 "$DIZIN/launchd.log" 2>/dev/null; exit 1; }
"$PY" - "$DIZIN/durum.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print("köprü sürümü :", d.get("surum"))
print("yazıcı       :", d.get("yazici"))
k = d.get("kagit4x6") or {}
print("4x6 kağıt    :", k.get("ad"), k.get("yazi"), k.get("mm"))
print("yazıcı hali  :", d.get("durum"), "| nedenler:", d.get("nedenler"), "| kuyruk:", d.get("kuyruk"))
print("kağıtlar     :", ", ".join("%s=%sx%s" % (x["ad"], x["mm"][0], x["mm"][1]) for x in d.get("kagitlar", [])))
for s in d.get("secenekler", []):
    print("seçenek      : %s (%s) varsayılan=%s -> %s" % (s["anahtar"], s["yazi"], s.get("varsayilan"), ", ".join(c["ad"] for c in s["secimler"])))
print()
print("KURULUM TAMAM — köprü çalışıyor." if d.get("ok") else "DİKKAT: " + str(d.get("hata")))
PYEOF
