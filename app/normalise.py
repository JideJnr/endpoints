REPLACEMENTS = {
    " fc": "",
    " cf": "",
    " sc": "",
    " afc": "",
    "manchester city": "man city",
    "manchester united": "man utd",
    "nottingham forest": "nott forest",
    "tottenham hotspur": "tottenham",
    "wolverhampton": "wolves",
    "newcastle united": "newcastle",
    "brighton & hove albion": "brighton",
    "brighton and hove albion": "brighton",
    "atletico de madrid": "atletico madrid",
    "real betis balompie": "betis",
    "real betis": "betis",
    "deportivo alaves": "alaves",
    "borussia moenchengladbach": "gladbach",
    "borussia mgladbach": "gladbach",
    "bayer 04 leverkusen": "leverkusen",
    "bayer leverkusen": "leverkusen",
    "rb leipzig": "leipzig",
    "eintracht frankfurt": "frankfurt",
    "ac milan": "milan",
    "inter milan": "inter",
    "fc internazionale": "inter",
    "paris saint-germain": "psg",
    "paris sg": "psg",
    "olympique de marseille": "marseille",
    "olympique lyonnais": "lyon",
    "sporting cp": "sporting",
    "bodoe/glimt": "bodo glimt",
    "bodø/glimt": "bodo glimt",
}


def normalise(name: str) -> str:
    text = (name or "").lower().strip()
    for source, target in REPLACEMENTS.items():
        text = text.replace(source, target)
    return " ".join(text.split())
