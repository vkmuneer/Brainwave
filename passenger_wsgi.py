"""Entry point for cPanel / Passenger hosting.

cPanel's "Setup Python App" runs the site through Passenger, which imports this
file and looks for a WSGI callable named `application` - it does not run
run.py. Kept separate from run.py so local development stays a plain
`python run.py`.
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Passenger does not necessarily start with the project on sys.path.
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(BASE_DIR, ".env"))

from app import create_app  # noqa: E402

application = create_app()
