"""Génère les icônes Windows et navigateur à partir du logo Joboard livré."""

from pathlib import Path

from PIL import Image


def main() -> None:
    base = Path(__file__).resolve().parent
    out = base / "icon.ico"
    with Image.open(base / "web" / "assets" / "brand" / "icon-512.png") as image:
        image.convert("RGBA").save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    (base / "web" / "favicon.ico").write_bytes(out.read_bytes())
    print(f"Icône créée : {out}")


if __name__ == "__main__":
    main()
