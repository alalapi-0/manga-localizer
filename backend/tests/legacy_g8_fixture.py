"""Construct pre-retirement G8 history without disabling replay validation.

Use only around historical fixture construction or explicitly historical tests.
Never autouse this context: current API/queue policy tests must run unpatched.
There is no corresponding production configuration switch.
"""

from contextlib import contextmanager
from unittest.mock import patch

from manga_localizer.services import clean_plates


@contextmanager
def historical_local_g8():
    with patch.object(clean_plates, "require_local_clean_plate_write", return_value=None):
        yield
