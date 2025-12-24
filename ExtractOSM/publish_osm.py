# publish_osm.py
"""Publishes a classified OSM file to a PostGIS database using osm2pgsql.

This script acts as a safe and robust wrapper around the `osm2pgsql` command-line
tool. It is designed to be called from an automated build system.
"""

import argparse
import logging
import subprocess
import sys
from pathlib import Path
from typing import List


class CartoPublisher:
    """Orchestrates the osm2pgsql database import process."""

    def __init__(self, carto_dir: Path, logger: logging.Logger):
        """Initializes the CartoPublisher.

        Args:
            carto_dir (Path): The path to the 'openstreetmap-carto' project
                directory, which contains the `.lua` style file.
            logger (logging.Logger): A configured logger for output.
        """
        self.carto_dir = carto_dir
        self.logger = logger

    def publish(self, osm_file_path: Path):
        """
        Changes to the Carto directory and runs the osm2pgsql import command.

        Args:
            osm_file_path (Path): The path to the classified OSM PBF file to import.

        Raises:
            FileNotFoundError: If the carto_dir or osm_file_path do not exist.
            RuntimeError: If the osm2pgsql command fails.
        """
        if not self.carto_dir.is_dir():
            raise FileNotFoundError(f"Carto project directory not found: {self.carto_dir}")
        if not osm_file_path.is_file():
            raise FileNotFoundError(f"Input OSM file not found: {osm_file_path}")

        osm_file_abs_path = osm_file_path.resolve()

        command = [
            "osm2pgsql", "--append", "--slim", "-O", "flex",
            "-S", "openstreetmap-carto-flex.lua",
            "-d", "gis",  # Assumes the database name is 'gis'
            "--log-level=warn",
            str(osm_file_abs_path)
        ]

        self.logger.info(f"➡️ Publishing '{osm_file_path.name}' to PostGIS database...")
        self._run_command(command)
        self.logger.info("✅ OSM database ingest complete.")

    def _run_command(self, command: List[str]):
        """
        Executes an external command, streams its output, and handles errors.
        """
        command_str = " ".join(command)
        self.logger.info(f"   - Changing directory to: {self.carto_dir}")
        self.logger.info(f"   - Executing command: {command_str}")

        try:
            process = subprocess.Popen(
                command, cwd=self.carto_dir, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding='utf-8'
            )
            for line in iter(process.stdout.readline, ''):
                self.logger.info(f"     | {line.strip()}")
            process.stdout.close()
            return_code = process.wait()
            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, command_str)
        except FileNotFoundError:
            self.logger.error("❌ Command failed: 'osm2pgsql' not found.")
            self.logger.error("   Please ensure osm2pgsql is installed and in your system's PATH.")
            raise
        except subprocess.CalledProcessError as e:
            self.logger.error(f"❌ Command failed with exit code {e.returncode}:")
            self.logger.error(f"   - Command: {e.cmd}")
            raise RuntimeError("osm2pgsql import process failed.") from e


def setup_logger(level: int) -> logging.Logger:
    """Initializes and configures the logger for the script."""
    logger = logging.getLogger("publish_osm")
    logger.setLevel(level * 10) # Standard levels are multiples of 10
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter("%(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def main():
    """Orchestration layer: Handles I/O, config, and calls the publisher."""
    parser = argparse.ArgumentParser(
        description="Publish a classified OSM file to a PostGIS DB."
    )
    parser.add_argument(
        "--input", type=Path, required=True,
        help="Path to the classified OSM file to import."
    )
    parser.add_argument(
        "--carto-dir", type=Path, required=True,
        help="Path to the 'openstreetmap-carto' project directory."
    )
    parser.add_argument(
        "--log-level", type=int, choices=range(0, 6), default=5,
        help="Set log level (0=quiet, 5=verbose). Default is 5."
    )
    args = parser.parse_args()

    logger = setup_logger(args.log_level)

    logger.info("\n*️⃣ OSM Publish Configuration:")
    logger.info(f"   - Input OSM: {args.input}")
    logger.info(f"   - Carto Dir: {args.carto_dir}")
    print()

    try:
        publisher = CartoPublisher(args.carto_dir, logger)
        publisher.publish(args.input)

    except (FileNotFoundError, RuntimeError) as e:
        logger.error(f"\n   ❌ Publish failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()