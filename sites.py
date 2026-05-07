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
        # ScraperAPI Hobby plan: 100k credits/month. Tixr uses premium=true
        # (10 credits/call). At 1h interval = 24 calls/day × 10 = 7,200/month.
        # Catches flash announcements in <1h instead of up to 6h.
        "min_interval_hours": 1,
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
    {
        "name": "Riviera Theatre",
        "parser": "ticketmaster",
        "venue_ids": ["KovZpZAan6kA", "rZ7HnEZ17uMF4"],
    },
    {
        "name": "Concord Music Hall",
        "parser": "ticketmaster",
        "venue_ids": ["KovZpZAJtF7A"],
    },
    {
        "name": "Radius Chicago",
        "parser": "ticketmaster",
        "venue_ids": ["Z7r9jZa77B", "KovZ917AESx"],
    },
    {
        "name": "Lincoln Hall",
        "parser": "ticketmaster",
        "venue_ids": ["KovZpZAdldtA"],
    },
    {
        "name": "The Vic Theatre",
        "parser": "ticketmaster",
        "venue_ids": ["KovZpZA17AEA"],
    },
    {
        "name": "Bottom Lounge",
        "parser": "ticketmaster",
        "venue_ids": ["KovZpaoyCe"],
    },
    {
        "name": "Subterranean",
        "parser": "ticketmaster",
        # Main room + Downstairs run as separate TM venue records
        "venue_ids": ["KovZpZA1EktA", "KovZpZA6vJlA"],
    },
    {
        "name": "Sleeping Village",
        "parser": "ticketmaster",
        "venue_ids": ["KovZpa2h8e"],
    },
    {
        "name": "The Empty Bottle",
        "parser": "ticketmaster",
        "venue_ids": ["KovZpZAId16A"],
    },
    {
        "name": "Avondale Music Hall",
        "parser": "ticketmaster",
        "venue_ids": ["KovZ917AmTi"],
    },
    {
        "name": "Beat Kitchen",
        "parser": "ticketmaster",
        "venue_ids": ["KovZpZAJAn1A"],
    },
    {
        "name": "United Center",
        "parser": "ticketmaster",
        "venue_ids": ["KovZpa2M7e"],
    },
    {
        "name": "Allstate Arena",
        "parser": "ticketmaster",
        "venue_ids": ["KovZpa2MCe"],
    },
    {
        "name": "Wintrust Arena",
        "parser": "ticketmaster",
        "venue_ids": ["KovZ917A2S0"],
    },
]
