# Parser registry — maps parser type name to implementation.
from . import frontgate, rhp, playwright_generic

PARSERS = {
    "frontgate": frontgate.parse,
    "rhp":        rhp.parse,
    "playwright": playwright_generic.parse,
}

# Which parsers need Playwright installed in CI. Used by the workflow to
# skip the heavy install on runs where no playwright site is configured.
NEEDS_PLAYWRIGHT = {"playwright"}
