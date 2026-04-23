"""
Sites to watch. Each entry specifies a parser type and whatever config
that parser needs. Add or remove entries freely — no other code changes.

Parser types:
  - frontgate  : FrontGate Tickets (Webflow CMS)
  - rhp        : Rockhouse Partners venue CMS (Metro Chicago, many indie venues)
  - playwright : Headless browser for bot-protected sites (Rivers Casino, etc.)
"""

SITES = [
    {
        "name": "FrontGate Tickets",
        "parser": "frontgate",
        "url": "https://www.frontgatetickets.com/events",
    },
    {
        "name": "Metro Chicago",
        "parser": "rhp",
        "url": "https://metrochicago.com/events",
    },
    {
        "name": "Rivers Casino Des Plaines",
        "parser": "rivers",
        "url": "https://www.riverscasino.com/desplaines/entertainment/event-center",
    },
]
