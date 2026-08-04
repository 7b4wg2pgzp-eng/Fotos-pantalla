import sys
import os
import qrcode

if len(sys.argv) < 2:
    print("Uso: python3 make_qr.py <url>")
    sys.exit(1)

url = sys.argv[1]
img = qrcode.make(url)
out = "qr_invitados.png"
img.save(out)
print(f"QR guardado en {out} -> apunta a {url}")

if sys.platform == "darwin":
    os.system(f'open "{out}"')
