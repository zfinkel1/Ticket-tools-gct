# Parser registry — maps parser type name to implementation.
from . import frontgate, rhp, playwright_generic, rivers, tixr, taogroup

PARSERS = {
    "frontgate": frontgate.parse,
    "rhp":        rhp.parse,
    "playwright": playwright_generic.parse,
    "rivers":     rivers.parse,
    "tixr":       tixr.parse,
    "taogroup":   taogroup.parse,
}

# Which parsers need Playwright installed in CI.
NEEDS_PLAYWRIGHT = {"playwright", "rivers"}
