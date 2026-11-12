"""Turn adag deprecation warnings into errors, so a test run fails on a deprecated name."""

import warnings

import adag

warnings.simplefilter("error", adag.AdagDeprecationWarning)

try:
    _ = adag.Parameter  # a deprecated name
except adag.AdagDeprecationWarning as warning:
    print(warning)
