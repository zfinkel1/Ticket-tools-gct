# Parser registry — maps parser type name to implementation.
from . import frontgate, rhp, playwright_generic, rivers, tixr

PARSERS = {
    "frontgate": frontgate.parse,
    "rhp":        rhp.parse,
    "playwright": playwright_generic.parse,
    "rivers":     rivers.parse,
    "tixr":       tixr.parse,
}

# Which parsers need Playwright installed in CI.
NEEDS_PLAYWRIGHT = {"playwright", "rivers"}
