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
    {
        "name": "Tixr — Chicago",
        "parser": "tixr",
        "city": "chicago",
        "page_size": 50,
        # ScraperAPI free tier is ~100 premium requests/month. Check once every
        # 6 hours = 120/month (close but fine). If we burn through, bump higher.
        "min_interval_hours": 6,
    },
    {
        "name": "TAO Nightclub Chicago",
        "parser": "taogroup",
        "venue_id": 131,
    },
    {
        "name": "Salt Shed",
        "parser": "ticketmaster",
        # Indoor venue + Outdoor fairgrounds — both are TM-sold
        "venue_ids": ["KovZ917AI5F", "KovZ917Amf0"],
    },
    {
        "name": "Byline Bank Aragon Ballroom",
        "parser": "ticketmaster",
        "venue_ids": ["KovZpZAFdJnA"],
    },
    {
        "name": "Rosemont Theatre",
        "parser": "ticketmaster",
        "venue_ids": ["KovZpa2BOe"],
    },
    {
        "name": "House of Blues Chicago",
        "parser": "ticketmaster",
        # Main room + Foundation Room + Backporch Stage all run through TM
        "venue_ids": ["KovZpZAEAIlA", "KovZ917AR_1", "KovZ917ARgJ"],
    },
    {
        "name": "Park West",
        "parser": "ticketmaster",
        "venue_ids": ["KovZpZAan6AA"],
    },
    {
        "name": "Thalia Hall",
        "parser": "ticketmaster",
        "venue_ids": ["KovZpZAJntvA"],
    },
]
