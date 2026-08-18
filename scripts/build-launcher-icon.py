from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "ui" / "public" / "icons" / "icon-512.png"
TARGET = ROOT / "assets" / "launcher" / "x-omni.ico"


def main() -> None:
    if not SOURCE.is_file():
        raise SystemExit(f"Source icon is missing: {SOURCE}")
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(SOURCE) as image:
        image.convert("RGBA").save(
            TARGET,
            format="ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )
    print(TARGET)


if __name__ == "__main__":
    main()
