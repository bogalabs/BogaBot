"""Punto de entrada del bot. Ejecutá:  python run.py

Ajusta sys.path para que el paquete `bogabot` (en src/) sea importable sin
necesidad de instalar el proyecto.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent / "src"))

from bogabot.main import main  # noqa: E402

if __name__ == "__main__":
    main()
