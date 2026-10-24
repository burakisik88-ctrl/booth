#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OBSKURA Baskı Köprüsü — Mac'te arka planda çalışır, kabinden gelen fotoğrafı DNP'ye basar.

Neden var: Chrome'un yazdır ekranı eni boyundan büyük kağıtları (DNP 4x6 = 156x105 mm) kendi içinde
dik çeviriyor (Chromium hata 40267436); Mac bu ölçüyü sürücüde bulamayıp "özel kağıt" gönderiyor,
DNP de basmıyor. Köprü tarayıcıyı aradan çıkarır: fotoğrafı sürücünün KENDİ kağıt adıyla, tam o
kağıt ölçüsünde bir PDF'e koyup CUPS'a (lp) verir. Pencere çıkmaz.

Yalnız bu bilgisayardan (127.0.0.1) bağlantı kabul eder; baskı isteği yalnız izinli adreslerden
(kabin sitesi) gelir. Sadece Python'un kendi kütüphanesi kullanılır, ek kurulum yok.

"""
import json, os, re, subprocess, sys, time, threading, struct, glob
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SURUM = "1.1"
PORT = int(os.environ.get("OBSKURA_PORT", "8740"))
EV = os.path.expanduser("~/OBSKURA-Baski")
ISLER = os.path.join(EV, "isler")
GUNLUK = os.path.join(EV, "kopru.log")
IZINLI = set(filter(None, os.environ.get("OBSKURA_IZINLI", "").split(","))) | {
    "https://burakisik88-ctrl.github.io",
}
PPD_DIZIN = os.environ.get("OBSKURA_PPD_DIZIN", "/etc/cups/ppd")
LP = os.environ.get("OBSKURA_LP", "lp")
LPSTAT = os.environ.get("OBSKURA_LPSTAT", "lpstat")
LPOPTIONS = os.environ.get("OBSKURA_LPOPTIONS", "lpoptions")
SRGB_ADAYLAR = [
    "/System/Library/ColorSync/Profiles/sRGB Profile.icc",
    "/System/Library/ColorSync/Profiles/sRGB IEC61966-2.1.icc",
]
NTFY = os.environ.get("OBSKURA_NTFY", "https://ntfy.sh").rstrip("/")
KABIN = "https://burakisik88-ctrl.github.io/booth/"
os.makedirs(ISLER, exist_ok=True)
_kilit = threading.Lock()


def istasyon_kodu():
    """Bu yazıcı istasyonunun kalıcı kodu (ilk açılışta rastgele üretilir)."""
    yol = os.path.join(EV, "istasyon.json")
    try:
        with open(yol, encoding="utf-8") as f:
            k = json.load(f).get("kod")
            if k and re.fullmatch(r"[a-z0-9]{8,32}", k):
                return k
    except Exception:
        pass
    import secrets
    k = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(12))
    with open(yol, "w", encoding="utf-8") as f:
        json.dump({"kod": k, "olusturma": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    return k


KOD = istasyon_kodu()


def gunluk(*a):
    s = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    try:
        with open(GUNLUK, "a", encoding="utf-8") as f:
            f.write(s + "\n")
    except Exception:
        pass
    print(s, flush=True)


def calistir(komut, zaman=15):
    try:
        r = subprocess.run(komut, capture_output=True, text=True, timeout=zaman)
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except Exception as e:
        return 99, "", str(e)


# ---------------------------------------------------------------- yazıcı ve PPD
def yazici_bul():
    """DNP kuyruğunun adı. lpstat -e dile bağlı değil (yalnız adlar)."""
    zorla = os.environ.get("OBSKURA_YAZICI")
    if zorla:
        return zorla
    kod, cikti, _ = calistir([LPSTAT, "-e"])
    adlar = [x.strip() for x in cikti.splitlines() if x.strip()]
    for ad in adlar:
        if re.search(r"DS.?RX1|Dai_?Nippon|DNP", ad, re.I):
            return ad
    return None


def ppd_oku(yazici):
    yol = os.path.join(PPD_DIZIN, yazici + ".ppd")
    try:
        with open(yol, "rb") as f:
            return f.read().decode("latin-1", "replace")
    except Exception:
        return ""


def ppd_coz(metin):
    """Kağıtlar (ad, metin, pt) ve PickOne seçenekleri."""
    kagit = {}
    for m in re.finditer(r'^\*PaperDimension\s+([^/:\s]+)(?:/([^:]*))?:\s*"([\d.]+)\s+([\d.]+)"', metin, re.M):
        ad, yazi, w, h = m.group(1), (m.group(2) or m.group(1)).strip(), float(m.group(3)), float(m.group(4))
        kagit[ad] = {"ad": ad, "yazi": yazi, "pt": [w, h], "mm": [round(w / 72 * 25.4, 1), round(h / 72 * 25.4, 1)]}
    secenek = []
    for m in re.finditer(r'^\*OpenUI\s+\*([^/:\s]+)(?:/([^:]*))?:\s*(PickOne|Boolean)', metin, re.M):
        anahtar, yazi = m.group(1), (m.group(2) or m.group(1)).strip()
        if anahtar in ("PageSize", "PageRegion"):
            continue
        son = metin.find("*CloseUI", m.end())
        govde = metin[m.end(): son if son > 0 else len(metin)]
        var = re.search(r'^\*Default' + re.escape(anahtar) + r':\s*(\S+)', metin, re.M)
        secimler = []
        for c in re.finditer(r'^\*' + re.escape(anahtar) + r'\s+([^/:\s]+)(?:/([^:]*))?:', govde, re.M):
            secimler.append({"ad": c.group(1), "yazi": (c.group(2) or c.group(1)).strip()})
        if secimler:
            secenek.append({"anahtar": anahtar, "yazi": yazi, "varsayilan": var.group(1) if var else None, "secimler": secimler})
    varsayilan_kagit = re.search(r'^\*DefaultPageSize:\s*(\S+)', metin, re.M)
    return kagit, secenek, (varsayilan_kagit.group(1) if varsayilan_kagit else None)


def dnp4x6(kagitlar):
    """156x105 mm (6.15x4.13 in) kağıt. Önce yatay, sonra dik tanımı kabul et."""
    en_iyi, fark0 = None, 9e9
    for k in kagitlar.values():
        w, h = k["pt"]
        for (a, b) in ((w, h), (h, w)):
            fark = abs(a - 442.2) + abs(b - 297.6)
            if fark < fark0:
                en_iyi, fark0 = k, fark
    return en_iyi if fark0 <= 12 else None


def yazici_hali(yazici):
    hal = {"durum": None, "nedenler": None, "kabul": None, "mesaj": ""}
    kod, cikti, _ = calistir([LPOPTIONS, "-p", yazici])
    for m in re.finditer(r"(printer-state|printer-state-reasons|printer-is-accepting-jobs)=('[^']*'|\S+)", cikti):
        k, v = m.group(1), m.group(2).strip("'")
        if k == "printer-state":
            hal["durum"] = {"3": "boşta", "4": "basıyor", "5": "durdu"}.get(v, v)
        elif k == "printer-state-reasons":
            hal["nedenler"] = v
        else:
            hal["kabul"] = (v == "true")
    kod, cikti, _ = calistir([LPSTAT, "-l", "-p", yazici])
    hal["mesaj"] = " ".join(cikti.split())[:300]
    kod, cikti, _ = calistir([LPSTAT, "-o", yazici])
    hal["kuyruk"] = len([x for x in cikti.splitlines() if x.strip()])
    return hal


_onbellek = {"t": 0, "v": None}


def durum(taze=False):
    if not taze and _onbellek["v"] and time.time() - _onbellek["t"] < 3:
        return _onbellek["v"]
    y = yazici_bul()
    d = {"ok": False, "surum": SURUM, "yazici": y,
         "istasyon": {"kod": KOD}}
    if not y:
        d["hata"] = "DNP yazıcı bulunamadı (lpstat -e listesinde yok). Yazıcı açık ve USB takılı mı?"
    else:
        kagitlar, secenekler, vars_kagit = ppd_coz(ppd_oku(y))
        k = dnp4x6(kagitlar)
        d.update({
            "kagit4x6": k, "kagitlar": list(kagitlar.values()), "varsayilanKagit": vars_kagit,
            "secenekler": secenekler,
        })
        d.update(yazici_hali(y))
        if not kagitlar:
            d["hata"] = "Sürücü dosyası (PPD) okunamadı: " + os.path.join(PPD_DIZIN, y + ".ppd")
        elif not k:
            d["hata"] = "Sürücüde 156x105 mm (4x6) kağıt bulunamadı."
        else:
            d["ok"] = True
    _onbellek.update(t=time.time(), v=d)
    return d


# ---------------------------------------------------------------- PDF
def jpeg_boyut(b):
    i = 2
    while i < len(b) - 9:
        if b[i] != 0xFF:
            i += 1
            continue
        isaret = b[i + 1]
        if isaret in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", b[i + 5:i + 9])
            return w, h
        if isaret in (0xD8, 0x01) or 0xD0 <= isaret <= 0xD7:
            i += 2
            continue
        uz = struct.unpack(">H", b[i + 2:i + 4])[0]
        i += 2 + uz
    raise ValueError("JPEG ölçüsü okunamadı")


def jpeg_pdf(jpeg, w_pt, h_pt, cevir=False):
    """JPEG'i sayfayı TAM dolduracak şekilde, kağıt ölçüsünde tek sayfalık PDF'e koy (sRGB)."""
    gw, gh = jpeg_boyut(jpeg)
    icc = None
    for yol in SRGB_ADAYLAR:
        if os.path.exists(yol):
            with open(yol, "rb") as f:
                icc = f.read()
            break
    nesneler = []
    nesneler.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    nesneler.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    nesneler.append(("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.3f %.3f] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>" % (w_pt, h_pt)).encode())
    renk = "/ICCBased 6 0 R" if icc else "/DeviceRGB"
    nesneler.append((("<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace %s /BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n" % (gw, gh, renk, len(jpeg))).encode() + jpeg + b"\nendstream"))
    if cevir:  # görüntünün eni sayfanın boyuna: 90° saat yönü tersine
        icerik = ("q 0 %.3f %.3f 0 %.3f 0 cm /Im0 Do Q" % (h_pt, -w_pt, w_pt)).encode()
    else:
        icerik = ("q %.3f 0 0 %.3f 0 0 cm /Im0 Do Q" % (w_pt, h_pt)).encode()
    nesneler.append(b"<< /Length %d >>\nstream\n" % len(icerik) + icerik + b"\nendstream")
    if icc:
        nesneler.append(b"<< /N 3 /Alternate /DeviceRGB /Length %d >>\nstream\n" % len(icc) + icc + b"\nendstream")
    cikti = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    konum = []
    for i, n in enumerate(nesneler, 1):
        konum.append(len(cikti))
        cikti += b"%d 0 obj\n" % i + n + b"\nendobj\n"
    xref = len(cikti)
    cikti += b"xref\n0 %d\n0000000000 65535 f \n" % (len(nesneler) + 1)
    for k in konum:
        cikti += b"%010d 00000 n \n" % k
    cikti += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(nesneler) + 1, xref)
    return bytes(cikti)


# ---------------------------------------------------------------- baskı
def bas(jpeg, kopya, secimler):
    d = durum(taze=True)
    if not d.get("ok"):
        return {"ok": False, "hata": d.get("hata", "yazıcı hazır değil"), "durum": d}
    k = d["kagit4x6"]
    w, h = k["pt"]
    gw, gh = jpeg_boyut(jpeg)
    # yüzey yatay gelir (1843x1240). Sürücü kağıdı dik tanımlıysa görüntü PDF içinde 90° çevrilir.
    cevir = (gw > gh) != (w > h)
    izin = {s["anahtar"]: {c["ad"] for c in s["secimler"]} for s in d["secenekler"]}
    secenek_arg = []
    for s in secimler:
        if "=" not in s:
            continue
        a, b = s.split("=", 1)
        if a in izin and b in izin[a]:
            secenek_arg += ["-o", "%s=%s" % (a, b)]
        else:
            return {"ok": False, "hata": "Sürücüde olmayan seçenek: %s" % s}
    kopya = max(1, min(20, int(kopya or 1)))
    ad = time.strftime("%Y%m%d-%H%M%S-") + str(int(time.time() * 1000) % 1000)
    pdf_yol = os.path.join(ISLER, ad + ".pdf")
    with open(pdf_yol, "wb") as f:
        f.write(jpeg_pdf(jpeg, w, h, cevir))
    komut = [LP, "-d", d["yazici"], "-n", str(kopya), "-t", "OBSKURA " + ad,
             "-o", "PageSize=" + k["ad"], "-o", "print-scaling=none", "-o", "fit-to-page=false"] + secenek_arg + [pdf_yol]
    with _kilit:
        kod, cikti, hata = calistir(komut, 30)
    gunluk("bas", kopya, " ".join(komut[1:-1]), "->", kod, cikti.strip(), hata.strip())
    m = re.search(r"(\S+-\d+)", cikti)
    if kod != 0:
        return {"ok": False, "hata": (hata or cikti or "lp başarısız").strip()[:300]}
    _temizle()
    return {"ok": True, "is": m.group(1) if m else None, "kagit": k["ad"], "kopya": kopya, "cevrildi": cevir}


def is_durumu(no):
    kod, cikti, _ = calistir([LPSTAT, "-W", "not-completed", "-o"])
    bekliyor = any(line.split()[0] == no for line in cikti.splitlines() if line.strip())
    d = durum(taze=True)
    return {"ok": True, "is": no, "bekliyor": bekliyor, "yazici": {k: d.get(k) for k in ("durum", "nedenler", "kabul", "mesaj", "kuyruk")}}


def _temizle():
    dosyalar = sorted(glob.glob(os.path.join(ISLER, "*.pdf")))
    for f in dosyalar[:-200]:  # son 200 iş kalsın (yeniden basmak gerekirse)
        try:
            os.remove(f)
        except Exception:
            pass


# ---------------------------------------------------------------- HTTP
class Isleyici(BaseHTTPRequestHandler):
    server_version = "OBSKURA-Kopru/" + SURUM

    def log_message(self, *a):
        pass

    def _baslik(self, kod, tur="application/json; charset=utf-8"):
        self.send_response(kod)
        koken = self.headers.get("Origin")
        if koken and (koken in IZINLI or koken.startswith("http://127.0.0.1") or koken.startswith("http://localhost")):
            self.send_header("Access-Control-Allow-Origin", koken)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", tur)
        self.end_headers()

    def _json(self, kod, veri):
        b = json.dumps(veri, ensure_ascii=False).encode("utf-8")
        self._baslik(kod)
        self.wfile.write(b)

    def do_OPTIONS(self):
        self._baslik(204)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/durum"):
            return self._json(200, durum(taze=u.path == "/durum" and "taze" in u.query))
        if u.path == "/is":
            no = (parse_qs(u.query).get("no") or [""])[0]
            return self._json(200, is_durumu(no))
        self._json(404, {"ok": False, "hata": "yok"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/bas":
            return self._json(404, {"ok": False, "hata": "yok"})
        koken = self.headers.get("Origin")
        if koken and not (koken in IZINLI or koken.startswith("http://127.0.0.1") or koken.startswith("http://localhost")):
            gunluk("reddedildi", koken)
            return self._json(403, {"ok": False, "hata": "izinsiz adres: " + koken})
        uz = int(self.headers.get("Content-Length") or 0)
        if uz <= 0 or uz > 25_000_000:
            return self._json(400, {"ok": False, "hata": "görüntü yok ya da çok büyük"})
        govde = self.rfile.read(uz)
        if govde[:2] != b"\xff\xd8":
            return self._json(400, {"ok": False, "hata": "JPEG bekleniyordu"})
        q = parse_qs(u.query)
        try:
            sonuc = bas(govde, (q.get("kopya") or ["1"])[0], q.get("o") or [])
        except Exception as e:
            gunluk("HATA", repr(e))
            sonuc = {"ok": False, "hata": "köprü hatası: " + str(e)[:200]}
        self._json(200 if sonuc.get("ok") else 500, sonuc)


def main():
    if "--durum" in sys.argv:
        print(json.dumps(durum(taze=True), ensure_ascii=False, indent=1))
        return
    sunucu = ThreadingHTTPServer(("127.0.0.1", PORT), Isleyici)
    gunluk("OBSKURA köprü %s dinliyor 127.0.0.1:%d · istasyon %s" % (SURUM, PORT, KOD))
    sunucu.serve_forever()


if __name__ == "__main__":
    main()
