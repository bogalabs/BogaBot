"""Bootstrap: configura logging, carga settings y arranca el bot."""
from __future__ import annotations

import logging

from bogabot.bot import BogaBot
from bogabot.settings import ConfigError, Settings


def main() -> None:
    try:
        settings = Settings.load()
    except ConfigError as exc:
        raise SystemExit(f"[config] {exc}")

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    bot = BogaBot(settings)
    # log_handler=None: usamos nuestra propia config de logging (no la de discord.py).
    bot.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
